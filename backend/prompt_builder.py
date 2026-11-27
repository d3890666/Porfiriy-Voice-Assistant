import datetime
import logging
from typing import Any, Dict, List, Optional

try:
    from .db import db_singleton
    from .plugin_registry import plugin_registry_singleton
    from .quiet_mode import quiet_mode_singleton
except ImportError:
    try:
        from db import db_singleton
        from plugin_registry import plugin_registry_singleton
        from quiet_mode import quiet_mode_singleton
    except ImportError:
        db_singleton = None
        plugin_registry_singleton = None
        quiet_mode_singleton = None

logger = logging.getLogger("prompt_builder")


def resolve_target_player(dev: Optional[Dict[str, Any]], options: Dict[str, Any]) -> str:
    """Возвращает entity_id медиаплеера из карточки устройства или глобальных настроек."""
    dev_cfg = (dev or {}).get("config", {}) if isinstance(dev, dict) else {}
    return (
        dev_cfg.get("mass_player")
        or dev_cfg.get("airplay_player")
        or dev_cfg.get("response_player")
        or options.get("default_mass_player")
        or options.get("default_airplay_player")
        or options.get("default_media_player")
        or ""
    ).strip()


def format_dialogue_context_block(
    messages: List[Dict[str, Any]],
    *,
    header: str = "### PRIOR DIALOGUE CONTEXT:",
    include_sender: bool = False,
    max_chars_per_msg: int = 1200,
) -> str:
    """Форматирует историю диалога как текстовый fallback-блок для промпта."""
    if not messages:
        return ""
    lines = [
        header,
        "Note: Treat this strictly as prior context. If the user's new request is about a completely different topic, answer the new request directly without forcing the old topic.",
    ]
    for m in messages:
        role = "model" if str(m.get("role") or "").lower() in ("model", "assistant") else "user"
        txt = str(m.get("text") or "").strip()[:max_chars_per_msg]
        if not txt:
            continue
        if role == "model":
            lines.append(f"- Porfiriy: {txt}")
        else:
            sname = str(m.get("sender_name") or "").strip()
            prefix = sname if (include_sender and sname) else "User"
            lines.append(f"- {prefix}: {txt}")
    return "\n".join(lines) if len(lines) > 2 else ""


def build_live_history_turns(
    messages: List[Dict[str, Any]],
    current_turn: Any,
    *,
    include_sender: bool = False,
    max_chars_per_msg: int = 1200,
) -> List[Any]:
    """Собирает нативный список turns для Gemini Live send_client_content с чередованием user/model."""
    if not messages:
        return [current_turn]
    try:
        from google.genai import types as _types
    except ImportError:
        return [current_turn]

    merged: List[tuple[str, str]] = []
    for m in messages:
        role = "model" if str(m.get("role") or "").lower() in ("model", "assistant") else "user"
        txt = str(m.get("text") or "").strip()[:max_chars_per_msg]
        if not txt:
            continue
        if role == "user" and include_sender and m.get("sender_name"):
            txt = f"[{str(m['sender_name']).strip()}]: {txt}"
        if merged and merged[-1][0] == role:
            merged[-1] = (role, merged[-1][1] + "\n" + txt)
        else:
            merged.append((role, txt))

    if not merged:
        return [current_turn]

    if merged[0][0] == "model":
        merged.insert(0, ("user", "[Контекст предыдущего сообщения]"))
    if merged[-1][0] == "user":
        merged.append(("model", "Принято."))

    turns = [
        _types.Content(role=r, parts=[_types.Part.from_text(text=t)])
        for r, t in merged
    ]
    turns.append(current_turn)
    return turns


