package ru.porfiriy.watch

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AutomaticGainControl
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.math.abs
import kotlin.math.log10
import kotlin.math.sqrt
import kotlin.math.tanh

class MicRecorder {
    private var recorder: AudioRecord? = null
    private var agc: AutomaticGainControl? = null
    private var recordJob: Job? = null

    @Volatile
    var isRecording: Boolean = false
        private set

    @SuppressLint("MissingPermission")
    fun start(
        scope: CoroutineScope,
        onChunk: (pcmBytes: ByteArray, normalizedRms: Float) -> Unit,
        onError: (String) -> Unit,
    ) {
        stop()

        val sampleRate = 16000
        val channelConfig = AudioFormat.CHANNEL_IN_MONO
        val audioFormat = AudioFormat.ENCODING_PCM_16BIT
        val minBuf = AudioRecord.getMinBufferSize(sampleRate, channelConfig, audioFormat)
            .coerceAtLeast(4096)

        val record = createAudioRecord(sampleRate, channelConfig, audioFormat, minBuf * 2)
        if (record == null || record.state != AudioRecord.STATE_INITIALIZED) {
            record?.release()
            onError("Не удалось инициализировать микрофон часов")
            return
        }

        recorder = record
        try {
            if (AutomaticGainControl.isAvailable()) {
                val effect = AutomaticGainControl.create(record.audioSessionId)
                if (effect != null) {
                    effect.enabled = true
                    agc = effect
                }
            }
        } catch (_: Exception) {
        }
        isRecording = true
        try {
            record.startRecording()
        } catch (e: Exception) {
            isRecording = false
            try { agc?.release() } catch (_: Exception) {}
            agc = null
            record.release()
            recorder = null
            onError("Ошибка запуска микрофона: ${e.message}")
            return
        }

        recordJob = scope.launch(Dispatchers.IO) {
            val buffer = ByteArray(1024) // 512 samples = 32 ms @ 16kHz
            var smoothedRms = 0f
            try {
                while (isActive && isRecording) {
                    val read = record.read(buffer, 0, buffer.size)
                    if (read > 0) {
                        val copy = buffer.copyOf(read)
                        val rmsNorm = applySoftBoostAndNormalize(copy, read)
                        smoothedRms = if (rmsNorm > smoothedRms) {
                            smoothedRms * 0.30f + rmsNorm * 0.70f
                        } else {
                            smoothedRms * 0.70f + rmsNorm * 0.30f
                        }
                        onChunk(copy, smoothedRms)
                    } else if (read < 0) {
                        break
                    }
                }
            } catch (_: Exception) {
            }
        }
    }

    fun stop() {
        isRecording = false
        recordJob?.cancel()
        recordJob = null
        try {
            agc?.release()
        } catch (_: Exception) {
        }
        agc = null
        val r = recorder
        recorder = null
        if (r != null) {
            try {
                if (r.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
                    r.stop()
                }
            } catch (_: Exception) {
            }
            try {
                r.release()
            } catch (_: Exception) {
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun createAudioRecord(
        sampleRate: Int,
        channelConfig: Int,
        audioFormat: Int,
        bufferSize: Int,
    ): AudioRecord? {
        val sources = listOf(
            MediaRecorder.AudioSource.MIC,
            MediaRecorder.AudioSource.DEFAULT,
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
        )
        for (src in sources) {
            try {
                val ar = AudioRecord(src, sampleRate, channelConfig, audioFormat, bufferSize)
                if (ar.state == AudioRecord.STATE_INITIALIZED) {
                    return ar
                }
                ar.release()
            } catch (_: Exception) {
            }
        }
        return null
    }

    private fun applySoftBoostAndNormalize(pcm: ByteArray, len: Int, gain: Float = 2.5f): Float {
        val samples = len / 2
        if (samples <= 0) return 0f
        var sumSq = 0.0
        var i = 0
        while (i + 1 < len) {
            val lo = pcm[i].toInt() and 0xFF
            val hi = pcm[i + 1].toInt()
            val raw = ((hi shl 8) or lo).toFloat()
            val boosted = raw * gain
            val mag = abs(boosted)
            val limited = if (mag <= 24000f) {
                boosted
            } else {
                val sign = if (boosted >= 0f) 1f else -1f
                sign * (24000f + 8767f * tanh(((mag - 24000f) / 8767f).toDouble()).toFloat())
            }
            val clamped = limited.toInt().coerceIn(-32768, 32767)
            pcm[i] = (clamped and 0xFF).toByte()
            pcm[i + 1] = ((clamped shr 8) and 0xFF).toByte()
            sumSq += clamped * clamped.toDouble()
            i += 2
        }
        val rms = sqrt(sumSq / samples).toFloat()
        val dbfs = if (rms < 1f) -90f else (20.0 * log10(rms / 32768.0)).toFloat()
        // Map -58..-14 dBFS logarithmically to 0..1
        return ((dbfs + 58f) / 44f).coerceIn(0f, 1f)
    }
}
