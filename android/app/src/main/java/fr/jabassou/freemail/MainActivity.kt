package fr.jabassou.freemail

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.ActivityNotFoundException
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.hardware.biometrics.BiometricManager
import android.hardware.biometrics.BiometricPrompt
import android.os.Build
import android.os.CancellationSignal
import android.os.Bundle
import android.os.Message
import android.os.PowerManager
import android.provider.MediaStore
import android.provider.Settings
import android.util.Base64
import android.util.Log
import android.view.Gravity
import android.webkit.JavascriptInterface
import android.webkit.RenderProcessGoneDetail
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import android.view.View
import androidx.core.view.WindowInsetsCompat
import org.json.JSONObject

private const val LOCK_GRACE_MS = 30_000L // switching apps briefly does not ask again
private const val OPEN_COMPOSE_JS =
    "(function(){var t=window.FMAndroid&&FMAndroid.takeComposeTo();location.hash='compose'+(t?'&to='+encodeURIComponent(t):'')})()"

/** The app = the existing web UI in a WebView, served by the in-process Python backend. */
class MainActivity : ComponentActivity() {
    private lateinit var web: WebView
    private lateinit var splash: SplashView
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var loaded = false
    private var pendingHash: String? = null
    /** mailto: address handed to the page through FMAndroid.takeComposeTo(), never inlined in JavaScript. */
    @Volatile private var composeTo: String? = null
    private var pageReady = false
    private lateinit var content: FrameLayout
    private lateinit var lockView: LockView
    /** Theme chosen in the web UI (auto/light/dark): drives the system bars and the window background. */
    private var darkUi: Boolean? = null
    private var unlocked = false
    private var pausedAt = 0L
    /** The system prompt pauses/resumes the activity: never stack prompts, and after "cancel" wait for the button. */
    private var prompting = false
    private var dismissed = false

    /** Same hash twice would not fire hashchange: clear it first, then set it. */
    private fun openHash(h: String) {
        web.evaluateJavascript("history.replaceState(null,'',location.pathname);location.hash=" + JSONObject.quote(h.removePrefix("#")), null)
    }

