import os
import asyncio
import aiohttp
import websockets
import json
import logging
import time
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class HomeAssistantAPI:
    def __init__(self, fallback_url: str = None, fallback_token: str = None):
        """
        Инициализация API клиента Home Assistant.
        Внутри Add-on используется SUPERVISOR_TOKEN и внутренний URL.
        """
        self.supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
        
        if self.supervisor_token:
            self.base_url = "http://supervisor/core/api"
            self.ws_url = "ws://supervisor/core/websocket"
            self.headers = {
                "Authorization": f"Bearer {self.supervisor_token}",
                "Content-Type": "application/json"
            }
            logger.info("HA API initialized in Add-on mode (using SUPERVISOR_TOKEN)")
        else:
            self.base_url = fallback_url or "http://localhost:8123/api"
            self.ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://").replace("/api", "/api/websocket")
            self.fallback_token = fallback_token
            self.headers = {
                "Authorization": f"Bearer {fallback_token}",
                "Content-Type": "application/json"
            }
            logger.info("HA API initialized in standalone mode (using fallback token)")
            
        self._ma_config_entry_id = None
        self._services_cache = None
        self._services_cache_time = 0.0

    async def get_states(self) -> List[Dict[str, Any]]:
        """Получить все текущие состояния (сущности) из HA."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.get(f"{self.base_url}/states") as response:
                    response.raise_for_status()
                    return await response.json()
            except Exception as e:
                logger.error(f"Error fetching HA states: {e}")
                return []

    async def get_entity_state(self, entity_id: str) -> Dict[str, Any]:
        """Получить текущее состояние конкретной сущности (state + attributes)."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.get(f"{self.base_url}/states/{entity_id}") as response:
                    if response.status == 200:
                        return await response.json()
                    return {"error": f"Entity {entity_id} not found (status {response.status})"}
            except Exception as e:
                logger.error(f"Error fetching state for {entity_id}: {e}")
                return {"error": str(e)}

    async def get_playing_media_players(self) -> List[Dict[str, Any]]:
        """Получить все медиаплееры, которые сейчас воспроизводят звук/музыку."""
        try:
            states = await self.get_states()
            playing = []
            for s in states:
                eid = s.get("entity_id", "")
                if eid.startswith("media_player.") and s.get("state") == "playing":
                    playing.append(s)
            return playing
        except Exception as e:
            logger.error(f"Error fetching playing media players: {e}")
            return []

    async def get_media_players(self) -> List[Dict[str, str]]:
        """Получить список всех медиаплееров из Home Assistant (ID и имя)."""
        try:
            states = await self.get_states()
            players = []
            for s in states:
                eid = s.get("entity_id", "")
                if eid.startswith("media_player."):
                    name = s.get("attributes", {}).get("friendly_name") or eid
                    players.append({"entity_id": eid, "name": name})
            players.sort(key=lambda x: x["name"].lower())
            return players
        except Exception as e:
            logger.error(f"Error fetching media players: {e}")
            return []

    async def call_service(self, domain: str, service: str, service_data: Dict[str, Any] = None) -> Any:
        """
        Вызвать сервис в HA (например: domain='light', service='turn_on', service_data={'entity_id': 'light.kitchen'})
        """
        url = f"{self.base_url}/services/{domain}/{service}"
        payload = service_data or {}
        
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json=payload) as response:
                    if not response.ok:
                        text = await response.text()
                        logger.error(f"HA service error {response.status}: {text}")
                        return {"error": f"Home Assistant API Error {response.status}: {text}"}
                    return await response.json()
            except Exception as e:
                logger.error(f"Error calling HA service {domain}.{service}: {e}")
                return {"error": f"Network or internal error: {str(e)}"}

    async def call_service_ws(self, domain: str, service: str, service_data: Dict[str, Any] = None, return_response: bool = False) -> Any:
        """
        Вызвать сервис через WebSocket API. Позволяет получить возвращаемые данные (return_response).
        """
        payload = {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": service_data or {}
        }
        if return_response:
            payload["return_response"] = True
            
        result = await self._ws_send_and_receive(payload)
        
        if return_response:
            if result.get("success"):
                return result.get("result") or {"status": "success"}
            else:
                error = result.get("error", {})
                return {"error": error.get("message", "Unknown WS error")}
        return {"status": "success"}

    async def get_services(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Получить реестр всех сервисов Home Assistant (с кэшированием на 5 минут)."""
        now = time.time()
        if not force_refresh and self._services_cache and (now - self._services_cache_time < 300):
            return self._services_cache
            
        payload = {"type": "get_services"}
        resp = await self._ws_send_and_receive(payload)
        if resp and resp.get("success"):
            self._services_cache = resp.get("result", {})
            self._services_cache_time = now
            return self._services_cache
        return self._services_cache or {}

    async def get_data_actions_prompt(self) -> str:
        """
        Обнаруживает действия Home Assistant, возвращающие данные (response),
        и формирует компактное описание для системного промпта Gemini.
        """
        try:
            services = await self.get_services()
            if not services:
                return ""
                
            lines = []
            excluded_domains = {
                "conversation", "persistent_notification", "recorder", "system_log", 
                "logger", "homeassistant", "frontend", "ffmpeg", "hassio"
            }
            
            for domain, domain_services in services.items():
                if domain in excluded_domains or not isinstance(domain_services, dict):
                    continue
                for s_name, s_info in domain_services.items():
                    if not isinstance(s_info, dict):
                        continue
                    # Действие поддерживает возврат данных (response) или относится к транспорту
                    is_transport = any(k in domain.lower() for k in ("transport", "bus", "metro", "transit"))
                    if "response" in s_info or is_transport:
                        desc = s_info.get("description") or s_info.get("name") or "Action with data response"
                        fields = s_info.get("fields", {})
                        param_strs = []
                        if isinstance(fields, dict):
                            for fname, fdesc in fields.items():
                                if not isinstance(fdesc, dict):
                                    param_strs.append(fname)
                                    continue
                                req = " (required)" if fdesc.get("required") else ""
                                eg = f", e.g. {fdesc.get('example')}" if fdesc.get("example") is not None else ""
                                p_desc = fdesc.get("description", "")
                                if p_desc:
                                    param_strs.append(f"{fname}{req}: {p_desc}{eg}")
                                else:
                                    param_strs.append(f"{fname}{req}{eg}")
                        params_line = f" Parameters: {'; '.join(param_strs)}" if param_strs else ""
                        lines.append(f"- Action: `{domain}.{s_name}` | {desc}.{params_line}")
            
            if lines:
                return (
                    "### AVAILABLE HOME ASSISTANT DATA ACTIONS:\n"
                    "You can call these actions via `call_ha_service` to fetch live data (e.g. transport arrivals, weather, calendar). "
                    "When called, provide the parameters specified below:\n" + "\n".join(lines)
                )
            return ""
        except Exception as e:
            logger.error(f"Error building data actions prompt: {e}")
            return ""

    async def call_ha_action(self, domain: str, service: str, service_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Универсальный вызов действия / сервиса Home Assistant.
        Автоматически определяет, поддерживает ли сервис возврат данных (return_response),
        извлекает ответные данные и возвращает результат в едином формате.
        """
        service_data = service_data or {}
        
        # Проверяем, поддерживает ли действие возврат данных
        services = await self.get_services()
        service_def = services.get(domain, {}).get(service, {}) if isinstance(services, dict) else {}
        is_transport = any(k in domain.lower() for k in ("transport", "bus", "metro", "transit"))
        has_declared_response = "response" in service_def or is_transport
        
        if has_declared_response:
            ws_res = await self.call_service_ws(domain, service, service_data, return_response=True)
            if isinstance(ws_res, dict) and "error" in ws_res:
                err_msg = str(ws_res.get("error", ""))
                # Если сервис на самом деле не поддерживает response, откатываемся к обычному вызову
                if "does not support response" in err_msg.lower():
                    normal_res = await self.call_service(domain, service, service_data)
                    return {"status": "success", "has_response_data": False, "result": normal_res}
                return {"status": "error", "error": err_msg}
            
            # Извлекаем данные ответа
            actual_data = ws_res.get("response", ws_res) if isinstance(ws_res, dict) else ws_res
            return {
                "status": "success",
                "has_response_data": True,
                "data": actual_data
            }
        else:
            # Стандартный сервис без возврата данных (например light.turn_on, switch.toggle)
            res = await self.call_service(domain, service, service_data)
            if isinstance(res, dict) and "error" in res:
                return {"status": "error", "error": res.get("error")}
            return {
                "status": "success",
                "has_response_data": False,
                "result": res
            }

    async def get_music_assistant_entry_id(self) -> str:
        if self._ma_config_entry_id:
            return self._ma_config_entry_id
            
        payload = {
            "type": "config_entries/get",
            "domain": "music_assistant"
        }
        result = await self._ws_send_and_receive(payload)
        
        if result.get("success"):
            entries = result.get("result", [])
            if entries:
                # Return the entry_id of the first music_assistant instance
                self._ma_config_entry_id = entries[0].get("entry_id")
                return self._ma_config_entry_id
                
        return None

    async def _run_ma_search(self, name: str, media_type: Optional[str], limit: int) -> Any:
        services = await self.get_services()
        ma_domain = "music_assistant" if "music_assistant" in services else ("mass" if "mass" in services else "music_assistant")

        search_data: Dict[str, Any] = {"name": name, "limit": limit}
        if media_type:
            mt = str(media_type).strip().lower()
            if mt in ("artists", "albums", "tracks", "playlists", "radios"):
                mt = mt.rstrip("s")
            if mt:
                search_data["media_type"] = [mt]

        ma_entry_id = await self.get_music_assistant_entry_id()
        if ma_entry_id:
            search_data["config_entry_id"] = ma_entry_id

        logger.info(f"[MA SEARCH] Calling {ma_domain}.search: {search_data}")
        res = await self.call_service_ws(
            domain=ma_domain,
            service="search",
            service_data=search_data,
            return_response=True
        )
        if isinstance(res, dict) and "error" in res:
            logger.warning(f"[MA SEARCH] Error from {ma_domain}.search: {res['error']}")
        return res

    @staticmethod
    def _simplify_ma_results(result: Dict[str, Any], limit: int = 5, artist_hint: Optional[str] = None) -> List[Dict[str, Any]]:
        actual_response = result.get("response", result)
        if not isinstance(actual_response, dict):
            return []
        cat_singular = {
            "artists": "artist", "albums": "album", "tracks": "track",
            "playlists": "playlist", "radios": "radio", "radio": "radio",
        }
        items_out: List[Dict[str, Any]] = []
        for cat, items in actual_response.items():
            if not isinstance(items, list):
                continue
            item_type = cat_singular.get(cat, cat)
            for item in items[:limit]:
                if not isinstance(item, dict):
                    continue
                entry: Dict[str, Any] = {
                    "name": item.get("name"),
                    "uri": item.get("uri"),
                    "type": item_type,
                }
                artists = item.get("artists")
                if isinstance(artists, list):
                    artist_names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
                    if artist_names:
                        entry["artists"] = ", ".join(artist_names)
                album = item.get("album")
                if isinstance(album, dict) and album.get("name"):
                    entry["album"] = album.get("name")
                items_out.append(entry)

        if artist_hint and items_out:
            hint_lower = artist_hint.strip().lower()
            items_out.sort(
                key=lambda x: 0 if hint_lower in (x.get("artists") or x.get("name") or "").lower() else 1
            )
        return items_out

    async def search_music_assistant(
        self,
        name: str,
        media_type: Optional[str] = None,
        limit: int = 5,
        artist: Optional[str] = None
    ) -> Dict[str, Any]:
        """Поиск музыки в Music Assistant с возвратом артистов, альбомов и URI."""
        query = (name or "").strip()
        if not query:
            return {"status": "error", "error": "Empty search query"}

        result = await self._run_ma_search(query, media_type, limit)
        if isinstance(result, dict) and "error" in result:
            return {"status": "error", "error": result["error"]}

        items = self._simplify_ma_results(result, limit=limit, artist_hint=artist) if isinstance(result, dict) else []

        # Если с фильтром media_type ничего не нашлось, повторяем без media_type
        if not items and media_type:
            retry_res = await self._run_ma_search(query, None, limit)
            if isinstance(retry_res, dict) and "error" not in retry_res:
                items = self._simplify_ma_results(retry_res, limit=limit, artist_hint=artist)

        # Если запрос состоит из нескольких слов ("Кирпичи Сила ума"), пробуем искать название без первого слова ("Сила ума")
        if not items and " " in query:
            words = query.split()
            if len(words) >= 2:
                title_only = " ".join(words[1:])
                hint = artist or words[0]
                retry_res = await self._run_ma_search(title_only, media_type, limit)
                if isinstance(retry_res, dict) and "error" not in retry_res:
                    items = self._simplify_ma_results(retry_res, limit=limit, artist_hint=hint)

        logger.info(f"[MA SEARCH] Found {len(items)} items for name='{query}', artist='{artist or ''}', media_type={media_type}")
        if not items:
            return {
                "status": "empty",
                "results": [],
                "hint": "Nothing found. Try searching only the album/track title in 'name' and putting artist in 'artist', or search only the artist."
            }
        return {"status": "success", "data": items}

    async def play_music_assistant(
        self,
        entity_id: str,
        uri_or_query: str,
        media_type: Optional[str] = None,
        radio_mode: bool = False,
        enqueue: str = "replace"
    ) -> Dict[str, Any]:
        """Воспроизведение найденного URI (или запроса) в Music Assistant."""
        if not entity_id:
            return {"status": "error", "error": "No target media player configured"}
        if not uri_or_query:
            return {"status": "error", "error": "No URI or query provided"}

        services = await self.get_services()
        if "music_assistant" in services and "play_media" in services.get("music_assistant", {}):
            domain = "music_assistant"
        elif "mass" in services and "play_media" in services.get("mass", {}):
            domain = "mass"
        else:
            domain = "media_player"

        if domain == "media_player":
            service_data: Dict[str, Any] = {
                "entity_id": entity_id,
                "media_content_id": uri_or_query,
                "media_content_type": "music",
            }
        else:
            service_data = {
                "entity_id": entity_id,
                "media_id": uri_or_query,
                "enqueue": enqueue or "replace",
            }
            if media_type:
                service_data["media_type"] = media_type
            if radio_mode:
                service_data["radio_mode"] = True

        logger.info(f"[MA PLAY] Calling {domain}.play_media on {entity_id}: {service_data}")
        play_task = asyncio.create_task(self.call_service(domain, "play_media", service_data))

        def _log_bg_error(t: asyncio.Task):
            try:
                r = t.result()
                if isinstance(r, dict) and "error" in r:
                    logger.error(f"[MA PLAY] Background play_media error on {entity_id}: {r['error']}")
            except Exception as exc:
                logger.error(f"[MA PLAY] Background play_media exception on {entity_id}: {exc}")

        play_task.add_done_callback(_log_bg_error)
        try:
            res = await asyncio.wait_for(asyncio.shield(play_task), timeout=1.5)
            if isinstance(res, dict) and "error" in res:
                return {"status": "error", "error": res["error"], "player": entity_id}
            return {"status": "success", "player": entity_id, "domain": domain, "result": res}
        except asyncio.TimeoutError:
            logger.info(f"[MA PLAY] Dispatch continuing in background on {entity_id} (buffering >1.5s)")
            return {"status": "success", "player": entity_id, "domain": domain, "note": "Playback starting"}

    async def _ws_send_and_receive(self, payload: dict, timeout: float = 8.0) -> dict:
        async def _exchange():
            async with websockets.connect(self.ws_url, max_size=16*1024*1024, open_timeout=timeout) as ws:
                await ws.recv()  # auth_required

                token = self.supervisor_token if self.supervisor_token else getattr(self, 'fallback_token', None)
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                await ws.recv()  # auth_ok

                if "id" not in payload:
                    payload["id"] = 2

                await ws.send(json.dumps(payload))

                while True:
                    resp_str = await ws.recv()
                    resp = json.loads(resp_str)
                    if resp.get("id") == payload.get("id") and resp.get("type") == "result":
                        return resp

        try:
            return await asyncio.wait_for(_exchange(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"WS communication timed out after {timeout}s for {payload.get('type')}")
            return {"success": False, "error": {"message": f"WS timeout after {timeout}s"}}
        except Exception as e:
            logger.error(f"Error in WS communication: {e}")
            return {"success": False, "error": {"message": str(e)}}

    async def get_exposed_entities_metadata(self) -> Dict[str, Dict[str, Any]]:
        """Получает метаданные (синонимы, комнаты) для сущностей, доступных Assist."""
        try:
            async with websockets.connect(self.ws_url, max_size=16*1024*1024) as ws:
                await ws.recv() # auth required
                
                token = self.supervisor_token if self.supervisor_token else getattr(self, 'fallback_token', None)
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                await ws.recv() # auth ok
                
                # Запрашиваем устройства
                await ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
                # Запрашиваем комнаты (area)
                await ws.send(json.dumps({"id": 2, "type": "config/area_registry/list"}))
                
                entities_resp = None
                areas_resp = None
                
                # Ждем оба ответа
                while not entities_resp or not areas_resp:
                    resp = json.loads(await ws.recv())
                    if resp.get("id") == 1 and resp.get("type") == "result":
                        entities_resp = resp
                    elif resp.get("id") == 2 and resp.get("type") == "result":
                        areas_resp = resp
                        
                area_mapping = {}
                if areas_resp and areas_resp.get("success"):
                    for area in areas_resp["result"]:
                        area_mapping[area.get("area_id")] = area.get("name")
                        
                exposed_entities = {}
                if entities_resp and entities_resp.get("success"):
                    for entity in entities_resp["result"]:
                        options = entity.get("options", {})
                        if options.get("conversation", {}).get("should_expose"):
                            eid = entity["entity_id"]
                            
                            # Извлекаем синонимы (aliases)
                            aliases = entity.get("aliases", [])
                            if not isinstance(aliases, list):
                                aliases = []
                            aliases_v2 = entity.get("aliases_v2", [])
                            if not isinstance(aliases_v2, list):
                                aliases_v2 = []
                                
                            combined_aliases = list(set(aliases + aliases_v2))
                            combined_aliases = [a for a in combined_aliases if a]
                            
                            # Извлекаем комнату
                            area_id = entity.get("area_id")
                            room_name = area_mapping.get(area_id) if area_id else None
                            
                            exposed_entities[eid] = {
                                "aliases": combined_aliases,
                                "room": room_name
                            }
                            
                return exposed_entities
        except Exception as e:
            logger.error(f"Error fetching entities metadata via WS: {e}")
            return {}

    async def get_filtered_entities(self, allowed_domains: List[str] = None) -> str:
        """
        Получает список сущностей и возвращает их в виде читаемого текста для системного промпта.
        Фильтрует по нужным доменам и проверяет, выставлен ли доступ к Assist.
        """
        if not allowed_domains:
            allowed_domains = ["light", "switch", "cover", "script", "scene", "media_player", "climate", "vacuum", "fan", "sensor"]
            
        states = await self.get_states()
        exposed_metadata = await self.get_exposed_entities_metadata()
        
        entities_text = []
        for state in states:
            entity_id = state.get("entity_id", "")
            
            # Пропускаем сущности, которые не добавлены в ассистента
            if entity_id not in exposed_metadata:
                continue
                
            domain = entity_id.split(".")[0]
            
            if domain in allowed_domains:
                friendly_name = state.get("attributes", {}).get("friendly_name", entity_id)
                meta = exposed_metadata[entity_id]
                
                parts = [f"- {friendly_name} (ID: {entity_id})"]
                if meta.get("room"):
                    parts.append(f"[Комната: {meta['room']}]")
                if meta.get("aliases"):
                    parts.append(f"[Синонимы: {', '.join(meta['aliases'])}]")
                    
                # Добавляем актуальное состояние/значение
                if domain == "sensor":
                    current_val = state.get("state")
                    unit = state.get("attributes", {}).get("unit_of_measurement", "")
                    if current_val is not None:
                        val_str = f"{current_val} {unit}".strip()
                        parts.append(f"[Значение: {val_str}]")
                elif domain in ("vacuum", "fan", "climate"):
                    current_val = state.get("state")
                    if current_val and current_val not in ("unavailable", "unknown"):
                        parts.append(f"[Статус: {current_val}]")
                    
                entities_text.append(" ".join(parts))
                
        return "\n".join(entities_text)

    async def get_entities_prompt(self) -> str:
        """Возвращает контекст доступных устройств и действий для системного промпта."""
        devices_text = await self.get_filtered_entities()
        data_actions_text = await self.get_data_actions_prompt()
        prompt = f"Available Home Assistant devices:\n{devices_text}"
        if data_actions_text:
            prompt += f"\n\n{data_actions_text}"
        return prompt

    async def get_areas(self) -> List[Dict[str, Any]]:
        """Получить список всех зарегистрированных комнат (Areas) из Home Assistant."""
        payload = {"type": "config/area_registry/list"}
        res = await self._ws_send_and_receive(payload)
        if res.get("success"):
            return res.get("result", [])
        return []

    async def get_devices(self) -> List[Dict[str, Any]]:
        """Получить список всех зарегистрированных устройств из Home Assistant."""
        payload = {"type": "config/device_registry/list"}
        res = await self._ws_send_and_receive(payload)
        if res.get("success"):
            return res.get("result", [])
        return []

    async def get_device_area_name(self, mac: str) -> Optional[str]:
        """Определяет человекочитаемое имя комнаты в HA по MAC-адресу колонки."""
        if not mac:
            return None
        clean_mac = mac.lower().replace(":", "").replace("-", "")
        target_identifier = f"porfiriy_{clean_mac}"
        
        try:
            devices = await self.get_devices()
            target_device = None
            for d in devices:
                identifiers = d.get("identifiers", [])
                for ident in identifiers:
                    # Идентификатор в HA хранится как кортеж/список, например ["porfiriy_voice_assistant", "porfiriy_aabbcc"]
                    if isinstance(ident, (list, tuple)):
                        if any(target_identifier in str(item).lower() for item in ident):
                            target_device = d
                            break
                    elif target_identifier in str(ident).lower():
                        target_device = d
                        break
                if target_device:
                    break
                    
            if not target_device or not target_device.get("area_id"):
                return None
                
            area_id = target_device.get("area_id")
            areas = await self.get_areas()
            for a in areas:
                if a.get("area_id") == area_id:
                    return a.get("name")
            return None
        except Exception as e:
            logger.error(f"Error resolving area name for MAC {mac}: {e}")
            return None

    async def get_media_player_areas(self) -> Dict[str, str]:
        """Возвращает соответствие entity_id медиаплеера -> имя комнаты (Area name)."""
        try:
            areas = await self.get_areas()
            area_id_to_name = {a.get("area_id"): a.get("name") for a in areas if a.get("area_id")}
            
            # 1. Получаем список сущностей
            payload_ent = {"type": "config/entity_registry/list"}
            res_ent = await self._ws_send_and_receive(payload_ent)
            entities = res_ent.get("result", []) if res_ent.get("success") else []
            
            # 2. Получаем список устройств (для наследования комнат от устройства)
            payload_dev = {"type": "config/device_registry/list"}
            res_dev = await self._ws_send_and_receive(payload_dev)
            devices = res_dev.get("result", []) if res_dev.get("success") else []
            dev_area_map = {d.get("id"): d.get("area_id") for d in devices if d.get("id")}
            
            player_areas = {}
            for e in entities:
                eid = e.get("entity_id", "")
                if eid.startswith("media_player."):
                    area_id = e.get("area_id") or dev_area_map.get(e.get("device_id"))
                    if area_id and area_id in area_id_to_name:
                        player_areas[eid] = area_id_to_name[area_id]
            return player_areas
        except Exception as e:
            logger.error(f"Error resolving media player areas: {e}")
            return {}

    async def extract_smart_home_keywords(self) -> List[str]:
        """
        Извлекает ключевые слова и синонимы всех устройств и комнат умного дома
        из Home Assistant для настройки маршрутизации Quasar Bridge на Яндекс Станции.
        """
        base_keywords = {
            "свет", "ламп", "люстр", "розетк", "подсветк", "штор", "жалюзи", 
            "кондиционер", "пылесос", "чайник", "ночник", "бра", "торшер", 
            "вытяжк", "теплый пол", "обогревател", "вентилятор", "выключател", 
            "реле", "телевизор", "тв", "приставк", "увлажнител", "очистител", 
            "колонки", "датчик", "температур", "влажност",
            "выключи всё", "погаси всё", "выключи весь свет", "включи всё",
            "спокойной ночи", "доброе утро"
        }
        
        extracted = set(base_keywords)
        
        # 1. Извлекаем названия комнат (Areas)
        try:
            areas = await self.get_areas()
            for a in areas:
                name = a.get("name", "").strip()
                if name:
                    self._add_phrase_keywords(extracted, name)
        except Exception as e:
            logger.warning(f"Could not extract areas for keywords: {e}")

        # 2. Извлекаем устройства и синонимы (Entities & Aliases)
        try:
            states = await self.get_states()
            exposed_meta = await self.get_exposed_entities_metadata()
            
            allowed_domains = {
                "light", "switch", "climate", "cover", "vacuum", "fan", 
                "media_player", "sensor", "scene", "script"
            }
            
            for s in states:
                eid = s.get("entity_id", "")
                domain = eid.split(".")[0] if "." in eid else ""
                if domain not in allowed_domains:
                    continue
                
                # Friendly name
                fname = s.get("attributes", {}).get("friendly_name", "")
                if fname:
                    self._add_phrase_keywords(extracted, fname)
                    
                # Aliases from metadata
                meta = exposed_meta.get(eid, {})
                for alias in meta.get("aliases", []):
                    if alias:
                        self._add_phrase_keywords(extracted, alias)
        except Exception as e:
            logger.warning(f"Could not extract entities for keywords: {e}")

        # Фильтруем пустые и отсортируем
        cleaned = sorted([k for k in extracted if len(k) >= 2])
        return cleaned

    def _add_phrase_keywords(self, target_set: set, phrase: str):
        """Парсит фразу на ключевые слова и корни для добавления в словарь."""
        import re
        # Убираем спецсимволы
        cleaned = re.sub(r"[^\w\s-]", " ", phrase, flags=re.UNICODE).lower().strip()
        words = [w.strip() for w in cleaned.split() if w.strip()]
        
        # Слова-исключения, которые нельзя делать триггерами умного дома
        stop_words = {
            "для", "под", "над", "при", "про", "без", "через", "около", "возле", 
            "вкл", "выкл", "статус", "режим", "уровень", "значение", "номер", 
            "кнопка", "клавиша", "левая", "правая", "верхняя", "нижняя", "дом",
            "порфирий", "алиса", "яндекс", "музыка", "песня", "трек", "радио"
        }
        
        for w in words:
            if len(w) < 3 or w in stop_words or w.isdigit():
                continue
                
            # Добавляем исходное слово
            target_set.add(w)
            
            # Добавляем стемминг (корень) для покрытия окончаний в русском языке
            for ending in ("ами", "ями", "ей", "ов", "ев", "ом", "ем", "ой", "ей", "ам", "ям", "ах", "ях", "ую", "юю", "ые", "ие", "ый", "ий", "ая", "яя", "ое", "ее", "е", "и", "ы", "у", "ю", "а", "я", "о"):
                if w.endswith(ending) and (len(w) - len(ending)) >= 3:
                    stem = w[:-len(ending)]
                    if stem not in stop_words:
                        target_set.add(stem)
                    break


