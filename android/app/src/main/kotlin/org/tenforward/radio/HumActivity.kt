package org.tenforward.radio

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import java.io.File
import java.util.concurrent.Executors

/**
 * Hum a request: record a tune (or pick a recording), say what it is about, and send it. The server
 * writes a song from it and puts it on the request line, so nothing plays from this screen but the
 * recording itself.
 */
class HumActivity : Activity() {

    companion object {
        private const val ASK_MIC = 41
        private const val PICK_FILE = 42
        private const val MAX_MS = 60_000
        private const val MODE_CONTINUE = "continue"
        private const val MODE_HUM_ONLY = "hum_only"
        private const val OFFLINE = "Cannot reach Ten Forward, so there is nowhere to send this. Check the addresses under the gear on the first screen."
    }

    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    private lateinit var btnRec: TextView
    private lateinit var btnPlay: TextView
    private lateinit var btnSend: TextView
    private lateinit var btnMode: TextView
    private lateinit var elapsed: TextView
    private lateinit var msg: TextView
    private lateinit var modeHint: TextView
    private lateinit var topic: EditText
    private lateinit var style: EditText

    private lateinit var clip: File
    private var haveClip = false
    private var recorder: MediaRecorder? = null
    private var recStart = 0L
    private var player: MediaPlayer? = null
    private var mode = MODE_CONTINUE
    private var sending = false
    private var online = false

