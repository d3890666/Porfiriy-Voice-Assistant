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

    async def call_service(self, domain: str, service: str, service_data: Dict[str, Any] = None) -> Any:
        """
        Вызвать сервис в HA (например: domain='light', service='turn_on', service_data={'entity_id': 'light.kitchen'})
        """
        url = f"{self.base_url}/services/{domain}/{service}"
        payload = service_data or {}
        
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json=payload) as response:
                    response.raise_for_status()
                    return await response.json()
            except Exception as e:
                logger.error(f"Error calling HA service {domain}.{service}: {e}")
                return {"error": str(e)}

    async def get_exposed_entity_ids(self) -> set:
        """Получает список entity_id, которым разрешен доступ к Assist (conversation)."""
        try:
            async with websockets.connect(self.ws_url) as ws:
                auth_msg = await ws.recv()
                
                token = self.supervisor_token if self.supervisor_token else getattr(self, 'fallback_token', None)
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                auth_ok = await ws.recv()
                
                await ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
                
                # Читаем ответы, пока не получим результат нашего запроса (id: 1)
                while True:
                    resp = json.loads(await ws.recv())
                    if resp.get("id") == 1 and resp.get("type") == "result":
                        break
                        
                if not resp.get("success"):
                    return set()
                    
                exposed_ids = set()
                for entity in resp["result"]:
                    options = entity.get("options", {})
                    # Проверяем доступность для conversation (Assist)
                    if options.get("conversation", {}).get("should_expose"):
                        exposed_ids.add(entity["entity_id"])
                        
                return exposed_ids
        except Exception as e:
            logger.error(f"Error fetching exposed entities via WS: {e}")
            return set()

    async def get_filtered_entities(self, allowed_domains: List[str] = None) -> str:
        """
        Получает список сущностей и возвращает их в виде читаемого текста для системного промпта.
        Фильтрует по нужным доменам и проверяет, выставлен ли доступ к Assist.
        """
        if not allowed_domains:
            allowed_domains = ["light", "switch", "script", "scene", "media_player", "climate"]
            
        states = await self.get_states()
        exposed_ids = await self.get_exposed_entity_ids()
        
        entities_text = []
        for state in states:
            entity_id = state.get("entity_id", "")
            
            # Пропускаем сущности, которые не добавлены в ассистента
            if entity_id not in exposed_ids:
                continue
                
            domain = entity_id.split(".")[0]
            
            if domain in allowed_domains:
                friendly_name = state.get("attributes", {}).get("friendly_name", entity_id)
                entities_text.append(f"- {friendly_name} (ID: {entity_id})")
                
        return "\n".join(entities_text)
