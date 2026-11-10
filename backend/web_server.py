from __future__ import annotations
import os
import re
import json
import asyncio
import logging
import aiohttp
from aiohttp import web
from typing import Optional, Dict, Any, Callable, List

logger = logging.getLogger("web_server")

try:
    from .dlna_player import DLNAMediaManager
except ImportError:
    from dlna_player import DLNAMediaManager

try:
    from .openai_service import OpenAIService
except ImportError:
    from openai_service import OpenAIService

try:
    from .quasar_manager import QuasarManager
except ImportError:
    from quasar_manager import QuasarManager

def get_addon_version() -> str:
    """Считывает актуальную версию аддона напрямую из config.yaml (Single Source of Truth)."""
    candidates = [
        "/backend/config.yaml",
        os.path.join(os.path.dirname(__file__), "config.yaml"),
        os.path.join(os.path.dirname(__file__), "..", "config.yaml"),
        "/config.yaml"
    ]
    for p in candidates:
        if os.path.exists(p) and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    content = f.read()
                m = re.search(r'version:\s*["\']?([^"\'\s]+)["\']?', content)
                if m:
                    return m.group(1).strip()
            except Exception as e:
                logger.debug(f"Error reading {p}: {e}")
    return "0.0.106"

class WebServer:
    def __init__(self, device_manager, ha_api, options_callback, options_save_callback: Optional[Callable[[Dict[str, Any]], None]] = None, phrase_manager=None, models_callback: Optional[Callable[[], List[Dict[str, Any]]]] = None, refresh_models_callback=None, ww_engine_callback=None, port: int = 8099):
        self.device_manager = device_manager
        self.ha_api = ha_api
        self.options_callback = options_callback
        self.options_save_callback = options_save_callback
        self.phrase_manager = phrase_manager
        self.models_callback = models_callback
        self.refresh_models_callback = refresh_models_callback
        self.ww_engine_callback = ww_engine_callback  # () -> WakeWordEngine | None
        self.port = port
        self.app = web.Application()
        self.sse_queues = set()
        self.static_dir = os.path.join(os.path.dirname(__file__), "static")
        
        # Подписываемся на события device_manager для SSE
        self.device_manager.add_subscriber(self._on_device_event)
        
        # Интеграция DLNA DMR (Digital Media Renderer) для воспроизведения музыки
        self.dlna_manager = DLNAMediaManager(self.device_manager, port=self.port)
        self.dlna_manager.register_routes(self.app)
        self.device_manager.media_manager = self.dlna_manager

        # Интеграция OpenAI-совместимого эндпоинта (Quasar Bridge / Яндекс Станция)
        self.openai_service = OpenAIService(self.device_manager, self.ha_api, self.options_callback)
        self.openai_service.register_routes(self.app)

        # Менеджер управления конфигурацией и маршрутизацией Quasar Bridge
        self.quasar_manager = QuasarManager()

        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/devices", self.handle_get_devices)
        self.app.router.add_post("/api/devices/yandex/add", self.handle_add_yandex_station)
        self.app.router.add_get("/api/quasar/config", self.handle_get_quasar_config)
        self.app.router.add_post("/api/quasar/config", self.handle_save_quasar_config)
        self.app.router.add_get("/api/quasar/version", self.handle_get_quasar_version)
        self.app.router.add_post("/api/quasar/update", self.handle_update_quasar)
        self.app.router.add_post("/api/quasar/squeezelite", self.handle_quasar_squeezelite)
        self.app.router.add_post("/api/quasar/airplay", self.handle_quasar_airplay)
        self.app.router.add_post("/api/quasar/ducking", self.handle_quasar_ducking)
        self.app.router.add_post("/api/station/volume_sync", self.handle_station_volume_sync)
        self.app.router.add_get("/api/quasar/extract-ha-keywords", self.handle_extract_ha_keywords)
        self.app.router.add_post("/api/quasar/test-route", self.handle_quasar_test_route)
        self.app.router.add_delete("/api/devices/{mac}", self.handle_delete_device)
        self.app.router.add_post("/api/devices/{mac}/config", self.handle_device_config)
        self.app.router.add_post("/api/devices/bulk_config", self.handle_bulk_config)
        self.app.router.add_post("/api/devices/{mac}/beep", self.handle_device_beep)
        self.app.router.add_post("/api/devices/{mac}/reboot", self.handle_device_reboot)
        self.app.router.add_post("/api/devices/{mac}/wake", self.handle_device_wake)
        self.app.router.add_get("/api/areas", self.handle_get_areas)
        self.app.router.add_get("/api/media_players", self.handle_get_media_players)
        self.app.router.add_get("/api/global", self.handle_get_global)
        self.app.router.add_post("/api/global", self.handle_set_global)
        self.app.router.add_post("/api/models/refresh", self.handle_refresh_models)
        self.app.router.add_post("/api/phrases/regenerate", self.handle_regenerate_phrases)
        self.app.router.add_get("/api/phrases/status", self.handle_phrases_status)
        self.app.router.add_get("/api/firmware/info", self.handle_firmware_info)
        self.app.router.add_post("/api/devices/{mac}/ota", self.handle_device_ota)
        self.app.router.add_post("/api/devices/bulk_ota", self.handle_bulk_ota)
        self.app.router.add_post("/api/devices/{mac}/mic_test/start", self.handle_mic_test_start)
        self.app.router.add_get("/api/devices/{mac}/mic_test/audio", self.handle_mic_test_audio)
        self.app.router.add_get("/api/devices/{mac}/mic_test/status", self.handle_mic_test_status)
        self.app.router.add_get("/api/devices/{mac}/last_utterance/audio", self.handle_last_utterance_audio)
        self.app.router.add_get("/api/devices/{mac}/last_utterance/status", self.handle_last_utterance_status)
        self.app.router.add_get("/api/events", self.handle_events)
        self.app.router.add_get("/api/ww_monitor", self.handle_ww_monitor)
        self.app.router.add_get("/static/{filename:.*}", self.handle_static)
        # Виртуальные стримеры (pc_streamer)
        self.app.router.add_get("/api/virtual/{mac}/response.wav", self.handle_virtual_response_wav)
        self.app.router.add_post("/api/virtual/upload_model", self.handle_upload_ww_model)
        self.app.router.add_patch("/api/devices/{mac}/streamer_config", self.handle_streamer_config)

    def _on_device_event(self, event_type: str, device: Dict[str, Any]):
        """Рассылка SSE события всем открытым вкладкам веб-интерфейса."""
        msg = json.dumps({"event": event_type, "device": device})
        for q in list(self.sse_queues):
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    async def handle_index(self, request):
        index_file = os.path.join(self.static_dir, "index.html")
        app_js_file = os.path.join(self.static_dir, "app.js")
        version = get_addon_version()
        if os.path.exists(index_file):
            with open(index_file, "r", encoding="utf-8") as f:
                content = f.read()

            # Динамически подставляем актуальную версию из config.yaml в шапку страницы
            content = re.sub(
                r'Голосовой алгоритмический следователь\s*•\s*(?:<span id="app-version">)?v[0-9.]+(?:</span>)?',
                f'Голосовой алгоритмический следователь • <span id="app-version">v{version}</span>',
                content
            )
            content = content.replace("{{VERSION}}", version)
            # Обновляем кэш-бастеры скриптов и стилей (?v=X.X.XX)
            content = re.sub(r'(\?v=)[0-9.]+', rf'\g<1>{version}', content)

            # Автоматически инлайним свежий app.js прямо в HTML для 100% защиты от кэширования в Ingress
            if os.path.exists(app_js_file):
                try:
                    with open(app_js_file, "r", encoding="utf-8") as fjs:
                        js_content = fjs.read()
                    content = re.sub(
                        r'<script\s+src=["\']static/app\.js.*?["\']></script>',
                        lambda _: f'<script>\n{js_content}\n</script>',
                        content
                    )
                except Exception as je:
                    logger.warning(f"Could not inline app.js: {je}")
            return web.Response(
                text=content,
                content_type="text/html",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0"
                }
            )
        return web.Response(text="Porfiriy Web UI loading...", content_type="text/html")

    async def handle_static(self, request):
        filename = request.match_info.get("filename", "")
        filepath = os.path.join(self.static_dir, filename)
        if os.path.exists(filepath) and os.path.isfile(filepath):
            content_type = "text/css" if filename.endswith(".css") else (
                "application/javascript" if filename.endswith(".js") else (
                    "image/png" if filename.endswith(".png") else "application/octet-stream"
                )
            )
            with open(filepath, "rb") as f:
                body = f.read()
            return web.Response(
                body=body,
                content_type=content_type,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0"
                }
            )
        return web.Response(status=404, text="Not Found")

    async def handle_get_devices(self, request):
        devices = self.device_manager.get_all_devices()
        
        # Обновляем имя комнат из HA если доступно
        for d in devices:
            if not d.get("area_name") and self.ha_api:
                area_name = await self.ha_api.get_device_area_name(d["mac"])
                if area_name:
                    d["area_name"] = area_name
                    self.device_manager.set_device_area(d["mac"], area_name)
                    
        return web.json_response({"devices": devices})

    async def handle_delete_device(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        try:
            success = self.device_manager.delete_device(mac)
            if success:
                return web.json_response({"success": True, "mac": mac})
            return web.json_response({"success": False, "error": f"Device {mac} not found"}, status=404)
        except Exception as e:
            logger.error(f"Error deleting device {mac}: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_device_config(self, request):
        mac = request.match_info["mac"]
        try:
            data = await request.json()
            success = await self.device_manager.update_device_config(mac, data)
            return web.json_response({"success": success})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_bulk_config(self, request):
        try:
            body = await request.json()
            target_macs = body.get("target_macs", [])
            field_mask = body.get("fields", {})
            
            if not field_mask:
                return web.json_response({"success": False, "error": "No fields to update"}, status=400)
                
            results = await self.device_manager.bulk_update_config(target_macs, field_mask)
            return web.json_response({"success": True, "results": results})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_device_beep(self, request):
        mac = request.match_info["mac"]
        success = await self.device_manager.send_command(mac, {"type": "beep"})
        return web.json_response({"success": success})

    async def handle_device_reboot(self, request):
        mac = request.match_info["mac"]
        success = await self.device_manager.send_command(mac, {"type": "reboot"})
        return web.json_response({"success": success})

    async def handle_device_wake(self, request):
        mac = request.match_info["mac"]
        success = await self.device_manager.trigger_wake(mac)
        return web.json_response({"success": success})

    async def handle_firmware_info(self, request):
        info = self.device_manager.get_firmware_info()
        return web.json_response(info)

    async def handle_device_ota(self, request):
        mac = request.match_info["mac"]
        success = await self.device_manager.start_ota_update(mac)
        return web.json_response({"success": success})

    async def handle_bulk_ota(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        target_macs = body.get("target_macs", ["all_outdated"])
        started = await self.device_manager.start_bulk_ota(target_macs)
        return web.json_response({"success": True, "started_count": len(started), "devices": started})

    async def handle_get_areas(self, request):
        if self.ha_api:
            areas = await self.ha_api.get_areas()
            return web.json_response({"areas": areas})
        return web.json_response({"areas": []})

    async def handle_get_media_players(self, request):
        if self.ha_api:
            players = await self.ha_api.get_media_players()
            return web.json_response({"media_players": players})
        return web.json_response({"media_players": []})

    def _safe_options(self, opts: Dict[str, Any]) -> Dict[str, Any]:
        safe = dict(opts)
        key = safe.get("gemini_api_key", "")
        safe["has_api_key"] = bool(key)
        if key:
            keys = [k.strip() for k in key.split(",") if k.strip()]
            safe["gemini_api_key"] = ",".join([k[:4] + "..." + k[-4:] if len(k) >= 8 else "********" for k in keys])

        ma_key = safe.get("ma_api_key", "")
        safe["has_ma_api_key"] = bool(ma_key)
        if ma_key:
            safe["ma_api_key"] = ma_key[:4] + "..." + ma_key[-4:] if len(ma_key) >= 8 else "********"

        op_key = safe.get("openai_api_key", "")
        safe["has_openai_api_key"] = bool(op_key)
        if op_key:
            safe["openai_api_key"] = op_key[:3] + "..." + op_key[-3:] if len(op_key) >= 6 else "******"
        return safe

    async def handle_get_global(self, request):
        opts = self.options_callback() if self.options_callback else {}
        models = self.models_callback() if self.models_callback else []
        media_players = []
        if self.ha_api:
            try:
                media_players = await self.ha_api.get_media_players()
            except Exception as e:
                logger.warning(f"Error fetching media players for global settings: {e}")
        return web.json_response({
            "version": get_addon_version(),
            "options": self._safe_options(opts),
            "models": models,
            "media_players": media_players
        })

    async def handle_refresh_models(self, request):
        if self.refresh_models_callback:
            try:
                models = await self.refresh_models_callback()
                return web.json_response({"success": True, "models": models})
            except Exception as e:
                logger.error(f"Error refreshing models via API: {e}")
                return web.json_response({"success": False, "error": str(e)}, status=500)
        return web.json_response({"success": False, "error": "Model refresher not configured"}, status=400)

    async def handle_set_global(self, request):
        try:
            data = await request.json()
            current_opts = self.options_callback() if self.options_callback else {}
            updated_opts = dict(current_opts)
            
            allowed_fields = [
                "gemini_api_key", "gemini_model", "voice_name", "temperature",
                "thinking_timeout_s", "enable_google_search", "vad_silence_duration_ms",
                "enable_barge_in", "barge_in_threshold_rms", "prompt_persona", "prompt_users",
                "prompt_smart_home", "prompt_general",
                "ducking_mode", "enable_media_ducking", "ducking_volume_factor", "default_media_player",
                "ma_api_key", "regenerate_phrases",
                "ww_server_threshold",
                "openai_api_enabled", "openai_api_port", "openai_api_key", "default_airplay_player"
            ]
            
            for field in allowed_fields:
                if field in data:
                    val = data[field]
                    if field == "gemini_api_key":
                        # Если передан пустой или маскированный ключ — оставляем прежний
                        if val and "..." not in str(val) and "*" not in str(val):
                            updated_opts[field] = str(val).strip()
                    elif field == "ma_api_key":
                        if val is not None:
                            sval = str(val).strip()
                            if "..." not in sval and "*" not in sval:
                                updated_opts[field] = sval
                    elif field == "ducking_mode":
                        if str(val) in ["same_area", "all", "disabled"]:
                            updated_opts[field] = str(val)
                            updated_opts["enable_media_ducking"] = (str(val) != "disabled")
                    elif field == "enable_media_ducking":
                        bval = bool(val)
                        updated_opts[field] = bval
                        if not bval:
                            updated_opts["ducking_mode"] = "disabled"
                        elif updated_opts.get("ducking_mode") == "disabled":
                            updated_opts["ducking_mode"] = "same_area"
                    elif field in ["thinking_timeout_s", "vad_silence_duration_ms", "barge_in_threshold_rms", "openai_api_port"]:
                        try:
                            updated_opts[field] = int(val)
                        except (ValueError, TypeError):
                            pass
                    elif field in ["temperature", "ducking_volume_factor"]:
                        try:
                            updated_opts[field] = float(val)
                        except (ValueError, TypeError):
                            pass
                    elif field == "ww_server_threshold":
                        try:
                            new_thr = max(0.5, min(1.0, float(val)))
                            updated_opts[field] = new_thr
                            # Hot-reload в WakeWordEngine
                            if self.ww_engine_callback:
                                engine = self.ww_engine_callback()
                                if engine:
                                    engine.set_threshold(new_thr)
                        except (ValueError, TypeError):
                            pass
                    elif field in ["enable_google_search", "enable_barge_in", "regenerate_phrases", "openai_api_enabled"]:
                        updated_opts[field] = bool(val)
                    elif field in ["openai_api_key", "default_airplay_player"]:
                        sval = str(val).strip()
                        if field == "openai_api_key" and ("..." in sval or "*" in sval):
                            pass
                        else:
                            updated_opts[field] = sval
                    else:
                        updated_opts[field] = str(val)

            # 1. Мгновенно сохраняем в памяти бэкенда (Hot Reload) и локальный файл
            if self.options_save_callback:
                self.options_save_callback(updated_opts)
            
            # 2. Синхронизируем с Home Assistant Supervisor API только те поля, что объявлены в schema config.yaml
            supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
            if supervisor_token:
                try:
                    ha_allowed = {
                        "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password",
                        "debug_mode", "debug_audio"
                    }
                    ha_opts = {k: v for k, v in updated_opts.items() if k in ha_allowed}
                    if ha_opts:
                        async with aiohttp.ClientSession() as session:
                            url = "http://supervisor/addons/self/options"
                            async with session.post(
                                url,
                                headers={
                                    "Authorization": f"Bearer {supervisor_token}",
                                    "Content-Type": "application/json"
                                },
                                json={"options": ha_opts},
                                timeout=aiohttp.ClientTimeout(total=5)
                            ) as resp:
                                if resp.status == 200:
                                    logger.info("Synchronized options with Home Assistant Supervisor.")
                                else:
                                    text_err = await resp.text()
                                    logger.warning(f"Supervisor options sync returned HTTP {resp.status}: {text_err}")
                except Exception as se:
                    logger.warning(f"Failed to sync options with Supervisor: {se}")

            return web.json_response({"success": True, "options": self._safe_options(updated_opts)})
        except Exception as e:
            logger.error(f"Error updating global options: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_regenerate_phrases(self, request):
        if not self.phrase_manager:
            return web.json_response({"success": False, "error": "PhraseManager is not configured"}, status=503)
            
        opts = self.options_callback() if self.options_callback else {}
        asyncio.create_task(self.phrase_manager.regenerate(opts))
        return web.json_response({"success": True, "message": "Phrase regeneration started in background"})

    async def handle_phrases_status(self, request):
        if not self.phrase_manager:
            return web.json_response({"is_ready": False, "total_phrases": 0, "categories": {}, "is_generating": False})
        return web.json_response(self.phrase_manager.get_status())

    async def handle_mic_test_start(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        duration_s = 5.0
        try:
            body = await request.json()
            if "duration_s" in body:
                duration_s = float(body["duration_s"])
        except Exception:
            pass
            
        success = await self.device_manager.start_mic_test(mac, duration_s)
        if success:
            return web.json_response({"status": "recording", "mac": mac, "duration_s": duration_s})
        return web.json_response({"status": "error", "message": "Колонка офлайн или не подключена к WebSocket"}, status=400)

    async def handle_mic_test_audio(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        result = self.device_manager.get_mic_test_result(mac)
        if not result or not result.get("wav"):
            return web.Response(text="Запись теста микрофона отсутствует", status=404)
            
        return web.Response(
            body=result["wav"],
            content_type="audio/wav",
            headers={
                "Content-Disposition": f'inline; filename="mic_test_{mac}.wav"',
                "Cache-Control": "no-cache, no-store, must-revalidate"
            }
        )

    async def handle_mic_test_status(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        is_active = self.device_manager.is_mic_test_active(mac)
        result = self.device_manager.get_mic_test_result(mac)
        stats = result.get("stats") if result else None
        return web.json_response({
            "mac": mac,
            "is_recording": is_active,
            "has_recording": result is not None and bool(result.get("wav")),
            "stats": stats
        })

    async def handle_last_utterance_audio(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        result = self.device_manager.get_last_utterance_result(mac)
        if not result or not result.get("wav"):
            return web.Response(text="Запись последней реплики отсутствует", status=404)
            
        return web.Response(
            body=result["wav"],
            content_type="audio/wav",
            headers={
                "Content-Disposition": f'inline; filename="utterance_{mac}.wav"',
                "Cache-Control": "no-cache, no-store, must-revalidate"
            }
        )

    async def handle_last_utterance_status(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        result = self.device_manager.get_last_utterance_result(mac)
        stats = result.get("stats") if result else None
        return web.json_response({
            "mac": mac,
            "has_recording": result is not None and bool(result.get("wav")),
            "stats": stats
        })

    async def handle_events(self, request):
        """Server-Sent Events (SSE) для обновления дашборда в реальном времени."""
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache, no-transform',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
                'Access-Control-Allow-Origin': '*'
            }
        )
        await response.prepare(request)
        
        q = asyncio.Queue(maxsize=100)
        self.sse_queues.add(q)
        
        try:
            # Отправляем приветственное событие и сразу принудительно сбрасываем буфер
            await response.write(b": keepalive\n\ndata: {\"event\": \"connected\"}\n\n")
            await response.drain()
            while True:
                msg = await q.get()
                await response.write(f"data: {msg}\n\n".encode("utf-8"))
                await response.drain()
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError, aiohttp.ClientConnectionResetError, aiohttp.ClientPayloadError):
            pass
        except Exception as e:
            logger.debug(f"SSE connection closed: {e}")
        finally:
            self.sse_queues.discard(q)
            
        return response

    async def handle_ww_monitor(self, request):
        """Быстрый REST-эндпоинт живой телеметрии вейкворда (гарантированный fallback)."""
        data = {}
        for mac, dev in self.device_manager.devices.items():
            if dev.get("device_type") == "pc_streamer" or (dev.get("config") and dev.get("config").get("wake_word_mode") == "server"):
                data[mac] = {
                    "mac": mac,
                    "name": str(dev.get("name", mac)),
                    "score": float(dev.get("ww_score", 0.0)),
                    "peak": float(dev.get("ww_peak_score", 0.0)),
                    "rms": float(dev.get("ww_rms_dbfs", -60.0)),
                    "threshold": float(dev.get("config", {}).get("ww_threshold", 0.94)),
                    "is_online": bool(dev.get("is_online", False))
                }

        engine_info = {
            "is_ready": False,
            "model_name": None,
            "error": None
        }
        if self.ww_engine_callback:
            try:
                engine = self.ww_engine_callback()
                if engine:
                    engine_info = {
                        "is_ready": bool(engine.is_ready),
                        "model_name": getattr(engine, "model_name", None),
                        "error": getattr(engine, "last_error", None)
                    }
            except Exception:
                pass
        data["__engine__"] = engine_info
        return web.json_response(data)

    async def handle_add_yandex_station(self, request):
        """Ручное добавление или регистрация Яндекс Станции (Quasar Bridge)."""
        import uuid
        try:
            body = await request.json()
            mac = (body.get("mac") or body.get("id") or f"yandex_{uuid.uuid4().hex[:6]}").strip().lower()
            name = body.get("name", "Яндекс Станция").strip()
            ip = body.get("ip", "").strip()
            airplay_player = body.get("airplay_player", "").strip()
            mass_player = (body.get("mass_player") or airplay_player).strip()
            output_mode = body.get("output_mode", "ssh_stream").strip()
            api_key = body.get("api_key", "").strip()
            area_name = body.get("area_name", "").strip() or None

            dev = self.device_manager.register_device(mac, {
                "name": name,
                "ip": ip,
                "device_type": "yandex_station",
                "firmware": "Quasar Bridge",
                "area_name": area_name,
                "config": {
                    "output_mode": output_mode,
                    "mass_player": mass_player,
                    "airplay_player": mass_player,
                    "api_key": api_key,
                    "ssh_port": 2222
                }
            })
            if area_name:
                self.device_manager.set_device_area(mac, area_name)
            return web.json_response({"success": True, "device": dev})
        except Exception as e:
            logger.error(f"Error adding Yandex Station: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=400)

    def _resolve_quasar_target(self, param_mac: str = None, param_ip: str = None) -> Tuple[str, int]:
        """Определяет IP и порт SSH для Яндекс Станции по MAC или явному IP."""
        ip = param_ip or ""
        port = 2222
        if param_mac:
            dev = self.device_manager.get_device(param_mac)
            if dev:
                ip = dev.get("ip") or dev.get("config", {}).get("ip") or ip
                port = int(dev.get("config", {}).get("ssh_port", 2222))
        return ip, port

    async def handle_get_quasar_config(self, request: web.Request) -> web.Response:
        """GET /api/quasar/config?mac=...&ip=..."""
        mac = request.query.get("mac", "")
        ip_query = request.query.get("ip", "")
        ip, port = self._resolve_quasar_target(mac, ip_query)
        if not ip or ip == "---":
            return web.json_response({"success": False, "error": "IP-адрес колонки не найден"}, status=400)

        res = await self.quasar_manager.get_config(ip, port)
        status_code = 200 if res.get("success") else 400
        return web.json_response(res, status=status_code)

    async def handle_save_quasar_config(self, request: web.Request) -> web.Response:
        """POST /api/quasar/config"""
        try:
            body = await request.json()
            mac = body.get("mac", "")
            ip_body = body.get("ip", "")
            ip, port = self._resolve_quasar_target(mac, ip_body)
            if not ip or ip == "---":
                return web.json_response({"success": False, "error": "IP-адрес колонки не найден"}, status=400)

            res = await self.quasar_manager.save_config(ip, port, body)
            status_code = 200 if res.get("success") else 400
            return web.json_response(res, status=status_code)
        except Exception as e:
            logger.error(f"Error saving Quasar config: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_extract_ha_keywords(self, request: web.Request) -> web.Response:
        """GET /api/quasar/extract-ha-keywords"""
        try:
            keywords = await self.ha_api.extract_smart_home_keywords()
            return web.json_response({
                "success": True,
                "keywords": keywords,
                "count": len(keywords)
            })
        except Exception as e:
            logger.error(f"Error extracting HA keywords: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_quasar_test_route(self, request: web.Request) -> web.Response:
        """POST /api/quasar/test-route"""
        try:
            body = await request.json()
            mac = body.get("mac", "")
            ip_body = body.get("ip", "")
            phrase = body.get("phrase", "").strip()
            if not phrase:
                return web.json_response({"success": False, "error": "Фраза не указана"}, status=400)

            ip, port = self._resolve_quasar_target(mac, ip_body)
            if not ip or ip == "---":
                return web.json_response({"success": False, "error": "IP-адрес колонки не найден"}, status=400)

            res = await self.quasar_manager.test_route(ip, port, phrase)
            status_code = 200 if res.get("success") else 400
            return web.json_response(res, status=status_code)
        except Exception as e:
            logger.error(f"Error testing route: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_get_quasar_version(self, request: web.Request) -> web.Response:
        """GET /api/quasar/version?mac=...&ip=..."""
        mac = request.query.get("mac", "")
        ip_query = request.query.get("ip", "")
        ip, port = self._resolve_quasar_target(mac, ip_query)
        if not ip or ip == "---":
            # Если колонка не найдена в сети, отдаем хотя бы инфо о бандле
            bundled = self.quasar_manager.get_bundled_version_info()
            return web.json_response({
                "success": False,
                "error": "IP-адрес колонки не найден",
                "bundled_version": bundled.get("version", "1.2.4-porfiriy.1"),
                "bundled_info": bundled
            })

        res = await self.quasar_manager.get_version_info(ip, port)
        status_code = 200 if res.get("success") else 400
        return web.json_response(res, status=status_code)

    async def handle_update_quasar(self, request: web.Request) -> web.Response:
        """POST /api/quasar/update"""
        try:
            body = await request.json()
            mac = body.get("mac", "")
            ip_body = body.get("ip", "")
            ip, port = self._resolve_quasar_target(mac, ip_body)
            if not ip or ip == "---":
                return web.json_response({"success": False, "error": "IP-адрес колонки не найден"}, status=400)

            opts = self.options_callback() if self.options_callback else {}
            porfiriy_port = int(opts.get("openai_api_port", 8000) or 8000)
            res = await self.quasar_manager.update_station(ip, port, porfiriy_port=porfiriy_port)
            status_code = 200 if res.get("success") else 400
            return web.json_response(res, status=status_code)
        except Exception as e:
            logger.error(f"Error updating quasar: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_quasar_squeezelite(self, request: web.Request) -> web.Response:
        """POST /api/quasar/squeezelite {"mac": "...", "ip": "...", "action": "restart"}"""
        try:
            body = await request.json()
            mac = body.get("mac", "")
            ip_body = body.get("ip", "")
            action = body.get("action", "restart")
            ip, port = self._resolve_quasar_target(mac, ip_body)
            if not ip or ip == "---":
                return web.json_response({"success": False, "error": "IP-адрес колонки не найден"}, status=400)

            res = await self.quasar_manager.control_squeezelite(ip, port, action)
            if mac and res.get("success"):
                self.device_manager.update_device_runtime(mac, {"squeezelite_running": res.get("squeezelite_running", False)})
            status_code = 200 if res.get("success") else 400
            return web.json_response(res, status=status_code)
        except Exception as e:
            logger.error(f"Error controlling squeezelite: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_quasar_airplay(self, request: web.Request) -> web.Response:
        """POST /api/quasar/airplay {"mac": "...", "ip": "...", "action": "restart"}"""
        try:
            body = await request.json()
            mac = body.get("mac", "")
            ip_body = body.get("ip", "")
            action = body.get("action", "restart")
            ip, port = self._resolve_quasar_target(mac, ip_body)
            if not ip or ip == "---":
                return web.json_response({"success": False, "error": "IP-адрес колонки не найден"}, status=400)

            res = await self.quasar_manager.control_station_airplay(ip, port, action)
            if mac and res.get("success"):
                self.device_manager.update_device_runtime(mac, {"airplay_running": res.get("airplay_running", False)})
            status_code = 200 if res.get("success") else 400
            return web.json_response(res, status=status_code)
        except Exception as e:
            logger.error(f"Error controlling station airplay: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_quasar_ducking(self, request: web.Request) -> web.Response:
        """POST /api/quasar/ducking {"mac": "...", "ip": "...", "action": "restart"}"""
        try:
            body = await request.json()
            mac = body.get("mac", "")
            ip_body = body.get("ip", "")
            action = body.get("action", "restart")
            ip, port = self._resolve_quasar_target(mac, ip_body)
            if not ip or ip == "---":
                return web.json_response({"success": False, "error": "IP-адрес колонки не найден"}, status=400)

            res = await self.quasar_manager.control_station_ducking(ip, port, action)
            if mac and res.get("success"):
                self.device_manager.update_device_runtime(mac, {"ducking_running": res.get("ducking_running", False)})
            status_code = 200 if res.get("success") else 400
            return web.json_response(res, status=status_code)
        except Exception as e:
            logger.error(f"Error controlling station ducking: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_station_volume_sync(self, request: web.Request) -> web.Response:
        """
        Синхронизирует изменение громкости со Станции (от Алисы или кольца) в Music Assistant / Home Assistant.
        POST /api/station/volume_sync {"volume": 40, "player_name": "Яндекс Станция"}
        """
        try:
            body = await request.json()
            vol = body.get("volume")
            if vol is None:
                return web.json_response({"error": "volume is required"}, status=400)

            try:
                vol_int = int(vol)
            except (ValueError, TypeError):
                return web.json_response({"error": "invalid volume integer"}, status=400)

            vol_float = max(0.0, min(1.0, vol_int / 100.0))
            client_ip = (request.remote or "").strip()
            if client_ip.startswith("::ffff:"):
                client_ip = client_ip[7:]
            player_name = str(body.get("player_name") or "").strip()
            logger.info(f"[VOLUME SYNC] Received volume sync from {client_ip} (player_name='{player_name}'): {vol_int}% ({vol_float})")

            if not self.ha_api:
                return web.json_response({"success": False, "error": "ha_api not initialized"}, status=503)

            # 1. Ищем устройство по точному IP среди зарегистрированных Станций
            target_players: List[str] = []
            all_devices = self.device_manager.get_devices() if self.device_manager else []
            matching_dev = None

            for dev in all_devices:
                dev_ip = (dev.get("ip") or dev.get("config", {}).get("ip") or "").strip()
                if dev_ip and dev_ip == client_ip:
                    matching_dev = dev
                    break

            if matching_dev:
                cfg = matching_dev.get("config", {})
                configured_player = (cfg.get("mass_player") or cfg.get("airplay_player") or "").strip()
                if configured_player:
                    target_players.append(configured_player)

            # 2. Проверяем активные играющие плееры в HA (и точное совпадение по player_name, если передано)
            playing = await self.ha_api.get_playing_media_players()
            if player_name:
                pn_lower = player_name.lower()
                for p in playing:
                    eid = p.get("entity_id", "")
                    fname = str(p.get("attributes", {}).get("friendly_name", "")).strip().lower()
                    if fname == pn_lower or pn_lower in fname:
                        if eid and eid not in target_players:
                            target_players.append(eid)

            if not target_players and player_name:
                pn_lower = player_name.lower()
                all_players = await self.ha_api.get_media_players()
                for p in all_players:
                    eid = p.get("entity_id", "")
                    fname = str(p.get("name", "")).strip().lower()
                    if fname == pn_lower or pn_lower in fname:
                        if eid and eid not in target_players:
                            target_players.append(eid)

            if not target_players:
                for p in playing:
                    eid = p.get("entity_id", "")
                    if any(k in eid.lower() for k in ["squeezelite", "mass", "airplay", "yandex", "station"]):
                        target_players.append(eid)
                        break
                if not target_players and playing:
                    target_players.append(playing[0].get("entity_id"))

            # 3. Фолбэк на глобальный плеер по умолчанию
            if not target_players:
                global_opts = self.options_callback() if self.options_callback else {}
                default_p = (global_opts.get("default_airplay_player") or "").strip()
                if default_p:
                    target_players.append(default_p)

            target_players = [p for p in dict.fromkeys(target_players) if p]
            if not target_players:
                logger.warning(f"[VOLUME SYNC] No suitable media_player found to sync volume {vol_int}% for {client_ip}")
                return web.json_response({"success": False, "error": "No media player found"}, status=404)

            synced_players = []
            errors = []
            for tp in target_players:
                logger.info(f"[VOLUME SYNC] Updating Home Assistant player {tp} volume to {vol_float} ({vol_int}%)")
                res = await self.ha_api.call_service("media_player", "volume_set", {
                    "entity_id": tp,
                    "volume_level": vol_float
                })
                if isinstance(res, dict) and "error" in res:
                    logger.error(f"[VOLUME SYNC] Failed updating {tp}: {res['error']}")
                    errors.append({"player": tp, "error": res["error"]})
                else:
                    synced_players.append(tp)

            if not synced_players:
                return web.json_response({"success": False, "errors": errors}, status=502)

            return web.json_response({
                "success": True,
                "player": synced_players[0],
                "players": synced_players,
                "volume": vol_int,
                "volume_level": vol_float,
                "errors": errors or None
            })
        except Exception as e:
            logger.error(f"[VOLUME SYNC] Error processing volume sync: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def start(self):
        # Отключаем спам в системный журнал HA от периодического поллинга /api/ww_monitor
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"Porfiriy Ingress Web Server running on http://0.0.0.0:{self.port}")

        # Запуск отдельного слушателя на внешнем порту для OpenAI API (по умолчанию 8000)
        opts = self.options_callback() if self.options_callback else {}
        openai_port = int(opts.get("openai_api_port", 8000))
        if openai_port and openai_port != self.port:
            try:
                site_openai = web.TCPSite(runner, "0.0.0.0", openai_port)
                await site_openai.start()
                logger.info(f"Porfiriy OpenAI API Gateway running on http://0.0.0.0:{openai_port}")
            except Exception as e:
                logger.error(f"Failed to start OpenAI API site on port {openai_port}: {e}")

        # Запуск SSDP сервера обнаружения DLNA медиаплееров
        asyncio.create_task(self.dlna_manager.start_ssdp())

    # ------------------------------------------------------------------ #
    # Виртуальные стримеры (pc_streamer)
    # ------------------------------------------------------------------ #

    async def handle_virtual_response_wav(self, request):
        """Отдаёт последний WAV-ответ Gemini для данного pc_streamer.
        Вызывается Home Assistant media_player.play_media."""
        mac = request.match_info.get("mac", "").strip().lower()
        wav = self.device_manager.get_response_wav(mac)
        if not wav:
            return web.Response(status=404, text=f"No response WAV for {mac}")
        return web.Response(
            body=wav,
            content_type="audio/wav",
            headers={
                "Content-Disposition": f'inline; filename="response_{mac}.wav"',
                "Cache-Control": "no-cache, no-store, must-revalidate"
            }
        )

    async def handle_upload_ww_model(self, request):
        """Загрузка нового .onnx файла вейкворда на сервер."""
        try:
            reader = await request.multipart()
            field = await reader.next()
            if not field or field.name != "model":
                return web.json_response({"success": False, "error": "Field 'model' not found"}, status=400)
            filename = field.filename or "porfiriy.onnx"
            if not filename.endswith(".onnx"):
                return web.json_response({"success": False, "error": "Only .onnx files allowed"}, status=400)

            # Сохраняем в /data/ (постоянное хранилище аддона)
            save_dir = "/data"
            if not os.path.exists(save_dir):
                save_dir = os.path.dirname(__file__)  # fallback для локальной разработки
            save_path = os.path.join(save_dir, filename)

            with open(save_path, "wb") as f:
                while True:
                    chunk = await field.read_chunk(8192)
                    if not chunk:
                        break
                    f.write(chunk)

            size = os.path.getsize(save_path)
            logger.info(f"[WW] Uploaded new model: '{save_path}' ({size} bytes)")

            # Перезагружаем движок с новой моделью
            if self.ww_engine_callback:
                engine = self.ww_engine_callback()
                if engine:
                    engine._model = None  # принудительный сброс
                    engine.model_path = save_path
                    engine._load_model()

            return web.json_response({"success": True, "path": save_path, "size": size})
        except Exception as e:
            logger.error(f"[WW] Upload model error: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_streamer_config(self, request):
        """Обновляет настройки pc_streamer: response_player, ww_threshold, area_name."""
        mac = request.match_info.get("mac", "").strip().lower()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

        allowed = {"response_player", "ww_threshold", "area_name", "speaker_volume", "audio_output_mode", "wake_word_mode"}
        cfg = {k: v for k, v in data.items() if k in allowed}

        if "response_player" in cfg and "audio_output_mode" not in cfg:
            # Если выбран плеер, переключаем вывод на внешний плеер HA, если сброшен («») — возвращаем stream (со звуком в динамики ПК)
            cfg["audio_output_mode"] = "external_player" if bool(cfg["response_player"]) else "stream"

        if "ww_threshold" in cfg:
            cfg["ww_threshold"] = float(cfg["ww_threshold"])
            # Обновляем порог в движке на лету
            if self.ww_engine_callback:
                engine = self.ww_engine_callback()
                if engine:
                    engine.set_threshold(cfg["ww_threshold"])

        success = await self.device_manager.update_device_config(mac, cfg)
        return web.json_response({"success": success})

