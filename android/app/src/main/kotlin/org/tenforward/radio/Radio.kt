package org.tenforward.radio

import android.os.Handler
import android.os.Looper
import java.util.concurrent.CopyOnWriteArrayList

/** What is on the air right now. The service writes it, the screen reads it. */
object Radio {
    interface Listener {
        fun onRadio()
    }

    private val listeners = CopyOnWriteArrayList<Listener>()
    private val main = Handler(Looper.getMainLooper())

    @Volatile var stationId: String? = null
    @Volatile var stationName: String = ""
    @Volatile var song: Song? = null
    @Volatile var liked: Boolean = false
    @Volatile var playing: Boolean = false
    @Volatile var status: String = ""
    @Volatile var sleepAt: Long = 0L
    @Volatile var stopAfterSong: Boolean = false

    fun add(l: Listener) {
        listeners.addIfAbsent(l)
    }

    fun remove(l: Listener) {
        listeners.remove(l)
    }

    fun changed() {
        main.post { for (l in listeners) l.onRadio() }
    }
}