    private val pickFiles = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
        val uris: Array<Uri>? = if (r.resultCode != RESULT_OK) null else
            r.data?.clipData?.let { c -> Array(c.itemCount) { c.getItemAt(it).uri } }
                ?: WebChromeClient.FileChooserParams.parseResult(r.resultCode, r.data)
        fileCallback?.onReceiveValue(uris)
        fileCallback = null
    }
    private val zimbraLogin = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
        web.evaluateJavascript("window.fmZimbraDone&&fmZimbraDone(${r.resultCode == RESULT_OK})", null)
    }
    private val askNotif = registerForActivityResult(ActivityResultContracts.RequestPermission()) { maybeAskBattery() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Prefs(this).configured(this)) {
            startActivity(Intent(this, SetupActivity::class.java))
            finish()
            return
        }
        enableEdgeToEdge()
        val root = FrameLayout(this)
        content = FrameLayout(this).apply { setBackgroundColor(Palette.bg(this@MainActivity)) }
        web = WebView(this).apply { setBackgroundColor(Palette.bg(this@MainActivity)) }
        splash = SplashView(this)
        content.addView(web, FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT)
        root.addView(content, FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT)
        root.addView(splash, FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT)
        lockView = LockView(this) { authenticate() }
        root.addView(lockView, FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT)
        setContentView(root)
        applyLockPrivacy()
        // white status-bar icons over the gradient splash; theme colours once the app is shown
        WindowCompat.getInsetsController(window, root).isAppearanceLightStatusBars = false
        ViewCompat.setOnApplyWindowInsetsListener(content) { v, insets ->
            val b = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(b.left, b.top, b.right, b.bottom)
            WindowInsetsCompat.CONSUMED
        }
        setupWebView()
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else moveTaskToBack(true)
            }
        })
        MailService.start(this)
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) askNotif.launch(Manifest.permission.POST_NOTIFICATIONS) else maybeAskBattery()
        load(intent)
    }

    private fun load(intent: Intent?) {
        splash.setStatus(getString(if (Server.port == 0) R.string.splash_engine else R.string.splash_connecting))
        Thread {
            val url = try {
                Server.ensure(this)
            } catch (e: Throwable) {
                Log.e("FreeMail", "server", e)
                null
            }
            runOnUiThread {
                if (url == null) {
                    Toast.makeText(this, R.string.err_server, Toast.LENGTH_LONG).show()
                    startActivity(Intent(this, SetupActivity::class.java))
                    return@runOnUiThread
                }
                splash.setStatus(getString(R.string.splash_connecting))
                web.loadUrl(url + "?t=" + Prefs(this).token + hashFor(intent))
                loaded = true
            }
        }.start()
    }

    /** Notification extras / mailto: intents -> deep link understood by index.html. */
    private fun hashFor(intent: Intent?): String {
        if (intent == null) return ""
        // extras can come from any app (the activity is exported for mailto:): validate before use
        intent.getStringExtra("folder")?.takeIf { it.isNotBlank() && it.length <= 200 }?.let { f ->
            val uid = intent.getStringExtra("uid")?.takeIf { it.matches(Regex("\\d{1,10}")) }
            return "#m=" + Uri.encode(f) + (if (uid != null) "&u=$uid" else "")
        }
        val data = intent.data
        if (data?.scheme == "mailto") {
            val to = data.schemeSpecificPart.substringBefore('?').trim()
            return if (android.util.Patterns.EMAIL_ADDRESS.matcher(to).matches()) "#compose&to=" + Uri.encode(to) else "#compose"
        }
        return ""
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val h = hashFor(intent)
        if (h.isEmpty()) return
        if (loaded && pageReady) openHash(h) else pendingHash = h // page still starting: applied in onPageFinished
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            setSupportMultipleWindows(true)
            javaScriptCanOpenWindowsAutomatically = false
            allowFileAccess = false
            allowContentAccess = false
            userAgentString = "$userAgentString FreeMailAndroid"
        }
        web.addJavascriptInterface(JsApi(), "FMAndroid")
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                if (request.url.host == "127.0.0.1") return false
                openExternal(request.url)
                return true
            }

            override fun onPageFinished(view: WebView, url: String) {
                pageReady = true
                pendingHash?.let { pendingHash = null; openHash(it) }
                if (splash.visibility == View.VISIBLE) splash.dismiss {
                    applyBars()
                }
            }

            override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
                recreate()
                return true
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(view: WebView, cb: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = cb
                return try {
                    pickFiles.launch(params.createIntent().apply {
                        if (params.mode == FileChooserParams.MODE_OPEN_MULTIPLE) putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                    })
                    true
                } catch (e: ActivityNotFoundException) {
                    fileCallback = null
                    false
                }
            }

            // links with target=_blank (mail bodies, unsubscribe): open in the browser, never in the app
            override fun onCreateWindow(view: WebView, isDialog: Boolean, isUserGesture: Boolean, resultMsg: Message): Boolean {
                val hit = view.hitTestResult
                if ((hit.type == WebView.HitTestResult.SRC_ANCHOR_TYPE || hit.type == WebView.HitTestResult.SRC_IMAGE_ANCHOR_TYPE) && hit.extra != null) {
                    openExternal(Uri.parse(hit.extra))
                    return false
                }
                val tmp = WebView(this@MainActivity)
                tmp.webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(v: WebView, r: WebResourceRequest): Boolean {
                        openExternal(r.url)
                        v.destroy()
                        return true
                    }
                }
                (resultMsg.obj as WebView.WebViewTransport).webView = tmp
                resultMsg.sendToTarget()
                return true
            }
        }
    }

    private fun openExternal(uri: Uri) {
        if (uri.scheme == "mailto") {
            val to = uri.schemeSpecificPart.substringBefore('?').trim()
            composeTo = if (android.util.Patterns.EMAIL_ADDRESS.matcher(to).matches()) to else ""
            web.evaluateJavascript(OPEN_COMPOSE_JS, null)
            return
        }
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: ActivityNotFoundException) {
            Toast.makeText(this, R.string.err_no_app, Toast.LENGTH_SHORT).show()
        }
    }

    private fun maybeAskBattery() {
        val prefs = Prefs(this)
        val pm = getSystemService(PowerManager::class.java)
        if (prefs.askedBattery || pm.isIgnoringBatteryOptimizations(packageName)) return
        prefs.askedBattery = true
        AlertDialog.Builder(this)
            .setTitle(R.string.battery_title)
            .setMessage(R.string.battery_msg)
            .setPositiveButton(R.string.allow) { _, _ ->
                try {
                    @SuppressLint("BatteryLife")
                    val i = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
                    startActivity(i)
                } catch (e: ActivityNotFoundException) {
                    startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
                }
            }
            .setNegativeButton(R.string.later, null)
            .show()
    }

    override fun onResume() {
        super.onResume()
        if (::web.isInitialized) web.onResume()
        setForeground(true)
        if (::lockView.isInitialized) {
            val prefs = Prefs(this)
            val stale = android.os.SystemClock.elapsedRealtime() - pausedAt > LOCK_GRACE_MS
            if (prefs.lock && (!unlocked || stale)) {
                unlocked = false
                lockView.visibility = View.VISIBLE
                if (!dismissed) authenticate()
            } else lockView.visibility = View.GONE
        }
    }

    override fun onPause() {
        if (::web.isInitialized) web.onPause()
        setForeground(false)
        if (unlocked) pausedAt = android.os.SystemClock.elapsedRealtime()
        super.onPause()
    }

    /** Fingerprint / face, or the phone PIN / pattern as fallback. */
    private fun authenticate() {
        if (prompting) return
        prompting = true
        dismissed = false
        val b = BiometricPrompt.Builder(this)
            .setTitle(getString(R.string.lock_title))
            .setSubtitle(Prefs(this).user ?: "")
        if (Build.VERSION.SDK_INT >= 30) {
            b.setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_WEAK or BiometricManager.Authenticators.DEVICE_CREDENTIAL)
        } else {
            @Suppress("DEPRECATION") b.setDeviceCredentialAllowed(true)
        }
        try {
            b.build().authenticate(CancellationSignal(), mainExecutor, object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    prompting = false
                    unlocked = true
                    pausedAt = android.os.SystemClock.elapsedRealtime()
                    lockView.visibility = View.GONE
                }

                override fun onAuthenticationError(code: Int, msg: CharSequence) {
                    prompting = false
                    dismissed = true
                    lockView.setMessage(msg.toString()) // stays locked; the button retries
                }
            })
        } catch (e: Exception) {
            prompting = false
            Log.e("FreeMail", "biometric prompt", e)
            lockView.setMessage(e.message ?: "")
        }
    }

    /** With the lock on, the app preview in the recent-apps screen is hidden too. */
    private fun applyLockPrivacy() {
        if (Build.VERSION.SDK_INT >= 33) setRecentsScreenshotEnabled(!Prefs(this).lock)
    }

    private fun applyBars() {
        val dark = darkUi ?: isNight()
        val c = WindowCompat.getInsetsController(window, web)
        c.isAppearanceLightStatusBars = !dark
        c.isAppearanceLightNavigationBars = !dark
    }

    /** Notifications are muted only while the UI is really on screen (the WebView keeps running when the screen is off). */
    private fun setForeground(on: Boolean) = Thread {
        try {
            if (Server.port != 0) Server.bridge().callAttr("set_foreground", on)
        } catch (_: Throwable) {
        }
    }.start()

    override fun onStop() {
        dismissed = false // leaving the app: ask again automatically next time
        super.onStop()
    }

    override fun onDestroy() {
        if (::web.isInitialized) web.destroy()
        super.onDestroy()
    }

    /** Exposed to index.html as window.FMAndroid (only our 127.0.0.1 page runs scripts). */
    inner class JsApi {
        /** One-shot read of the address from the last mailto: link (see openExternal). */
        @JavascriptInterface
        fun takeComposeTo(): String = (composeTo ?: "").also { composeTo = null }

        /** Web theme changed: match the window background (behind the navigation bar) and the bar icons. */
        @JavascriptInterface
        fun setDark(dark: Boolean) = runOnUiThread {
            darkUi = dark
            val bg = if (dark) 0xFF0A0C11.toInt() else 0xFFF4F5F9.toInt()
            content.setBackgroundColor(bg)
            web.setBackgroundColor(bg)
            window.decorView.setBackgroundColor(bg)
            if (splash.visibility != View.VISIBLE) applyBars()
        }

        @JavascriptInterface
        fun copy(text: String): String = try {
            val cm = getSystemService(android.content.ClipboardManager::class.java)
            cm.setPrimaryClip(android.content.ClipData.newPlainText("Free Mail", text))
            "ok"
        } catch (e: Exception) {
            "error: ${e.message}"
        }

        @JavascriptInterface
        fun lockAvailable(): Boolean = getSystemService(android.app.KeyguardManager::class.java).isDeviceSecure

        @JavascriptInterface
        fun lockEnabled(): Boolean = Prefs(this@MainActivity).lock

        @JavascriptInterface
        fun setLock(on: Boolean): String {
            if (on && !lockAvailable()) return getString(R.string.lock_no_secure)
            Prefs(this@MainActivity).lock = on
            unlocked = true
            pausedAt = android.os.SystemClock.elapsedRealtime()
            runOnUiThread { applyLockPrivacy() }
            return "ok"
        }

        @JavascriptInterface
        fun installUpdate(url: String, version: String): String =
            Updater.install(this@MainActivity, url) { msg ->
                runOnUiThread { Toast.makeText(this@MainActivity, msg, Toast.LENGTH_LONG).show() }
            }

        @JavascriptInterface
        fun saveFile(name: String, mime: String, b64: String): String = try {
            val bytes = Base64.decode(b64, Base64.DEFAULT)
            val safe = name.replace(Regex("[\\\\/:*?\"<>|]"), "_").ifBlank { "piece-jointe" }
            val type = mime.ifBlank { "application/octet-stream" }
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, safe)
                put(MediaStore.Downloads.MIME_TYPE, type)
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IllegalStateException("MediaStore refused")
            contentResolver.openOutputStream(uri)?.use { it.write(bytes) }
            values.clear()
            values.put(MediaStore.Downloads.IS_PENDING, 0)
            contentResolver.update(uri, values, null, null)
            runOnUiThread {
                Toast.makeText(this@MainActivity, getString(R.string.saved_downloads, safe), Toast.LENGTH_SHORT).show()
                try {
                    startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, type).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
                } catch (_: ActivityNotFoundException) {
                }
            }
            "ok"
        } catch (e: Exception) {
            "error: ${e.message}"
        }

        /** Links from mails / unsubscribe pages: always in the user's browser, never inside the app. */
        @JavascriptInterface
        fun openUrl(url: String) {
            val u = Uri.parse(url)
            if (u.scheme in setOf("http", "https", "mailto")) runOnUiThread { openExternal(u) }
        }

        @JavascriptInterface
        fun version(): String = appVersion().let { (n, c) -> "$n · build $c" }

        @JavascriptInterface
        fun zimbraLogin() = runOnUiThread { zimbraLogin.launch(Intent(this@MainActivity, ZimbraLoginActivity::class.java)) }

        @JavascriptInterface
        fun changeAccount() = runOnUiThread { startActivity(Intent(this@MainActivity, SetupActivity::class.java)) }

        /** Background-delivery diagnostics shown in the drawer. */
        @JavascriptInterface
        fun status(): String {
            val pm = getSystemService(PowerManager::class.java)
            val am = getSystemService(android.app.AlarmManager::class.java)
            return JSONObject()
                .put("notifications", Bridge.canNotify() && androidx.core.app.NotificationManagerCompat.from(this@MainActivity).areNotificationsEnabled())
                .put("battery", pm.isIgnoringBatteryOptimizations(packageName))
                .put("exact", Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms())
                .put("last", Bridge.lastCheck)
                .toString()
        }

        @JavascriptInterface
        fun fixBackground() = runOnUiThread {
            val notifOk = Bridge.canNotify() && androidx.core.app.NotificationManagerCompat.from(this@MainActivity).areNotificationsEnabled()
            try {
                if (!notifOk) {
                    startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName))
                } else {
                    @SuppressLint("BatteryLife")
                    val i = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
                    startActivity(i)
                }
            } catch (_: ActivityNotFoundException) {
                startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            }
        }

        @JavascriptInterface
        fun notificationSettings() = runOnUiThread {
            startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName))
        }
    }
}
