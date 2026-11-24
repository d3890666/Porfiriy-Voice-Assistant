import asyncio
import collections
import json
import os
import logging
import math
import time
from typing import Optional
import websockets
from websockets.exceptions import ConnectionClosed

from audio_utils import (
    calculate_pcm_rms,
    rms_to_dbfs,
    scale_pcm16,
    pcm16_to_wav,
    get_earcon_pcm,
    SUCCESS_CHIME,
    ERROR_CHIME,
)
from config import (
    DEFAULT_GEMINI_MODEL,
    get_options,
    save_options,
    get_active_api_key,
    get_available_models,
    refresh_available_models,
)
from net_utils import get_local_addon_base_url
from prompt_builder import build_system_prompt
from tool_executor import ToolExecutor

from ha_api import HomeAssistantAPI
from gemini_client import GeminiProxyClient
from google.genai import types
from phrase_manager import PhraseManager
from device_manager import DeviceManager
from mqtt_discovery import MQTTDiscoveryManager
from web_server import WebServer
from wakeword_engine import WakeWordEngine

phrase_manager = PhraseManager()
device_manager = DeviceManager()
ha_api_singleton = HomeAssistantAPI()
tool_executor = ToolExecutor(ha_api_singleton)

# Серверный детектор вейкворда (один на весь процесс)
_WW_MODEL_PATH = "porfiriy.onnx"
_WW_DEFAULT_THRESHOLD = 0.94
ww_engine: Optional[WakeWordEngine] = None  # инициализируется в main()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("server")
audio_logger = logging.getLogger("audio")
tool_logger = logging.getLogger("tools")

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
        if device_type == "yandex_station" and client_mac not in device_manager.devices:
            reg_ip = (first_data.get("ip") or "").strip()
            if not reg_ip and websocket.remote_address:
                reg_ip = str(websocket.remote_address[0])
            for existing_mac, existing_dev in list(device_manager.devices.items()):
                if existing_dev.get("device_type") == "yandex_station":
                    ex_ip = (existing_dev.get("ip") or existing_dev.get("config", {}).get("ip") or "").strip()
                    if reg_ip and ex_ip == reg_ip:
                        logger.info(f"[YANDEX-WS] Matched station IP {reg_ip} to existing device '{existing_mac}' (instead of '{client_mac}')")
                        client_mac = existing_mac
                        first_data["mac"] = existing_mac
                        first_data["name"] = existing_dev.get("name", first_data.get("name"))
                        break
        device_manager.register_device(client_mac, first_data, ws=websocket)
        if device_type == "pc_streamer":
            logger.info(f"[STREAMER] Routing {client_mac} to server-side wake word handler.")
            await handle_pc_streamer(websocket, client_mac, first_data)
            return
        elif device_type == "yandex_station":
            logger.info(f"[YANDEX-WS] Routing {client_mac} to Yandex Station audio streamer.")
            await handle_yandex_station_streamer(websocket, client_mac, first_data)
            return
    elif isinstance(first_msg, bytes):
        # Редкий случай: клиент сразу льёт байты (старый debug_client)
        pass  # обработаем ниже вместе с остальными

    ha_api = ha_api_singleton
    
    # Проверяем, привязана ли уже эта колонка к комнате в HA
    area_name = await ha_api.get_device_area_name(client_mac)
    if area_name:
        device_manager.set_device_area(client_mac, area_name)
        logger.info(f"Connected device {client_mac} belongs to area: '{area_name}'")
    
    # 1. Формируем системный промпт и контекст устройств
    full_prompt = await build_system_prompt(options, ha_api, area_name=area_name, mode="satellite")
    logger.info(f"Built system prompt ({len(full_prompt)} chars) for {client_mac}.")
    
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
        model=options.get("gemini_model", DEFAULT_GEMINI_MODEL),
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
                                
                            # Если для устройства включен серверный вейкворд и колонка в покое — проверяем вейкворд через openWakeWord
                            dev_cfg = device_manager.get_device_config(client_mac)
                            if dev_cfg.get("wake_word_mode") == "server":
                                dev_obj = device_manager.get_device(client_mac) or {}
                                if dev_obj.get("state") == "idle" and not session_state.get("is_gemini_speaking") and not session_state.get("is_thinking"):
                                    if time.time() < session_state.get("cooldown_until", 0.0):
                                        continue
                                    if ww_engine and ww_engine.is_ready:
                                        dev_thresh = dev_cfg.get("ww_threshold")
                                        active_thresh = float(dev_thresh) if dev_thresh is not None else ww_engine.threshold
                                        score, peak, rms = ww_engine.process_chunk(message, client_mac, custom_threshold=active_thresh)
                                        device_manager.update_ww_score(client_mac, score, peak_score=peak, rms_dbfs=rms)
                                        if ww_engine.is_triggered(client_mac):
                                            preroll = ww_engine.consume_preroll(client_mac)
                                            ww_engine.reset(client_mac)
                                            device_manager.update_ww_score(client_mac, 0.0, peak_score=0.0, rms_dbfs=rms)
                                            logger.info(f"[WW-SERVER] Server-side wake word triggered for {client_mac} (score={score:.3f}, peak={peak:.3f})")
                                            session_state["utterance_buffer"] = collections.deque(maxlen=150)
                                            device_manager.set_device_state(client_mac, "listening")
                                            cancel_thinking_watchdog()
                                            if getattr(device_manager, "media_manager", None):
                                                asyncio.create_task(device_manager.media_manager.get_player(client_mac).pause())
                                            session_state["is_gemini_speaking"] = False
                                            session_state["is_tool_pending"] = False
                                            session_state["first_audio_sent"] = False
                                            session_state["first_audio_received"] = False
                                            await duck_media()
                                            try:
                                                await websocket.send(json.dumps({"type": "listen"}))
                                            except Exception:
                                                pass
                                            if preroll:
                                                async with gemini_send_lock:
                                                    await session.send_realtime_input(
                                                        audio=types.Blob(data=preroll, mime_type="audio/pcm;rate=16000")
                                                    )
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
                                    if getattr(device_manager, "media_manager", None):
                                        asyncio.create_task(device_manager.media_manager.get_player(client_mac).pause())
                                    
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
                                    if getattr(device_manager, "media_manager", None):
                                        asyncio.create_task(device_manager.media_manager.get_player(client_mac).pause())
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
                                    if getattr(device_manager, "media_manager", None):
                                        asyncio.create_task(device_manager.media_manager.get_player(client_mac).stop())
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
                                    
                                        dev_cfg = device_manager.get_device_config(client_mac)
                                        out_mode = dev_cfg.get("audio_output_mode", "stream")
                                        resp_player = dev_cfg.get("response_player")

                                        if out_mode == "external_player" and resp_player and resp_player != "auto":
                                            session_state.setdefault("external_audio_chunks", []).append(pcm_audio)
                                        else:
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
                                    session_state["cooldown_until"] = time.time() + 2.0
                                    if ww_engine:
                                        ww_engine.reset(client_mac)
                                    device_manager.set_device_state(client_mac, "idle")

                                    # Если настроен внешний плеер — воспроизводим WAV на медиаплеере Home Assistant
                                    dev_cfg = device_manager.get_device_config(client_mac)
                                    out_mode = dev_cfg.get("audio_output_mode", "stream")
                                    resp_player = dev_cfg.get("response_player")
                                    ext_chunks = session_state.get("external_audio_chunks", [])
                                    if out_mode == "external_player" and resp_player and resp_player != "auto" and ext_chunks:
                                        raw_pcm = b"".join(ext_chunks)
                                        wav_bytes = pcm16_to_wav(raw_pcm, sample_rate=24000)
                                        device_manager.save_response_wav(client_mac, wav_bytes)
                                        wav_url = f"{get_local_addon_base_url()}/api/virtual/{client_mac}/response.wav"
                                        logger.info(f"Routing Gemini response to external player {resp_player}: {wav_url}")
                                        asyncio.create_task(ha_api.call_service("media_player", "play_media", {
                                            "entity_id": resp_player,
                                            "media_content_id": wav_url,
                                            "media_content_type": "music"
                                        }))
                                        session_state["external_audio_chunks"] = []
                                        # Шлем легкий earcon на само устройство о завершении
                                        try:
                                            speaker_vol = float(dev_cfg.get("speaker_volume", 0.5))
                                            await websocket.send(scale_pcm16(SUCCESS_CHIME, speaker_vol))
                                        except Exception:
                                            pass

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
                        
                            # Обработка вызовов функций (Home Assistant / Music Assistant)
                            if response.tool_call:
                                session_state["is_tool_pending"] = True
                                cancel_thinking_watchdog()
                                await websocket.send(json.dumps({"type": "thinking"}))
                                tool_logger.info(f"RAW Tool Call from Gemini: {response.tool_call}")
                                function_responses = []
                                speaker_vol = float(device_manager.get_device_config(client_mac).get("speaker_volume", 0.5))

                                async def _esp_tool_feedback(kind: str, has_data: bool):
                                    if kind == "error":
                                        phrase = phrase_manager.get_phrase("device_error")
                                        if phrase:
                                            await phrase_manager.play_phrase(websocket, phrase, volume=speaker_vol)
                                        else:
                                            await websocket.send(scale_pcm16(ERROR_CHIME, speaker_vol))
                                    elif not has_data:
                                        await websocket.send(scale_pcm16(SUCCESS_CHIME, speaker_vol))

                                for fc in response.tool_call.function_calls:
                                    safe_result = await tool_executor.execute(
                                        fc.name,
                                        dict(fc.args) if fc.args else {},
                                        options,
                                        dev=device_manager.get_device(client_mac),
                                        on_feedback=_esp_tool_feedback,
                                    )
                                    function_responses.append(types.FunctionResponse(
                                        name=fc.name,
                                        id=fc.id,
                                        response=safe_result,
                                    ))

                                if function_responses:
                                    tool_logger.info(f"Sending Tool Responses: {function_responses}")
                                    async with gemini_send_lock:
                                        await session.send_tool_response(function_responses=function_responses)
                                    session_state["is_tool_pending"] = False
                                    session_state["is_gemini_speaking"] = False
                                    session_state["first_audio_sent"] = False
                                    start_thinking_watchdog()
                                    
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

            async def _force_wake_esp():
                logger.info(f"[ESP32] ⚡ Force wake triggered via Web UI for {client_mac}")
                session_state["utterance_buffer"] = collections.deque(maxlen=150)
                device_manager.set_device_state(client_mac, "listening")
                cancel_thinking_watchdog()
                session_state["is_gemini_speaking"] = False
                session_state["is_tool_pending"] = False
                session_state["first_audio_sent"] = False
                session_state["first_audio_received"] = False
                await duck_media()
                try:
                    await websocket.send(json.dumps({"type": "listen"}))
                except Exception:
                    pass
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
                    except Exception:
                        pass
                return True

            device_manager.register_wake_handler(client_mac, _force_wake_esp)

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
        device_manager.unregister_wake_handler(client_mac, _force_wake_esp)
        device_manager.set_device_offline(client_mac, ws=websocket)

