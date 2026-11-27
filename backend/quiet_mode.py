import asyncio
import datetime
import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("quiet_mode")

_TRACKABLE_SENSOR_CLASSES = {"motion", "occupancy", "presence"}


def is_in_time_window(start_hm: str, end_hm: str, now_local: Optional[datetime.datetime] = None) -> bool:
    """Проверяет попадание текущего локального времени в окно start_hm-end_hm (с переходом через полночь)."""
    m1 = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(start_hm or ""))
    m2 = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(end_hm or ""))
    if not m1 or not m2:
        return False
    h1, min1 = int(m1.group(1)), int(m1.group(2))
    h2, min2 = int(m2.group(1)), int(m2.group(2))
    if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= min1 <= 59 and 0 <= min2 <= 59):
        return False
    cur = now_local or datetime.datetime.now().astimezone()
    cur_min = cur.hour * 60 + cur.minute
    s_min = h1 * 60 + min1
    e_min = h2 * 60 + min2
    if s_min <= e_min:
        return s_min <= cur_min < e_min
    return cur_min >= s_min or cur_min < e_min


def is_entity_awake(entity_id: str, state: str) -> bool:
    """Возвращает True, если сущность сигнализирует о том, что в доме НЕ спят."""
    domain = (entity_id or "").split(".", 1)[0].strip().lower()
    st = (state or "").strip().lower()
    if domain in ("light", "switch", "input_boolean", "binary_sensor"):
        return st == "on"
    if domain == "media_player":
        return st in ("playing", "on", "buffering")
    if domain == "cover":
        return st in ("open", "opening")
    return st == "on"


