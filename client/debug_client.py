import asyncio
import websockets
import pyaudio
import webrtcvad
import argparse
import logging
import queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

FORMAT = pyaudio.paInt16
CHANNELS = 1
SEND_RATE = 16000
RECEIVE_RATE = 24000
CHUNK = 320 # 20ms of 16kHz audio for WebRTC VAD

class DebugClient:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.pya = pyaudio.PyAudio()
        self.vad = webrtcvad.Vad(3) # Aggressiveness level from 0 to 3 (3 is most aggressive)
        self.play_queue = queue.Queue()
        self.is_playing = False

    async def _read_mic_and_send(self, ws):
        stream = self.pya.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SEND_RATE,
            input=True,
            frames_per_buffer=CHUNK
        )
        logger.info("Microphone capturing started (16kHz).")
        
        while True:
            try:
                # Читаем чанк с микрофона
                data = await asyncio.to_thread(stream.read, CHUNK, exception_on_overflow=False)
                
                # Отправляем сырой PCM на сервер
                await ws.send(data)
                await asyncio.sleep(0.001)
            except Exception as e:
                logger.error(f"Mic reading error: {e}")
                break

    async def _receive_and_play(self, ws):
        stream = self.pya.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RECEIVE_RATE,
            output=True
        )
        logger.info("Speaker playback started (24kHz).")
        
        # Асинхронная функция для воспроизведения из очереди, чтобы не блокировать получение данных из WS
        async def play_audio_from_queue():
            while True:
                if not self.play_queue.empty():
                    self.is_playing = True
                    data = self.play_queue.get_nowait()
                    await asyncio.to_thread(stream.write, data)
                else:
                    self.is_playing = False
                    await asyncio.sleep(0.01)
                    
        asyncio.create_task(play_audio_from_queue())
        
        while True:
            try:
                message = await ws.recv()
                if isinstance(message, bytes):
                    # Кладем пришедший аудио-ответ Gemini в очередь на воспроизведение
                    self.play_queue.put(message)
                else:
                    logger.info(f"Server sent text: {message}")
            except Exception as e:
                logger.error(f"WS receive error: {e}")
                break

    async def _read_console_and_send(self, ws):
        import json
        import sys
        loop = asyncio.get_running_loop()
        while True:
            # Читаем строку из консоли асинхронно
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            text = line.strip()
            if text:
                logger.info(f"Sending text: {text}")
                await ws.send(json.dumps({"text": text}))

    async def run(self):
        logger.info(f"Connecting to Backend Server at {self.ws_url} ...")
        async with websockets.connect(self.ws_url) as ws:
            logger.info("Successfully connected to Backend!")
            logger.info("You can type text and press Enter at any time.")
            
            task1 = asyncio.create_task(self._read_mic_and_send(ws))
            task2 = asyncio.create_task(self._receive_and_play(ws))
            task3 = asyncio.create_task(self._read_console_and_send(ws))
            
            # Ждем завершения любой из задач (при ошибке или разрыве соединения)
            done, pending = await asyncio.wait(
                [task1, task2, task3],
                return_when=asyncio.FIRST_COMPLETED
            )
            for p in pending:
                p.cancel()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Porfiriy Debug PC Client")
    parser.add_argument("--url", type=str, default="ws://localhost:8765", help="Backend WebSocket URL (e.g. ws://192.168.1.100:8765)")
    args = parser.parse_args()
    
    client = DebugClient(args.url)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        logger.info("Exiting client...")
