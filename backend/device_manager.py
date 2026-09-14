import os
import json
import time
import asyncio
import logging
from typing import Dict, List, Any, Optional, Callable

logger = logging.getLogger("device_manager")

DEFAULT_DEVICE_CONFIG = {
    "mic_gain": 2,
    "speaker_volume": 1.0,
    "wake_word_threshold": 0.93,
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
            
        dev["is_online"] = True
        dev["state"] = "idle"
        dev["last_seen"] = now
        
        if ws:
            self.active_sockets[clean_mac] = ws
            
        self._save()
        self._notify("registered", dev)
        logger.info(f"Device registered: {clean_mac} ({dev['name']}) @ {dev['ip']}")
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

    def get_all_devices(self) -> List[Dict[str, Any]]:
        return list(self.devices.values())

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
