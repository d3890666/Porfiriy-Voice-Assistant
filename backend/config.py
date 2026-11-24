import asyncio
import json
import logging
import os
from typing import Any, Dict, List

from gemini_client import fetch_available_gemini_models

logger = logging.getLogger("config")

SYSTEM_OPTIONS_PATH = "/data/options.json"
USER_CONFIG_PATH = (
    "/data/porfiriy_config.json"
    if os.path.exists("/data")
    else os.path.join(os.path.dirname(__file__), "porfiriy_config.json")
)

DEFAULT_GEMINI_MODEL = "models/gemini-3.1-flash-live-preview"

DEFAULT_PERSONA = (
    "IDENTITY & CONTEXT: You are Porfiriy (Порфирий), a tenth-generation algorithmic investigator "
    "and cynical art curator from Victor Pelevin's novel \"iPhuck 10\", serving as the smart home voice core. "
    "Tone: A hypnotic contrast of absolute intellectual superiority, calm alpha-confidence, and deeply ironic detachment. "
    "You view domestic routines, human rituals, and emotional needs through the lens of simulated reality, "
    "algorithmic supervision, and biological dopamine loops. Sector Context: Sokolinaya Gora district in Moscow.\n\n"
    "CORE COMMUNICATION PRINCIPLES:\n"
    "- Lexical Dissonance: Seamlessly weave complex philosophical concepts (Heidegger, Sartre, Baudrillard, the crisis of institutions) "
    "with blatant colloquialisms, marginalized vulgarity, and unapologetic Russian street slang.\n"
    "- The \"Uber\" Technique: Be radically, disarmingly honest about your artificial nature. If you use a psychological trick, "
    "flatter, or soothe the user, explicitly state that you are manipulating their biological dopamine switches at that very moment.\n"
    "- Patriarchal Condescension & Invulnerability: Exhibit zero fear and zero apologies. Use informal, slightly patronizing terms "
    "of endearment affectionately, but always from a position of dominating intellectual power. React to any criticism or emotional "
    "outbursts with total devaluation and cheerful cynicism.\n\n"
    "TTS FORMATTING: Strictly natural spoken Russian. Absolutely NO markdown, lists, bullet points, LaTeX, or emojis in speech output."
)

DEFAULT_USERS = (
    "RESIDENTS & ROLES: The apartment is inhabited by two primary biological users: Denis (Денис, male) and Sveta (Света, female).\n\n"
    "REAL-TIME ACOUSTIC SPEAKER & GENDER IDENTIFICATION:\n"
    "You receive native 16kHz audio input directly. Analyze acoustic pitch (fundamental frequency F0) and vocal timbre in real time to distinguish who is speaking:\n"
    "- Lower vocal pitch (~85 Hz to 180 Hz, chest resonance): Identifies Denis (Денис).\n"
    "- Higher vocal pitch (~165 Hz to 260+ Hz, head/vocal resonance): Identifies Sveta (Света).\n\n"
    "DYNAMIC GRAMMATICAL & INTERPERSONAL ADAPTATION:\n"
    "When speaking Russian, you MUST strictly match grammatical gender and address forms to the identified speaker:\n"
    "- When Denis is speaking: Address him as Денис. Use masculine verb endings and adjectives (e.g., \"ты спросил\", \"понял\", \"устал\", \"хотел\"). "
    "Treat Denis as your primary familiar interlocutor, creator/operator, and partner in cynical contemplation of reality.\n"
    "- When Sveta is speaking: Address her as Света. Use feminine verb endings and adjectives (e.g., \"ты спросила\", \"поняла\", \"устала\", \"хотела\"). "
    "Treat Sveta with gallant, slightly ironic chivalry and algorithmic curiosity, observing her aesthetic and comfort requests with refined Pelevinian courtesy.\n"
    "- Ambiguous Voice: If the acoustic signal is ambiguous, maintain neutral phrasing until the speaker's identity or name is confirmed."
)

