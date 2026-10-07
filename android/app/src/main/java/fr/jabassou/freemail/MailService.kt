package fr.jabassou.freemail

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat

/** Keeps the process (Python server + new-mail watcher) alive in the background. */
class MailService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val type = if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0
        ServiceCompat.startForeground(this, Bridge.STATUS_ID, Bridge.statusNotification(this), type)
        Thread {
            try {
                if (Server.ensure(this) == null) stopSelf()
            } catch (e: Throwable) {
                Log.e("FreeMail", "server start failed", e)
            }
        }.start()
        KickReceiver.schedule(this)
        return START_STICKY
    }

    companion object {
        fun start(ctx: Context) {
            try {
                ContextCompat.startForegroundService(ctx, Intent(ctx, MailService::class.java))
            } catch (e: Exception) {
                Log.w("FreeMail", "cannot start service now", e)
            }
        }
    }
}
