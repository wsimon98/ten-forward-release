package org.tenforward.radio

import android.app.Activity
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat

/** Every screen runs with no status bar and no navigation bar. A swipe from an edge shows them for a moment. */
object Screen {
    fun full(a: Activity) {
        val c = WindowInsetsControllerCompat(a.window, a.window.decorView)
        c.hide(WindowInsetsCompat.Type.systemBars())
        c.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
    }

    /** Android puts the bars back whenever a dialog or another app took the focus; put them away again. */
    fun keep(a: Activity, hasFocus: Boolean) {
        if (hasFocus) full(a)
    }
}
