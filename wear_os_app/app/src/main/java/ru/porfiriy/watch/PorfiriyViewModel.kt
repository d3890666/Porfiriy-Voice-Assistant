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

enum class WatchUiPhase {
    PERMISSION_NEEDED,
    CONNECTING,
    LISTENING,
    THINKING,
    RESPONDING,
    DONE,
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
)

class PorfiriyController(context: Context) {
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

    init {
        scope.launch {
            wsClient.events.collect { ev -> handleWsEvent(ev) }
        }
    }

    fun onPermissionUpdated(granted: Boolean, autoStart: Boolean = true) {
        hasAudioPermission = granted
        if (!granted) {
            _uiState.update { it.copy(phase = WatchUiPhase.PERMISSION_NEEDED, statusText = "Нужен микрофон") }
        } else if (autoStart && (_uiState.value.phase == WatchUiPhase.PERMISSION_NEEDED || _uiState.value.phase == WatchUiPhase.CONNECTING)) {
            startFreshSession()
        }
    }

    fun startFreshSession() {
        if (!hasAudioPermission) {
            _uiState.update { it.copy(phase = WatchUiPhase.PERMISSION_NEEDED, statusText = "Нужен микрофон") }
            return
        }
        val settings = settingsStore.settings.value
        _uiState.update {
            WatchUiState(
                phase = WatchUiPhase.CONNECTING,
                statusText = "Подключение…",
                routeBadge = "",
                boundUserName = it.boundUserName,
                hasTelegram = it.hasTelegram,
            )
        }
        startMicCapture()
        wsClient.connect(settings)
        hapticTick()
    }

    fun finishSpeaking() {
        if (_uiState.value.phase != WatchUiPhase.LISTENING && _uiState.value.phase != WatchUiPhase.CONNECTING) return
        mic.stop()
        wsClient.sendEndOfSpeech()
        _uiState.update { it.copy(phase = WatchUiPhase.THINKING, rms = 0f, statusText = "Думаю…") }
        hapticTick()
    }

    fun continueDialogue() {
        if (!hasAudioPermission) return
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
        wsClient.sendToTelegram(text)
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
            _uiState.update { it.copy(isProbing = false, probeStatus = msg) }
        }
    }

    fun pauseActiveMic() {
        mic.stop()
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
        mic.start(
            scope = scope,
            onChunk = { pcm, rms ->
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
                if (_uiState.value.phase == WatchUiPhase.CONNECTING || _uiState.value.phase == WatchUiPhase.LISTENING) {
                    mic.stop()
                    _uiState.update {
                        it.copy(phase = WatchUiPhase.ERROR, errorMessage = "Соединение закрыто", statusText = "Офлайн")
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
