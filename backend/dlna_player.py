"""
DLNA Digital Media Renderer (DMR) & Streaming Audio Bridge for Porfiriy Voice Assistant.
Позволяет Home Assistant и Music Assistant обнаруживать колонки как media_player.
Транскодирует любые аудиопотоки (радио, MP3, FLAC, Spotify, TTS) через ffmpeg в 16-бит PCM
и транслирует напрямую в WebSocket подключенной ESP32 колонки.
"""

from __future__ import annotations
import os
import sys
import math
import time
import socket
import asyncio
import logging
import subprocess
import xml.etree.ElementTree as ET
from typing import Dict, Any, Optional
from aiohttp import web

logger = logging.getLogger("media_player")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

# Стандартные XML описания сервисов UPnP / DLNA
AVT_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>SetAVTransportURI</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>CurrentURI</name><direction>in</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
        <argument><name>CurrentURIMetaData</name><direction>in</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>Play</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>Speed</name><direction>in</direction><relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>Pause</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>Stop</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetTransportInfo</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>CurrentTransportState</name><direction>out</direction><relatedStateVariable>TransportState</relatedStateVariable></argument>
        <argument><name>CurrentTransportStatus</name><direction>out</direction><relatedStateVariable>TransportStatus</relatedStateVariable></argument>
        <argument><name>CurrentSpeed</name><direction>out</direction><relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetPositionInfo</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>Track</name><direction>out</direction><relatedStateVariable>CurrentTrack</relatedStateVariable></argument>
        <argument><name>TrackDuration</name><direction>out</direction><relatedStateVariable>CurrentTrackDuration</relatedStateVariable></argument>
        <argument><name>TrackMetaData</name><direction>out</direction><relatedStateVariable>CurrentTrackMetaData</relatedStateVariable></argument>
        <argument><name>TrackURI</name><direction>out</direction><relatedStateVariable>CurrentTrackURI</relatedStateVariable></argument>
        <argument><name>RelTime</name><direction>out</direction><relatedStateVariable>RelativeTimePosition</relatedStateVariable></argument>
        <argument><name>AbsTime</name><direction>out</direction><relatedStateVariable>AbsoluteTimePosition</relatedStateVariable></argument>
        <argument><name>RelCount</name><direction>out</direction><relatedStateVariable>RelativeCounterPosition</relatedStateVariable></argument>
        <argument><name>AbsCount</name><direction>out</direction><relatedStateVariable>AbsoluteCounterPosition</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetMediaInfo</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>NrTracks</name><direction>out</direction><relatedStateVariable>NumberOfTracks</relatedStateVariable></argument>
        <argument><name>MediaDuration</name><direction>out</direction><relatedStateVariable>CurrentMediaDuration</relatedStateVariable></argument>
        <argument><name>CurrentURI</name><direction>out</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
        <argument><name>CurrentURIMetaData</name><direction>out</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
        <argument><name>NextURI</name><direction>out</direction><relatedStateVariable>NextAVTransportURI</relatedStateVariable></argument>
        <argument><name>NextURIMetaData</name><direction>out</direction><relatedStateVariable>NextAVTransportURIMetaData</relatedStateVariable></argument>
        <argument><name>PlayMedium</name><direction>out</direction><relatedStateVariable>PlaybackStorageMedium</relatedStateVariable></argument>
        <argument><name>RecordMedium</name><direction>out</direction><relatedStateVariable>RecordStorageMedium</relatedStateVariable></argument>
        <argument><name>WriteStatus</name><direction>out</direction><relatedStateVariable>RecordMediumWriteStatus</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>TransportState</name><dataType>string</dataType><defaultValue>STOPPED</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>TransportStatus</name><dataType>string</dataType><defaultValue>OK</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>TransportPlaySpeed</name><dataType>string</dataType><defaultValue>1</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>AVTransportURI</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>AVTransportURIMetaData</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrack</name><dataType>ui4</dataType><defaultValue>0</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrackDuration</name><dataType>string</dataType><defaultValue>00:00:00</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrackMetaData</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrackURI</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>RelativeTimePosition</name><dataType>string</dataType><defaultValue>00:00:00</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>AbsoluteTimePosition</name><dataType>string</dataType><defaultValue>00:00:00</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>RelativeCounterPosition</name><dataType>i4</dataType><defaultValue>0</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>AbsoluteCounterPosition</name><dataType>i4</dataType><defaultValue>0</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>NumberOfTracks</name><dataType>ui4</dataType><defaultValue>0</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentMediaDuration</name><dataType>string</dataType><defaultValue>00:00:00</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>NextAVTransportURI</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>NextAVTransportURIMetaData</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>PlaybackStorageMedium</name><dataType>string</dataType><defaultValue>NETWORK</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>RecordStorageMedium</name><dataType>string</dataType><defaultValue>NOT_IMPLEMENTED</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>RecordMediumWriteStatus</name><dataType>string</dataType><defaultValue>NOT_IMPLEMENTED</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""

