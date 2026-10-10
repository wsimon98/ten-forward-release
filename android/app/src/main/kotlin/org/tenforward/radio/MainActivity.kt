package org.tenforward.radio

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.common.util.concurrent.ListenableFuture
import java.util.concurrent.Executors

/**
 * The whole app: a list of channels, and what is playing. Tap a channel to tune to it, hold one to change
 * it. Full screen — no status bar, no buttons bar — because it is a radio, not a phone.
 */
class MainActivity : Activity(), Radio.Listener, Watch.Listener {

    private lateinit var dial: DialView
    private lateinit var list: RecyclerView
    private lateinit var adapter: StationAdapter
    private lateinit var chip: TextView
    private lateinit var msg: TextView
    private lateinit var building: TextView
    private lateinit var nowTitle: TextView
    private lateinit var nowSub: TextView
    private lateinit var btnPlay: TextView
    private lateinit var btnLike: TextView
    private lateinit var seek: android.widget.SeekBar
    private lateinit var tNow: TextView
    private lateinit var tLeft: TextView
    private var dragging = false
    private var ticker: Runnable? = null

    private var controllerFuture: ListenableFuture<MediaController>? = null
    private var controller: MediaController? = null
    private var stations: List<Station> = emptyList()
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var askedAboutUpdate = false
    private var saveWhenAllowed = false
    private var tuneWhenLoaded: String? = null

