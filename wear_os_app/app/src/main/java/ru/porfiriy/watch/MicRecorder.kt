package ru.porfiriy.watch

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.math.sqrt

class MicRecorder {
    private var recorder: AudioRecord? = null
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
        isRecording = true
        try {
            record.startRecording()
        } catch (e: Exception) {
            isRecording = false
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
                        val rmsNorm = computeNormalizedRms(copy, read)
                        smoothedRms = smoothedRms * 0.65f + rmsNorm * 0.35f
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
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            MediaRecorder.AudioSource.MIC,
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

    private fun computeNormalizedRms(pcm: ByteArray, len: Int): Float {
        val samples = len / 2
        if (samples <= 0) return 0f
        var sumSq = 0.0
        var i = 0
        while (i + 1 < len) {
            val lo = pcm[i].toInt() and 0xFF
            val hi = pcm[i + 1].toInt()
            val sample = (hi shl 8) or lo
            sumSq += sample * sample.toDouble()
            i += 2
        }
        val rms = sqrt(sumSq / samples).toFloat()
        // Map typical voice RMS (100..6500) to 0..1 smoothly
        return ((rms - 90f) / 4800f).coerceIn(0f, 1f)
    }
}