RC_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetVolume</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
        <argument><name>CurrentVolume</name><direction>out</direction><relatedStateVariable>Volume</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>SetVolume</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
        <argument><name>DesiredVolume</name><direction>in</direction><relatedStateVariable>Volume</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetMute</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
        <argument><name>CurrentMute</name><direction>out</direction><relatedStateVariable>Mute</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>SetMute</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
        <argument><name>DesiredMute</name><direction>in</direction><relatedStateVariable>Mute</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>Volume</name><dataType>ui2</dataType><defaultValue>50</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>Mute</name><dataType>boolean</dataType><defaultValue>0</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Channel</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""

CM_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetProtocolInfo</name>
      <argumentList>
        <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
        <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetCurrentConnectionIDs</name>
      <argumentList>
        <argument><name>ConnectionIDs</name><direction>out</direction><relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetCurrentConnectionInfo</name>
      <argumentList>
        <argument><name>ConnectionID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
        <argument><name>RcsID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_RcsID</relatedStateVariable></argument>
        <argument><name>AVTransportID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_AVTransportID</relatedStateVariable></argument>
        <argument><name>ProtocolInfo</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ProtocolInfo</relatedStateVariable></argument>
        <argument><name>PeerConnectionManager</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionManager</relatedStateVariable></argument>
        <argument><name>PeerConnectionID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
        <argument><name>Direction</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Direction</relatedStateVariable></argument>
        <argument><name>Status</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionStatus</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SinkProtocolInfo</name><dataType>string</dataType><defaultValue>http-get:*:audio/mpeg:*,http-get:*:audio/mp3:*,http-get:*:audio/wav:*,http-get:*:audio/x-wav:*,http-get:*:audio/flac:*,http-get:*:audio/aac:*,http-get:*:audio/ogg:*</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentConnectionIDs</name><dataType>string</dataType><defaultValue>0</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name><dataType>i4</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""


