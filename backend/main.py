import asyncio
import json
import os
import logging
import math
import struct
import websockets
from websockets.exceptions import ConnectionClosed

from ha_api import HomeAssistantAPI
from gemini_client import GeminiProxyClient
from google.genai import types
from phrase_manager import PhraseManager
from device_manager import DeviceManager
from mqtt_discovery import MQTTDiscoveryManager
from web_server import WebServer

phrase_manager = PhraseManager()
device_manager = DeviceManager()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("server")
audio_logger = logging.getLogger("audio")
tool_logger = logging.getLogger("tools")

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

options_path = "/data/options.json"
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
    "scripts (script), scenes (scene), and media players (media_player).\n\n"
    "DEVICE DISCOVERY: Always verify device names against the provided list of available Home Assistant entities before issuing commands.\n\n"
    "CURTAINS & BLINDS (COVER):\n"
    "- To open curtains or blinds: call call_ha_service with domain \"cover\", service \"open_cover\", entity_id.\n"
    "- To close curtains or blinds: call call_ha_service with domain \"cover\", service \"close_cover\", entity_id.\n"
    "- To stop curtains: call call_ha_service with domain \"cover\", service \"stop_cover\", entity_id.\n"
    "- To set specific opening percentage: call call_ha_service with domain \"cover\", service \"set_cover_position\", entity_id, and position (0 to 100).\n\n"
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

def get_options():
    global _runtime_options
    if _runtime_options is not None:
        opts = dict(_runtime_options)
    elif os.path.exists(options_path):
        try:
            with open(options_path, "r", encoding="utf-8") as f:
                _runtime_options = json.load(f)
                opts = dict(_runtime_options)
        except Exception as e:
            logger.error(f"Error loading options.json: {e}")
            opts = {}
    else:
        opts = {}

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
        "enable_barge_in": True
    }

    for k, v in defaults.items():
        if k not in opts or (isinstance(opts[k], str) and not opts[k].strip() and k.startswith("prompt_")):
            opts[k] = v

    _runtime_options = opts
    return dict(_runtime_options)

def save_options(new_options: dict):
    global _runtime_options
    opts = get_options()
    opts.update(new_options)
    _runtime_options = opts
    try:
        os.makedirs(os.path.dirname(options_path), exist_ok=True)
        with open(options_path, "w", encoding="utf-8") as f:
            json.dump(opts, f, indent=2, ensure_ascii=False)
        logger.info("Saved updated options to /data/options.json")
    except Exception as e:
        logger.warning(f"Failed to persist options to file: {e}")

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
    
    gemini_client = GeminiProxyClient(
        api_key=options.get("gemini_api_key"),
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
                                await phrase_manager.play_phrase(
                                    websocket, 
                                    phrase, 
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
                if not options.get("enable_media_ducking", True):
                    return
                try:
                    factor = float(options.get("ducking_volume_factor", 0.25))
                    playing_players = await ha_api.get_playing_media_players()
                    for p in playing_players:
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
                            if session_state.get("is_tool_pending"):
                                # КРИТИЧНО: Нельзя отправлять аудио, пока выполняется Tool Call, иначе сервер Gemini закроет соединение с ошибкой 1008
                                continue
                                
                            if not options.get("enable_barge_in", True) and session_state.get("is_gemini_speaking"):
                                # Игнорируем микрофон пока говорит ассистент, если перебивание выключено
                                continue
                                
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
                                
                                if msg_type == "register":
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
                                    session_state["first_audio_received"] = False
                                    phrase = phrase_manager.get_phrase("empty_noise")
                                    if phrase:
                                        try:
                                            await phrase_manager.play_phrase(websocket, phrase)
                                        except Exception:
                                            pass
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
                                        
                                        pcm_audio = part.inline_data.data
                                        audio_logger.debug(f"Sending {len(pcm_audio)} bytes audio chunk from Gemini to WS Client (chunked)")
                                    
                                        # Чанкуем аудио на сервере, чтобы ESP32 не падала от нехватки памяти
                                        CHUNK_SIZE = 2048
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
                                        if "error" in result:
                                            phrase = phrase_manager.get_phrase("device_error")
                                            if phrase:
                                                await phrase_manager.play_phrase(websocket, phrase)
                                            else:
                                                await websocket.send(ERROR_CHIME)
                                        else:
                                            await websocket.send(SUCCESS_CHIME)
                                        
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
                                        if "error" in result:
                                            await websocket.send(ERROR_CHIME)
                                        else:
                                            await websocket.send(SUCCESS_CHIME)
                                        
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
                    phrase = phrase_manager.get_phrase("network_error")
                    if phrase:
                        logger.info("Playing dynamic network_error phrase...")
                        try:
                            await phrase_manager.play_phrase(websocket, phrase)
                        except Exception:
                            pass
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

async def main():
    port = 8765
    logger.info(f"Porfiriy Backend Server starting on ws://0.0.0.0:{port} ...")
    
    # Загружаем / генерируем динамический кэш фраз Порфирия
    options = get_options()
    phrase_manager.load_from_cache()
    asyncio.create_task(phrase_manager.initialize(options))
    
    # 1. Запуск MQTT Discovery Manager
    ha_api = HomeAssistantAPI()
    mqtt_manager = MQTTDiscoveryManager(device_manager, options)
    asyncio.create_task(mqtt_manager.start())
    
    # 2. Запуск Ingress Web Server (порт 8099)
    web_server = WebServer(
        device_manager, 
        ha_api, 
        get_options, 
        options_save_callback=save_options, 
        phrase_manager=phrase_manager, 
        port=8099
    )
    asyncio.create_task(web_server.start())
    
    # 3. Поднимаем WebSocket аудио-сервер (порт 8765)
    async with websockets.serve(handle_client, "0.0.0.0", port):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
