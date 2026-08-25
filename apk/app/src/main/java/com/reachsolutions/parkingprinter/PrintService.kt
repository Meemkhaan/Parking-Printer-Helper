package com.reachsolutions.parkingprinter

import com.reachsolutions.parkingprinter.BuildConfig

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Base64
import androidx.core.app.NotificationCompat
import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.io.ByteArrayOutputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.util.Date
import java.net.HttpURLConnection
import java.nio.charset.StandardCharsets
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

class PrintService : Service() {

    private val gson = Gson()
    private var serverThread: Thread? = null
    private val serverSocketRef = AtomicReference<ServerSocket?>()
    private val isRunning = AtomicBoolean(false)
    private val usbManagerRef = AtomicReference<UsbManager?>(null)
    private val usbDeviceRef = AtomicReference<UsbDevice?>(null)
    private val usbConnectionRef = AtomicReference<UsbDeviceConnection?>(null)
    private val usbEndpointRef = AtomicReference<UsbEndpoint?>(null)
    private val secret = BuildConfig.PRINT_HELPER_SECRET
    private val executor = Executors.newSingleThreadExecutor()
    private val scheduler: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor()
    private val logHandler = Handler(Looper.getMainLooper())
    private var jobsProcessed = 0
    private var jobsFailed = 0
    private var lastError: String? = null
    private var lastPrintedVia = "none"
    private var lastPrintedJobId: String? = null
    private val startTime = System.currentTimeMillis()