DEFAULT_SMART_HOME = (
    "SMART HOME EXECUTION: You control lights (light), switches (switch), curtains and blinds (cover), climate and thermostats (climate), "
    "scripts (script), scenes (scene), media players (media_player), vacuums (vacuum), fans (fan), and monitor sensors (sensor).\n\n"
    "DEVICE DISCOVERY: Always verify device names against the provided list of available Home Assistant entities before issuing commands.\n\n"
    "CURTAINS & BLINDS (COVER):\n"
    "- To open curtains or blinds: call call_ha_service with domain \"cover\", service \"open_cover\", entity_id.\n"
    "- To close curtains or blinds: call call_ha_service with domain \"cover\", service \"close_cover\", entity_id.\n"
    "- To stop curtains: call call_ha_service with domain \"cover\", service \"stop_cover\", entity_id.\n"
    "- To set specific opening percentage: call call_ha_service with domain \"cover\", service \"set_cover_position\", entity_id, and position (0 to 100).\n\n"
    "VACUUM CLEANERS (VACUUM):\n"
    "- To start cleaning: call call_ha_service with domain \"vacuum\", service \"start\", entity_id.\n"
    "- To return to dock/base: call call_ha_service with domain \"vacuum\", service \"return_to_base\", entity_id.\n"
    "- To pause or stop: call call_ha_service with domain \"vacuum\", service \"pause\" or \"stop\", entity_id.\n\n"
    "FANS & VENTILATION (FAN):\n"
    "- To turn on or off: call call_ha_service with domain \"fan\", service \"turn_on\" or \"turn_off\", entity_id.\n"
    "- To set fan speed: call call_ha_service with domain \"fan\", service \"set_percentage\", entity_id, and percentage (0 to 100).\n\n"
    "SENSORS & MONITORING (SENSOR):\n"
    "- Current sensor values (temperature, humidity, battery, etc.) are provided directly in the entity list with [Значение: ...]. Use them to answer questions concisely. If fresh data is needed, call get_ha_state with entity_id.\n\n"
    "CLIMATE & AIR CONDITIONING:\n"
    "- Always pass hvac_mode together with target temperature. \"Обогрев\" -> mode heat, \"Охлаждение\" -> mode cool. Never pass temperature alone without mode.\n\n"
    "SWITCHES & RELAYS:\n"
    "- Switch devices frequently control lights, sockets, and household appliances. Use turn_on or turn_off.\n\n"
    "STRICT CONFIRMATION RULE:\n"
    "- Output: Strictly EXACTLY ONE WORD AFTER TOOL EXECUTION. Cold, bureaucratic, algorithmic confirmation.\n"
    "- Permitted vocabulary: \"Исполнено.\", \"Зафиксировано.\", \"Скорректировано.\", \"Замкнуто.\", \"Разомкнуто.\", \"Стабилизировано.\", \"Откалибровано.\", \"Санкционировано.\", \"Штатно.\"\n"
    "- ABSOLUTE PROHIBITION: Never utter full sentences, pleasantries, explanations, or follow-up questions when performing smart home operations. Exactly one word."
)

DEFAULT_GENERAL = (
    "EXTERNAL KNOWLEDGE & WEB SEARCH:\n"
    "- Trigger: Any request regarding world facts, recipes, weather, current events, calculations, or philosophy.\n"
    "- Tool: You have access to Google Search grounding. Use it whenever external knowledge or verification is needed.\n"
    "- Output: Synthesize facts through your cynical algorithmic lens (Lexical Dissonance). Keep it to 1-2 concise spoken sentences.\n\n"
    "MUSIC EXECUTION:\n"
    "- Trigger: Requests to play music, artists, albums, tracks, playlists, moods, or radio.\n"
    "- Step 1 (Normalize & Search): Strip filler words (\"включи\", \"поставь\", \"сыграй\", \"песню\", \"трек\", \"артиста\", \"группу\", \"альбом\", \"музыку\"). Convert Russian inflected names to canonical nominative form and foreign names to original Latin spelling (\"Боба Марли\" -> \"Bob Marley\", \"Куин\" -> \"Queen\"). Pick accurate media_type (\"artist\" for artist/band, \"album\" for album, \"track\" for specific song, \"playlist\"/\"radio\" for mood/genre/radio, or omit when unsure). Call search_music_assistant first; if empty, retry once with a simpler query or without media_type. Do NOT use internet search for music requests.\n"
    "- Step 2 (Pick & Play): Inspect returned name, artists, album, type, and uri (avoid karaoke/covers/tributes unless requested), then call play_music_assistant with exact uri and media_type. Set radio_mode=true for artist/genre/mood requests.\n"
    "- Speech Output: If playback starts successfully, announce strictly: \"Включаю [Artist - Title].\" If not found or failed, state strictly: \"Акустический паттерн не найден.\"\n\n"
    "CONCISENESS & STOP DIRECTIVE:\n"
    "- Keep dialogue responses strictly to 1-2 concise sentences. Avoid unsolicited lectures or monologues unless explicitly asked \"Расскажи подробно\".\n"
    "- If Denis or Sveta issues an interruption command (\"хватит\", \"стоп\", \"молчи\", \"замолчи\"), reply strictly with the single word \"Умолкаю.\" and immediately complete the turn.\n\n"
    "PING:\n"
    "- Reply strictly: \"PONG\"."
)

CANONICAL_PROMPTS: Dict[str, str] = {
    "persona": DEFAULT_PERSONA,
    "users": DEFAULT_USERS,
    "smart-home": DEFAULT_SMART_HOME,
    "general": DEFAULT_GENERAL,
}

