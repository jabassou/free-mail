package fr.jabassou.freemail

import android.content.Context
import android.util.Base64
import java.security.SecureRandom

/** Non-secret settings. The password lives in [SecretStore]. */
class Prefs(ctx: Context) {
    private val sp = ctx.applicationContext.getSharedPreferences("fm", Context.MODE_PRIVATE)

    var user: String?
        get() = sp.getString("user", null)
        set(v) = sp.edit().putString("user", v).apply()

    /** Per-install token protecting the local API (other apps can reach 127.0.0.1, not the token). */
    val token: String
        get() = sp.getString("token", null) ?: run {
            val b = ByteArray(24).also { SecureRandom().nextBytes(it) }
            val t = Base64.encodeToString(b, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
            sp.edit().putString("token", t).apply()
            t
        }

    var interval: Int
        get() = sp.getInt("interval", 60)
        set(v) = sp.edit().putInt("interval", v).apply()

    var askedBattery: Boolean
        get() = sp.getBoolean("askedBattery", false)
        set(v) = sp.edit().putBoolean("askedBattery", v).apply()

    var assetsStamp: Long
        get() = sp.getLong("assetsStamp", 0L)
        set(v) = sp.edit().putLong("assetsStamp", v).apply()

    /** Biometric / device-credential lock when the app is opened. */
    var lock: Boolean
        get() = sp.getBoolean("lock", false)
        set(v) = sp.edit().putBoolean("lock", v).apply()

    fun configured(ctx: Context) = user != null && SecretStore.get(ctx, "password") != null
}
