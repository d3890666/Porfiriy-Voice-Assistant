import os
import asyncio
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

class GeminiProxyClient:
    def __init__(self, api_key: str, system_prompt: str, ha_api):
        """
        Инициализация клиента Gemini Live API с инструментами управления Home Assistant.
        """
        if not api_key:
            logger.warning("Gemini API key is empty! Connection will likely fail.")
            
        self.client = genai.Client(
            http_options={"api_version": "v1beta"},
            api_key=api_key
        )
        self.ha_api = ha_api
        self.model = "models/gemini-2.0-flash-exp" # Используем актуальную модель для Live API
        self.system_prompt = system_prompt

    def _get_config(self) -> types.LiveConnectConfig:
        """Настройка конфигурации сессии (Промпт, Голос, Инструменты)."""
        ha_tool = types.FunctionDeclaration(
            name="call_ha_service",
            description="Call a Home Assistant service to control a smart home device or execute a script.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "domain": types.Schema(type="STRING", description="The domain of the service, e.g. light, switch, script, scene, climate"),
                    "service": types.Schema(type="STRING", description="The service to call, e.g. turn_on, turn_off, toggle"),
                    "entity_id": types.Schema(type="STRING", description="The exact entity_id of the device from the provided context, e.g. light.kitchen"),
                },
                required=["domain", "service", "entity_id"]
            )
        )
        tool = types.Tool(function_declarations=[ha_tool])
        
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(parts=[types.Part.from_text(self.system_prompt)]),
            tools=[tool],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Zephyr")
                )
            ),
        )

    def connect(self):
        """Возвращает асинхронный контекстный менеджер сессии."""
        return self.client.aio.live.connect(model=self.model, config=self._get_config())
