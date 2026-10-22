package org.tenforward.radio

import android.content.Context
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.CacheWriter
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * The shelf: what the app keeps on the phone so it has something to show and play before the
 * computer answers, or when it never does.
 *
 *   - the channel list as it was last seen, so the dial is drawn at once instead of after the
 *     two addresses have been asked "are you there";
 *   - a couple of hearted songs per channel, with their audio pulled into the player's cache,
 *     so tuning to a channel plays one of them straight away and the radio takes over from
 *     the computer when it is reached.
 *
 * Nothing here is a second queue: the computer's radio still decides what comes next. The shelf
 * is only what plays while the radio is out of reach.
 */
object Keep {
    private const val PER_CHANNEL = 2
    private const val REFILL_EVERY_MS = 30 * 60 * 1000L
    private val io = Executors.newSingleThreadExecutor()
    @Volatile private var lastRefill = 0L
    @Volatile private var filling = false

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

    // ---------------------------------------------------------------- hearted songs per channel
    private fun shelf(): JSONObject = try {
        JSONObject(Prefs.favJson.ifEmpty { "{}" })
    } catch (e: Exception) {
        JSONObject()
    }

    /** The hearted songs kept for this channel (Favorites and "all" get every channel's). */
    fun favorites(stationId: String?): List<Song> {
        val s = shelf()
        val keys = if (stationId == null || stationId == "all" || stationId == "favorites") s.keys().asSequence().toList() else listOf(stationId)
        val out = ArrayList<Song>()
        for (k in keys) {
            val arr = s.optJSONArray(k) ?: continue
            for (i in 0 until arr.length()) Song.from(arr.optJSONObject(i))?.let { out.add(it) }
        }
        return out
    }

    /** One kept song for a channel, the one least recently handed out. Null when nothing is on the shelf. */
    fun pick(stationId: String?, notId: String? = null): Song? {
        val list = favorites(stationId).filter { it.id != notId }
        if (list.isEmpty()) return null
        val turn = Prefs.shelfTurn
        Prefs.shelfTurn = turn + 1
        return list[turn % list.size]
    }

    /**
     * Fill the shelf from the computer: two hearted songs per channel, audio and all. Runs on its
     * own thread at most every half hour, one download at a time, and never gets in the way of
     * the radio (the player's own fetches are on another thread entirely).
     */
    fun refill(ctx: Context, stations: List<Station>, force: Boolean = false) {
        val now = System.currentTimeMillis()
        if (filling || (!force && now - lastRefill < REFILL_EVERY_MS)) return
        filling = true
        lastRefill = now
        val app = ctx.applicationContext
        io.execute {
            try {
                val shelf = JSONObject()
                for (st in stations) {
                    if (st.id == "favorites" || st.id == "all") continue
                    val songs = try {
                        Api.favoriteSongs(st.id, PER_CHANNEL)
                    } catch (e: Exception) {
                        break           // the computer went away; keep what was on the shelf
                    }
                    if (songs.isEmpty()) continue
                    val arr = JSONArray()
                    for (j in songs) arr.put(j)
                    shelf.put(st.id, arr)
                }
                if (shelf.length() > 0) Prefs.favJson = shelf.toString()
                // now the audio, so the player can start one with no network at all
                val http = DefaultHttpDataSource.Factory().setConnectTimeoutMs(8000).setReadTimeoutMs(20000)
                val source = CacheDataSource.Factory().setCache(PlaybackService.cache(app)).setUpstreamDataSourceFactory(http)
                for (song in favorites(null)) {
                    try {
                        val url = Api.mediaUrl(song)
                        if (!url.startsWith("http")) continue
                        CacheWriter(source.createDataSource(), DataSpec(android.net.Uri.parse(url)), null, null).cache()
                    } catch (e: Exception) {
                        // a song that would not come down is simply not on the shelf yet
                    }
                }
            } catch (e: Exception) {
                ErrorLog.note("shelf", "could not fill the shelf", e)
            } finally {
                filling = false
            }
        }
    }
}
