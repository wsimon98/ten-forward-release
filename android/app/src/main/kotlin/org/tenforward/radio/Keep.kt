package org.tenforward.radio

import android.content.Context
import android.net.ConnectivityManager
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors

/**
 * The shelf: what the app keeps on the phone so it can play before the computer answers, or when it never does
 * (the computer is off, Tailscale is off, no signal).
 *
 *   - the channel list as it was last seen, so the dial is drawn at once instead of after the two addresses have
 *     been asked "are you there";
 *   - PER_CHANNEL songs per channel (the hearted ones first, then the newest), saved as files in the app's own
 *     storage. They used to sit in the player's cache, where a long drive of ordinary listening could push them
 *     out; a file on the shelf stays until the shelf is refilled.
 *
 * Nothing here is a second queue: the computer's radio still decides what comes next. The shelf is what plays
 * while the radio is out of reach, and the app keeps asking for the computer the whole time.
 */
object Keep {
    const val PER_CHANNEL = 4
    private const val REFILL_EVERY_MS = 30 * 60 * 1000L
    private val io = Executors.newSingleThreadExecutor()
    @Volatile private var lastRefill = 0L
    @Volatile private var filling = false
    @Volatile private var dir: File? = null

    fun init(ctx: Context) {
        if (dir == null) dir = File(ctx.applicationContext.filesDir, "shelf").also { it.mkdirs() }
    }

    /** The song's file on the shelf, or null when it is not on the phone. */
    fun file(song: Song): File? {
        val f = File(dir ?: return null, song.id + ".mp3")
        return if (f.exists() && f.length() > 50_000) f else null
    }

    // ---------------------------------------------------------------- the channel list
    fun rememberStations(list: List<Station>) {
        val arr = JSONArray()
        for (s in list) arr.put(s.raw)
        Prefs.stationsJson = arr.toString()
    }

    fun stations(): List<Station> {
        val raw = Prefs.stationsJson
        if (raw.isEmpty()) return emptyList()
        return try {
            val arr = JSONArray(raw)
            val out = ArrayList<Station>(arr.length())
            for (i in 0 until arr.length()) out.add(Station.from(arr.getJSONObject(i)))
            out
        } catch (e: Exception) {
            emptyList()
        }
    }

    // ---------------------------------------------------------------- songs per channel
    private fun shelf(): JSONObject = try {
        JSONObject(Prefs.favJson.ifEmpty { "{}" })
    } catch (e: Exception) {
        JSONObject()
    }

    /** The songs on the shelf for this channel whose audio is on the phone (Favorites / "all": every channel's hearted ones). */
    fun songs(stationId: String?, likedOnly: Boolean = false): List<Song> {
        val s = shelf()
        val every = stationId == null || stationId == "all" || stationId == "favorites"
        val keys = if (every) s.keys().asSequence().toList() else listOf(stationId!!)
        val out = ArrayList<Song>()
        for (k in keys) {
            val arr = s.optJSONArray(k) ?: continue
            for (i in 0 until arr.length()) {
                val song = Song.from(arr.optJSONObject(i)) ?: continue
                if ((likedOnly || stationId == "favorites") && !song.liked) continue
                if (file(song) != null) out.add(song)
            }
        }
        return out
    }

    fun favorites(stationId: String?): List<Song> = songs(stationId, likedOnly = true)

    /** One song off the shelf for a channel, taking turns. Null when none of its songs are on the phone. */
    fun pick(stationId: String?, notId: String? = null): Song? {
        val list = songs(stationId).filter { it.id != notId }
        if (list.isEmpty()) return null
        val turn = Prefs.shelfTurn
        Prefs.shelfTurn = turn + 1
        return list[turn % list.size]
    }

    private fun unmetered(ctx: Context): Boolean {
        return try {
            val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            val metered: Boolean = cm.isActiveNetworkMetered
            metered == false
        } catch (e: Exception) {
            false
        }
    }

    /**
     * Fill the shelf from the computer: PER_CHANNEL songs per channel, hearted first. Runs on its own thread at
     * most every half hour. The list always updates; the audio only comes down on an unmetered connection (home
     * wifi), because a full shelf is a few hundred MB and nobody wants that on mobile data. Files no longer on the
     * shelf are deleted.
     */
    fun refill(ctx: Context, stations: List<Station>, force: Boolean = false) {
        init(ctx)
        val now = System.currentTimeMillis()
        if (filling || (!force && now - lastRefill < REFILL_EVERY_MS)) return
        filling = true
        lastRefill = now
        val app = ctx.applicationContext
        io.execute {
            try {
                val shelf = shelf()
                for (st in stations) {
                    if (st.id == "favorites" || st.id == "all") continue
                    val picked = ArrayList<JSONObject>()
                    try {
                        val ids = HashSet<String>()
                        for (j in Api.favoriteSongs(st.id, PER_CHANNEL)) if (ids.add(j.optString("id"))) picked.add(j)
                        if (picked.size < PER_CHANNEL) {
                            for (j in Api.newestSongs(st.id, PER_CHANNEL * 2)) {
                                if (picked.size >= PER_CHANNEL) break
                                if (ids.add(j.optString("id"))) picked.add(j)
                            }
                        }
                    } catch (e: Exception) {
                        break           // the computer went away; keep what is on the shelf for the rest
                    }
                    if (picked.isEmpty()) continue
                    val arr = JSONArray()
                    for (j in picked) arr.put(j)
                    shelf.put(st.id, arr)
                }
                Prefs.favJson = shelf.toString()
                if (!unmetered(app)) return@execute
                val keep = HashSet<String>()
                val d = dir ?: return@execute
                for (k in shelf.keys()) {
                    val arr = shelf.optJSONArray(k) ?: continue
                    for (i in 0 until arr.length()) {
                        val song = Song.from(arr.optJSONObject(i)) ?: continue
                        keep.add(song.id + ".mp3")
                        if (file(song) != null) continue
                        try {
                            Api.download(song, File(d, song.id + ".mp3"))
                        } catch (e: Exception) {
                            if (e is Api.Unreachable) return@execute    // gone; the next refill carries on
                        }
                    }
                }
                d.listFiles()?.forEach { f -> if (f.name !in keep && !f.name.endsWith(".part")) f.delete() }
            } catch (e: Exception) {
                ErrorLog.note("shelf", "could not fill the shelf", e)
            } finally {
                filling = false
            }
        }
    }
}
