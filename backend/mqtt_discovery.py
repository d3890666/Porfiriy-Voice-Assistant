import json
import logging
import asyncio
import os
from typing import Dict, Any, Optional
import paho.mqtt.client as mqtt

logger = logging.getLogger("mqtt_discovery")

class MQTTDiscoveryManager:
    def __init__(self, device_manager, options: Optional[Dict[str, Any]] = None):
        self.device_manager = device_manager
        self.options = options or {}
        self.client: Optional[mqtt.Client] = None
        self.is_connected = False
        self.loop = None
        
        # Подписываемся на события менеджера устройств
        self.device_manager.add_subscriber(self._on_device_event)

    def _resolve_broker_config(self):
        """Определяет параметры подключения к MQTT брокеру."""
        host = self.options.get("mqtt_host") or os.environ.get("HA_MQTT_HOST") or ""
        port = int(self.options.get("mqtt_port") or os.environ.get("HA_MQTT_PORT") or 1883)
        user = self.options.get("mqtt_username") or os.environ.get("HA_MQTT_USERNAME") or ""
        password = self.options.get("mqtt_password") or os.environ.get("HA_MQTT_PASSWORD") or ""
        
        # Если запущено в HA и имя хоста пустое, пробуем стандартный сервис Mosquitto
        if not host and (os.path.exists("/data") or os.environ.get("SUPERVISOR_TOKEN")):
            host = "core-mosquitto"
            
        return host, port, user, password

    async def start(self):
        """Асинхронный запуск MQTT клиента."""
        self.loop = asyncio.get_running_loop()
        host, port, user, password = self._resolve_broker_config()
        
        if not host:
            logger.info("MQTT Broker is not configured. MQTT Discovery is disabled.")
            return

        try:
            # paho-mqtt v2 compatible callback API
            try:
                self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="porfiriy_addon_bridge")
            except Exception:
                self.client = mqtt.Client(client_id="porfiriy_addon_bridge")
                
            if user:
                self.client.username_pw_set(user, password)
                
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            
            logger.info(f"Connecting to MQTT Broker at {host}:{port} ...")
            # Подключаемся в фоновом потоке
            await asyncio.to_thread(self.client.connect, host, port, 60)
            self.client.loop_start()
            
            # Регистрируем все уже известные устройства
            for dev in self.device_manager.get_all_devices():
                await self.publish_device_discovery(dev)
                await self.publish_device_state(dev)
        except Exception as e:
            logger.warning(f"Could not connect to MQTT broker ({host}:{port}): {e}. Will retry later if available.")

    def stop(self):
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.is_connected = False

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self.is_connected = True
            logger.info("Connected to MQTT Broker successfully!")
            # Подписываемся на команды управления колонками
            self.client.subscribe("porfiriy/+/set/#")
        else:
            logger.warning(f"MQTT connect failed with result code: {rc}")

    def _on_disconnect(self, client, userdata, rc, properties=None):
        self.is_connected = False
        logger.info("Disconnected from MQTT Broker.")

    def _on_message(self, client, userdata, msg):
        """Обработка входящих команд управления из Home Assistant."""
        try:
            topic = msg.topic
            payload = msg.payload.decode("utf-8").strip()
            logger.info(f"MQTT Command received: {topic} -> {payload}")
            
            # Топик: porfiriy/<dev_id>/set/<param>
            parts = topic.split("/")
            if len(parts) >= 4 and parts[0] == "porfiriy" and parts[2] == "set":
                dev_id = parts[1]
                param = parts[3]
                
                # Ищем устройство по clean_mac / dev_id
                target_mac = None
                for d in self.device_manager.get_all_devices():
                    clean_mac = d["mac"].replace(":", "").replace("-", "").lower()
                    if clean_mac == dev_id or d["mac"].lower() == dev_id:
                        target_mac = d["mac"]
                        break
                        
                if not target_mac:
                    return
                    
                if param == "volume":
                    vol = float(payload)
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(
                            self.device_manager.update_device_config(target_mac, {"speaker_volume": vol}),
                            self.loop
                        )
                elif param == "mic_gain":
                    gain = int(payload)
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(
                            self.device_manager.update_device_config(target_mac, {"mic_gain": gain}),
                            self.loop
                        )
                elif param == "wake_threshold":
                    th = float(payload)
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(
                            self.device_manager.update_device_config(target_mac, {"wake_word_threshold": th}),
                            self.loop
                        )
                elif param == "barge_in":
                    barge_in = payload.upper() == "ON"
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(
                            self.device_manager.update_device_config(target_mac, {"enable_barge_in": barge_in}),
                            self.loop
                        )
                elif param == "beep":
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(
                            self.device_manager.send_command(target_mac, {"type": "beep"}),
                            self.loop
                        )
                elif param == "reboot":
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(
                            self.device_manager.send_command(target_mac, {"type": "reboot"}),
                            self.loop
                        )
        except Exception as e:
            logger.error(f"Error handling MQTT message: {e}")

    def _on_device_event(self, event_type: str, device: Dict[str, Any]):
        """Реакция на события DeviceManager."""
        if not self.is_connected or not self.client:
            return
        if event_type == "registered":
            asyncio.create_task(self.publish_device_discovery(device))
            asyncio.create_task(self.publish_device_state(device))
        else:
            asyncio.create_task(self.publish_device_state(device))

    async def publish_device_discovery(self, device: Dict[str, Any]):
        """Публикация конфигураций Home Assistant MQTT Discovery."""
        if not self.is_connected or not self.client:
            return
            
        clean_mac = device["mac"].replace(":", "").replace("-", "").lower()
        dev_name = device.get("name") or f"Porfiriy Speaker {clean_mac[-6:]}"
        
        device_dict = {
            "identifiers": [f"porfiriy_{clean_mac}"],
            "name": dev_name,
            "manufacturer": "Porfiriy AI",
            "model": "ESP32-S3 Voice Satellite",
            "sw_version": device.get("firmware", "0.0.52"),
            "configuration_url": f"http://{device.get('ip', '')}" if device.get('ip') else None
        }
        
        state_topic = f"porfiriy/{clean_mac}/state"
        
        entities = [
            # 1. RSSI (Sensor)
            ("sensor", "rssi", {
                "name": "Wi-Fi Signal",
                "unique_id": f"porfiriy_{clean_mac}_rssi",
                "state_topic": state_topic,
                "value_template": "{{ value_json.rssi }}",
                "unit_of_measurement": "dBm",
                "device_class": "signal_strength",
                "entity_category": "diagnostic",
                "device": device_dict
            }),
            # 2. State (Sensor)
            ("sensor", "state", {
                "name": "Voice State",
                "unique_id": f"porfiriy_{clean_mac}_state",
                "state_topic": state_topic,
                "value_template": "{{ value_json.state }}",
                "icon": "mdi:microphone",
                "device": device_dict
            }),
            # 3. Volume (Number)
            ("number", "volume", {
                "name": "Speaker Volume",
                "unique_id": f"porfiriy_{clean_mac}_volume",
                "state_topic": state_topic,
                "value_template": "{{ value_json.volume }}",
                "command_topic": f"porfiriy/{clean_mac}/set/volume",
                "min": 0.1,
                "max": 2.0,
                "step": 0.05,
                "icon": "mdi:volume-high",
                "device": device_dict
            }),
            # 4. Wake Word Threshold (Number)
            ("number", "wake_threshold", {
                "name": "Wake Word Sensitivity",
                "unique_id": f"porfiriy_{clean_mac}_wake_threshold",
                "state_topic": state_topic,
                "value_template": "{{ value_json.wake_threshold }}",
                "command_topic": f"porfiriy/{clean_mac}/set/wake_threshold",
                "min": 0.80,
                "max": 0.99,
                "step": 0.01,
                "icon": "mdi:ear-hearing",
                "entity_category": "config",
                "device": device_dict
            }),
            # 5. Mic Gain (Number)
            ("number", "mic_gain", {
                "name": "Microphone Gain",
                "unique_id": f"porfiriy_{clean_mac}_mic_gain",
                "state_topic": state_topic,
                "value_template": "{{ value_json.mic_gain }}",
                "command_topic": f"porfiriy/{clean_mac}/set/mic_gain",
                "min": 1,
                "max": 10,
                "step": 1,
                "icon": "mdi:microphone-settings",
                "entity_category": "config",
                "device": device_dict
            }),
            # 6. Barge-in (Switch)
            ("switch", "barge_in", {
                "name": "Barge-in Interrupt",
                "unique_id": f"porfiriy_{clean_mac}_barge_in",
                "state_topic": state_topic,
                "value_template": "{{ value_json.barge_in }}",
                "command_topic": f"porfiriy/{clean_mac}/set/barge_in",
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:account-voice",
                "entity_category": "config",
                "device": device_dict
            }),
            # 7. Beep (Button)
            ("button", "beep", {
                "name": "Play Sound Test (Beep)",
                "unique_id": f"porfiriy_{clean_mac}_beep",
                "command_topic": f"porfiriy/{clean_mac}/set/beep",
                "payload_press": "PRESS",
                "icon": "mdi:bell-ring",
                "device": device_dict
            }),
            # 8. Reboot (Button)
            ("button", "reboot", {
                "name": "Restart Device",
                "unique_id": f"porfiriy_{clean_mac}_reboot",
                "command_topic": f"porfiriy/{clean_mac}/set/reboot",
                "payload_press": "PRESS",
                "device_class": "restart",
                "entity_category": "diagnostic",
                "device": device_dict
            })
        ]
        
        for component, object_id, payload in entities:
            topic = f"homeassistant/{component}/{clean_mac}_{object_id}/config"
            self.client.publish(topic, json.dumps(payload), retain=True)

    async def publish_device_state(self, device: Dict[str, Any]):
        """Публикация текущей телеметрии и состояний колонок."""
        if not self.is_connected or not self.client:
            return
            
        clean_mac = device["mac"].replace(":", "").replace("-", "").lower()
        cfg = device.get("config", {})
        
        payload = {
            "rssi": device.get("rssi", -60),
            "state": device.get("state", "idle"),
            "is_online": device.get("is_online", False),
            "volume": cfg.get("speaker_volume", 1.0),
            "mic_gain": cfg.get("mic_gain", 2),
            "wake_threshold": cfg.get("wake_word_threshold", 0.93),
            "barge_in": "ON" if cfg.get("enable_barge_in", False) else "OFF",
            "uptime": device.get("uptime", 0),
            "ip": device.get("ip", "")
        }
        
        topic = f"porfiriy/{clean_mac}/state"
        self.client.publish(topic, json.dumps(payload), retain=False)
