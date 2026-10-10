package org.tenforward.radio

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors

/**
 * Watches the computer for a channel being filled for the first time.
 *
 * A channel made a minute ago has nothing on it, and while it fills the server deliberately makes nothing
 * else — so the honest thing to do is say so, say roughly how long, and say to put another channel on
 * meanwhile. When it is done a notification arrives the way a text message does: at the top of the screen
 * and then waiting in the shade until it is tapped.
 *
 * It runs while the app is open, and — because the player is a foreground service — while music is playing
 * with the screen off, which is the case this is for. Nothing runs when the app is closed and nothing is
 * playing: Ten Forward lives on the home network with no push service, so there is nothing to wake.
 */
object Watch {
    interface Listener {
        fun onBuilding()
        fun onChannelReady(id: String, name: String) {}
    }

    private const val CHANNEL = "station_ready"
    private const val FAST_MS = 12_000L     // while something is being built
    private const val SLOW_MS = 45_000L     // the rest of the time

    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val listeners = CopyOnWriteArrayList<Listener>()
    private var watchers = 0
    private var tick: Runnable? = null
    private var app: Context? = null

    @Volatile private var polling = false

    /** The line for the screen. Empty when nothing is being built. */
    @Volatile
    var line: String = ""
        private set

    fun add(l: Listener) {
        listeners.addIfAbsent(l)
    }

    fun remove(l: Listener) {
        listeners.remove(l)
    }

    /** Called by whatever is alive — the screen, the player, or both. The last one out turns it off. */
    fun begin(ctx: Context) {
        app = ctx.applicationContext
        Prefs.init(ctx)
        watchers++
        if (tick == null) {
            poll()
            schedule()
        }
    }

    fun end() {
        watchers--
        if (watchers > 0) return
        watchers = 0
        tick?.let { main.removeCallbacks(it) }
        tick = null
    }

    private fun schedule() {
        tick?.let { main.removeCallbacks(it) }
        val r = Runnable {
            poll()
            schedule()
        }
        tick = r
        main.postDelayed(r, if (line.isEmpty()) SLOW_MS else FAST_MS)
    }

    private fun poll() {
        if (polling) return
        polling = true
        io.execute {
            try {
                if (Api.base == null) Api.resolve(true)
                if (Api.base != null) read(Api.status())
            } catch (e: Exception) {
                // the radio says for itself when the computer is unreachable; this is only the banner
            } finally {
                polling = false
            }
        }
    }

    private fun read(s: JSONObject) {
        val b = s.optJSONObject("building") ?: JSONObject()
        val building = b.optJSONArray("stations")
        var text = ""
        if (building != null && building.length() > 0) {
            val st = building.getJSONObject(0)
            val ready = st.optInt("ready")
            val target = st.optInt("target", 5)
            text = "Building ${st.optString("name")} — $ready of $target songs, ${aboutMins(st.optInt("eta_s"))} left. " +
                "Nothing else is being made until it is done, so put another channel on meanwhile."
        }
        if (text != line) {
            line = text
            main.post { for (l in listeners) l.onBuilding() }
        }
        val done = b.optJSONArray("ready") ?: return
        for (i in 0 until done.length()) {
            val r = done.optJSONObject(i) ?: continue
            val id = r.optString("id")
            if (id.isEmpty()) continue
            val key = id + ":" + Math.round(r.optDouble("at", 0.0))
            if (Prefs.saidReady.contains(key)) continue
            Prefs.rememberSaid(key)
            val name = r.optString("name").ifEmpty { id }
            announce(id, name, r.optInt("songs"))
            main.post { for (l in listeners) l.onChannelReady(id, name) }
        }
    }

    private fun aboutMins(seconds: Int): String {
        if (seconds < 45) return "under a minute"
        val m = Math.round(seconds / 60.0).toInt()
        return if (m <= 1) "about a minute" else "about $m minutes"
    }

    /** The ordinary kind: it slides down from the top and then waits in the shade until it is tapped. */
    private fun announce(id: String, name: String, songs: Int) {
        val ctx = app ?: return
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        val ch = NotificationChannel(CHANNEL, "A channel is ready", NotificationManager.IMPORTANCE_HIGH)
        ch.description = "When a new channel has finished making its first songs"
        ch.enableVibration(true)
        nm.createNotificationChannel(ch)

        val open = Intent(ctx, MainActivity::class.java)
            .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra(MainActivity.EXTRA_TUNE, id)
        val pi = PendingIntent.getActivity(
            ctx, id.hashCode(), open,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val body = "It made its first $songs song${if (songs == 1) "" else "s"}. Tap to tune in."
        val n = Notification.Builder(ctx, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_radio)
            .setContentTitle("$name is ready")
            .setContentText(body)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setContentIntent(pi)
            .setAutoCancel(true)
            .setCategory(Notification.CATEGORY_STATUS)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .build()
        nm.notify(id.hashCode(), n)
    }
}
