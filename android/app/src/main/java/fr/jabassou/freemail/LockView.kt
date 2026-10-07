package fr.jabassou.freemail

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView

/** Covers the mailbox until the fingerprint / phone PIN check succeeds. */
class LockView(ctx: Context, private val onUnlock: () -> Unit) : FrameLayout(ctx) {
    private val message: TextView

    init {
        visibility = View.GONE
        isClickable = true // nothing behind it can be touched
        background = GradientDrawable(
            GradientDrawable.Orientation.TL_BR,
            intArrayOf(Color.parseColor("#7C3AED"), Color.parseColor("#4F46E5"), Color.parseColor("#06B6D4"))
        )
        val col = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER_HORIZONTAL }
        col.addView(ImageView(ctx).apply {
            setImageResource(R.drawable.ic_launcher_fg)
            background = GradientDrawable().apply {
                cornerRadius = ctx.dp(30).toFloat()
                setColor(Color.argb(46, 255, 255, 255))
            }
        }, LinearLayout.LayoutParams(ctx.dp(96), ctx.dp(96)))
        col.addView(TextView(ctx).apply {
            text = ctx.getString(R.string.lock_title)
            setTextColor(Color.WHITE)
            textSize = 24f
            typeface = Typeface.create("sans-serif-medium", Typeface.BOLD)
            setPadding(0, ctx.dp(20), 0, ctx.dp(6))
        }, WRAP_CONTENT, WRAP_CONTENT)
        message = TextView(ctx).apply {
            text = ctx.getString(R.string.lock_hint)
            setTextColor(Color.argb(210, 255, 255, 255))
            textSize = 14f
            gravity = Gravity.CENTER
            setPadding(ctx.dp(32), 0, ctx.dp(32), 0)
        }
        col.addView(message, WRAP_CONTENT, WRAP_CONTENT)
        col.addView(Button(ctx).apply {
            text = ctx.getString(R.string.lock_unlock)
            isAllCaps = false
            textSize = 16f
            setTextColor(Color.parseColor("#4F46E5"))
            background = GradientDrawable().apply { cornerRadius = ctx.dp(16).toFloat(); setColor(Color.WHITE) }
            setPadding(ctx.dp(28), 0, ctx.dp(28), 0)
            setOnClickListener { onUnlock() }
        }, LinearLayout.LayoutParams(WRAP_CONTENT, ctx.dp(52)).apply { topMargin = ctx.dp(28) })
        addView(col, LayoutParams(MATCH_PARENT, WRAP_CONTENT, Gravity.CENTER))
    }

    fun setMessage(s: String) {
        message.text = s.ifBlank { context.getString(R.string.lock_hint) }
    }
}
