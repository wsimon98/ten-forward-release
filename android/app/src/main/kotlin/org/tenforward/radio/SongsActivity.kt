package org.tenforward.radio

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import android.widget.Toast
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import java.net.URLEncoder
import java.util.concurrent.Executors

/**
 * Pick a song off a channel instead of taking whatever the radio hands you.
 *
 * Two gestures, both big enough to use without looking:
 *   tap  — play it now, and stay on that channel afterwards
 *   hold — line it up, so it plays after what is on now
 *
 * The line itself lives on the server (/api/playqueue), not in this app, which is why a song
 * lined up on the laptop plays here and vice versa. Nothing in this app has to keep a queue:
 * /api/radio/next hands lined-up songs out first all by itself.
 */
class SongsActivity : Activity() {

    companion object {
        const val EXTRA_STATION = "station_id"
        const val EXTRA_NAME = "station_name"
        const val EXTRA_LIKED = "liked"

        /** liked = only the hearted songs (of that channel, or of every channel for "all"), with a switch between the two. */
        fun open(ctx: Context, stationId: String, stationName: String, liked: Boolean = false) {
            ctx.startActivity(
                Intent(ctx, SongsActivity::class.java)
                    .putExtra(EXTRA_STATION, stationId)
                    .putExtra(EXTRA_NAME, stationName)
                    .putExtra(EXTRA_LIKED, liked)
            )
        }
    }

    private var liked = false
    private var allChannels = false
    private var homeId = "all"
    private var homeName = "All channels"
    private lateinit var scope: TextView

    private lateinit var list: RecyclerView
    private lateinit var msg: TextView
    private lateinit var qn: TextView
    private lateinit var adapter: SongAdapter

    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    private var stationId = "all"
    private var stationName = "All songs"
    private var queued = HashSet<String>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Prefs.init(this)
        ErrorLog.init(this)
        setContentView(R.layout.activity_songs)
        Screen.full(this)

        stationId = intent.getStringExtra(EXTRA_STATION) ?: "all"
        stationName = intent.getStringExtra(EXTRA_NAME) ?: "All songs"
        liked = intent.getBooleanExtra(EXTRA_LIKED, false)
        homeId = stationId
        homeName = stationName
        allChannels = liked && stationId == "all"

        list = findViewById(R.id.list)
        msg = findViewById(R.id.msg)
        qn = findViewById(R.id.qn)
        scope = findViewById(R.id.btn_scope)
        adapter = SongAdapter(::playNow, ::lineUp) { queued.contains(it) }
        list.layoutManager = LinearLayoutManager(this)
        list.adapter = adapter

