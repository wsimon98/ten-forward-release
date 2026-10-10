package org.tenforward.radio

import org.json.JSONArray
import org.json.JSONObject

data class Station(
    val id: String,
    val name: String,
    val color: String,
    val description: String,
    val ready: Int,
    val unplayed: Int,
    val queued: Int,
    val nextUp: String?,
    val instrumental: Boolean,
    /** The whole row as the server sent it, so the editor can show everything without a second call. */
    val raw: JSONObject,
) {
    fun str(key: String, dflt: String = ""): String {
        val v = raw.opt(key)
        if (v == null || v === JSONObject.NULL) return dflt
        return v.toString()
    }

    fun int(key: String, dflt: Int): Int = raw.optInt(key, dflt)

    fun on(key: String, dflt: Boolean = false): Boolean = when (val v = raw.opt(key)) {
        null, JSONObject.NULL -> dflt
        is Boolean -> v
        is Number -> v.toInt() != 0
        else -> v.toString() == "1" || v.toString().equals("true", true)
    }

    /** A list field (themes, style lines) as text: commas for the short ones, a line each for the long. */
    fun list(key: String, separator: String): String {
        val v = raw.opt(key)
        if (v == null || v === JSONObject.NULL) return ""
        if (v is JSONArray) {
            val out = ArrayList<String>(v.length())
            for (i in 0 until v.length()) out.add(v.optString(i))
            return out.filter { it.isNotEmpty() }.joinToString(separator)
        }
        return v.toString()
    }

    companion object {
        fun from(j: JSONObject): Station {
            val nu = j.optJSONObject("next_up")
            return Station(
                id = j.optString("id"),
                name = j.optString("name", j.optString("id")),
                color = j.optString("color", "teal"),
                description = j.optString("description", ""),
                ready = j.optInt("ready"),
                unplayed = j.optInt("unplayed"),
                queued = j.optInt("queued"),
                nextUp = nu?.optString("title"),
                instrumental = j.optInt("instrumental") == 1,
                raw = j,
            )
        }
    }
}

data class Song(
    val id: String,
    val title: String,
    val style: String,
    val stationId: String,
    val liked: Boolean,
    val durationS: Double,
    val path: String,
    val coverTrack: String = "",
    val coverArtist: String = "",
    val coverKind: String = "",
) {
    /**
     * Somebody else's song, said out loud. A "cover" is their words sung in this channel's own
     * sound; an "interpolation" only borrows a piece of them. Null when the song is an original.
     * You should never have to guess which of the three you are listening to.
     */
    fun coverLine(): String? {
        if (coverTrack.isEmpty() && coverArtist.isEmpty()) return null
        val what = if (coverKind == "interpolation") "Interpolates" else "Cover of"
        val who = when {
            coverTrack.isNotEmpty() && coverArtist.isNotEmpty() -> "$coverTrack — $coverArtist"
            coverTrack.isNotEmpty() -> coverTrack
            else -> coverArtist
        }
        return "$what $who"
    }

    companion object {
        /** The server hands back relative media paths; the app puts its own base address in front. */
        fun from(j: JSONObject?): Song? {
            if (j == null) return null
            val urls = j.optJSONObject("urls") ?: return null
            val path = when {
                urls.has("mp3") -> urls.optString("mp3")
                urls.has("voiced") -> urls.optString("voiced")
                urls.has("master") -> urls.optString("master")
                else -> return null
            }
            if (path.isEmpty()) return null
            val cov = j.optJSONObject("cover_of")
            return Song(
                id = j.optString("id"),
                title = j.optString("title", "Untitled"),
                style = j.optString("style", ""),
                stationId = j.optString("station_id", ""),
                liked = j.optInt("liked") == 1,
                durationS = j.optDouble("duration_s", 0.0),
                path = path,
                coverTrack = cov?.optString("track") ?: "",
                coverArtist = cov?.optString("artist") ?: "",
                coverKind = cov?.optString("kind") ?: "",
            )
        }
    }
}
