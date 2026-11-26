import logging
from typing import List, Dict, Any
import aiohttp
from google import genai
from google.genai import types

try:
    from .plugin_registry import plugin_registry_singleton
except ImportError:
    try:
        from plugin_registry import plugin_registry_singleton
    except ImportError:
        plugin_registry_singleton = None

logger = logging.getLogger(__name__)


def _json_schema_to_genai(schema_dict: Dict[str, Any], root_defs: Dict[str, Any] | None = None) -> types.Schema:
    """Конвертирует JSON Schema словарь (в т.ч. FastMCP/Pydantic anyOf/$ref) в types.Schema для FunctionDeclaration."""
    if not isinstance(schema_dict, dict):
        return types.Schema(type=types.Type.STRING)

    if root_defs is None:
        defs = {}
        if isinstance(schema_dict.get("$defs"), dict):
            defs.update(schema_dict["$defs"])
        if isinstance(schema_dict.get("definitions"), dict):
            defs.update(schema_dict["definitions"])
        root_defs = defs

    # Разворачиваем $ref
    if "$ref" in schema_dict and isinstance(schema_dict["$ref"], str):
        ref_name = schema_dict["$ref"].rsplit("/", 1)[-1]
        if ref_name in root_defs and isinstance(root_defs[ref_name], dict):
            merged = dict(root_defs[ref_name])
            if schema_dict.get("description") and not merged.get("description"):
                merged["description"] = schema_dict["description"]
            return _json_schema_to_genai(merged, root_defs)

    # Разворачиваем anyOf / oneOf (например, Optional[int] -> [{"type": "integer"}, {"type": "null"}])
    for union_key in ("anyOf", "oneOf"):
        variants = schema_dict.get(union_key)
        if isinstance(variants, list) and variants:
            non_null = [v for v in variants if isinstance(v, dict) and str(v.get("type") or "").lower() != "null"]
            chosen = dict(non_null[0] if non_null else variants[0])
            if schema_dict.get("description") and not chosen.get("description"):
                chosen["description"] = schema_dict["description"]
            return _json_schema_to_genai(chosen, root_defs)

    type_map = {
        "OBJECT": types.Type.OBJECT,
        "STRING": types.Type.STRING,
        "INTEGER": types.Type.INTEGER,
        "NUMBER": types.Type.NUMBER,
        "BOOLEAN": types.Type.BOOLEAN,
        "ARRAY": types.Type.ARRAY,
    }
    raw_type_val = schema_dict.get("type")
    if isinstance(raw_type_val, list):
        non_null_t = [str(t).upper() for t in raw_type_val if str(t).lower() != "null"]
        raw_type = non_null_t[0] if non_null_t else "STRING"
    elif raw_type_val:
        raw_type = str(raw_type_val).upper()
    elif isinstance(schema_dict.get("properties"), dict):
        raw_type = "OBJECT"
    else:
        raw_type = "STRING"

    g_type = type_map.get(raw_type, types.Type.STRING)
    kwargs: Dict[str, Any] = {"type": g_type}
    if schema_dict.get("description"):
        kwargs["description"] = str(schema_dict["description"])
    if schema_dict.get("enum"):
        kwargs["enum"] = [str(x) for x in schema_dict["enum"] if x is not None]
    if raw_type == "OBJECT" and isinstance(schema_dict.get("properties"), dict):
        props = {
            k: _json_schema_to_genai(v, root_defs)
            for k, v in schema_dict["properties"].items()
            if isinstance(v, dict)
        }
        kwargs["properties"] = props
        if schema_dict.get("required"):
            req = [r for r in schema_dict["required"] if r in props]
            if req:
                kwargs["required"] = req
    if raw_type == "ARRAY":
        if isinstance(schema_dict.get("items"), dict):
            kwargs["items"] = _json_schema_to_genai(schema_dict["items"], root_defs)
        else:
            kwargs["items"] = types.Schema(type=types.Type.STRING)
    return types.Schema(**kwargs)

DEFAULT_LIVE_MODELS: List[Dict[str, Any]] = [
    {"id": "models/gemini-3.1-flash-live-preview", "name": "Gemini 3.1 Flash Live Preview (Новейшая)", "is_live": True},
    {"id": "models/gemini-2.5-flash-native-audio-preview", "name": "Gemini 2.5 Flash Native Audio Preview", "is_live": True},
    {"id": "models/gemini-2.0-flash-exp", "name": "Gemini 2.0 Flash Experimental", "is_live": True},
]

