package org.tenforward.radio

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

/**
 * Everything the app says to Ten Forward. Two addresses are typed in by hand (home wifi and Tailscale);
 * `resolve` asks each one "are you there" with a short timeout and keeps the first that answers.
 * Every call is blocking: they are made from a background thread.
 */
object Api {
    class AuthNeeded : Exception("somebody has to sign in")
    class Unreachable : Exception("cannot reach Ten Forward")

    private val client = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(25, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    /** The writer takes half a minute to build a channel, and longer when the radio is planning a song. */
    private val patient = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(200, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private val prober = OkHttpClient.Builder()
        .connectTimeout(1500, TimeUnit.MILLISECONDS)
        .readTimeout(1500, TimeUnit.MILLISECONDS)
        .build()

    private val JSON = "application/json; charset=utf-8".toMediaType()

    @Volatile var base: String? = null
    @Volatile var via: String = ""            // "wifi" or "away"
    @Volatile var serverVersion: String = ""
    @Volatile var loginRequired: Boolean = false

    fun clean(url: String): String {
        var u = url.trim().trimEnd('/')
        if (u.isEmpty()) return ""
        if (!u.startsWith("http://") && !u.startsWith("https://")) u = "http://$u"
        return u
    }

    /** GET /api/hello on one address, quickly. Null when it is not Ten Forward or does not answer. */
    fun hello(url: String): JSONObject? {
        val u = clean(url)
        if (u.isEmpty()) return null
        return try {
            prober.newCall(Request.Builder().url("$u/api/hello").build()).execute().use { r ->
                if (!r.isSuccessful) return null
                val body = r.body?.string() ?: return null
                val j = JSONObject(body)
                if (j.optString("app") == "Ten Forward") j else null
            }
        } catch (e: Exception) {
            null
        }
    }

    /** Home address first, then the one that works from anywhere. */
    fun resolve(force: Boolean = false): String? {
        if (!force && base != null) return base
        for ((label, url) in listOf("wifi" to Prefs.lanUrl, "away" to Prefs.tailUrl)) {
            val j = hello(url) ?: continue
            base = clean(url)
            via = label
            serverVersion = j.optString("version")
            loginRequired = j.optBoolean("login_required", false)
            return base
        }
        base = null
        via = ""
        return null
    }

    private fun call(path: String, post: Boolean = false, json: String? = null, slow: Boolean = false): String {
        val root = base ?: throw Unreachable()
        val b = Request.Builder().url(root + path)
        if (Prefs.token.isNotEmpty()) b.header("Authorization", "Bearer ${Prefs.token}")
        if (post) b.post((json ?: "{}").toRequestBody(JSON))
        try {
            (if (slow) patient else client).newCall(b.build()).execute().use { r ->
                val text = r.body?.string() ?: ""
                if (r.code == 401 && !path.startsWith("/api/auth/")) {
                    Prefs.token = ""
                    throw AuthNeeded()
                }
                if (!r.isSuccessful) {
                    val why = detail(text) ?: "server said ${r.code}"
                    noteFailure(path, why, null)
                    throw Exception(why)
                }
                return text
            }
        } catch (e: java.io.IOException) {
            base = null
            noteFailure(path, "could not reach Ten Forward", e)
            throw Unreachable()
        }
    }

    private fun callDelete(path: String): String {
        val root = base ?: throw Unreachable()
        val b = Request.Builder().url(root + path).delete()
        if (Prefs.token.isNotEmpty()) b.header("Authorization", "Bearer ${Prefs.token}")
        try {
            client.newCall(b.build()).execute().use { r ->
                val text = r.body?.string() ?: ""
                if (r.code == 401) {
                    Prefs.token = ""
                    throw AuthNeeded()
                }
                if (!r.isSuccessful) {
                    val why = detail(text) ?: "server said ${r.code}"
                    noteFailure(path, why, null)
                    throw Exception(why)
                }
                return text
            }
        } catch (e: java.io.IOException) {
            base = null
            noteFailure(path, "could not reach Ten Forward", e)
            throw Unreachable()
        }
    }

    /** Write it down for Settings → Problems. The log endpoint itself is left out, or a failure to report
     *  a problem would report a problem. */
    private fun noteFailure(path: String, why: String, e: Exception?) {
        if (path.startsWith("/api/client/log")) return
        ErrorLog.note("server", "$path — $why", e)
    }

    private fun detail(text: String): String? = try {
        JSONObject(text).optString("detail").ifEmpty { null }
    } catch (e: Exception) {
        null
    }

    fun login(name: String, password: String?, device: String): String {
        val root = base ?: throw Unreachable()
        val j = JSONObject().put("name", name).put("device", device)
        if (!password.isNullOrEmpty()) j.put("password", password)
        val b = Request.Builder().url("$root/api/auth/login").post(j.toString().toRequestBody(JSON))
        client.newCall(b.build()).execute().use { r ->
            val text = r.body?.string() ?: ""
            if (!r.isSuccessful) throw Exception(detail(text) ?: "that did not work")
            val token = JSONObject(text).optString("token")
            Prefs.token = token
            Prefs.userName = name
            return token
        }
    }

    fun logout() {
        try {
            if (base != null && Prefs.token.isNotEmpty()) call("/api/auth/logout", true)
        } catch (e: Exception) {
            // signing out locally is what matters
        }
        Prefs.token = ""
        Prefs.userName = ""
    }

    /** Everything the computer is doing, including any channel being filled for the first time. */
    fun status(): JSONObject = JSONObject(call("/api/status"))

    fun stations(): List<Station> {
        val arr = JSONArray(call("/api/stations"))
        val out = ArrayList<Station>(arr.length())
        for (i in 0 until arr.length()) out.add(Station.from(arr.getJSONObject(i)))
        return out
    }

    fun next(stationId: String, excludeSongId: String?): Song? {
        var path = "/api/radio/next?station=" + URLEncoder.encode(stationId, "UTF-8")
        if (!excludeSongId.isNullOrEmpty()) path += "&exclude=" + URLEncoder.encode(excludeSongId, "UTF-8")
        val j = JSONObject(call(path))
        return Song.from(j.optJSONObject("song"))
    }

    /** Heard. The channel it was heard ON goes with it, because a song played from Favorites should count
     *  for Favorites and not for the channel the song happens to live on. */
    fun played(songId: String, stationId: String? = null) {
        val body = JSONObject()
        if (!stationId.isNullOrEmpty()) body.put("station", stationId)
        call("/api/songs/$songId/played", true, body.toString())
    }

    /** Hearts or un-hearts; returns the state it ended up in. The server keeps the file for good. */
    fun like(songId: String): Boolean {
        val j = JSONObject(call("/api/songs/$songId/like", true))
        return j.optInt("liked") == 1
    }

    fun log(event: String, songId: String?) {
        try {
            val j = JSONObject().put("event", event).put("source", "android")
            if (songId != null) j.put("song_id", songId)
            call("/api/client/log", true, j.toString())
        } catch (e: Exception) {
            // never let a log line break the music
        }
    }

    /** One problem, written into the server's client log (which keeps two days). True when it landed. */
    fun report(entry: JSONObject): Boolean = try {
        call("/api/client/log", true, entry.toString())
        true
    } catch (e: Exception) {
        false
    }

    // ---------------------------------------------------------------- channels, from the phone
    /** Create or change a channel. The server merges what it is sent into the row it already has. */
    fun saveStation(body: JSONObject): Station = Station.from(JSONObject(call("/api/stations", true, body.toString())))

    /** Plain English in, a whole channel back. Nothing is saved by this: the screen fills itself and the
     *  person presses save, the same as any other channel. */
    fun quickStation(body: JSONObject): JSONObject = JSONObject(call("/api/stations/quick", true, body.toString(), slow = true))

    fun deleteStation(stationId: String) {
        callDelete("/api/stations/" + URLEncoder.encode(stationId, "UTF-8"))
    }

    /** Ask for one more song on a channel now, instead of waiting for the keep-ahead to notice. */
    /**
     * The songs you can pick from on a channel. brief=1 leaves the lyrics behind: a channel with
     * 300 songs is a lot of text to pull down a mobile connection just to draw a list.
     */
    fun songs(stationId: String, limit: Int = 300): List<Song> {
        val path = "/api/songs?limit=" + limit + "&brief=1&station=" + URLEncoder.encode(stationId, "UTF-8")
        val arr = JSONArray(call(path))
        val out = ArrayList<Song>(arr.length())
        for (i in 0 until arr.length()) Song.from(arr.optJSONObject(i))?.let { out.add(it) }
        return out
    }

    // ---------------------------------------------------------------- the line
    // Songs picked by hand, played before the radio picks anything. It lives on the server, so the
    // phone, the phone's browser and the desk all see one list. This app never keeps its own copy:
    // /api/radio/next hands lined-up songs out first, which is the only reason the radio honours it.

    /** The ids of everything lined up, so a list can show which ones are already in it. */
    fun queueIds(): List<String> = queue().map { it.song.id }

    /** One song in the line, with the handle needed to take it back out again. */
    data class Queued(val queueId: String, val song: Song)

    /** The whole line, in order. */
    fun queue(): List<Queued> {
        val arr = JSONObject(call("/api/playqueue")).optJSONArray("queue") ?: return emptyList()
        val out = ArrayList<Queued>(arr.length())
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val s = Song.from(o) ?: continue
            out.add(Queued(o.optString("queue_id"), s))
        }
        return out
    }

    /** Take one back out of the line. */
    fun queueDrop(queueId: String) {
        callDelete("/api/playqueue/" + URLEncoder.encode(queueId, "UTF-8"))
    }

    /** Line a song up. Returns how many are now in the line. */
    fun queueAdd(songId: String, next: Boolean = false): Int {
        val body = JSONObject().put("song_id", songId).put("next", next)
        return JSONObject(call("/api/playqueue", true, body.toString())).optInt("count")
    }

    fun queueClear() {
        call("/api/playqueue/clear", true)
    }

    fun generate(stationId: String) {
        call("/api/radio/" + URLEncoder.encode(stationId, "UTF-8") + "/generate", true)
    }

    /** The theme catalog, names only, for the picker in the channel editor. */
    fun themeNames(): List<String> {
        val arr = JSONObject(call("/api/themes")).optJSONArray("themes") ?: return emptyList()
        val out = ArrayList<String>(arr.length())
        for (i in 0 until arr.length()) {
            val name = arr.optJSONObject(i)?.optString("name") ?: continue
            if (name.isNotEmpty()) out.add(name)
        }
        return out.sorted()
    }

    /** What the computer is handing out at /app/TenForward.apk: {version, build, size_mb, url}. */
    fun appVersion(): JSONObject? {
        val root = base ?: resolve(true) ?: return null
        return try {
            prober.newCall(Request.Builder().url("$root/api/app/version").build()).execute().use { r ->
                if (!r.isSuccessful) return null
                val body = r.body?.string() ?: return null
                val j = JSONObject(body)
                if (j.optInt("build", 0) > 0) j else null
            }
        } catch (e: Exception) {
            null
        }
    }

    /** ExoPlayer cannot send headers from a notification, so media carries the token in the address. */
    fun mediaUrl(song: Song): String {
        val root = base ?: ""
        return root + song.path + if (Prefs.token.isNotEmpty()) "?token=" + URLEncoder.encode(Prefs.token, "UTF-8") else ""
    }
}
