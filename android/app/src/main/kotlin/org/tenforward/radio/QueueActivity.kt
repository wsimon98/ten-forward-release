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
import java.util.concurrent.Executors

/**
 * What is lined up, and the two things you want to do to it: play one now, or take one out.
 *
 * Before this the line was write-only from the car - the app could add a song and wipe the whole
 * list, and nothing else, so one mis-hold could only be undone by clearing everything. The line
 * itself lives on the server, so this is the same list the laptop is looking at.
 */
class QueueActivity : Activity() {

    companion object {
        fun open(ctx: Context) = ctx.startActivity(Intent(ctx, QueueActivity::class.java))
    }

    private lateinit var list: RecyclerView
    private lateinit var msg: TextView
    private lateinit var adapter: QueueAdapter
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Prefs.init(this)
        ErrorLog.init(this)
        setContentView(R.layout.activity_queue)
        Screen.full(this)

        list = findViewById(R.id.list)
        msg = findViewById(R.id.msg)
        adapter = QueueAdapter(::playNow, ::drop)
        list.layoutManager = LinearLayoutManager(this)
        list.adapter = adapter

        findViewById<View>(R.id.btn_close).setOnClickListener { finish() }
        findViewById<View>(R.id.btn_clear).setOnClickListener { clearAll() }
        load()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        Screen.keep(this, hasFocus)
    }

    private fun load() {
        msg.visibility = View.VISIBLE
        msg.text = "Looking…"
        io.execute {
            try {
                if (Api.base == null) Api.resolve(true)
                val q = Api.queue()
                main.post {
                    adapter.submit(q)
                    msg.visibility = if (q.isEmpty()) View.VISIBLE else View.GONE
                    if (q.isEmpty()) msg.text = "Nothing lined up. Open SONGS on a channel and hold one."
                }
            } catch (e: Exception) {
                main.post { msg.visibility = View.VISIBLE; msg.text = why(e) }
            }
        }
    }

    private fun why(e: Exception): String = when (e) {
        is Api.Unreachable -> "Cannot reach Ten Forward right now."
        is Api.AuthNeeded -> "Sign in on the main screen first."
        else -> e.message ?: "Something went wrong."
    }

    /** Take it out of the line and play it straight away, so the line does not serve it twice. */
    private fun playNow(q: Api.Queued) {
        io.execute {
            try {
                Api.queueDrop(q.queueId)
            } catch (e: Exception) {
                // if it could not be removed the radio will simply reach it in its own time
            }
        }
        PlaybackService.instance?.playNow(q.song, Radio.stationId, Radio.stationName)
            ?: run {
                startService(Intent(this, PlaybackService::class.java))
                main.postDelayed({ PlaybackService.instance?.playNow(q.song, Radio.stationId, Radio.stationName) }, 700)
            }
        Toast.makeText(this, "Playing: ${q.song.title}", Toast.LENGTH_SHORT).show()
        finish()
    }

    private fun drop(q: Api.Queued) {
        io.execute {
            try {
                Api.queueDrop(q.queueId)
                main.post { load(); PlaybackService.instance?.requeue() }
            } catch (e: Exception) {
                main.post { Toast.makeText(this, why(e), Toast.LENGTH_LONG).show() }
            }
        }
    }

    private fun clearAll() {
        io.execute {
            try {
                Api.queueClear()
                main.post {
                    adapter.submit(emptyList())
                    msg.visibility = View.VISIBLE
                    msg.text = "Nothing lined up."
                    PlaybackService.instance?.requeue()
                    Toast.makeText(this, "Queue cleared", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                main.post { Toast.makeText(this, why(e), Toast.LENGTH_LONG).show() }
            }
        }
    }
}

/** A row per lined-up song: its place in the line, what it is, and a way out. */
class QueueAdapter(
    private val onTap: (Api.Queued) -> Unit,
    private val onDrop: (Api.Queued) -> Unit,
) : RecyclerView.Adapter<QueueAdapter.Row>() {

    private val items = ArrayList<Api.Queued>()

    fun submit(list: List<Api.Queued>) {
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
        val q = items[position]
        val ctx = holder.itemView.context
        val cover = q.song.coverLine()
        holder.title.text = "${position + 1}.  ${q.song.title}"
        holder.sub.text = cover ?: q.song.style
        holder.sub.setTextColor(Lcars.color(ctx, if (cover != null) "lav" else "muted"))
        holder.bar.setBackgroundColor(Lcars.color(ctx, "teal"))
        holder.dur.text = fmt(q.song.durationS)
        holder.queue.text = "✕"
        holder.queue.setTextColor(Lcars.color(ctx, "salmon"))
        holder.queue.setOnClickListener { onDrop(q) }
        holder.itemView.setOnClickListener { onTap(q) }
    }

    private fun fmt(seconds: Double): String {
        val t = seconds.toInt().coerceAtLeast(0)
        return "%d:%02d".format(t / 60, t % 60)
    }
}
