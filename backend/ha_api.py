import os
import aiohttp
import websockets
import json
import logging
from typing import List, Dict, Any

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

    async def _ws_send_and_receive(self, payload: dict) -> dict:
        try:
            async with websockets.connect(self.ws_url) as ws:
                await ws.recv() # auth_required
                
                token = self.supervisor_token if self.supervisor_token else getattr(self, 'fallback_token', None)
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                await ws.recv() # auth_ok
                
                if "id" not in payload:
                    payload["id"] = 2
                    
                await ws.send(json.dumps(payload))
                
                while True:
                    resp_str = await ws.recv()
                    resp = json.loads(resp_str)
                    if resp.get("id") == payload.get("id") and resp.get("type") == "result":
                        return resp
        except Exception as e:
            logger.error(f"Error in WS communication: {e}")
            return {"success": False, "error": {"message": str(e)}}

    async def get_exposed_entities_metadata(self) -> Dict[str, Dict[str, Any]]:
        """Получает метаданные (синонимы, комнаты) для сущностей, доступных Assist."""
        try:
            async with websockets.connect(self.ws_url) as ws:
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
            allowed_domains = ["light", "switch", "script", "scene", "media_player", "climate"]
            
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
                if meta["room"]:
                    parts.append(f"[Комната: {meta['room']}]")
                if meta["aliases"]:
                    parts.append(f"[Синонимы: {', '.join(meta['aliases'])}]")
                    
                entities_text.append(" ".join(parts))
                
        return "\n".join(entities_text)
