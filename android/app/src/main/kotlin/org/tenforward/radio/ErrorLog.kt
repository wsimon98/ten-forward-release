package org.tenforward.radio

import android.content.Context
import android.os.Build
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

/**
 * What went wrong, written down. One line of JSON per problem in the app's own folder, nothing older
 * than two days kept, and a copy sent to Ten Forward when it can be reached. Settings shows the list.
 */
object ErrorLog {

    const val KEEP_HOURS = 48
    private const val KEEP_MS = KEEP_HOURS * 60L * 60L * 1000L
    private const val MAX_LINES = 400

    data class Entry(val at: Long, val level: String, val tag: String, val message: String, val detail: String) {
        fun when_(): String = SimpleDateFormat("MMM d, HH:mm:ss", Locale.getDefault()).format(Date(at))
        fun line(): String = "${when_()}  [$tag] $message" + if (detail.isEmpty()) "" else "\n$detail"
        fun json(): JSONObject = JSONObject()
            .put("at", at).put("level", level).put("tag", tag)
            .put("message", message).put("detail", detail)
    }

    private lateinit var file: File
    private val io = Executors.newSingleThreadExecutor()
    private val lock = Any()
    @Volatile private var sending = false     // never log the failure of sending a log

    fun init(ctx: Context) {
        if (::file.isInitialized) return
        file = File(ctx.applicationContext.filesDir, "problems.log")
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { t, e ->
            try {
                write(Entry(System.currentTimeMillis(), "crash", "app", e.toString(), stack(e) + "\nthread ${t.name}"))
            } catch (ignored: Throwable) {
            }
            previous?.uncaughtException(t, e)
        }
        io.execute { prune() }
    }

    fun note(tag: String, message: String, t: Throwable? = null, level: String = "error") {
        if (!::file.isInitialized) return
        val e = Entry(System.currentTimeMillis(), level, tag, message.take(400), t?.let { stack(it) } ?: "")
        io.execute {
            write(e)
            if (Prefs.reportProblems && !sending) send(listOf(e))
        }
    }

    /** Newest first, already pruned to the last two days. */
    fun all(): List<Entry> {
        if (!::file.isInitialized || !file.exists()) return emptyList()
        val cutoff = System.currentTimeMillis() - KEEP_MS
        val out = ArrayList<Entry>()
        synchronized(lock) {
            file.forEachLine { line ->
                val e = parse(line) ?: return@forEachLine
                if (e.at >= cutoff) out.add(e)
            }
        }
        out.reverse()
        return out
    }

    fun clear() {
        if (!::file.isInitialized) return
        synchronized(lock) { file.delete() }
    }

    fun text(): String {
        val head = "Ten Forward app ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE}) · " +
            "${Build.MANUFACTURER} ${Build.MODEL} · Android ${Build.VERSION.RELEASE}"
        val body = all().joinToString("\n\n") { it.line() }
        return if (body.isEmpty()) "$head\n\nNothing has gone wrong in the last $KEEP_HOURS hours." else "$head\n\n$body"
    }

    /** Hand the whole list to Ten Forward, one line each. Returns how many went. */
    fun send(entries: List<Entry>): Int {
        if (entries.isEmpty()) return 0
        sending = true
        var sent = 0
        try {
            if (Api.base == null && Api.resolve(true) == null) return 0
            for (e in entries) {
                val j = e.json()
                    .put("event", "android-problem")
                    .put("source", "android")
                    .put("device", "${Build.MANUFACTURER} ${Build.MODEL}")
                    .put("app_version", "${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})")
                if (Api.report(j)) sent++
            }
        } catch (ignored: Exception) {
            // the phone keeps its own copy either way
        } finally {
            sending = false
        }
        return sent
    }

    // ---------------------------------------------------------------- the file itself
    private fun write(e: Entry) {
        synchronized(lock) {
            try {
                file.appendText(e.json().toString() + "\n")
            } catch (ignored: Exception) {
                return
            }
        }
        prune()
    }

    /** Two days, and never more than a few hundred lines, so the file cannot grow without end. */
    private fun prune() {
        synchronized(lock) {
            if (!file.exists()) return
            val cutoff = System.currentTimeMillis() - KEEP_MS
            val kept = try {
                file.readLines().mapNotNull { parse(it) }.filter { it.at >= cutoff }.takeLast(MAX_LINES)
            } catch (e: Exception) {
                return
            }
            try {
                file.writeText(kept.joinToString("\n", postfix = if (kept.isEmpty()) "" else "\n") { it.json().toString() })
            } catch (ignored: Exception) {
            }
        }
    }

    private fun parse(line: String): Entry? = try {
        val j = JSONObject(line)
        Entry(j.optLong("at"), j.optString("level", "error"), j.optString("tag"), j.optString("message"), j.optString("detail"))
    } catch (e: Exception) {
        null
    }

    private fun stack(t: Throwable): String {
        val sw = java.io.StringWriter()
        t.printStackTrace(java.io.PrintWriter(sw))
        return sw.toString().take(2000)
    }
}
