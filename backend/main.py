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

def get_options():
    if os.path.exists(options_path):
        with open(options_path, "r") as f:
            return json.load(f)
    return {
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "gemini_model": "gemini-2.0-flash-exp",
        "system_prompt": "Ты умный голосовой помощник Порфирий, интегрированный в Умный Дом.",
        "voice_name": "Zephyr",
        "debug_mode": False,
        "enable_google_search": True,
        "vad_silence_duration_ms": 600,
        "enable_barge_in": True
    }

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
        
    logger.info(f"Client connected from {websocket.remote_address}")
    
    ha_api = HomeAssistantAPI()
    
    # 1. Формируем контекст устройств
    devices_text = await ha_api.get_filtered_entities()
    system_prompt = options.get('system_prompt', '')
    full_prompt = f"{system_prompt}\n\nНиже список доступных устройств Умного Дома:\n{devices_text}"
    logger.info(f"Loaded {len(devices_text.splitlines())} HA entities into the system prompt:\n{devices_text}")
    
    gemini_client = GeminiProxyClient(
        api_key=options.get("gemini_api_key"),
        system_prompt=full_prompt,
        ha_api=ha_api,
        voice_name=options.get("voice_name", "Zephyr"),
        model=options.get("gemini_model", "gemini-2.0-flash-exp"),
        enable_google_search=options.get("enable_google_search", True),
        vad_silence_duration_ms=options.get("vad_silence_duration_ms", 600)
    )
    
    try:
        async with gemini_client.connect() as session, asyncio.TaskGroup() as tg:
            
            session_state = {
                "is_gemini_speaking": False,
                "first_audio_received": False,
                "first_audio_sent": False,
                "is_tool_pending": False
            }
            
            async def receive_from_client():
                """Слушает входящие аудио-чанки (PCM) от WebSocket клиента (ПК/ESP32) и шлет их в Gemini."""
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
                                logger.info("Started receiving audio stream from microphone...")
                                session_state["first_audio_received"] = True
                                
                            audio_logger.debug(f"Received {len(message)} bytes audio chunk from WS Client, sending to Gemini")
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
                                if "text" in data:
                                    logger.info(f"Received text input from WS Client: {data['text']}")
                                    await session.send_client_content(
                                        turns=[types.Content(parts=[types.Part.from_text(text=data['text'])])],
                                        turn_complete=True
                                    )
                            except Exception as e:
                                logger.error(f"Error parsing text message: {e}")
                except ConnectionClosed:
                    logger.info("Client disconnected (WS read)")
                except Exception as e:
                    logger.error(f"Error reading WS client: {e}")

            async def receive_from_gemini():
                """Слушает ответы от Gemini, пересылает аудио клиенту и исполняет Tool Calls (HA)."""
                try:
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
                                        
                                    audio_logger.debug(f"Sending {len(part.inline_data.data)} bytes audio chunk from Gemini to WS Client")
                                    # Пересылаем сырой PCM аудио-чанк обратно клиенту
                                    await websocket.send(part.inline_data.data)
                                    
                        # Обработка транскрипции и состояния
                        content = response.server_content
                        if content:
                            if getattr(content, "turn_complete", False):
                                logger.info("Gemini finished turn. Mic is now OPEN.")
                                session_state["is_gemini_speaking"] = False
                                # Сбрасываем флаг отправки аудио для следующего ответа
                                session_state["first_audio_sent"] = False
                                
                            # Обработка прерывания
                            if getattr(content, "interrupted", False):
                                logger.info("Gemini Interrupted by User (Barge-in)!")
                                await websocket.send(json.dumps({"type": "interrupted"}))
                                
                            if getattr(content, "input_transcription", None):
                                logger.info(f"User Speech Recognized: {content.input_transcription.text}")
                            if getattr(content, "output_transcription", None):
                                logger.info(f"Gemini Speech: {content.output_transcription.text}")
                        
                        # Обработка вызовов функций (Home Assistant)
                        if response.tool_call:
                            session_state["is_tool_pending"] = True
                            tool_logger.info(f"RAW Tool Call from Gemini: {response.tool_call}")
                            function_responses = []
                            for fc in response.tool_call.function_calls:
                                name = fc.name
                                if name == "call_ha_service":
                                    args = fc.args
                                    domain = args.get("domain")
                                    service = args.get("service")
                                    entity_id = args.get("entity_id")
                                    
                                    tool_logger.info(f"Gemini Calling Tool: {domain}.{service} on {entity_id}")
                                    
                                    # Выполняем действие в Home Assistant
                                    result = await ha_api.call_service(
                                        domain=domain,
                                        service=service,
                                        service_data={"entity_id": entity_id}
                                    )
                                    
                                    tool_logger.info(f"HA Action Result: {result}")
                                    
                                    # Отправляем звуковой отклик (Earcon) клиенту напрямую
                                    if "error" in result:
                                        await websocket.send(ERROR_CHIME)
                                    else:
                                        await websocket.send(SUCCESS_CHIME)
                                    
                                    function_responses.append(types.FunctionResponse(
                                        name=fc.name,
                                        id=fc.id,
                                        response={"result": result}
                                    ))
                                    
                                elif name == "search_music_assistant":
                                    args = fc.args
                                    tool_logger.info(f"Gemini Calling MA Search: {args}")
                                    search_data = dict(args)
                                    # HA API expects media_type to be a list if provided
                                    if "media_type" in search_data:
                                        search_data["media_type"] = [search_data["media_type"]]
                                        
                                    result = await ha_api.call_service_ws(
                                        domain="mass",
                                        service="search",
                                        service_data=search_data,
                                        return_response=True
                                    )
                                    
                                    simplified_result = []
                                    if isinstance(result, dict) and not "error" in result:
                                        for cat, items in result.items():
                                            if isinstance(items, list):
                                                for item in items[:5]: # top 5 per category
                                                    simplified_result.append({
                                                        "name": item.get("name"),
                                                        "uri": item.get("uri"),
                                                        "type": cat
                                                    })
                                        result = simplified_result if simplified_result else {"result": "Ничего не найдено"}
                                        
                                    tool_logger.info(f"MA Search Result: {result}")
                                    function_responses.append(types.FunctionResponse(
                                        name=fc.name,
                                        id=fc.id,
                                        response={"result": result}
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
                                        
                                    function_responses.append(types.FunctionResponse(
                                        name=fc.name,
                                        id=fc.id,
                                        response={"result": result}
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
                                await session.send_tool_response(function_responses=function_responses)
                                session_state["is_tool_pending"] = False
                                    
                except ConnectionClosed:
                    logger.info("Client disconnected (Gemini read)")
                except Exception as e:
                    logger.error(f"Error receiving from Gemini: {e}")

            # Запускаем задачи параллельно
            tg.create_task(receive_from_client())
            tg.create_task(receive_from_gemini())
            
    except Exception as e:
        logger.error(f"Gemini Session Setup Error: {e}")
        await websocket.close()

async def main():
    port = 8765
    logger.info(f"Porfiriy Backend Server starting on ws://0.0.0.0:{port} ...")
    
    # Поднимаем WebSocket сервер
    async with websockets.serve(handle_client, "0.0.0.0", port):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
