import os
import io
import wave
import struct
import math
import json
import time
import asyncio
import logging
from typing import Dict, List, Any, Optional, Callable

logger = logging.getLogger("device_manager")

TARGET_FIRMWARE_VERSION = "0.0.103"

def is_newer_version(target: str, current: str) -> bool:
    """Проверяет, новее ли целевая версия, чем текущая (SemVer)."""
    try:
        def parse_v(v_str):
            if not v_str:
                return []
            return [int(x) for x in str(v_str).lower().replace("v", "").strip().split(".") if x.isdigit()]
        t_parts = parse_v(target)
        c_parts = parse_v(current)
        if not t_parts or not c_parts:
            return False
        return t_parts > c_parts
    except Exception:
        return False

def filter_pcm16_highpass(pcm_data: bytes, cutoff_hz: float = 85.0, sample_rate: int = 16000) -> bytes:
    """Удаление сетевого гула 50/100 Гц и постоянного смещения (DC offset) через High-Pass фильтр 2-го порядка."""
    if not pcm_data:
        return b""
    try:
        import numpy as np
        from scipy import signal
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        if len(samples) < 8:
            return pcm_data
        b, a = signal.butter(2, cutoff_hz, btype='highpass', fs=sample_rate)
        filtered = signal.lfilter(b, a, samples)
        clamped = np.clip(np.round(filtered), -32768, 32767).astype(np.int16)
        return clamped.tobytes()
    except Exception as e:
        logger.debug(f"[FILTER] High-pass filter error: {e}")
        return pcm_data

def pcm16_to_wav(pcm_data: bytes, sample_rate: int = 16000) -> bytes:
    """Упаковка сырых 16-битных PCM сэмплов в стандартный RIFF WAV контейнер."""
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()

def analyze_pcm16(pcm_data: bytes, sample_rate: int = 16000) -> Dict[str, Any]:
    """Расчет акустических метрик (RMS dBFS, Peak, клиппинг)."""
    if not pcm_data:
        return {
            "duration_s": 0.0,
            "samples_count": 0,
            "rms_linear": 0.0,
            "rms_dbfs": -100.0,
            "peak": 0,
            "peak_dbfs": -100.0,
            "clipping_samples": 0,
            "clipping_percent": 0.0,
            "quality": "no_audio"
        }
    
    num_samples = len(pcm_data) // 2
    samples = struct.unpack(f"<{num_samples}h", pcm_data[:num_samples * 2])
    
    peak = 0
    clipping_count = 0
    sum_sq = 0.0
    
    for s in samples:
        abs_s = abs(s)
        if abs_s > peak:
            peak = abs_s
        if abs_s >= 32600:
            clipping_count += 1
        sum_sq += s * s
        
    rms = math.sqrt(sum_sq / num_samples) if num_samples > 0 else 0.0
    rms_dbfs = 20.0 * math.log10(rms / 32768.0) if rms > 0 else -100.0
    peak_dbfs = 20.0 * math.log10(peak / 32768.0) if peak > 0 else -100.0
    clip_pct = (clipping_count / num_samples) * 100.0 if num_samples > 0 else 0.0
    
    quality = "optimal"
    if clip_pct > 1.5:
        quality = "critical_clipping"
    elif clip_pct > 0.1:
        quality = "warning_clipping"
    elif rms_dbfs < -42.0:
        quality = "too_quiet"
    elif rms_dbfs > -10.0:
        quality = "loud"
        
    return {
        "duration_s": round(num_samples / float(sample_rate), 2),
        "samples_count": num_samples,
        "rms_linear": round(rms, 1),
        "rms_dbfs": round(rms_dbfs, 1),
        "peak": peak,
        "peak_dbfs": round(peak_dbfs, 1),
        "clipping_samples": clipping_count,
        "clipping_percent": round(clip_pct, 2),
        "quality": quality
    }

