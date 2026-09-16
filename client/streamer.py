#!/usr/bin/env python3
"""
Porfiriy Audio Streamer Client (PC / Raspberry Pi / Linux / Windows / macOS)

Скрипт для превращения любого компьютера с микрофоном в голосовой сателлит Порфирия.
- Непрерывно транслирует звук с выбранного микрофона на сервер Porfiriy WebSocket (16 кГц PCM).
- Серверный движок openWakeWord детектирует вейкворд "Порфирий".
- Ответ может воспроизводиться как локально через выбранный динамик (режим 'stream'),
  так и на любом внешнем медиаплеере Home Assistant (режим 'external_player').

Использование:
  python streamer.py --list-devices
  python streamer.py --url ws://192.168.1.50:8765 --input-device "Microphone"
  python streamer.py --url ws://192.168.1.50:8765 --input-device 1 --output-device 3
"""

import sys
import os
import time
import json
import queue
import socket
import logging
import asyncio
import argparse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


try:
    import pyaudio
except ImportError:
    print("Ошибка: библиотека pyaudio не установлена.")
    print("Установите её командой: pip install pyaudio websockets")
    sys.exit(1)

try:
    import websockets
except ImportError:
    print("Ошибка: библиотека websockets не установлена.")
    print("Установите её командой: pip install websockets")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("streamer")

SAMPLE_RATE_INPUT = 16000     # Частота захвата для вейкворда и Gemini Live
SAMPLE_RATE_OUTPUT = 24000    # Частота ответа Gemini Live
AUDIO_CHANNELS = 1            # Моно
AUDIO_FORMAT = pyaudio.paInt16 # 16-бит знаковый PCM
CHUNK_SIZE = 512              # 32 мс на 16 кГц


def clean_device_name(name: str) -> str:
    """Устраняет искажение русских символов в именах устройств PortAudio на Windows."""
    if not isinstance(name, str):
        return str(name)
    for enc in ("cp1251", "cp1252", "latin1", "iso-8859-1"):
        try:
            raw = name.encode(enc, errors="ignore")
            decoded = raw.decode("utf-8", errors="ignore")
            if decoded and any(0x0400 <= ord(c) <= 0x04FF for c in decoded):
                return decoded
        except Exception:
            pass
    return name


def resample_pcm(pcm_bytes: bytes, in_rate: int, out_rate: int) -> bytes:
    """Конвертация частоты дискретизации для 16-бит моно PCM."""
    if in_rate == out_rate or not pcm_bytes:
        return pcm_bytes
    try:
        import audioop
        converted, _ = audioop.ratecv(pcm_bytes, 2, 1, in_rate, out_rate, None)
        return converted
    except Exception:
        import struct
        count = len(pcm_bytes) // 2
        samples = struct.unpack(f"<{count}h", pcm_bytes)
        ratio = in_rate / float(out_rate)
        out_count = int(count / ratio)
        out_samples = []
        for i in range(out_count):
            src_idx = i * ratio
            idx0 = int(src_idx)
            idx1 = min(idx0 + 1, count - 1)
            frac = src_idx - idx0
            val = int(samples[idx0] * (1.0 - frac) + samples[idx1] * frac)
            out_samples.append(max(-32768, min(32767, val)))
        return struct.pack(f"<{len(out_samples)}h", *out_samples)


def list_audio_devices():
    """Выводит детальный список всех доступных аудиоустройств."""
    p = pyaudio.PyAudio()
    num_devices = p.get_device_count()

    print("\n" + "=" * 70)
    print(" 🎙️ ДОСТУПНЫЕ АУДИОУСТРОЙСТВА В СИСТЕМЕ:")
    print("=" * 70)

    input_devs = []
    output_devs = []

    for i in range(num_devices):
        try:
            info = p.get_device_info_by_index(i)
            max_in = info.get("maxInputChannels", 0)
            max_out = info.get("maxOutputChannels", 0)
            name = clean_device_name(info.get("name", "Неизвестное устройство"))
            default_rate = int(info.get("defaultSampleRate", 0))

            if max_in > 0:
                input_devs.append((i, name, max_in, default_rate))
            if max_out > 0:
                output_devs.append((i, name, max_out, default_rate))
        except Exception as e:
            pass

    print("\n📥 УСТРОЙСТВА ВВОДА (МИКРОФОНЫ):")
    if not input_devs:
        print("  [!] Микрофоны не обнаружены.")
    else:
        for idx, name, ch, rate in input_devs:
            print(f"  [{idx:2d}] {name} (каналов: {ch}, {rate} Гц)")

    print("\n📤 УСТРОЙСТВА ВЫВОДА (ДИНАМИКИ / НАУШНИКИ):")
    if not output_devs:
        print("  [!] Динамики не обнаружены.")
    else:
        for idx, name, ch, rate in output_devs:
            print(f"  [{idx:2d}] {name} (каналов: {ch}, {rate} Гц)")

    print("\n" + "=" * 70)
    print("Пример запуска:")
    print("  python streamer.py --url ws://<IP_СЕРВЕРА>:8765 --input-device 1")
    print("=" * 70 + "\n")

    p.terminate()


