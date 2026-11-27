from __future__ import annotations
import os
import re
import json
import asyncio
import logging
import aiohttp
from aiohttp import web
from typing import Optional, Dict, Any, Callable, List, Tuple

from audio_utils import get_earcon_pcm, get_earcon_wav, list_earcon_presets
from config import CANONICAL_PROMPTS, get_addon_version

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

try:
    from .station_ducking import StationDuckingManager
except ImportError:
    from station_ducking import StationDuckingManager

try:
    from .quiet_mode import quiet_mode_singleton
except ImportError:
    from quiet_mode import quiet_mode_singleton

try:
    from .db import db_singleton
    from .big_models import big_model_pool_singleton
    from .serper_search import serper_search_singleton
    from .mcp_client import mcp_http_client_singleton
    from .scheduler import scheduler_singleton
    from .ww_capture import ww_capture_store
    from .plugins.telegram_bot import TelegramBotPlugin
    from .plugins.telegram_userbot import TelegramUserbotPlugin
    from .plugins.mail_imap import MailImapPlugin
    from .plugins.keenetic import KeeneticPlugin
    from .plugins.kinowatch import KinoWatchPlugin
    from .plugins.yandex_maps import YandexMapsPlugin
except ImportError:
    from db import db_singleton
    from big_models import big_model_pool_singleton
    from serper_search import serper_search_singleton
    from mcp_client import mcp_http_client_singleton
    from scheduler import scheduler_singleton
    from ww_capture import ww_capture_store
    from plugins.telegram_bot import TelegramBotPlugin
    from plugins.telegram_userbot import TelegramUserbotPlugin
    from plugins.mail_imap import MailImapPlugin
    from plugins.keenetic import KeeneticPlugin
    from plugins.kinowatch import KinoWatchPlugin
    from plugins.yandex_maps import YandexMapsPlugin

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

        # Серверный авто-дакинг для Яндекс Станции через Home Assistant
        self.ducking_manager = StationDuckingManager(self.device_manager, self.ha_api)

        # Менеджер управления конфигурацией и маршрутизацией Quasar Bridge
        self.quasar_manager = QuasarManager()

        # Интеграция OpenAI-совместимого эндпоинта (Quasar Bridge / Яндекс Станция)
        self.openai_service = OpenAIService(
            self.device_manager,
            self.ha_api,
            self.options_callback,
            ducking_manager=self.ducking_manager,
            quasar_manager=self.quasar_manager
        )
        self.openai_service.register_routes(self.app)

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
        self.app.router.add_post("/api/station/alice_state", self.handle_station_alice_state)
        self.app.router.add_post("/api/station/airplay_state", self.handle_station_airplay_state)
        self.app.router.add_get("/api/quasar/extract-ha-keywords", self.handle_extract_ha_keywords)
        self.app.router.add_post("/api/quasar/test-route", self.handle_quasar_test_route)
        self.app.router.add_delete("/api/devices/{mac}", self.handle_delete_device)
        self.app.router.add_post("/api/devices/{mac}/config", self.handle_device_config)
        self.app.router.add_post("/api/devices/bulk_config", self.handle_bulk_config)
        self.app.router.add_post("/api/devices/{mac}/beep", self.handle_device_beep)
        self.app.router.add_post("/api/devices/{mac}/reboot", self.handle_device_reboot)
        self.app.router.add_post("/api/devices/{mac}/wake", self.handle_device_wake)
        self.app.router.add_post("/api/devices/{mac}/stop", self.handle_device_stop)
        self.app.router.add_get("/api/areas", self.handle_get_areas)
        self.app.router.add_get("/api/media_players", self.handle_get_media_players)
        self.app.router.add_get("/api/global", self.handle_get_global)
        self.app.router.add_post("/api/global", self.handle_set_global)
        self.app.router.add_post("/api/models/refresh", self.handle_refresh_models)
        self.app.router.add_post("/api/phrases/regenerate", self.handle_regenerate_phrases)
        self.app.router.add_get("/api/phrases/status", self.handle_phrases_status)
        self.app.router.add_get("/api/firmware/info", self.handle_firmware_info)
        self.app.router.add_get("/api/watch/apk/info", self.handle_watch_apk_info)
        self.app.router.add_get("/api/watch/apk/download", self.handle_watch_apk_download)
        self.app.router.add_post("/api/devices/{mac}/ota", self.handle_device_ota)
        self.app.router.add_post("/api/devices/bulk_ota", self.handle_bulk_ota)
        self.app.router.add_post("/api/devices/{mac}/mic_test/start", self.handle_mic_test_start)
        self.app.router.add_get("/api/devices/{mac}/mic_test/audio", self.handle_mic_test_audio)
        self.app.router.add_get("/api/devices/{mac}/mic_test/status", self.handle_mic_test_status)
        self.app.router.add_get("/api/devices/{mac}/last_utterance/audio", self.handle_last_utterance_audio)
        self.app.router.add_get("/api/devices/{mac}/last_utterance/status", self.handle_last_utterance_status)
        self.app.router.add_get("/api/events", self.handle_events)
        self.app.router.add_get("/api/ww_monitor", self.handle_ww_monitor)
        self.app.router.add_get("/api/ww_captures", self.handle_get_ww_captures)
        self.app.router.add_get("/api/ww_captures/archive", self.handle_download_ww_captures_archive)
        self.app.router.add_get("/api/ww_captures/clip/{filename}", self.handle_get_ww_capture_clip)
        self.app.router.add_delete("/api/ww_captures/clip/{filename}", self.handle_delete_ww_capture_clip)
        self.app.router.add_delete("/api/ww_captures", self.handle_clear_ww_captures)
        self.app.router.add_get("/static/{filename:.*}", self.handle_static)
        # Виртуальные стримеры (pc_streamer)
        self.app.router.add_get("/api/virtual/{mac}/response.wav", self.handle_virtual_response_wav)
        self.app.router.add_post("/api/virtual/upload_model", self.handle_upload_ww_model)
        self.app.router.add_patch("/api/devices/{mac}/streamer_config", self.handle_streamer_config)
        # Библиотека звуков (Earcons) и нативный режим Яндекс Станции (openWakeWord + LED)
        self.app.router.add_get("/api/earcons/catalog", self.handle_earcons_catalog)
        self.app.router.add_get("/api/earcons/preview/{category}/{preset}", self.handle_earcon_preview)
        self.app.router.add_post("/api/devices/{mac}/play_earcon", self.handle_device_play_earcon)
        self.app.router.add_get("/api/station/{mac}/native_status", self.handle_station_native_status)
        self.app.router.add_post("/api/station/{mac}/toggle_alice", self.handle_station_toggle_alice)
        self.app.router.add_post("/api/station/{mac}/stationd", self.handle_station_control_stationd)
        self.app.router.add_post("/api/station/{mac}/upload_model", self.handle_station_upload_model)
        self.app.router.add_post("/api/station/{mac}/dump_mic", self.handle_station_dump_mic)
        self.app.router.add_get("/api/station/{mac}/wifi", self.handle_station_wifi_get)
        self.app.router.add_post("/api/station/{mac}/wifi", self.handle_station_wifi_save)
        self.app.router.add_delete("/api/station/{mac}/wifi", self.handle_station_wifi_delete)
        # Пользователи, семейные каналы, плагины/MCP и аудит вызовов (Porfiriy 2.0)
        self.app.router.add_get("/api/users", self.handle_get_users)
        self.app.router.add_post("/api/users", self.handle_save_user)
        self.app.router.add_delete("/api/users/{user_id}", self.handle_delete_user)
        self.app.router.add_post("/api/family/channels", self.handle_save_family_channels)
        self.app.router.add_get("/api/providers", self.handle_get_providers)
        self.app.router.add_post("/api/providers", self.handle_save_provider)
        self.app.router.add_post("/api/providers/{provider_id}/toggle", self.handle_toggle_provider)
        self.app.router.add_post("/api/providers/{provider_id}/sync_tools", self.handle_sync_provider_tools)
        self.app.router.add_post("/api/providers/builtin.telegram_bot/test", self.handle_test_telegram_bot)
        self.app.router.add_post("/api/providers/builtin.telegram_userbot/accounts/{account_id}/auth/start", self.handle_tg_userbot_auth_start)
        self.app.router.add_post("/api/providers/builtin.telegram_userbot/accounts/{account_id}/auth/verify", self.handle_tg_userbot_auth_verify)
        self.app.router.add_post("/api/providers/builtin.mail_imap/accounts/{account_id}/test", self.handle_mail_imap_test)
        self.app.router.add_post("/api/providers/builtin.keenetic/test", self.handle_test_keenetic)
        self.app.router.add_post("/api/providers/builtin.kinowatch/auth/start", self.handle_kinowatch_auth_start)
        self.app.router.add_post("/api/providers/builtin.kinowatch/auth/poll", self.handle_kinowatch_auth_poll)
        self.app.router.add_post("/api/providers/builtin.kinowatch/folders", self.handle_kinowatch_folders)
        self.app.router.add_post("/api/providers/builtin.yandex_maps/test", self.handle_test_yandex_maps)
        self.app.router.add_post("/api/providers/{provider_id}/accounts", self.handle_save_provider_account)
        self.app.router.add_delete("/api/providers/accounts/{account_id}", self.handle_delete_provider_account)
        self.app.router.add_delete("/api/providers/{provider_id}", self.handle_delete_provider)
        self.app.router.add_get("/api/tools/audit", self.handle_get_tools_audit)
        self.app.router.add_post("/api/big_models/test", self.handle_test_big_model)
        self.app.router.add_post("/api/web_search/test", self.handle_test_web_search)
        self.app.router.add_get("/api/memory", self.handle_get_memory)
        self.app.router.add_post("/api/memory", self.handle_add_memory)
        self.app.router.add_post("/api/memory/cleanup", self.handle_cleanup_memory)
        self.app.router.add_patch("/api/memory/{item_id}", self.handle_patch_memory)
        self.app.router.add_delete("/api/memory/{item_id}", self.handle_delete_memory)
        # Персистентный планировщик задач и радаров
        self.app.router.add_get("/api/schedules", self.handle_get_schedules)
        self.app.router.add_post("/api/schedules", self.handle_create_schedule)
        self.app.router.add_patch("/api/schedules/{job_id}", self.handle_patch_schedule)
        self.app.router.add_delete("/api/schedules/{job_id}", self.handle_delete_schedule)
        self.app.router.add_post("/api/schedules/{job_id}/toggle", self.handle_toggle_schedule)
        self.app.router.add_post("/api/schedules/{job_id}/run", self.handle_run_schedule)
        self.app.router.add_get("/api/schedules/{job_id}/runs", self.handle_get_schedule_runs)
        # Умный ночной тихий режим
        self.app.router.add_get("/api/quiet_mode/candidates", self.handle_get_quiet_candidates)
        self.app.router.add_get("/api/quiet_mode/status", self.handle_get_quiet_status)

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
        opts = self.options_callback() if self.options_callback else {}
        states_by_id = {}
        if self.ha_api and any(d.get("device_type") == "yandex_station" for d in devices):
            try:
                all_states = await asyncio.wait_for(self.ha_api.get_states(), timeout=2.0)
                states_by_id = {s.get("entity_id"): s for s in all_states if s.get("entity_id")}
            except Exception:
                pass
        
        # Обновляем имя комнат, версию прошивки и текущую громкость плеера для Станций
        bundled_quasar_info = self.quasar_manager.get_bundled_version_info()
        bundled_quasar_ver = bundled_quasar_info.get("version", "")

        for d in devices:
            if not d.get("area_name") and self.ha_api:
                area_name = await self.ha_api.get_device_area_name(d["mac"])
                if area_name:
                    d["area_name"] = area_name
                    self.device_manager.set_device_area(d["mac"], area_name)
            if d.get("device_type") == "yandex_station":
                d["target_firmware"] = bundled_quasar_ver
                cfg = d.setdefault("config", {})
                installed_ver = d.get("firmware") or cfg.get("quasar_version") or ""
                d["firmware"] = installed_ver
                d["has_update"] = bool(not installed_ver or installed_ver != bundled_quasar_ver)

                saved_vol = self.ducking_manager.get_saved_volume(d["mac"])
                if saved_vol is not None:
                    cfg["speaker_volume"] = saved_vol
                else:
                    player = StationDuckingManager.resolve_station_player(d, opts)
                    st = states_by_id.get(player)
                    vol_lvl = st.get("attributes", {}).get("volume_level") if isinstance(st, dict) else None
                    if vol_lvl is not None:
                        cfg["speaker_volume"] = round(float(vol_lvl), 2)
                    
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
            if "speaker_volume" in data:
                dev = self.device_manager.get_device(mac)
                if dev and dev.get("device_type") == "yandex_station":
                    vol_level = max(0.0, min(1.0, round(float(data["speaker_volume"]), 2)))
                    data["speaker_volume"] = vol_level
                    opts = self.options_callback() if self.options_callback else {}
                    player = StationDuckingManager.resolve_station_player(dev, opts)
                    if player and self.ha_api:
                        self.ducking_manager.set_explicit_volume(mac, vol_level)
                        logger.info(f"[CARD VOLUME] Setting {player} volume to {vol_level} from station card {mac}")
                        await self.ha_api.call_service("media_player", "volume_set", {
                            "entity_id": player,
                            "volume_level": vol_level
                        })
                    if self.quasar_manager:
                        target_pct = int(round(vol_level * 100))
                        self.quasar_manager.set_station_hw_volume(dev, target_pct)
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

    async def handle_device_stop(self, request):
        mac = request.match_info["mac"]
        success = await self.device_manager.send_command(mac, {"type": "stop"})
        self.device_manager.set_device_state(mac, "idle")
        return web.json_response({"success": success})

    async def handle_firmware_info(self, request):
        info = self.device_manager.get_firmware_info()
        return web.json_response(info)

    async def handle_watch_apk_info(self, request):
        info = self.device_manager.get_watch_apk_info()
        safe = {k: v for k, v in info.items() if k != "path"}
        return web.json_response(safe)

    async def handle_watch_apk_download(self, request):
        path = self.device_manager.get_watch_apk_path()
        if not path or not os.path.exists(path):
            return web.json_response({"error": "Watch APK not found on server"}, status=404)
        return web.FileResponse(
            path=path,
            headers={
                "Content-Type": "application/vnd.android.package-archive",
                "Content-Disposition": 'attachment; filename="porfiriy-watch.apk"',
                "Cache-Control": "no-cache",
            },
        )

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

        serper_key = safe.get("serper_api_key", "")
        safe["has_serper_api_key"] = bool(serper_key)
        if serper_key:
            safe["serper_api_key"] = serper_key[:4] + "..." + serper_key[-4:] if len(serper_key) >= 8 else "********"
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
            "big_models_health": big_model_pool_singleton.get_health_map(),
            "media_players": media_players,
            "addon_version": get_addon_version(),
            "bundled_quasar_version": self.quasar_manager.get_bundled_version_info().get("version", ""),
            "canonical_prompts": CANONICAL_PROMPTS,
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
                "gemini_api_key", "gemini_model", "gemini_fallback_model", "normalize_numbers_enabled",
                "ww_server_enabled", "big_models_enabled", "big_models_threshold_chars", "big_models_pool",
                "web_search_model", "serper_api_key",
                "voice_name", "temperature",
                "thinking_timeout_s", "enable_google_search", "vad_silence_duration_ms",
                "enable_barge_in", "barge_in_threshold_rms", "prompt_persona", "prompt_users",
                "prompt_smart_home", "prompt_general",
                "ducking_mode", "enable_media_ducking", "ducking_volume_factor", "default_media_player",
                "ma_api_key", "regenerate_phrases",
                "ww_server_threshold",
                "openai_api_enabled", "openai_api_port", "openai_api_key", "default_airplay_player",
                "earcon_volume_enabled", "earcon_volume_preset",
                "earcon_smarthome_enabled", "earcon_smarthome_preset", "earcon_wake_preset",
                "quiet_mode_enabled", "quiet_mode_start", "quiet_mode_end",
                "quiet_mode_entities", "quiet_mode_ww_threshold",
                "quiet_mode_mute_wake_earcon", "quiet_mode_volume",
                "quiet_mode_silence_dbfs", "quiet_mode_force",
            ]
            
            for field in allowed_fields:
                if field in data:
                    val = data[field]
                    if field == "gemini_api_key":
                        # Если передан пустой или маскированный ключ — оставляем прежний
                        if val and "..." not in str(val) and "*" not in str(val):
                            updated_opts[field] = str(val).strip()
                    elif field in ("ma_api_key", "serper_api_key"):
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
                    elif field in ["enable_google_search", "enable_barge_in", "regenerate_phrases", "openai_api_enabled", "big_models_enabled", "earcon_volume_enabled", "earcon_smarthome_enabled", "quiet_mode_enabled", "quiet_mode_mute_wake_earcon", "normalize_numbers_enabled", "ww_server_enabled"]:
                        updated_opts[field] = bool(val)
                    elif field == "quiet_mode_entities":
                        if isinstance(val, list):
                            updated_opts[field] = [str(x).strip() for x in val if str(x).strip()]
                    elif field in ["quiet_mode_ww_threshold", "quiet_mode_volume", "quiet_mode_silence_dbfs"]:
                        try:
                            fval = float(val)
                            if field == "quiet_mode_ww_threshold":
                                fval = max(0.05, min(0.98, fval))
                            elif field == "quiet_mode_volume":
                                fval = max(0.0, min(1.0, fval))
                            else:
                                fval = max(-70.0, min(-15.0, fval))
                            updated_opts[field] = fval
                        except (ValueError, TypeError):
                            pass
                    elif field == "quiet_mode_force":
                        if str(val).strip().lower() in ("auto", "on", "off"):
                            updated_opts[field] = str(val).strip().lower()
                    elif field == "big_models_threshold_chars":
                        try:
                            updated_opts[field] = max(500, min(100000, int(val)))
                        except (ValueError, TypeError):
                            pass
                    elif field == "big_models_pool":
                        if isinstance(val, list):
                            cleaned_pool = []
                            for idx, item in enumerate(val):
                                if isinstance(item, dict) and str(item.get("model") or "").strip():
                                    cleaned_pool.append({
                                        "model": str(item["model"]).strip(),
                                        "priority": int(item.get("priority", idx + 1)),
                                        "enabled": bool(item.get("enabled", True)),
                                    })
                            updated_opts[field] = cleaned_pool
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
            asyncio.create_task(quiet_mode_singleton.on_config_changed())
            
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

    def _sync_quasar_device_metadata(
        self,
        mac: str,
        *,
        ip: Optional[str] = None,
        installed_version: str = "",
        target_version: str = "",
        has_update: Optional[bool] = None,
        res: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Обновляет метаданные карточки Яндекс Станции и уведомляет SSE-клиентов."""
        if not mac:
            return
        dev = self.device_manager.get_device(mac)
        if not dev:
            return
        if ip and dev.get("ip") != ip:
            dev["ip"] = ip
        if installed_version:
            dev["firmware"] = installed_version
            dev.setdefault("config", {})["quasar_version"] = installed_version
        if target_version:
            dev["target_firmware"] = target_version
        if has_update is not None:
            dev["has_update"] = has_update
        if res:
            if "squeezelite_running" in res:
                dev["squeezelite_running"] = res["squeezelite_running"]
            if "airplay_running" in res:
                dev["airplay_running"] = res["airplay_running"]
        self.device_manager.notify_device_updated("registered", dev)

    async def handle_get_quasar_version(self, request: web.Request) -> web.Response:
        """GET /api/quasar/version?mac=...&ip=..."""
        mac = request.query.get("mac", "")
        ip_query = request.query.get("ip", "")
        ip, port = self._resolve_quasar_target(mac, ip_query)
        if not ip or ip == "---":
            bundled = self.quasar_manager.get_bundled_version_info()
            return web.json_response({
                "success": False,
                "error": "IP-адрес колонки не найден",
                "bundled_version": bundled.get("version", ""),
                "bundled_info": bundled
            })

        res = await self.quasar_manager.get_version_info(ip, port)
        if res.get("success") and mac:
            self._sync_quasar_device_metadata(
                mac,
                installed_version=res.get("installed_version", ""),
                target_version=res.get("bundled_version", ""),
                has_update=res.get("has_update", False),
                res=res,
            )
        status_code = 200 if res.get("success") else 400
        return web.json_response(res, status=status_code)

    async def handle_update_quasar(self, request: web.Request) -> web.Response:
        """POST /api/quasar/update"""
        try:
            body = await request.json()
            mac = body.get("mac", "")
            ip_body = body.get("ip", "")
            server_ip = body.get("server_ip") or None
            opts = self.options_callback() if self.options_callback else {}
            openai_port = int(opts.get("openai_api_port", 8000))

            ip, port = self._resolve_quasar_target(mac, ip_body)
            if not ip or ip == "---":
                return web.json_response({"success": False, "error": "IP-адрес колонки не найден"}, status=400)

            res = await self.quasar_manager.update_station(ip, port, server_ip=server_ip, openai_port=openai_port)
            if mac and res.get("success"):
                new_ver = res.get("version", "")
                self._sync_quasar_device_metadata(
                    mac,
                    ip=ip,
                    installed_version=new_ver,
                    target_version=new_ver,
                    has_update=False if new_ver else None,
                    res=res,
                )

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

    async def handle_station_alice_state(self, request: web.Request) -> web.Response:
        """
        Принимает событие состояния Алисы со Станции (listening / idle) для серверного авто-дакинга через HA.
        POST /api/station/alice_state {"state": "listening"}
        """
        try:
            body = await request.json()
            state = body.get("state", "")
            opts = self.options_callback() if self.options_callback else {}
            res = await self.ducking_manager.on_alice_state(request.remote or "", state, opts)
            return web.json_response(res)
        except Exception as e:
            logger.error(f"[SERVER DUCKING] Error in handle_station_alice_state: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_station_airplay_state(self, request: web.Request) -> web.Response:
        """
        Принимает событие состояния AirPlay со Станции (playing / idle).
        POST /api/station/airplay_state {"state": "playing", "ip": "..."}
        Если AirPlay начинает воспроизведение, ставит на паузу медиаплеер Squeezelite (Music Assistant).
        """
        try:
            body = await request.json()
            state = body.get("state", "")
            ip = body.get("ip") or request.remote or ""
            opts = self.options_callback() if self.options_callback else {}
            res = await self.ducking_manager.on_airplay_state(ip, state, opts)
            return web.json_response(res)
        except Exception as e:
            logger.error(f"[MUTUAL PLAYBACK] Error in handle_station_airplay_state: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def _on_ha_state_changed(self, entity_id: str, new_state: str, old_state: str):
        """
        Слушатель событий смены состояния сущностей в Home Assistant.
        Передаёт события в планировщик (ha_event триггеры) и отключает активный AirPlay при запуске Squeezelite.
        """
        try:
            await scheduler_singleton.on_ha_state_changed(entity_id, new_state, old_state)
        except Exception as e:
            logger.warning(f"[SCHEDULER] HA state hook error for {entity_id}: {e}")

        try:
            await quiet_mode_singleton.on_ha_state_changed(entity_id, new_state, old_state)
        except Exception as e:
            logger.warning(f"[QUIET] HA state hook error for {entity_id}: {e}")

        if new_state != "playing":
            return

        opts = self.options_callback() if self.options_callback else {}
        for dev in self.device_manager.get_all_devices():
            if dev.get("device_type") == "yandex_station":
                player = StationDuckingManager.resolve_station_player(dev, opts)
                if player and player == entity_id:
                    ip = dev.get("ip") or dev.get("config", {}).get("ip")
                    if ip and ip != "---":
                        logger.info(f"[MUTUAL PLAYBACK] Squeezelite player {player} started playing -> checking/stopping active AirPlay on {ip}")
                        await self.quasar_manager.stop_station_airplay_if_playing(ip)

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

        # Подписка на события Home Assistant для взаимного исключения AirPlay и Squeezelite
        if self.ha_api:
            asyncio.create_task(self.ha_api.start_state_listener(self._on_ha_state_changed))

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
                    engine.reload_model(save_path)

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

    # ------------------------------------------------------------------ #
    # Звуковые сигналы (Earcons) и нативный режим Яндекс Станции 2
    # ------------------------------------------------------------------ #

    async def handle_earcons_catalog(self, request):
        return web.json_response({"success": True, "catalog": list_earcon_presets()})

    async def handle_earcon_preview(self, request):
        category = request.match_info.get("category", "").strip().lower()
        preset = request.match_info.get("preset", "").strip().lower()
        wav = get_earcon_wav(category, preset, sample_rate=24000)
        return web.Response(
            body=wav,
            content_type="audio/wav",
            headers={"Cache-Control": "no-cache"},
        )

    async def handle_device_play_earcon(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        category = str(data.get("category", "smarthome"))
        preset = str(data.get("preset", "marimba_tudun"))
        dev = self.device_manager.get_device(mac)
        if not dev:
            return web.json_response({"success": False, "error": "Device not found"}, status=404)
        ws = self.device_manager.active_sockets.get(mac)
        if ws and not getattr(ws, "closed", False):
            pcm = get_earcon_pcm(category, preset, sample_rate=24000, volume=1.0)
            if pcm:
                await ws.send(pcm)
                return web.json_response({"success": True, "via": "ws"})
        ok = await self.openai_service.play_earcon_ssh(dev, category, preset)
        return web.json_response({"success": ok, "via": "ssh"})

    def _resolve_station_ssh(self, mac: str):
        dev = self.device_manager.get_device(mac)
        if not dev:
            return None, None, 2222
        cfg = dev.get("config", {})
        ip = (dev.get("ip") or cfg.get("ip") or "").strip()
        port = int(cfg.get("ssh_port", 2222))
        return dev, ip, port

    async def handle_station_native_status(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        dev, ip, port = self._resolve_station_ssh(mac)
        if not dev or not ip or ip == "---":
            return web.json_response({"success": False, "error": "Station IP not configured"}, status=400)
        res = await self.quasar_manager.get_native_station_status(ip, port=port)
        return web.json_response(res)

    async def handle_station_toggle_alice(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        dev, ip, port = self._resolve_station_ssh(mac)
        if not dev or not ip or ip == "---":
            return web.json_response({"success": False, "error": "Station IP not configured"}, status=400)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        disable_alice = bool(data.get("disable_alice", False))
        res = await self.quasar_manager.set_alice_disabled(ip, disable_alice, port=port)
        if res.get("success"):
            await self.device_manager.update_device_config(mac, {"disable_alice": disable_alice})
        return web.json_response(res)

    async def handle_station_control_stationd(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        dev, ip, port = self._resolve_station_ssh(mac)
        if not dev or not ip or ip == "---":
            return web.json_response({"success": False, "error": "Station IP not configured"}, status=400)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        action = str(data.get("action", "status"))
        if action == "deploy":
            server_ip = request.host.split(":")[0] if request.host else None
            res = await self.quasar_manager.deploy_stationd_files(ip, port=port, server_ip=server_ip)
        else:
            res = await self.quasar_manager.control_stationd(ip, action, port=port)
        return web.json_response(res)

    async def handle_station_upload_model(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        dev, ip, port = self._resolve_station_ssh(mac)
        if not dev or not ip or ip == "---":
            return web.json_response({"success": False, "error": "Station IP not configured"}, status=400)
        try:
            reader = await request.multipart()
            field = await reader.next()
            if not field or field.name != "model":
                return web.json_response({"success": False, "error": "Field 'model' not found"}, status=400)
            filename = field.filename or "porfiriy.onnx"
            if not filename.endswith(".onnx"):
                return web.json_response({"success": False, "error": "Only .onnx files allowed"}, status=400)
            chunks = []
            while True:
                chunk = await field.read_chunk(16384)
                if not chunk:
                    break
                chunks.append(chunk)
            model_bytes = b"".join(chunks)
            res = await self.quasar_manager.upload_station_ww_model(ip, model_bytes, filename=filename, port=port)
            if res.get("success") and res.get("model_name"):
                await self.device_manager.update_device_config(mac, {"ww_model": res["model_name"]})
            return web.json_response(res)
        except Exception as e:
            logger.error(f"[STATIOND] Upload model error: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_station_dump_mic(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        ws = self.device_manager.active_sockets.get(mac)
        if not ws or getattr(ws, "closed", False):
            return web.json_response({"success": False, "error": "Станция не подключена по WebSocket (porfiriy-stationd не запущен)"}, status=400)
        try:
            await ws.send(json.dumps({"type": "dump_mic"}))
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_station_wifi_get(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        dev, ip, port = self._resolve_station_ssh(mac)
        if not dev or not ip or ip == "---":
            return web.json_response({"success": False, "error": "Station IP not configured"}, status=400)
        res = await self.quasar_manager.get_station_wifi_networks(ip, port=port)
        return web.json_response(res)

    async def handle_station_wifi_save(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        dev, ip, port = self._resolve_station_ssh(mac)
        if not dev or not ip or ip == "---":
            return web.json_response({"success": False, "error": "Station IP not configured"}, status=400)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        res = await self.quasar_manager.save_station_wifi_network(
            ip,
            ssid=str(data.get("ssid", "")),
            password=str(data.get("password", "")),
            server_ip=str(data.get("server_ip", "")),
            priority=int(data.get("priority", 10)),
            ws_port=int(data.get("ws_port", 8765)),
            port=port,
        )
        return web.json_response(res)

    async def handle_station_wifi_delete(self, request):
        mac = request.match_info.get("mac", "").strip().lower()
        dev, ip, port = self._resolve_station_ssh(mac)
        if not dev or not ip or ip == "---":
            return web.json_response({"success": False, "error": "Station IP not configured"}, status=400)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        res = await self.quasar_manager.delete_station_wifi_network(ip, ssid=str(data.get("ssid", "")), port=port)
        return web.json_response(res)

    # ------------------------------------------------------------------
    # Porfiriy 2.0: Пользователи, семейные каналы, плагины/MCP, аудит
    # ------------------------------------------------------------------
    async def handle_get_users(self, request):
        return web.json_response({
            "success": True,
            "users": db_singleton.list_users(),
            "family_channels": db_singleton.get_family_channels(),
        })

    async def handle_save_user(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        try:
            user = db_singleton.upsert_user(
                user_id=data.get("id"),
                name=str(data.get("name") or ""),
                gender=str(data.get("gender") or "unknown"),
                voice_notes=str(data.get("voice_notes") or ""),
                persona_notes=str(data.get("persona_notes") or ""),
                telegram_chat_id=data.get("telegram_chat_id"),
                ha_tracker_entity=data.get("ha_tracker_entity"),
                is_active=bool(data.get("is_active", True)),
            )
            return web.json_response({"success": True, "user": user})
        except ValueError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_delete_user(self, request):
        uid = request.match_info.get("user_id", "").strip()
        try:
            deleted = db_singleton.delete_user(uid)
            if not deleted:
                return web.json_response({"success": False, "error": "Пользователь не найден"}, status=404)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_save_family_channels(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        try:
            if isinstance(data, dict):
                for ctype, val in data.items():
                    if ctype in ("telegram_chat",):
                        db_singleton.set_family_channel(ctype, str(val or ""), label="Общий чат дома")
            return web.json_response({"success": True, "family_channels": db_singleton.get_family_channels()})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_get_providers(self, request):
        return web.json_response({
            "success": True,
            "providers": db_singleton.list_providers(mask_secrets=True),
        })

    async def handle_save_provider(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        try:
            prov = db_singleton.upsert_provider(
                provider_id=data.get("id"),
                kind=str(data.get("kind") or "mcp_http"),
                title=str(data.get("title") or ""),
                description=str(data.get("description") or ""),
                enabled=data.get("enabled"),
                config=data.get("config") if isinstance(data.get("config"), dict) else None,
                cached_tools=data.get("cached_tools") if isinstance(data.get("cached_tools"), list) else None,
            )
            return web.json_response({"success": True, "provider": prov})
        except ValueError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_toggle_provider(self, request):
        pid = request.match_info.get("provider_id", "").strip()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        enabled = bool(data.get("enabled", False))
        ok = db_singleton.set_provider_enabled(pid, enabled)
        if not ok:
            return web.json_response({"success": False, "error": "Провайдер не найден"}, status=404)
        return web.json_response({"success": True, "enabled": enabled})

    async def handle_sync_provider_tools(self, request):
        pid = request.match_info.get("provider_id", "").strip()
        prov = db_singleton.get_provider(pid, mask_secrets=False)
        if not prov:
            return web.json_response({"success": False, "error": "Провайдер не найден"}, status=404)
        if prov.get("kind") != "mcp_http":
            return web.json_response({"success": False, "error": "Синхронизация поддерживается только для mcp_http"}, status=400)
        cfg = prov.get("config") or {}
        url = str(cfg.get("url") or "").strip()
        api_token = str(cfg.get("api_token") or "").strip()
        extra_headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else None
        res = await mcp_http_client_singleton.list_tools(url, api_token=api_token, extra_headers=extra_headers)
        if not res.get("success"):
            return web.json_response(res, status=400)
        tools = res.get("tools") or []
        db_singleton.upsert_provider(
            provider_id=pid,
            kind=prov["kind"],
            title=prov["title"],
            description=prov.get("description") or "",
            enabled=prov.get("enabled"),
            config=None,
            cached_tools=tools,
        )
        return web.json_response({
            "success": True,
            "count": len(tools),
            "latency_ms": res.get("latency_ms", 0),
            "tools": [t["name"] for t in tools],
            "server_info": res.get("server_info") or {},
        })

    async def handle_delete_provider(self, request):
        pid = request.match_info.get("provider_id", "").strip()
        try:
            deleted = db_singleton.delete_provider(pid)
            if not deleted:
                return web.json_response({"success": False, "error": "Провайдер не найден"}, status=404)
            return web.json_response({"success": True})
        except ValueError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_save_provider_account(self, request):
        pid = request.match_info.get("provider_id", "").strip()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        try:
            owner_raw = data.get("owner_user_id")
            owner = str(owner_raw).strip() if owner_raw else None
            acc = db_singleton.upsert_provider_account(
                provider_id=pid,
                label=str(data.get("label") or ""),
                owner_user_id=owner,
                credentials=data.get("credentials") if isinstance(data.get("credentials"), dict) else {},
                settings=data.get("settings") if isinstance(data.get("settings"), dict) else {},
                account_id=data.get("id"),
                enabled=bool(data.get("enabled", True)),
            )
            return web.json_response({"success": True, "account": acc})
        except ValueError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_delete_provider_account(self, request):
        aid = request.match_info.get("account_id", "").strip()
        try:
            deleted = db_singleton.delete_provider_account(aid)
            if not deleted:
                return web.json_response({"success": False, "error": "Аккаунт не найден"}, status=404)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_get_tools_audit(self, request):
        try:
            limit = int(request.query.get("limit", "50"))
        except Exception:
            limit = 50
        return web.json_response({"success": True, "items": db_singleton.list_audit_log(limit=limit)})

    async def handle_test_telegram_bot(self, request):
        import time as _time
        try:
            data = await request.json()
        except Exception:
            data = {}
        target = str((data or {}).get("target") or "denis").strip()
        text = str(
            (data or {}).get("text")
            or "Привет! Это тестовое сообщение от Порфирия 2.0. Канал связи работает штатно."
        ).strip()
        title = str((data or {}).get("title") or "Порфирий • Тест связи").strip()
        provider = db_singleton.get_provider("builtin.telegram_bot", mask_secrets=False)
        t0 = _time.monotonic()
        res = await TelegramBotPlugin.send_message(
            db=db_singleton,
            provider=provider,
            ha_api=self.ha_api,
            target=target,
            text=text,
            title=title,
        )
        dur_ms = int((_time.monotonic() - t0) * 1000)
        try:
            db_singleton.log_tool_call(
                tool_name="send_to_telegram (test)",
                provider_id="builtin.telegram_bot",
                speaker_user_id=target,
                args_preview=f"target={target}",
                status="ok" if res.get("status") == "ok" else "error",
                duration_ms=dur_ms,
                error=str(res.get("error") or ""),
            )
        except Exception:
            pass
        ok = res.get("status") == "ok"
        return web.json_response({"success": ok, **res}, status=200 if ok else 400)

    async def handle_get_memory(self, request):
        q = request.query.get("q", "").strip()
        kind = request.query.get("kind", "").strip() or None
        list_name = request.query.get("list_name", "").strip() or None
        status = request.query.get("status", "active").strip()
        if status == "all":
            status = None
        try:
            limit = int(request.query.get("limit", "100"))
        except Exception:
            limit = 100
        items = db_singleton.search_memory(
            query=q,
            kind=kind,
            list_name=list_name,
            status=status,
            limit=limit,
            touch_access=False,
        )
        return web.json_response({
            "success": True,
            "items": items,
            "lists": db_singleton.list_memory_lists(),
            "stats": db_singleton.get_memory_stats(),
        })

    async def handle_add_memory(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        try:
            ttl_raw = data.get("ttl_days")
            item = db_singleton.add_memory_item(
                kind=str(data.get("kind") or "fact"),
                content=str(data.get("content") or ""),
                owner_user_id=(str(data.get("owner_user_id")).strip() if data.get("owner_user_id") else None),
                list_name=str(data.get("list_name") or ""),
                title=str(data.get("title") or ""),
                tags=str(data.get("tags") or ""),
                is_pinned=bool(data.get("is_pinned", False)),
                ttl_days=int(ttl_raw) if ttl_raw else None,
            )
            return web.json_response({"success": True, "item": item})
        except ValueError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_patch_memory(self, request):
        try:
            item_id = int(request.match_info.get("item_id", "0"))
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid request"}, status=400)
        updated = db_singleton.update_memory_item(item_id, **(data if isinstance(data, dict) else {}))
        if not updated:
            return web.json_response({"success": False, "error": "Запись не найдена"}, status=404)
        return web.json_response({"success": True, "item": updated})

    async def handle_delete_memory(self, request):
        try:
            item_id = int(request.match_info.get("item_id", "0"))
        except Exception:
            return web.json_response({"success": False, "error": "Invalid id"}, status=400)
        deleted = db_singleton.delete_memory_item(item_id)
        if not deleted:
            return web.json_response({"success": False, "error": "Запись не найдена"}, status=404)
        return web.json_response({"success": True})

    async def handle_cleanup_memory(self, request):
        prov = db_singleton.get_provider("builtin.memory", mask_secrets=False) or {}
        cfg = prov.get("config") or {}
        res = db_singleton.run_memory_cleanup(
            auto_cleanup_days=int(cfg.get("auto_cleanup_days") or 30),
            archive_days=int(cfg.get("archive_days") or 14),
            max_active_items=int(cfg.get("max_active_items") or 500),
        )
        return web.json_response({"success": True, **res, "stats": db_singleton.get_memory_stats()})

    async def handle_tg_userbot_auth_start(self, request):
        aid = request.match_info.get("account_id", "").strip()
        res = await TelegramUserbotPlugin.start_auth(db_singleton, aid)
        return web.json_response(res, status=200 if res.get("success") else 400)

    async def handle_tg_userbot_auth_verify(self, request):
        aid = request.match_info.get("account_id", "").strip()
        try:
            data = await request.json()
        except Exception:
            data = {}
        res = await TelegramUserbotPlugin.verify_auth(
            db_singleton,
            aid,
            code=str((data or {}).get("code") or ""),
            password=str((data or {}).get("password") or ""),
        )
        return web.json_response(res, status=200 if res.get("success") else 400)

    async def handle_mail_imap_test(self, request):
        aid = request.match_info.get("account_id", "").strip()
        res = await MailImapPlugin.test_connection(db_singleton, aid)
        return web.json_response(res, status=200 if res.get("success") else 400)

    async def handle_test_keenetic(self, request):
        res = await KeeneticPlugin.test_connection(db_singleton, self.ha_api)
        return web.json_response(res, status=200 if res.get("success") else 400)

    async def handle_kinowatch_auth_start(self, request):
        res = await KinoWatchPlugin.start_device_auth(db_singleton)
        return web.json_response(res, status=200 if res.get("success") else 400)

    async def handle_kinowatch_auth_poll(self, request):
        res = await KinoWatchPlugin.poll_device_auth(db_singleton)
        return web.json_response(res, status=200 if res.get("success") else 400)

    async def handle_kinowatch_folders(self, request):
        res = await KinoWatchPlugin.sync_folders(db_singleton)
        return web.json_response(res, status=200 if res.get("success") else 400)

    async def handle_test_yandex_maps(self, request):
        res = await YandexMapsPlugin.test_connection(db_singleton)
        return web.json_response(res, status=200 if res.get("success") else 400)

    async def handle_test_big_model(self, request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        model_id = str((data or {}).get("model") or "").strip()
        enable_search = bool((data or {}).get("enable_google_search", False))
        opts = self.options_callback() if self.options_callback else {}
        res = await big_model_pool_singleton.test_model(
            model_id,
            opts.get("gemini_api_key", ""),
            enable_google_search=enable_search,
        )
        return web.json_response(res, status=200 if res.get("success") else 400)

    async def handle_test_web_search(self, request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        opts = self.options_callback() if self.options_callback else {}
        raw_key = str((data or {}).get("serper_api_key") or "").strip()
        if not raw_key or "..." in raw_key or "*" in raw_key:
            raw_key = str(opts.get("serper_api_key") or "").strip()
        query = str((data or {}).get("query") or "курс доллара к рублю сегодня").strip()
        res = await serper_search_singleton.search(query, raw_key)
        payload = {
            "success": bool(res.get("success")),
            "provider": res.get("provider", "serper.dev"),
            "latency_ms": res.get("latency_ms", 0),
            "results_count": res.get("results_count", 0),
            "reply": (res.get("answer") or "")[:240],
            "error": res.get("error", ""),
        }
        return web.json_response(payload, status=200 if payload["success"] else 400)

    # ------------------------------------------------------------------ #
    # Persistent Scheduler REST API
    # ------------------------------------------------------------------ #
    async def handle_get_schedules(self, request):
        status = request.query.get("status", "").strip() or None
        return web.json_response({
            "success": True,
            "jobs": db_singleton.list_scheduled_jobs(status=status),
        })

    async def handle_create_schedule(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        try:
            job = await scheduler_singleton.create_job(
                job_id=data.get("id"),
                title=str(data.get("title") or "").strip(),
                trigger=data.get("trigger") if isinstance(data.get("trigger"), dict) else {},
                action=data.get("action") if isinstance(data.get("action"), dict) else {},
                owner_user_id=str(data.get("owner_user_id") or "family"),
                notify_channel=str(data.get("notify_channel") or ""),
                quiet_hours=str(data.get("quiet_hours") or ""),
                one_shot=data.get("one_shot") if data.get("one_shot") is None else bool(data.get("one_shot")),
                enabled=bool(data.get("enabled", True)),
            )
            return web.json_response({"success": True, "job": job})
        except ValueError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_patch_schedule(self, request):
        jid = request.match_info.get("job_id", "").strip()
        existing = db_singleton.get_scheduled_job(jid)
        if not existing:
            return web.json_response({"success": False, "error": "Задача не найдена"}, status=404)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        try:
            job = await scheduler_singleton.create_job(
                job_id=jid,
                title=str(data.get("title") or existing["title"]).strip(),
                trigger=data.get("trigger") if isinstance(data.get("trigger"), dict) else existing["trigger"],
                action=data.get("action") if isinstance(data.get("action"), dict) else existing["action"],
                owner_user_id=str(data.get("owner_user_id") or existing["owner_user_id"]),
                notify_channel=str(data.get("notify_channel") if "notify_channel" in data else existing["notify_channel"]),
                quiet_hours=str(data.get("quiet_hours") if "quiet_hours" in data else existing["quiet_hours"]),
                one_shot=bool(data["one_shot"]) if "one_shot" in data else existing["one_shot"],
                enabled=bool(data["enabled"]) if "enabled" in data else existing["enabled"],
            )
            return web.json_response({"success": True, "job": job})
        except ValueError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_delete_schedule(self, request):
        jid = request.match_info.get("job_id", "").strip()
        ok = db_singleton.delete_scheduled_job(jid)
        if not ok:
            return web.json_response({"success": False, "error": "Задача не найдена"}, status=404)
        return web.json_response({"success": True})

    async def handle_toggle_schedule(self, request):
        jid = request.match_info.get("job_id", "").strip()
        job = db_singleton.get_scheduled_job(jid)
        if not job:
            return web.json_response({"success": False, "error": "Задача не найдена"}, status=404)
        try:
            data = await request.json()
        except Exception:
            data = {}
        enabled = bool(data.get("enabled", not job["enabled"]))
        nxt = job.get("next_run_at")
        if enabled and not nxt and (job.get("trigger") or {}).get("type") in ("interval", "cron"):
            nxt = scheduler_singleton._compute_following_run(job["trigger"])
        upd = db_singleton.upsert_scheduled_job(
            job_id=jid,
            job_type=job["job_type"],
            title=job["title"],
            owner_user_id=job["owner_user_id"],
            cron_or_run_at=job["cron_or_run_at"],
            trigger=job["trigger"],
            action=job["action"],
            notify_channel=job["notify_channel"],
            quiet_hours=job["quiet_hours"],
            one_shot=job["one_shot"],
            enabled=enabled,
            status="active" if enabled else "paused",
            next_run_at=nxt,
        )
        return web.json_response({"success": True, "job": upd})

    async def handle_run_schedule(self, request):
        jid = request.match_info.get("job_id", "").strip()
        res = await scheduler_singleton.run_job_now(jid)
        return web.json_response({"success": res.get("status") == "ok", "result": res}, status=200 if res.get("status") == "ok" else 400)

    async def handle_get_schedule_runs(self, request):
        jid = request.match_info.get("job_id", "").strip()
        try:
            limit = int(request.query.get("limit", "20"))
        except Exception:
            limit = 20
        return web.json_response({"success": True, "runs": db_singleton.list_job_runs(jid, limit=limit)})

    async def handle_get_ww_captures(self, request):
        mac = request.query.get("mac", "").strip() or None
        try:
            limit = int(request.query.get("limit", "50"))
        except Exception:
            limit = 50
        data = ww_capture_store.list_clips(mac=mac, limit=limit)
        return web.json_response({"success": True, **data})

    async def handle_get_ww_capture_clip(self, request):
        filename = request.match_info.get("filename", "")
        full_path = ww_capture_store.get_clip_path(filename)
        if not full_path:
            return web.json_response({"success": False, "error": "Clip not found"}, status=404)
        return web.FileResponse(full_path, headers={"Content-Type": "audio/wav", "Cache-Control": "no-store"})

    async def handle_delete_ww_capture_clip(self, request):
        filename = request.match_info.get("filename", "")
        ok = ww_capture_store.delete_clip(filename)
        return web.json_response({"success": ok})

    async def handle_clear_ww_captures(self, request):
        mac = request.query.get("mac", "").strip() or None
        removed = ww_capture_store.clear_clips(mac=mac)
        return web.json_response({"success": True, "removed": removed})

    async def handle_download_ww_captures_archive(self, request):
        from datetime import datetime
        mac = request.query.get("mac", "").strip() or None
        zip_bytes = await asyncio.to_thread(ww_capture_store.build_zip_bytes, mac)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"porfiriy_ww_captures_{ts}.zip"
        return web.Response(
            body=zip_bytes,
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": f'attachment; filename="{fname}"',
                "Cache-Control": "no-store",
            },
        )

    async def handle_get_quiet_candidates(self, request):
        try:
            data = await quiet_mode_singleton.get_candidates()
            return web.json_response(data)
        except Exception as e:
            logger.warning(f"[QUIET] Failed to list candidates: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_get_quiet_status(self, request):
        try:
            return web.json_response({"success": True, "status": quiet_mode_singleton.get_status()})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)