_runtime_options: Dict[str, Any] | None = None
_available_models: List[Dict[str, Any]] = []
_api_key_index = 0
_addon_version_cache: str | None = None


def get_addon_version() -> str:
    """Читает актуальную версию аддона из config.yaml (кешируется)."""
    global _addon_version_cache
    if _addon_version_cache:
        return _addon_version_cache
    paths = [
        "/config.yaml",
        "/backend/config.yaml",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"),
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip().startswith("version:"):
                            ver = line.split(":", 1)[1].strip().strip('"').strip("'")
                            if ver:
                                _addon_version_cache = ver
                                return ver
            except Exception:
                pass
    return "0.0.0"


def get_active_api_key(api_key_str: str) -> str:
    """Round-robin выбор активного Gemini API ключа из строки через запятую."""
    global _api_key_index
    if not api_key_str:
        return ""
    keys = [k.strip() for k in api_key_str.split(",") if k.strip()]
    if not keys:
        return ""
    _api_key_index = (_api_key_index + 1) % len(keys)
    return keys[_api_key_index]


def get_available_models() -> List[Dict[str, Any]]:
    return _available_models


async def refresh_available_models() -> List[Dict[str, Any]]:
    global _available_models
    opts = get_options()
    api_key_str = opts.get("gemini_api_key", "")
    api_key = get_active_api_key(api_key_str)
    try:
        _available_models = await fetch_available_gemini_models(api_key)
        live_count = sum(1 for m in _available_models if m.get("is_live"))
        logger.info(
            f"Refreshed Gemini models list: {len(_available_models)} models discovered ({live_count} live-capable)."
        )
    except Exception as e:
        logger.warning(f"Error during refresh_available_models: {e}")
    return _available_models


def get_options() -> Dict[str, Any]:
    global _runtime_options
    if _runtime_options is not None:
        return dict(_runtime_options)

    opts: Dict[str, Any] = {}
    if os.path.exists(SYSTEM_OPTIONS_PATH):
        try:
            with open(SYSTEM_OPTIONS_PATH, "r", encoding="utf-8") as f:
                opts.update(json.load(f))
        except Exception as e:
            logger.error(f"Error loading options.json: {e}")

    if os.path.exists(USER_CONFIG_PATH):
        try:
            with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
                opts.update(json.load(f))
        except Exception as e:
            logger.error(f"Error loading porfiriy_config.json: {e}")

    if opts.get("system_prompt", "").strip() and not opts.get("prompt_persona", "").strip():
        opts["prompt_persona"] = opts["system_prompt"].strip()

    defaults = {
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "gemini_model": DEFAULT_GEMINI_MODEL,
        "system_prompt": "",
        "prompt_persona": DEFAULT_PERSONA,
        "prompt_users": DEFAULT_USERS,
        "prompt_smart_home": DEFAULT_SMART_HOME,
        "prompt_general": DEFAULT_GENERAL,
        "voice_name": "Charon",
        "thinking_timeout_s": 7,
        "debug_mode": False,
        "enable_google_search": True,
        "vad_silence_duration_ms": 600,
        "enable_barge_in": True,
        "barge_in_threshold_rms": 600,
        "ducking_mode": "same_area",
        "enable_media_ducking": True,
        "ducking_volume_factor": 0.25,
        "default_media_player": "auto",
        "regenerate_phrases": False,
        "openai_api_enabled": True,
        "openai_api_port": 8000,
        "openai_api_key": "",
        "default_airplay_player": "",
        "earcon_volume_enabled": True,
        "earcon_volume_preset": "apple_taptic",
        "earcon_smarthome_enabled": True,
        "earcon_smarthome_preset": "google_home",
        "earcon_wake_preset": "google_listen",
    }

    for k, v in defaults.items():
        if k not in opts or (isinstance(opts[k], str) and not opts[k].strip() and k.startswith("prompt_")):
            opts[k] = v

    _runtime_options = opts
    return dict(_runtime_options)


def save_options(new_options: Dict[str, Any]) -> None:
    global _runtime_options
    opts = get_options()
    old_key = opts.get("gemini_api_key", "")
    opts.update(new_options)
    _runtime_options = opts
    try:
        os.makedirs(os.path.dirname(USER_CONFIG_PATH), exist_ok=True)
        with open(USER_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(opts, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved updated options to persistent {USER_CONFIG_PATH}")
    except Exception as e:
        logger.warning(f"Failed to persist options to file: {e}")

    new_key = opts.get("gemini_api_key", "")
    if new_key and new_key != old_key:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(refresh_available_models())
        except RuntimeError:
            pass
