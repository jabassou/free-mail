package fr.jabassou.freemail

import android.content.Context
import android.util.Log
import com.chaquo.python.Python
import java.io.File

/** The Python backend (webui.py) running in-process on 127.0.0.1:<random port>. */
object Server {
    private const val TAG = "FreeMail"
    @Volatile var port = 0
        private set
    private val lock = Any()

    fun py(): Python = Python.getInstance()
    fun bridge() = py().getModule("android_bridge")
    fun home(ctx: Context) = File(ctx.filesDir, "fm")

    /** Starts the server once; returns its base URL, or null when no account is configured. */
    fun ensure(ctx: Context): String? {
        synchronized(lock) {
            if (port != 0) return url()
            val prefs = Prefs(ctx)
            val user = prefs.user ?: return null
            val pw = SecretStore.get(ctx, "password") ?: return null
            syncWebAssets(ctx)
            port = bridge().callAttr("start", home(ctx).absolutePath, user, pw, prefs.token, prefs.interval, java.util.Locale.getDefault().language,
                ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName ?: "").toInt()
            Log.i(TAG, "server on 127.0.0.1:$port")
            return url()
        }
    }

    fun url() = "http://127.0.0.1:$port/"

    /** Copies the web UI from the APK assets to files/fm/web when the app was (re)installed. */
    private fun syncWebAssets(ctx: Context) {
        val prefs = Prefs(ctx)
        val stamp = ctx.packageManager.getPackageInfo(ctx.packageName, 0).lastUpdateTime
        val dest = File(home(ctx), "web")
        if (prefs.assetsStamp == stamp && dest.isDirectory) return
        dest.deleteRecursively()
        copyAssetDir(ctx, "web", dest)
        prefs.assetsStamp = stamp
    }

    private fun copyAssetDir(ctx: Context, path: String, dest: File) {
        val children: Array<String> = ctx.assets.list(path) ?: emptyArray()
        if (children.isEmpty()) {
            dest.parentFile?.mkdirs()
            ctx.assets.open(path).use { input -> dest.outputStream().use { input.copyTo(it) } }
            return
        }
        dest.mkdirs()
        for (c in children) copyAssetDir(ctx, "$path/$c", File(dest, c))
    }
}
