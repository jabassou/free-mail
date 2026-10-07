package fr.jabassou.freemail

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.RemoteInput

/** Restart the watcher after a reboot or an app update. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (Prefs(ctx).configured(ctx)) MailService.start(ctx)
    }
}

/** "Mark read" and inline "Reply" buttons of a new-mail notification. */
class ActionReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val folder = intent.getStringExtra("folder") ?: return
        val uid = intent.getStringExtra("uid") ?: return
        val nid = intent.getIntExtra("nid", 0)
        when (intent.action) {
            READ -> {
                NotificationManagerCompat.from(ctx).cancel(nid)
                background("mark seen") {
                    Server.ensure(ctx)
                    Server.bridge().callAttr("mark_seen", folder, uid)
                }
            }
            REPLY -> {
                val text = RemoteInput.getResultsFromIntent(intent)?.getCharSequence(KEY_TEXT)?.toString()?.trim()
                if (text.isNullOrEmpty()) return
                // Starting Python + IMAP + SMTP can exceed a receiver's 10 s budget: run it in a plain thread,
                // the foreground MailService keeps the process alive meanwhile.
                MailService.start(ctx)
                Thread {
                    try {
                        Server.ensure(ctx) ?: error("not configured")
                        Server.bridge().callAttr("quick_reply", folder, uid, text)
                        Bridge.replied(nid, true)
                    } catch (e: Throwable) {
                        Log.e("FreeMail", "quick reply failed", e)
                        Bridge.replied(nid, false, e.message?.substringAfterLast(": ")?.take(160) ?: "")
                    }
                }.start()
            }
        }
    }

    /** Network work off the main thread, keeping the receiver (and the CPU) alive until it is done. */
    private fun background(what: String, block: () -> Unit) {
        val pending = goAsync()
        Thread {
            try {
                block()
            } catch (e: Throwable) {
                Log.e("FreeMail", "$what failed", e)
            } finally {
                pending.finish()
            }
        }.start()
    }

    companion object {
        const val READ = "fr.jabassou.freemail.READ"
        const val REPLY = "fr.jabassou.freemail.REPLY"
        const val KEY_TEXT = "reply_text"
    }
}

/** Wakes the watcher periodically, including in Doze (Android then spaces alarms to ~10 min). */
class KickReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        schedule(ctx)
        // Keep the CPU awake for the whole check (fresh IMAP connection + STATUS of every folder),
        // otherwise the phone falls back asleep halfway and the notification never comes.
        val wl = ctx.getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "freemail:check")
            .apply { setReferenceCounted(false); acquire(60_000L) }
        Thread {
            try {
                if (Server.ensure(ctx) != null) Server.bridge().callAttr("check_now")
            } catch (e: Throwable) {
                Log.e("FreeMail", "check failed", e)
            } finally {
                if (wl.isHeld) wl.release()
            }
        }.start()
    }

    companion object {
        private const val PERIOD = 5 * 60_000L // Doze stretches it to ~9-15 min, IMAP IDLE covers the inbox instantly

        fun schedule(ctx: Context) {
            val am = ctx.getSystemService(AlarmManager::class.java)
            val pi = PendingIntent.getBroadcast(
                ctx, 7, Intent(ctx, KickReceiver::class.java),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
            )
            val at = SystemClock.elapsedRealtime() + PERIOD
            val exact = Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms()
            if (exact) am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi)
            else am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi)
        }
    }
}