class AudioPlayerSession:
    """Сессия воспроизведения одного потока для конкретного устройства."""
    def __init__(self, mac: str, device_manager):
        self.mac = mac.strip().lower()
        self.device_manager = device_manager
        self.current_uri = ""
        self.metadata = ""
        self.state = "STOPPED"  # STOPPED, PLAYING, PAUSED_PLAYBACK
        self.start_time = 0.0
        self.process: Optional[asyncio.subprocess.Process] = None
        self.stream_task: Optional[asyncio.Task] = None
        self.volume = 50  # 0..100
        self.is_muted = False

    def get_position_sec(self) -> int:
        if self.state == "PLAYING" and self.start_time > 0:
            return int(time.time() - self.start_time)
        return 0

    def format_time(self, seconds: int) -> str:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    async def play(self, uri: Optional[str] = None):
        if uri:
            self.current_uri = uri
        if not self.current_uri:
            logger.warning(f"[PLAYER] Cannot play: URI is empty for {self.mac}")
            return False

        # Останавливаем предыдущий поток если был
        await self.stop(silent=True)

        ws = self.device_manager.active_sockets.get(self.mac)
        if not ws or getattr(ws, "closed", False):
            logger.warning(f"[PLAYER] Device {self.mac} is offline, cannot play media.")
            return False

        logger.info(f"[PLAYER] Starting playback on {self.mac}: {self.current_uri}")
        self.state = "PLAYING"
        self.start_time = time.time()
        self.device_manager.set_device_state(self.mac, "speaking")

        # Переводим колонку в состояние воспроизведения
        try:
            await ws.send('{"type":"speaking"}')
        except Exception:
            pass

        self.stream_task = asyncio.create_task(self._ffmpeg_stream_worker())
        return True

    async def _ffmpeg_stream_worker(self):
        """Запускает ffmpeg, читает 16-бит PCM и льет пакетами в сокет колонки."""
        ws = self.device_manager.active_sockets.get(self.mac)
        if not ws:
            self.state = "STOPPED"
            return

        # Запуск транскодера ffmpeg
        # -re читает со скоростью реального времени (real-time pacing)
        # s16le, 16000 Hz, 1 channel (Mono) - нативный формат прошивки ESP32
        cmd = [
            "ffmpeg",
            "-re",
            "-i", self.current_uri,
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            "-loglevel", "error",
            "pipe:1"
        ]

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            # Чанк 1024 байта (512 сэмплов = 32 мс)
            CHUNK_SIZE = 1024
            while self.state == "PLAYING" and self.process and self.process.returncode is None:
                chunk = await self.process.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break

                ws = self.device_manager.active_sockets.get(self.mac)
                if not ws or getattr(ws, "closed", False):
                    break

                # Применяем громкость колонки
                dev_cfg = self.device_manager.get_device_config(self.mac)
                vol_factor = float(dev_cfg.get("speaker_volume", 0.5))
                if self.is_muted:
                    vol_factor = 0.0

                if vol_factor != 1.0:
                    import numpy as np
                    samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                    scaled = np.clip(samples * vol_factor, -32768, 32767).astype(np.int16)
                    chunk = scaled.tobytes()

                await ws.send(chunk)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[PLAYER] Error streaming audio to {self.mac}: {e}")
        finally:
            if self.process:
                try:
                    self.process.terminate()
                    await self.process.wait()
                except Exception:
                    pass
                self.process = None

            if self.state == "PLAYING":
                self.state = "STOPPED"
                self.device_manager.set_device_state(self.mac, "idle")
                try:
                    ws = self.device_manager.active_sockets.get(self.mac)
                    if ws and not getattr(ws, "closed", False):
                        await ws.send('{"type":"sleep"}')
                except Exception:
                    pass

    async def pause(self):
        if self.state == "PLAYING":
            self.state = "PAUSED_PLAYBACK"
            if self.process:
                try:
                    self.process.terminate()
                except Exception:
                    pass
                self.process = None
            if self.stream_task and not self.stream_task.done():
                self.stream_task.cancel()
            self.device_manager.set_device_state(self.mac, "idle")
            ws = self.device_manager.active_sockets.get(self.mac)
            if ws and not getattr(ws, "closed", False):
                try:
                    await ws.send('{"type":"sleep"}')
                except Exception:
                    pass
            logger.info(f"[PLAYER] Paused playback for {self.mac}")
            return True
        return False

    async def stop(self, silent: bool = False):
        self.state = "STOPPED"
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
            self.process = None
        if self.stream_task and not self.stream_task.done():
            self.stream_task.cancel()
        self.stream_task = None
        self.device_manager.set_device_state(self.mac, "idle")
        if not silent:
            ws = self.device_manager.active_sockets.get(self.mac)
            if ws and not getattr(ws, "closed", False):
                try:
                    await ws.send('{"type":"sleep"}')
                except Exception:
                    pass
            logger.info(f"[PLAYER] Stopped playback for {self.mac}")
        return True

    def set_volume(self, volume: int):
        self.volume = max(0, min(100, volume))
        # Синхронизируем с speaker_volume (0.0..1.0)
        spk_vol = round(self.volume / 100.0, 2)
        asyncio.create_task(self.device_manager.update_device_config(self.mac, {"speaker_volume": spk_vol}))


