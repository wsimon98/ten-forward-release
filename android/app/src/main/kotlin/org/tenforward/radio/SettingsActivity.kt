package org.tenforward.radio

import android.app.Activity
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import java.util.concurrent.Executors

/** The two addresses, who is listening, this app's own version, and what has gone wrong lately. */
class SettingsActivity : Activity() {

    private lateinit var fLan: EditText
    private lateinit var fTail: EditText
    private lateinit var fName: EditText
    private lateinit var fPass: EditText
    private lateinit var out: TextView
    private lateinit var who: TextView
    private lateinit var updateOut: TextView
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Prefs.init(this)
        ErrorLog.init(this)
        setContentView(R.layout.activity_settings)
        Screen.full(this)

        fLan = findViewById(R.id.f_lan)
        fTail = findViewById(R.id.f_tail)
        fName = findViewById(R.id.f_name)
        fPass = findViewById(R.id.f_pass)
        out = findViewById(R.id.test_out)
        who = findViewById(R.id.who)
        updateOut = findViewById(R.id.update_out)

        fLan.setText(Prefs.lanUrl)
        fTail.setText(Prefs.tailUrl)
        fName.setText(if (Prefs.userName.isNotEmpty()) Prefs.userName else "admin")
        showWho()

        findViewById<View>(R.id.btn_save).setOnClickListener { save(true) }
        findViewById<View>(R.id.btn_test).setOnClickListener { test() }
        findViewById<View>(R.id.btn_signin).setOnClickListener { signIn() }
        findViewById<View>(R.id.btn_signout).setOnClickListener { signOut() }
        findViewById<View>(R.id.btn_update).setOnClickListener {
            UpdateFlow.check(this, io, main, tellWhenCurrent = true) { line -> updateOut.text = line }
        }
        findViewById<View>(R.id.btn_problems).setOnClickListener {
            startActivity(android.content.Intent(this, ProblemsActivity::class.java))
        }

        findViewById<TextView>(R.id.version).text =
            "Ten Forward app " + BuildConfig.VERSION_NAME + " (build " + BuildConfig.VERSION_CODE + ")"
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        Screen.keep(this, hasFocus)
    }

    private fun showWho() {
        who.text = when {
            Prefs.token.isNotEmpty() -> "Signed in as ${Prefs.userName}."
            else -> "Not signed in. If your Ten Forward has only one person and no password, you do not need to."
        }
    }

    private fun save(toast: Boolean) {
        Prefs.lanUrl = Api.clean(fLan.text.toString())
        Prefs.tailUrl = Api.clean(fTail.text.toString())
        fLan.setText(Prefs.lanUrl)
        fTail.setText(Prefs.tailUrl)
        Api.base = null
        if (toast) {
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
            test()
        }
    }

    private fun test() {
        save(false)
        out.text = "Asking…"
        io.execute {
            val lan = Api.hello(Prefs.lanUrl)
            val tail = Api.hello(Prefs.tailUrl)
            val lines = ArrayList<String>()
            lines.add(
                when {
                    Prefs.lanUrl.isEmpty() -> "Own wifi: nothing typed in"
                    lan != null -> "Own wifi: Ten Forward " + lan.optString("version") + " answers"
                    else -> "Own wifi: no answer"
                }
            )
            lines.add(
                when {
                    Prefs.tailUrl.isEmpty() -> "Away: nothing typed in"
                    tail != null -> "Away: Ten Forward " + tail.optString("version") + " answers"
                    else -> "Away: no answer"
                }
            )
            val hello = lan ?: tail
            if (hello != null && hello.optBoolean("login_required", false) && Prefs.token.isEmpty()) {
                lines.add("This one asks who is listening: sign in below.")
            }
            Api.resolve(true)
            main.post { out.text = lines.joinToString("\n") }
        }
    }

    private fun signIn() {
        save(false)
        val name = fName.text.toString().trim()
        if (name.isEmpty()) {
            Toast.makeText(this, "A name first", Toast.LENGTH_SHORT).show()
            return
        }
        val pass = fPass.text.toString()
        out.text = "Signing in…"
        io.execute {
            try {
                if (Api.resolve(true) == null) throw Api.Unreachable()
                Api.login(name, pass, android.os.Build.MODEL ?: "Android")
                main.post {
                    fPass.setText("")
                    out.text = "Signed in as $name"
                    showWho()
                }
            } catch (e: Exception) {
                main.post { out.text = e.message ?: "That did not work" }
            }
        }
    }

    private fun signOut() {
        io.execute {
            Api.logout()
            main.post {
                out.text = "Signed out"
                showWho()
            }
        }
    }
}