async def fetch_available_gemini_models(api_key: str) -> List[Dict[str, Any]]:
    """
    Получает список доступных моделей Gemini из Google API.
    Сначала пробует официальный genai SDK, при сбое - прямой REST через aiohttp.
    Возвращает упорядоченный список словарей с метаданными и флагом is_live.
    """
    if not api_key:
        return list(DEFAULT_LIVE_MODELS)

    models_dict = {}
    
    # 1. Запрос через google.genai SDK
    try:
        client = genai.Client(
            http_options={"api_version": "v1alpha"},
            api_key=api_key
        )
        pager = await client.aio.models.list(config={"page_size": 100})
        async for m in pager:
            m_id = getattr(m, "name", "")
            if not m_id:
                continue
            display_name = getattr(m, "display_name", "") or m_id
            actions = getattr(m, "supported_actions", []) or []
            
            is_live = False
            if "bidiGenerateContent" in actions:
                is_live = True
            elif any(tag in m_id.lower() for tag in ["live", "native-audio", "flash-exp"]):
                is_live = True
                
            models_dict[m_id] = {
                "id": m_id,
                "name": display_name,
                "is_live": is_live
            }
    except Exception as e:
        logger.warning(f"Failed to fetch models via genai SDK: {e}. Trying REST fallback...")
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("models", []):
                            m_id = item.get("name", "")
                            if not m_id:
                                continue
                            display_name = item.get("displayName") or m_id
                            methods = item.get("supportedGenerationMethods", [])
                            is_live = ("bidiGenerateContent" in methods) or any(tag in m_id.lower() for tag in ["live", "native-audio", "flash-exp"])
                            models_dict[m_id] = {
                                "id": m_id,
                                "name": display_name,
                                "is_live": is_live
                            }
                    else:
                        logger.warning(f"REST models request returned status {resp.status}")
        except Exception as re:
            logger.warning(f"Failed to fetch models via REST: {re}")

    # Дополняем эталонными Live моделями, если их нет
    for dm in DEFAULT_LIVE_MODELS:
        if dm["id"] not in models_dict:
            models_dict[dm["id"]] = dm

    # Разделяем и сортируем: сначала live-модели, затем остальные
    live_models = [m for m in models_dict.values() if m.get("is_live")]
    other_models = [m for m in models_dict.values() if not m.get("is_live")]

    live_models.sort(key=lambda x: x["name"])
    other_models.sort(key=lambda x: x["name"])

    return live_models + other_models

