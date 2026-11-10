import asyncio
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class StationDuckingManager:
    """
    Серверный контроллер авто-дакинга и громкости для Яндекс Станции через Home Assistant.
    Управляет исключительно громкостью привязанного медиаплеера Music Assistant (media_player.volume_set).
    """

    DUCK_REDUCTION_FACTOR = 0.40  # Снижение на 60% (остается 40% от исходной громкости)
    MIN_DUCK_VOLUME = 0.02
    DEFAULT_WATCHDOG_S = 8.0

    def __init__(self, device_manager, ha_api):
        self.device_manager = device_manager
        self.ha_api = ha_api
        # mac -> {"active": bool, "player": str, "original_volume": float, "speech_arrived": bool, "watchdog_task": Task | None}
        self._states: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def resolve_station_player(dev: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> str:
        cfg = dev.get("config", {})
        opts = options or {}
        return (
            cfg.get("mass_player") or
            cfg.get("airplay_player") or
            cfg.get("response_player") or
            opts.get("default_mass_player") or
            opts.get("default_airplay_player") or
            opts.get("default_media_player") or
            ""
        ).strip()

    def find_station_by_ip(self, client_ip: str) -> Optional[Dict[str, Any]]:
        ip = (client_ip or "").strip()
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        if not ip or not self.device_manager:
            return None
        for dev in self.device_manager.get_devices():
            dev_ip = (dev.get("ip") or dev.get("config", {}).get("ip") or "").strip()
            if dev_ip and dev_ip == ip:
                return dev
        return None

    def _cancel_watchdog(self, mac: str) -> None:
        st = self._states.get(mac)
        if not st:
            return
        task = st.get("watchdog_task")
        if task and not task.done():
            task.cancel()
        st["watchdog_task"] = None

    def get_saved_volume(self, mac: str) -> Optional[float]:
        st = self._states.get(mac)
        if st and st.get("active"):
            return st.get("original_volume")
        return None

    def set_explicit_volume(self, mac: str, volume_level: float) -> None:
        """Обновляет сохраненную громкость при ручной или голосовой установке, чтобы дакинг не откатил её."""
        self._cancel_watchdog(mac)
        st = self._states.get(mac)
        if st:
            st["original_volume"] = volume_level
            st["active"] = False
            st["speech_arrived"] = False

    async def _restore_volume(self, mac: str, reason: str) -> None:
        st = self._states.get(mac)
        if not st or not st.get("active"):
            return

        player = st.get("player")
        orig_vol = st.get("original_volume")
        st["active"] = False
        st["speech_arrived"] = False
        self._cancel_watchdog(mac)

        if player and orig_vol is not None and self.ha_api:
            logger.info(f"[SERVER DUCKING] Restoring {player} for {mac} to {orig_vol} ({reason})")
            await self.ha_api.call_service("media_player", "volume_set", {
                "entity_id": player,
                "volume_level": orig_vol
            })

    async def _watchdog_restore(self, mac: str, timeout_s: float) -> None:
        try:
            await asyncio.sleep(timeout_s)
            st = self._states.get(mac)
            if st and st.get("active") and not st.get("speech_arrived"):
                await self._restore_volume(mac, f"watchdog {timeout_s}s timeout")
        except asyncio.CancelledError:
            pass

    async def on_alice_state(self, client_ip: str, state: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Обрабатывает событие состояния Алисы (listening / idle) со Станции."""
        dev = self.find_station_by_ip(client_ip)
        if not dev:
            return {"success": False, "error": f"Station not found for IP {client_ip}"}

        mac = dev.get("mac", "")
        player = self.resolve_station_player(dev, options)
        if not player or not self.ha_api:
            return {"success": False, "error": "No media_player configured for station"}

        state_norm = (state or "").strip().lower()
        if state_norm in ("listening", "wake", "1", "2", "3"):
            existing = self._states.get(mac)
            if existing and existing.get("active"):
                return {"success": True, "action": "already_ducked", "player": player}

            entity_state = await self.ha_api.get_entity_state(player)
            if not isinstance(entity_state, dict) or entity_state.get("state") != "playing":
                return {"success": True, "action": "skipped_not_playing", "player": player}

            curr_vol = entity_state.get("attributes", {}).get("volume_level")
            if curr_vol is None:
                return {"success": True, "action": "skipped_no_volume", "player": player}

            orig_vol = float(curr_vol)
            duck_vol = max(self.MIN_DUCK_VOLUME, round(orig_vol * self.DUCK_REDUCTION_FACTOR, 2))

            self._cancel_watchdog(mac)
            self._states[mac] = {
                "active": True,
                "player": player,
                "original_volume": orig_vol,
                "speech_arrived": False,
                "watchdog_task": None,
            }

            logger.info(f"[SERVER DUCKING] Alice listening on {mac}: ducking {player} {orig_vol} -> {duck_vol} (-60%)")
            await self.ha_api.call_service("media_player", "volume_set", {
                "entity_id": player,
                "volume_level": duck_vol
            })
            self._states[mac]["watchdog_task"] = asyncio.create_task(
                self._watchdog_restore(mac, self.DEFAULT_WATCHDOG_S)
            )
            return {"success": True, "action": "ducked", "player": player, "from": orig_vol, "to": duck_vol}

        elif state_norm in ("idle", "0"):
            st = self._states.get(mac)
            if st and st.get("active") and not st.get("speech_arrived"):
                await self._restore_volume(mac, "alice_idle")
                return {"success": True, "action": "restored", "player": player}
            return {"success": True, "action": "noop", "player": player}

        return {"success": False, "error": f"Unknown state: {state}"}

    async def on_speech_arrived(self, mac: str, restore_immediately: bool = False) -> Optional[float]:
        """
        Вызывается, когда слова с колонки дошли до Порфирия (/v1/chat/completions).
        Отменяет 8-секундный таймер; при restore_immediately=True сразу возвращает исходную громкость.
        """
        st = self._states.get(mac)
        if not st or not st.get("active"):
            return None

        orig_vol = st.get("original_volume")
        st["speech_arrived"] = True
        self._cancel_watchdog(mac)

        if restore_immediately:
            await self._restore_volume(mac, "speech_arrived")
        return orig_vol

    async def finish_speech(self, mac: str) -> None:
        """Восстанавливает громкость после завершения обработки речи Порфирием."""
        st = self._states.get(mac)
        if st and st.get("active"):
            await self._restore_volume(mac, "speech_finished")
