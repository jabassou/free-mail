package fr.jabassou.freemail

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.RemoteInput
import androidx.core.content.ContextCompat

/** Kotlin side called from Python (Chaquopy): Android notifications for new mail. */
object Bridge {
    const val CH_MAIL = "mail"
    const val CH_QUIET = "mail_quiet"
    const val CH_SERVICE = "status"
    const val STATUS_ID = 42
    const val ACCENT = 0xFF7C3AED.toInt()

    lateinit var app: Context
        private set

    fun init(ctx: Context) {
        app = ctx.applicationContext
        val nm = app.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CH_MAIL, app.getString(R.string.ch_mail), NotificationManager.IMPORTANCE_HIGH).apply {
                description = app.getString(R.string.ch_mail_desc)
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 180, 90, 180)
                enableLights(true)
                lightColor = ACCENT
            }
        )
        nm.createNotificationChannel(
            NotificationChannel(CH_QUIET, app.getString(R.string.ch_quiet), NotificationManager.IMPORTANCE_LOW).apply {
                description = app.getString(R.string.ch_quiet_desc)
                enableVibration(false)
                setSound(null, null)
            }
        )
        nm.deleteNotificationChannel("service") // v1 channel (hidden from the status bar)
        nm.createNotificationChannel(
            NotificationChannel(CH_SERVICE, app.getString(R.string.ch_status), NotificationManager.IMPORTANCE_LOW).apply {
                description = app.getString(R.string.ch_status_desc)
                setShowBadge(false)
            }
        )
    }

    @Volatile var lastCheck: String = ""
        private set
    @Volatile private var unread = -1
    @Volatile private var detail = ""

    /** Persistent status-bar notification: unread count + last check (also the foreground-service notification). */
    fun statusNotification(ctx: Context): android.app.Notification {
        val open = PendingIntent.getActivity(ctx, 0, Intent(ctx, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val title = when {
            unread < 0 -> ctx.getString(R.string.status_watching)
            unread == 0 -> ctx.getString(R.string.status_none)
            else -> ctx.resources.getQuantityString(R.plurals.status_unread, unread, unread)
        }
        val text = listOf(detail, if (lastCheck.isNotEmpty()) ctx.getString(R.string.status_checked, lastCheck) else "").filter { it.isNotEmpty() }.joinToString(" · ")
        return NotificationCompat.Builder(ctx, CH_SERVICE)
            .setSmallIcon(R.drawable.ic_stat_mail)
            .setColor(ACCENT)
            .setContentTitle(title)
            .setContentText(text.ifEmpty { ctx.getString(R.string.tap_to_open) })
            .setNumber(maxOf(unread, 0))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(open)
            .build()
    }

    /** Called by the Python notifier after every check. */
    @JvmStatic
    @SuppressLint("MissingPermission")
    fun updateStatus(count: Int, folders: String, last: String) {
        unread = count
        detail = folders
        lastCheck = last
        if (canNotify()) NotificationManagerCompat.from(app).notify(STATUS_ID, statusNotification(app))
    }

    /** The mail was read / moved / deleted in the app: remove its notification. */
    @JvmStatic
    fun cancelMail(id: Int) {
        NotificationManagerCompat.from(app).cancel(id)
    }

    /** Keeps the CPU awake while Python handles an IMAP push (IDLE) that woke the phone. */
    @JvmStatic
    fun holdWake(ms: Int) {
        app.getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "freemail:push")
            .apply { setReferenceCounted(false) }
            .acquire(ms.toLong())
    }

    fun canNotify() = Build.VERSION.SDK_INT < 33 ||
        ContextCompat.checkSelfPermission(app, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    /** Called by webui.Notifier (Python thread). Empty uid = summary notification.
     *  silent = quiet hours: posted on a low-importance channel (no sound, no vibration, no heads-up). */
    @JvmStatic
    @SuppressLint("MissingPermission")
    fun notifyMail(id: Int, title: String, body: String, folder: String, uid: String, silent: Boolean) {
        if (!canNotify()) return
        val open = Intent(app, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
            if (folder.isNotEmpty()) putExtra("folder", folder)
            if (uid.isNotEmpty()) putExtra("uid", uid)
        }
        val pi = PendingIntent.getActivity(app, id, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val b = NotificationCompat.Builder(app, if (silent) CH_QUIET else CH_MAIL)
            .setSmallIcon(R.drawable.ic_stat_mail)
            .setColor(ACCENT)
            .setContentTitle(title)
            .setContentText(body.lineSequence().firstOrNull() ?: "")
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setContentIntent(pi)
            .setAutoCancel(true)
            .setGroup("freemail")
            .setCategory(NotificationCompat.CATEGORY_EMAIL)
            .setPriority(if (silent) NotificationCompat.PRIORITY_LOW else NotificationCompat.PRIORITY_HIGH)
            .setSilent(silent)
        if (uid.isNotEmpty()) {
            val read = Intent(app, ActionReceiver::class.java).setAction(ActionReceiver.READ)
                .putExtra("folder", folder).putExtra("uid", uid).putExtra("nid", id)
            b.addAction(0, app.getString(R.string.action_mark_read), PendingIntent.getBroadcast(app, id, read, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
            // inline reply: the RemoteInput text is filled in by the system, so this PendingIntent must be mutable
            // (explicit component: no other app can redirect it)
            val reply = Intent(app, ActionReceiver::class.java).setAction(ActionReceiver.REPLY)
                .putExtra("folder", folder).putExtra("uid", uid).putExtra("nid", id)
            val replyPi = PendingIntent.getBroadcast(app, id + 1, reply, PendingIntent.FLAG_MUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            val input = RemoteInput.Builder(ActionReceiver.KEY_TEXT).setLabel(app.getString(R.string.reply_hint)).build()
            b.addAction(
                NotificationCompat.Action.Builder(0, app.getString(R.string.action_reply), replyPi)
                    .addRemoteInput(input)
                    .setAllowGeneratedReplies(false)
                    .setSemanticAction(NotificationCompat.Action.SEMANTIC_ACTION_REPLY)
                    .setShowsUserInterface(false)
                    .build()
            )
        }
        NotificationManagerCompat.from(app).notify(id, b.build())
    }

    /** Replaces a notification by a short confirmation after an inline reply (stops the reply spinner). */
    @SuppressLint("MissingPermission")
    fun replied(id: Int, ok: Boolean, error: String = "") {
        if (!canNotify()) return
        val n = NotificationCompat.Builder(app, CH_QUIET)
            .setSmallIcon(R.drawable.ic_stat_mail)
            .setColor(ACCENT)
            .setContentTitle(app.getString(if (ok) R.string.reply_sent else R.string.reply_failed))
            .setContentText(error)
            .setTimeoutAfter(if (ok) 4000L else 20000L)
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(app).notify(id, n)
    }
}