class QuietModeManager:
    """Умный ночной тихий режим: временное окно + реальные состояния света/ТВ/штор/датчиков HA."""

    def __init__(self) -> None:
        self.ha_api: Any = None
        self.device_manager: Any = None
        self.options_callback: Optional[Callable[[], Dict[str, Any]]] = None
        self._ha_states: Dict[str, str] = {}
        self._ha_names: Dict[str, str] = {}
        self._last_active: Optional[bool] = None
        self._clock_task: Optional[asyncio.Task] = None

    def configure(
        self,
        *,
        ha_api: Any,
        device_manager: Any,
        options_callback: Callable[[], Dict[str, Any]],
    ) -> None:
        self.ha_api = ha_api
        self.device_manager = device_manager
        self.options_callback = options_callback

    def _get_options(self) -> Dict[str, Any]:
        return self.options_callback() if self.options_callback else {}

    async def start(self) -> None:
        await self._hydrate_states()
        await self.sync_devices_threshold(force=True)
        if self._clock_task is None or self._clock_task.done():
            self._clock_task = asyncio.create_task(self._clock_loop())

    async def _hydrate_states(self) -> None:
        if not self.ha_api:
            return
        try:
            states = await self.ha_api.get_states()
            if isinstance(states, list):
                for s in states:
                    eid = str(s.get("entity_id") or "").strip()
                    if not eid:
                        continue
                    self._ha_states[eid] = str(s.get("state") or "")
                    fname = str((s.get("attributes") or {}).get("friendly_name") or eid).strip()
                    self._ha_names[eid] = fname
        except Exception as e:
            logger.warning(f"[QUIET] Failed to hydrate HA states: {e}")

    async def _clock_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                await self.sync_devices_threshold(force=False)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[QUIET] Clock loop error: {e}")

    async def on_ha_state_changed(self, entity_id: str, new_state: str, old_state: str) -> None:
        if not entity_id:
            return
        self._ha_states[entity_id] = str(new_state or "")
        tracked = self._get_options().get("quiet_mode_entities") or []
        if isinstance(tracked, list) and entity_id in tracked:
            await self.sync_devices_threshold(force=False)

    async def on_config_changed(self) -> None:
        await self.sync_devices_threshold(force=True)

    def get_status(self, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        opts = options if options is not None else self._get_options()
        enabled = bool(opts.get("quiet_mode_enabled", False))
        force = str(opts.get("quiet_mode_force") or "auto").strip().lower()
        if force not in ("auto", "on", "off"):
            force = "auto"
        start_hm = str(opts.get("quiet_mode_start") or "23:00").strip()
        end_hm = str(opts.get("quiet_mode_end") or "08:00").strip()
        tracked_raw = opts.get("quiet_mode_entities") or []
        tracked = [str(e).strip() for e in tracked_raw if str(e).strip()] if isinstance(tracked_raw, list) else []

        in_window = is_in_time_window(start_hm, end_hm)
        awake_entities: List[Dict[str, str]] = []
        for eid in tracked:
            st = self._ha_states.get(eid, "")
            if is_entity_awake(eid, st):
                awake_entities.append({
                    "entity_id": eid,
                    "name": self._ha_names.get(eid) or eid,
                    "state": st,
                })

        if not enabled:
            active = False
            reason = "выключен в настройках"
        elif force == "off":
            active = False
            reason = "принудительно выключен"
        elif force == "on":
            active = True
            reason = "принудительно включён"
        else:
            active = in_window and len(awake_entities) == 0
            if not in_window:
                reason = f"сейчас день (окно {start_hm}–{end_hm})"
            elif awake_entities:
                names = ", ".join(x["name"] for x in awake_entities[:3])
                if len(awake_entities) > 3:
                    names += f" и ещё {len(awake_entities) - 3}"
                reason = f"включено: {names}"
            else:
                reason = f"ночное окно {start_hm}–{end_hm}, все выбранные объекты спят"

        return {
            "enabled": enabled,
            "active": active,
            "force": force,
            "in_window": in_window,
            "start": start_hm,
            "end": end_hm,
            "tracked_count": len(tracked),
            "awake_entities": awake_entities,
            "reason": reason,
        }

    def is_active(self, options: Optional[Dict[str, Any]] = None) -> bool:
        return bool(self.get_status(options).get("active"))

    def get_effective_ww_threshold(self, dev_cfg: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> float:
        opts = options if options is not None else self._get_options()
        day_thr = float((dev_cfg or {}).get("ww_threshold", 0.20))
        if self.is_active(opts):
            night_thr = float(opts.get("quiet_mode_ww_threshold", 0.42))
            return round(max(day_thr, min(0.98, night_thr)), 2)
        return round(day_thr, 2)

    async def sync_devices_threshold(self, force: bool = False) -> None:
        opts = self._get_options()
        st = self.get_status(opts)
        if not force and self._last_active is not None and st["active"] == self._last_active:
            return
        self._last_active = st["active"]
        logger.info(f"[QUIET] Quiet mode -> {'ACTIVE' if st['active'] else 'INACTIVE'} ({st['reason']})")

        if not self.device_manager:
            return
        for mac, dev in list(self.device_manager.devices.items()):
            if not dev.get("is_online") or dev.get("device_type") != "yandex_station":
                continue
            eff_thr = self.get_effective_ww_threshold(dev.get("config") or {}, opts)
            try:
                await self.device_manager.send_command(
                    mac,
                    {"type": "set_config", "config": {"ww_threshold": eff_thr}},
                )
                logger.info(f"[QUIET] Sent runtime ww_threshold={eff_thr:.2f} to {mac}")
            except Exception as e:
                logger.warning(f"[QUIET] Failed to push ww_threshold to {mac}: {e}")

    async def get_candidates(self) -> Dict[str, Any]:
        groups: Dict[str, List[Dict[str, Any]]] = {
            "lights": [],
            "media": [],
            "covers": [],
            "sensors": [],
        }
        if not self.ha_api:
            return {"success": True, "groups": groups, "status": self.get_status()}

        states = await self.ha_api.get_states()
        exposed = await self.ha_api.get_exposed_entities_metadata()

        for s in states or []:
            eid = str(s.get("entity_id") or "").strip()
            if not eid or "." not in eid:
                continue
            st_val = str(s.get("state") or "")
            attrs = s.get("attributes") or {}
            fname = str(attrs.get("friendly_name") or eid).strip()
            self._ha_states[eid] = st_val
            self._ha_names[eid] = fname

            domain = eid.split(".", 1)[0].lower()
            room = str((exposed.get(eid) or {}).get("room") or "")
            dev_class = str(attrs.get("device_class") or "").lower()

            target_group: Optional[str] = None
            if domain == "light":
                target_group = "lights"
            elif domain == "switch" and eid in exposed:
                target_group = "lights"
            elif domain == "media_player":
                target_group = "media"
            elif domain == "cover":
                target_group = "covers"
            elif domain == "input_boolean" or (domain == "binary_sensor" and dev_class in _TRACKABLE_SENSOR_CLASSES):
                target_group = "sensors"

            if not target_group:
                continue

            groups[target_group].append({
                "entity_id": eid,
                "name": fname,
                "domain": domain,
                "room": room,
                "state": st_val,
                "awake": is_entity_awake(eid, st_val),
            })

        for g in groups.values():
            g.sort(key=lambda item: (item["room"].lower(), item["name"].lower(), item["entity_id"]))

        return {"success": True, "groups": groups, "status": self.get_status()}


quiet_mode_singleton = QuietModeManager()
