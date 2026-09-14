import os
import json
import asyncio
import logging
from aiohttp import web
from typing import Optional, Dict, Any

logger = logging.getLogger("web_server")

class WebServer:
    def __init__(self, device_manager, ha_api, options_callback, port: int = 8099):
        self.device_manager = device_manager
        self.ha_api = ha_api
        self.options_callback = options_callback
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
        self.app.router.add_get("/api/events", self.handle_events)
        
        if os.path.exists(self.static_dir):
            self.app.router.add_static("/static/", self.static_dir)

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
        if os.path.exists(index_file):
            return web.FileResponse(index_file)
        return web.Response(text="Porfiriy Web UI loading...", content_type="text/html")

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

    async def handle_get_areas(self, request):
        if self.ha_api:
            areas = await self.ha_api.get_areas()
            return web.json_response({"areas": areas})
        return web.json_response({"areas": []})

    async def handle_get_global(self, request):
        opts = self.options_callback() if self.options_callback else {}
        # Скрываем api key в выводе
        safe_opts = dict(opts)
        if "gemini_api_key" in safe_opts and safe_opts["gemini_api_key"]:
            safe_opts["gemini_api_key"] = safe_opts["gemini_api_key"][:4] + "..." + safe_opts["gemini_api_key"][-4:]
        return web.json_response({"options": safe_opts})

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
        
        q = asyncio.Queue()
        self.sse_queues.add(q)
        
        try:
            # Отправляем приветственное событие
            await response.write(b"data: {\"event\": \"connected\"}\n\n")
            while True:
                msg = await q.get()
                await response.write(f"data: {msg}\n\n".encode("utf-8"))
        except asyncio.CancelledError:
            pass
        finally:
            self.sse_queues.discard(q)
            
        return response

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"Porfiriy Ingress Web Server running on http://0.0.0.0:{self.port}")
