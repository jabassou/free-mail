package fr.jabassou.freemail

import android.content.Intent
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

/** First launch (or account change): Free address + password, checked over IMAP before saving. */
class SetupActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val prefs = Prefs(this)

        val root = ScrollView(this).apply { setBackgroundColor(Palette.bg(this@SetupActivity)); isFillViewport = true }
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(28), dp(28), dp(28), dp(28))
        }
        root.addView(col, MATCH_PARENT, MATCH_PARENT)

        val logo = ImageView(this).apply {
            setImageResource(R.drawable.ic_launcher_fg)
            background = gradient(20)
        }
        col.addView(logo, LinearLayout.LayoutParams(dp(72), dp(72)))
        col.addView(label(getString(R.string.app_name), 30f, bold = true).apply { setPadding(0, dp(18), 0, dp(4)) })
        col.addView(label(getString(R.string.setup_subtitle), 15f, color = Palette.muted(this)).apply { setPadding(0, 0, 0, dp(26)) })

        fun field(hint: String, type: Int) = EditText(this).apply {
            this.hint = hint
            inputType = type
            setSingleLine()
            setTextColor(Palette.ink(this@SetupActivity))
            setHintTextColor(Palette.muted(this@SetupActivity))
            background = rounded(Palette.card(this@SetupActivity), 14, Palette.line(this@SetupActivity))
            setPadding(dp(16), dp(14), dp(16), dp(14))
        }
        val email = field(getString(R.string.setup_email_hint), InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS).apply { setText(prefs.user ?: "") }
        val pw = field(getString(R.string.setup_password_hint), InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD).apply { imeOptions = EditorInfo.IME_ACTION_DONE }
        val lp = { LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { bottomMargin = dp(12) } }
        col.addView(label(getString(R.string.setup_email), 13f, bold = true, color = Palette.muted(this)).apply { setPadding(dp(4), 0, 0, dp(6)) })
        col.addView(email, lp())
        col.addView(label(getString(R.string.setup_password), 13f, bold = true, color = Palette.muted(this)).apply { setPadding(dp(4), dp(4), 0, dp(6)) })
        col.addView(pw, lp())

        val err = label("", 14f, color = Palette.ERR).apply { visible(false) }
        col.addView(err, lp())
        val go = Button(this).apply {
            text = getString(R.string.sign_in)
            isAllCaps = false
            setTextColor(android.graphics.Color.WHITE)
            textSize = 16f
            background = gradient(14)
            stateListAnimator = null
        }
        col.addView(go, LinearLayout.LayoutParams(MATCH_PARENT, dp(54)))
        val spin = ProgressBar(this).apply { visible(false) }
        col.addView(spin, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { gravity = Gravity.CENTER_HORIZONTAL; topMargin = dp(16) })
        col.addView(label(
            getString(R.string.setup_note),
            12.5f, color = Palette.muted(this)
        ).apply { setPadding(dp(4), dp(22), dp(4), 0) })

        setContentView(root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val b = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(b.left, b.top, b.right, b.bottom)
            WindowInsetsCompat.CONSUMED
        }

        fun submit() {
            val u = email.text.toString().trim().lowercase()
            val p = pw.text.toString()
            if (!u.contains("@") || p.isEmpty()) {
                err.text = getString(R.string.setup_missing)
                err.visible(true)
                return
            }
            go.isEnabled = false
            spin.visible(true)
            err.visible(false)
            Thread {
                val res = try {
                    Server.bridge().callAttr("test_login", Server.home(this).absolutePath, u, p).toString()
                } catch (e: Throwable) {
                    e.message ?: e.toString()
                }
                runOnUiThread {
                    spin.visible(false)
                    go.isEnabled = true
                    if (res.isNotEmpty()) {
                        err.text = if (res.startsWith("AUTH:")) getString(R.string.setup_refused) else res
                        err.visible(true)
                        return@runOnUiThread
                    }
                    prefs.user = u
                    SecretStore.put(this, "password", p)
                    if (Server.port != 0) Thread { Server.bridge().callAttr("set_password", p) }.start()
                    MailService.start(this)
                    startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP))
                    finish()
                }
            }.start()
        }
        go.setOnClickListener { submit() }
        pw.setOnEditorActionListener { _, id, _ -> if (id == EditorInfo.IME_ACTION_DONE) { submit(); true } else false }
    }
}