async def handle_pc_streamer(websocket, client_mac: str, reg_data: dict):
    """
    Обрабатывает подключение pc_streamer клиента:
    - Непрерывно принимает PCM 16 kHz
    - Детектирует вейкворд через WakeWordEngine (openWakeWord ONNX)
    - При срабатывании — открывает сессию Gemini, собирает ответ
    - Ответ сохраняется как WAV и передаётся на HA media_player
    """
    options = get_options()
    ha_api = ha_api_singleton
    dev_config = device_manager.get_device_config(client_mac)
    response_player = dev_config.get("response_player") or options.get("default_media_player", "auto")
    area_name = reg_data.get("area_name") or device_manager.get_device_area(client_mac)
    ww_threshold = float(dev_config.get("ww_threshold", _WW_DEFAULT_THRESHOLD))

    logger.info(f"[STREAMER] {client_mac} listening. response_player={response_player}, threshold={ww_threshold}")
    device_manager.set_device_state(client_mac, "listening")

    # Буфер и очередь для стриминга аудио во время активной сессии Gemini
    session_active = False
    streamer_state = "idle"  # "idle", "listening", "thinking", "speaking"
    user_has_spoken = False
    last_speech_time = 0.0
    session_start_time = 0.0
    last_session_end_time = 0.0
    cooldown_seconds = 2.0
    audio_queue: Optional[asyncio.Queue] = None
    gemini_audio_buf: list = []

    async def _run_gemini_session(preroll: bytes):
        """Открывает Gemini Live сессию, в реальном времени стримит микрофон и воспроизводит ответ."""
        nonlocal session_active, audio_queue, streamer_state, user_has_spoken, last_speech_time, session_start_time
        session_active = True
        streamer_state = "listening"
        user_has_spoken = False
        session_start_time = time.time()
        last_speech_time = session_start_time
        device_manager.set_device_state(client_mac, "listening")

        api_key_str = (options.get("gemini_api_key") or "").strip()
        api_key = get_active_api_key(api_key_str)
        if not api_key:
            logger.error("[STREAMER] Gemini API key missing, cannot start session.")
            session_active = False
            audio_queue = None
            return

        # Формируем системный промпт
        full_prompt = await build_system_prompt(options, ha_api, area_name=area_name, mode="satellite")

        vad_silence = int(options.get("vad_silence_duration_ms", 600))
        gemini_client = GeminiProxyClient(
            api_key=api_key,
            system_prompt=full_prompt,
            ha_api=ha_api,
            voice_name=options.get("voice_name", "Charon"),
            model=options.get("gemini_model", DEFAULT_GEMINI_MODEL),
            enable_google_search=options.get("enable_google_search", True),
            vad_silence_duration_ms=vad_silence
        )

        pcm_response_chunks = []
        gemini_is_speaking = False

        async def _stream_mic_to_gemini(session):
            """Непрерывно перекачивает аудио из очереди микрофона в Gemini Live."""
            try:
                # 1. Отправляем pre-roll (звук до вейкворда)
                if preroll:
                    await session.send_realtime_input(
                        audio=types.Blob(data=preroll, mime_type="audio/pcm;rate=16000")
                    )
                # 2. Непрерывно пересылаем входящие куски голоса пользователя
                while session_active:
                    chunk = await audio_queue.get()
                    if chunk is None:
                        break
                    # Если Gemini в данный момент воспроизводит речь, не отправляем звук микрофона
                    # в сокет Gemini Live, чтобы не вызывать ложное самоперебивание (AEC/Barge-in lock)
                    if gemini_is_speaking:
                        continue
                    await session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                    )
            except asyncio.CancelledError:
                pass
            except Exception as e_stream:
                logger.debug(f"[STREAMER] Mic streaming worker note for {client_mac}: {e_stream}")

        async def _receive_from_gemini(session):
            nonlocal gemini_is_speaking, streamer_state, last_session_end_time
            """Принимает аудиоответ от Gemini до наступления turn_complete."""
            async for response in session.receive():
                if response.server_content and response.server_content.model_turn:
                    gemini_is_speaking = True
                    streamer_state = "speaking"
                    device_manager.set_device_state(client_mac, "speaking")
                    current_cfg = device_manager.get_device_config(client_mac)
                    active_vol = float(current_cfg.get("speaker_volume", 0.5))
                    active_out_mode = current_cfg.get("audio_output_mode", "stream")
                    for part in response.server_content.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            scaled_chunk = scale_pcm16(part.inline_data.data, active_vol)
                            pcm_response_chunks.append(scaled_chunk)
                            if active_out_mode == "stream":
                                try:
                                    await websocket.send(scaled_chunk)
                                except Exception:
                                    pass
                if response.tool_call:
                    logger.info(f"[STREAMER] RAW Tool Call from Gemini: {response.tool_call}")
                    function_responses = []
                    current_cfg = device_manager.get_device_config(client_mac)
                    active_vol = float(current_cfg.get("speaker_volume", 0.5))

                    async def _streamer_tool_feedback(kind: str, has_data: bool):
                        try:
                            if kind == "error":
                                await websocket.send(scale_pcm16(ERROR_CHIME, active_vol))
                            elif not has_data:
                                await websocket.send(scale_pcm16(SUCCESS_CHIME, active_vol))
                        except Exception:
                            pass

                    for fc in response.tool_call.function_calls:
                        safe_result = await tool_executor.execute(
                            fc.name,
                            dict(fc.args) if fc.args else {},
                            options,
                            dev=device_manager.get_device(client_mac),
                            on_feedback=_streamer_tool_feedback,
                        )
                        function_responses.append(types.FunctionResponse(
                            name=fc.name,
                            id=fc.id,
                            response=safe_result,
                        ))
                    if function_responses:
                        logger.info(f"[STREAMER] Sending Tool Responses: {function_responses}")
                        await session.send_tool_response(function_responses=function_responses)
                if response.server_content and getattr(response.server_content, "turn_complete", False):
                    gemini_is_speaking = False
                    streamer_state = "idle"
                    last_session_end_time = time.time()
                    if ww_engine:
                        ww_engine.reset(client_mac)
                    logger.info(f"[STREAMER] Gemini turn complete for {client_mac}.")
                    current_cfg = device_manager.get_device_config(client_mac)
                    active_out_mode = current_cfg.get("audio_output_mode", "stream")
                    if active_out_mode == "stream":
                        try:
                            await websocket.send(json.dumps({"type": "sleep"}))
                        except Exception:
                            pass
                    break

        sender_task = None
        try:
            async with gemini_client.connect() as session:
                sender_task = asyncio.create_task(_stream_mic_to_gemini(session))
                # Ограничиваем сессию таймаутом 35 секунд на случай молчания
                await asyncio.wait_for(_receive_from_gemini(session), timeout=35.0)
        except asyncio.TimeoutError:
            logger.info(f"[STREAMER] Gemini session completed by silence timeout for {client_mac}.")
        except Exception as e:
            logger.error(f"[STREAMER] Gemini session error for {client_mac}: {e}")
        finally:
            session_active = False
            streamer_state = "idle"
            last_session_end_time = time.time()
            if ww_engine:
                ww_engine.reset(client_mac)
            if audio_queue:
                audio_queue.put_nowait(None)
            if sender_task and not sender_task.done():
                sender_task.cancel()
            audio_queue = None
            device_manager.set_device_state(client_mac, "idle")

        if not pcm_response_chunks:
            logger.warning(f"[STREAMER] No audio received from Gemini for {client_mac}.")
            return

        # Упаковываем PCM в WAV и кэшируем (для Web UI и внешнего плеера)
        raw_pcm = b"".join(pcm_response_chunks)
        wav_bytes = pcm16_to_wav(raw_pcm, sample_rate=24000)
        device_manager.save_response_wav(client_mac, wav_bytes)

        # Сохраняем голосовую реплику пользователя для кнопки «💬 Реплика»
        if preroll or gemini_audio_buf:
            device_manager.save_last_utterance(client_mac, preroll + b"".join(gemini_audio_buf))
            gemini_audio_buf.clear()

        current_cfg = device_manager.get_device_config(client_mac)
        out_mode = current_cfg.get("audio_output_mode", "stream")
        if out_mode == "external_player":
            resp_p = current_cfg.get("response_player") or response_player
            player = resp_p if resp_p and resp_p != "auto" else None
            if not player:
                logger.warning(f"[STREAMER] No response_player configured for {client_mac}. Set it in device settings.")
                return

            wav_url = f"{get_local_addon_base_url()}/api/virtual/{client_mac}/response.wav"
            logger.info(f"[STREAMER] Playing response on {player}: {wav_url}")
            try:
                await ha_api.call_service("media_player", "play_media", {
                    "entity_id": player,
                    "media_content_id": wav_url,
                    "media_content_type": "music"
                })
                # Сигнал готовности на стример
                try:
                    active_vol = float(current_cfg.get("speaker_volume", 0.5))
                    await websocket.send(scale_pcm16(SUCCESS_CHIME, active_vol))
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"[STREAMER] Failed to play media on {player}: {e}")

    async def _force_wake_streamer():
        nonlocal session_active, audio_queue, streamer_state, user_has_spoken, last_speech_time, session_start_time
        if session_active:
            logger.info(f"[STREAMER] Session already active for {client_mac}, ignoring duplicate force wake.")
            return True
        logger.info(f"[STREAMER] ⚡ Force wake triggered via Web UI for {client_mac}")
        try:
            await websocket.send(json.dumps({"type": "listen"}))
        except Exception:
            pass
        try:
            speaker_vol = float(device_manager.get_device_config(client_mac).get("speaker_volume", 0.5))
            await websocket.send(scale_pcm16(SUCCESS_CHIME, speaker_vol))
        except Exception:
            pass
        gemini_audio_buf.clear()
        audio_queue = asyncio.Queue()
        asyncio.create_task(_run_gemini_session(b""))
        return True

    device_manager.register_wake_handler(client_mac, _force_wake_streamer)

    try:
        async for message in websocket:
            if isinstance(message, bytes):
                # Проверяем, активен ли режим отладки/теста микрофона для этого устройства
                if device_manager.is_mic_test_active(client_mac):
                    device_manager.append_mic_test_chunk(client_mac, message)
                    if time.time() >= device_manager.get_mic_test_end_time(client_mac):
                        device_manager.finish_mic_test(client_mac)
                    continue

                # Если сессия диалога с Gemini активна — энергетический VAD конца речи
                if session_active:
                    dev_cfg = device_manager.get_device_config(client_mac)
                    sil_thresh = float(dev_cfg.get("silence_threshold_energy", 180))
                    sil_timeout = float(dev_cfg.get("silence_timeout_ms", 700)) / 1000.0
                    listen_to = float(dev_cfg.get("listen_timeout_s", 6))
                    raw_rms = calculate_pcm_rms(message)

                    if streamer_state == "listening":
                        now = time.time()
                        if raw_rms >= sil_thresh:
                            user_has_spoken = True
                            last_speech_time = now
                            if audio_queue is not None:
                                audio_queue.put_nowait(message)
                                gemini_audio_buf.append(message)
                        else:
                            if user_has_spoken:
                                if now - last_speech_time > sil_timeout:
                                    logger.info(f"[STREAMER] End of speech detected for {client_mac} (silence > {sil_timeout*1000:.0f}ms). Switching to THINKING.")
                                    streamer_state = "thinking"
                                    device_manager.set_device_state(client_mac, "thinking")
                                    try:
                                        await websocket.send(json.dumps({"type": "thinking"}))
                                    except Exception:
                                        pass
                                    if audio_queue is not None:
                                        silence_chunk = b"\x00" * 1024
                                        for _ in range(28):
                                            audio_queue.put_nowait(silence_chunk)
                                else:
                                    if audio_queue is not None:
                                        audio_queue.put_nowait(message)
                                        gemini_audio_buf.append(message)
                            else:
                                if now - session_start_time > listen_to:
                                    logger.info(f"[STREAMER] Listen timeout ({listen_to}s) with no speech for {client_mac}. Going to sleep.")
                                    session_active = False
                                    streamer_state = "idle"
                                    last_session_end_time = now
                                    if ww_engine:
                                        ww_engine.reset(client_mac)
                                    device_manager.set_device_state(client_mac, "idle")
                                    if audio_queue:
                                        audio_queue.put_nowait(None)
                                    audio_queue = None
                                    try:
                                        await websocket.send(json.dumps({"type": "sleep"}))
                                    except Exception:
                                        pass
                                else:
                                    if audio_queue is not None:
                                        audio_queue.put_nowait(message)
                                        gemini_audio_buf.append(message)

                    calc_dbfs = float(round(20.0 * math.log10(max(1.0, raw_rms) / 32768.0), 1))
                    device_manager.update_ww_score(client_mac, 0.0, peak_score=0.0, rms_dbfs=calc_dbfs)

                # Прогоняем PCM через вейкворд (когда нет активной сессии и прошел кулдаун)
                now = time.time()
                is_cooldown = (now - last_session_end_time < cooldown_seconds)
                if not session_active and not is_cooldown and ww_engine and ww_engine.is_ready:
                    dev_cfg = device_manager.get_device_config(client_mac)
                    dev_thresh = dev_cfg.get("ww_threshold")
                    active_thresh = float(dev_thresh) if dev_thresh is not None else ww_engine.threshold
                    score, peak, rms = ww_engine.process_chunk(message, client_mac, custom_threshold=active_thresh)
                    device_manager.update_ww_score(client_mac, score, peak_score=peak, rms_dbfs=rms)

                    if ww_engine.is_triggered(client_mac):
                        preroll = ww_engine.consume_preroll(client_mac)
                        # Сбрасываем внутренний буфер и скор нейросети во избежание залипания
                        ww_engine.reset(client_mac)
                        device_manager.update_ww_score(client_mac, 0.0, peak_score=0.0, rms_dbfs=rms)
                        # Отправляем звуковой сигнал chime клиенту
                        try:
                            speaker_vol = float(device_manager.get_device_config(client_mac).get("speaker_volume", 0.5))
                            await websocket.send(scale_pcm16(SUCCESS_CHIME, speaker_vol))
                        except Exception:
                            pass
                        gemini_audio_buf.clear()
                        audio_queue = asyncio.Queue()
                        asyncio.create_task(_run_gemini_session(preroll))
                elif not session_active:
                    # Резервный расчет громкости микрофона (гарантирует живую шкалу даже если модель не загружена)
                    raw_rms = calculate_pcm_rms(message)
                    calc_dbfs = float(round(20.0 * math.log10(max(1.0, raw_rms) / 32768.0), 1))
                    device_manager.update_ww_score(client_mac, 0.0, peak_score=0.0, rms_dbfs=calc_dbfs)

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
                            await device_manager.update_device_config(client_mac, {"ww_threshold": ww_threshold})
                        if "response_player" in cfg:
                            response_player = cfg["response_player"]
                            await device_manager.update_device_config(client_mac, {"response_player": response_player})
                except Exception as e:
                    logger.debug(f"[STREAMER] Text message parse error: {e}")

    except ConnectionClosed:
        logger.info(f"[STREAMER] {client_mac} disconnected.")
    except Exception as e:
        logger.error(f"[STREAMER] Unexpected error for {client_mac}: {e}")
    finally:
        device_manager.unregister_wake_handler(client_mac)
        if ww_engine:
            ww_engine.reset(client_mac)
        device_manager.set_device_offline(client_mac, ws=websocket)


