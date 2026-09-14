import asyncio
import websockets
import pyaudio
import webrtcvad
import argparse
import logging
import queue
import json
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

FORMAT = pyaudio.paInt16
CHANNELS = 1
SEND_RATE = 16000
RECEIVE_RATE = 24000
CHUNK = 320 # 20ms of 16kHz audio for WebRTC VAD

class DebugClient:
    def __init__(self, ws_url, device_id="pc_debug_client", name="PC Debug Client (Компьютер)", no_register=False):
        self.ws_url = ws_url
        self.device_id = device_id
        self.name = name
        self.no_register = no_register
        self.pya = pyaudio.PyAudio()
        self.vad = webrtcvad.Vad(3) # Aggressiveness level from 0 to 3
        self.play_queue = queue.Queue()
        self.is_playing = False
        self.start_time = time.time()

    async def _heartbeat_loop(self, ws):
        """Периодическая отправка телеметрии и RSSI на сервер."""
        while True:
            await asyncio.sleep(20)
            try:
                uptime = int(time.time() - self.start_time)
                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "rssi": -42,
                    "uptime": uptime,
                    "state": "idle"
                }))
            except Exception:
                break

    async def _read_mic_and_send(self, ws):
        stream = self.pya.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SEND_RATE,
            input=True,
            frames_per_buffer=CHUNK
        )
        logger.info("Microphone capturing started (16kHz).")
        
        silence_chunk = b'\x00' * CHUNK
        frames_since_speech = 100 # start muted
        
        while True:
            try:
                # Читаем чанк с микрофона
                data = await asyncio.to_thread(stream.read, CHUNK, exception_on_overflow=False)
                
                # Noise gate: сбрасываем счетчик если есть речь
                if self.vad.is_speech(data, SEND_RATE):
                    frames_since_speech = 0
                else:
                    frames_since_speech += 1
                
                # Отправляем сырой PCM на сервер (или тишину, если долго нет речи)
                if frames_since_speech < 25:
                    await ws.send(data)
                else:
                    await ws.send(silence_chunk)
                    
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
                    self.play_queue.put(message)
                else:
                    logger.info(f"Server sent text: {message}")
                    try:
                        msg_data = json.loads(message)
                        msg_type = msg_data.get("type")
                        if msg_type == "interrupted":
                            logger.info("Server reported interruption. Clearing play queue.")
                            while not self.play_queue.empty():
                                try:
                                    self.play_queue.get_nowait()
                                except queue.Empty:
                                    break
                        elif msg_type == "beep":
                            logger.info("🔔 [BEEP] Received sound test command from Home Assistant / Web UI!")
                        elif msg_type == "set_config":
                            logger.info(f"⚙️ [CONFIG] Received config update from Server: {msg_data.get('config')}")
                        elif msg_type == "reboot":
                            logger.info("🔄 [REBOOT] Received reboot command from Server!")
                        elif msg_type == "start_mic_test":
                            dur_ms = msg_data.get("duration_ms", 5000)
                            logger.info(f"🎙️ [MIC-TEST] Received start_mic_test command from Server ({dur_ms} ms)")
                            async def finish_mic_test():
                                await asyncio.sleep(dur_ms / 1000.0)
                                try:
                                    await ws.send(json.dumps({"type": "mic_test_complete"}))
                                    logger.info("🎙️ [MIC-TEST] Mic test completed, sent mic_test_complete to Server")
                                except Exception as err:
                                    logger.error(f"Error sending mic_test_complete: {err}")
                            asyncio.create_task(finish_mic_test())
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"WS receive error: {e}")
                break

    async def _read_console_and_send(self, ws):
        import sys
        loop = asyncio.get_running_loop()
        while True:
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
            
            # Отправляем стартовый пакет регистрации в Home Assistant / MQTT
            if not self.no_register:
                reg_pkt = {
                    "type": "register",
                    "mac": self.device_id,
                    "name": self.name,
                    "ip": "127.0.0.1",
                    "rssi": -42,
                    "uptime": 0,
                    "device_type": "pc_debug",
                    "config": {
                        "speaker_volume": 1.0,
                        "mic_gain": 2,
                        "wake_word_threshold": 0.93
                    }
                }
                logger.info(f"Sending registration packet for device '{self.device_id}' to Home Assistant...")
                await ws.send(json.dumps(reg_pkt))
            
            logger.info("You can speak into microphone or type text commands and press Enter.")
            
            task1 = asyncio.create_task(self._read_mic_and_send(ws))
            task2 = asyncio.create_task(self._receive_and_play(ws))
            task3 = asyncio.create_task(self._read_console_and_send(ws))
            task4 = asyncio.create_task(self._heartbeat_loop(ws))
            
            done, pending = await asyncio.wait(
                [task1, task2, task3, task4],
                return_when=asyncio.FIRST_COMPLETED
            )
            for p in pending:
                p.cancel()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Porfiriy Debug PC Client")
    parser.add_argument("--url", type=str, default="ws://localhost:8765", help="Backend WebSocket URL (e.g. ws://192.168.1.100:8765)")
    parser.add_argument("--device-id", type=str, default="pc_debug_client", help="Device ID / virtual MAC (default: pc_debug_client)")
    parser.add_argument("--name", type=str, default="PC Debug Client (Компьютер)", help="Device friendly name")
    parser.add_argument("--no-register", action="store_true", help="Skip Home Assistant MQTT registration (anonymous debug mode)")
    args = parser.parse_args()
    
    client = DebugClient(args.url, device_id=args.device_id, name=args.name, no_register=args.no_register)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        logger.info("Exiting client...")
