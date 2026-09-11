import asyncio
import json
import os
import logging
import websockets
from websockets.exceptions import ConnectionClosed

from ha_api import HomeAssistantAPI
from gemini_client import GeminiProxyClient
from google.genai import types

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

options_path = "/data/options.json"

def get_options():
    if os.path.exists(options_path):
        with open(options_path, "r") as f:
            return json.load(f)
    return {
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "gemini_model": "gemini-2.0-flash-exp",
        "system_prompt": "Ты умный голосовой помощник Порфирий, интегрированный в Умный Дом.",
        "voice_name": "Zephyr"
    }

async def handle_client(websocket):
    logger.info(f"Client connected from {websocket.remote_address}")
    options = get_options()
    
    ha_api = HomeAssistantAPI()
    
    # 1. Формируем контекст устройств
    devices_text = await ha_api.get_filtered_entities()
    full_prompt = f"{options.get('system_prompt', '')}\n\nНиже список доступных устройств Умного Дома:\n{devices_text}"
    logger.info(f"Loaded {len(devices_text.splitlines())} HA entities into the system prompt.")
    
    gemini_client = GeminiProxyClient(
        api_key=options.get("gemini_api_key"),
        system_prompt=full_prompt,
        ha_api=ha_api,
        voice_name=options.get("voice_name", "Zephyr"),
        model=options.get("gemini_model", "gemini-2.0-flash-exp")
    )
    
    try:
        async with gemini_client.connect() as session, asyncio.TaskGroup() as tg:
            
            async def receive_from_client():
                """Слушает входящие аудио-чанки (PCM) от WebSocket клиента (ПК/ESP32) и шлет их в Gemini."""
                try:
                    async for message in websocket:
                        if isinstance(message, bytes):
                            await session.send(input={"data": message, "mime_type": "audio/pcm"})
                except ConnectionClosed:
                    logger.info("Client disconnected (WS read)")
                except Exception as e:
                    logger.error(f"Error reading WS client: {e}")

            async def receive_from_gemini():
                """Слушает ответы от Gemini, пересылает аудио клиенту и исполняет Tool Calls (HA)."""
                try:
                    async for response in session.receive():
                        # Обработка аудио потока от модели
                        if response.server_content and response.server_content.model_turn:
                            for part in response.server_content.model_turn.parts:
                                if part.inline_data and part.inline_data.data:
                                    # Пересылаем сырой PCM аудио-чанк обратно клиенту
                                    await websocket.send(part.inline_data.data)
                        
                        # Обработка вызовов функций (Home Assistant)
                        if response.tool_calls:
                            for tool_call in response.tool_calls:
                                name = tool_call.function_call.name
                                if name == "call_ha_service":
                                    args = tool_call.function_call.args
                                    domain = args.get("domain")
                                    service = args.get("service")
                                    entity_id = args.get("entity_id")
                                    
                                    logger.info(f"Gemini Calling Tool: {domain}.{service} on {entity_id}")
                                    
                                    # Выполняем действие в Home Assistant
                                    result = await ha_api.call_service(
                                        domain=domain,
                                        service=service,
                                        service_data={"entity_id": entity_id}
                                    )
                                    
                                    logger.info(f"HA Action Result: {result}")
                                    
                                    # Формируем и отправляем ответ обратно в Gemini
                                    tool_response = types.LiveClientContent(
                                        turn_complete=True,
                                        client_content=types.ClientContent(
                                            turns=[
                                                types.Content(
                                                    parts=[
                                                        types.Part.from_function_response(
                                                            name=name,
                                                            response={"result": result}
                                                        )
                                                    ]
                                                )
                                            ]
                                        )
                                    )
                                    await session.send(input=tool_response)
                                    
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