async def handle_yandex_station_streamer(websocket, client_mac: str, reg_data: dict):
    """
    Обрабатывает нативное WebSocket-подключение Яндекс Станции 2 (`porfiriy-stationd`):
    - В режиме покоя (`idle`) локальный openWakeWord на Станции слушает микрофон,
      а по WebSocket приходят только heartbeat и телеметрия (`ww_telemetry`) для мини-отладки в UI.
    - При локальной сработке вейкворда (`wake_word_detected`) открывается сессия Gemini Live API
      (`bidiGenerateContent`), принимается pre-roll + живой 16 kHz PCM голос.
    - Ответ 24 kHz PCM (либо короткий earcon: щелчок громкости / «тудун» умного дома)
      стримится обратно на Станцию для воспроизведения через ALSA `default` (`Master`).
    - Синхронизирует состояния (`idle`/`listening`/`thinking`/`speaking`) для LED-панели Станции.
    """
    import base64

    ha_api = ha_api_singleton
    dev_config = device_manager.get_device_config(client_mac)
    area_name = reg_data.get("area_name") or device_manager.get_device_area(client_mac)
    speaker_ip = reg_data.get("ip") or dev_config.get("ip") or ""

    logger.info(f"[YANDEX-WS] {client_mac} connected (porfiriy-stationd). ip={speaker_ip}")
    device_manager.set_device_state(client_mac, "idle")

    session_active = False
    audio_queue: Optional[asyncio.Queue] = None
    gemini_is_speaking = False

    async def _send_json(payload: dict):
        try:
            await websocket.send(json.dumps(payload))
        except Exception:
            pass

    # Сразу отправляем Станции актуальный конфиг (openWakeWord + DSP + LED)
    try:
        opts_init = get_options()
        init_cfg = dict(device_manager.get_device_config(client_mac))
        init_cfg["earcon_wake_preset"] = opts_init.get("earcon_wake_preset", "soft_chime")
        await _send_json({"type": "set_config", "config": init_cfg})
    except Exception:
        pass

    async def _run_gemini_session():
        nonlocal session_active, audio_queue, gemini_is_speaking
        options = get_options()
        session_active = True
        gemini_is_speaking = False
        device_manager.set_device_state(client_mac, "listening")
        await _send_json({"type": "state", "state": "listening"})

        # Опциональный мягкий сигнал активации «слушаю»
        wake_preset = options.get("earcon_wake_preset", "soft_chime")
        if wake_preset and wake_preset != "none":
            wake_pcm = get_earcon_pcm("wake", wake_preset, sample_rate=24000, volume=1.0)
            if wake_pcm:
                try:
                    await websocket.send(wake_pcm)
                except Exception:
                    pass

        api_key_str = (options.get("gemini_api_key") or "").strip()
        api_key = get_active_api_key(api_key_str)
        if not api_key:
            logger.error("[YANDEX-WS] Gemini API key missing, cannot start session.")
            session_active = False
            device_manager.set_device_state(client_mac, "idle")
            await _send_json({"type": "state", "state": "idle"})
            return

        station_ctx = (
            "### HARDWARE & SATELLITE CONTEXT:\n"
            "- Device Type: Yandex Station 2 (Native Local openWakeWord + WebSocket Live Audio Stream).\n"
            "- Acoustic Input: Real-time 16kHz PCM stream directly from Station microphones (after DC-blocker & AEC).\n"
            "- Speech Directive: Output strictly spoken, natural Russian. 1-2 concise spoken sentences."
        )
        full_prompt = await build_system_prompt(
            options, ha_api, area_name=area_name, mode="satellite", extra_context=station_ctx
        )

        vad_silence = int(options.get("vad_silence_duration_ms", 600))
        gemini_client = GeminiProxyClient(
            api_key=api_key,
            system_prompt=full_prompt,
            ha_api=ha_api,
            voice_name=options.get("voice_name", "Charon"),
            model=options.get("gemini_model", DEFAULT_GEMINI_MODEL),
            enable_google_search=options.get("enable_google_search", True),
            vad_silence_duration_ms=vad_silence,
            response_modalities=["AUDIO"],
        )

        pcm_response_chunks = []
        turn_state = {
            "tool_executed": False,
            "playback_changed": False,
            "volume_changed": False,
            "simple_smarthome_executed": False,
            "earcon_played": False,
        }

        async def _stream_mic_to_gemini(session):
            try:
                while session_active:
                    chunk = await audio_queue.get()
                    if chunk is None:
                        break
                    if gemini_is_speaking:
                        continue
                    await session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                    )
            except asyncio.CancelledError:
                pass
            except Exception as e_stream:
                logger.debug(f"[YANDEX-WS] Mic stream worker note: {e_stream}")

        async def _receive_from_gemini(session):
            nonlocal gemini_is_speaking
            async for response in session.receive():
                if response.tool_call:
                    logger.info(f"[YANDEX-WS] Tool Call from Gemini: {response.tool_call}")
                    device_manager.set_device_state(client_mac, "thinking")
                    await _send_json({"type": "state", "state": "thinking"})
                    function_responses = []
                    for fc in response.tool_call.function_calls:
                        tool_res = await tool_executor.execute(
                            fc.name,
                            dict(fc.args) if fc.args else {},
                            options,
                            dev=device_manager.get_device(client_mac),
                            turn_state=turn_state,
                        )
                        function_responses.append(types.FunctionResponse(
                            name=fc.name,
                            id=fc.id,
                            response=tool_res,
                        ))
                    await session.send_tool_response(function_responses=function_responses)

                    # Тихое подтверждение earcon (щелчок / «тудун») вместо голоса для громкости и простого УД
                    earcon_cat = None
                    earcon_preset = None
                    if turn_state.get("volume_changed") and options.get("earcon_volume_enabled", True):
                        earcon_cat = "volume"
                        earcon_preset = options.get("earcon_volume_preset", "alice_click")
                    elif turn_state.get("simple_smarthome_executed") and options.get("earcon_smarthome_enabled", True):
                        earcon_cat = "smarthome"
                        earcon_preset = options.get("earcon_smarthome_preset", "marimba_tudun")

                    if earcon_cat and earcon_preset:
                        earcon_pcm = get_earcon_pcm(earcon_cat, earcon_preset, sample_rate=24000, volume=1.0)
                        if earcon_pcm:
                            gemini_is_speaking = True
                            turn_state["earcon_played"] = True
                            pcm_response_chunks.append(earcon_pcm)
                            try:
                                await websocket.send(earcon_pcm)
                            except Exception:
                                pass
                            logger.info(f"[YANDEX-WS] Played earcon {earcon_cat}/{earcon_preset} instead of voice reply")
                            gemini_is_speaking = False
                            await _send_json({"type": "turn_complete"})
                            break
                    continue

                if response.server_content and response.server_content.model_turn:
                    if not gemini_is_speaking:
                        gemini_is_speaking = True
                        device_manager.set_device_state(client_mac, "speaking")
                        await _send_json({"type": "state", "state": "speaking"})
                    for part in response.server_content.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            chunk = part.inline_data.data
                            pcm_response_chunks.append(chunk)
                            try:
                                await websocket.send(chunk)
                            except Exception:
                                pass

                if response.server_content and response.server_content.turn_complete:
                    gemini_is_speaking = False
                    logger.info(f"[YANDEX-WS] Gemini turn complete for {client_mac}.")
                    await _send_json({"type": "turn_complete"})
                    break

        sender_task = None
        try:
            async with gemini_client.connect() as session:
                sender_task = asyncio.create_task(_stream_mic_to_gemini(session))
                await asyncio.wait_for(_receive_from_gemini(session), timeout=45.0)
        except asyncio.TimeoutError:
            logger.info(f"[YANDEX-WS] Session timed out for {client_mac}.")
            await _send_json({"type": "turn_complete"})
        except Exception as e:
            logger.error(f"[YANDEX-WS] Gemini session error for {client_mac}: {e}")
            await _send_json({"type": "turn_complete"})
        finally:
            session_active = False
            gemini_is_speaking = False
            device_manager.set_device_state(client_mac, "idle")
            await _send_json({"type": "state", "state": "idle"})
            if audio_queue:
                audio_queue.put_nowait(None)
            if sender_task and not sender_task.done():
                sender_task.cancel()
            audio_queue = None

        if pcm_response_chunks:
            raw_pcm = b"".join(pcm_response_chunks)
            wav_bytes = pcm16_to_wav(raw_pcm, sample_rate=24000)
            device_manager.save_response_wav(client_mac, wav_bytes)

    try:
        async for message in websocket:
            if isinstance(message, bytes):
                if client_mac and device_manager.is_mic_test_active(client_mac):
                    device_manager.append_mic_test_chunk(client_mac, message)
                    if time.time() >= device_manager.get_mic_test_end_time(client_mac):
                        device_manager.finish_mic_test(client_mac)
                    continue
                if session_active and audio_queue is not None:
                    audio_queue.put_nowait(message)

            elif isinstance(message, str):
                try:
                    data = json.loads(message)
                    mtype = data.get("type")
                    if mtype == "wake_word_detected":
                        if not session_active:
                            logger.info(
                                f"[YANDEX-WS] Local wake word detected on {client_mac} "
                                f"(score={data.get('score')}, infer_ms={data.get('infer_ms')})"
                            )
                            audio_queue = asyncio.Queue()
                            asyncio.create_task(_run_gemini_session())
                    elif mtype == "end_of_speech":
                        if session_active and audio_queue is not None:
                            device_manager.set_device_state(client_mac, "thinking")
                            await _send_json({"type": "state", "state": "thinking"})
                            silence = b"\x00" * 1024
                            for _ in range(24):
                                audio_queue.put_nowait(silence)
                    elif mtype == "ww_telemetry":
                        device_manager.update_ww_score(
                            client_mac,
                            score=float(data.get("score", 0.0)),
                            peak_score=float(data.get("peak", 0.0)),
                            rms_dbfs=float(data.get("rms_dbfs", -60.0)),
                            infer_ms=float(data.get("infer_ms", 0.0)) if data.get("infer_ms") is not None else None,
                        )
                    elif mtype == "mic_dump_wav":
                        wav_b64 = data.get("wav_b64", "")
                        if wav_b64:
                            raw_wav = base64.b64decode(wav_b64)
                            device_manager.mic_test_results[client_mac.strip().lower()] = {
                                "status": "ready",
                                "wav": raw_wav,
                                "timestamp": time.time(),
                                "analysis": {"quality": "station_aec_dump", "duration_s": round(float(data.get("duration_s", 3.0)), 1)},
                            }
                            logger.info(f"[YANDEX-WS] Saved 3s AEC mic dump for {client_mac} ({len(raw_wav)} bytes)")
                    elif mtype == "heartbeat":
                        device_manager.update_heartbeat(
                            client_mac,
                            rssi=int(data.get("rssi", -40)),
                            uptime=int(data.get("uptime", 0)),
                            state=data.get("state", "idle"),
                        )
                except Exception as e_msg:
                    logger.debug(f"[YANDEX-WS] Text message parse note: {e_msg}")

    except ConnectionClosed:
        logger.info(f"[YANDEX-WS] {client_mac} disconnected.")
    except Exception as e:
        logger.error(f"[YANDEX-WS] Error for {client_mac}: {e}")
    finally:
        session_active = False
        if audio_queue:
            audio_queue.put_nowait(None)
        device_manager.set_device_offline(client_mac, ws=websocket)


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
    ha_api = ha_api_singleton
    mqtt_manager = MQTTDiscoveryManager(device_manager, options)
    asyncio.create_task(mqtt_manager.start())
    
    # 3. Запуск Ingress Web Server (порт 8099)
    web_server = WebServer(
        device_manager, 
        ha_api, 
        get_options, 
        options_save_callback=save_options, 
        phrase_manager=phrase_manager, 
        models_callback=get_available_models,
        refresh_models_callback=refresh_available_models,
        ww_engine_callback=lambda: ww_engine,
        port=8099
    )
    asyncio.create_task(web_server.start())
    
    # 4. Поднимаем WebSocket аудио-сервер (порт 8765)
    try:
        async with websockets.serve(handle_client, "0.0.0.0", port):
            await asyncio.Future()  # run forever
    finally:
        mqtt_manager.stop()

if __name__ == "__main__":
    asyncio.run(main())
