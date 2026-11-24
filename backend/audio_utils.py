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


# ---------------------------------------------------------------------------
# Библиотека приятных звуковых подтверждений (Earcons) 24 kHz 16-bit mono PCM
# ---------------------------------------------------------------------------

EARCON_CATALOG: Dict[str, Dict[str, str]] = {
    "volume": {
        "alice_click": "Мягкий щелчок (как у Алисы)",
        "wood_tick": "Тёплый деревянный тик",
        "crystal_tap": "Чистый кристальный клик",
        "soft_pop": "Деликатный мягкий поп",
    },
    "smarthome": {
        "marimba_tudun": "Мягкий «ту-дун» (маримба)",
        "warm_chord": "Тёплый гармонический аккорд",
        "short_ding": "Короткий чистый дзынь",
        "cyber_ping": "Лёгкий футуристичный пинг",
    },
    "wake": {
        "soft_chime": "Мягкое «слушаю»",
        "subtle_bloop": "Деликатный короткий отклик",
        "double_tap": "Лёгкий двойной импульс",
        "none": "Без звука (выкл)",
    },
}

_EARCON_CACHE: Dict[tuple, bytes] = {}


def _synth_note(
    freq_start: float,
    freq_end: float,
    duration_ms: float,
    sample_rate: int = 24000,
    amp: float = 0.45,
    attack_ms: float = 4.0,
    decay_tau_ms: float = 60.0,
    harmonics: tuple = ((1.0, 1.0),),
) -> list:
    """Синтезирует мягкую затухающую ноту/импульс с плавной атакой (float -1..1)."""
    n = max(1, int(sample_rate * duration_ms / 1000.0))
    att = max(1, int(sample_rate * attack_ms / 1000.0))
    tau = max(1.0, sample_rate * decay_tau_ms / 1000.0)
    out = [0.0] * n
    phase = 0.0
    for i in range(n):
        frac = i / float(n)
        f = freq_start + (freq_end - freq_start) * frac
        phase += 2.0 * math.pi * f / sample_rate
        env = (i / float(att)) if i < att else math.exp(-(i - att) / tau)
        # Плавное сведение в 0 на последних 5 мс, чтобы не было щелчка на конце
        tail_n = min(n // 4, int(sample_rate * 0.005))
        if tail_n > 0 and i >= n - tail_n:
            env *= (n - i) / float(tail_n)
        val = 0.0
        for mult, weight in harmonics:
            val += weight * math.sin(phase * mult)
        out[i] = val * env * amp
    return out


def _mix_layers(layers: list, total_ms: float, sample_rate: int = 24000) -> bytes:
    """Смешивает список кортежей (offset_ms, samples_float) в 16-bit mono PCM."""
    total_n = max(1, int(sample_rate * total_ms / 1000.0))
    buf = [0.0] * total_n
    for offset_ms, samples in layers:
        start = int(sample_rate * offset_ms / 1000.0)
        for i, s in enumerate(samples):
            idx = start + i
            if 0 <= idx < total_n:
                buf[idx] += s
    # Мягкий лимитер
    peak = max((abs(x) for x in buf), default=0.0)
    scale = (0.85 / peak) if peak > 0.85 else 1.0
    out = bytearray(total_n * 2)
    for i, x in enumerate(buf):
        v = max(-32767, min(32767, int(round(x * scale * 32767.0))))
        struct.pack_into("<h", out, i * 2, v)
    return bytes(out)


def _build_earcon_pcm(category: str, preset: str, sample_rate: int = 24000) -> bytes:
    if category == "volume":
        if preset == "wood_tick":
            s = _synth_note(920, 680, 32, sample_rate, amp=0.50, attack_ms=1.0, decay_tau_ms=8.0, harmonics=((1.0, 0.8), (2.3, 0.25)))
            return _mix_layers([(0, s)], 40, sample_rate)
        if preset == "crystal_tap":
            s = _synth_note(2100, 1950, 28, sample_rate, amp=0.38, attack_ms=0.8, decay_tau_ms=7.0, harmonics=((1.0, 0.85), (1.5, 0.2)))
            return _mix_layers([(0, s)], 36, sample_rate)
        if preset == "soft_pop":
            s = _synth_note(360, 170, 42, sample_rate, amp=0.55, attack_ms=2.0, decay_tau_ms=11.0, harmonics=((1.0, 1.0),))
            return _mix_layers([(0, s)], 48, sample_rate)
        # default: alice_click
        s1 = _synth_note(1350, 580, 26, sample_rate, amp=0.48, attack_ms=0.8, decay_tau_ms=6.5, harmonics=((1.0, 0.85), (2.0, 0.2)))
        return _mix_layers([(0, s1)], 34, sample_rate)

    if category == "smarthome":
        if preset == "warm_chord":
            n1 = _synth_note(349.23, 349.23, 250, sample_rate, amp=0.30, attack_ms=8.0, decay_tau_ms=90.0, harmonics=((1.0, 0.85), (2.0, 0.18)))
            n2 = _synth_note(440.00, 440.00, 240, sample_rate, amp=0.28, attack_ms=8.0, decay_tau_ms=85.0, harmonics=((1.0, 0.85), (2.0, 0.15)))
            n3 = _synth_note(523.25, 523.25, 230, sample_rate, amp=0.26, attack_ms=10.0, decay_tau_ms=80.0, harmonics=((1.0, 0.9),))
            return _mix_layers([(0, n1), (22, n2), (44, n3)], 290, sample_rate)
        if preset == "short_ding":
            n1 = _synth_note(880.0, 880.0, 210, sample_rate, amp=0.40, attack_ms=3.0, decay_tau_ms=65.0, harmonics=((1.0, 0.8), (2.0, 0.22), (3.0, 0.06)))
            return _mix_layers([(0, n1)], 225, sample_rate)
        if preset == "cyber_ping":
            n1 = _synth_note(659.25, 659.25, 95, sample_rate, amp=0.36, attack_ms=3.0, decay_tau_ms=35.0, harmonics=((1.0, 0.9), (2.0, 0.12)))
            n2 = _synth_note(987.77, 987.77, 130, sample_rate, amp=0.38, attack_ms=3.0, decay_tau_ms=48.0, harmonics=((1.0, 0.9), (2.0, 0.12)))
            return _mix_layers([(0, n1), (68, n2)], 215, sample_rate)
        # default: marimba_tudun (мягкий «ту-дун» C5 -> G5)
        n1 = _synth_note(523.25, 523.25, 110, sample_rate, amp=0.42, attack_ms=3.0, decay_tau_ms=38.0, harmonics=((1.0, 0.85), (2.0, 0.15), (4.0, 0.05)))
        n2 = _synth_note(783.99, 783.99, 175, sample_rate, amp=0.44, attack_ms=3.0, decay_tau_ms=65.0, harmonics=((1.0, 0.85), (2.0, 0.14), (4.0, 0.04)))
        return _mix_layers([(0, n1), (78, n2)], 265, sample_rate)

    if category == "wake":
        if preset == "none":
            return b""
        if preset == "subtle_bloop":
            n1 = _synth_note(440.0, 620.0, 105, sample_rate, amp=0.36, attack_ms=4.0, decay_tau_ms=38.0, harmonics=((1.0, 0.9), (2.0, 0.1)))
            return _mix_layers([(0, n1)], 120, sample_rate)
        if preset == "double_tap":
            n1 = _synth_note(740.0, 680.0, 38, sample_rate, amp=0.38, attack_ms=1.5, decay_tau_ms=12.0, harmonics=((1.0, 0.9),))
            n2 = _synth_note(980.0, 920.0, 44, sample_rate, amp=0.40, attack_ms=1.5, decay_tau_ms=15.0, harmonics=((1.0, 0.9),))
            return _mix_layers([(0, n1), (62, n2)], 120, sample_rate)
        # default: soft_chime (E5 -> B5)
        n1 = _synth_note(659.25, 659.25, 95, sample_rate, amp=0.34, attack_ms=4.0, decay_tau_ms=36.0, harmonics=((1.0, 0.88), (2.0, 0.12)))
        n2 = _synth_note(987.77, 987.77, 135, sample_rate, amp=0.36, attack_ms=4.0, decay_tau_ms=52.0, harmonics=((1.0, 0.88), (2.0, 0.12)))
        return _mix_layers([(0, n1), (65, n2)], 210, sample_rate)

    return b""


def get_earcon_pcm(category: str, preset: str, sample_rate: int = 24000, volume: float = 1.0) -> bytes:
    """Возвращает 16-bit mono PCM для выбранного пресета earcon (с кешированием)."""
    key = (category, preset, sample_rate)
    if key not in _EARCON_CACHE:
        _EARCON_CACHE[key] = _build_earcon_pcm(category, preset, sample_rate=sample_rate)
    pcm = _EARCON_CACHE[key]
    return scale_pcm16(pcm, volume) if (pcm and volume != 1.0) else pcm


def get_earcon_wav(category: str, preset: str, sample_rate: int = 24000) -> bytes:
    """Возвращает WAV-байты для предпрослушивания earcon в браузере."""
    pcm = get_earcon_pcm(category, preset, sample_rate=sample_rate, volume=1.0)
    return pcm16_to_wav(pcm, sample_rate=sample_rate) if pcm else pcm16_to_wav(b"\x00" * 480, sample_rate=sample_rate)


def list_earcon_presets() -> Dict[str, Dict[str, str]]:
    return {k: dict(v) for k, v in EARCON_CATALOG.items()}


_SIMPLE_SMARTHOME_DOMAINS = {
    "light", "switch", "cover", "fan", "scene", "script",
    "input_boolean", "lock", "humidifier", "valve", "siren",
}
_SIMPLE_SMARTHOME_SERVICES = {
    "turn_on", "turn_off", "toggle",
    "open_cover", "close_cover", "stop_cover", "set_cover_position",
    "set_percentage", "lock", "unlock", "open", "close",
}


def is_simple_smarthome_action(domain: str, service: str) -> bool:
    """Определяет, является ли вызов HA простым действием вкл/выкл, для которого достаточно звука «тудун»."""
    d = (domain or "").strip().lower()
    s = (service or "").strip().lower()
    return d in _SIMPLE_SMARTHOME_DOMAINS and (s in _SIMPLE_SMARTHOME_SERVICES or d in ("scene", "script"))


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
