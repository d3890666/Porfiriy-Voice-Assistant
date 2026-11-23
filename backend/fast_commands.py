import asyncio
import logging
import re
from typing import Any, Dict, Optional

from net_utils import get_device_ip

logger = logging.getLogger("fast_commands")


async def handle_direct_playback_command(
    user_text: str,
    music_player: str,
    *,
    ha_api: Any,
    quasar_manager: Any = None,
    dev: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Быстрый перехват команд управления плеером (пауза, стоп, дальше, продолжить)."""
    speaker_ip = get_device_ip(dev)
    if not music_player and not speaker_ip:
        return None

    text_lower = user_text.lower().strip().rstrip(".!?")

    # 1. Пауза
    is_pause = (
        text_lower in (
            "пауза", "поставь на паузу", "на паузу", "сделай паузу",
            "паузу", "замри", "приостанови", "пауза музыка",
        )
        or bool(re.search(r"\b(пауз[ауе]|приостанов\w*)\b", text_lower))
    )
    if is_pause:
        if speaker_ip and quasar_manager:
            asyncio.create_task(quasar_manager.stop_station_airplay_if_playing(speaker_ip))
        if music_player and ha_api:
            await ha_api.call_service("media_player", "media_play_pause", {"entity_id": music_player})
        return "Ставлю на паузу."

    # 2. Стоп
    is_stop = (
        text_lower in (
            "стоп", "останови музыку", "выключи музыку", "хватит",
            "замолчи", "останови", "стоп музыка", "выруби музыку",
        )
        or bool(re.search(r"^(стоп|останови(\s+музыку)?|выключи\s+музыку|хватит|замолчи|выруби\s+музыку)$", text_lower))
    )
    if is_stop:
        if speaker_ip and quasar_manager:
            asyncio.create_task(quasar_manager.stop_station_airplay_if_playing(speaker_ip))
        if music_player and ha_api:
            await ha_api.call_service("media_player", "media_stop", {"entity_id": music_player})
        return "Останавливаю воспроизведение."

    # 3. Продолжить
    is_resume = (
        text_lower in (
            "продолжи", "возобнови", "возобнови музыку", "играй дальше",
            "сними с паузы", "продолжить", "играй", "музыку", "включи музыку",
            "запусти музыку", "нажми на play", "нажми play", "нажми плей", "плей",
        )
        or bool(re.search(r"^(продолжи|возобнови|сними\s+с\s+паузы|нажми\s+(на\s+)?(play|плей)|играй(\s+дальше)?)$", text_lower))
    )
    if is_resume:
        if speaker_ip and quasar_manager:
            asyncio.create_task(quasar_manager.stop_station_airplay_if_playing(speaker_ip))
        if music_player and ha_api:
            await ha_api.call_service("media_player", "media_play", {"entity_id": music_player})
        return "Продолжаю воспроизведение."

    # 4. Следующий трек
    is_next = (
        text_lower in (
            "следующий трек", "следующую", "следующий", "следующая",
            "дальше", "дальше трек", "переключи трек", "переключи",
            "пропусти", "скип", "вперед",
        )
        or bool(re.search(r"^(следующ\w*(\s+трек)?|дальше|переключи(\s+трек)?|пропусти|скип|вперед)$", text_lower))
    )
    if is_next:
        if music_player and ha_api:
            await ha_api.call_service("media_player", "media_next_track", {"entity_id": music_player})
        return "Включаю следующий трек."

    # 5. Предыдущий трек
    is_prev = (
        text_lower in ("предыдущий трек", "предыдущую", "предыдущий", "назад", "прошлый трек")
        or bool(re.search(r"^(предыдущ\w*(\s+трек)?|назад|прошлый\s+трек)$", text_lower))
    )
    if is_prev:
        if music_player and ha_api:
            await ha_api.call_service("media_player", "media_previous_track", {"entity_id": music_player})
        return "Возвращаю предыдущий трек."

    return None


async def handle_direct_volume_command(
    user_text: str,
    mac: str,
    music_player: str,
    *,
    ha_api: Any,
    device_manager: Any,
    ducking_manager: Any = None,
    quasar_manager: Any = None,
) -> Optional[bool]:
    """
    Мгновенно и бесшумно меняет громкость привязанного плеера Music Assistant через Home Assistant
    и синхронизирует аппаратный ALSA Master на Яндекс Станции.
    """
    if not music_player or not ha_api:
        return None

    t = re.sub(r"[.,!?]+$", "", user_text.lower().strip())
    new_vol: Optional[float] = None
    delta: Optional[float] = None

    m_abs = re.match(
        r"^(?:поставь\s+|установи\s+|сделай\s+|включи\s+)?(?:громкость(?:\s+голоса)?|звук|голос)(?:\s+на)?\s+(\d{1,3})\s*(%|процент\w*)?$",
        t,
    )
    if m_abs:
        val = int(m_abs.group(1))
        has_pct = bool(m_abs.group(2))
        if has_pct or val > 10:
            new_vol = max(0.0, min(1.0, round(val / 100.0, 2)))
        else:
            new_vol = max(0.0, min(1.0, round(val / 10.0, 2)))
    elif t in ("выключи звук", "без звука", "громкость ноль", "громкость на ноль", "звук ноль", "громкость минимум", "голос на ноль"):
        new_vol = 0.0
    elif t in ("громкость максимум", "громкость на максимум", "звук максимум", "максимальная громкость", "голос на максимум"):
        new_vol = 1.0
    elif re.match(r"^(?:сделай\s+|чуть\s+|немного\s+)?(?:погромче|громче|прибавь(?:\s+громкость|\s+звук|\s+голос)?|голос\s+погромче|говори\s+громче)$", t):
        delta = 0.10
    elif re.match(r"^(?:сделай\s+|чуть\s+|немного\s+)?(?:потише|тише|тихо|убавь(?:\s+громкость|\s+звук|\s+голос)?|голос\s+потише|говори\s+потише)$", t):
        delta = -0.10

    if new_vol is None and delta is None:
        return None

    if delta is not None:
        base_vol = ducking_manager.get_saved_volume(mac) if ducking_manager else None
        if base_vol is None:
            st = await ha_api.get_entity_state(music_player)
            curr = st.get("attributes", {}).get("volume_level") if isinstance(st, dict) else None
            base_vol = float(curr) if curr is not None else 0.5
        new_vol = max(0.0, min(1.0, round(base_vol + delta, 2)))

    logger.info(f"[OPENAI VOLUME] Setting {music_player} volume to {new_vol} (mac={mac}) silently")
    if ducking_manager:
        ducking_manager.set_explicit_volume(mac, new_vol)

    await ha_api.call_service("media_player", "volume_set", {
        "entity_id": music_player,
        "volume_level": new_vol,
    })
    try:
        await device_manager.update_device_config(mac, {"speaker_volume": new_vol})
    except Exception:
        pass

    if quasar_manager:
        try:
            dev = device_manager.get_device(mac)
            if dev and dev.get("device_type") == "yandex_station":
                quasar_manager.set_station_hw_volume(dev, int(round(new_vol * 100)))
        except Exception as e_vol:
            logger.warning(f"[OPENAI VOLUME] Hardware volume sync note: {e_vol}")

    return True
