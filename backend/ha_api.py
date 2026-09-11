import os
import aiohttp
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
            self.headers = {
                "Authorization": f"Bearer {self.supervisor_token}",
                "Content-Type": "application/json"
            }
            logger.info("HA API initialized in Add-on mode (using SUPERVISOR_TOKEN)")
        else:
            self.base_url = fallback_url or "http://localhost:8123/api"
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

    async def get_filtered_entities(self, allowed_domains: List[str] = None) -> str:
        """
        Получает список сущностей и возвращает их в виде читаемого текста для системного промпта.
        Фильтрует по нужным доменам (light, switch, script, и т.д.).
        """
        if not allowed_domains:
            allowed_domains = ["light", "switch", "script", "scene", "media_player", "climate"]
            
        states = await self.get_states()
        
        entities_text = []
        for state in states:
            entity_id = state.get("entity_id", "")
            domain = entity_id.split(".")[0]
            
            if domain in allowed_domains:
                friendly_name = state.get("attributes", {}).get("friendly_name", entity_id)
                entities_text.append(f"- {friendly_name} (ID: {entity_id})")
                
        return "\n".join(entities_text)
