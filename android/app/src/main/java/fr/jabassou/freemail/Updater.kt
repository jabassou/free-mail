package fr.jabassou.freemail

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import android.widget.Toast
import java.io.File
import java.net.URL
import javax.net.ssl.HttpsURLConnection

/** In-app update: downloads the release APK from GitHub and hands it to the system installer.
 *  Android only accepts it when it is signed with the same key as the installed app. */
object Updater {
    private const val TAG = "FreeMail"
    const val ACTION_STATUS = "fr.jabassou.freemail.INSTALL_STATUS"
    @Volatile private var running = false

    /** Returns "ok" when the download started, else a message for the user. */
    fun install(ctx: Context, url: String, onError: (String) -> Unit): String {
        if (!url.startsWith("https://github.com/")) return ctx.getString(R.string.update_bad_url)
        if (!ctx.packageManager.canRequestPackageInstalls()) {
            ctx.startActivity(
                Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${ctx.packageName}"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            return ctx.getString(R.string.update_allow_sources)
        }
        if (running) return ctx.getString(R.string.update_running)
        running = true
        val app = ctx.applicationContext
        Thread {
            val apk = File(app.cacheDir, "update.apk")
            try {
                download(url, apk)
                val installer = app.packageManager.packageInstaller
                val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
                    setAppPackageName(app.packageName)
                    if (Build.VERSION.SDK_INT >= 31) setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
                }
                val id = installer.createSession(params)
                installer.openSession(id).use { s ->
                    s.openWrite("base.apk", 0, apk.length()).use { out ->
                        apk.inputStream().use { it.copyTo(out) }
                        s.fsync(out)
                    }
                    // mutable: the installer adds the status extras; explicit component, so it cannot be redirected
                    val status = PendingIntent.getBroadcast(
                        app, 9, Intent(app, InstallReceiver::class.java).setAction(ACTION_STATUS),
                        PendingIntent.FLAG_MUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
                    )
                    s.commit(status.intentSender)
                }
            } catch (e: Throwable) {
                Log.e(TAG, "update failed", e)
                onError(app.getString(R.string.update_failed, e.message ?: e.javaClass.simpleName))
            } finally {
                apk.delete()
                running = false
            }
        }.start()
        return "ok"
    }

    /** GitHub release assets redirect to their storage host: follow https redirects only. */
    private fun download(url: String, dest: File) {
        var u = URL(url)
        repeat(6) {
            val c = u.openConnection() as HttpsURLConnection
            c.instanceFollowRedirects = false
            c.connectTimeout = 20_000
            c.readTimeout = 60_000
            c.setRequestProperty("Accept", "application/octet-stream")
            when (val code = c.responseCode) {
                in 300..399 -> {
                    val next = URL(u, c.getHeaderField("Location") ?: error("redirect without Location"))
                    c.disconnect()
                    require(next.protocol == "https") { "insecure redirect" }
                    u = next
                }
                200 -> {
                    c.inputStream.use { i -> dest.outputStream().use { i.copyTo(it) } }
                    require(dest.length() > 1_000_000) { "file too small" }
                    return
                }
                else -> error("HTTP $code")
            }
        }
        error("too many redirects")
    }
}

/** Result of the PackageInstaller session. */
class InstallReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, Int.MIN_VALUE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirm: Intent? = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                else @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_INTENT)
                confirm?.let { ctx.startActivity(it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
            }
            PackageInstaller.STATUS_SUCCESS -> Unit // the new version restarts through MY_PACKAGE_REPLACED
            else -> {
                val msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: "?"
                Log.w("FreeMail", "install failed: $msg")
                Toast.makeText(ctx, ctx.getString(R.string.update_failed, msg), Toast.LENGTH_LONG).show()
            }
        }
    }
}