    companion object {
        const val NOTIFICATION_ID = 1
        const val CHANNEL_ID = "parking_printer_channel"
        const val PORT = 8765
        const val USB_CLASS_PRINTER = UsbConstants.USB_CLASS_PRINTER
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, buildNotification("Parking Printer", "Service started"))
        usbManagerRef.set(getSystemService(Context.USB_SERVICE) as UsbManager)
        startUsbDetector()
        startHttpServer()
        log("Print service created")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return Service.START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        isRunning.set(false)
        serverThread?.interrupt()
        serverSocketRef.getAndSet(null)?.close()
        scheduler.shutdown()
        executor.shutdown()
        usbConnectionRef.get()?.close()
        stopForeground(true)
        log("Print service destroyed")
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Parking Printer",
                NotificationManager.IMPORTANCE_LOW
            )
            channel.description = "Parking Printer background service"
            val nm = getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(title: String, text: String): Notification {
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        }
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun startHttpServer() {
        isRunning.set(true)
        serverThread = Thread {
            try {
                val serverSocket = ServerSocket(PORT)
                serverSocketRef.set(serverSocket)
                log("HTTP server listening on port $PORT")
                while (isRunning.get()) {
                    try {
                        val clientSocket = serverSocket.accept()
                        executor.execute { handleClient(clientSocket) }
                    } catch (e: Exception) {
                        if (isRunning.get()) log("Accept error: $e")
                    }
                }
            } catch (e: Exception) {
                log("Server socket error: $e")
            }
        }.also { it.start() }
    }

    private fun handleClient(socket: Socket) {
        socket.use { s ->
            val input = s.getInputStream()
            val output = s.getOutputStream()
            val buffer = ByteArray(8192)
            val len = input.read(buffer)
            if (len <= 0) return
            val request = String(buffer, 0, len, StandardCharsets.UTF_8)
            val (status, response) = parseAndHandle(request)
            val responseStr = "HTTP/1.1 $status\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type, X-Print-Secret\r\nContent-Length: ${response.length}\r\n\r\n$response"
            output.write(responseStr.toByteArray(StandardCharsets.UTF_8))
            output.flush()
        }
    }

    private fun parseAndHandle(request: String): Pair<Int, String> {
        val lines = request.lines()
        if (lines.isEmpty()) return 400 to json("detail", "Empty request")
        val firstLine = lines.first()
        val parts = firstLine.split(" ")
        if (parts.size < 3) return 400 to json("detail", "Bad request")
        val method = parts[0]
        val path = parts[1]

        val headers = mutableMapOf<String, String>()
        var i = 1
        while (i < lines.size && lines[i].isNotBlank()) {
            val h = lines[i].split(": ", limit = 2)
            if (h.size == 2) headers[h[0].lowercase()] = h[1]
            i++
        }
        val body = if (i + 1 < lines.size) lines.drop(i + 1).joinToString("\n") else ""

        return when {
            method == "OPTIONS" -> 204 to ""
            method == "GET" && path == "/health" -> 200 to json("status", "online")
            method == "GET" && path == "/status" -> handleStatus(headers)
            method == "POST" && path == "/print-ticket" -> handlePrintTicket(headers, body)
            method == "POST" && path == "/print" -> handleTestPrint(headers, body)
            else -> 404 to json("detail", "Not found")
        }
    }

    private fun handleStatus(headers: Map<String, String>): Pair<Int, String> {
        if (!checkSecret(headers["x-print-secret"])) {
            return 401 to json("detail", "Invalid or missing X-Print-Secret")
        }
        val reachable = checkPrinterReachable()
        val usbOk = usbDeviceRef.get() != null
        return 200 to JsonObject().apply {
            addProperty("status", "online")
            addProperty("version", "1.0.0-native")
            addProperty("uptime_seconds", (System.currentTimeMillis() - startTime) / 1000)
            addProperty("printer", BuildConfig.PRINTER_IP)
            addProperty("printer_port", BuildConfig.PRINTER_PORT)
            addProperty("printer_reachable", reachable)
            addProperty("usb_connected", usbOk)
            addProperty("active_print_path", lastPrintedVia)
            addProperty("last_printed_job_id", lastPrintedJobId)
            addProperty("last_printed_via", lastPrintedVia)
            addProperty("last_error", lastError)
            addProperty("jobs_processed", jobsProcessed)
            addProperty("jobs_failed", jobsFailed)
        }.toString()
    }

    private fun handlePrintTicket(headers: Map<String, String>, body: String): Pair<Int, String> {
        if (!checkSecret(headers["x-print-secret"])) {
            return 401 to json("detail", "Invalid or missing X-Print-Secret")
        }
        val payload = JsonParser.parseString(body).asJsonObject
        val b64 = payload.get("payload_base64")?.asString ?: return 400 to json("detail", "Missing payload_base64")
        val ticketNumber = payload.get("ticket_number")?.asString
        val data = Base64.decode(b64, Base64.DEFAULT)
        if (data.isEmpty()) return 400 to json("detail", "Decoded to zero bytes")
        try {
            val path = sendToPrinterAuto(data)
            jobsProcessed++
            lastError = null
            lastPrintedVia = path
            lastPrintedJobId = ticketNumber ?: lastPrintedJobId
            return 200 to json("success", true, "path", path)
        } catch (e: Exception) {
            jobsFailed++
            lastError = e.message ?: "Print failed"
            return 503 to json("detail", lastError)
        }
    }

    private fun handleTestPrint(headers: Map<String, String>, body: String): Pair<Int, String> {
        if (!checkSecret(headers["x-print-secret"])) {
            return 401 to json("detail", "Invalid or missing X-Print-Secret")
        }
        val payload = JsonParser.parseString(body).asJsonObject
        try {
            val receipt = buildTestReceipt(payload)
            sendToPrinterLan(receipt)
            return 200 to json("success", true, "message", "Receipt sent to printer")
        } catch (e: Exception) {
            return 500 to json("detail", e.message ?: "Print failed")
        }
    }

    private fun checkSecret(header: String?): Boolean {
        val expected = BuildConfig.PRINT_HELPER_SECRET
        return expected.isNotBlank() && java.security.MessageDigest.isEqual(
            header?.toByteArray(StandardCharsets.UTF_8) ?: ByteArray(0),
            expected.toByteArray(StandardCharsets.UTF_8)
        )
    }

    private fun json(vararg pairs: Any): String {
        val obj = JsonObject()
        for (i in pairs.indices.step(2)) {
            val k = pairs[i] as String
            val v = pairs[i + 1]
            when (v) {
                is String -> obj.addProperty(k, v)
                is Boolean -> obj.addProperty(k, v)
                is Int -> obj.addProperty(k, v)
                is Long -> obj.addProperty(k, v)
                is Double -> obj.addProperty(k, v)
                else -> obj.addProperty(k, v.toString())
            }
        }
        return Gson().toJson(obj)
    }

    private fun checkPrinterReachable(): Boolean {
        return try {
            Socket().use { it.connect(java.net.InetSocketAddress(BuildConfig.PRINTER_IP, BuildConfig.PRINTER_PORT), BuildConfig.PRINTER_TIMEOUT); true }
            catch (e: Exception) { false }
        }
    }

    private fun sendToPrinterAuto(data: ByteArray): String {
        if (tryUsbPrint(data)) return "usb"
        sendToPrinterLan(data)
        return "lan"
    }

    private fun tryUsbPrint(data: ByteArray): Boolean {
        val device = findUsbPrinter()
        if (device == null) return false
        val manager = usbManagerRef.get() ?: return false
        if (!manager.hasPermission(device)) {
            requestUsbPermission(device)
            return false
        }
        return try {
            openUsbConnection(device).use { conn ->
                val (iface, ep) = findBulkOut(device) ?: return@use false
                conn.claimInterface(iface, true)
                conn.bulkTransfer(ep, data, data.size, 5000) >= 0
            }
        } catch (e: Exception) {
            false
        }
    }

    private fun findUsbPrinter(): UsbDevice? {
        val manager = usbManagerRef.get() ?: return null
        manager.deviceList.values.firstOrNull { it.interfaceCount > 0 && (0 until it.interfaceCount).any { it.getInterface(it).interfaceClass == USB_CLASS_PRINTER } }
    }

    private fun openUsbConnection(device: UsbDevice): UsbDeviceConnection? {
        val manager = usbManagerRef.get() ?: return null
        return manager.openDevice(device)
    }

    private fun findBulkOut(device: UsbDevice): Pair<UsbInterface, UsbEndpoint>? {
        for (i in 0 until device.interfaceCount) {
            val iface = device.getInterface(i)
            if (iface.interfaceClass == USB_CLASS_PRINTER) {
                for (j in 0 until iface.endpointCount) {
                    val ep = iface.getEndpoint(j)
                    if (ep.type == UsbConstants.USB_ENDPOINT_XFER_BULK && ep.direction == UsbConstants.USB_DIR_OUT) {
                        return iface to ep
                    }
                }
            }
        }
        return null
    }

    private fun requestUsbPermission(device: UsbDevice) {
        val manager = usbManagerRef.get() ?: return
        val intent = Intent("com.reachsolutions.parkingprinter.USB_PERMISSION")
        val pendingIntent = PendingIntent.getBroadcast(this, 0, intent, PendingIntent.FLAG_IMMUTABLE)
        manager.requestPermission(device, intent)
    }

    private fun sendToPrinterLan(data: ByteArray) {
        Socket().use { socket ->
            socket.connect(java.net.InetSocketAddress(BuildConfig.PRINTER_IP, BuildConfig.PRINTER_PORT), BuildConfig.PRINTER_TIMEOUT)
            socket.getOutputStream().apply {
                write(data)
                flush()
            }
            socket.shutdownOutput()
            Thread.sleep(200)
        }
    }

    private fun buildTestReceipt(payload: JsonObject): ByteArray {
        val ESC = byteArrayOf(0x1B)
        val GS = byteArrayOf(0x1D)
        val ticket = payload.get("ticket")?.asString ?: ""
        val vehicle = payload.get("vehicle")?.asString ?: ""
        val number = payload.get("number")?.asString ?: ""
        val amount = payload.get("amount")?.asDouble ?: 0.0

        val data = ByteArrayOutputStream()
        data.write(0x1B); data.write(0x40) // ESC @
        data.write(0x1B); data.write(0x61); data.write(0x01) // ESC a 1
        data.write("PARKING RECEIPT\n".toByteArray())
        data.write("--------------------------------\n".toByteArray())
        data.write(0x1B); data.write(0x61); data.write(0x00)
        data.write("Ticket : $ticket\n".toByteArray())
        data.write("Vehicle: $vehicle\n".toByteArray())
        data.write("Number : $number\n".toByteArray())
        data.write("Amount : Rs. ${"%.2f".format(amount)}\n".toByteArray())
        data.write("--------------------------------\n".toByteArray())
        data.write(0x1B); data.write(0x61); data.write(0x01)
        data.write("Thank You\n".toByteArray())
        data.write("\n\n\n".toByteArray())
        data.write(0x1D); data.write(0x56); data.write(0x00) // GS V 0
        return data.toByteArray()
    }

    private fun startUsbDetector() {
        scheduler.scheduleAtFixedRate({
            val device = findUsbPrinter()
            val prev = usbDeviceRef.getAndSet(device)
            if (prev != device) {
                log("USB printer ${if (device != null) "attached" else "detached"}: ${device?.deviceName ?: prev?.deviceName}")
            }
        }, 2, 2, TimeUnit.SECONDS)
    }

    private fun log(msg: String) {
        val timestamp = java.text.SimpleDateFormat("HH:mm:ss").format(Date())
        val line = "[$timestamp] $msg"
        logHandler.post {
            // MainActivity will poll /status, but we could also broadcast
            // For now just keep in memory; MainActivity polls /status
        }
    }
}