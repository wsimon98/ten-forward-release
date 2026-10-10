package org.tenforward.radio

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import java.util.concurrent.Executors

/**
 * Everything that has gone wrong in the last two days, in plain words: the app writes them down as they
 * happen, sends a copy to Ten Forward when it can, and keeps nothing older than that.
 */
class ProblemsActivity : Activity() {

    private lateinit var body: TextView
    private lateinit var count: TextView
    private lateinit var swReport: Switch
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Prefs.init(this)
        ErrorLog.init(this)
        setContentView(R.layout.activity_problems)
        Screen.full(this)

        body = findViewById(R.id.body)
        count = findViewById(R.id.count)
        swReport = findViewById(R.id.sw_report)
        swReport.isChecked = Prefs.reportProblems
        swReport.setOnCheckedChangeListener { _, on -> Prefs.reportProblems = on }

        findViewById<View>(R.id.btn_send).setOnClickListener { send() }
        findViewById<View>(R.id.btn_copy).setOnClickListener { copy() }
        findViewById<View>(R.id.btn_share).setOnClickListener { share() }
        findViewById<View>(R.id.btn_clear).setOnClickListener { clear() }
        findViewById<View>(R.id.btn_close).setOnClickListener { finish() }
        show()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        Screen.keep(this, hasFocus)
    }

    private fun show() {
        val entries = ErrorLog.all()
        count.text = when (entries.size) {
            0 -> "Nothing in the last ${ErrorLog.KEEP_HOURS} hours."
            1 -> "1 problem in the last ${ErrorLog.KEEP_HOURS} hours."
            else -> "${entries.size} problems in the last ${ErrorLog.KEEP_HOURS} hours."
        }
        body.text = ErrorLog.text()
    }

    private fun send() {
        val entries = ErrorLog.all()
        if (entries.isEmpty()) {
            Toast.makeText(this, "Nothing to send", Toast.LENGTH_SHORT).show()
            return
        }
        count.text = "Sending…"
        io.execute {
            val sent = ErrorLog.send(entries)
            main.post {
                Toast.makeText(
                    this,
                    if (sent > 0) "Sent $sent to Ten Forward — they show up in Settings on the web page" else "Could not reach Ten Forward",
                    Toast.LENGTH_LONG,
                ).show()
                show()
            }
        }
    }

    private fun copy() {
        val cb = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cb.setPrimaryClip(ClipData.newPlainText("Ten Forward problems", ErrorLog.text()))
        Toast.makeText(this, "Copied", Toast.LENGTH_SHORT).show()
    }

    private fun share() {
        val i = Intent(Intent.ACTION_SEND)
            .setType("text/plain")
            .putExtra(Intent.EXTRA_SUBJECT, "Ten Forward app problems")
            .putExtra(Intent.EXTRA_TEXT, ErrorLog.text())
        startActivity(Intent.createChooser(i, "Send the list"))
    }

    private fun clear() {
        ErrorLog.clear()
        show()
        Toast.makeText(this, "Cleared", Toast.LENGTH_SHORT).show()
    }
}
