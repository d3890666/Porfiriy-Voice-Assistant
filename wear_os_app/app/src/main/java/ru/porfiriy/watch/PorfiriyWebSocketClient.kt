package ru.porfiriy.watch

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

sealed class WatchWsEvent {
    data class Connected(val url: String, val isBackup: Boolean) : WatchWsEvent()
    data class Ready(
        val userName: String,
        val hasTelegram: Boolean,
        val targetVersion: String,
        val hasUpdate: Boolean,
        val apkAvailable: Boolean,
    ) : WatchWsEvent()
    data class StateChanged(val state: String) : WatchWsEvent()
    data class ToolCall(val name: String, val label: String) : WatchWsEvent()
    data class InputTranscript(val text: String) : WatchWsEvent()
    data class TextDelta(val delta: String) : WatchWsEvent()
    data class TurnComplete(val fullText: String) : WatchWsEvent()
    data class TgSent(val ok: Boolean, val recipient: String, val error: String) : WatchWsEvent()
    data class UpdateAvailable(val version: String, val autoStart: Boolean) : WatchWsEvent()
    data class Error(val message: String) : WatchWsEvent()
    data object Disconnected : WatchWsEvent()
}

data class ProbeResult(
    val ok: Boolean,
    val usedBackup: Boolean = false,
    val latencyMs: Long = 0,
    val userName: String = "",
    val hasUpdate: Boolean = false,
    val targetVersion: String = "",
    val apkAvailable: Boolean = false,
    val error: String = "",
)

data class ApkDownloadResult(
    val ok: Boolean,
    val file: File? = null,
    val version: String = "",
    val error: String = "",
)

