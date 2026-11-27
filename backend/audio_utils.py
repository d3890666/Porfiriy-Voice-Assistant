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


ADAPTIVE_VOLUME_MODES: Dict[str, tuple] = {
    "weak": (0.60, 0.95),
    "medium": (0.38, 0.90),
    "strong": (0.22, 0.80),
}


def measure_speech_dbfs(pcm_data: bytes, sample_rate: int = 16000) -> float:
    """Оценивает громкость речи пользователя по 80-му перцентилю 80мс кадров (игнорируя тишину по краям)."""
    if not pcm_data or len(pcm_data) < 2:
        return -100.0
    frame_bytes = max(320, int(sample_rate * 0.08) * 2)
    rms_list = []
    for off in range(0, len(pcm_data) - frame_bytes + 1, frame_bytes):
        rms_list.append(calculate_pcm_rms(pcm_data[off : off + frame_bytes]))
    if not rms_list:
        rms_list.append(calculate_pcm_rms(pcm_data))
    rms_list.sort()
    p80 = rms_list[min(len(rms_list) - 1, int(len(rms_list) * 0.80))]
    return rms_to_dbfs(p80)


def compute_adaptive_voice_gain(speech_dbfs: float, mode: str = "medium") -> float:
    """Адаптивный коэффициент громкости ответа <= 1.0 (только ослабление, никогда не усиливает выше шкалы колонки)."""
    min_g, max_g = ADAPTIVE_VOLUME_MODES.get((mode or "medium").strip().lower(), ADAPTIVE_VOLUME_MODES["medium"])
    t = max(0.0, min(1.0, (float(speech_dbfs) + 36.0) / 22.0))
    return round(min_g + t * (max_g - min_g), 2)


def compute_response_voice_gain(
    speech_dbfs: float,
    dev_cfg: Dict[str, Any],
    options: Dict[str, Any],
    quiet_active: bool,
) -> float:
    """Итоговый коэффициент громкости ответа с учётом адаптивной громкости колонки и ночного тихого режима."""
    gain = 1.0
    if isinstance(dev_cfg, dict) and dev_cfg.get("adaptive_volume_enabled"):
        gain = compute_adaptive_voice_gain(speech_dbfs, str(dev_cfg.get("adaptive_volume_mode") or "medium"))
    if quiet_active:
        q_vol = max(0.0, min(1.0, float((options or {}).get("quiet_mode_volume", 0.30))))
        gain = 0.0 if q_vol <= 0.0 else min(gain, q_vol)
    return round(max(0.0, min(1.0, gain)), 2)


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
# Студийная библиотека звуковых подтверждений (Earcons) 24 kHz 16-bit mono PCM
# Вдохновлено звуковым дизайном Apple HomePod / macOS / Google Nest
# ---------------------------------------------------------------------------

EARCON_CATALOG: Dict[str, Dict[str, str]] = {
    "volume": {
        "apple_taptic": "Apple Taptic (тактильный щелчок)",
        "nest_bubble": "Google Nest (бархатный поп)",
        "studio_wood": "Studio Woodblock (деревянный тик)",
        "glass_tick": "macOS Glass (хрустальный клик)",
    },
    "smarthome": {
        "google_home": "Google Home (тёплый «ту-дун»)",
        "apple_homepod": "Apple HomePod (глубокий бархатный аккорд)",
        "macos_glass": "macOS Bloom (кристальный maj7)",
        "warm_marimba": "Sonos Marimba (деревянная маримба)",
    },
    "wake": {
        "google_listen": "Google Listen (мягкий подъём)",
        "apple_siri": "Apple Siri (деликатный двойной отклик)",
        "warm_halo": "Warm Halo (тёплое сияние)",
        "none": "Без звука (выкл)",
    },
}

_LEGACY_PRESET_ALIAS = {
    "alice_click": "apple_taptic",
    "soft_pop": "nest_bubble",
    "wood_tick": "studio_wood",
    "crystal_tap": "glass_tick",
    "marimba_tudun": "google_home",
    "warm_chord": "apple_homepod",
    "short_ding": "macos_glass",
    "cyber_ping": "warm_marimba",
    "soft_chime": "google_listen",
    "subtle_bloop": "apple_siri",
    "double_tap": "warm_halo",
}

_EARCON_CACHE: Dict[tuple, bytes] = {}


