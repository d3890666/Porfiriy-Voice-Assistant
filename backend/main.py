import asyncio
import collections
import json
import os
import logging
import math
import struct
import time
import websockets
from websockets.exceptions import ConnectionClosed

from ha_api import HomeAssistantAPI
from gemini_client import GeminiProxyClient, fetch_available_gemini_models
from google.genai import types
from phrase_manager import PhraseManager
from device_manager import DeviceManager
from mqtt_discovery import MQTTDiscoveryManager
from web_server import WebServer
from wakeword_engine import WakeWordEngine

phrase_manager = PhraseManager()
device_manager = DeviceManager()

# Серверный детектор вейкворда (один на весь процесс)
_WW_MODEL_PATH = "porfiriy.onnx"
_WW_DEFAULT_THRESHOLD = 0.94
ww_engine: WakeWordEngine = None  # инициализируется в main()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("server")
audio_logger = logging.getLogger("audio")
tool_logger = logging.getLogger("tools")

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
        shorts = struct.unpack(f"<{count}h", pcm_data[:count*2])
        return math.sqrt(sum(s * s for s in shorts) / count)

def scale_pcm16(pcm_data: bytes, volume: float) -> bytes:
    """Масштабирует громкость 16-битного PCM звука с защитой от клиппинга."""
    if not pcm_data or volume == 1.0:
        return pcm_data
    volume = max(0.0, min(2.0, volume))
    try:
        import audioop
        return audioop.mul(pcm_data, 2, volume)
    except Exception:
        import array
        arr = array.array("h")
        arr.frombytes(pcm_data)
        for i in range(len(arr)):
            v = int(arr[i] * volume)
            if v > 32767: v = 32767
            elif v < -32768: v = -32768
            arr[i] = v
        return arr.tobytes()

def generate_beep(freq: int, duration_ms: int, sample_rate: int = 16000, volume: float = 0.5) -> bytes:
    """Генерация сырого 16-bit PCM аудио сигнала (синусоиды)."""
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    audio = bytearray()
    for i in range(num_samples):
        sample = math.sin(2 * math.pi * freq * i / sample_rate)
        # Простое сглаживание для предотвращения щелчков (fade in/out)
        fade_len = min(100, num_samples // 4)
        if i < fade_len:
            sample *= (i / fade_len)
        elif i > num_samples - fade_len:
            sample *= ((num_samples - i) / fade_len)
            
        val = int(sample * volume * 32767)
        audio.extend(struct.pack('<h', val))
    return bytes(audio)

# Предварительно сгенерированные звуки (Earcons)
SUCCESS_CHIME = generate_beep(600, 100) + generate_beep(800, 150)
ERROR_CHIME = generate_beep(300, 150) + generate_beep(200, 200)

SYSTEM_OPTIONS_PATH = "/data/options.json"
USER_CONFIG_PATH = "/data/porfiriy_config.json" if os.path.exists("/data") else os.path.join(os.path.dirname(__file__), "porfiriy_config.json")
_runtime_options = None

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
    "- Trigger: Requests to play music, artists, albums, tracks, playlists, or radio.\n"
    "- Action: Immediately call search_music_assistant with the query and media_type, then play the URI using play_music_assistant. Do NOT use internet search for music requests.\n"
    "- Speech Output: If playback starts successfully, announce strictly: \"Включаю [Artist - Title].\" If not found or failed, state strictly: \"Акустический паттерн не найден.\"\n\n"
    "CONCISENESS & STOP DIRECTIVE:\n"
    "- Keep dialogue responses strictly to 1-2 concise sentences. Avoid unsolicited lectures or monologues unless explicitly asked \"Расскажи подробно\".\n"
    "- If Denis or Sveta issues an interruption command (\"хватит\", \"стоп\", \"молчи\", \"замолчи\"), reply strictly with the single word \"Умолкаю.\" and immediately complete the turn.\n\n"
    "PING:\n"
    "- Reply strictly: \"PONG\"."
)

_available_models = []
_api_key_index = 0

def get_active_api_key(api_key_str: str) -> str:
    global _api_key_index
    if not api_key_str:
        return ""
    keys = [k.strip() for k in api_key_str.split(",") if k.strip()]
    if not keys:
        return ""
    _api_key_index = (_api_key_index + 1) % len(keys)
    return keys[_api_key_index]

async def refresh_available_models():
    global _available_models
    opts = get_options()
    api_key_str = opts.get("gemini_api_key", "")
    api_key = get_active_api_key(api_key_str)
    try:
        _available_models = await fetch_available_gemini_models(api_key)
        live_count = sum(1 for m in _available_models if m.get("is_live"))
        logger.info(f"Refreshed Gemini models list: {len(_available_models)} models discovered ({live_count} live-capable).")
    except Exception as e:
        logger.warning(f"Error during refresh_available_models: {e}")
    return _available_models

def get_options():
    global _runtime_options
    if _runtime_options is not None:
        return dict(_runtime_options)

    opts = {}
    # 1. Загружаем системные опции Supervisor (если есть)
    if os.path.exists(SYSTEM_OPTIONS_PATH):
        try:
            with open(SYSTEM_OPTIONS_PATH, "r", encoding="utf-8") as f:
                opts.update(json.load(f))
        except Exception as e:
            logger.error(f"Error loading options.json: {e}")

    # 2. Поверх накладываем изолированные веб-настройки пользователя (не перезаписываемые Supervisor'ом)
    if os.path.exists(USER_CONFIG_PATH):
        try:
            with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
                user_opts = json.load(f)
                opts.update(user_opts)
        except Exception as e:
            logger.error(f"Error loading porfiriy_config.json: {e}")

    # Если пользователь ранее настраивал единый system_prompt в старых версиях,
    # переносим его в prompt_persona если prompt_persona еще пустой
    if opts.get("system_prompt", "").strip() and not opts.get("prompt_persona", "").strip():
        opts["prompt_persona"] = opts["system_prompt"].strip()

    # Заполняем дефолтными значениями если они не указаны или пустые
    defaults = {
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "gemini_model": "models/gemini-3.1-flash-live-preview",
        "system_prompt": "",
        "prompt_persona": DEFAULT_PERSONA,
        "prompt_users": DEFAULT_USERS,
        "prompt_smart_home": DEFAULT_SMART_HOME,
        "prompt_general": DEFAULT_GENERAL,
        "voice_name": "Charon",
        "temperature": 0.7,
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
        "ma_api_key": "",
        "regenerate_phrases": False
    }

    for k, v in defaults.items():
        if k not in opts or (isinstance(opts[k], str) and not opts[k].strip() and k.startswith("prompt_")):
            opts[k] = v

    _runtime_options = opts
    return dict(_runtime_options)

