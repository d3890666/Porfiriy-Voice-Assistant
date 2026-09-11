import asyncio
import json
import websockets
import sys
import argparse

async def send_text(ip: str, text: str):
    uri = f"ws://{ip}:8765"
    print(f"Connecting to {uri} ...")
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected! Sending text command...")
            payload = json.dumps({"text": text})
            await websocket.send(payload)
            print(f"--> Sent: {text}")
            
            print("\nListening for server responses (audio ignored). Press Ctrl+C to exit.")
            while True:
                response = await websocket.recv()
                if isinstance(response, bytes):
                    # Если сервер возвращает аудио, просто игнорируем его, 
                    # чтобы не засорять консоль
                    pass
                else:
                    print(f"<-- Received text message from server: {response}")
    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception as e:
        print(f"Connection error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send text command to Porfiriy Backend")
    parser.add_argument("command", nargs="+", help="Text command to send (e.g. 'включи свет на кухне')")
    parser.add_argument("--ip", default="192.168.1.1", help="IP address of the Home Assistant server (default: 192.168.1.1)")
    
    args = parser.parse_args()
    text_command = " ".join(args.command)
    
    # Для Windows может понадобиться
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    asyncio.run(send_text(args.ip, text_command))