class DLNAMediaManager:
    """Управляет медиаплеерами и UPnP/DLNA DMR интерфейсом для колонок."""
    def __init__(self, device_manager, port: int = 8099):
        self.device_manager = device_manager
        self.port = port
        self.players: Dict[str, AudioPlayerSession] = {}
        self.ssdp_server_task: Optional[asyncio.Task] = None

    def get_player(self, mac: str) -> AudioPlayerSession:
        clean_mac = mac.strip().lower()
        if clean_mac not in self.players:
            self.players[clean_mac] = AudioPlayerSession(clean_mac, self.device_manager)
        return self.players[clean_mac]

    def get_host_ip(self) -> str:
        """Определяет локальный IP-адрес сервера для SSDP анонса."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    # --- HTTP роуты DLNA ---
    def register_routes(self, app: web.Application):
        app.router.add_get("/dmr/{mac}/description.xml", self.handle_description_xml)
        app.router.add_get("/dmr/{mac}/avt.xml", self.handle_avt_scpd)
        app.router.add_get("/dmr/{mac}/rc.xml", self.handle_rc_scpd)
        app.router.add_get("/dmr/{mac}/cm.xml", self.handle_cm_scpd)
        app.router.add_post("/dmr/{mac}/avt_control", self.handle_avt_control)
        app.router.add_post("/dmr/{mac}/rc_control", self.handle_rc_control)
        app.router.add_post("/dmr/{mac}/cm_control", self.handle_cm_control)
        
        # REST API для прямого управления медиа
        app.router.add_post("/api/devices/{mac}/play_media", self.handle_rest_play)
        app.router.add_post("/api/devices/{mac}/stop_media", self.handle_rest_stop)
        app.router.add_post("/api/devices/{mac}/pause_media", self.handle_rest_pause)
        app.router.add_get("/api/devices/{mac}/media_status", self.handle_rest_status)

    async def handle_description_xml(self, request: web.Request):
        mac = request.match_info["mac"].strip().lower()
        dev = self.device_manager.get_device(mac) or {}
        name = dev.get("name") or f"Колонка {mac[-5:].replace(':', '')}"
        clean_mac = mac.replace(":", "").replace("-", "")

        host = request.host
        xml_content = f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Порфирий ({name})</friendlyName>
    <manufacturer>Porfiriy Voice Assistant</manufacturer>
    <manufacturerURL>https://github.com/d3890666/Porfiriy-Voice-Assistant</manufacturerURL>
    <modelDescription>Porfiriy Smart Speaker Media Renderer</modelDescription>
    <modelName>Porfiriy DMR</modelName>
    <modelNumber>1.0</modelNumber>
    <modelURL>https://github.com/d3890666/Porfiriy-Voice-Assistant</modelURL>
    <UDN>uuid:porfiriy-dmr-{clean_mac}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <SCPDURL>/dmr/{mac}/rc.xml</SCPDURL>
        <controlURL>/dmr/{mac}/rc_control</controlURL>
        <eventSubURL>/dmr/{mac}/rc_events</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/dmr/{mac}/cm.xml</SCPDURL>
        <controlURL>/dmr/{mac}/cm_control</controlURL>
        <eventSubURL>/dmr/{mac}/cm_events</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <SCPDURL>/dmr/{mac}/avt.xml</SCPDURL>
        <controlURL>/dmr/{mac}/avt_control</controlURL>
        <eventSubURL>/dmr/{mac}/avt_events</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>"""
        return web.Response(text=xml_content, content_type="text/xml")

    async def handle_avt_scpd(self, request: web.Request):
        return web.Response(text=AVT_SCPD, content_type="text/xml")

    async def handle_rc_scpd(self, request: web.Request):
        return web.Response(text=RC_SCPD, content_type="text/xml")

    async def handle_cm_scpd(self, request: web.Request):
        return web.Response(text=CM_SCPD, content_type="text/xml")

    def _soap_response(self, action: str, service: str, body_xml: str) -> web.Response:
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:{action}Response xmlns:u="urn:schemas-upnp-org:service:{service}:1">
      {body_xml}
    </u:{action}Response>
  </s:Body>