def resolve_device_index(p: pyaudio.PyAudio, identifier, is_input: bool):
    """Находит индекс устройства по номеру или подстроке в названии."""
    if identifier is None:
        return None

    # Попытка преобразовать в число
    try:
        idx = int(identifier)
        info = p.get_device_info_by_index(idx)
        channels = info.get("maxInputChannels", 0) if is_input else info.get("maxOutputChannels", 0)
        if channels > 0:
            return idx
        else:
            role = "ввода (микрофон)" if is_input else "вывода (динамик)"
            logger.warning(f"Устройство с индексом [{idx}] не поддерживает {role}.")
    except (ValueError, IOError):
        pass

    # Поиск по подстроке в названии
    target = str(identifier).lower()
    for i in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(i)
            channels = info.get("maxInputChannels", 0) if is_input else info.get("maxOutputChannels", 0)
            dev_name = clean_device_name(info.get("name", "")).lower()
            if channels > 0 and target in dev_name:
                return i
        except Exception:
            pass

    role = "ввода" if is_input else "вывода"
    logger.error(f"Не удалось найти аудиоустройство {role} по запросу '{identifier}'. Используйте --list-devices.")
    return None


class StreamerClient:
    def __init__(self, ws_url: str, mac: str, name: str, input_id=None, output_id=None, enable_playback=True):
        self.ws_url = ws_url
        self.mac = mac.lower().replace(":", "_").replace("-", "_")
        self.name = name
        self.input_id = input_id
        self.output_id = output_id
        self.enable_playback = enable_playback

        self.p = pyaudio.PyAudio()
        self.in_idx = resolve_device_index(self.p, self.input_id, is_input=True)
        self.out_idx = resolve_device_index(self.p, self.output_id, is_input=False) if self.enable_playback else None

        self.play_queue = queue.Queue()
        self.is_running = True
        self.start_time = time.time()

    def _log_selected_devices(self):
        try:
            if self.in_idx is not None:
                in_info = self.p.get_device_info_by_index(self.in_idx)
                logger.info(f"🎤 Захват звука: [{self.in_idx}] {in_info.get('name')}")
            else:
                default_in = self.p.get_default_input_device_info()
                logger.info(f"🎤 Захват звука: [По умолчанию] {default_in.get('name')}")
        except Exception as e:
            logger.warning(f"Инфо микрофона: {e}")

        if self.enable_playback:
            try:
                if self.out_idx is not None:
                    out_info = self.p.get_device_info_by_index(self.out_idx)
                    logger.info(f"🔊 Вывод звука: [{self.out_idx}] {out_info.get('name')}")
                else:
                    default_out = self.p.get_default_output_device_info()
                    logger.info(f"🔊 Вывод звука: [По умолчанию] {default_out.get('name')}")
            except Exception as e:
                logger.warning(f"Инфо динамика: {e}")
        else:
            logger.info("🔊 Локальный вывод звука отключен (--no-playback).")

    async def _send_mic_stream(self, ws):
        """Непрерывный захват микрофона и передача PCM 16kHz в WebSocket."""
        loop = asyncio.get_running_loop()
        stream = None
        capture_rate = SAMPLE_RATE_INPUT

        # Определение нативной частоты устройства
        native_rate = 16000
        try:
            if self.in_idx is not None:
                info = self.p.get_device_info_by_index(self.in_idx)
                native_rate = int(info.get("defaultSampleRate", 16000))
            else:
                default_in = self.p.get_default_input_device_info()
                native_rate = int(default_in.get("defaultSampleRate", 16000))
        except Exception:
            native_rate = 16000

        try:
            # 1. Пробуем открыть напрямую на 16000 Гц
            try:
                stream = self.p.open(
                    format=AUDIO_FORMAT,
                    channels=AUDIO_CHANNELS,
                    rate=SAMPLE_RATE_INPUT,
                    input=True,
                    input_device_index=self.in_idx,
                    frames_per_buffer=CHUNK_SIZE
                )
                capture_rate = SAMPLE_RATE_INPUT
                chunk_read = CHUNK_SIZE
            except Exception as ex_open:
                # 2. Если драйвер (например WASAPI) требует нативную частоту (48000 или 44100)
                logger.info(f"Захват 16000 Гц не поддержан драйвером ({ex_open}). Переключаемся на нативную частоту {native_rate} Гц.")
                chunk_read = int(CHUNK_SIZE * (native_rate / float(SAMPLE_RATE_INPUT)))
                stream = self.p.open(
                    format=AUDIO_FORMAT,
                    channels=AUDIO_CHANNELS,
                    rate=native_rate,
                    input=True,
                    input_device_index=self.in_idx,
                    frames_per_buffer=chunk_read
                )
                capture_rate = native_rate

            logger.info(f"🎙️ Поток микрофона запущен ({capture_rate} Гц -> 16 кГц Mono PCM). Говорите 'Порфирий'...")

            while self.is_running:
                data = await loop.run_in_executor(None, stream.read, chunk_read, False)
                if data:
                    if capture_rate != SAMPLE_RATE_INPUT:
                        data = resample_pcm(data, capture_rate, SAMPLE_RATE_INPUT)
                    await ws.send(data)
                await asyncio.sleep(0.001)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Ошибка захвата микрофона: {e}")
            await asyncio.sleep(1.0)
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

    async def _receive_and_play(self, ws):
        """Прием ответов от сервера и воспроизведение (если режим 'stream')."""
        if not self.enable_playback:
            while self.is_running:
                try:
                    msg = await ws.recv()
                    if isinstance(msg, str):
                        self._handle_json_message(msg)
                except Exception:
                    break
            return

        loop = asyncio.get_running_loop()
        stream = None

        native_out_rate = SAMPLE_RATE_OUTPUT
        try:
            if self.out_idx is not None:
                out_info = self.p.get_device_info_by_index(self.out_idx)
                native_out_rate = int(out_info.get("defaultSampleRate", SAMPLE_RATE_OUTPUT))
            else:
                default_out = self.p.get_default_output_device_info()
                native_out_rate = int(default_out.get("defaultSampleRate", SAMPLE_RATE_OUTPUT))
        except Exception:
            native_out_rate = SAMPLE_RATE_OUTPUT

        try:
            playback_rate = SAMPLE_RATE_OUTPUT
            try:
                stream = self.p.open(
                    format=AUDIO_FORMAT,
                    channels=AUDIO_CHANNELS,
                    rate=SAMPLE_RATE_OUTPUT,
                    output=True,
                    output_device_index=self.out_idx
                )
            except Exception as ex_out:
                logger.info(f"Вывод 24000 Гц не поддержан ({ex_out}), переключаемся на {native_out_rate} Гц.")
                stream = self.p.open(
                    format=AUDIO_FORMAT,
                    channels=AUDIO_CHANNELS,
                    rate=native_out_rate,
                    output=True,
                    output_device_index=self.out_idx
                )
                playback_rate = native_out_rate

            async def playback_worker():
                while self.is_running:
                    try:
                        chunk = self.play_queue.get_nowait()
                        if playback_rate != SAMPLE_RATE_OUTPUT:
                            chunk = resample_pcm(chunk, SAMPLE_RATE_OUTPUT, playback_rate)
                        await loop.run_in_executor(None, stream.write, chunk)
                    except queue.Empty:
                        await asyncio.sleep(0.01)
                    except Exception as ex:
                        logger.error(f"Ошибка воспроизведения аудио: {ex}")
                        await asyncio.sleep(0.05)

            playback_task = asyncio.create_task(playback_worker())

            while self.is_running:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    self.play_queue.put(msg)
                elif isinstance(msg, str):
                    self._handle_json_message(msg)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Ошибка приема из сокета: {e}")
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

    def _handle_json_message(self, text: str):
        """Обработка системных событий от сервера."""
        try:
            data = json.loads(text)
            m_type = data.get("type")
            if m_type == "interrupted":
                logger.info("⚡ Перебивание (Barge-in)! Очистка очереди воспроизведения.")
                while not self.play_queue.empty():
                    try:
                        self.play_queue.get_nowait()
                    except queue.Empty:
                        break
            elif m_type == "beep":
                logger.info("🔔 Получен звуковой сигнал готовности.")
            elif m_type == "set_config":
                cfg = data.get("config", {})
                logger.info(f"⚙️ Синхронизирована конфигурация с сервера: {cfg}")
        except Exception as e:
            logger.debug(f"Ошибка парсинга сообщения: {e}")

    async def _heartbeat_loop(self, ws):
        """Периодическая телеметрия на сервер."""
        while self.is_running:
            await asyncio.sleep(15)
            try:
                uptime = int(time.time() - self.start_time)
                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "rssi": -35,
                    "uptime": uptime,
                    "state": "idle"
                }))
            except Exception:
                break

    async def run(self):
        self._log_selected_devices()
        logger.info(f"Подключение к серверу Porfiriy: {self.ws_url} ...")

        retry_count = 0
        while self.is_running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=15) as ws:
                    logger.info("✅ Успешно подключено к WebSocket серверу Порфирия!")
                    retry_count = 0

                    reg_packet = {
                        "type": "register",
                        "mac": self.mac,
                        "name": self.name,
                        "device_type": "pc_streamer",
                        "ip": "127.0.0.1",
                        "rssi": -35,
                        "uptime": 0,
                        "config": {
                            "audio_output_mode": "stream" if self.enable_playback else "external_player",
                            "speaker_volume": 0.8,
                            "ww_threshold": 0.94
                        }
                    }
                    await ws.send(json.dumps(reg_packet))
                    logger.info(f"Зарегистрирован сателлит '{self.name}' (MAC: {self.mac})")

                    t_mic = asyncio.create_task(self._send_mic_stream(ws))
                    t_recv = asyncio.create_task(self._receive_and_play(ws))
                    t_hb = asyncio.create_task(self._heartbeat_loop(ws))

                    done, pending = await asyncio.wait(
                        [t_mic, t_recv, t_hb],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in pending:
                        t.cancel()

            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                retry_count += 1
                wait_sec = min(15, 2 * retry_count)
                logger.warning(f"Соединение потеряно ({e}). Переподключение через {wait_sec} сек...")
                await asyncio.sleep(wait_sec)
            except Exception as e:
                logger.error(f"Неожиданная ошибка: {e}")
                await asyncio.sleep(5)

    def close(self):
        self.is_running = False
        try:
            self.p.terminate()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Porfiriy Audio Streamer Client")
    parser.add_argument("--list-devices", action="store_true", help="Показать список доступных микрофонов и динамиков")
    parser.add_argument("--url", type=str, default="ws://localhost:8765", help="Адрес WebSocket сервера Порфирия (например: ws://192.168.1.100:8765)")
    parser.add_argument("--input-device", type=str, default=None, help="Индекс или часть названия микрофона")
    parser.add_argument("--output-device", type=str, default=None, help="Индекс или часть названия динамика")
    parser.add_argument("--mac", type=str, default=None, help="Уникальный ID / MAC устройства (по умолчанию: имя ПК)")
    parser.add_argument("--name", type=str, default=None, help="Отображаемое имя сателлита")
    parser.add_argument("--no-playback", action="store_true", help="Отключить локальный аудиовыход (режим 'только микрофон')")

    args = parser.parse_args()

    if args.list_devices:
        list_audio_devices()
        return

    hostname = socket.gethostname() or "pc"
    mac = args.mac or f"streamer_{hostname.lower()}"
    name = args.name or f"PC Streamer ({hostname})"

    client = StreamerClient(
        ws_url=args.url,
        mac=mac,
        name=name,
        input_id=args.input_device,
        output_id=args.output_device,
        enable_playback=not args.no_playback
    )

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        logger.info("Завершение работы стримера...")
    finally:
        client.close()


if __name__ == "__main__":
    main()