class PorfiriyWebSocketClient(
    private val settingsStore: SettingsStore,
    private val scope: CoroutineScope,
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(2500, TimeUnit.MILLISECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .writeTimeout(10000, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    private val _events = MutableSharedFlow<WatchWsEvent>(extraBufferCapacity = 128)
    val events: SharedFlow<WatchWsEvent> = _events.asSharedFlow()

    @Volatile
    private var activeSocket: WebSocket? = null

    @Volatile
    private var isOpen = false

    @Volatile
    private var intentionalClose = false

    @Volatile
    private var connectJob: Job? = null

    private val preRollLock = Any()
    private val preRollQueue = ArrayDeque<ByteArray>()
    private val maxPreRollChunks = 110

    fun isConnected(): Boolean = isOpen && activeSocket != null

    fun connect(settings: WatchSettings) {
        disconnect()
        intentionalClose = false
        synchronized(preRollLock) {
            preRollQueue.clear()
        }
        connectJob = scope.launch(Dispatchers.IO) {
            val primary = settings.primaryUrl.trim()
            val backup = settings.backupUrl.trim()

            if (primary.isEmpty() && backup.isEmpty()) {
                _events.tryEmit(WatchWsEvent.Error("Не задан адрес сервера Порфирия"))
                return@launch
            }

            if (primary.isNotEmpty()) {
                val okPrimary = tryOpenSocket(primary, isBackup = false, settings = settings)
                if (okPrimary) return@launch
            }

            if (backup.isNotEmpty() && backup != primary) {
                val okBackup = tryOpenSocket(backup, isBackup = true, settings = settings)
                if (okBackup) return@launch
            }

            if (!intentionalClose) {
                _events.tryEmit(
                    WatchWsEvent.Error(
                        if (backup.isNotEmpty()) {
                            "Нет связи с основным и резервным сервером"
                        } else {
                            "Сервер недоступен ($primary)"
                        }
                    )
                )
            }
        }
    }

    private suspend fun tryOpenSocket(
        url: String,
        isBackup: Boolean,
        settings: WatchSettings,
    ): Boolean {
        val openDeferred = CompletableDeferred<Boolean>()
        val request = try {
            Request.Builder().url(url).build()
        } catch (_: Exception) {
            return false
        }

        val listener = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (intentionalClose || (activeSocket != null && activeSocket !== webSocket)) {
                    webSocket.close(1000, "Superseded")
                    openDeferred.complete(false)
                    return
                }
                isOpen = true
                val reg = JSONObject().apply {
                    put("type", "register")
                    put("mac", settings.deviceMac)
                    put("name", settings.deviceName)
                    put("device_type", "wear_os")
                    put("firmware", BuildConfig.VERSION_NAME)
                    put("api_key", settings.apiKey)
                }
                webSocket.send(reg.toString())

                synchronized(preRollLock) {
                    while (preRollQueue.isNotEmpty()) {
                        val chunk = preRollQueue.removeFirst()
                        webSocket.send(chunk.toByteString())
                    }
                }

                _events.tryEmit(WatchWsEvent.Connected(url, isBackup))
                openDeferred.complete(true)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleServerJson(text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                // Audio bytes not used on watch
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                val wasOpen = isOpen
                if (activeSocket === webSocket) {
                    isOpen = false
                    activeSocket = null
                }
                if (!openDeferred.isCompleted) {
                    openDeferred.complete(false)
                } else if (wasOpen && !intentionalClose) {
                    _events.tryEmit(WatchWsEvent.Error(t.message ?: "Соединение разорвано"))
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (activeSocket === webSocket) {
                    isOpen = false
                    activeSocket = null
                }
                if (!openDeferred.isCompleted) {
                    openDeferred.complete(false)
                } else if (!intentionalClose) {
                    _events.tryEmit(WatchWsEvent.Disconnected)
                }
            }
        }

        val ws = client.newWebSocket(request, listener)
        activeSocket = ws

        val opened = withTimeoutOrNull(2800L) { openDeferred.await() } ?: false
        if (!opened) {
            isOpen = false
            if (activeSocket === ws) activeSocket = null
            ws.cancel()
        }
        return opened
    }

    private fun handleServerJson(raw: String) {
        try {
            val obj = JSONObject(raw)
            when (obj.optString("type")) {
                "set_config" -> {
                    val cfg = obj.optJSONObject("config") ?: return
                    settingsStore.updateLedColors(
                        idle = cfg.optString("led_color_idle", ""),
                        listen = cfg.optString("led_color_listen", ""),
                        think = cfg.optString("led_color_think", ""),
                        speak = cfg.optString("led_color_speak", ""),
                    )
                }
                "ready" -> {
                    _events.tryEmit(
                        WatchWsEvent.Ready(
                            userName = obj.optString("user_name", ""),
                            hasTelegram = obj.optBoolean("has_telegram", false),
                            targetVersion = obj.optString("target_version", ""),
                            hasUpdate = obj.optBoolean("has_update", false),
                            apkAvailable = obj.optBoolean("apk_available", false),
                        )
                    )
                }
                "state" -> {
                    _events.tryEmit(WatchWsEvent.StateChanged(obj.optString("state", "")))
                }
                "tool_call" -> {
                    _events.tryEmit(
                        WatchWsEvent.ToolCall(
                            name = obj.optString("name", ""),
                            label = obj.optString("label", ""),
                        )
                    )
                }
                "input_transcript" -> {
                    _events.tryEmit(WatchWsEvent.InputTranscript(obj.optString("text", "")))
                }
                "text_delta" -> {
                    _events.tryEmit(WatchWsEvent.TextDelta(obj.optString("text", "")))
                }
                "turn_complete" -> {
                    _events.tryEmit(WatchWsEvent.TurnComplete(obj.optString("text", "")))
                }
                "tg_sent" -> {
                    _events.tryEmit(
                        WatchWsEvent.TgSent(
                            ok = obj.optBoolean("ok", false),
                            recipient = obj.optString("recipient", ""),
                            error = obj.optString("error", ""),
                        )
                    )
                }
                "update_available" -> {
                    _events.tryEmit(
                        WatchWsEvent.UpdateAvailable(
                            version = obj.optString("version", ""),
                            autoStart = obj.optBoolean("auto_start", false),
                        )
                    )
                }
                "error" -> {
                    _events.tryEmit(WatchWsEvent.Error(obj.optString("message", "Ошибка сервера")))
                }
            }
        } catch (_: Exception) {
        }
    }

    fun sendAudioChunk(pcm: ByteArray) {
        val ws = activeSocket
        if (isOpen && ws != null) {
            ws.send(pcm.toByteString())
        } else {
            synchronized(preRollLock) {
                if (preRollQueue.size >= maxPreRollChunks) {
                    preRollQueue.removeFirst()
                }
                preRollQueue.addLast(pcm)
            }
        }
    }

    fun sendEndOfSpeech() {
        val ws = activeSocket ?: return
        if (isOpen) {
            ws.send(JSONObject().put("type", "end_of_speech").toString())
        }
    }

    fun sendCancel() {
        val ws = activeSocket ?: return
        if (isOpen) {
            ws.send(JSONObject().put("type", "cancel").toString())
        }
    }

    fun sendNewTurn() {
        val ws = activeSocket ?: return
        if (isOpen) {
            ws.send(JSONObject().put("type", "new_turn").toString())
        }
    }

    fun sendToTelegram(text: String, settings: WatchSettings) {
        val ws = activeSocket
        if (ws != null && isOpen) {
            ws.send(JSONObject().put("type", "send_to_tg").put("text", text).toString())
            return
        }
        // Reconnect transparently in lightweight probe mode to deliver Telegram message
        scope.launch(Dispatchers.IO) {
            val candidates = buildCandidates(settings)
            var lastErr = "Нет соединения с сервером"
            for ((url, _) in candidates) {
                val done = CompletableDeferred<WatchWsEvent.TgSent?>()
                val req = try {
                    Request.Builder().url(url).build()
                } catch (_: Exception) {
                    continue
                }
                val tmpWs = client.newWebSocket(req, object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        val reg = JSONObject().apply {
                            put("type", "register")
                            put("mac", settings.deviceMac)
                            put("name", settings.deviceName)
                            put("device_type", "wear_os")
                            put("firmware", BuildConfig.VERSION_NAME)
                            put("api_key", settings.apiKey)
                            put("probe", true)
                        }
                        webSocket.send(reg.toString())
                    }

                    override fun onMessage(webSocket: WebSocket, raw: String) {
                        try {
                            val obj = JSONObject(raw)
                            when (obj.optString("type")) {
                                "ready" -> {
                                    webSocket.send(JSONObject().put("type", "send_to_tg").put("text", text).toString())
                                }
                                "tg_sent" -> {
                                    done.complete(
                                        WatchWsEvent.TgSent(
                                            ok = obj.optBoolean("ok", false),
                                            recipient = obj.optString("recipient", ""),
                                            error = obj.optString("error", ""),
                                        )
                                    )
                                    webSocket.close(1000, "TG sent")
                                }
                                "error" -> {
                                    done.complete(WatchWsEvent.TgSent(false, "", obj.optString("message", "Ошибка")))
                                    webSocket.close(1000, "Error")
                                }
                            }
                        } catch (_: Exception) {
                        }
                    }

                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        if (!done.isCompleted) {
                            lastErr = t.message ?: "Нет связи"
                            done.complete(null)
                        }
                    }
                })

                val res = withTimeoutOrNull(8000L) { done.await() }
                if (res != null) {
                    _events.tryEmit(res)
                    return@launch
                } else {
                    tmpWs.cancel()
                }
            }
            _events.tryEmit(WatchWsEvent.TgSent(ok = false, recipient = "", error = lastErr))
        }
    }

    fun disconnect() {
        intentionalClose = true
        connectJob?.cancel()
        connectJob = null
        isOpen = false
        val ws = activeSocket
        activeSocket = null
        ws?.close(1000, "Client closed")
    }

    private fun buildCandidates(settings: WatchSettings): List<Pair<String, Boolean>> = buildList {
        if (settings.primaryUrl.isNotBlank()) add(settings.primaryUrl.trim() to false)
        if (settings.backupUrl.isNotBlank() && settings.backupUrl.trim() != settings.primaryUrl.trim()) {
            add(settings.backupUrl.trim() to true)
        }
    }

    suspend fun probeConnection(settings: WatchSettings): ProbeResult {
        val candidates = buildCandidates(settings)
        if (candidates.isEmpty()) {
            return ProbeResult(ok = false, error = "Не задан URL сервера")
        }

        var lastErr = "Сервер недоступен"
        for ((url, isBackup) in candidates) {
            val t0 = System.currentTimeMillis()
            val done = CompletableDeferred<ProbeResult>()
            val req = try {
                Request.Builder().url(url).build()
            } catch (_: Exception) {
                lastErr = "Некорректный URL: $url"
                continue
            }

            var detectedUser = ""
            var hasUpd = false
            var targetVer = ""
            var apkAvail = false

            val ws = client.newWebSocket(req, object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    val reg = JSONObject().apply {
                        put("type", "register")
                        put("mac", settings.deviceMac)
                        put("name", settings.deviceName)
                        put("device_type", "wear_os")
                        put("firmware", BuildConfig.VERSION_NAME)
                        put("api_key", settings.apiKey)
                        put("probe", true)
                    }
                    webSocket.send(reg.toString())
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    try {
                        val obj = JSONObject(text)
                        when (obj.optString("type")) {
                            "set_config" -> {
                                val cfg = obj.optJSONObject("config")
                                if (cfg != null) {
                                    settingsStore.updateLedColors(
                                        idle = cfg.optString("led_color_idle", ""),
                                        listen = cfg.optString("led_color_listen", ""),
                                        think = cfg.optString("led_color_think", ""),
                                        speak = cfg.optString("led_color_speak", ""),
                                    )
                                }
                            }
                            "ready" -> {
                                detectedUser = obj.optString("user_name", "")
                                hasUpd = obj.optBoolean("has_update", false)
                                targetVer = obj.optString("target_version", "")
                                apkAvail = obj.optBoolean("apk_available", false)
                                webSocket.send(JSONObject().put("type", "ping").toString())
                            }
                            "pong" -> {
                                val dt = System.currentTimeMillis() - t0
                                done.complete(
                                    ProbeResult(
                                        ok = true,
                                        usedBackup = isBackup,
                                        latencyMs = dt,
                                        userName = detectedUser,
                                        hasUpdate = hasUpd,
                                        targetVersion = targetVer,
                                        apkAvailable = apkAvail,
                                    )
                                )
                                webSocket.close(1000, "Probe done")
                            }
                            "error" -> {
                                done.complete(ProbeResult(ok = false, error = obj.optString("message", "Ошибка")))
                                webSocket.close(1000, "Probe error")
                            }
                        }
                    } catch (_: Exception) {
                    }
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    if (!done.isCompleted) {
                        done.complete(ProbeResult(ok = false, error = t.message ?: "Нет ответа"))
                    }
                }
            })

            val res = withTimeoutOrNull(3000L) { done.await() }
            if (res == null) {
                ws.cancel()
                lastErr = "Таймаут ($url)"
            } else if (res.ok) {
                return res
            } else {
                lastErr = res.error
                if (res.error.contains("токен", ignoreCase = true) || res.error.contains("API key", ignoreCase = true)) {
                    return res
                }
            }
        }
        return ProbeResult(ok = false, error = lastErr)
    }

    suspend fun downloadApk(
        settings: WatchSettings,
        outputFile: File,
        onProgress: (percent: Int, version: String) -> Unit,
    ): ApkDownloadResult {
        val candidates = buildCandidates(settings)
        if (candidates.isEmpty()) {
            return ApkDownloadResult(ok = false, error = "Не задан URL сервера")
        }

        var lastErr = "Сервер недоступен"
        for ((url, _) in candidates) {
            val done = CompletableDeferred<ApkDownloadResult>()
            val req = try {
                Request.Builder().url(url).build()
            } catch (_: Exception) {
                continue
            }

            outputFile.parentFile?.mkdirs()
            if (outputFile.exists()) outputFile.delete()

            var fos: FileOutputStream? = null
            val digest = MessageDigest.getInstance("SHA-256")
            var totalSize = 0L
            var received = 0L
            var expectedSha = ""
            var version = ""

            val ws = client.newWebSocket(req, object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    val reg = JSONObject().apply {
                        put("type", "register")
                        put("mac", settings.deviceMac)
                        put("name", settings.deviceName)
                        put("device_type", "wear_os")
                        put("firmware", BuildConfig.VERSION_NAME)
                        put("api_key", settings.apiKey)
                        put("probe", true)
                    }
                    webSocket.send(reg.toString())
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    try {
                        val obj = JSONObject(text)
                        when (obj.optString("type")) {
                            "ready" -> {
                                webSocket.send(JSONObject().put("type", "download_apk").toString())
                            }
                            "apk_start" -> {
                                totalSize = obj.optLong("size", 0L)
                                expectedSha = obj.optString("sha256", "")
                                version = obj.optString("version", "")
                                fos = FileOutputStream(outputFile)
                                onProgress(0, version)
                            }
                            "apk_end" -> {
                                try {
                                    fos?.flush()
                                    fos?.close()
                                } catch (_: Exception) {
                                }
                                fos = null
                                val actualSha = digest.digest().joinToString("") { "%02x".format(it) }
                                if (expectedSha.isNotBlank() && !actualSha.equals(expectedSha, ignoreCase = true)) {
                                    outputFile.delete()
                                    done.complete(ApkDownloadResult(ok = false, error = "Ошибка контрольной суммы APK"))
                                } else {
                                    onProgress(100, version)
                                    done.complete(ApkDownloadResult(ok = true, file = outputFile, version = version))
                                }
                                webSocket.close(1000, "APK downloaded")
                            }
                            "apk_error", "error" -> {
                                try { fos?.close() } catch (_: Exception) {}
                                fos = null
                                done.complete(ApkDownloadResult(ok = false, error = obj.optString("message", "Ошибка загрузки APK")))
                                webSocket.close(1000, "APK error")
                            }
                        }
                    } catch (e: Exception) {
                        try { fos?.close() } catch (_: Exception) {}
                        fos = null
                        if (!done.isCompleted) {
                            done.complete(ApkDownloadResult(ok = false, error = e.message ?: "Ошибка записи"))
                        }
                    }
                }

                override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                    try {
                        val arr = bytes.toByteArray()
                        fos?.write(arr)
                        digest.update(arr)
                        received += arr.size
                        if (totalSize > 0) {
                            val pct = ((received * 100L) / totalSize).toInt().coerceIn(0, 99)
                            onProgress(pct, version)
                        }
                    } catch (e: Exception) {
                        try { fos?.close() } catch (_: Exception) {}
                        fos = null
                        if (!done.isCompleted) {
                            done.complete(ApkDownloadResult(ok = false, error = e.message ?: "Ошибка записи"))
                        }
                        webSocket.close(1000, "Write error")
                    }
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    try { fos?.close() } catch (_: Exception) {}
                    fos = null
                    if (!done.isCompleted) {
                        done.complete(ApkDownloadResult(ok = false, error = t.message ?: "Обрыв загрузки"))
                    }
                }
            })

            val res = withTimeoutOrNull(90000L) { done.await() }
            if (res == null) {
                try { fos?.close() } catch (_: Exception) {}
                ws.cancel()
                lastErr = "Таймаут загрузки APK"
            } else if (res.ok) {
                return res
            } else {
                lastErr = res.error
                if (res.error.contains("APK не найден", ignoreCase = true) || res.error.contains("токен", ignoreCase = true)) {
                    return res
                }
            }
        }
        return ApkDownloadResult(ok = false, error = lastErr)
    }
}