</s:Envelope>"""
        return web.Response(text=content, content_type="text/xml; charset=utf-8")

    async def handle_avt_control(self, request: web.Request):
        mac = request.match_info["mac"].strip().lower()
        player = self.get_player(mac)
        body = await request.text()

        if "SetAVTransportURI" in body:
            m = re.search(r"<CurrentURI>(.*?)</CurrentURI>", body)
            if m:
                player.current_uri = m.group(1).replace("&amp;", "&")
                logger.info(f"[DLNA] SetAVTransportURI for {mac}: {player.current_uri}")
            return self._soap_response("SetAVTransportURI", "AVTransport", "")

        elif "Play" in body:
            await player.play()
            return self._soap_response("Play", "AVTransport", "")

        elif "Pause" in body:
            await player.pause()
            return self._soap_response("Pause", "AVTransport", "")

        elif "Stop" in body:
            await player.stop()
            return self._soap_response("Stop", "AVTransport", "")

        elif "GetTransportInfo" in body:
            res = f"""<CurrentTransportState>{player.state}</CurrentTransportState>
<CurrentTransportStatus>OK</CurrentTransportStatus>
<CurrentSpeed>1</CurrentSpeed>"""
            return self._soap_response("GetTransportInfo", "AVTransport", res)

        elif "GetPositionInfo" in body:
            pos_str = player.format_time(player.get_position_sec())
            res = f"""<Track>1</Track>
<TrackDuration>00:00:00</TrackDuration>
<TrackMetaData></TrackMetaData>
<TrackURI>{player.current_uri}</TrackURI>
<RelTime>{pos_str}</RelTime>
<AbsTime>{pos_str}</AbsTime>
<RelCount>0</RelCount>
<AbsCount>0</AbsCount>"""
            return self._soap_response("GetPositionInfo", "AVTransport", res)

        elif "GetMediaInfo" in body:
            res = f"""<NrTracks>1</NrTracks>
<MediaDuration>00:00:00</MediaDuration>
<CurrentURI>{player.current_uri}</CurrentURI>
<CurrentURIMetaData></CurrentURIMetaData>
<NextURI></NextURI>
<NextURIMetaData></NextURIMetaData>
<PlayMedium>NETWORK</PlayMedium>
<RecordMedium>NOT_IMPLEMENTED</RecordMedium>
<WriteStatus>NOT_IMPLEMENTED</WriteStatus>"""
            return self._soap_response("GetMediaInfo", "AVTransport", res)

        return self._soap_response("AVTransport", "AVTransport", "")

    async def handle_rc_control(self, request: web.Request):
        mac = request.match_info["mac"].strip().lower()
        player = self.get_player(mac)
        body = await request.text()

        if "GetVolume" in body:
            dev_cfg = self.device_manager.get_device_config(mac)
            vol_pct = int(float(dev_cfg.get("speaker_volume", 0.5)) * 100)
            return self._soap_response("GetVolume", "RenderingControl", f"<CurrentVolume>{vol_pct}</CurrentVolume>")

        elif "SetVolume" in body:
            m = re.search(r"<DesiredVolume>(\d+)</DesiredVolume>", body)
            if m:
                vol = int(m.group(1))
                player.set_volume(vol)
                logger.info(f"[DLNA] SetVolume for {mac}: {vol}%")
            return self._soap_response("SetVolume", "RenderingControl", "")

        elif "GetMute" in body:
            return self._soap_response("GetMute", "RenderingControl", f"<CurrentMute>{1 if player.is_muted else 0}</CurrentMute>")

        elif "SetMute" in body:
            m = re.search(r"<DesiredMute>(\d+)</DesiredMute>", body)
            if m:
                player.is_muted = (m.group(1) == "1")
            return self._soap_response("SetMute", "RenderingControl", "")

        return self._soap_response("RenderingControl", "RenderingControl", "")

    async def handle_cm_control(self, request: web.Request):
        body = await request.text()
        if "GetProtocolInfo" in body:
            res = """<Source></Source>
