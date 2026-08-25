package com.reachsolutions.parkingprinter

import com.reachsolutions.parkingprinter.BuildConfig

import android.app.Activity
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.widget.Button
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.delay
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

class MainActivity : AppCompatActivity() {

    private lateinit var tvStatus: TextView
    private lateinit var tvLog: TextView
    private lateinit var scrollView: ScrollView
    private val client = OkHttpClient()
    private val gson = com.google.gson.Gson()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        tvStatus = findViewById(R.id.tvStatus)
        tvLog = findViewById(R.id.tvLog)
        scrollView = findViewById(R.id.scrollView)

        findViewById<Button>(R.id.btnRefresh).setOnClickListener { refresh() }

        startPrintService()
        startPolling()
        log("App started")
    }

    private fun startPrintService() {
        val intent = Intent(this, PrintService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
    }

    private fun startPolling() {
        CoroutineScope(Dispatchers.IO).launch {
            while (true) {
                try {
                    refresh()
                } catch (e: Exception) {
                    log("Poll error: $e")
                }
                delay(2000)
            }
        }
    }

    private fun refresh() {
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val request = Request.Builder()
                    .url("http://127.0.0.1:8765/status")
                    .addHeader("X-Print-Secret", BuildConfig.PRINT_HELPER_SECRET)
                    .build()
                val response = client.newCall(request).execute()
                if (response.isSuccessful) {
                    val body = response.body?.string() ?: return@launch
                    val json = JSONObject(body)
                    runOnUiThread {
                        tvStatus.text = formatStatus(json)
                    }
                } else {
                    log("Status HTTP ${response.code}")
                }
            } catch (e: Exception) {
                log("Refresh failed: $e")
            }
        }
    }

    private fun formatStatus(json: JSONObject): String {
        val sb = StringBuilder()
        sb.append("Printer: ${json.optString("printer")} reachable=${json.optBoolean("printer_reachable")}\n")
        sb.append("USB: ${json.optBoolean("usb_connected")} path=${json.optString("active_print_path")}\n")
        sb.append("Processed: ${json.optInt("jobs_processed")} Failed: ${json.optInt("jobs_failed")}\n")
        sb.append("Last via: ${json.optString("last_printed_via")}\n")
        sb.append("Version: ${json.optString("version")}")
        return sb.toString()
    }

    private fun log(msg: String) {
        val timestamp = java.text.SimpleDateFormat("HH:mm:ss").format(java.util.Date())
        runOnUiThread {
            tvLog.append("\n[$timestamp] $msg")
            tvLog.post { tvLog.fullScroll(ScrollView.FOCUS_DOWN) }
        }
        Log.d("ParkingPrinter", msg)
    }
}