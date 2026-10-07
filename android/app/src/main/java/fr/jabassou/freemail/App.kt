package fr.jabassou.freemail

import android.app.Application
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        Bridge.init(this)
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
    }
}