async def build_system_prompt(
    options: Dict[str, Any],
    ha_api: Any,
    *,
    dev: Optional[Dict[str, Any]] = None,
    area_name: Optional[str] = None,
    mode: str = "satellite",
    extra_context: str = "",
) -> str:
    """Единая сборка системного промпта Порфирия для всех клиентов (ESP32, PC Streamer, Яндекс Станция)."""
    persona = options.get("prompt_persona", "") or options.get("system_prompt", "")
    db_users = ""
    if db_singleton:
        try:
            db_users = db_singleton.build_users_prompt_section()
        except Exception as e:
            logger.warning(f"Failed to build users prompt from DB: {e}")
    users = db_users or options.get("prompt_users", "")
    smart_home = options.get("prompt_smart_home", "")
    general = options.get("prompt_general", "")
    resolved_area = area_name or ((dev or {}).get("area_name") if isinstance(dev, dict) else None) or "Не указана"
    target_player = resolve_target_player(dev, options)

    if mode == "station_openai":
        entities_prompt = ""
        if ha_api:
            try:
                entities_prompt = await ha_api.get_entities_prompt()
            except Exception as e:
                logger.warning(f"Failed to fetch entities prompt: {e}")

        parts = [p for p in (persona, users, smart_home, general) if p and p.strip()]
        if entities_prompt and entities_prompt.strip():
            parts.append(entities_prompt.strip())

        parts.append(
            f"### HARDWARE & SATELLITE CONTEXT:\n"
            f"- Device Type: Yandex Station 2 (Quasar Bridge integration).\n"
            f"- Room / Area: {resolved_area}.\n"
            f"- Target Music Player: {target_player or 'Not configured'}.\n"
            f"- Strict Speech Directive: You are talking to a voice synthesizer (Alice TTS). "
            f"Output strictly clean, spoken Russian without markdown, bullets, asterisks, or emojis. "
            f"Limit response strictly to 1-2 concise spoken sentences."
        )
        parts.append(
            f"### MUSIC MANAGEMENT (MUSIC ASSISTANT):\n"
            f"- Target player: '{target_player}'.\n"
            f"- Step 1 (Normalize & Search): Strip filler words ('включи', 'поставь', 'сыграй', 'песню', 'трек', 'артиста', 'группу', 'альбом', 'музыку'). "
            f"Convert Russian inflected cases to canonical nominative form and foreign names to original Latin spelling "
            f"('Боба Марли' -> 'Bob Marley', 'Куин' -> 'Queen', 'Нирвану' -> 'Nirvana', 'Пинк Флойд' -> 'Pink Floyd'). "
            f"When user specifies both artist and album/track (e.g. 'альбом Сила ума группы Кирпичи'), put ONLY the title in name ('Сила ума') and put the band in artist ('Кирпичи'). "
            f"Pick accurate media_type: 'artist' for artist/band, 'album' for album, 'track' for specific song, 'playlist'/'radio' for mood/genre/radio (or omit if unsure). "
            f"Always call search_music_assistant first. If empty, retry once with a simpler query or without media_type.\n"
            f"- Step 2 (Pick & Play): Inspect returned name, artists, album, type, and uri (avoid karaoke/covers/tributes unless asked). "
            f"Call play_music_assistant with the exact uri, media_type, player='{target_player}', and radio_mode=true for artist/genre/mood requests.\n"
            f"- Speech Output: Reply strictly 'Включаю [Artist - Title].' on success, or 'Акустический паттерн не найден.' if nothing matched."
        )
    else:
        modular_parts = []
        for key, title in (
            ("prompt_persona", "PERSONA & CONTEXT"),
            ("prompt_users", "USERS & ACOUSTIC IDENTIFICATION"),
            ("prompt_smart_home", "SMART HOME EXECUTION RULES"),
            ("prompt_general", "GENERAL DIALOGUE, SEARCH & MEDIA"),
        ):
            val = db_users if (key == "prompt_users" and db_users) else options.get(key, "")
            if val and val.strip():
                modular_parts.append(f"### {title}\n{val.strip()}")

        prompt_base = "\n\n".join(modular_parts) or (options.get("system_prompt", "") or "").strip()
        anti_hallucination = (
            "CRITICAL DIRECTIVE: NEVER fabricate or claim you performed a smart home action, set a timer/alarm, "
            "created a reminder, or scheduled a background watcher unless you have explicitly called the corresponding tool "
            "(e.g. call_ha_service, schedule_create, schedule_manage) and received status='success'/'scheduled'/'ok'. "
            "Devices under domain 'switch' can also control lights or appliances; use them when appropriate."
        )

        devices_text = ""
        data_actions_text = ""
        if ha_api:
            try:
                devices_text = await ha_api.get_filtered_entities()
                data_actions_text = await ha_api.get_data_actions_prompt()
            except Exception as e:
                logger.warning(f"Failed to fetch HA entities/actions for prompt: {e}")

        parts = [p for p in (prompt_base, anti_hallucination) if p]
        if devices_text:
            parts.append(f"Available Home Assistant devices:\n{devices_text}")
        if data_actions_text:
            parts.append(data_actions_text)
        if area_name:
            parts.append(
                f"CURRENT ACOUSTIC LOCATION: The user is speaking through the device in room '{area_name}'. "
                f"When handling ambiguous smart home requests (e.g. 'turn on light', 'close curtains'), "
                f"ALWAYS prioritize devices located in '{area_name}'."
            )

    if plugin_registry_singleton:
        try:
            plugin_sections = plugin_registry_singleton.get_active_prompt_sections(options)
            if plugin_sections and plugin_sections.strip():
                parts.append(plugin_sections.strip())
        except Exception as e:
            logger.warning(f"Failed to build plugin prompt sections: {e}")

    if db_singleton:
        try:
            recent_rich = db_singleton.list_recent_rich_results(limit=5, ttl_hours=72)
            if recent_rich:
                local_tz = datetime.datetime.now().astimezone().tzinfo
                rich_lines = [
                    "### RECENT CACHED SEARCH / RECIPE / MARKETPLACE RESULTS (last 72h):",
                    "IMPORTANT: The items below are ALREADY saved in cache with full formatted HTML, numbers, and clickable links.",
                    "If the user asks to send/drop the last search result or any of these topics to Telegram ('скинь это в телегу', 'отправь про ... в телегу', 'пришли вчерашний поиск'), DO NOT call `web_search` again — immediately call `send_to_telegram(target=..., use_last_result=True, result_query='<topic>')`.",
                ]
                for item in recent_rich:
                    ts_str = ""
                    try:
                        dt = datetime.datetime.fromisoformat(item["created_at"]).astimezone(local_tz)
                        ts_str = dt.strftime("%d.%m %H:%M")
                    except Exception:
                        ts_str = str(item.get("created_at") or "")[:16]
                    topic = (item.get("topic") or item.get("query") or "").strip()[:100]
                    kind = item.get("kind") or "web_search"
                    if topic:
                        rich_lines.append(f"- [{ts_str}] ({kind}) {topic}")
                parts.append("\n".join(rich_lines))
        except Exception as e:
            logger.warning(f"Failed to build recent rich_results prompt section: {e}")

    if extra_context and extra_context.strip():
        parts.append(extra_context.strip())

    parts.append(
        "### PERSISTENT SCHEDULER & AUTOMATIONS (schedule_create, schedule_list, schedule_manage):\n"
        "- Use `schedule_create` whenever the user asks for countdown timers ('поставь таймер на 5 минут' -> `direct_tool` + `alarm_chime`), "
        "delayed Telegram notifications ('через 10 секунд напиши в телегу...' -> `direct_tool` + `send_to_telegram`), "
        "delayed device actions (`direct_tool` + `call_ha_service`), recurring price/release watchers or daily briefings (`ai_prompt`), "
        "or Home Assistant event triggers ('когда Денис вернётся домой и не уйдёт 5 минут' -> `ha_event` with `ha_tracker`, `to_state='home'`, `for_seconds=300`).\n"
        "- Use `schedule_list` and `schedule_manage` to check, pause, resume, run, or cancel timers and scheduled tasks."
    )

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S (%A)")
    parts.append(f"### CURRENT LOCAL DATE & TIME:\n{now_str}")

    if quiet_mode_singleton and quiet_mode_singleton.is_active(options):
        parts.append(
            "### QUIET NIGHT MODE (ACTIVE NOW):\n"
            "- Night quiet mode is currently active (people may be asleep; accidental false wake triggers are possible).\n"
            "- If the audio contains ONLY background noise, breathing, coughing, snoring, silence, or unintelligible fragments with NO clear Russian request or smart-home command, "
            "output strictly `[SILENCE]` and nothing else.\n"
            "- If there IS a clear request (e.g. 'подними шторы', 'сколько времени', 'выключи розетку'), execute it immediately, calmly, and concisely."
        )

    return "\n\n".join(parts)
