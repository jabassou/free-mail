package fr.jabassou.freemail

import android.annotation.SuppressLint
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.webkit.CookieManager
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import org.json.JSONObject

/**
 * Opens the Free webmail in a WebView (it needs JavaScript, like on the Mac with Playwright),
 * fills in the stored credentials, waits for the ZM_AUTH_TOKEN cookie and hands the session
 * to the Python server so the Zimbra filter features work on the phone.
 */
class ZimbraLoginActivity : ComponentActivity() {
    private val base = "https://zimbra.free.fr/"
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var web: WebView
    private lateinit var status: android.widget.TextView
    private var fills = 0
    private var done = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setBackgroundColor(Palette.bg(this@ZimbraLoginActivity)) }
        val head = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(14), dp(20), dp(10))
        }
        head.addView(label(getString(R.string.zimbra_title), 18f, bold = true))
        status = label(getString(R.string.zimbra_opening), 13.5f, color = Palette.muted(this))
        head.addView(status)
        col.addView(head)
        col.addView(ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply { isIndeterminate = true })
        web = WebView(this)
        col.addView(web, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        setContentView(col)
        ViewCompat.setOnApplyWindowInsetsListener(col) { v, insets ->
            val b = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(b.left, b.top, b.right, b.bottom)
            WindowInsetsCompat.CONSUMED
        }

        CookieManager.getInstance().apply {
            setAcceptCookie(true)
            setAcceptThirdPartyCookies(web, true)
            removeAllCookies(null)
        }
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String) {
                if (!done) tryFill()
            }
        }
        web.loadUrl(base)
        handler.post(poll)
    }

    private val poll = object : Runnable {
        override fun run() {
            if (done) return
            val c = CookieManager.getInstance().getCookie(base)
            if (c != null && c.contains("ZM_AUTH_TOKEN=")) finishLogin(c) else handler.postDelayed(this, 700)
        }
    }

    /** Same strategy as the desktop Playwright login: first visible text field + password field, then submit. */
    private fun tryFill() {
        if (fills >= 8) return
        val user = Prefs(this).user ?: return
        val pw = SecretStore.get(this, "password") ?: return
        val js = """
            (function(u,p){
              var pw=document.querySelector('input[type=password]'); if(!pw) return 'nopw';
              var us=document.querySelector('input:not([type=hidden]):not([type=password]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio])');
              function set(el,v){ var d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value'); d.set.call(el,v);
                el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); }
              if(us) set(us,u); set(pw,p);
              var f=pw.form, b=f&&f.querySelector('[type=submit],button:not([type=button])');
              if(b) b.click(); else if(f) f.submit();
              return 'ok';
            })(${JSONObject.quote(user)},${JSONObject.quote(pw)})
        """.trimIndent()
        web.evaluateJavascript(js) { r ->
            if (r?.contains("ok") == true) {
                fills = 99
                status.text = getString(R.string.zimbra_sent)
            } else {
                fills++
                handler.postDelayed({ if (!done) tryFill() }, 800)
            }
        }
    }

    private fun finishLogin(cookie: String) {
        done = true
        status.text = getString(R.string.zimbra_reading)
        Thread {
            val res = try {
                JSONObject(Server.bridge().callAttr("zimbra_login", cookie).toString())
            } catch (e: Throwable) {
                JSONObject().put("error", e.message ?: e.toString())
            }
            runOnUiThread {
                if (res.has("error")) {
                    status.text = getString(R.string.zimbra_failed, res.optString("error"))
                } else {
                    Toast.makeText(this, getString(R.string.zimbra_ok, res.optInt("filters")), Toast.LENGTH_SHORT).show()
                    setResult(RESULT_OK)
                    finish()
                }
            }
        }.start()
    }

    override fun onDestroy() {
        done = true
        handler.removeCallbacksAndMessages(null)
        web.destroy()
        super.onDestroy()
    }
}