    private val tick = object : Runnable {
        override fun run() {
            if (recorder == null) return
            val s = ((System.currentTimeMillis() - recStart) / 1000).toInt()
            elapsed.text = "Recording... ${s}s of 60s"
            main.postDelayed(this, 250)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Prefs.init(this)
        ErrorLog.init(this)
        setContentView(R.layout.activity_hum)
        Screen.full(this)

        clip = File(cacheDir, "hum.m4a")
        btnRec = findViewById(R.id.btn_rec)
        btnPlay = findViewById(R.id.btn_play)
        btnSend = findViewById(R.id.btn_send)
        btnMode = findViewById(R.id.btn_mode)
        elapsed = findViewById(R.id.elapsed)
        msg = findViewById(R.id.msg)
        modeHint = findViewById(R.id.mode_hint)
        topic = findViewById(R.id.topic)
        style = findViewById(R.id.style)

        findViewById<View>(R.id.btn_close).setOnClickListener { finish() }
        btnRec.setOnClickListener { onRecordTapped() }
        btnPlay.setOnClickListener { onPlayTapped() }
        findViewById<View>(R.id.btn_pick).setOnClickListener { pickFile() }
        btnMode.setOnClickListener {
            mode = if (mode == MODE_CONTINUE) MODE_HUM_ONLY else MODE_CONTINUE
            showMode()
        }
        btnSend.setOnClickListener { send() }

        // a clip left over from an earlier visit is not kept: start clean
        clip.delete()
        showMode()
        refresh()
        checkOnline()
    }

    override fun onResume() {
        super.onResume()
        if (!online) checkOnline()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        Screen.keep(this, hasFocus)
    }

    override fun onStop() {
        super.onStop()
        if (recorder != null) stopRecording()
        stopPlayback()
    }

    override fun onDestroy() {
        super.onDestroy()
        main.removeCallbacksAndMessages(null)
        if (recorder != null) stopRecording()
        stopPlayback()
        io.shutdown()
    }

    // ------------------------------------------------------------------ reaching the computer
    private fun checkOnline() {
        io.execute {
            val ok = try {
                Api.base != null || Api.resolve(true) != null
            } catch (e: Exception) {
                false
            }
            main.post {
                online = ok
                refresh()
            }
        }
    }

    // ------------------------------------------------------------------ what the buttons say
    private fun showMode() {
        if (mode == MODE_CONTINUE) {
            btnMode.text = "Carry on from my hum"
            modeHint.text = "The radio takes your hum as the start and writes the rest of the song."
        } else {
            btnMode.text = "Sing my tune as it is"
            modeHint.text = "The radio sings your tune as hummed, with words put to it."
        }
    }

    private fun refresh() {
        val recording = recorder != null
        btnRec.text = if (recording) "■ STOP" else "● RECORD"
        btnPlay.alpha = if (haveClip && !recording) 1f else 0.4f
        btnPlay.text = if (player != null) "■ STOP PLAYBACK" else "▶ PLAY BACK"
        val canSend = haveClip && !recording && !sending && online
        btnSend.alpha = if (canSend) 1f else 0.4f
        btnSend.isEnabled = online && !sending
        btnSend.text = if (sending) "SENDING..." else "SEND"
        if (!online && !sending) {
            msg.text = OFFLINE
        } else if (msg.text.toString() == OFFLINE) {
            msg.text = ""
        }
    }

    // ------------------------------------------------------------------ recording
    private fun onRecordTapped() {
        if (recorder != null) {
            stopRecording()
            return
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), ASK_MIC)
            return
        }
        startRecording()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != ASK_MIC) return
        if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            startRecording()
        } else {
            msg.text = "Without the microphone there is nothing to record. You can still pick a file."
        }
    }

    private fun startRecording() {
        stopPlayback()
        var r: MediaRecorder? = null
        try {
            clip.delete()
            r = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(this) else MediaRecorder()
            r.setAudioSource(MediaRecorder.AudioSource.MIC)
            r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            r.setAudioSamplingRate(44100)
            r.setAudioChannels(1)
            r.setAudioEncodingBitRate(96000)
            r.setMaxDuration(MAX_MS)
            r.setOutputFile(clip.absolutePath)
            r.setOnInfoListener { _, what, _ ->
                if (what == MediaRecorder.MEDIA_RECORDER_INFO_MAX_DURATION_REACHED) {
                    main.post { if (recorder != null) stopRecording() }
                }
            }
            r.prepare()
            r.start()
        } catch (e: Exception) {
            try {
                r?.release()
            } catch (x: Exception) {
                // already gone
            }
            clip.delete()
            haveClip = false
            msg.text = "The microphone would not start: " + (e.message ?: "unknown reason")
            ErrorLog.note("hum", "record start failed", e)
            refresh()
            return
        }
        recorder = r
        recStart = System.currentTimeMillis()
        haveClip = false
        msg.text = ""
        elapsed.text = "Recording... 0s of 60s"
        main.postDelayed(tick, 250)
        refresh()
    }

    private fun stopRecording() {
        val r = recorder ?: return
        recorder = null
        main.removeCallbacks(tick)
        val secs = ((System.currentTimeMillis() - recStart) / 1000).toInt()
        var ok = true
        try {
            r.stop()
        } catch (e: RuntimeException) {
            ok = false      // stopped before anything was captured
        }
        try {
            r.release()
        } catch (e: Exception) {
            // already gone
        }
        if (ok && clip.exists() && clip.length() > 0) {
            haveClip = true
            elapsed.text = "Recorded ${secs}s. Play it back, or send it."
        } else {
            clip.delete()
            haveClip = false
            elapsed.text = "That was too short to keep. Try again."
        }
        refresh()
    }

    // ------------------------------------------------------------------ playing it back
    private fun onPlayTapped() {
        if (player != null) {
            stopPlayback()
            refresh()
            return
        }
        if (!haveClip || recorder != null) return
        try {
            val p = MediaPlayer()
            p.setDataSource(clip.absolutePath)
            p.setOnCompletionListener {
                stopPlayback()
                refresh()
            }
            p.setOnErrorListener { _, _, _ ->
                stopPlayback()
                msg.text = "That recording would not play."
                refresh()
                true
            }
            p.prepare()
            p.start()
            player = p
        } catch (e: Exception) {
            stopPlayback()
            msg.text = "That recording would not play."
        }
        refresh()
    }

    private fun stopPlayback() {
        val p = player ?: return
        player = null
        try {
            p.stop()
        } catch (e: Exception) {
            // not started
        }
        try {
            p.release()
        } catch (e: Exception) {
            // already gone
        }
    }

    // ------------------------------------------------------------------ a file instead
    private fun pickFile() {
        if (recorder != null) stopRecording()
        stopPlayback()
        val i = Intent(Intent.ACTION_OPEN_DOCUMENT)
            .addCategory(Intent.CATEGORY_OPENABLE)
            .setType("audio/*")
        try {
            startActivityForResult(i, PICK_FILE)
        } catch (e: Exception) {
            msg.text = "No file picker is available on this phone."
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != PICK_FILE || resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        msg.text = "Copying the file..."
        io.execute {
            var error: String? = null
            try {
                val input = contentResolver.openInputStream(uri) ?: throw Exception("could not open that file")
                input.use { src -> clip.outputStream().use { out -> src.copyTo(out) } }
                if (clip.length() == 0L) throw Exception("that file is empty")
            } catch (e: Exception) {
                clip.delete()
                error = e.message ?: "could not read that file"
            }
            main.post {
                if (error == null) {
                    haveClip = true
                    elapsed.text = "File ready (${clip.length() / 1024} KB). Play it back, or send it."
                    msg.text = ""
                } else {
                    haveClip = false
                    msg.text = "Could not use that file: $error"
                }
                refresh()
            }
        }
    }

    // ------------------------------------------------------------------ sending it
    private fun send() {
        if (sending) return
        if (!online) {
            msg.text = OFFLINE
            return
        }
        if (recorder != null) {
            msg.text = "Stop recording first."
            return
        }
        if (!haveClip || !clip.exists()) {
            msg.text = "Record something or pick a file first."
            return
        }
        stopPlayback()
        sending = true
        msg.text = "Sending... the radio is listening to it and writing the words. This can take a minute."
        hideResult()
        refresh()
        val sendMode = mode
        val sendTopic = topic.text.toString().trim()
        val sendStyle = style.text.toString().trim()
        io.execute {
            try {
                if (Api.base == null && Api.resolve(true) == null) throw Api.Unreachable()
                val j = Api.hum(clip, sendMode, sendTopic, sendStyle)
                main.post {
                    sending = false
                    msg.text = ""
                    showResult(j.optString("title"), j.optString("style"), j.optString("lyrics"))
                    Toast.makeText(this, "Queued: it will play on the request line when it is ready", Toast.LENGTH_LONG).show()
                    refresh()
                }
            } catch (e: Exception) {
                main.post {
                    sending = false
                    if (e is Api.Unreachable) online = false
                    msg.text = why(e)
                    refresh()
                }
            }
        }
    }

    private fun hideResult() {
        findViewById<View>(R.id.res_title).visibility = View.GONE
        findViewById<View>(R.id.res_style).visibility = View.GONE
        findViewById<View>(R.id.res_lyrics).visibility = View.GONE
    }

    private fun showResult(title: String, styleText: String, lyrics: String) {
        val t = findViewById<TextView>(R.id.res_title)
        val s = findViewById<TextView>(R.id.res_style)
        val l = findViewById<TextView>(R.id.res_lyrics)
        t.text = if (title.isEmpty()) "Untitled" else title
        t.visibility = View.VISIBLE
        s.text = styleText
        s.visibility = if (styleText.isEmpty()) View.GONE else View.VISIBLE
        l.text = lyrics
        l.visibility = if (lyrics.isEmpty()) View.GONE else View.VISIBLE
    }

    private fun why(e: Exception): String = when {
        e is Api.AuthNeeded -> "Sign in first: the gear on the first screen."
        e is Api.Unreachable -> OFFLINE
        (e.message ?: "").contains("newer engine", true) -> "Ten Forward says: " + e.message
        else -> e.message ?: "That did not work"
    }
}
