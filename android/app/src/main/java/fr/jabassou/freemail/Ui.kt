package fr.jabassou.freemail

import android.content.Context
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.util.TypedValue
import android.view.View
import android.widget.TextView

/** Tiny helpers for the two native screens (setup, webmail login); everything else is the web UI. */
internal fun Context.dp(v: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()

/** (versionName, versionCode) of the installed APK. */
internal fun Context.appVersion(): Pair<String, Long> = try {
    val pi = packageManager.getPackageInfo(packageName, 0)
    (pi.versionName ?: "?") to (if (android.os.Build.VERSION.SDK_INT >= 28) pi.longVersionCode else @Suppress("DEPRECATION") pi.versionCode.toLong())
} catch (e: Exception) {
    "?" to 0L
}

internal fun Context.isNight() =
    (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES

internal object Palette {
    fun bg(c: Context) = if (c.isNight()) Color.parseColor("#0B0D14") else Color.parseColor("#F6F7FB")
    fun card(c: Context) = if (c.isNight()) Color.parseColor("#151826") else Color.WHITE
    fun ink(c: Context) = if (c.isNight()) Color.parseColor("#E8E9F1") else Color.parseColor("#151826")
    fun muted(c: Context) = if (c.isNight()) Color.parseColor("#9AA0B4") else Color.parseColor("#5B6175")
    fun line(c: Context) = if (c.isNight()) Color.parseColor("#2A2F42") else Color.parseColor("#DDE0EA")
    const val ERR = 0xFFEF4444.toInt()
}

internal fun Context.label(s: String, sp: Float, bold: Boolean = false, color: Int = Palette.ink(this)) = TextView(this).apply {
    text = s
    setTextSize(TypedValue.COMPLEX_UNIT_SP, sp)
    setTextColor(color)
    if (bold) typeface = Typeface.DEFAULT_BOLD
}

internal fun Context.rounded(color: Int, radiusDp: Int, stroke: Int? = null) = GradientDrawable().apply {
    setColor(color)
    cornerRadius = dp(radiusDp).toFloat()
    if (stroke != null) setStroke(dp(1), stroke)
}

internal fun Context.gradient(radiusDp: Int) = GradientDrawable(
    GradientDrawable.Orientation.TL_BR, intArrayOf(Color.parseColor("#7C3AED"), Color.parseColor("#06B6D4"))
).apply { cornerRadius = dp(radiusDp).toFloat() }

internal fun View.visible(on: Boolean) {
    visibility = if (on) View.VISIBLE else View.GONE
}
