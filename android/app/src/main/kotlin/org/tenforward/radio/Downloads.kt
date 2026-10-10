package org.tenforward.radio

import android.content.ContentValues
import android.content.Context
import android.media.MediaScannerConnection
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.OutputStream
import java.util.concurrent.TimeUnit

/** Keep a song on the phone: it lands in Music/Ten Forward, where every music player can find it. */
object Downloads {

    private const val FOLDER = "Ten Forward"

    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    /** Blocking: call it from a background thread. Returns where the file landed. */
    fun save(ctx: Context, song: Song): String {
        val ext = song.path.substringAfterLast('.', "mp3").lowercase().take(4).ifEmpty { "mp3" }
        val name = clean(song.title) + "." + ext
        val mime = if (ext == "wav") "audio/wav" else "audio/mpeg"
        val b = Request.Builder().url(Api.mediaUrl(song))
        if (Prefs.token.isNotEmpty()) b.header("Authorization", "Bearer ${Prefs.token}")
        client.newCall(b.build()).execute().use { r ->
            if (!r.isSuccessful) throw Exception("the computer said ${r.code}")
            val body = r.body ?: throw Exception("nothing came back")
            return if (Build.VERSION.SDK_INT >= 29) intoMediaStore(ctx, name, mime, body.byteStream())
            else intoMusicFolder(ctx, name, body.byteStream())
        }
    }

    /** Android 10 and up: the music library owns the file, no storage permission needed. */
    private fun intoMediaStore(ctx: Context, name: String, mime: String, input: java.io.InputStream): String {
        val values = ContentValues().apply {
            put(MediaStore.Audio.Media.DISPLAY_NAME, name)
            put(MediaStore.Audio.Media.MIME_TYPE, mime)
            put(MediaStore.Audio.Media.RELATIVE_PATH, Environment.DIRECTORY_MUSIC + "/" + FOLDER)
            put(MediaStore.Audio.Media.IS_PENDING, 1)
        }
        val resolver = ctx.contentResolver
        val uri = resolver.insert(MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, values)
            ?: throw Exception("the music folder would not take it")
        try {
            resolver.openOutputStream(uri).use { out ->
                copy(input, out ?: throw Exception("could not write the file"))
            }
        } catch (e: Exception) {
            resolver.delete(uri, null, null)
            throw e
        }
        values.clear()
        values.put(MediaStore.Audio.Media.IS_PENDING, 0)
        resolver.update(uri, values, null, null)
        return "Music/$FOLDER/$name"
    }

    /** Android 8 and 9: a plain file, then tell the music library it is there. */
    private fun intoMusicFolder(ctx: Context, name: String, input: java.io.InputStream): String {
        @Suppress("DEPRECATION")
        val dir = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MUSIC), FOLDER)
        dir.mkdirs()
        val file = File(dir, name)
        file.outputStream().use { out -> copy(input, out) }
        MediaScannerConnection.scanFile(ctx, arrayOf(file.absolutePath), null, null)
        return "Music/$FOLDER/$name"
    }

    private fun copy(input: java.io.InputStream, out: OutputStream) {
        val buf = ByteArray(64 * 1024)
        while (true) {
            val n = input.read(buf)
            if (n < 0) break
            out.write(buf, 0, n)
        }
        out.flush()
    }

    private fun clean(title: String): String {
        val t = title.trim().replace(Regex("[\\\\/:*?\"<>|]"), " ").replace(Regex("\\s+"), " ").trim()
        return (if (t.isEmpty()) "Ten Forward song" else t).take(80)
    }
}
