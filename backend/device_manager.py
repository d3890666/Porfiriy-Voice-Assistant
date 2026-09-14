import os
import json
import time
import asyncio
import logging
from typing import Dict, List, Any, Optional, Callable

logger = logging.getLogger("device_manager")

TARGET_FIRMWARE_VERSION = "0.0.63"

DEFAULT_DEVICE_CONFIG = {
    "mic_gain": 2,
    "speaker_volume": 1.0,
    "wake_word_threshold": 0.93,
    "wake_word_window_size": 3,
    "wake_word_window_mode": 1,
    "silence_timeout_ms": 700,
    "listen_timeout_s": 6,
    "silence_threshold_energy": 180,
    "enable_barge_in": False,
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
            
        dev["target_firmware"] = TARGET_FIRMWARE_VERSION
        dev["has_update"] = (
            dev.get("device_type") == "esp32" and 
            dev.get("firmware") != TARGET_FIRMWARE_VERSION
        )
        dev["is_online"] = True
        dev["state"] = "idle"
        dev["last_seen"] = now
        
        if ws:
            self.active_sockets[clean_mac] = ws
            
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
                if d.get("firmware") != TARGET_FIRMWARE_VERSION:
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
                dev_copy.get("firmware") != TARGET_FIRMWARE_VERSION
            )
            result.append(dev_copy)
        return result

    async def send_command(self, mac: str, command: Dict[str, Any]) -> bool:
        """Отправка JSON-команды на подключенное устройство по WebSocket."""
        clean_mac = mac.strip().lower()
        ws = self.active_sockets.get(clean_mac)
        if ws and not getattr(ws, "closed", False):
            try:
                await ws.send(json.dumps(command))
                return True
            except Exception as e:
                logger.error(f"Error sending command to {clean_mac}: {e}")
                return False
        return False

    async def update_device_config(self, mac: str, new_config: Dict[str, Any]) -> bool:
        """Обновление конфигурации для одного устройства."""
        clean_mac = mac.strip().lower()
        dev = self.devices.get(clean_mac)
        if not dev:
            return False
            
        dev.setdefault("config", {})
        dev["config"].update(new_config)
        self._save()
        
        # Если онлайн, отправляем команду применения на устройство
        await self.send_command(clean_mac, {
            "type": "set_config",
            "config": new_config
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
        
        for mac, dev in self.devices.items():
            if is_all or mac in target_macs:
                dev.setdefault("config", {})
                dev["config"].update(field_mask_config)
                
                # Шлем на устройство команду
                sent = await self.send_command(mac, {
                    "type": "set_config",
                    "config": field_mask_config
                })
                results[mac] = sent
                self._notify("config_updated", dev)
                
        self._save()
        return results

    def set_device_area(self, mac: str, area_name: Optional[str]):
        clean_mac = mac.strip().lower()
        if clean_mac in self.devices:
            self.devices[clean_mac]["area_name"] = area_name

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
