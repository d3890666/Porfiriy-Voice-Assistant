package ru.porfiriy.watch

import android.content.Context
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.io.File
import java.util.concurrent.atomic.AtomicInteger

enum class WatchUiPhase {
    PERMISSION_NEEDED,
    IDLE,
    CONNECTING,
    LISTENING,
    THINKING,
    RESPONDING,
    DONE,
    UPDATING,
    ERROR,
}

enum class TgSendStatus {
    IDLE,
    SENDING,
    SENT,
    FAILED,
}

data class WatchUiState(
    val phase: WatchUiPhase = WatchUiPhase.CONNECTING,
    val rms: Float = 0f,
    val statusText: String = "Подключение…",
    val routeBadge: String = "",
    val boundUserName: String = "",
    val hasTelegram: Boolean = false,
    val inputTranscript: String = "",
    val replyText: String = "",
    val tgStatus: TgSendStatus = TgSendStatus.IDLE,
    val tgMessage: String = "",
    val errorMessage: String = "",
    val probeStatus: String = "",
    val isProbing: Boolean = false,
    val hasUpdate: Boolean = false,
    val targetVersion: String = "",
    val apkAvailable: Boolean = false,
    val updateProgress: Int = 0,
    val updateStatus: String = "",
)

class PorfiriyController(
    context: Context,
    private val onInstallApkReady: (File) -> Unit = {},
) {
    private val appContext = context.applicationContext
    val settingsStore = SettingsStore(appContext)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val mic = MicRecorder()
    private val wsClient = PorfiriyWebSocketClient(settingsStore, scope)

    private val vibrator: Vibrator? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        (appContext.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)?.defaultVibrator
    } else {
        @Suppress("DEPRECATION")
        appContext.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
    }

    private val _uiState = MutableStateFlow(WatchUiState())
    val uiState: StateFlow<WatchUiState> = _uiState.asStateFlow()

    private var hasAudioPermission = false
    private var wasBackgrounded = false
    private val speechChunksCount = AtomicInteger(0)

    init {
        scope.launch {
            wsClient.events.collect { ev -> handleWsEvent(ev) }
        }
    }

    fun onPermissionUpdated(granted: Boolean, autoStart: Boolean = true) {
        hasAudioPermission = granted
        if (!granted) {
            _uiState.update { it.copy(phase = WatchUiPhase.PERMISSION_NEEDED, statusText = "Нужен микрофон") }
        } else if (autoStart) {
            startFreshSession()
        }
    }

    fun startFreshSession() {
        if (!hasAudioPermission) {
            _uiState.update { it.copy(phase = WatchUiPhase.PERMISSION_NEEDED, statusText = "Нужен микрофон") }
            return
        }
        wasBackgrounded = false
        speechChunksCount.set(0)
        val settings = settingsStore.settings.value
        _uiState.update {
            it.copy(
                phase = WatchUiPhase.CONNECTING,
                rms = 0f,
                statusText = "Подключение…",
                routeBadge = "",
                inputTranscript = "",
                replyText = "",
                tgStatus = TgSendStatus.IDLE,
                tgMessage = "",
                errorMessage = "",
            )
        }
        startMicCapture()
        wsClient.connect(settings)
        hapticTick()
    }

    fun finishSpeaking() {
        val phase = _uiState.value.phase
        if (phase != WatchUiPhase.LISTENING && phase != WatchUiPhase.CONNECTING) return
        mic.stop()
        // Protect against sending pure silence (prevents infinite "thinking")
        if (speechChunksCount.get() < 2) {
            wsClient.sendCancel()
            _uiState.update {
                it.copy(
                    phase = WatchUiPhase.IDLE,
                    rms = 0f,
                    statusText = "Ничего не услышал",
                )
            }
            hapticTick()
            return
        }
        wsClient.sendEndOfSpeech()
        _uiState.update { it.copy(phase = WatchUiPhase.THINKING, rms = 0f, statusText = "Думаю…") }
        hapticTick()
    }

    fun cancelActiveTurn() {
        mic.stop()
        wsClient.sendCancel()
        speechChunksCount.set(0)
        _uiState.update {
            it.copy(
                phase = WatchUiPhase.IDLE,
                rms = 0f,
                statusText = "Отменено",
                errorMessage = "",
            )
        }
        hapticTick()
    }

    fun continueDialogue() {
        if (!hasAudioPermission) return
        if (!wsClient.isConnected()) {
            startFreshSession()
            return
        }
        speechChunksCount.set(0)
        _uiState.update {
            it.copy(
                phase = WatchUiPhase.LISTENING,
                rms = 0f,
                statusText = "Слушаю…",
                inputTranscript = "",
                replyText = "",
                tgStatus = TgSendStatus.IDLE,
                tgMessage = "",
                errorMessage = "",
            )
        }
        startMicCapture()
        wsClient.sendNewTurn()
        hapticTick()
    }

    fun sendCurrentToTelegram() {
        val text = _uiState.value.replyText.trim()
        if (text.isEmpty() || _uiState.value.tgStatus == TgSendStatus.SENDING) return
        _uiState.update { it.copy(tgStatus = TgSendStatus.SENDING, tgMessage = "Отправляю…") }
        hapticTick()
        wsClient.sendToTelegram(text, settingsStore.settings.value)
    }

    fun runConnectionProbe() {
        if (_uiState.value.isProbing) return
        _uiState.update { it.copy(isProbing = true, probeStatus = "Проверка…") }
        scope.launch {
            val res = wsClient.probeConnection(settingsStore.settings.value)
            val msg = if (res.ok) {
                val route = if (res.usedBackup) "Резерв" else "Основной"
                val who = if (res.userName.isNotBlank()) " • ${res.userName}" else ""
                "✓ $route (${res.latencyMs} мс)$who"
            } else {
                "✕ ${res.error}"
            }
            _uiState.update {
                it.copy(
                    isProbing = false,
                    probeStatus = msg,
                    hasUpdate = if (res.ok) res.hasUpdate else it.hasUpdate,
                    targetVersion = if (res.ok && res.targetVersion.isNotBlank()) res.targetVersion else it.targetVersion,
                    apkAvailable = if (res.ok) res.apkAvailable else it.apkAvailable,
                )
            }
        }
    }

    fun startAppUpdate() {
        if (_uiState.value.phase == WatchUiPhase.UPDATING) return
        mic.stop()
        wsClient.disconnect()
        _uiState.update {
            it.copy(
                phase = WatchUiPhase.UPDATING,
                rms = 0f,
                updateProgress = 0,
                updateStatus = "Скачивание APK…",
                errorMessage = "",
            )
        }
        scope.launch {
            val targetFile = File(File(appContext.cacheDir, "updates"), "porfiriy-watch.apk")
            val res = wsClient.downloadApk(
                settings = settingsStore.settings.value,
                outputFile = targetFile,
                onProgress = { pct, ver ->
                    _uiState.update {
                        it.copy(
                            updateProgress = pct,
                            targetVersion = ver.ifBlank { it.targetVersion },
                            updateStatus = if (ver.isNotBlank()) "Обновление v$ver ($pct%)" else "Загрузка ($pct%)",
                        )
                    }
                },
            )
            if (res.ok && res.file != null) {
                _uiState.update {
                    it.copy(
                        phase = WatchUiPhase.IDLE,
                        updateProgress = 100,
                        updateStatus = "Открываю установщик…",
                        statusText = "Установка v${res.version}",
                    )
                }
                hapticDouble()
                onInstallApkReady(res.file)
            } else {
                _uiState.update {
                    it.copy(
                        phase = WatchUiPhase.ERROR,
                        errorMessage = res.error.ifBlank { "Не удалось скачать APK" },
                        updateStatus = res.error,
                    )
                }
            }
        }
    }

    fun onBackgrounded() {
        wasBackgrounded = true
        mic.stop()
        if (_uiState.value.phase != WatchUiPhase.UPDATING) {
            wsClient.disconnect()
        }
    }

    fun onForegrounded(isInSettings: Boolean) {
        if (!wasBackgrounded) return
        wasBackgrounded = false
        if (isInSettings || !hasAudioPermission) return
        val cur = _uiState.value.phase
        if (cur == WatchUiPhase.DONE || cur == WatchUiPhase.UPDATING) {
            // Keep finished response visible so user can read it or tap "В ТГ"
            return
        }
        startFreshSession()
    }

    fun closeSession() {
        mic.stop()
        wsClient.disconnect()
    }

    fun destroy() {
        closeSession()
        scope.cancel()
    }

    private fun startMicCapture() {
        speechChunksCount.set(0)
        mic.start(
            scope = scope,
            onChunk = { pcm, rms ->
                if (rms > 0.04f) {
                    speechChunksCount.incrementAndGet()
                }
                wsClient.sendAudioChunk(pcm)
                _uiState.update { st ->
                    if (st.phase == WatchUiPhase.LISTENING || st.phase == WatchUiPhase.CONNECTING) {
                        st.copy(rms = rms)
                    } else st
                }
            },
            onError = { err ->
                _uiState.update { it.copy(phase = WatchUiPhase.ERROR, errorMessage = err, statusText = "Ошибка") }
            },
        )
    }

    private fun handleWsEvent(ev: WatchWsEvent) {
        when (ev) {
            is WatchWsEvent.Connected -> {
                val badge = if (ev.isBackup) "Резерв" else "Локально"
                _uiState.update {
                    it.copy(
                        phase = if (mic.isRecording) WatchUiPhase.LISTENING else it.phase,
                        routeBadge = badge,
                        statusText = if (mic.isRecording) "Слушаю…" else it.statusText,
                    )
                }
            }
            is WatchWsEvent.Ready -> {
                _uiState.update {
                    it.copy(
                        boundUserName = ev.userName,
                        hasTelegram = ev.hasTelegram,
                        targetVersion = ev.targetVersion.ifBlank { it.targetVersion },
                        hasUpdate = ev.hasUpdate,
                        apkAvailable = ev.apkAvailable,
                    )
                }
            }
            is WatchWsEvent.StateChanged -> {
                when (ev.state) {
                    "listening" -> _uiState.update {
                        it.copy(phase = WatchUiPhase.LISTENING, statusText = "Слушаю…")
                    }
                    "thinking" -> {
                        mic.stop()
                        _uiState.update {
                            it.copy(phase = WatchUiPhase.THINKING, rms = 0f, statusText = "Думаю…")
                        }
                    }
                    "responding" -> {
                        mic.stop()
                        _uiState.update {
                            it.copy(phase = WatchUiPhase.RESPONDING, rms = 0f, statusText = "Отвечаю…")
                        }
                    }
                }
            }
            is WatchWsEvent.ToolCall -> {
                val label = when {
                    ev.label.isNotBlank() -> ev.label
                    ev.name == "call_ha_service" || ev.name == "get_ha_state" -> "Умный дом…"
                    ev.name == "search_music_assistant" || ev.name == "play_music_assistant" -> "Музыка…"
                    ev.name == "web_search" -> "Ищу в сети…"
                    ev.name == "send_to_telegram" -> "Пишу в TG…"
                    else -> "Выполняю…"
                }
                _uiState.update { it.copy(phase = WatchUiPhase.THINKING, statusText = label) }
            }
            is WatchWsEvent.InputTranscript -> {
                _uiState.update { it.copy(inputTranscript = ev.text) }
            }
            is WatchWsEvent.TextDelta -> {
                mic.stop()
                _uiState.update { st ->
                    st.copy(
                        phase = WatchUiPhase.RESPONDING,
                        statusText = "Отвечаю…",
                        replyText = st.replyText + ev.delta,
                    )
                }
            }
            is WatchWsEvent.TurnComplete -> {
                mic.stop()
                _uiState.update { st ->
                    val finalTxt = ev.fullText.ifBlank { st.replyText }.ifBlank { "Готово." }
                    st.copy(
                        phase = WatchUiPhase.DONE,
                        rms = 0f,
                        statusText = "Готово",
                        replyText = finalTxt,
                    )
                }
                hapticDouble()
            }
            is WatchWsEvent.TgSent -> {
                if (ev.ok) {
                    val label = if (ev.recipient.isNotBlank()) "В ТГ (${ev.recipient})" else "Отправлено"
                    _uiState.update { it.copy(tgStatus = TgSendStatus.SENT, tgMessage = label) }
                    hapticDouble()
                } else {
                    _uiState.update {
                        it.copy(
                            tgStatus = TgSendStatus.FAILED,
                            tgMessage = ev.error.ifBlank { "Ошибка ТГ" },
                        )
                    }
                    hapticTick()
                }
            }
            is WatchWsEvent.UpdateAvailable -> {
                _uiState.update {
                    it.copy(hasUpdate = true, targetVersion = ev.version.ifBlank { it.targetVersion })
                }
                if (ev.autoStart) {
                    startAppUpdate()
                }
            }
            is WatchWsEvent.Error -> {
                mic.stop()
                _uiState.update {
                    it.copy(
                        phase = WatchUiPhase.ERROR,
                        rms = 0f,
                        errorMessage = ev.message,
                        statusText = "Ошибка",
                    )
                }
            }
            WatchWsEvent.Disconnected -> {
                val cur = _uiState.value.phase
                if (cur == WatchUiPhase.CONNECTING || cur == WatchUiPhase.LISTENING || cur == WatchUiPhase.THINKING) {
                    mic.stop()
                    _uiState.update {
                        it.copy(phase = WatchUiPhase.IDLE, rms = 0f, statusText = "Нажмите 🎤")
                    }
                }
            }
        }
    }

    private fun hapticTick() {
        try {
            vibrator?.vibrate(VibrationEffect.createOneShot(18L, VibrationEffect.DEFAULT_AMPLITUDE))
        } catch (_: Exception) {
        }
    }

    private fun hapticDouble() {
        try {
            vibrator?.vibrate(
                VibrationEffect.createWaveform(longArrayOf(0L, 16L, 55L, 24L), -1)
            )
        } catch (_: Exception) {
        }
    }
}