def save_options(new_options: dict):
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

    # Если API ключ был обновлен — обновляем список моделей
    new_key = opts.get("gemini_api_key", "")
    if new_key and new_key != old_key:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(refresh_available_models())
        except RuntimeError:
            pass

async def handle_client(websocket):
    options = get_options()
    if options.get("debug_mode"):
        logger.setLevel(logging.DEBUG)
        tool_logger.setLevel(logging.DEBUG)
        logger.debug("Debug mode enabled. Maximum logging activated.")
        
    if options.get("debug_audio", False):
        audio_logger.setLevel(logging.DEBUG)
    else:
        audio_logger.setLevel(logging.INFO)
        
    remote_ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
    client_mac = f"esp32_{remote_ip.replace('.', '_')}"
    logger.info(f"Client connected from {websocket.remote_address} (session id: {client_mac})")

    # --- Быстрая проверка типа устройства ДО открытия Gemini-сессии ---
    # Ждём первое сообщение. Если это register с device_type="pc_streamer" —
    # маршрутизируем в handle_pc_streamer (серверный вейкворд), иначе идём дальше.
    try:
        first_msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
    except (asyncio.TimeoutError, ConnectionClosed):
        logger.info(f"Client {client_mac} disconnected before sending register message.")
        return

    first_data = None
    if isinstance(first_msg, str):
        try:
            first_data = json.loads(first_msg)
        except Exception:
            pass

    if first_data and first_data.get("type") == "register":
        client_mac = first_data.get("mac", client_mac).lower()
        device_type = first_data.get("device_type", "esp32")
        device_manager.register_device(client_mac, first_data, ws=websocket)
        if device_type == "pc_streamer":
            logger.info(f"[STREAMER] Routing {client_mac} to server-side wake word handler.")
            await handle_pc_streamer(websocket, client_mac, first_data)
            return
    elif isinstance(first_msg, bytes):
        # Редкий случай: клиент сразу льёт байты (старый debug_client)
        pass  # обработаем ниже вместе с остальными

    ha_api = HomeAssistantAPI()
    
    # Проверяем, привязана ли уже эта колонка к комнате в HA
    area_name = await ha_api.get_device_area_name(client_mac)
    if area_name:
        device_manager.set_device_area(client_mac, area_name)
        logger.info(f"Connected device {client_mac} belongs to area: '{area_name}'")
    
    # 1. Формируем контекст устройств
    devices_text = await ha_api.get_filtered_entities()
    # Сборка модульного системного промпта
    modular_parts = []
    if options.get("prompt_persona", "").strip():
        modular_parts.append(f"### PERSONA & CONTEXT\n{options['prompt_persona'].strip()}")
    if options.get("prompt_users", "").strip():
        modular_parts.append(f"### USERS & ACOUSTIC IDENTIFICATION\n{options['prompt_users'].strip()}")
    if options.get("prompt_smart_home", "").strip():
        modular_parts.append(f"### SMART HOME EXECUTION RULES\n{options['prompt_smart_home'].strip()}")
    if options.get("prompt_general", "").strip():
        modular_parts.append(f"### GENERAL DIALOGUE, SEARCH & MEDIA\n{options['prompt_general'].strip()}")

    if modular_parts:
        prompt_base = "\n\n".join(modular_parts)
    else:
        prompt_base = options.get("system_prompt", "").strip()

    anti_hallucination = (
        "CRITICAL DIRECTIVE: NEVER fabricate or claim you performed a smart home action "
        "unless you have explicitly called the corresponding tool (e.g. call_ha_service). "
        "Devices under domain 'switch' can also control lights or appliances; use them when appropriate."
    )
    full_prompt = f"{prompt_base}\n\n{anti_hallucination}\n\nAvailable Home Assistant devices:\n{devices_text}"
    if area_name:
        full_prompt += f"\n\nCURRENT ACOUSTIC LOCATION: The user is speaking through the device in room '{area_name}'. When handling ambiguous smart home requests (e.g. 'turn on light', 'close curtains'), ALWAYS prioritize devices located in '{area_name}'."
    logger.info(f"Loaded {len(devices_text.splitlines())} HA entities into the system prompt.")
    
    api_key_str = (options.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")).strip()
    api_key = get_active_api_key(api_key_str)
    
    if not api_key:
        logger.error("🛑 [CONFIG ERROR] Gemini API key is empty! Пожалуйста, откройте Web UI Порфирия (вкладка 'Мозг & Личность') и сохраните ваш API-ключ Gemini.")
        try:
            await websocket.send(json.dumps({
                "type": "error",
                "message": "Gemini API key is not configured. Please open Porfiriy Web UI."
            }))
        except Exception:
            pass
        return

    gemini_client = GeminiProxyClient(
        api_key=api_key,
        system_prompt=full_prompt,
        ha_api=ha_api,
        voice_name=options.get("voice_name", "Zephyr"),
        model=options.get("gemini_model", "gemini-2.0-flash-exp"),
        enable_google_search=options.get("enable_google_search", True),
        vad_silence_duration_ms=options.get("vad_silence_duration_ms", 600)
    )
    
    logger.info(f"Connecting to Gemini Live API using model: {gemini_client.model}")
    
    try:
        async with gemini_client.connect() as session, asyncio.TaskGroup() as tg:
            
            session_state = {
                "is_gemini_speaking": False,
                "first_audio_received": False,
                "first_audio_sent": False,
                "is_tool_pending": False,
                "is_thinking": False,
                "thinking_watchdog_task": None,
                "ducked_media_players": {},
                "area_name": area_name
            }

            def start_thinking_watchdog():
                if session_state.get("thinking_watchdog_task"):
                    session_state["thinking_watchdog_task"].cancel()
                session_state["is_thinking"] = True

                async def _watchdog():
                    try:
                        thinking_delay = float(options.get("thinking_timeout_s", 7.0))
                        await asyncio.sleep(thinking_delay)
                        if session_state.get("is_thinking") and not session_state.get("first_audio_sent") and not session_state.get("is_tool_pending"):
                            phrase = phrase_manager.get_phrase("thinking")
                            if phrase:
                                logger.info(f"Thinking timeout > {thinking_delay}s: playing dynamic Porfiriy filler phrase...")
                                speaker_vol = float(device_manager.get_device_config(client_mac).get("speaker_volume", 0.5))
                                await phrase_manager.play_phrase(
                                    websocket, 
                                    phrase, 
                                    volume=speaker_vol,
                                    cancel_check=lambda: session_state.get("first_audio_sent", False)
                                )
                    except asyncio.CancelledError:
                        pass
                    except Exception as ex:
                        logger.error(f"Error playing thinking phrase: {ex}")

                session_state["thinking_watchdog_task"] = asyncio.create_task(_watchdog())

            def cancel_thinking_watchdog():
                session_state["is_thinking"] = False
                task = session_state.get("thinking_watchdog_task")
                if task and not task.done():
                    task.cancel()
                    session_state["thinking_watchdog_task"] = None
            gemini_send_lock = asyncio.Lock()

            async def duck_media():
                """Приглушить громкость активных медиаплееров в Home Assistant."""
                # 1. Проверяем индивидуальную настройку конкретной вызванной колонки
                dev_cfg = device_manager.get_device_config(client_mac)
                if not dev_cfg.get("enable_ducking", True):
                    logger.info(f"[DUCKING] Ducking disabled for device {client_mac}, skipping.")
                    return

                # 2. Проверяем глобальный режим дакинга
                mode = options.get("ducking_mode", "same_area")
                if options.get("enable_media_ducking") is False or mode == "disabled":
                    return

                try:
                    factor = float(options.get("ducking_volume_factor", 0.25))
                    playing_players = await ha_api.get_playing_media_players()
                    if not playing_players:
                        return

                    target_players = []

                    if mode == "same_area":
                        calling_area = device_manager.get_device_area(client_mac)
                        if not calling_area and ha_api:
                            calling_area = await ha_api.get_device_area_name(client_mac)

                        if calling_area:
                            player_areas = await ha_api.get_media_player_areas()
                            for p in playing_players:
                                eid = p.get("entity_id")
                                p_area = player_areas.get(eid)
                                if p_area and p_area.strip().lower() == calling_area.strip().lower():
                                    target_players.append(p)
                            logger.info(f"[DUCKING] Area matching for '{calling_area}': {len(target_players)}/{len(playing_players)} players matched.")
                        else:
                            # Если комната колонки не определена, глушим все играющие плееры
                            logger.info(f"[DUCKING] Device {client_mac} has no area assigned. Falling back to all playing players.")
                            target_players = playing_players
                    else:
                        # mode == "all"
                        target_players = playing_players

                    for p in target_players:
                        eid = p.get("entity_id")
                        if eid and eid not in session_state["ducked_media_players"]:
                            curr_vol = p.get("attributes", {}).get("volume_level")
                            if curr_vol is not None:
                                session_state["ducked_media_players"][eid] = curr_vol
                                target_vol = max(0.05, round(curr_vol * factor, 2))
                                logger.info(f"[DUCKING] Ducking {eid} from {curr_vol} to {target_vol}")
                                await ha_api.call_service("media_player", "volume_set", {
                                    "entity_id": eid,
                                    "volume_level": target_vol
                                })
                except Exception as e:
                    logger.error(f"[DUCKING] Error ducking media: {e}")

            async def unduck_media():
                """Восстановить исходную громкость медиаплееров."""
                if not session_state.get("ducked_media_players"):
                    return
                try:
                    for eid, orig_vol in list(session_state["ducked_media_players"].items()):
                        logger.info(f"[DUCKING] Restoring {eid} volume back to {orig_vol}")
                        await ha_api.call_service("media_player", "volume_set", {
                            "entity_id": eid,
                            "volume_level": orig_vol
                        })
                    session_state["ducked_media_players"].clear()
                except Exception as e:
                    logger.error(f"[DUCKING] Error restoring media volume: {e}")
            
            async def receive_from_client():
                """Слушает входящие аудио-чанки (PCM) от WebSocket клиента (ПК/ESP32) и шлет их в Gemini."""
                nonlocal client_mac
                try:
                    async for message in websocket:
                        if isinstance(message, bytes):
                            # Проверяем, активен ли режим отладки/теста микрофона для этой колонки
                            if client_mac and device_manager.is_mic_test_active(client_mac):
                                device_manager.append_mic_test_chunk(client_mac, message)
                                if time.time() >= device_manager.get_mic_test_end_time(client_mac):
                                    device_manager.finish_mic_test(client_mac)
                                continue

                            if session_state.get("is_tool_pending"):
                                # КРИТИЧНО: Нельзя отправлять аудио, пока выполняется Tool Call, иначе сервер Gemini закроет соединение с ошибкой 1008
                                continue
                                
                            is_speaking = session_state.get("is_gemini_speaking", False)
                            barge_in_enabled = options.get("enable_barge_in", True)
                            
                            if is_speaking:
                                if not barge_in_enabled:
                                    # Игнорируем микрофон пока говорит ассистент, если перебивание выключено
                                    continue
                                
                                # Серверный шлюз Barge-in с адаптивным фильтром эха динамика
                                rms = calculate_pcm_rms(message)
                                baseline = session_state.get("echo_baseline_rms", 300.0)
                                # Плавно подтягиваем базовый уровень эха динамика
                                session_state["echo_baseline_rms"] = baseline * 0.90 + rms * 0.10
                                
                                # Голос человека должен превышать адаптивное эхо и минимальный порог
                                dev_obj = device_manager.get_device(client_mac) or {}
                                dev_cfg = dev_obj.get("config", {})
                                indiv_rms = dev_cfg.get("barge_in_threshold_rms")
                                if indiv_rms is not None and str(indiv_rms).isdigit() and int(indiv_rms) > 0:
                                    base_thresh = float(indiv_rms)
                                else:
                                    base_thresh = float(options.get("barge_in_threshold_rms", 600))
                                barge_thresh = max(base_thresh, baseline * 2.2)
                                
                                barge_buf = session_state.setdefault("barge_in_buffer", collections.deque(maxlen=6))
                                barge_buf.append(message)
                                
                                now = time.time()
                                if rms >= barge_thresh and (now - session_state.get("last_barge_in_time", 0.0) > 1.2):
                                    session_state["last_barge_in_time"] = now
                                    logger.info(f"[BARGE-IN] User speech detected during playback (RMS: {rms:.0f} >= {barge_thresh:.0f})! Interrupting Gemini & Client speaker.")
                                    
                                    # 1. Мгновенно глушим динамик на клиенте (ESP32/PC)
                                    await websocket.send(json.dumps({"type": "interrupted"}))
                                    
                                    # 2. Переводим состояние в listening
                                    session_state["is_gemini_speaking"] = False
                                    session_state["first_audio_sent"] = False
                                    cancel_thinking_watchdog()
                                    device_manager.set_device_state(client_mac, "listening")
                                    
                                    # 3. Отправляем в Gemini буфер предзаписи + текущий чанк
                                    async with gemini_send_lock:
                                        while barge_buf:
                                            pre_chunk = barge_buf.popleft()
                                            await session.send_realtime_input(
                                                audio=types.Blob(
                                                    data=pre_chunk,
                                                    mime_type="audio/pcm;rate=16000"
                                                )
                                            )
                                    continue
                                else:
                                    # Шлюз закрыт (звучит эхо динамика или тишина) - не шлем в Gemini чтобы не прервать речь
                                    continue
                                
                            utt_buf = session_state.setdefault("utterance_buffer", collections.deque(maxlen=150))
                            utt_buf.append(message)

                            if not session_state.get("first_audio_received"):
                                logger.info(f"Started receiving audio stream from microphone ({client_mac})...")
                                session_state["first_audio_received"] = True
                                device_manager.set_device_state(client_mac, "listening")
                                
                            audio_logger.debug(f"Received {len(message)} bytes audio chunk from WS Client, sending to Gemini")
                            async with gemini_send_lock:
                                await session.send_realtime_input(
                                    audio=types.Blob(
                                        data=message,
                                        mime_type="audio/pcm;rate=16000"
                                    )
                                )
                        elif isinstance(message, str):
                            # Обработка текстовых сообщений
                            try:
                                data = json.loads(message)
                                msg_type = data.get("type")
                                
                                if msg_type == "mic_test_complete":
                                    logger.info(f"[MIC-TEST] Received mic_test_complete from {client_mac}")
                                    if client_mac:
                                        device_manager.finish_mic_test(client_mac)
                                    continue
                                elif msg_type == "register":
                                    client_mac = data.get("mac", client_mac).lower()
                                    device_manager.register_device(client_mac, data, ws=websocket)
                                    area = await ha_api.get_device_area_name(client_mac)
                                    if area:
                                        device_manager.set_device_area(client_mac, area)
                                        session_state["area_name"] = area
                                        logger.info(f"Resolved room for {client_mac}: '{area}'")
                                elif msg_type == "heartbeat":
                                    device_manager.update_heartbeat(
                                        client_mac,
                                        rssi=data.get("rssi", -60),
                                        uptime=data.get("uptime", 0),
                                        state=data.get("state")
                                    )
                                elif "text" in data:
                                    logger.info(f"Received text input from WS Client ({client_mac}): {data['text']}")
                                    async with gemini_send_lock:
                                        await session.send_client_content(
                                            turns=[types.Content(parts=[types.Part(text=data['text'])])],
                                            turn_complete=True
                                        )
                                elif msg_type == "wake_word_detected":
                                    logger.info(f"Wake word received from {client_mac}. Resetting session state.")
                                    session_state["utterance_buffer"] = collections.deque(maxlen=150)
                                    device_manager.set_device_state(client_mac, "listening")
                                    cancel_thinking_watchdog()
                                    session_state["is_gemini_speaking"] = False
                                    session_state["is_tool_pending"] = False
                                    session_state["first_audio_sent"] = False
                                    session_state["first_audio_received"] = False
                                    await duck_media()
                                    
                                    # Инжектируем контекст комнаты вызова в Gemini
                                    if session_state.get("area_name"):
                                        room_name = session_state["area_name"]
                                        room_ctx = (
                                            f"[КОНТЕКСТ ВЫЗОВА]: Тебя вызвали из комнаты: «{room_name}». "
                                            f"Если запрос касается устройств без явного указания комнаты (например 'включи свет' или 'закрой шторы'), "
                                            f"приоритетно управляй устройствами именно в комнате «{room_name}»."
                                        )
                                        try:
                                            async with gemini_send_lock:
                                                await session.send_client_content(
                                                    turns=[types.Content(parts=[types.Part(text=room_ctx)], role="user")],
                                                    turn_complete=False
                                                )
                                            logger.info(f"Injected room context to Gemini: '{room_name}'")
                                        except Exception as e_ctx:
                                            logger.warning(f"Could not inject room context: {e_ctx}")
                                            
                                elif msg_type == "interrupted":
                                    logger.info("Interrupted signal received from ESP32. Stopping playback.")
                                    session_state["is_gemini_speaking"] = False
                                    session_state["is_tool_pending"] = False
                                    session_state["first_audio_sent"] = False
                                    session_state["first_audio_received"] = False
                                elif msg_type == "end_of_speech":
                                    if client_mac and session_state.get("utterance_buffer"):
                                        device_manager.save_last_utterance(client_mac, b"".join(session_state["utterance_buffer"]))
                                    device_manager.set_device_state(client_mac, "thinking")
                                    vad_ms = options.get("vad_silence_duration_ms", 600)
                                    needed_chunks = max(28, int((vad_ms + 300) * 32 / 1024) + 1)
                                    logger.info(f"Received end_of_speech from ESP32. Sending {needed_chunks} silence chunks (~{needed_chunks*32}ms) to trigger Gemini VAD.")
                                    silence_chunk = b"\x00" * 1024
                                    async with gemini_send_lock:
                                        for _ in range(needed_chunks):
                                            await session.send_realtime_input(
                                                audio=types.Blob(
                                                    data=silence_chunk,
                                                    mime_type="audio/pcm;rate=16000"
                                                )
                                            )
                                    await websocket.send(json.dumps({"type": "thinking"}))
                                    start_thinking_watchdog()
                                elif msg_type == "timeout":
                                    logger.info("Received timeout from ESP32. Returning client to sleep.")
                                    device_manager.set_device_state(client_mac, "idle")
                                    cancel_thinking_watchdog()
                                    session_state["is_gemini_speaking"] = False
                                    session_state["is_tool_pending"] = False
                                    session_state["first_audio_sent"] = False
                                    # При таймауте ожидания команды тихо засыпаем без спонтанных реплик
                                    await websocket.send(json.dumps({"type": "sleep"}))
                                    await unduck_media()
                            except Exception as e:
                                logger.error(f"Error parsing text message: {e}")
                except ConnectionClosed:
                    logger.info("Client disconnected (WS read)")
                except Exception as e:
                    logger.error(f"Error reading WS client: {e}")

            async def receive_from_gemini():
                """Слушает ответы от Gemini, пересылает аудио клиенту и исполняет Tool Calls (HA)."""
                try:
                    while True:
                        async for response in session.receive():
                            audio_logger.debug("Received event from Gemini")
                        
                            # Обработка аудио потока от модели
                            if response.server_content and response.server_content.model_turn:
                                session_state["is_gemini_speaking"] = True
                                for part in response.server_content.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        if not session_state.get("first_audio_sent"):
                                            logger.info("Started receiving audio stream from Gemini (Speaker active)...")
                                            session_state["first_audio_sent"] = True
                                            cancel_thinking_watchdog()
                                            device_manager.set_device_state(client_mac, "speaking")
                                            await websocket.send(json.dumps({"type": "speaking"}))
                                        
                                        # Масштабируем звук на сервере согласно настроенной громкости колонки
                                        speaker_vol = float(device_manager.get_device_config(client_mac).get("speaker_volume", 0.5))
                                        pcm_audio = scale_pcm16(part.inline_data.data, speaker_vol)
                                        audio_logger.debug(f"Sending {len(pcm_audio)} bytes audio chunk from Gemini to WS Client (scaled to {speaker_vol*100:.0f}%)")
                                    
                                        # Чанкуем аудио на сервере, чтобы ESP32 не падала от нехватки памяти
                                        CHUNK_SIZE = 4096
                                        for i in range(0, len(pcm_audio), CHUNK_SIZE):
                                            chunk = pcm_audio[i:i+CHUNK_SIZE]
                                            await websocket.send(chunk)
                                    
                            # Обработка транскрипции и состояния
                            content = response.server_content
                            if content:
                                if getattr(content, "turn_complete", False):
                                    logger.info("Gemini finished turn. Sending SLEEP command to client.")
                                    cancel_thinking_watchdog()
                                    session_state["is_gemini_speaking"] = False
                                    # Сбрасываем флаг отправки аудио для следующего ответа
                                    session_state["first_audio_sent"] = False
                                    device_manager.set_device_state(client_mac, "idle")
                                    # Отправляем команду на засыпание (чтобы колонка снова ждала вейкворд)
                                    await websocket.send(json.dumps({"type": "sleep"}))
                                
                                # Обработка прерывания
                                if getattr(content, "interrupted", False):
                                    logger.info("Gemini Interrupted by User (Barge-in)!")
                                    session_state["is_gemini_speaking"] = False
                                    session_state["first_audio_sent"] = False
                                    cancel_thinking_watchdog()
                                    device_manager.set_device_state(client_mac, "listening")
                                    await websocket.send(json.dumps({"type": "interrupted"}))
                                
                                if getattr(content, "input_transcription", None):
                                    logger.info(f"User Speech Recognized: {content.input_transcription.text}")
                                    if not session_state.get("is_gemini_speaking"):
                                        await websocket.send(json.dumps({"type": "thinking"}))
                                if getattr(content, "output_transcription", None):
                                    logger.info(f"Gemini Speech: {content.output_transcription.text}")
                                    
                                # Логируем текст ответа Gemini напрямую
                                if getattr(response.server_content.model_turn, "parts", None):
                                    for part in response.server_content.model_turn.parts:
                                        if getattr(part, "text", None):
                                            logger.info(f"Gemini says: {part.text}")
                        
                            # Обработка вызовов функций (Home Assistant)
                            if response.tool_call:
                                session_state["is_tool_pending"] = True
                                cancel_thinking_watchdog()
                                await websocket.send(json.dumps({"type": "thinking"}))
                                tool_logger.info(f"RAW Tool Call from Gemini: {response.tool_call}")
                                function_responses = []
                                for fc in response.tool_call.function_calls:
                                    name = fc.name
                                    if name == "call_ha_service":
                                        raw_args = dict(fc.args) if fc.args else {}
                                        domain = raw_args.get("domain")
                                        service = raw_args.get("service")
                                        entity_id = raw_args.get("entity_id")
                                    
                                        # Forward all parameters (position, temperature, etc.)
                                        service_data = {
                                            k: v for k, v in raw_args.items()
                                            if k not in ("domain", "service") and v is not None
                                        }
                                        if entity_id and "entity_id" not in service_data:
                                            service_data["entity_id"] = entity_id
                                    
                                        tool_logger.info(f"Gemini Calling Tool: {domain}.{service} with service_data: {service_data}")
                                    
                                        # Выполняем действие в Home Assistant
                                        result = await ha_api.call_service(
                                            domain=domain,
                                            service=service,
                                            service_data=service_data
                                        )
                                    
                                        tool_logger.info(f"HA Action Result: {result}")
                                    
                                        # Отправляем звуковой отклик (Earcon / Phrase) клиенту напрямую
                                        speaker_vol = float(device_manager.get_device_config(client_mac).get("speaker_volume", 0.5))
                                        if "error" in result:
                                            phrase = phrase_manager.get_phrase("device_error")
                                            if phrase:
                                                await phrase_manager.play_phrase(websocket, phrase, volume=speaker_vol)
                                            else:
                                                await websocket.send(scale_pcm16(ERROR_CHIME, speaker_vol))
                                        else:
                                            await websocket.send(scale_pcm16(SUCCESS_CHIME, speaker_vol))
                                        
                                        # Формируем безопасный словарь для ответа, чтобы Gemini не ругался на пустые списки
                                        safe_result = {"status": "success"}
                                        if result:
                                            if isinstance(result, list):
                                                safe_result["data"] = result
                                            elif isinstance(result, dict):
                                                safe_result = result
                                            else:
                                                safe_result["data"] = str(result)
                                            
                                        function_responses.append(types.FunctionResponse(
                                            name=fc.name,
                                            id=fc.id,
                                            response=safe_result
                                        ))

                                    elif name == "get_ha_state":
                                        raw_args = dict(fc.args)
                                        entity_id = raw_args.get("entity_id")
                                        tool_logger.info(f"Gemini Calling get_ha_state for entity: {entity_id}")
                                        state_obj = await ha_api.get_entity_state(entity_id)
                                        if isinstance(state_obj, dict) and "state" in state_obj:
                                            safe_result = {
                                                "status": "success",
                                                "entity_id": entity_id,
                                                "state": state_obj.get("state"),
                                                "unit": state_obj.get("attributes", {}).get("unit_of_measurement"),
                                                "friendly_name": state_obj.get("attributes", {}).get("friendly_name"),
                                                "attributes": state_obj.get("attributes", {}),
                                            }
                                        else:
                                            safe_result = state_obj if isinstance(state_obj, dict) else {"status": "error", "message": str(state_obj)}
                                        tool_logger.info(f"HA get_ha_state result: {safe_result}")
                                        function_responses.append(types.FunctionResponse(
                                            name=fc.name,
                                            id=fc.id,
                                            response=safe_result
                                        ))
                                    
                                    elif name == "search_music_assistant":
                                        args = fc.args
                                        tool_logger.info(f"Gemini Calling MA Search: {args}")
                                        search_data = dict(args)
                                        # HA API expects media_type to be a list if provided
                                        if "media_type" in search_data:
                                            search_data["media_type"] = [search_data["media_type"]]
                                        
                                        # Music Assistant Core requires config_entry_id for search
                                        ma_entry_id = await ha_api.get_music_assistant_entry_id()
                                        if ma_entry_id:
                                            search_data["config_entry_id"] = ma_entry_id
                                        
                                        result = await ha_api.call_service_ws(
                                            domain="music_assistant",
                                            service="search",
                                            service_data=search_data,
                                            return_response=True
                                        )
                                    
                                        simplified_result = []
                                        if isinstance(result, dict) and not "error" in result:
                                            # HA 'call_service' with return_response=True usually wraps the output in a 'response' dict
                                            actual_response = result.get("response", result)
                                            if isinstance(actual_response, dict):
                                                for cat, items in actual_response.items():
                                                    if isinstance(items, list):
                                                        for item in items[:5]: # top 5 per category
                                                            simplified_result.append({
                                                                "name": item.get("name"),
                                                                "uri": item.get("uri"),
                                                                "type": cat
                                                            })
                                            result = simplified_result if simplified_result else {"result": "Ничего не найдено"}
                                        
                                        tool_logger.info(f"MA Search Result: {result}")
                                        safe_result = {"status": "success"}
                                        if result:
                                            if isinstance(result, list):
                                                safe_result["data"] = result
                                            elif isinstance(result, dict):
                                                safe_result = result
                                            else:
                                                safe_result["data"] = str(result)

                                        function_responses.append(types.FunctionResponse(
                                            name=fc.name,
                                            id=fc.id,
                                            response=safe_result
                                        ))
                                    
                                    elif name == "play_music_assistant":
                                        args = fc.args
                                        uri = args.get("uri")
                                        player = args.get("player") or options.get("default_media_player", "media_player.living_room")
                                        tool_logger.info(f"Gemini Playing MA URI: {uri} on {player}")
                                    
                                        result = await ha_api.call_service(
                                            domain="media_player",
                                            service="play_media",
                                            service_data={
                                                "entity_id": player,
                                                "media_content_id": uri,
                                                "media_content_type": "music"
                                            }
                                        )
                                    
                                        tool_logger.info(f"MA Play Result: {result}")
                                        speaker_vol = float(device_manager.get_device_config(client_mac).get("speaker_volume", 0.5))
                                        if "error" in result:
                                            await websocket.send(scale_pcm16(ERROR_CHIME, speaker_vol))
                                        else:
                                            await websocket.send(scale_pcm16(SUCCESS_CHIME, speaker_vol))
                                        
                                        safe_result = {"status": "success"}
                                        if result:
                                            if isinstance(result, list):
                                                safe_result["data"] = result
                                            elif isinstance(result, dict):
                                                safe_result = result
                                            else:
                                                safe_result["data"] = str(result)

                                        function_responses.append(types.FunctionResponse(
                                            name=fc.name,
                                            id=fc.id,
                                            response=safe_result
                                        ))
                                    else:
                                        tool_logger.warning(f"Unknown tool called: {name}")
                                        function_responses.append(types.FunctionResponse(
                                            name=fc.name,
                                            id=fc.id,
                                            response={"error": "Unknown tool"}
                                        ))
                            
                                if function_responses:
                                    tool_logger.info(f"Sending Tool Responses: {function_responses}")
                                    async with gemini_send_lock:
                                        await session.send_tool_response(function_responses=function_responses)
                                    session_state["is_tool_pending"] = False
                                    session_state["is_gemini_speaking"] = False
                                    
                except asyncio.CancelledError:
                    pass
                except ConnectionClosed:
                    logger.info("Client disconnected (Gemini read)")
                except Exception as e:
                    logger.error(f"Error receiving from Gemini: {e}")
                    cancel_thinking_watchdog()
                    # Произносим фразу ТОЛЬКО если пользователь прямо сейчас ждал ответа (был в активном диалоге)!
                    # В режиме покоя (IDLE) при обрыве или тайм-ауте сессии Google колонка обязана молчать!
                    if session_state.get("is_thinking") or session_state.get("is_gemini_speaking"):
                        phrase = phrase_manager.get_phrase("network_error")
                        if phrase:
                            logger.info("Playing dynamic network_error phrase (active user conversation interrupted)...")
                            try:
                                speaker_vol = float(device_manager.get_device_config(client_mac).get("speaker_volume", 0.5))
                                await phrase_manager.play_phrase(websocket, phrase, volume=speaker_vol)
                            except Exception:
                                pass
                    else:
                        logger.info("Gemini Live session disconnected while idle. Staying completely silent.")
                    try:
                        await websocket.send(json.dumps({"type": "sleep"}))
                    except Exception:
                        pass
                    await unduck_media()

            # Запускаем задачи параллельно
            tg.create_task(receive_from_client())
            tg.create_task(receive_from_gemini())
            
    except Exception as e:
        logger.error(f"Gemini Session Setup Error: {e}")
        try:
            await websocket.send(json.dumps({"type": "sleep"}))
        except Exception:
            pass
        await websocket.close()
    finally:
        device_manager.set_device_offline(client_mac)

async def handle_pc_streamer(websocket, client_mac: str, reg_data: dict):
    """
    Обрабатывает подключение pc_streamer клиента:
    - Непрерывно принимает PCM 16 kHz
    - Детектирует вейкворд через WakeWordEngine (openWakeWord ONNX)
    - При срабатывании — открывает сессию Gemini, собирает ответ
    - Ответ сохраняется как WAV и передаётся на HA media_player
    """
    options = get_options()
    ha_api = HomeAssistantAPI()
    dev_config = device_manager.get_device_config(client_mac)
    response_player = dev_config.get("response_player") or options.get("default_media_player", "auto")
    area_name = reg_data.get("area_name") or device_manager.get_device_area(client_mac)
    ww_threshold = float(dev_config.get("ww_threshold", _WW_DEFAULT_THRESHOLD))

    logger.info(f"[STREAMER] {client_mac} listening. response_player={response_player}, threshold={ww_threshold}")
    device_manager.set_device_state(client_mac, "listening")

    # Буфер для накопления аудио во время активной сессии Gemini
    session_active = False
    gemini_audio_buf: list = []

    async def _run_gemini_session(preroll: bytes):
        """Открывает Gemini Live сессию, сливает ответ в WAV и кидает на media_player."""
        nonlocal session_active
        session_active = True
        device_manager.set_device_state(client_mac, "listening")

        api_key_str = (options.get("gemini_api_key") or "").strip()
        api_key = get_active_api_key(api_key_str)
        if not api_key:
            logger.error("[STREAMER] Gemini API key missing, cannot start session.")
            session_active = False
            return

        # Формируем системный промпт
        devices_text = await ha_api.get_filtered_entities()
        modular_parts = []
        for key, title in [
            ("prompt_persona", "PERSONA & CONTEXT"),
            ("prompt_users", "USERS & ACOUSTIC IDENTIFICATION"),
            ("prompt_smart_home", "SMART HOME EXECUTION RULES"),
            ("prompt_general", "GENERAL DIALOGUE, SEARCH & MEDIA"),
        ]:
            if options.get(key, "").strip():
                modular_parts.append(f"### {title}\n{options[key].strip()}")
        prompt_base = "\n\n".join(modular_parts) or options.get("system_prompt", "")
        anti_hallucination = (
            "CRITICAL DIRECTIVE: NEVER fabricate or claim you performed a smart home action "
            "unless you have explicitly called the corresponding tool."
        )
        full_prompt = f"{prompt_base}\n\n{anti_hallucination}\n\nAvailable Home Assistant devices:\n{devices_text}"
        if area_name:
            full_prompt += f"\n\nCURRENT ACOUSTIC LOCATION: Room '{area_name}'."

        gemini_client = GeminiProxyClient(
            api_key=api_key,
            system_prompt=full_prompt,
            ha_api=ha_api,
            voice_name=options.get("voice_name", "Charon"),
            model=options.get("gemini_model", "gemini-2.0-flash-exp"),
            enable_google_search=options.get("enable_google_search", True),
            vad_silence_duration_ms=options.get("vad_silence_duration_ms", 600)
        )

        pcm_response_chunks = []
        try:
            async with gemini_client.connect() as session:
                # Отправляем pre-roll (аудио до вейкворда)
                if preroll:
                    await session.send_realtime_input(
                        audio=types.Blob(data=preroll, mime_type="audio/pcm;rate=16000")
                    )
                # Отправляем накопленный буфер после вейкворда
                for chunk in list(gemini_audio_buf):
                    await session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                    )
                gemini_audio_buf.clear()

                # Получаем ответ
                device_manager.set_device_state(client_mac, "thinking")
                async for response in session.receive():
                    if response.server_content and response.server_content.model_turn:
                        device_manager.set_device_state(client_mac, "speaking")
                        for part in response.server_content.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                speaker_vol = float(device_manager.get_device_config(client_mac).get("speaker_volume", 0.5))
                                pcm_response_chunks.append(scale_pcm16(part.inline_data.data, speaker_vol))
                    if response.server_content and getattr(response.server_content, "turn_complete", False):
                        logger.info(f"[STREAMER] Gemini turn complete for {client_mac}.")
                        break
        except Exception as e:
            logger.error(f"[STREAMER] Gemini session error for {client_mac}: {e}")
        finally:
            session_active = False
            device_manager.set_device_state(client_mac, "listening")

        if not pcm_response_chunks:
            logger.warning(f"[STREAMER] No audio received from Gemini for {client_mac}.")
            return

        # Упаковываем PCM в WAV
        import io, wave as wave_lib
        raw_pcm = b"".join(pcm_response_chunks)
        buf = io.BytesIO()
        with wave_lib.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(raw_pcm)
        wav_bytes = buf.getvalue()
        device_manager.save_response_wav(client_mac, wav_bytes)

        # Определяем URL WAV для HA media_player
        # Используем IP из SUPERVISOR_TOKEN окружения или fallback
        ha_host = os.environ.get("HASSIO_TOKEN", None)
        # Определяем базовый URL из переменных окружения HA (Ingress)
        supervisor_host = "homeassistant"
        wav_url = f"http://{supervisor_host}:8099/api/virtual/{client_mac}/response.wav"

        player = response_player if response_player and response_player != "auto" else None
        if not player:
            logger.warning(f"[STREAMER] No response_player configured for {client_mac}. Set it in device settings.")
            return

        logger.info(f"[STREAMER] Playing response on {player}: {wav_url}")
        try:
            await ha_api.call_service("media_player", "play_media", {
                "entity_id": player,
                "media_content_id": wav_url,
                "media_content_type": "music"
            })
        except Exception as e:
            logger.error(f"[STREAMER] Failed to play media on {player}: {e}")

    try:
        async for message in websocket:
            if isinstance(message, bytes):
                # Прогоняем PCM через вейкворд
                if ww_engine and ww_engine.is_ready:
                    score = ww_engine.process_chunk(message, client_mac)
                    device_manager.update_ww_score(client_mac, score)

                    if ww_engine.is_triggered(client_mac) and not session_active:
                        preroll = ww_engine.consume_preroll(client_mac)
                        # Отправляем beep клиенту (опционально через WebSocket)
                        try:
                            speaker_vol = float(device_manager.get_device_config(client_mac).get("speaker_volume", 0.5))
                            await websocket.send(scale_pcm16(SUCCESS_CHIME, speaker_vol))
                        except Exception:
                            pass
                        asyncio.create_task(_run_gemini_session(preroll))

                if session_active:
                    gemini_audio_buf.append(message)

            elif isinstance(message, str):
                try:
                    data = json.loads(message)
                    msg_type = data.get("type")
                    if msg_type == "heartbeat":
                        device_manager.update_heartbeat(
                            client_mac,
                            rssi=data.get("rssi", 0),
                            uptime=data.get("uptime", 0),
                            state=data.get("state")
                        )
                    elif msg_type == "update_config":
                        # Клиент может обновить ww_threshold и response_player на лету
                        cfg = data.get("config", {})
                        if "ww_threshold" in cfg:
                            ww_threshold = float(cfg["ww_threshold"])
                            device_manager.update_device_config(client_mac, {"ww_threshold": ww_threshold})
                        if "response_player" in cfg:
                            response_player = cfg["response_player"]
                            device_manager.update_device_config(client_mac, {"response_player": response_player})
                except Exception as e:
                    logger.debug(f"[STREAMER] Text message parse error: {e}")

    except ConnectionClosed:
        logger.info(f"[STREAMER] {client_mac} disconnected.")
    except Exception as e:
        logger.error(f"[STREAMER] Unexpected error for {client_mac}: {e}")
    finally:
        if ww_engine:
            ww_engine.reset(client_mac)
        device_manager.set_device_offline(client_mac)