<Sink>http-get:*:audio/mpeg:*,http-get:*:audio/mp3:*,http-get:*:audio/wav:*,http-get:*:audio/x-wav:*,http-get:*:audio/flac:*,http-get:*:audio/aac:*</Sink>"""
            return self._soap_response("GetProtocolInfo", "ConnectionManager", res)
        elif "GetCurrentConnectionIDs" in body:
            return self._soap_response("GetCurrentConnectionIDs", "ConnectionManager", "<ConnectionIDs>0</ConnectionIDs>")
        return self._soap_response("ConnectionManager", "ConnectionManager", "")

    # --- REST API Handlers ---
    async def handle_rest_play(self, request: web.Request):
        mac = request.match_info["mac"].strip().lower()
        player = self.get_player(mac)
        try:
            data = await request.json()
            url = data.get("url")
            if not url:
                return web.json_response({"success": False, "error": "URL is required"}, status=400)
            success = await player.play(url)
            return web.json_response({"success": success, "state": player.state, "url": url})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_rest_stop(self, request: web.Request):
        mac = request.match_info["mac"].strip().lower()
        player = self.get_player(mac)
        await player.stop()
        return web.json_response({"success": True, "state": "STOPPED"})

    async def handle_rest_pause(self, request: web.Request):
        mac = request.match_info["mac"].strip().lower()
        player = self.get_player(mac)
        await player.pause()
        return web.json_response({"success": True, "state": player.state})

    async def handle_rest_status(self, request: web.Request):
        mac = request.match_info["mac"].strip().lower()
        player = self.get_player(mac)
        return web.json_response({
            "mac": mac,
            "state": player.state,
            "current_uri": player.current_uri,
            "position_sec": player.get_position_sec(),
            "volume": player.volume,
            "is_muted": player.is_muted
        })

    # --- SSDP Discovery Server ---
    async def start_ssdp(self):
        """Фоновый сервер SSDP для авто-обнаружения в Home Assistant."""
        loop = asyncio.get_running_loop()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except Exception:
                pass
            sock.bind(("", SSDP_PORT))
            mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.setblocking(False)
            logger.info(f"[SSDP] Listening for DLNA/UPnP discovery on {SSDP_ADDR}:{SSDP_PORT}")

            while True:
                try:
                    data, addr = await loop.sock_recvfrom(sock, 2048)
                    text = data.decode("utf-8", errors="ignore")
                    if "M-SEARCH" in text and ("ssdp:all" in text or "MediaRenderer" in text or "rootdevice" in text):
                        # Отправляем ответы для каждого онлайн устройства
                        host_ip = self.get_host_ip()
                        for mac, dev in self.device_manager.devices.items():
                            if not dev.get("is_online", False):
                                continue
                            clean_mac = mac.replace(":", "").replace("-", "").lower()
                            location = f"http://{host_ip}:{self.port}/dmr/{mac}/description.xml"
                            response = (
                                "HTTP/1.1 200 OK\r\n"
                                "CACHE-CONTROL: max-age=1800\r\n"
                                f"DATE: {time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}\r\n"
                                "EXT:\r\n"
                                f"LOCATION: {location}\r\n"
                                "SERVER: Linux/5.0 UPnP/1.0 Porfiriy/1.0\r\n"
                                "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
                                f"USN: uuid:porfiriy-dmr-{clean_mac}::urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
                                "\r\n"
                            )
                            await loop.sock_sendto(sock, response.encode("utf-8"), addr)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"[SSDP] Error: {e}")
                    await asyncio.sleep(1.0)
        except Exception as ex:
            logger.info(f"[SSDP] Multicast bind notice: {ex} (Manual URL entry in HA DLNA DMR is always available).")
        finally:
            try:
                sock.close()
            except Exception:
                pass