class GeminiProxyClient:
    def __init__(self, api_key: str, system_prompt: str, ha_api=None, voice_name: str = "Zephyr", model: str = "models/gemini-3.1-flash-live-preview", enable_google_search: bool = True, vad_silence_duration_ms: int = 600, response_modalities: List[str] = None, manual_vad: bool = False, enable_tools: bool = True):
        """
        Инициализация клиента Gemini Live API с инструментами управления Home Assistant.
        """
        if not api_key:
            raise ValueError("Gemini API key is not configured! Please open Porfiriy Web UI ('Мозг & Личность') and enter your API key.")
            
        api_ver = "v1beta" if manual_vad else "v1alpha"
        self.client = genai.Client(
            http_options={"api_version": api_ver},
            api_key=api_key
        )
        
        # Если пользователь не указал префикс models/, добавляем его
        self.model = model if model.startswith("models/") else f"models/{model}"
        
        self.system_prompt = system_prompt
        self.voice_name = voice_name
        self.enable_google_search = enable_google_search
        self.vad_silence_duration_ms = vad_silence_duration_ms
        self.response_modalities = response_modalities or ["AUDIO"]
        self.manual_vad = manual_vad
        self.enable_tools = enable_tools

    def _get_config(self) -> types.LiveConnectConfig:
        """Настройка конфигурации сессии (Промпт, Голос, Инструменты)."""
        tool_extra = {}
        if self.manual_vad and "gemini-3.8" in (self.model or "").lower():
            if hasattr(types, "Behavior") and hasattr(types.Behavior, "BLOCKING"):
                tool_extra["behavior"] = types.Behavior.BLOCKING
            else:
                tool_extra["behavior"] = "BLOCKING"

        def _make_decl(**kwargs):
            if tool_extra:
                try:
                    return types.FunctionDeclaration(**kwargs, **tool_extra)
                except Exception:
                    pass
            return types.FunctionDeclaration(**kwargs)

        ha_tool = _make_decl(
            name="call_ha_service",
            description=(
                "Call a Home Assistant service or action to control smart home devices, run scripts/scenes, "
                "or query live data actions (e.g. transport arrivals moscow_transport.get_arrivals, calendar events, weather forecasts)."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "domain": types.Schema(type=types.Type.STRING, description="The domain of the service or action, e.g. light, switch, cover, script, scene, climate, media_player, vacuum, fan, moscow_transport"),
                    "service": types.Schema(type=types.Type.STRING, description="The service or action to call, e.g. turn_on, turn_off, toggle, open_cover, close_cover, stop_cover, set_cover_position, start, pause, stop, return_to_base, set_percentage, get_arrivals"),
                    "entity_id": types.Schema(type=types.Type.STRING, description="Optional entity_id of the device from the provided context (e.g. light.kitchen, cover.living_room_curtains). Omit for services that do not use entity_id."),
                    "parameters": types.Schema(type=types.Type.OBJECT, description="Optional key-value arguments for actions (e.g. {'stop_id': '9600123'} for transport arrivals, or extra action-specific parameters)."),
                    "position": types.Schema(type=types.Type.INTEGER, description="Optional target position for cover (curtains/blinds) from 0 (closed) to 100 (open)."),
                    "temperature": types.Schema(type=types.Type.NUMBER, description="Optional target temperature for climate devices."),
                    "hvac_mode": types.Schema(type=types.Type.STRING, description="Optional HVAC mode for climate devices (e.g. heat, cool, off)."),
                    "percentage": types.Schema(type=types.Type.INTEGER, description="Optional fan speed percentage (0-100) for fan devices."),
                },
                required=["domain", "service"]
            )
        )
        get_state_tool = _make_decl(
            name="get_ha_state",
            description="Get current state, sensor value (temperature, humidity, battery, etc.) or attributes of any Home Assistant entity.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "entity_id": types.Schema(type=types.Type.STRING, description="The entity_id to inspect, e.g. sensor.living_room_temp, vacuum.robot, climate.thermostat"),
                },
                required=["entity_id"]
            )
        )
        search_music_tool = _make_decl(
            name="search_music_assistant",
            description=(
                "Search Music Assistant catalog for artists, albums, tracks, playlists, or radio. "
                "Always normalize the query: remove filler words ('включи', 'поставь', 'песню', 'трек', 'артиста', 'группу', 'альбом', 'музыку'), "
                "use nominative case, and prefer original Latin spelling for foreign names (e.g. 'Боба Марли' -> 'Bob Marley', 'Куин' -> 'Queen'). "
                "Returns items with name, artists, album, type, and uri."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "name": types.Schema(
                        type=types.Type.STRING,
                        description="Clean track, album, or artist title in canonical form without filler words (e.g. 'Сила ума', 'Bob Marley', 'Bohemian Rhapsody')."
                    ),
                    "artist": types.Schema(
                        type=types.Type.STRING,
                        description="Optional artist/band name when searching for a specific album or track (e.g. 'Кирпичи', 'Queen')."
                    ),
                    "media_type": types.Schema(
                        type=types.Type.STRING,
                        description="Optional type filter: 'artist' (when user asks for an artist/band), 'album', 'track' (for a specific song), 'playlist', or 'radio'. Omit when unsure to search all types."
                    ),
                },
                required=["name"]
            )
        )
        
        play_music_tool = _make_decl(
            name="play_music_assistant",
            description="Play a music URI obtained from search_music_assistant on the smart speaker.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "uri": types.Schema(type=types.Type.STRING, description="Exact URI of the chosen item from search_music_assistant"),
                    "media_type": types.Schema(type=types.Type.STRING, description="Optional media type of the URI: 'artist', 'album', 'track', 'playlist', or 'radio'"),
                    "radio_mode": types.Schema(type=types.Type.BOOLEAN, description="Optional. Set true for artist/genre/mood requests to play similar music continuously."),
                    "player": types.Schema(type=types.Type.STRING, description="Optional target media_player entity_id. Leave empty to use the speaker's default player."),
                },
                required=["uri"]
            )
        )

        schedule_action_tool = _make_decl(
            name="schedule_ha_action",
            description=(
                "Schedule a delayed Home Assistant action or countdown timer (e.g. 'выключи свет через 5 минут', "
                "'включи пончик через 30 секунд', 'выключи музыку через полчаса', 'поставь таймер на 3 минуты'). "
                "For delayed device control, pass domain, service, entity_id, and delay_seconds. "
                "For a plain countdown alarm/timer with a chime, pass domain='alarm', service='ring', delay_seconds, and label."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "delay_seconds": types.Schema(type=types.Type.INTEGER, description="Delay in seconds before executing (e.g. 30, 300, 1800, 3600)."),
                    "domain": types.Schema(type=types.Type.STRING, description="HA domain (light, switch, cover, media_player, fan, climate, script, scene) or 'alarm' for a countdown chime."),
                    "service": types.Schema(type=types.Type.STRING, description="HA service (turn_on, turn_off, toggle, media_pause, close_cover, etc.) or 'ring' for alarm."),
                    "entity_id": types.Schema(type=types.Type.STRING, description="Target HA entity_id (required for device control, omit for plain alarm)."),
                    "parameters": types.Schema(type=types.Type.OBJECT, description="Optional extra HA service parameters."),
                    "label": types.Schema(type=types.Type.STRING, description="Short Russian description of the timer (e.g. 'выключить свет на кухне' or 'макароны')."),
                },
                required=["delay_seconds", "domain", "service"]
            )
        )

        manage_timers_tool = _make_decl(
            name="manage_timers",
            description="List active scheduled actions/timers, cancel a specific timer, or cancel all timers ('какие таймеры стоят', 'отмени таймер', 'отмени все таймеры').",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "action": types.Schema(type=types.Type.STRING, description="Action: 'list', 'cancel', or 'cancel_all'."),
                    "query": types.Schema(type=types.Type.STRING, description="Optional timer_id or Russian keyword (e.g. 'кухн', 'свет', 't1') when action is 'cancel'."),
                },
                required=["action"]
            )
        )

        base_decls = [ha_tool, get_state_tool, search_music_tool, play_music_tool, schedule_action_tool, manage_timers_tool]
        declared_names = {d.name for d in base_decls}

        # Явный инструмент делегирования в пул больших моделей и/или быстрый веб-поиск
        try:
            from config import get_options as _get_opts
            _opts = _get_opts()
            if _opts.get("big_models_enabled"):
                big_tool = _make_decl(
                    name="ask_big_model",
                    description=(
                        "Delegate a complex analytical task, multi-source synthesis, or large text analysis "
                        "to the pool of large-context Gemini models."
                    ),
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "task": types.Schema(type=types.Type.STRING, description="Clear instruction or question for the big model."),
                            "context": types.Schema(type=types.Type.STRING, description="Raw text/data to analyze or summarize."),
                        },
                        required=["task"],
                    ),
                )
                base_decls.append(big_tool)
                declared_names.add("ask_big_model")

            if (
                _opts.get("serper_api_key")
                or _opts.get("web_search_model")
                or _opts.get("big_models_enabled")
            ):
                web_search_tool = _make_decl(
                    name="web_search",
                    description=(
                        "Search the live Internet via Google Search "
                        "(use for fresh news, release dates, prices, weather, facts, or anything post-training)."
                    ),
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "query": types.Schema(type=types.Type.STRING, description="Clear search query or question."),
                        },
                        required=["query"],
                    ),
                )
                base_decls.append(web_search_tool)
                declared_names.add("web_search")
        except Exception:
            pass

        if plugin_registry_singleton:
            try:
                for spec in plugin_registry_singleton.get_active_tool_specs():
                    t_name = (spec.get("name") or "").strip()
                    if not t_name or t_name in declared_names:
                        continue
                    params_dict = spec.get("parameters") or {"type": "OBJECT", "properties": {}}
                    base_decls.append(
                        _make_decl(
                            name=t_name,
                            description=spec.get("description") or "",
                            parameters=_json_schema_to_genai(params_dict),
                        )
                    )
                    declared_names.add(t_name)
            except Exception as e:
                logger.warning(f"Failed to attach plugin tool declarations: {e}")

        tools = None
        if self.enable_tools:
            tool_args = {"function_declarations": base_decls}
            if self.enable_google_search:
                tool_args["google_search"] = types.GoogleSearch()
                
            tool = types.Tool(**tool_args)
            tools = [tool]
        elif self.enable_google_search:
            tools = [types.Tool(google_search=types.GoogleSearch())]
            
        realtime_input_config = None
        if self.manual_vad:
            try:
                realtime_input_config = types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(
                        disabled=True
                    ),
                    activity_handling="NO_INTERRUPTION",
                    turn_coverage="TURN_INCLUDES_ONLY_ACTIVITY",
                )
            except Exception:
                realtime_input_config = types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(
                        disabled=True
                    )
                )
        elif self.vad_silence_duration_ms:
            realtime_input_config = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    silence_duration_ms=self.vad_silence_duration_ms
                )
            )

        speech_config = None
        if "AUDIO" in self.response_modalities:
            speech_config = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice_name)
                )
            )

        thinking_config = None
        if "extended-thinking" in (self.model or "").lower() and hasattr(types, "ThinkingConfig"):
            try:
                thinking_config = types.ThinkingConfig(thinking_level="low")
            except Exception:
                try:
                    thinking_config = types.ThinkingConfig(thinking_budget=1024)
                except Exception:
                    thinking_config = None

        live_kwargs: Dict[str, Any] = {
            "response_modalities": self.response_modalities,
            "system_instruction": types.Content(parts=[types.Part.from_text(text=self.system_prompt)]),
            "tools": tools,
            "speech_config": speech_config,
            "realtime_input_config": realtime_input_config,
            "input_audio_transcription": types.AudioTranscriptionConfig(),
            "output_audio_transcription": types.AudioTranscriptionConfig(),
        }
        if thinking_config is not None:
            live_kwargs["thinking_config"] = thinking_config

        return types.LiveConnectConfig(**live_kwargs)

    def connect(self):
        """Возвращает асинхронный контекстный менеджер сессии."""
        return self.client.aio.live.connect(model=self.model, config=self._get_config())