    companion object {
        /** Put on the intent by the "your channel is ready" notification: tune to it once the list is in. */
        const val EXTRA_TUNE = "tune_station"
        private const val ASK_NOTIFICATIONS = 1
        private const val ASK_STORAGE = 2
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Prefs.init(this)
        ErrorLog.init(this)
        setContentView(R.layout.activity_main)
        Screen.full(this)

        dial = findViewById(R.id.dial)
        list = findViewById(R.id.list)
        // a third of the screen, pinned, with the channels scrolling under it. On its side the layout gives
        // the dial everything instead, so leave its height alone there.
        if (resources.configuration.orientation == Configuration.ORIENTATION_PORTRAIT) {
            dial.layoutParams.height = resources.displayMetrics.heightPixels / 3
        }
        dial.onTune = { st -> service { it.tune(st) } }
        dial.onPlayPause = { onPlayTapped() }
        chip = findViewById(R.id.chip_conn)
        msg = findViewById(R.id.msg)
        building = findViewById(R.id.building)
        nowTitle = findViewById(R.id.now_title)
        nowSub = findViewById(R.id.now_sub)
        btnPlay = findViewById(R.id.btn_play)
        btnLike = findViewById(R.id.btn_like)
        seek = findViewById(R.id.seek)
        tNow = findViewById(R.id.t_now)
        tLeft = findViewById(R.id.t_left)
        seek.setOnSeekBarChangeListener(object : android.widget.SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: android.widget.SeekBar, p: Int, fromUser: Boolean) {}
            override fun onStartTrackingTouch(sb: android.widget.SeekBar) { dragging = true }
            override fun onStopTrackingTouch(sb: android.widget.SeekBar) {
                dragging = false
                val total = PlaybackService.instance?.position()?.second ?: 0L
                if (total > 0) service { it.seekWithin(total * sb.progress / 1000) }
            }
        })

        list.layoutManager = LinearLayoutManager(this)
        adapter = StationAdapter(
            onTap = { st -> service { it.tune(st) } },
            onHold = { st -> edit(st) },
            onSongs = { st -> songs(st) },
        )
        list.adapter = adapter

        findViewById<View>(R.id.btn_settings).setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        findViewById<View>(R.id.btn_add).setOnClickListener { edit(null) }
        findViewById<View>(R.id.btn_prev).setOnClickListener { service { it.previous() } }
        findViewById<View>(R.id.btn_play).setOnClickListener { onPlayTapped() }
        findViewById<View>(R.id.btn_next).setOnClickListener { service { it.skip() } }
        findViewById<View>(R.id.btn_like).setOnClickListener { service { it.heart() } }
        findViewById<View>(R.id.btn_queue).setOnClickListener { QueueActivity.open(this) }
        findViewById<View>(R.id.btn_down).setOnClickListener { saveSong() }
        findViewById<View>(R.id.btn_sleep).setOnClickListener { sleepMenu() }

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), ASK_NOTIFICATIONS)
        }

        wantTune(intent)

        if (Prefs.lanUrl.isEmpty() && Prefs.tailUrl.isEmpty()) {
            Toast.makeText(this, "Type in the addresses of your Ten Forward", Toast.LENGTH_LONG).show()
            startActivity(Intent(this, SettingsActivity::class.java))
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        wantTune(intent)
    }

    /** The notification says which channel; the channel itself only arrives with the next list. */
    private fun wantTune(from: Intent?) {
        val id = from?.getStringExtra(EXTRA_TUNE) ?: return
        from.removeExtra(EXTRA_TUNE)
        val known = stations.firstOrNull { it.id == id }
        if (known != null) service { it.tune(known) } else tuneWhenLoaded = id
    }

    /** SONGS on a channel row: pick something off it instead of taking what the radio hands you. */
    private fun songs(station: Station) = SongsActivity.open(this, station.id, station.name)

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        Screen.keep(this, hasFocus)
    }

    override fun onStart() {
        super.onStart()
        startTicking()
        val token = SessionToken(this, ComponentName(this, PlaybackService::class.java))
        val future = MediaController.Builder(this, token).buildAsync()
        controllerFuture = future
        future.addListener({
            controller = try {
                future.get()
            } catch (e: Exception) {
                null
            }
        }, ContextCompat.getMainExecutor(this))
        Radio.add(this)
        Watch.add(this)
        Watch.begin(this)
        onRadio()
        onBuilding()
        refresh()
    }

    override fun onStop() {
        stopTicking()          // a ticker in a car app must not keep running once the screen is gone
        Radio.remove(this)
        Watch.remove(this)
        Watch.end()
        controllerFuture?.let { MediaController.releaseFuture(it) }
        controllerFuture = null
        controller = null
        super.onStop()
    }

    // ---------------------------------------------------------------- the server
    private fun refresh() {
        io.execute {
            val base = Api.resolve(true)
            if (base == null) {
                main.post {
                    setChip("OFFLINE", R.color.red)
                    setMsg(
                        if (Prefs.lanUrl.isEmpty() && Prefs.tailUrl.isEmpty()) "Open the gear and type in your two addresses."
                        else "Cannot reach Ten Forward. Is the computer on? Check the addresses under the gear."
                    )
                }
                return@execute
            }
            try {
                val sts = Api.stations()
                main.post {
                    stations = sts
                    setChip(if (Api.via == "wifi") "WIFI" else "AWAY", if (Api.via == "wifi") R.color.green else R.color.blue)
                    setMsg(null)
                    adapter.submit(sts)
                    dial.submit(sts, Radio.stationId ?: Prefs.lastStation)
                    render()
                    tuneWhenLoaded?.let { want ->
                        val st = sts.firstOrNull { it.id == want }
                        if (st != null) {
                            tuneWhenLoaded = null
                            service { it.tune(st) }
                        }
                    }
                }
                lookForUpdate()
            } catch (e: Api.AuthNeeded) {
                main.post {
                    setChip("SIGN IN", R.color.gold)
                    setMsg("Somebody has to say who is listening. Open the gear and sign in.")
                }
            } catch (e: Exception) {
                main.post { setMsg(e.message ?: "Could not load the channels") }
            }
        }
    }

    /** The service is created by the media session connection; tapping before that is rare but possible. */
    private fun service(action: (PlaybackService) -> Unit) {
        val s = PlaybackService.instance
        if (s != null) {
            action(s)
            return
        }
        startService(Intent(this, PlaybackService::class.java))
        main.postDelayed({
            val late = PlaybackService.instance
            if (late != null) action(late) else Toast.makeText(this, "The player is still starting", Toast.LENGTH_SHORT).show()
        }, 700)
    }

    private fun onPlayTapped() {
        if (Radio.song == null) {
            val last = stations.firstOrNull { it.id == Prefs.lastStation } ?: stations.firstOrNull()
            if (last != null) service { it.tune(last) } else Toast.makeText(this, "Pick a channel", Toast.LENGTH_SHORT).show()
        } else {
            service { it.playPause() }
        }
    }

    private fun sleepMenu() {
        val items = arrayOf("Stop after this song", "In 15 minutes", "In 30 minutes", "In an hour", "Off")
        AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
            .setTitle("Sleep timer")
            .setItems(items) { _, which ->
                val minutes = when (which) {
                    0 -> -1
                    1 -> 15
                    2 -> 30
                    3 -> 60
                    else -> 0
                }
                service { it.sleep(minutes) }
            }
            .show()
    }

    // ---------------------------------------------------------------- changing a channel
    private fun edit(station: Station?) {
        val i = Intent(this, StationEditActivity::class.java)
        if (station != null) i.putExtra(StationEditActivity.EXTRA_STATION, station.raw.toString())
        startActivity(i)
    }

    // ---------------------------------------------------------------- keeping a song on the phone
    private fun saveSong() {
        val song = Radio.song
        if (song == null) {
            Toast.makeText(this, "Nothing is playing", Toast.LENGTH_SHORT).show()
            return
        }
        if (Build.VERSION.SDK_INT < 29 &&
            checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED
        ) {
            saveWhenAllowed = true
            requestPermissions(arrayOf(Manifest.permission.WRITE_EXTERNAL_STORAGE), ASK_STORAGE)
            return
        }
        Toast.makeText(this, "Saving \"${song.title}\"…", Toast.LENGTH_SHORT).show()
        io.execute {
            try {
                val where = Downloads.save(this, song)
                main.post { Toast.makeText(this, "Saved to $where", Toast.LENGTH_LONG).show() }
            } catch (e: Exception) {
                ErrorLog.note("download", "could not save \"${song.title}\"", e)
                main.post { Toast.makeText(this, "Could not save it: ${e.message}", Toast.LENGTH_LONG).show() }
            }
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, results)
        val granted = results.isNotEmpty() && results[0] == PackageManager.PERMISSION_GRANTED
        if (requestCode == ASK_STORAGE && saveWhenAllowed) {
            saveWhenAllowed = false
            if (granted) saveSong()
            else Toast.makeText(this, "Android has to allow saving to Music first", Toast.LENGTH_LONG).show()
        }
    }

    // ---------------------------------------------------------------- a newer app on the computer
    /** Asked once per opening, and only of the computer this app already talks to. */
    private fun lookForUpdate() {
        if (askedAboutUpdate) return
        askedAboutUpdate = true
        UpdateFlow.offerIfNewer(this, io, main)
    }

    // ---------------------------------------------------------------- a channel being built
    override fun onBuilding() {
        val line = Watch.line
        building.text = line
        building.visibility = if (line.isEmpty()) View.GONE else View.VISIBLE
    }

    /** The notification has already gone out; this is for somebody looking at the screen when it lands. */
    override fun onChannelReady(id: String, name: String) {
        Toast.makeText(this, "$name is ready", Toast.LENGTH_LONG).show()
        refresh()
    }

    // ---------------------------------------------------------------- drawing
    override fun onRadio() = render()

    /** One second at a time while the screen is on. Stopped in onStop - see startTicking's pair. */
    private fun startTicking() {
        stopTicking()
        val r = object : Runnable {
            override fun run() {
                drawPosition()
                nowTitle.postDelayed(this, 1000)
            }
        }
        ticker = r
        nowTitle.post(r)
    }

    private fun stopTicking() {
        ticker?.let { nowTitle.removeCallbacks(it) }
        ticker = null
    }

    private fun drawPosition() {
        val p = PlaybackService.instance?.position()
        val at = p?.first ?: 0L
        var total = p?.second ?: 0L
        if (total <= 0) total = ((Radio.song?.durationS ?: 0.0) * 1000).toLong()
        if (total <= 0) {
            seek.progress = 0
            tNow.text = "0:00"
            tLeft.text = ""
            return
        }
        if (!dragging) seek.progress = ((at.coerceAtMost(total) * 1000) / total).toInt()
        tNow.text = clock(at)
        tLeft.text = "-" + clock((total - at).coerceAtLeast(0L))
    }

    private fun clock(ms: Long): String {
        val s = (ms / 1000).toInt()
        return "%d:%02d".format(s / 60, s % 60)
    }

    private fun render() {
        val song = Radio.song
        nowTitle.text = song?.title ?: "Nothing playing"
        nowTitle.isSelected = true
        val bits = ArrayList<String>()
        if (Radio.stationName.isNotEmpty()) bits.add(Radio.stationName)
        if (Radio.status.isNotEmpty()) bits.add(Radio.status)
        else if (song != null) bits.add(song.coverLine() ?: song.style.take(60))
        nowSub.text = if (bits.isEmpty()) "Pick a channel" else bits.joinToString(" · ")
        btnPlay.text = if (Radio.playing) "❚❚" else "▶"
        btnLike.text = if (Radio.liked) "♥" else "♡"
        adapter.onAir = Radio.stationId
        adapter.notifyDataSetChanged()
        dial.tuned(Radio.stationId)
    }

    private fun setChip(text: String, colorRes: Int) {
        chip.text = text
        chip.background?.mutate()?.setTint(getColor(colorRes))
    }

    private fun setMsg(text: String?) {
        msg.text = text ?: ""
        msg.visibility = if (text.isNullOrEmpty()) View.GONE else View.VISIBLE
    }
}
