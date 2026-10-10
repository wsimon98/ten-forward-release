package org.tenforward.radio

import android.app.Activity
import android.app.AlertDialog
import android.os.Handler
import android.widget.Toast
import java.util.concurrent.Executor

/**
 * The asking and the downloading, shared by the first screen (which checks quietly when the app opens)
 * and Settings (where the person can ask for it). Android always has the last word on installing.
 */
object UpdateFlow {

    /**
     * @param tellWhenCurrent say so even when there is nothing newer (Settings does, the radio does not)
     * @param say where to write progress, if the screen has somewhere for it
     */
    fun check(a: Activity, io: Executor, main: Handler, tellWhenCurrent: Boolean, say: ((String) -> Unit)? = null) {
        say?.invoke("Asking the computer…")
        io.execute {
            val onServer = Updater.onServer()
            main.post {
                if (a.isFinishing) return@post
                when {
                    onServer == null ->
                        say?.invoke("Could not ask — is Ten Forward switched on?")
                            ?: Unit
                    onServer.build > BuildConfig.VERSION_CODE -> {
                        say?.invoke("${onServer.label()} is waiting.")
                        offer(a, onServer)
                    }
                    tellWhenCurrent ->
                        say?.invoke("This is the newest app the computer has (${onServer.label()}).")
                            ?: Toast.makeText(a, "Already the newest one", Toast.LENGTH_SHORT).show()
                    else -> Unit
                }
            }
        }
    }

    /** The quiet check the radio screen makes when it opens: nothing is said unless there is something. */
    fun offerIfNewer(a: Activity, io: Executor, main: Handler) {
        io.execute {
            val info = Updater.newer() ?: return@execute
            main.post { if (!a.isFinishing) offer(a, info) }
        }
    }

    fun offer(a: Activity, info: Updater.Info) {
        AlertDialog.Builder(a, android.R.style.Theme_Material_Dialog_Alert)
            .setTitle("A newer app is ready")
            .setMessage(
                "Ten Forward on the computer is handing out ${info.label()}.\n" +
                    "This one is ${BuildConfig.VERSION_NAME} (build ${BuildConfig.VERSION_CODE}).\n\n" +
                    "The app downloads it; Android asks you to confirm the install."
            )
            .setPositiveButton("Update") { _, _ -> start(a, info) }
            .setNegativeButton("Not now", null)
            .setNeutralButton("Skip this one") { _, _ -> Prefs.skipBuild = info.build }
            .show()
    }

    fun start(a: Activity, info: Updater.Info) {
        if (!Updater.allowed(a)) {
            AlertDialog.Builder(a, android.R.style.Theme_Material_Dialog_Alert)
                .setTitle("Android has to allow this first")
                .setMessage("Android only lets an app install an update if you say so. The next screen is that switch — turn it on for Ten Forward, then press Update again.")
                .setPositiveButton("Take me there") { _, _ -> Updater.askAllowed(a) }
                .setNegativeButton("Cancel", null)
                .show()
            return
        }
        val main = Handler(a.mainLooper)
        val dialog = AlertDialog.Builder(a, android.R.style.Theme_Material_Dialog_Alert)
            .setTitle("Getting the new app")
            .setMessage("Starting…")
            .setCancelable(false)
            .create()
        dialog.show()
        Thread {
            try {
                val apk = Updater.download(a, info) { pct ->
                    main.post { dialog.setMessage(if (pct < 0) "Downloading…" else "Downloading… $pct%") }
                }
                main.post {
                    dialog.dismiss()
                    Updater.install(a, apk)
                }
            } catch (e: Exception) {
                ErrorLog.note("update", "could not download the new app", e)
                main.post {
                    dialog.dismiss()
                    Toast.makeText(a, "Could not get it: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }
}
