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

class WebServer:
    def __init__(self, device_manager, ha_api, options_callback, options_save_callback: Optional[Callable[[Dict[str, Any]], None]] = None, phrase_manager=None, models_callback: Optional[Callable[[], List[Dict[str, Any]]]] = None, refresh_models_callback=None, port: int = 8099):
        self.device_manager = device_manager
        self.ha_api = ha_api
        self.options_callback = options_callback
        self.options_save_callback = options_save_callback
        self.phrase_manager = phrase_manager
        self.models_callback = models_callback
        self.refresh_models_callback = refresh_models_callback
        self.port = port
        self.app = web.Application()
        self.sse_queues = set()
        self.static_dir = os.path.join(os.path.dirname(__file__), "static")
        
        # Подписываемся на события device_manager для SSE
        self.device_manager.add_subscriber(self._on_device_event)
        
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/devices", self.handle_get_devices)
        self.app.router.add_post("/api/devices/{mac}/config", self.handle_device_config)
        self.app.router.add_post("/api/devices/bulk_config", self.handle_bulk_config)
        self.app.router.add_post("/api/devices/{mac}/beep", self.handle_device_beep)
        self.app.router.add_post("/api/devices/{mac}/reboot", self.handle_device_reboot)
        self.app.router.add_get("/api/areas", self.handle_get_areas)
        self.app.router.add_get("/api/global", self.handle_get_global)
        self.app.router.add_post("/api/global", self.handle_set_global)
        self.app.router.add_post("/api/models/refresh", self.handle_refresh_models)
        self.app.router.add_post("/api/phrases/regenerate", self.handle_regenerate_phrases)
        self.app.router.add_get("/api/phrases/status", self.handle_phrases_status)
        self.app.router.add_get("/api/firmware/info", self.handle_firmware_info)
        self.app.router.add_post("/api/devices/{mac}/ota", self.handle_device_ota)
        self.app.router.add_post("/api/devices/bulk_ota", self.handle_bulk_ota)
        self.app.router.add_get("/api/events", self.handle_events)
        self.app.router.add_get("/static/{filename:.*}", self.handle_static)

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
        if os.path.exists(index_file):
            with open(index_file, "r", encoding="utf-8") as f:
                content = f.read()
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

    def _safe_options(self, opts: Dict[str, Any]) -> Dict[str, Any]:
        safe = dict(opts)
        key = safe.get("gemini_api_key", "")
        safe["has_api_key"] = bool(key)
        if key:
            safe["gemini_api_key"] = key[:4] + "..." + key[-4:] if len(key) >= 8 else "********"
        return safe

    async def handle_get_global(self, request):
        opts = self.options_callback() if self.options_callback else {}
        models = self.models_callback() if self.models_callback else []
        return web.json_response({
            "options": self._safe_options(opts),
            "models": models
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
                "enable_barge_in", "prompt_persona", "prompt_users", "prompt_smart_home",
                "prompt_general"
            ]
            
            for field in allowed_fields:
                if field in data:
                    val = data[field]
                    if field == "gemini_api_key":
                        # Если передан пустой или маскированный ключ — оставляем прежний
                        if val and "..." not in str(val) and "*" not in str(val):
                            updated_opts[field] = str(val).strip()
                    elif field in ["thinking_timeout_s", "vad_silence_duration_ms"]:
                        try:
                            updated_opts[field] = int(val)
                        except (ValueError, TypeError):
                            pass
                    elif field == "temperature":
                        try:
                            updated_opts[field] = float(val)
                        except (ValueError, TypeError):
                            pass
                    elif field in ["enable_google_search", "enable_barge_in"]:
                        updated_opts[field] = bool(val)
                    else:
                        updated_opts[field] = str(val)

            # 1. Мгновенно сохраняем в памяти бэкенда (Hot Reload) и локальный файл
            if self.options_save_callback:
                self.options_save_callback(updated_opts)
            
            # 2. Синхронизируем с Home Assistant Supervisor API (чтобы в UI HA настройки обновились)
            supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
            if supervisor_token:
                try:
                    ha_allowed = {
                        "gemini_api_key", "gemini_model", "system_prompt",
                        "prompt_persona", "prompt_users", "prompt_smart_home", "prompt_general",
                        "voice_name", "temperature", "thinking_timeout_s", "enable_google_search",
                        "vad_silence_duration_ms", "enable_barge_in"
                    }
                    ha_opts = {k: v for k, v in updated_opts.items() if k in ha_allowed}
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

    async def handle_events(self, request):
        """Server-Sent Events (SSE) для обновления дашборда в реальном времени."""
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'Access-Control-Allow-Origin': '*'
            }
        )
        await response.prepare(request)
        
        q = asyncio.Queue(maxsize=50)
        self.sse_queues.add(q)
        
        try:
            # Отправляем приветственное событие
            await response.write(b"data: {\"event\": \"connected\"}\n\n")
            while True:
                msg = await q.get()
                await response.write(f"data: {msg}\n\n".encode("utf-8"))
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError, aiohttp.ClientConnectionResetError, aiohttp.ClientPayloadError):
            pass
        except Exception as e:
            logger.debug(f"SSE connection closed: {e}")
        finally:
            self.sse_queues.discard(q)
            
        return response

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"Porfiriy Ingress Web Server running on http://0.0.0.0:{self.port}")
