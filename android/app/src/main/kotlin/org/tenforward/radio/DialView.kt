package org.tenforward.radio

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Typeface
import android.text.TextPaint
import android.text.TextUtils
import android.util.AttributeSet
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.View
import android.widget.OverScroller
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * The tuning dial: the channels laid along a scale, with a needle down the middle.
 *
 * Drag the scale and let go and it settles on the nearest channel and tunes to it, the way a radio does.
 * Tap a name on the scale to go straight there. Tap the big name at the top to play or pause — on its side
 * the dial has the whole screen and there is no other control, so that tap is the whole transport.
 */
class DialView @JvmOverloads constructor(ctx: Context, attrs: AttributeSet? = null, style: Int = 0) :
    View(ctx, attrs, style) {

    var onTune: ((Station) -> Unit)? = null
    var onPlayPause: (() -> Unit)? = null

    private var stations: List<Station> = emptyList()
    private var index = 0                 // the channel the needle is meant to be on
    private var offset = 0f               // pixels along the scale; channel k sits at k * pitch
    private var pitch = 1f

    private val scroller = OverScroller(ctx)
    private var flinging = false
    private var settling = false
    private var settleTunes = false
    private var tapped = false

    private val big = TextPaint(Paint.ANTI_ALIAS_FLAG)
    private val sub = TextPaint(Paint.ANTI_ALIAS_FLAG)
    private val label = TextPaint(Paint.ANTI_ALIAS_FLAG)
    private val stroke = Paint(Paint.ANTI_ALIAS_FLAG)
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)

    private val density = resources.displayMetrics.density

    init {
        for (p in listOf(big, sub, label)) p.textAlign = Paint.Align.CENTER
        big.typeface = Typeface.create("sans-serif-condensed", Typeface.BOLD)
        sub.typeface = Typeface.create("sans-serif-condensed", Typeface.NORMAL)
        label.typeface = Typeface.create("sans-serif-condensed", Typeface.BOLD)
        big.letterSpacing = 0.08f
        label.letterSpacing = 0.05f
        stroke.style = Paint.Style.STROKE
        fill.style = Paint.Style.FILL
        isClickable = true
    }

    private fun dp(v: Float) = v * density
    private fun col(id: Int) = context.getColor(id)

    // ---------------------------------------------------------------- what is on it
    /** The channels, in the order the server gave them, and which one is playing. */
    fun submit(list: List<Station>, tuned: String?) {
        val keep = stations.getOrNull(index)?.id
        stations = list
        val want = list.indexOfFirst { it.id == (tuned ?: keep) }
        index = (if (want >= 0) want else index).coerceIn(0, max(0, list.size - 1))
        if (scroller.isFinished) offset = index * pitch
        invalidate()
    }

    /** Move the needle because something else changed channel. Never calls back. */
    fun tuned(id: String?) {
        val want = stations.indexOfFirst { it.id == id }
        if (want < 0 || want == nearest()) {
            invalidate()
            return
        }
        snapTo(want, tunes = false)
    }

    override fun onSizeChanged(w: Int, h: Int, ow: Int, oh: Int) {
        super.onSizeChanged(w, h, ow, oh)
        pitch = max(dp(90f), w * 0.42f)
        offset = index * pitch
    }

    // ---------------------------------------------------------------- touch
    private fun nearest(): Int =
        if (stations.isEmpty()) 0 else (offset / pitch).roundToInt().coerceIn(0, stations.size - 1)

    private fun indexAt(x: Float): Int? {
        if (stations.isEmpty()) return null
        val k = ((x - width / 2f + offset) / pitch).roundToInt()
        return if (k in stations.indices) k else null
    }

    private fun snapTo(to: Int, tunes: Boolean) {
        if (stations.isEmpty()) return
        index = to.coerceIn(0, stations.size - 1)
        val target = index * pitch
        settleTunes = tunes
        settling = true
        scroller.startScroll(offset.toInt(), 0, (target - offset).toInt(), 0, 220)
        postInvalidateOnAnimation()
    }

    private val gestures = GestureDetector(ctx, object : GestureDetector.SimpleOnGestureListener() {
        override fun onDown(e: MotionEvent): Boolean {
            scroller.forceFinished(true)
            flinging = false
            settling = false
            tapped = false
            return true
        }

        override fun onScroll(e1: MotionEvent?, e2: MotionEvent, dx: Float, dy: Float): Boolean {
            if (stations.isEmpty()) return false
            parent?.requestDisallowInterceptTouchEvent(true)   // the channel list below must not steal it
            offset = (offset + dx).coerceIn(0f, (stations.size - 1) * pitch)
            invalidate()
            return true
        }

        override fun onFling(e1: MotionEvent?, e2: MotionEvent, vx: Float, vy: Float): Boolean {
            if (stations.isEmpty()) return false
            flinging = true
            scroller.fling(offset.toInt(), 0, (-vx / 2f).toInt(), 0, 0, ((stations.size - 1) * pitch).toInt(), 0, 0)
            postInvalidateOnAnimation()
            return true
        }

        override fun onSingleTapUp(e: MotionEvent): Boolean {
            tapped = true
            performClick()
            if (e.y < height * 0.48f) {          // the big name: play and pause
                onPlayPause?.invoke()
                return true
            }
            indexAt(e.x)?.let { snapTo(it, tunes = true) }
            return true
        }
    })

    override fun performClick(): Boolean {
        super.performClick()
        return true
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        val handled = gestures.onTouchEvent(event)
        val done = event.actionMasked == MotionEvent.ACTION_UP || event.actionMasked == MotionEvent.ACTION_CANCEL
        if (done && !tapped && !flinging && scroller.isFinished) snapTo(nearest(), tunes = true)
        return handled || super.onTouchEvent(event)
    }

    override fun computeScroll() {
        if (scroller.computeScrollOffset()) {
            offset = scroller.currX.toFloat()
            postInvalidateOnAnimation()
            return
        }
        if (flinging) {                 // a fling ran out: settle on whatever it stopped nearest
            flinging = false
            snapTo(nearest(), tunes = true)
            return
        }
        if (settling) {
            settling = false
            offset = index * pitch
            invalidate()
            if (settleTunes) stations.getOrNull(index)?.let { onTune?.invoke(it) }
        }
    }

    // ---------------------------------------------------------------- drawing
    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat()
        val h = height.toFloat()
        canvas.drawColor(col(R.color.bg))
        if (stations.isEmpty()) {
            big.textSize = min(h * 0.12f, dp(22f))
            big.color = col(R.color.dim)
            canvas.drawText("NO CHANNELS YET", w / 2f, h * 0.52f, big)
            return
        }
        val cx = w / 2f
        val here = nearest()
        val st = stations[here]
        val colour = Lcars.color(context, st.color)

        // what the needle is sitting on
        big.textSize = min(h * 0.17f, dp(42f))
        big.color = colour
        val name = TextUtils.ellipsize(st.name.uppercase(), big, w - dp(28f), TextUtils.TruncateAt.END)
        canvas.drawText(name, 0, name.length, cx, h * 0.31f, big)

        sub.textSize = min(h * 0.075f, dp(14f))
        sub.color = col(R.color.muted)
        canvas.drawText(subtitle(st), cx, h * 0.31f + sub.textSize * 1.8f, sub)

        // the scale
        val ruleY = h * 0.63f
        stroke.color = col(R.color.gold)
        stroke.strokeWidth = dp(2f)
        canvas.drawLine(0f, ruleY, w, ruleY, stroke)

        label.textSize = min(h * 0.07f, dp(13f))
        for ((k, s) in stations.withIndex()) {
            val x = cx + (k * pitch - offset)
            if (x < -pitch || x > w + pitch) continue
            val c = Lcars.color(context, s.color)
            val on = k == here
            stroke.color = c
            stroke.strokeWidth = if (on) dp(3f) else dp(1.5f)
            canvas.drawLine(x, ruleY, x, ruleY + (if (on) dp(17f) else dp(9f)), stroke)
            label.color = if (on) c else col(R.color.dim)
            val t = TextUtils.ellipsize(s.name.uppercase(), label, pitch - dp(12f), TextUtils.TruncateAt.END)
            canvas.drawText(t, 0, t.length, x, h * 0.93f, label)
        }

        // the needle
        stroke.color = col(R.color.orange)
        stroke.strokeWidth = dp(2f)
        canvas.drawLine(cx, ruleY - dp(10f), cx, h * 0.80f, stroke)
        fill.color = col(R.color.orange)
        val point = Path()
        point.moveTo(cx - dp(7f), ruleY - dp(15f))
        point.lineTo(cx + dp(7f), ruleY - dp(15f))
        point.lineTo(cx, ruleY - dp(3f))
        point.close()
        canvas.drawPath(point, fill)
    }

    private fun subtitle(s: Station): String {
        if (s.str("kind") == "favorites" || s.id == "favorites") return "${s.ready} hearted"
        val bits = ArrayList<String>()
        bits.add("${s.unplayed} new")
        bits.add("${s.ready} in all")
        if (s.queued > 0) bits.add("${s.queued} being made")
        return bits.joinToString(" · ")
    }
}