DEFAULT_DEVICE_CONFIG = {
    "mic_gain": 2.0,
    "speaker_volume": 0.5,
    "wake_word_threshold": 0.93,
    "wake_word_window_size": 3,
    "wake_word_window_mode": 1,
    "wake_word_mode": "local",       # "local" (TFLite на плате) или "server" (openWakeWord ONNX)
    "ww_threshold": 0.94,            # Порог серверного вейкворда
    "audio_output_mode": "stream",   # "stream" (встроенный/в сокет) или "external_player" (HA media_player)
    "response_player": "",           # entity_id плеера HA для вывода звука
    "silence_timeout_ms": 700,
    "listen_timeout_s": 6,
    "silence_threshold_energy": 180,
    "barge_in_threshold_rms": None,
    "enable_ducking": True,
    "led_brightness": 50,
    "led_color_idle": "#000000",
    "led_color_listen": "#0000ff",
    "led_color_think": "#ffaa00",
    "led_color_speak": "#00ff00",
    "led_mode_idle": 0,
    "led_mode_listen": 1,
    "led_mode_think": 2,
    "led_mode_speak": 1
}

class DeviceManager:
    def __init__(self, storage_path: Optional[str] = None):
        if storage_path:
            self.storage_path = storage_path
        elif os.path.exists("/data"):
            self.storage_path = "/data/devices.json"
        else:
            self.storage_path = os.path.join(os.path.dirname(__file__), "devices.json")
            
        self.devices: Dict[str, Dict[str, Any]] = {}
        self.active_sockets: Dict[str, Any] = {}
        self.subscribers: List[Callable[[str, Dict[str, Any]], Any]] = []
        self.mic_test_sessions: Dict[str, Dict[str, Any]] = {}
        self.mic_test_results: Dict[str, Dict[str, Any]] = {}
        self.last_utterance_results: Dict[str, Dict[str, Any]] = {}
        # Кэш WAV-ответов Gemini для виртуальных стримеров (pc_streamer)
        self._response_wav: Dict[str, bytes] = {}
        # Обработчики принудительного вызова ассистента
        self._wake_handlers: Dict[str, Callable] = {}
        self._load()

    def _load(self):
        """Загрузка сохраненных устройств и их конфигураций из файла."""
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    for mac, data in saved.items():
                        # При старте все устройства считаются офлайн до подключения
                        data["is_online"] = False
                        data["state"] = "offline"
                        self.devices[mac] = data
                logger.info(f"Loaded {len(self.devices)} devices from {self.storage_path}")
            except Exception as e:
                logger.error(f"Failed to load devices from {self.storage_path}: {e}")

    def _save(self):
        """Сохранение устройств на диск."""
        try:
            to_save = {}
            for mac, data in self.devices.items():
                copy_d = dict(data)
                # Не сохраняем временные статусы и ссылки
                copy_d["is_online"] = False
                copy_d["state"] = "offline"
                to_save[mac] = copy_d
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save devices to {self.storage_path}: {e}")

    def add_subscriber(self, callback: Callable[[str, Dict[str, Any]], Any]):
        """Подписка на события изменения состояния устройств (для SSE и MQTT)."""
        self.subscribers.append(callback)

    def _notify(self, event_type: str, device_data: Dict[str, Any]):
        for cb in self.subscribers:
            try:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(event_type, device_data))
                else:
                    cb(event_type, device_data)
            except Exception as e:
                logger.error(f"Error in device subscriber: {e}")

    def register_device(self, mac: str, info: Dict[str, Any], ws=None) -> Dict[str, Any]:
        """Регистрация или обновление устройства при подключении к WebSocket."""
        clean_mac = mac.strip().lower()
        now = time.time()
        
        if clean_mac in self.devices:
            dev = self.devices[clean_mac]
            dev["ip"] = info.get("ip", dev.get("ip", ""))
            dev["name"] = info.get("name", dev.get("name", f"Porfiriy {clean_mac[-6:]}"))
            dev["rssi"] = info.get("rssi", dev.get("rssi", -60))
            dev["uptime"] = info.get("uptime", 0)
            dev["device_type"] = info.get("device_type", dev.get("device_type", "esp32"))
            dev["firmware"] = info.get("firmware", dev.get("firmware", "0.0.52"))
            
            # Мерджим конфиг (сохраненный конфиг сервера имеет приоритет)
            dev_config = dict(DEFAULT_DEVICE_CONFIG)
            dev_config.update(info.get("config", {}))
            dev_config.update(dev.get("config", {}))
            dev["config"] = dev_config
        else:
            dev_config = dict(DEFAULT_DEVICE_CONFIG)
            dev_config.update(info.get("config", {}))
            dev = {
                "mac": clean_mac,
                "name": info.get("name", f"Porfiriy {clean_mac[-6:]}"),
                "ip": info.get("ip", ""),
                "rssi": info.get("rssi", -60),
                "uptime": info.get("uptime", 0),
                "device_type": info.get("device_type", "esp32"),
                "firmware": info.get("firmware", "0.0.52"),
                "config": dev_config,
                "area_name": None
            }
            self.devices[clean_mac] = dev

        if dev.get("device_type") == "pc_streamer":
            dev["config"].setdefault("wake_word_mode", "server")
            
        dev["target_firmware"] = TARGET_FIRMWARE_VERSION
        dev["has_update"] = (
            dev.get("device_type") == "esp32" and 
            is_newer_version(TARGET_FIRMWARE_VERSION, dev.get("firmware", ""))
        )
        # Сброс зависшего статуса OTA при успешном подключении платы
        dev["ota_status"] = None
        dev["ota_progress"] = 0
        dev["is_online"] = True
        dev["state"] = "idle"
        dev["last_seen"] = now
        
        if ws:
            self.active_sockets[clean_mac] = ws
            # Автоматически отправляем сохраненный на сервере конфиг на устройство при подключении
            cfg_to_send = dict(dev["config"])
            if dev.get("device_type") == "esp32":
                cfg_to_send["speaker_volume"] = 1.0  # На ESP32 держим 1.0, чтобы не было двойного затухания
            asyncio.create_task(self.send_command(clean_mac, {"type": "set_config", "config": cfg_to_send}))
            
        self._save()
        self._notify("registered", dev)
        logger.info(f"Device registered: {clean_mac} ({dev['name']}) @ {dev['ip']} (firmware: {dev['firmware']}, update: {dev['has_update']})")
        return dev

    def update_heartbeat(self, mac: str, rssi: int, uptime: int, state: Optional[str] = None):
        """Обновление периодической телеметрии (RSSI, uptime)."""
        clean_mac = mac.strip().lower()
        if clean_mac in self.devices:
            dev = self.devices[clean_mac]
            dev["rssi"] = rssi
            dev["uptime"] = uptime
            dev["last_seen"] = time.time()
            dev["is_online"] = True
            if state:
                dev["state"] = state
            self._notify("heartbeat", dev)

    def set_device_state(self, mac: str, state: str):
        """Обновление состояния (idle, listening, thinking, speaking)."""
        clean_mac = mac.strip().lower()
        if clean_mac in self.devices:
            dev = self.devices[clean_mac]
            dev["state"] = state
            dev["last_seen"] = time.time()
            dev["is_online"] = True
            self._notify("state_changed", dev)

    def set_device_offline(self, mac: str):
        """Фиксация отключения устройства."""
        clean_mac = mac.strip().lower()
        self._wake_handlers.pop(clean_mac, None)
        if clean_mac in self.active_sockets:
            del self.active_sockets[clean_mac]
        if clean_mac in self.devices:
            dev = self.devices[clean_mac]
            dev["is_online"] = False
            dev["state"] = "offline"
            self._notify("offline", dev)
            logger.info(f"Device went offline: {clean_mac}")

    def get_device(self, mac: str) -> Optional[Dict[str, Any]]:
        return self.devices.get(mac.strip().lower())

    def get_firmware_path(self) -> Optional[str]:
        """Поиск пути к бинарнику прошивки."""
        candidates = [
            "/backend/firmware/firmware.bin",
            os.path.join(os.path.dirname(__file__), "firmware", "firmware.bin"),
            os.path.join(os.path.dirname(__file__), "..", "esp32_firmware", ".pio", "build", "esp32-s3-devkitc-1", "firmware.bin"),
            "/data/firmware.bin"
        ]
        for c in candidates:
            if os.path.exists(c) and os.path.isfile(c):
                return os.path.abspath(c)
        return None

    def get_firmware_info(self) -> Dict[str, Any]:
        """Информация об актуальной серверной прошивке."""
        path = self.get_firmware_path()
        exists = bool(path and os.path.exists(path))
        size = os.path.getsize(path) if exists else 0
        outdated = self.get_outdated_devices()
        return {
            "target_version": TARGET_FIRMWARE_VERSION,
            "available": exists,
            "size": size,
            "path": path,
            "outdated_count": len(outdated)
        }

    def get_outdated_devices(self) -> List[Dict[str, Any]]:
        """Список подключенных ESP32 с устаревшей прошивкой."""
        outdated = []
        for d in self.get_all_devices():
            if d.get("device_type") == "esp32" and d.get("is_online", False):
                if is_newer_version(TARGET_FIRMWARE_VERSION, d.get("firmware", "")):
                    outdated.append(d)
        return outdated

    def get_all_devices(self) -> List[Dict[str, Any]]:
        result = []
        for d in self.devices.values():
            dev_copy = dict(d)
            dev_copy["target_firmware"] = TARGET_FIRMWARE_VERSION
            dev_copy["has_update"] = (
                dev_copy.get("device_type") == "esp32" and 
                dev_copy.get("is_online", False) and 
                is_newer_version(TARGET_FIRMWARE_VERSION, dev_copy.get("firmware", ""))
            )
            result.append(dev_copy)
        return result

    async def send_command(self, mac: str, command: Dict[str, Any]) -> bool:
        """Отправка JSON-команды на подключенное устройство по WebSocket."""
        clean_mac = mac.strip().lower()
        ws = self.active_sockets.get(clean_mac)

        # Fallback 1: Поиск без разделителей (двоеточий и дефисов)
        if not ws or getattr(ws, "closed", False):
            norm_target = clean_mac.replace(":", "").replace("-", "")
            for k, s in self.active_sockets.items():
                if k.replace(":", "").replace("-", "") == norm_target and not getattr(s, "closed", False):
                    ws = s
                    break

        # Fallback 2: Если активен ровно один сокет
        if not ws or getattr(ws, "closed", False):
            active_items = [(k, s) for k, s in self.active_sockets.items() if not getattr(s, "closed", False)]
            if len(active_items) == 1:
                ws = active_items[0][1]
                logger.debug(f"Using single active socket ({active_items[0][0]}) for target {clean_mac}")

        if ws and not getattr(ws, "closed", False):
            try:
                await ws.send(json.dumps(command))
                logger.info(f"Command {command.get('type')} successfully sent to {clean_mac}")
                return True
            except Exception as e:
                logger.error(f"Error sending command to {clean_mac}: {e}")
                return False

        logger.warning(f"send_command failed for {clean_mac}: socket not found or closed. Active sockets: {list(self.active_sockets.keys())}")
        return False

    def register_wake_handler(self, mac: str, handler: Callable):
        """Регистрирует обработчик принудительного вызова для устройства."""
        clean_mac = mac.strip().lower()
        self._wake_handlers[clean_mac] = handler

    def unregister_wake_handler(self, mac: str):
        """Удаляет зарегистрированный обработчик вызова для устройства."""
        clean_mac = mac.strip().lower()
        self._wake_handlers.pop(clean_mac, None)

    async def trigger_wake(self, mac: str) -> bool:
        """Принудительный вызов ассистента (эмуляция сработки вейкворда)."""
        clean_mac = mac.strip().lower()
        dev = self.devices.get(clean_mac)
        if not dev or not dev.get("is_online"):
            # Поиск без двоеточий / дефисов
            clean_mac_norm = clean_mac.replace(":", "").replace("-", "")
            for k, d in self.devices.items():
                if k.replace(":", "").replace("-", "") == clean_mac_norm and d.get("is_online"):
                    clean_mac = k
                    dev = d
                    break

        handler = self._wake_handlers.get(clean_mac)
        if handler:
            try:
                res = handler()
                if asyncio.iscoroutine(res):
                    return await res
                return bool(res)
            except Exception as e:
                logger.error(f"[WAKE] Error executing wake handler for {clean_mac}: {e}")
                return False

        # Fallback: для подключенных плат отправляем команду listen
        if clean_mac in self.active_sockets:
            logger.info(f"[WAKE] Sending 'listen' fallback command to {clean_mac}")
            return await self.send_command(clean_mac, {"type": "listen"})

        logger.warning(f"[WAKE] Device {clean_mac} is offline or has no active connection")
        return False

    async def update_device_config(self, mac: str, new_config: Dict[str, Any]) -> bool:
        """Обновление конфигурации для одного устройства."""
        clean_mac = mac.strip().lower()
        dev = self.devices.get(clean_mac)
        if not dev:
            norm_target = clean_mac.replace(":", "").replace("-", "")
            for k, d in self.devices.items():
                if k.replace(":", "").replace("-", "") == norm_target:
                    clean_mac = k
                    dev = d
                    break
        if not dev:
            logger.warning(f"Cannot update config: device {clean_mac} not found")
            return False
            
        dev.setdefault("config", {})
        dev["config"].update(new_config)
        self._save()
        
        # Только для ESP32 держим speaker_volume = 1.0 (pass-through), так как сервер масштабирует PCM напрямую
        cfg_to_send = dict(new_config)
        if dev.get("device_type") == "esp32" and "speaker_volume" in cfg_to_send:
            cfg_to_send["speaker_volume"] = 1.0

        await self.send_command(clean_mac, {
            "type": "set_config",
            "config": cfg_to_send
        })
        self._notify("config_updated", dev)
        return True

    async def bulk_update_config(self, target_macs: List[str], field_mask_config: Dict[str, Any]) -> Dict[str, bool]:
        """
        Групповое обновление параметров для выбранных устройств.
        Обновляются ТОЛЬКО поля, переданные в field_mask_config.
        """
        results = {}
        is_all = "all" in target_macs or len(target_macs) == 0
        norm_targets = [m.strip().lower() for m in target_macs]

        for mac, dev in self.devices.items():
            clean_mac = mac.strip().lower()
            if is_all or clean_mac in norm_targets:
                dev.setdefault("config", {})
                dev["config"].update(field_mask_config)
                
                cfg_to_send = dict(field_mask_config)
                if dev.get("device_type") == "esp32" and "speaker_volume" in cfg_to_send:
                    cfg_to_send["speaker_volume"] = 1.0

                if clean_mac in self.active_sockets:
                    sent = await self.send_command(clean_mac, {
                        "type": "set_config",
                        "config": cfg_to_send
                    })
                    results[clean_mac] = sent
                self._notify("config_updated", dev)
                
        self._save()
        return results

    def set_device_area(self, mac: str, area_name: Optional[str]):
        clean_mac = mac.strip().lower()
        if clean_mac in self.devices:
            self.devices[clean_mac]["area_name"] = area_name

    def get_device_config(self, mac: str) -> Dict[str, Any]:
        """Возвращает актуальный конфиг конкретного устройства или дефолты."""
        clean_mac = mac.strip().lower()
        dev = self.devices.get(clean_mac)
        if dev and "config" in dev:
            return dev["config"]
        return dict(DEFAULT_DEVICE_CONFIG)

    def get_device_area(self, mac: str) -> Optional[str]:
        """Возвращает имя комнаты, к которой привязана колонка."""
        clean_mac = mac.strip().lower()
        dev = self.devices.get(clean_mac)
        if dev:
            return dev.get("area_name")
        return None

    async def start_ota_update(self, mac: str) -> bool:
        """Запуск асинхронной передачи прошивки по WebSocket."""
        clean_mac = mac.strip().lower()
        ws = self.active_sockets.get(clean_mac)
        if not ws or getattr(ws, "closed", False):
            logger.error(f"[OTA] Device {clean_mac} is not connected to WebSocket")
            return False
            
        fw_path = self.get_firmware_path()
        if not fw_path or not os.path.exists(fw_path):
            logger.error(f"[OTA] Firmware binary not found for OTA update")
            return False
            
        asyncio.create_task(self._ota_worker(clean_mac, ws, fw_path))
        return True

    async def _ota_worker(self, mac: str, ws, fw_path: str):
        total_size = os.path.getsize(fw_path)
        logger.info(f"[OTA] Starting OTA stream for {mac}: size {total_size} bytes, target version {TARGET_FIRMWARE_VERSION}")
        
        dev = self.devices.get(mac, {})
        dev["ota_progress"] = 0
        dev["ota_status"] = "starting"
        self._notify("ota_progress", {
            "mac": mac, 
            "percent": 0, 
            "status": "starting", 
            "total": total_size
        })
        
        try:
            # 1. Отправляем команду ota_start
            await ws.send(json.dumps({
                "type": "ota_start",
                "size": total_size,
                "version": TARGET_FIRMWARE_VERSION
            }))
            
            # Даем ESP32 300мс на переход в STATE_OTA, сброс DMA и Update.begin()
            await asyncio.sleep(0.3)
            
            # 2. Потоковая передача файла чанками по 2048 байт (2 КБ)
            chunk_size = 2048
            sent_bytes = 0
            last_notify_percent = 0
            
            with open(fw_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    await ws.send(chunk)
                    sent_bytes += len(chunk)
                    
                    percent = int((sent_bytes / total_size) * 100)
                    if percent >= last_notify_percent + 4 or sent_bytes == total_size:
                        last_notify_percent = percent
                        dev["ota_progress"] = percent
                        dev["ota_status"] = "flashing"
                        self._notify("ota_progress", {
                            "mac": mac, 
                            "percent": percent, 
                            "status": "flashing",
                            "sent": sent_bytes, 
                            "total": total_size
                        })
                    # Темп передачи: ~15мс между чанками 2 КБ = ~130 КБ/с (весь бинарник за ~8-9 сек)
                    await asyncio.sleep(0.015)
                    
            logger.info(f"[OTA] All bytes sent ({sent_bytes}/{total_size}). Sending ota_end to {mac}...")
            
            # 3. Отправляем ota_end для финализации и перезагрузки ESP32
            await ws.send(json.dumps({"type": "ota_end"}))
            
            dev["ota_progress"] = 100
            dev["ota_status"] = "rebooting"
            self._notify("ota_progress", {
                "mac": mac, 
                "percent": 100, 
                "status": "rebooting",
                "total": total_size
            })
            
        except Exception as e:
            logger.error(f"[OTA] Error during OTA update for {mac}: {e}")
            dev["ota_status"] = "error"
            self._notify("ota_progress", {
                "mac": mac, 
                "percent": 0, 
                "status": "error", 
                "error": str(e)
            })

    async def start_bulk_ota(self, target_macs: Optional[List[str]] = None) -> List[str]:
        """Последовательное OTA-обновление выбранных (или всех устаревших) ESP32."""
        if not target_macs or "all" in target_macs or "all_outdated" in target_macs:
            outdated = self.get_outdated_devices()
            target_macs = [d["mac"] for d in outdated]
            
        started = []
        for mac in target_macs:
            clean_mac = mac.strip().lower()
            dev = self.devices.get(clean_mac)
            if dev and dev.get("is_online") and clean_mac in self.active_sockets:
                ok = await self.start_ota_update(clean_mac)
                if ok:
                    started.append(clean_mac)
                    # Пауза между стартами обновлений устройств
                    await asyncio.sleep(0.5)
        return started

    async def start_mic_test(self, mac: str, duration_s: float = 5.0) -> bool:
        """Запуск тестовой записи звука с микрофона на N секунд."""
        clean_mac = mac.strip().lower()
        ws = self.active_sockets.get(clean_mac)
        if not ws:
            logger.warning(f"[MIC-TEST] Cannot start mic test for {clean_mac}: device is offline")
            return False
            
        duration_s = max(1.0, min(15.0, float(duration_s)))
        duration_ms = int(duration_s * 1000)
        
        self.mic_test_sessions[clean_mac] = {
            "active": True,
            "start_time": time.time(),
            "end_time": time.time() + duration_s,
            "duration_s": duration_s,
            "chunks": []
        }
        
        logger.info(f"[MIC-TEST] Starting microphone test for {clean_mac} ({duration_s}s)")
        self._notify("mic_test_started", {"mac": clean_mac, "duration_s": duration_s})
        
        # Гарантируем автоматическое завершение теста по тайм-ауту (даже если плата со старой прошивкой ничего не прислала)
        asyncio.create_task(self._auto_finish_mic_test(clean_mac, duration_s))
        
        await self.send_command(clean_mac, {
            "type": "start_mic_test",
            "duration_ms": duration_ms
        })
        return True

    async def _auto_finish_mic_test(self, mac: str, duration_s: float):
        """Фоновый сторожевой таймер: завершает тест и готовит результат, если от колонки не пришло завершение."""
        await asyncio.sleep(duration_s + 0.6)
        clean_mac = mac.strip().lower()
        session = self.mic_test_sessions.get(clean_mac)
        if session and session.get("active"):
            logger.info(f"[MIC-TEST] Watchdog auto-finishing mic test for {clean_mac}")
            self.finish_mic_test(clean_mac)

    def is_mic_test_active(self, mac: str) -> bool:
        clean_mac = mac.strip().lower()
        session = self.mic_test_sessions.get(clean_mac)
        if not session or not session.get("active"):
            return False
        if time.time() > session.get("end_time", 0) + 2.0:
            session["active"] = False
            return False
        return True

    def get_mic_test_end_time(self, mac: str) -> float:
        clean_mac = mac.strip().lower()
        session = self.mic_test_sessions.get(clean_mac)
        return session.get("end_time", 0.0) if session else 0.0

    def append_mic_test_chunk(self, mac: str, chunk: bytes):
        clean_mac = mac.strip().lower()
        session = self.mic_test_sessions.get(clean_mac)
        if session and session.get("active"):
            session["chunks"].append(chunk)

    def finish_mic_test(self, mac: str) -> Optional[Dict[str, Any]]:
        clean_mac = mac.strip().lower()
        session = self.mic_test_sessions.get(clean_mac)
        if not session:
            return None
            
        session["active"] = False
        chunks = session.get("chunks", [])
        raw_pcm = b"".join(chunks)
        filtered_pcm = filter_pcm16_highpass(raw_pcm) if raw_pcm else b""
        
        wav_bytes = pcm16_to_wav(filtered_pcm) if filtered_pcm else b""
        stats = analyze_pcm16(filtered_pcm)
        stats["mac"] = clean_mac
        stats["timestamp"] = time.time()
        stats["has_audio"] = bool(filtered_pcm and len(filtered_pcm) >= 1600)
        
        self.mic_test_results[clean_mac] = {
            "wav": wav_bytes,
            "stats": stats,
            "timestamp": time.time()
        }
        
        logger.info(f"[MIC-TEST] Finished test for {clean_mac}: {len(filtered_pcm)} bytes PCM, {stats['duration_s']}s, RMS: {stats['rms_dbfs']} dBFS, Peak: {stats['peak']}, Clip: {stats['clipping_percent']}%")
        self._notify("mic_test_ready", {"mac": clean_mac, "stats": stats, "has_audio": stats["has_audio"]})
        return stats

    def get_mic_test_result(self, mac: str) -> Optional[Dict[str, Any]]:
        clean_mac = mac.strip().lower()
        return self.mic_test_results.get(clean_mac)

    def save_last_utterance(self, mac: str, pcm_data: bytes):
        """Сохранение последней боевой команды пользователя."""
        clean_mac = mac.strip().lower()
        if not pcm_data or len(pcm_data) < 1600:
            return
            
        filtered_pcm = filter_pcm16_highpass(pcm_data)
        wav_bytes = pcm16_to_wav(filtered_pcm)
        stats = analyze_pcm16(filtered_pcm)
        stats["mac"] = clean_mac
        stats["timestamp"] = time.time()
        
        self.last_utterance_results[clean_mac] = {
            "wav": wav_bytes,
            "stats": stats,
            "timestamp": time.time()
        }
        logger.info(f"[LAST-UTTERANCE] Saved utterance for {clean_mac}: {len(pcm_data)} bytes PCM, {stats['duration_s']}s, RMS: {stats['rms_dbfs']} dBFS")
        self._notify("last_utterance_ready", {"mac": clean_mac, "stats": stats})

    def get_last_utterance_result(self, mac: str) -> Optional[Dict[str, Any]]:
        clean_mac = mac.strip().lower()
        return self.last_utterance_results.get(clean_mac)

    # ------------------------------------------------------------------ #
    # Виртуальные стримеры (pc_streamer): ww_score и ответный WAV
    # ------------------------------------------------------------------ #

    def update_ww_score(self, mac: str, score: float, peak_score: float = 0.0, rms_dbfs: float = -60.0):
        """Обновляет live-score вейкворда для монитора в Web UI."""
        clean_mac = mac.strip().lower()
        dev = self.devices.get(clean_mac)
        if dev:
            dev["ww_score"] = float(round(score, 4))
            dev["ww_peak_score"] = float(round(peak_score, 4))
            dev["ww_rms_dbfs"] = float(rms_dbfs)
            last_score = dev.get("_last_notified_score", -1.0)
            last_rms = dev.get("_last_notified_rms", -99.0)
            now = time.time()
            last_notify_t = dev.get("_last_ww_notify_time", 0.0)

            # Отправка телеметрии:
            # 1. При высокой вероятности вейкворда (score >= 0.10) - отправляем моментально с мягким троттлом 100 мс
            # 2. При наличии звука/речи - регулярный срез 4 раза в секунду (раз в 250 мс)
            # 3. При резком скачке уровня звука (RMS delta >= 4.0 dB)
            should_notify = False
            if score >= 0.10 and (now - last_notify_t >= 0.10):
                should_notify = True
            elif abs(score - last_score) >= 0.02:
                should_notify = True
            elif (now - last_notify_t >= 0.25):
                should_notify = True
            elif abs(rms_dbfs - last_rms) >= 4.0 and (now - last_notify_t >= 0.12):
                should_notify = True

            if should_notify:
                dev["_last_notified_score"] = float(score)
                dev["_last_notified_rms"] = float(rms_dbfs)
                dev["_last_ww_notify_time"] = now
                self._notify("ww_score", {
                    "mac": clean_mac,
                    "name": str(dev.get("name", clean_mac)),
                    "ww_score": float(dev["ww_score"]),
                    "ww_peak": float(dev["ww_peak_score"]),
                    "rms_dbfs": float(dev["ww_rms_dbfs"]),
                    "threshold": float(dev.get("config", {}).get("ww_threshold", 0.94))
                })


    def save_response_wav(self, mac: str, wav_bytes: bytes):
        """Сохраняет WAV-ответ Gemini для последующей раздачи медиаплееру."""
        clean_mac = mac.strip().lower()
        self._response_wav[clean_mac] = wav_bytes
        logger.info(f"[STREAMER] Saved response WAV for {clean_mac}: {len(wav_bytes)} bytes")

    def get_response_wav(self, mac: str) -> Optional[bytes]:
        """Возвращает последний WAV-ответ Gemini для данного стримера."""
        clean_mac = mac.strip().lower()
        return self._response_wav.get(clean_mac)
