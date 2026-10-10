package org.tenforward.radio

import android.content.Context

/** Channel colours come from the server by name ("teal", "gold", …); look them up in colors.xml. */
object Lcars {
    fun color(ctx: Context, name: String): Int {
        val id = ctx.resources.getIdentifier(name.lowercase().trim(), "color", ctx.packageName)
        return ctx.getColor(if (id != 0) id else R.color.teal)
    }
}