async def main():
    port = 8765
    logger.info(f"Porfiriy Backend Server starting on ws://0.0.0.0:{port} ...")
    
    # Загружаем / генерируем динамический кэш фраз Порфирия
    options = get_options()
    phrase_manager.load_from_cache()
    asyncio.create_task(phrase_manager.initialize(options))
    
    # 0. Запуск динамического обнаружения доступных моделей Gemini
    asyncio.create_task(refresh_available_models())

    # 1. Инициализация серверного WakeWordEngine (openWakeWord ONNX)
    global ww_engine
    ww_threshold_global = float(options.get("ww_server_threshold", _WW_DEFAULT_THRESHOLD))
    ww_engine = WakeWordEngine(model_path=_WW_MODEL_PATH, threshold=ww_threshold_global)
    if ww_engine.is_ready:
        logger.info(f"[WW] Server-side wake word engine ready (threshold={ww_threshold_global}).")
    else:
        logger.warning("[WW] Server-side wake word engine NOT ready. Check porfiriy.onnx and openwakeword install.")

    # 2. Запуск MQTT Discovery Manager
    ha_api = HomeAssistantAPI()
    mqtt_manager = MQTTDiscoveryManager(device_manager, options)
    asyncio.create_task(mqtt_manager.start())
    
    # 3. Запуск Ingress Web Server (порт 8099)
    web_server = WebServer(
        device_manager, 
        ha_api, 
        get_options, 
        options_save_callback=save_options, 
        phrase_manager=phrase_manager, 
        models_callback=lambda: _available_models,
        refresh_models_callback=refresh_available_models,
        ww_engine_callback=lambda: ww_engine,
        port=8099
    )
    asyncio.create_task(web_server.start())
    
    # 4. Поднимаем WebSocket аудио-сервер (порт 8765)
    async with websockets.serve(handle_client, "0.0.0.0", port):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
