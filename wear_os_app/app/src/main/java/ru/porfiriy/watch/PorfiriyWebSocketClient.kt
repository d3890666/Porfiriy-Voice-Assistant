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
import java.util.concurrent.TimeUnit

sealed class WatchWsEvent {
    data class Connected(val url: String, val isBackup: Boolean) : WatchWsEvent()
    data class Ready(val userName: String, val hasTelegram: Boolean) : WatchWsEvent()
    data class StateChanged(val state: String) : WatchWsEvent()
    data class ToolCall(val name: String, val label: String) : WatchWsEvent()
    data class InputTranscript(val text: String) : WatchWsEvent()
    data class TextDelta(val delta: String) : WatchWsEvent()
    data class TurnComplete(val fullText: String) : WatchWsEvent()
    data class TgSent(val ok: Boolean, val recipient: String, val error: String) : WatchWsEvent()
    data class Error(val message: String) : WatchWsEvent()
    data object Disconnected : WatchWsEvent()
}

data class ProbeResult(
    val ok: Boolean,
    val usedBackup: Boolean = false,
    val latencyMs: Long = 0,
    val userName: String = "",
    val error: String = "",
)

class PorfiriyWebSocketClient(
    private val settingsStore: SettingsStore,
    private val scope: CoroutineScope,
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(2500, TimeUnit.MILLISECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .writeTimeout(5000, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    private val _events = MutableSharedFlow<WatchWsEvent>(extraBufferCapacity = 128)
    val events: SharedFlow<WatchWsEvent> = _events.asSharedFlow()

    @Volatile
    private var activeSocket: WebSocket? = null

    @Volatile
    private var isOpen = false

    @Volatile
    private var connectJob: Job? = null

    private val preRollLock = Any()
    private val preRollQueue = ArrayDeque<ByteArray>()
    private val maxPreRollChunks = 110 // ~3.5s @ 32ms/chunk

    fun connect(settings: WatchSettings) {
        disconnect()
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

            _events.tryEmit(
                WatchWsEvent.Error(
                    if (backup.isNotEmpty()) {
                        "Нет связи с основным и резервным сервером"
                    } else {
                        "Сервер Порфирия недоступен ($primary)"
                    }
                )
            )
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
        } catch (e: Exception) {
            return false
        }

        val listener = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (activeSocket != null && activeSocket !== webSocket) {
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
                    put("firmware", "1.0.0")
                    put("api_key", settings.apiKey)
                }
                webSocket.send(reg.toString())

                // Flush pre-roll microphone audio captured while connecting
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
                // Watch discards audio bytes; server already strips them for wear_os
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                val wasOpen = isOpen
                if (activeSocket === webSocket) {
                    isOpen = false
                    activeSocket = null
                }
                if (!openDeferred.isCompleted) {
                    openDeferred.complete(false)
                } else if (wasOpen) {
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
                } else {
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

    fun sendNewTurn() {
        val ws = activeSocket ?: return
        if (isOpen) {
            ws.send(JSONObject().put("type", "new_turn").toString())
        }
    }

    fun sendToTelegram(text: String) {
        val ws = activeSocket
        if (ws != null && isOpen) {
            ws.send(JSONObject().put("type", "send_to_tg").put("text", text).toString())
        } else {
            _events.tryEmit(WatchWsEvent.TgSent(ok = false, recipient = "", error = "Нет соединения с сервером"))
        }
    }

    fun disconnect() {
        connectJob?.cancel()
        connectJob = null
        isOpen = false
        val ws = activeSocket
        activeSocket = null
        ws?.close(1000, "Client closed")
    }

    suspend fun probeConnection(settings: WatchSettings): ProbeResult {
        val candidates = buildList {
            if (settings.primaryUrl.isNotBlank()) add(settings.primaryUrl.trim() to false)
            if (settings.backupUrl.isNotBlank() && settings.backupUrl.trim() != settings.primaryUrl.trim()) {
                add(settings.backupUrl.trim() to true)
            }
        }
        if (candidates.isEmpty()) {
            return ProbeResult(ok = false, error = "Не задан URL сервера")
        }

        var lastErr = "Сервер недоступен"
        for ((url, isBackup) in candidates) {
            val t0 = System.currentTimeMillis()
            val done = CompletableDeferred<ProbeResult>()
            val req = try {
                Request.Builder().url(url).build()
            } catch (e: Exception) {
                lastErr = "Некорректный URL: $url"
                continue
            }

            var detectedUser = ""
            val ws = client.newWebSocket(req, object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    val reg = JSONObject().apply {
                        put("type", "register")
                        put("mac", settings.deviceMac)
                        put("name", settings.deviceName)
                        put("device_type", "wear_os")
                        put("firmware", "1.0.0")
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
                                webSocket.send(JSONObject().put("type", "ping").toString())
                            }
                            "pong" -> {
                                val dt = System.currentTimeMillis() - t0
                                done.complete(ProbeResult(ok = true, usedBackup = isBackup, latencyMs = dt, userName = detectedUser))
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
}
