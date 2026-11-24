import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from audio_utils import is_simple_smarthome_action
from net_utils import get_device_ip
from prompt_builder import resolve_target_player

tool_logger = logging.getLogger("tools")


class ToolExecutor:
    """Единый исполнитель инструментов Gemini Live (Home Assistant, Music Assistant)."""

    def __init__(self, ha_api: Any, quasar_manager: Any = None, ducking_manager: Any = None):
        self.ha_api = ha_api
        self.quasar_manager = quasar_manager
        self.ducking_manager = ducking_manager

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