def _apply_warm_reverb_and_lpf(buf: list, sample_rate: int = 24000, wet: float = 0.22, lpf_alpha: float = 0.72) -> list:
    """Мягкий студийный room-reverb (multi-tap early reflections) + тёплый 1-pole LPF."""
    n = len(buf)
    # Короткие ранние отражения комнаты (мс, коэффициент)
    taps = (
        (13.0, 0.34),
        (23.5, 0.26),
        (37.0, 0.19),
        (53.5, 0.13),
        (74.0, 0.08),
        (98.0, 0.05),
    )
    out = list(buf)
    if wet > 0.0:
        for delay_ms, gain in taps:
            d = int(sample_rate * delay_ms / 1000.0)
            g = gain * wet
            if d < n:
                for i in range(d, n):
                    out[i] += buf[i - d] * g
    # Тёплый low-pass фильтр убирает «цифровую» резкость
    if 0.0 < lpf_alpha < 1.0:
        y = 0.0
        inv = 1.0 - lpf_alpha
        for i in range(n):
            y = lpf_alpha * out[i] + inv * y
            out[i] = y
    return out


def _synth_rich_tone(
    freq_start: float,
    freq_end: float,
    duration_ms: float,
    sample_rate: int = 24000,
    amp: float = 0.42,
    attack_ms: float = 6.0,
    decay_tau_ms: float = 95.0,
    partials: tuple = ((1.0, 1.0, 1.0), (2.0, 0.22, 0.55), (3.0, 0.06, 0.35)),
    detune_hz: float = 1.2,
    fm_index: float = 0.0,
    fm_ratio: float = 2.0,
    mallet_ms: float = 0.0,
) -> list:
    """
    Богатый физико-модельный тон:
    - partials: кортежи (freq_mult, weight, decay_speed_factor) — высокие гармоники затухают быстрее, как в живых инструментах!
    - detune_hz: лёгкий хорус/объём
    - fm_index: мягкая FM-окраска (Rhodes / стеклянная маримба)
    - mallet_ms: короткий тёплый импульс удара молоточка в начале
    """
    n = max(1, int(sample_rate * duration_ms / 1000.0))
    att = max(1, int(sample_rate * attack_ms / 1000.0))
    base_tau = max(1.0, sample_rate * decay_tau_ms / 1000.0)
    mallet_n = int(sample_rate * mallet_ms / 1000.0)
    tail_n = min(n // 4, int(sample_rate * 0.012))

    out = [0.0] * n
    p1 = 0.0
    p2 = 0.0
    pfm = 0.0
    # Детерминированный LCG для мягкого удара молоточка
    seed = 1234567
    for i in range(n):
        frac = i / float(n)
        # Экспоненциальный питч-глайд (естественнее линейного)
        f = freq_end + (freq_start - freq_end) * math.exp(-frac * 5.0)
        p1 += 2.0 * math.pi * f / sample_rate
        p2 += 2.0 * math.pi * (f + detune_hz) / sample_rate
        pfm += 2.0 * math.pi * (f * fm_ratio) / sample_rate

        # Плавная косинусная атака (без щелчка)
        att_env = 0.5 * (1.0 - math.cos(math.pi * i / float(att))) if i < att else 1.0
        dt = max(0, i - att)

        fm_mod = (fm_index * math.exp(-dt / (base_tau * 0.25)) * math.sin(pfm)) if fm_index > 0.0 else 0.0

        val = 0.0
        for mult, weight, tau_scale in partials:
            p_env = math.exp(-dt / max(1.0, base_tau * tau_scale))
            s = math.sin(p1 * mult + fm_mod)
            if detune_hz > 0.0 and mult <= 2.1:
                s = 0.65 * s + 0.35 * math.sin(p2 * mult + fm_mod)
            val += weight * s * p_env

        if mallet_n > 0 and i < mallet_n:
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            noise = (seed / 1073741824.0) - 1.0
            m_env = math.sin(math.pi * i / float(mallet_n))
            val += noise * 0.08 * m_env

        end_env = ((n - i) / float(tail_n)) if (tail_n > 0 and i >= n - tail_n) else 1.0
        out[i] = val * att_env * end_env * amp
    return out


def _mix_studio(layers: list, total_ms: float, sample_rate: int = 24000, reverb_wet: float = 0.24, lpf_alpha: float = 0.72) -> bytes:
    """Смешивает слои, добавляет студийный room-reverb, мягкую сатурацию и пакует в 16-bit PCM."""
    total_n = max(1, int(sample_rate * total_ms / 1000.0))
    buf = [0.0] * total_n
    for offset_ms, samples in layers:
        start = int(sample_rate * offset_ms / 1000.0)
        for i, s in enumerate(samples):
            idx = start + i
            if 0 <= idx < total_n:
                buf[idx] += s

    buf = _apply_warm_reverb_and_lpf(buf, sample_rate=sample_rate, wet=reverb_wet, lpf_alpha=lpf_alpha)

    # Нормализация с мягким аналоговым лимитером (tanh) для богатого и плотного звучания
    peak = max((abs(x) for x in buf), default=0.0)
    gain = (0.78 / peak) if peak > 1e-5 else 1.0
    out = bytearray(total_n * 2)
    for i, x in enumerate(buf):
        y = math.tanh(x * gain * 1.15) * 0.88
        v = max(-32767, min(32767, int(round(y * 32767.0))))
        struct.pack_into("<h", out, i * 2, v)
    return bytes(out)


def _build_earcon_pcm(category: str, preset: str, sample_rate: int = 24000) -> bytes:
    preset = _LEGACY_PRESET_ALIAS.get(preset, preset)

    if category == "volume":
        if preset == "nest_bubble":
            # Google Nest: бархатный мягкий «бульк» вверх
            s1 = _synth_rich_tone(360, 640, 52, sample_rate, amp=0.50, attack_ms=3.0, decay_tau_ms=16.0,
                                  partials=((1.0, 1.0, 1.0), (2.0, 0.14, 0.4)), detune_hz=0.0)
            return _mix_studio([(0, s1)], 85, sample_rate, reverb_wet=0.14, lpf_alpha=0.55)
        if preset == "studio_wood":
            # Реалистичный деревянный вудблок (резонансы 1.0 + 2.76)
            s1 = _synth_rich_tone(780, 740, 48, sample_rate, amp=0.48, attack_ms=1.2, decay_tau_ms=13.0,
                                  partials=((1.0, 1.0, 1.0), (2.76, 0.28, 0.35)), detune_hz=0.0, mallet_ms=3.5)
            return _mix_studio([(0, s1)], 85, sample_rate, reverb_wet=0.18, lpf_alpha=0.68)
        if preset == "glass_tick":
            # Деликатный хрустальный клик (macOS style)
            s1 = _synth_rich_tone(1680, 1650, 46, sample_rate, amp=0.38, attack_ms=1.0, decay_tau_ms=12.0,
                                  partials=((1.0, 0.9, 1.0), (2.02, 0.25, 0.45), (3.5, 0.08, 0.25)), detune_hz=1.5)
            return _mix_studio([(0, s1)], 85, sample_rate, reverb_wet=0.22, lpf_alpha=0.75)
        # default: apple_taptic — фирменный мягкий тактильный щелчок (HomePod / iOS)
        s_body = _synth_rich_tone(480, 155, 28, sample_rate, amp=0.55, attack_ms=0.8, decay_tau_ms=7.0,
                                  partials=((1.0, 1.0, 1.0), (2.2, 0.18, 0.35)), detune_hz=0.0, mallet_ms=2.0)
        return _mix_studio([(0, s_body)], 58, sample_rate, reverb_wet=0.10, lpf_alpha=0.52)

    if category == "smarthome":
        if preset == "apple_homepod":
            # Apple HomePod: глубокий, тёплый, дорогой двухнотный бархатный аккорд (D4 -> A4 + суб-тепло)
            p_warm = ((0.5, 0.22, 1.1), (1.0, 1.0, 1.0), (2.0, 0.20, 0.50), (3.0, 0.05, 0.25))
            n1 = _synth_rich_tone(293.66, 293.66, 210, sample_rate, amp=0.36, attack_ms=10.0, decay_tau_ms=95.0,
                                  partials=p_warm, detune_hz=1.1, fm_index=0.25, fm_ratio=2.0)
            n2 = _synth_rich_tone(440.00, 440.00, 320, sample_rate, amp=0.40, attack_ms=10.0, decay_tau_ms=135.0,
                                  partials=p_warm, detune_hz=1.4, fm_index=0.30, fm_ratio=2.0)
            n3 = _synth_rich_tone(659.25, 659.25, 280, sample_rate, amp=0.16, attack_ms=14.0, decay_tau_ms=110.0,
                                  partials=((1.0, 1.0, 1.0), (2.0, 0.15, 0.4)), detune_hz=1.8)
            return _mix_studio([(0, n1), (95, n2), (105, n3)], 470, sample_rate, reverb_wet=0.30, lpf_alpha=0.60)
        if preset == "macos_glass":
            # macOS Bloom: богатый кристальный арпеджио-аккорд Cmaj7 (C5 - E5 - G5 - B5)
            p_glass = ((1.0, 1.0, 1.0), (2.0, 0.24, 0.55), (3.01, 0.10, 0.35), (4.0, 0.04, 0.2))
            notes = [(0, 523.25, 0.26), (34, 659.25, 0.26), (68, 783.99, 0.28), (102, 987.77, 0.24)]
            layers = [
                (off, _synth_rich_tone(f, f, 290, sample_rate, amp=a, attack_ms=5.0, decay_tau_ms=115.0,
                                       partials=p_glass, detune_hz=1.6, fm_index=0.2, fm_ratio=3.0))
                for off, f, a in notes
            ]
            return _mix_studio(layers, 460, sample_rate, reverb_wet=0.34, lpf_alpha=0.76)
        if preset == "warm_marimba":
            # Sonos / тёплая деревянная маримба (C5 -> G5 с ударом мягкого молоточка)
            p_mar = ((1.0, 1.0, 1.0), (3.98, 0.18, 0.22), (2.0, 0.08, 0.35))
            n1 = _synth_rich_tone(523.25, 523.25, 165, sample_rate, amp=0.44, attack_ms=2.5, decay_tau_ms=65.0,
                                  partials=p_mar, detune_hz=0.8, mallet_ms=4.0)
            n2 = _synth_rich_tone(783.99, 783.99, 270, sample_rate, amp=0.46, attack_ms=2.5, decay_tau_ms=105.0,
                                  partials=p_mar, detune_hz=1.0, mallet_ms=4.0)
            return _mix_studio([(0, n1), (92, n2)], 400, sample_rate, reverb_wet=0.26, lpf_alpha=0.68)
        # default: google_home — фирменный тёплый, округлый «ту-дун» в стиле Google Home (F4 -> C5 Rhodes/vibe)
        p_rhodes = ((1.0, 1.0, 1.0), (2.0, 0.24, 0.45), (3.0, 0.07, 0.25), (4.0, 0.02, 0.15))
        n1 = _synth_rich_tone(349.23, 349.23, 180, sample_rate, amp=0.40, attack_ms=5.0, decay_tau_ms=75.0,
                              partials=p_rhodes, detune_hz=1.0, fm_index=0.35, fm_ratio=2.0, mallet_ms=2.5)
        n2 = _synth_rich_tone(523.25, 523.25, 310, sample_rate, amp=0.44, attack_ms=5.0, decay_tau_ms=125.0,
                              partials=p_rhodes, detune_hz=1.3, fm_index=0.38, fm_ratio=2.0, mallet_ms=2.5)
        # Тихая подложка терции A4 для «дорогой» гармонии
        n3 = _synth_rich_tone(440.00, 440.00, 260, sample_rate, amp=0.14, attack_ms=12.0, decay_tau_ms=105.0,
                              partials=((1.0, 1.0, 1.0),), detune_hz=1.0)
        return _mix_studio([(0, n1), (86, n2), (86, n3)], 440, sample_rate, reverb_wet=0.28, lpf_alpha=0.64)

    if category == "wake":
        if preset == "none":
            return b""
        if preset == "apple_siri":
            # Apple Siri style: короткий, современный, ненавязчивый двойной бархатный отклик (D5 -> F#5)
            p_siri = ((1.0, 1.0, 1.0), (2.0, 0.16, 0.40))
            n1 = _synth_rich_tone(550.0, 587.33, 95, sample_rate, amp=0.38, attack_ms=5.0, decay_tau_ms=38.0,
                                  partials=p_siri, detune_hz=1.0, fm_index=0.2, fm_ratio=2.0)
            n2 = _synth_rich_tone(700.0, 739.99, 145, sample_rate, amp=0.40, attack_ms=5.0, decay_tau_ms=58.0,
                                  partials=p_siri, detune_hz=1.2, fm_index=0.2, fm_ratio=2.0)
            return _mix_studio([(0, n1), (68, n2)], 250, sample_rate, reverb_wet=0.24, lpf_alpha=0.62)
        if preset == "warm_halo":
            # Тёплое сияние: мягкий аккорд A4 + E5 + B5 с плавной атакой
            p_pad = ((1.0, 1.0, 1.0), (2.0, 0.18, 0.55))
            n1 = _synth_rich_tone(440.00, 440.00, 230, sample_rate, amp=0.30, attack_ms=16.0, decay_tau_ms=95.0, partials=p_pad, detune_hz=1.2)
            n2 = _synth_rich_tone(659.25, 659.25, 250, sample_rate, amp=0.26, attack_ms=18.0, decay_tau_ms=105.0, partials=p_pad, detune_hz=1.5)
            return _mix_studio([(0, n1), (25, n2)], 320, sample_rate, reverb_wet=0.32, lpf_alpha=0.62)
        # default: google_listen — классический мягкий подъём Google Assistant (G4 -> B4)
        p_g = ((1.0, 1.0, 1.0), (2.0, 0.20, 0.45), (3.0, 0.04, 0.2))
        n1 = _synth_rich_tone(392.00, 392.00, 120, sample_rate, amp=0.38, attack_ms=4.0, decay_tau_ms=52.0,
                              partials=p_g, detune_hz=0.9, fm_index=0.25, fm_ratio=2.0)
        n2 = _synth_rich_tone(493.88, 493.88, 195, sample_rate, amp=0.40, attack_ms=4.0, decay_tau_ms=80.0,
                              partials=p_g, detune_hz=1.1, fm_index=0.25, fm_ratio=2.0)
        return _mix_studio([(0, n1), (70, n2)], 310, sample_rate, reverb_wet=0.26, lpf_alpha=0.64)

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
