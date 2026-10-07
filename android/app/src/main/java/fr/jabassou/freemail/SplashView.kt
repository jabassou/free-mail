package fr.jabassou.freemail

import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.SystemClock
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.view.animation.DecelerateInterpolator
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView

/** Branded loading screen shown while the embedded Python server starts and the UI loads. */
class SplashView(ctx: Context) : FrameLayout(ctx) {
    private val status: TextView
    private val shownAt = SystemClock.elapsedRealtime()
    private val pulse: ObjectAnimator

    init {
        background = GradientDrawable(
            GradientDrawable.Orientation.TL_BR,
            intArrayOf(Color.parseColor("#7C3AED"), Color.parseColor("#4F46E5"), Color.parseColor("#06B6D4"))
        )
        isClickable = true // swallow touches while loading

        // soft decorative circles
        fun blob(sizeDp: Int, alpha: Int, g: Int, mx: Int, my: Int) = View(ctx).apply {
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(Color.argb(alpha, 255, 255, 255)) }
            layoutParams = LayoutParams(ctx.dp(sizeDp), ctx.dp(sizeDp), g).apply { setMargins(mx, my, mx, my) }
        }
        addView(blob(320, 18, Gravity.TOP or Gravity.END, -ctx.dp(110), -ctx.dp(90)))
        addView(blob(260, 14, Gravity.BOTTOM or Gravity.START, -ctx.dp(110), -ctx.dp(70)))

        val col = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER_HORIZONTAL }
        val logo = ImageView(ctx).apply {
            setImageResource(R.drawable.ic_launcher_fg)
            background = GradientDrawable().apply {
                cornerRadius = ctx.dp(34).toFloat()
                setColor(Color.argb(46, 255, 255, 255))
                setStroke(ctx.dp(1), Color.argb(70, 255, 255, 255))
            }
            elevation = ctx.dp(10).toFloat()
        }
        col.addView(logo, LinearLayout.LayoutParams(ctx.dp(120), ctx.dp(120)))
        col.addView(TextView(ctx).apply {
            text = ctx.getString(R.string.app_name)
            setTextColor(Color.WHITE)
            textSize = 34f
            typeface = Typeface.create("sans-serif-medium", Typeface.BOLD)
            letterSpacing = -0.01f
            setPadding(0, ctx.dp(22), 0, ctx.dp(4))
        }, WRAP_CONTENT, WRAP_CONTENT)
        col.addView(TextView(ctx).apply {
            text = ctx.getString(R.string.splash_tagline)
            setTextColor(Color.argb(215, 255, 255, 255))
            textSize = 15f
        }, WRAP_CONTENT, WRAP_CONTENT)
        col.addView(ProgressBar(ctx, null, android.R.attr.progressBarStyleHorizontal).apply {
            isIndeterminate = true
            indeterminateTintList = ColorStateList.valueOf(Color.WHITE)
            if (Build.VERSION.SDK_INT >= 29) progressBackgroundTintList = ColorStateList.valueOf(Color.argb(60, 255, 255, 255))
        }, LinearLayout.LayoutParams(ctx.dp(150), ctx.dp(4)).apply { topMargin = ctx.dp(36) })
        status = TextView(ctx).apply {
            text = ctx.getString(R.string.splash_starting)
            setTextColor(Color.argb(190, 255, 255, 255))
            textSize = 13f
            setPadding(0, ctx.dp(12), 0, 0)
        }
        col.addView(status, WRAP_CONTENT, WRAP_CONTENT)
        addView(col, LayoutParams(MATCH_PARENT, WRAP_CONTENT, Gravity.CENTER))

        val (name, code) = ctx.appVersion()
        addView(TextView(ctx).apply {
            text = "v$name · build $code"
            setTextColor(Color.argb(150, 255, 255, 255))
            textSize = 12f
            gravity = Gravity.CENTER
        }, LayoutParams(MATCH_PARENT, WRAP_CONTENT, Gravity.BOTTOM).apply { bottomMargin = ctx.dp(36) })

        // entrance + breathing logo
        col.alpha = 0f
        col.translationY = ctx.dp(18).toFloat()
        col.animate().alpha(1f).translationY(0f).setDuration(520).setInterpolator(DecelerateInterpolator()).start()
        pulse = ObjectAnimator.ofFloat(logo, View.SCALE_X, 1f, 1.06f).apply {
            duration = 1100
            repeatCount = ValueAnimator.INFINITE
            repeatMode = ValueAnimator.REVERSE
            addUpdateListener { logo.scaleY = logo.scaleX }
            start()
        }
    }

    fun setStatus(s: String) {
        status.text = s
    }

    /** Fades out (keeps it on screen at least ~0.9 s so it never just flashes). */
    fun dismiss(after: () -> Unit = {}) {
        val wait = (900 - (SystemClock.elapsedRealtime() - shownAt)).coerceAtLeast(0)
        postDelayed({
            animate().alpha(0f).scaleX(1.04f).scaleY(1.04f).setDuration(380).withEndAction {
                pulse.cancel()
                visibility = View.GONE
                after()
            }.start()
        }, wait)
    }
}
