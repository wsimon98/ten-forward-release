package org.tenforward.radio

import android.content.Context
import android.content.SharedPreferences

/** The handful of things the app remembers: the two addresses, who is signed in, the last channel. */
object Prefs {
    private lateinit var sp: SharedPreferences

    fun init(ctx: Context) {
        if (!::sp.isInitialized) sp = ctx.applicationContext.getSharedPreferences("tenforward", Context.MODE_PRIVATE)
    }

    private fun get(key: String, dflt: String = ""): String = sp.getString(key, dflt) ?: dflt
    private fun put(key: String, value: String) = sp.edit().putString(key, value).apply()

    var lanUrl: String
        get() = get("lan")
        set(v) = put("lan", v.trim())

    var tailUrl: String
        get() = get("tail")
        set(v) = put("tail", v.trim())

    var token: String
        get() = get("token")
        set(v) = put("token", v)

    var userName: String
        get() = get("user")
        set(v) = put("user", v)

    var lastStation: String
        get() = get("station")
        set(v) = put("station", v)

    /** Send problems to Ten Forward as well as keeping a copy here. */
    var reportProblems: Boolean
        get() = sp.getBoolean("report", true)
        set(v) = sp.edit().putBoolean("report", v).apply()

    /** Channels already announced as ready, so one channel is one notification however often it is seen. */
    val saidReady: Set<String>
        get() = sp.getStringSet("said_ready", emptySet()) ?: emptySet()

    fun rememberSaid(key: String) {
        val keep = LinkedHashSet(saidReady)
        keep.add(key)
        while (keep.size > 30) keep.remove(keep.first())
        sp.edit().putStringSet("said_ready", keep).apply()
    }

    /** A build the person said no to; the app stops asking about that one. */
    var skipBuild: Int
        get() = sp.getInt("skip_build", 0)
        set(v) = sp.edit().putInt("skip_build", v).apply()
}
