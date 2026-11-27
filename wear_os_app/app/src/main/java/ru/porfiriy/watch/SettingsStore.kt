package ru.porfiriy.watch

import android.content.Context
import android.graphics.Color as AndroidColor
import androidx.compose.ui.graphics.Color
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.util.UUID

data class LedPalette(
    val idleHex: String = "#000000",
    val listenHex: String = "#0000ff",
    val thinkHex: String = "#ffaa00",
    val speakHex: String = "#00ff00",
) {
    val idleColor: Color get() = parseColorSafe(idleHex, Color(0xFF1E293B), treatBlackAsDim = true)
    val listenColor: Color get() = parseColorSafe(listenHex, Color(0xFF38BDF8), treatBlackAsDim = false)
    val thinkColor: Color get() = parseColorSafe(thinkHex, Color(0xFFFFAA00), treatBlackAsDim = false)
    val speakColor: Color get() = parseColorSafe(speakHex, Color(0xFF00FF88), treatBlackAsDim = false)

    companion object {
        fun parseColorSafe(hex: String, fallback: Color, treatBlackAsDim: Boolean): Color {
            val clean = hex.trim()
            if (clean.isEmpty()) return fallback
            if (treatBlackAsDim && (clean.equals("#000000", ignoreCase = true) || clean.equals("#000", ignoreCase = true))) {
                return Color(0xFF1E293B)
            }
            return try {
                val normalized = if (clean.startsWith("#")) clean else "#$clean"
                Color(AndroidColor.parseColor(normalized))
            } catch (_: Exception) {
                fallback
            }
        }
    }
}

data class WatchSettings(
    val primaryUrl: String = DEFAULT_PRIMARY_URL,
    val backupUrl: String = "",
    val apiKey: String = "",
    val deviceName: String = DEFAULT_DEVICE_NAME,
    val deviceMac: String = "",
    val palette: LedPalette = LedPalette(),
) {
    companion object {
        const val DEFAULT_PRIMARY_URL = "ws://192.168.110.50:8765"
        const val DEFAULT_DEVICE_NAME = "Pixel Watch 2"
    }
}

class SettingsStore(context: Context) {
    private val prefs = context.getSharedPreferences("porfiriy_watch_prefs", Context.MODE_PRIVATE)

    private val _settings = MutableStateFlow(loadFromPrefs())
    val settings: StateFlow<WatchSettings> = _settings.asStateFlow()

    private fun ensureMac(): String {
        val existing = prefs.getString(KEY_DEVICE_MAC, null)
        if (!existing.isNullOrBlank()) return existing
        val generated = "watch_" + UUID.randomUUID().toString().replace("-", "").take(8).lowercase()
        prefs.edit().putString(KEY_DEVICE_MAC, generated).apply()
        return generated
    }

    private fun loadFromPrefs(): WatchSettings {
        val mac = ensureMac()
        return WatchSettings(
            primaryUrl = prefs.getString(KEY_PRIMARY_URL, WatchSettings.DEFAULT_PRIMARY_URL)
                ?.ifBlank { WatchSettings.DEFAULT_PRIMARY_URL } ?: WatchSettings.DEFAULT_PRIMARY_URL,
            backupUrl = prefs.getString(KEY_BACKUP_URL, "") ?: "",
            apiKey = prefs.getString(KEY_API_KEY, "") ?: "",
            deviceName = prefs.getString(KEY_DEVICE_NAME, WatchSettings.DEFAULT_DEVICE_NAME)
                ?.ifBlank { WatchSettings.DEFAULT_DEVICE_NAME } ?: WatchSettings.DEFAULT_DEVICE_NAME,
            deviceMac = mac,
            palette = LedPalette(
                idleHex = prefs.getString(KEY_LED_IDLE, "#000000") ?: "#000000",
                listenHex = prefs.getString(KEY_LED_LISTEN, "#0000ff") ?: "#0000ff",
                thinkHex = prefs.getString(KEY_LED_THINK, "#ffaa00") ?: "#ffaa00",
                speakHex = prefs.getString(KEY_LED_SPEAK, "#00ff00") ?: "#00ff00",
            ),
        )
    }

    fun updatePrimaryUrl(url: String) {
        val clean = normalizeWsUrl(url).ifBlank { WatchSettings.DEFAULT_PRIMARY_URL }
        prefs.edit().putString(KEY_PRIMARY_URL, clean).apply()
        _settings.value = _settings.value.copy(primaryUrl = clean)
    }

    fun updateBackupUrl(url: String) {
        val clean = if (url.isBlank()) "" else normalizeWsUrl(url)
        prefs.edit().putString(KEY_BACKUP_URL, clean).apply()
        _settings.value = _settings.value.copy(backupUrl = clean)
    }

    fun updateApiKey(key: String) {
        val clean = key.trim()
        prefs.edit().putString(KEY_API_KEY, clean).apply()
        _settings.value = _settings.value.copy(apiKey = clean)
    }

    fun updateDeviceName(name: String) {
        val clean = name.trim().ifBlank { WatchSettings.DEFAULT_DEVICE_NAME }
        prefs.edit().putString(KEY_DEVICE_NAME, clean).apply()
        _settings.value = _settings.value.copy(deviceName = clean)
    }

    fun updateLedColors(idle: String?, listen: String?, think: String?, speak: String?) {
        val cur = _settings.value.palette
        val next = LedPalette(
            idleHex = idle?.takeIf { it.isNotBlank() } ?: cur.idleHex,
            listenHex = listen?.takeIf { it.isNotBlank() } ?: cur.listenHex,
            thinkHex = think?.takeIf { it.isNotBlank() } ?: cur.thinkHex,
            speakHex = speak?.takeIf { it.isNotBlank() } ?: cur.speakHex,
        )
        prefs.edit()
            .putString(KEY_LED_IDLE, next.idleHex)
            .putString(KEY_LED_LISTEN, next.listenHex)
            .putString(KEY_LED_THINK, next.thinkHex)
            .putString(KEY_LED_SPEAK, next.speakHex)
            .apply()
        _settings.value = _settings.value.copy(palette = next)
    }

    companion object {
        private const val KEY_PRIMARY_URL = "primary_url"
        private const val KEY_BACKUP_URL = "backup_url"
        private const val KEY_API_KEY = "api_key"
        private const val KEY_DEVICE_NAME = "device_name"
        private const val KEY_DEVICE_MAC = "device_mac"
        private const val KEY_LED_IDLE = "led_color_idle"
        private const val KEY_LED_LISTEN = "led_color_listen"
        private const val KEY_LED_THINK = "led_color_think"
        private const val KEY_LED_SPEAK = "led_color_speak"

        fun normalizeWsUrl(raw: String): String {
            val trimmed = raw.trim()
            if (trimmed.isEmpty()) return ""
            if (trimmed.startsWith("ws://", ignoreCase = true) || trimmed.startsWith("wss://", ignoreCase = true)) {
                return trimmed
            }
            if (trimmed.startsWith("http://", ignoreCase = true)) {
                return "ws://" + trimmed.substring(7)
            }
            if (trimmed.startsWith("https://", ignoreCase = true)) {
                return "wss://" + trimmed.substring(8)
            }
            return "ws://$trimmed"
        }
    }
}
