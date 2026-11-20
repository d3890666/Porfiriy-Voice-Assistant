import io
import logging
import math
import struct
import wave
from typing import Any, Dict

logger = logging.getLogger("audio_utils")


def calculate_pcm_rms(pcm_data: bytes) -> float:
    """Вычисляет среднеквадратичную амплитуду (RMS) 16-битного PCM звука."""
    if not pcm_data or len(pcm_data) < 2:
        return 0.0
    try:
        import audioop
        return float(audioop.rms(pcm_data, 2))
    except Exception:
        count = len(pcm_data) // 2
        if count == 0:
            return 0.0
        shorts = struct.unpack(f"<{count}h", pcm_data[: count * 2])
        return math.sqrt(sum(s * s for s in shorts) / count)


def rms_to_dbfs(rms: float) -> float:
    """Конвертирует линейный RMS (16-bit PCM) в dBFS."""
    return round(20.0 * math.log10(max(1.0, float(rms)) / 32768.0), 1)


def scale_pcm16(pcm_data: bytes, volume: float) -> bytes:
    """Масштабирует громкость 16-битного PCM звука с защитой от клиппинга."""
    if not pcm_data or volume == 1.0:
        return pcm_data
    volume = max(0.0, min(2.0, float(volume)))
    try:
        import audioop
        return audioop.mul(pcm_data, 2, volume)
    except Exception:
        import array
        arr = array.array("h")
        arr.frombytes(pcm_data)
        for i in range(len(arr)):
            v = int(arr[i] * volume)
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            arr[i] = v
        return arr.tobytes()


def generate_beep(freq: int, duration_ms: int, sample_rate: int = 16000, volume: float = 0.5) -> bytes:
    """Генерация сырого 16-bit PCM аудио сигнала (синусоиды)."""
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    audio = bytearray()
    fade_len = min(100, num_samples // 4) if num_samples >= 4 else 0
    for i in range(num_samples):
        sample = math.sin(2 * math.pi * freq * i / sample_rate)
        if fade_len:
            if i < fade_len:
                sample *= i / fade_len
            elif i > num_samples - fade_len:
                sample *= (num_samples - i) / fade_len
        val = int(sample * volume * 32767)
        audio.extend(struct.pack("<h", val))
    return bytes(audio)


# Предварительно сгенерированные звуки (Earcons)
SUCCESS_CHIME = generate_beep(600, 100) + generate_beep(800, 150)
ERROR_CHIME = generate_beep(300, 150) + generate_beep(200, 200)


def filter_pcm16_highpass(pcm_data: bytes, cutoff_hz: float = 85.0, sample_rate: int = 16000) -> bytes:
    """Удаление сетевого гула 50/100 Гц и постоянного смещения (DC offset) через High-Pass фильтр 2-го порядка."""
    if not pcm_data:
        return b""
    try:
        import numpy as np
        from scipy import signal
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        if len(samples) < 8:
            return pcm_data
        b, a = signal.butter(2, cutoff_hz, btype="highpass", fs=sample_rate)
        filtered = signal.lfilter(b, a, samples)
        clamped = np.clip(np.round(filtered), -32768, 32767).astype(np.int16)
        return clamped.tobytes()
    except Exception as e:
        logger.debug(f"[FILTER] High-pass filter error: {e}")
        return pcm_data


def pcm16_to_wav(pcm_data: bytes, sample_rate: int = 16000) -> bytes:
    """Упаковка сырых 16-битных PCM сэмплов в стандартный RIFF WAV контейнер."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def analyze_pcm16(pcm_data: bytes, sample_rate: int = 16000) -> Dict[str, Any]:
    """Расчет акустических метрик (RMS dBFS, Peak, клиппинг)."""
    if not pcm_data:
        return {
            "duration_s": 0.0,
            "samples_count": 0,
            "rms_linear": 0.0,
            "rms_dbfs": -100.0,
            "peak": 0,
            "peak_dbfs": -100.0,
            "clipping_samples": 0,
            "clipping_percent": 0.0,
            "quality": "no_audio",
        }

    num_samples = len(pcm_data) // 2
    samples = struct.unpack(f"<{num_samples}h", pcm_data[: num_samples * 2])

    peak = 0
    clipping_count = 0
    sum_sq = 0.0

    for s in samples:
        abs_s = abs(s)
        if abs_s > peak:
            peak = abs_s
        if abs_s >= 32600:
            clipping_count += 1
        sum_sq += s * s

    rms = math.sqrt(sum_sq / num_samples) if num_samples > 0 else 0.0
    rms_db = 20.0 * math.log10(rms / 32768.0) if rms > 0 else -100.0
    peak_db = 20.0 * math.log10(peak / 32768.0) if peak > 0 else -100.0
    clip_pct = (clipping_count / num_samples) * 100.0 if num_samples > 0 else 0.0

    quality = "optimal"
    if clip_pct > 1.5:
        quality = "critical_clipping"
    elif clip_pct > 0.1:
        quality = "warning_clipping"
    elif rms_db < -42.0:
        quality = "too_quiet"
    elif rms_db > -10.0:
        quality = "loud"

    return {
        "duration_s": round(num_samples / float(sample_rate), 2),
        "samples_count": num_samples,
        "rms_linear": round(rms, 1),
        "rms_dbfs": round(rms_db, 1),
        "peak": peak,
        "peak_dbfs": round(peak_db, 1),
        "clipping_samples": clipping_count,
        "clipping_percent": round(clip_pct, 2),
        "quality": quality,
    }