        findViewById<View>(R.id.btn_close).setOnClickListener { finish() }
        findViewById<View>(R.id.btn_clear).setOnClickListener { clearLine() }
        // hearted songs: this channel's, or everybody's. Only when there is a channel to switch from.
        scope.visibility = if (liked && homeId != "all") View.VISIBLE else View.GONE
        scope.setOnClickListener {
            allChannels = !allChannels
            heading()
            load()
        }
        heading()
        load()
    }

    private fun heading() {
        val t = findViewById<TextView>(R.id.title)
        if (!liked) {
            t.text = stationName.uppercase()
            return
        }
        t.text = "♥ " + (if (allChannels) "ALL CHANNELS" else homeName.uppercase())
        scope.text = if (allChannels) "THIS CHANNEL" else "ALL"
        findViewById<TextView>(R.id.hint).text =
            "Hearted songs. Tap one to play it now; the radio stays on " + (if (homeId == "all") "its channel" else homeName) + " and keeps making new ones."
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        Screen.keep(this, hasFocus)
    }

    // ------------------------------------------------------------------ loading
    private fun load() {
        msg.visibility = View.VISIBLE
        msg.text = "Looking…"
        io.execute {
            try {
                if (Api.base == null) Api.resolve(true)
                val songs = if (liked) Api.favoriteSongs(if (allChannels) "all" else homeId).mapNotNull { Song.from(it) }
                            else Api.songs(stationId)
                val line = Api.queueIds()
                main.post {
                    queued = HashSet(line)
                    adapter.submit(songs)
                    msg.visibility = if (songs.isEmpty()) View.VISIBLE else View.GONE
                    if (songs.isEmpty()) msg.text = if (liked) "Nothing hearted here yet." else "No songs on $stationName yet."
                    showLine(line.size)
                }
            } catch (e: Exception) {
                // out of reach: the hearted songs kept on the phone are still something to pick from
                val kept = if (liked) Keep.favorites(if (allChannels) "all" else homeId) else emptyList()
                main.post {
                    adapter.submit(kept)
                    msg.visibility = View.VISIBLE
                    msg.text = if (kept.isNotEmpty()) "Can't reach Ten Forward: these are the kept songs on the phone." else why(e)
                }
            }
        }
    }

    private fun showLine(n: Int) {
        qn.text = if (n > 0) "$n lined up" else "Nothing lined up yet"
    }

    private fun why(e: Exception): String = when (e) {
        is Api.Unreachable -> "Cannot reach Ten Forward right now."
        is Api.AuthNeeded -> "Sign in on the main screen first."
        else -> e.message ?: "Something went wrong."
    }

    // ------------------------------------------------------------------ the two gestures
    /** Tap: play it now, and leave the radio pointed at the channel it came from. */
    private fun playNow(song: Song) {
        // a hearted song picked from the list plays on the channel you are on, so the radio goes on
        // making new songs there behind it; from "all channels" with nothing on, its own channel
        var sid: String? = stationId
        var sname: String? = stationName
        if (liked) {
            if (homeId != "all") { sid = homeId; sname = homeName }
            else if (!Radio.stationId.isNullOrEmpty()) { sid = Radio.stationId; sname = Radio.stationName }
            else { sid = song.stationId.ifEmpty { null }; sname = null }
        }
        val svc = PlaybackService.instance
        if (svc == null) {
            startService(Intent(this, PlaybackService::class.java))
            main.postDelayed({ PlaybackService.instance?.playNow(song, sid, sname) }, 700)
        } else {
            svc.playNow(song, sid, sname)
        }
        Toast.makeText(this, "Playing: ${song.title}", Toast.LENGTH_SHORT).show()
        finish()
    }

    /** Hold: line it up behind whatever is on. */
    private fun lineUp(song: Song) {
        if (queued.contains(song.id)) {
            Toast.makeText(this, "Already lined up", Toast.LENGTH_SHORT).show()
            return
        }
        queued.add(song.id)
        adapter.notifyDataSetChanged()
        io.execute {
            try {
                val n = Api.queueAdd(song.id)
                // the radio had already loaded its own guess for what comes next; drop it so the
                // song just lined up is genuinely the next thing heard, not the one after that
                main.post {
                    PlaybackService.instance?.requeue()
                    showLine(n)
                    Toast.makeText(this, "Lined up: ${song.title}", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                main.post {
                    queued.remove(song.id)
                    adapter.notifyDataSetChanged()
                    Toast.makeText(this, why(e), Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    private fun clearLine() {
        io.execute {
            try {
                Api.queueClear()
                main.post {
                    queued.clear()
                    adapter.notifyDataSetChanged()
                    showLine(0)
                    Toast.makeText(this, "Queue cleared", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                main.post { Toast.makeText(this, why(e), Toast.LENGTH_LONG).show() }
            }
        }
    }
}

/** Tap plays, hold lines up. The "+" does the same as a hold, for anyone who would rather aim once. */
class SongAdapter(
    private val onTap: (Song) -> Unit,
    private val onHold: (Song) -> Unit,
    private val isQueued: (String) -> Boolean,
) : RecyclerView.Adapter<SongAdapter.Row>() {

    private val items = ArrayList<Song>()

    fun submit(list: List<Song>) {
        items.clear()
        items.addAll(list)
        notifyDataSetChanged()
    }

    class Row(v: View) : RecyclerView.ViewHolder(v) {
        val bar: View = v.findViewById(R.id.bar)
        val title: TextView = v.findViewById(R.id.title)
        val sub: TextView = v.findViewById(R.id.sub)
        val dur: TextView = v.findViewById(R.id.dur)
        val queue: TextView = v.findViewById(R.id.queue)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Row =
        Row(LayoutInflater.from(parent.context).inflate(R.layout.item_song, parent, false))

    override fun getItemCount(): Int = items.size

    override fun onBindViewHolder(holder: Row, position: Int) {
        val s = items[position]
        val ctx = holder.itemView.context
        val cover = s.coverLine()
        holder.title.text = s.title
        // a cover is somebody else's song. It says so here instead of the style, because which
        // one you are hearing matters more than what it sounds like.
        holder.sub.text = cover ?: s.style
        holder.sub.setTextColor(Lcars.color(ctx, if (cover != null) "lav" else "muted"))
        holder.bar.setBackgroundColor(Lcars.color(ctx, if (s.liked) "pink" else "teal"))
        holder.dur.text = fmt(s.durationS)

        val inLine = isQueued(s.id)
        holder.queue.text = if (inLine) "✓" else "+"
        holder.queue.setTextColor(Lcars.color(ctx, if (inLine) "gold" else "teal"))
        holder.queue.setOnClickListener { onHold(s) }

        holder.itemView.setOnClickListener { onTap(s) }
        holder.itemView.setOnLongClickListener {
            it.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS)
            onHold(s)
            true
        }
    }

    private fun fmt(seconds: Double): String {
        val t = seconds.toInt().coerceAtLeast(0)
        return "%d:%02d".format(t / 60, t % 60)
    }
}
