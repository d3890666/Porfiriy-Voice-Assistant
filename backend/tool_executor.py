import asyncio
import datetime
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from audio_utils import get_earcon_pcm, is_simple_smarthome_action
from net_utils import get_device_ip
from prompt_builder import resolve_target_player

try:
    from .plugin_registry import plugin_registry_singleton
except ImportError:
    try:
        from plugin_registry import plugin_registry_singleton
    except ImportError:
        plugin_registry_singleton = None

tool_logger = logging.getLogger("tools")


class ToolExecutor:
    """Единый исполнитель инструментов Gemini Live (Home Assistant, Music Assistant, таймеры)."""

    def __init__(self, ha_api: Any, quasar_manager: Any = None, ducking_manager: Any = None, device_manager: Any = None):
        self.ha_api = ha_api
        self.quasar_manager = quasar_manager
        self.ducking_manager = ducking_manager
        self.device_manager = device_manager
        self._timers: Dict[str, Dict[str, Any]] = {}
        self._timer_seq: int = 0

    async def execute(
        self,
        name: str,
        raw_args: Optional[Dict[str, Any]],
        options: Dict[str, Any],
        *,
        dev: Optional[Dict[str, Any]] = None,
        turn_state: Optional[Dict[str, Any]] = None,
        on_feedback: Optional[Callable[[str, bool], Awaitable[None]]] = None,
        default_player_fallback: str = "media_player.living_room",
        user_query: str = "",
        on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """
        Выполняет инструмент и возвращает словарь ответа для Gemini FunctionResponse.
        on_feedback(kind, has_data): опциональный коллбэк для звуковых уведомлений (ESP32 earcons),
        где kind in ("success", "error").
        """
        args = dict(raw_args) if raw_args else {}
        target_default_player = resolve_target_player(dev, options) or default_player_fallback

        try:
            if name == "call_ha_service":
                domain = args.get("domain")
                service = args.get("service")
                entity_id = args.get("entity_id")

                service_data = {
                    k: v
                    for k, v in args.items()
                    if k not in ("domain", "service", "parameters") and v is not None
                }
                if isinstance(args.get("parameters"), dict):
                    service_data.update(args["parameters"])
                if entity_id and "entity_id" not in service_data:
                    service_data["entity_id"] = entity_id

                tool_logger.info(f"Gemini Calling Tool: {domain}.{service} with service_data: {service_data}")

                if turn_state is not None:
                    turn_state["tool_executed"] = True
                    if domain in ("media_player", "music_assistant", "mass"):
                        turn_state["playback_changed"] = True
                    if domain == "media_player" and service == "volume_set" and self.ducking_manager and dev:
                        vol_lvl = service_data.get("volume_level")
                        if vol_lvl is not None:
                            self.ducking_manager.set_explicit_volume(dev.get("mac", ""), float(vol_lvl))

                action_res = await self.ha_api.call_ha_action(
                    domain=domain,
                    service=service,
                    service_data=service_data,
                )
                tool_logger.info(f"HA Action Result: {action_res}")

                has_data = bool(action_res.get("has_response_data", False)) if isinstance(action_res, dict) else False
                if isinstance(action_res, dict) and action_res.get("status") == "error":
                    if on_feedback:
                        await on_feedback("error", has_data)
                    return {"status": "error", "error": action_res.get("error", "Unknown error")}

                if on_feedback:
                    await on_feedback("success", has_data)

                if turn_state is not None:
                    if domain == "media_player" and service in ("volume_set", "volume_up", "volume_down"):
                        turn_state["volume_changed"] = True
                    elif not has_data and is_simple_smarthome_action(domain, service):
                        turn_state["simple_smarthome_executed"] = True

                safe_result: Dict[str, Any] = {"status": "success"}
                if isinstance(action_res, dict):
                    if has_data:
                        safe_result["data"] = action_res.get("data")
                    elif action_res.get("result"):
                        safe_result["data"] = action_res.get("result")
                return safe_result

            if name == "get_ha_state":
                entity_id = args.get("entity_id")
                tool_logger.info(f"Gemini Calling get_ha_state for entity: {entity_id}")
                state_obj = await self.ha_api.get_entity_state(entity_id)
                if isinstance(state_obj, dict) and "state" in state_obj:
                    attrs = state_obj.get("attributes") or {}
                    safe_result = {
                        "status": "success",
                        "entity_id": entity_id,
                        "state": state_obj.get("state"),
                        "unit": attrs.get("unit_of_measurement"),
                        "friendly_name": attrs.get("friendly_name"),
                        "attributes": attrs,
                    }
                elif isinstance(state_obj, dict):
                    safe_result = state_obj
                else:
                    safe_result = {"status": "not_found", "entity_id": entity_id}
                tool_logger.info(f"HA get_ha_state result: {safe_result}")
                return safe_result

            if name == "search_music_assistant":
                query = args.get("name") or args.get("query") or ""
                artist = args.get("artist")
                media_type = args.get("media_type")
                tool_logger.info(f"Gemini Calling MA Search: name='{query}', artist='{artist or ''}', media_type={media_type}")
                res = await self.ha_api.search_music_assistant(
                    name=query,
                    media_type=media_type,
                    artist=artist,
                )
                tool_logger.info(f"MA Search Result: {res}")
                return res if isinstance(res, dict) else {"result": res}

            if name in ("play_music_assistant", "play_music"):
                uri_or_query = args.get("uri") or args.get("query") or args.get("name") or ""
                media_type = args.get("media_type")
                radio_mode = bool(args.get("radio_mode", False))
                target_player = args.get("player") or target_default_player

                if turn_state is not None:
                    turn_state["tool_executed"] = True
                    turn_state["playback_changed"] = True

                if self.quasar_manager and dev:
                    speaker_ip = get_device_ip(dev)
                    if speaker_ip:
                        asyncio.create_task(self.quasar_manager.stop_station_airplay_if_playing(speaker_ip))

                tool_logger.info(f"Gemini Playing MA URI: {uri_or_query} on {target_player}")
                res = await self.ha_api.play_music_assistant(
                    entity_id=target_player,
                    uri_or_query=uri_or_query,
                    media_type=media_type,
                    radio_mode=radio_mode,
                )
                tool_logger.info(f"MA Play Result: {res}")

                if on_feedback:
                    status = "error" if (isinstance(res, dict) and res.get("status") == "error") else "success"
                    await on_feedback(status, False)
                return res if isinstance(res, dict) else {"result": res}

            if name == "schedule_ha_action":
                delay_s = int(args.get("delay_seconds") or 0)
                if delay_s <= 0 or delay_s > 86400:
                    return {"status": "error", "error": "delay_seconds must be between 1 and 86400 (24 hours)"}
                domain = str(args.get("domain") or "alarm").strip().lower()
                service = str(args.get("service") or "ring").strip().lower()
                entity_id = args.get("entity_id")
                label = str(args.get("label") or f"{domain}.{service}" + (f" {entity_id}" if entity_id else "")).strip()
                service_data = {}
                if isinstance(args.get("parameters"), dict):
                    service_data.update(args["parameters"])
                if entity_id:
                    service_data["entity_id"] = entity_id

                if domain != "alarm" and not entity_id and domain in ("light", "switch", "cover", "fan", "climate", "media_player"):
                    return {"status": "error", "error": f"entity_id is required to schedule {domain}.{service}"}

                self._timer_seq += 1
                tid = f"t{self._timer_seq}"
                fire_ts = time.time() + delay_s
                fire_local = datetime.datetime.fromtimestamp(fire_ts).strftime("%H:%M:%S")
                caller_mac = (dev or {}).get("mac", "")

                async def _timer_runner(timer_id: str):
                    try:
                        await asyncio.sleep(delay_s)
                        if domain == "alarm":
                            tool_logger.info(f"[TIMER] Alarm '{label}' ({timer_id}) fired!")
                            ws = self.device_manager.active_sockets.get(caller_mac) if (self.device_manager and caller_mac) else None
                            if ws and not getattr(ws, "closed", False):
                                chime = get_earcon_pcm("smarthome", "apple_homepod", sample_rate=24000, volume=1.0)
                                if chime:
                                    for _ in range(3):
                                        await ws.send(chime)
                                        await asyncio.sleep(0.45)
                                    await ws.send(json.dumps({"type": "turn_complete"}))
                        else:
                            tool_logger.info(f"[TIMER] Executing scheduled {domain}.{service} {service_data} ({timer_id}: {label})")
                            res = await self.ha_api.call_ha_action(
                                domain=domain,
                                service=service,
                                service_data=service_data,
                            )
                            tool_logger.info(f"[TIMER] Result ({timer_id}): {res}")
                    except asyncio.CancelledError:
                        tool_logger.info(f"[TIMER] Cancelled {timer_id} ({label})")
                    except Exception as ex:
                        tool_logger.error(f"[TIMER] Error in {timer_id} ({label}): {ex}")
                    finally:
                        self._timers.pop(timer_id, None)

                task = asyncio.create_task(_timer_runner(tid))
                self._timers[tid] = {
                    "id": tid,
                    "task": task,
                    "fire_ts": fire_ts,
                    "fire_local": fire_local,
                    "delay_seconds": delay_s,
                    "domain": domain,
                    "service": service,
                    "entity_id": entity_id or "",
                    "label": label,
                    "mac": caller_mac,
                }
                if turn_state is not None:
                    turn_state["tool_executed"] = True
                tool_logger.info(f"[TIMER] Scheduled {tid}: '{label}' in {delay_s}s (at {fire_local})")
                return {
                    "status": "scheduled",
                    "timer_id": tid,
                    "delay_seconds": delay_s,
                    "fire_at_local": fire_local,
                    "label": label,
                }

            if name == "manage_timers":
                action = str(args.get("action") or "list").strip().lower()
                query = str(args.get("query") or "").strip().lower()
                now_ts = time.time()

                if action == "list":
                    items = [
                        {
                            "timer_id": t["id"],
                            "label": t["label"],
                            "remaining_seconds": max(0, int(t["fire_ts"] - now_ts)),
                            "fire_at_local": t["fire_local"],
                        }
                        for t in self._timers.values()
                    ]
                    return {"status": "success", "count": len(items), "timers": items}

                if action == "cancel_all":
                    n = len(self._timers)
                    for t in list(self._timers.values()):
                        t["task"].cancel()
                    self._timers.clear()
                    return {"status": "success", "cancelled_count": n}

                if action == "cancel":
                    target_ids = []
                    if query:
                        for tid, t in self._timers.items():
                            hay = f"{tid} {t['label']} {t['entity_id']}".lower()
                            if query in hay:
                                target_ids.append(tid)
                    elif self._timers:
                        # Если запрос не уточнён — отменяем последний поставленный таймер
                        target_ids.append(list(self._timers.keys())[-1])

                    cancelled = []
                    for tid in target_ids:
                        t = self._timers.pop(tid, None)
                        if t:
                            t["task"].cancel()
                            cancelled.append({"timer_id": tid, "label": t["label"]})
                    return {
                        "status": "success" if cancelled else "not_found",
                        "cancelled": cancelled,
                    }

                return {"status": "error", "error": f"Unknown manage_timers action: {action}"}

            if name == "web_search":
                try:
                    from big_models import big_model_pool_singleton
                except ImportError:
                    from .big_models import big_model_pool_singleton
                if on_progress:
                    try:
                        await on_progress("🔍 Ищу актуальную информацию в интернете...")
                    except Exception:
                        pass
                q_txt = str(args.get("query") or "").strip()
                if not q_txt:
                    return {"status": "error", "error": "Укажите поисковый запрос query"}
                search_prompt = (
                    f"Найди актуальную информацию в интернете и дай точный, краткий ответ на русском языке "
                    f"(с датами, цифрами и фактами без воды):\n{q_txt}"
                )
                big_res = await big_model_pool_singleton.run_text_task(
                    search_prompt,
                    options,
                    enable_google_search=True,
                )
                if turn_state is not None:
                    turn_state["tool_executed"] = True
                if big_res.get("success"):
                    return {"status": "ok", "model": big_res["model"], "answer": big_res["text"]}
                return {"status": "error", "error": big_res.get("error") or "Web search failed"}

            if name == "ask_big_model":
                try:
                    from big_models import big_model_pool_singleton
                except ImportError:
                    from .big_models import big_model_pool_singleton
                if on_progress:
                    try:
                        await on_progress("🔍 Передаю сложную задачу большой модели, скоро вернусь...")
                    except Exception:
                        pass
                task_txt = str(args.get("task") or "").strip()
                ctx_txt = str(args.get("context") or "").strip()
                full_prompt = f"{task_txt}\n\nКонтекст:\n{ctx_txt}" if ctx_txt else task_txt
                big_res = await big_model_pool_singleton.run_text_task(full_prompt, options)
                if turn_state is not None:
                    turn_state["tool_executed"] = True
                if big_res.get("success"):
                    return {"status": "ok", "model": big_res["model"], "answer": big_res["text"]}
                return {"status": "error", "error": big_res.get("error") or "Big model pool failed"}

            if plugin_registry_singleton and plugin_registry_singleton.handles_tool(name):
                res = await plugin_registry_singleton.execute_tool(
                    name,
                    args,
                    {
                        "options": options,
                        "dev": dev,
                        "ha_api": self.ha_api,
                        "turn_state": turn_state,
                        "device_manager": self.device_manager,
                        "user_query": user_query,
                        "on_progress": on_progress,
                    },
                )
                if turn_state is not None:
                    turn_state["tool_executed"] = True
                return res

            tool_logger.warning(f"Unknown tool called: {name}")
            return {"status": "unknown_tool", "error": f"Unknown tool: {name}"}

        except Exception as e:
            tool_logger.error(f"Error executing tool {name}: {e}")
            if on_feedback:
                try:
                    await on_feedback("error", False)
                except Exception:
                    pass
            return {"status": "error", "error": str(e)}
