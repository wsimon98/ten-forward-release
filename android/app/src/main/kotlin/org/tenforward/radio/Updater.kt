package org.tenforward.radio

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.core.content.FileProvider
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Is there a newer app on the computer we already talk to? The server says what it is handing out at
 * /api/app/version; if its build number is higher than ours we fetch the APK and hand it to Android's
 * installer. Android asks the person to confirm — this never installs anything on its own.
 */
object Updater {

    data class Info(val version: String, val build: Int, val sizeMb: Double, val url: String) {
        fun label(): String = "$version (build $build)" + if (sizeMb > 0) " · ${sizeMb} MB" else ""
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    /** What the server has. Null when it cannot be asked or has nothing built. */
    fun onServer(): Info? {
        val j = try {
            Api.appVersion()
        } catch (e: Exception) {
            ErrorLog.note("update", "could not ask about updates", e)
            null
        } ?: return null
        val build = j.optInt("build", 0)
        if (build <= 0) return null
        val root = Api.base ?: return null
        return Info(
            version = j.optString("version", "?"),
            build = build,
            sizeMb = j.optDouble("size_mb", 0.0),
            url = root + j.optString("url", "/app/TenForward.apk"),
        )
    }

    /** The newer app, or null when we are already running it (or the person said no to this build). */
    fun newer(): Info? {
        val info = onServer() ?: return null
        if (info.build <= BuildConfig.VERSION_CODE) return null
        if (info.build == Prefs.skipBuild) return null
        return info
    }

    /** Downloads into the app's own folder. `progress` is 0..100, or -1 while the size is unknown. */
    fun download(ctx: Context, info: Info, progress: (Int) -> Unit): File {
        val dir = ctx.getExternalFilesDir(null) ?: ctx.filesDir
        dir.listFiles()?.forEach { if (it.name.startsWith("TenForward-") && it.name.endsWith(".apk")) it.delete() }
        val out = File(dir, "TenForward-${info.build}.apk")
        val b = Request.Builder().url(info.url)
        if (Prefs.token.isNotEmpty()) b.header("Authorization", "Bearer ${Prefs.token}")
        client.newCall(b.build()).execute().use { r ->
            if (!r.isSuccessful) throw Exception("the computer said ${r.code}")
            val body = r.body ?: throw Exception("nothing came back")
            val total = body.contentLength()
            body.byteStream().use { input ->
                out.outputStream().use { file ->
                    val buf = ByteArray(64 * 1024)
                    var done = 0L
                    var last = -1
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        file.write(buf, 0, n)
                        done += n
                        val pct = if (total > 0) ((done * 100) / total).toInt() else -1
                        if (pct != last) {
                            last = pct
                            progress(pct)
                        }
                    }
                }
            }
        }
        return out
    }

    /** True once Android is willing to let this app install an APK. */
    fun allowed(ctx: Context): Boolean = ctx.packageManager.canRequestPackageInstalls()

    fun askAllowed(ctx: Context) {
        val i = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + ctx.packageName))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(i)
    }

    /** Hands the file to Android's own installer. The person taps Update there. */
    fun install(ctx: Context, apk: File) {
        val uri = FileProvider.getUriForFile(ctx, ctx.packageName + ".files", apk)
        val i = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(i)
    }
}
