import os
import asyncio
import logging
from typing import List, Dict, Any
import aiohttp
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

DEFAULT_LIVE_MODELS: List[Dict[str, Any]] = [
    {"id": "models/gemini-3.1-flash-live-preview", "name": "Gemini 3.1 Flash Live Preview (Новейшая)", "is_live": True},
    {"id": "models/gemini-2.5-flash-native-audio-preview", "name": "Gemini 2.5 Flash Native Audio Preview", "is_live": True},
    {"id": "models/gemini-2.0-flash-exp", "name": "Gemini 2.0 Flash Experimental", "is_live": True},
    {"id": "gemini-2.0-flash-exp", "name": "Gemini 2.0 Flash Exp (Короткий ID)", "is_live": True},
]

async def fetch_available_gemini_models(api_key: str) -> List[Dict[str, Any]]:
    """
    Получает список доступных моделей Gemini из Google API.
    Сначала пробует официальный genai SDK, при сбое - прямой REST через aiohttp.
    Возвращает упорядоченный список словарей с метаданными и флагом is_live.
    """
    if not api_key:
        return list(DEFAULT_LIVE_MODELS)

    models_dict = {}
    
    # 1. Запрос через google.genai SDK
    try:
        client = genai.Client(
            http_options={"api_version": "v1alpha"},
            api_key=api_key
        )
        pager = await client.aio.models.list(config={"page_size": 100})
        async for m in pager:
            m_id = getattr(m, "name", "")
            if not m_id:
                continue
            display_name = getattr(m, "display_name", "") or m_id
            actions = getattr(m, "supported_actions", []) or []
            
            is_live = False
            if "bidiGenerateContent" in actions:
                is_live = True
            elif any(tag in m_id.lower() for tag in ["live", "native-audio", "flash-exp"]):
                is_live = True
                
            models_dict[m_id] = {
                "id": m_id,
                "name": display_name,
                "is_live": is_live
            }
    except Exception as e:
        logger.warning(f"Failed to fetch models via genai SDK: {e}. Trying REST fallback...")
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("models", []):
                            m_id = item.get("name", "")
                            if not m_id:
                                continue
                            display_name = item.get("displayName") or m_id
                            methods = item.get("supportedGenerationMethods", [])
                            is_live = ("bidiGenerateContent" in methods) or any(tag in m_id.lower() for tag in ["live", "native-audio", "flash-exp"])
                            models_dict[m_id] = {
                                "id": m_id,
                                "name": display_name,
                                "is_live": is_live
                            }
                    else:
                        logger.warning(f"REST models request returned status {resp.status}")
        except Exception as re:
            logger.warning(f"Failed to fetch models via REST: {re}")

    # Дополняем эталонными Live моделями, если их нет
    for dm in DEFAULT_LIVE_MODELS:
        if dm["id"] not in models_dict:
            models_dict[dm["id"]] = dm

    # Разделяем и сортируем: сначала live-модели, затем остальные
    live_models = [m for m in models_dict.values() if m.get("is_live")]
    other_models = [m for m in models_dict.values() if not m.get("is_live")]

    live_models.sort(key=lambda x: x["name"])
    other_models.sort(key=lambda x: x["name"])

    return live_models + other_models

class GeminiProxyClient:
    def __init__(self, api_key: str, system_prompt: str, ha_api, voice_name: str = "Zephyr", model: str = "gemini-2.0-flash-exp", enable_google_search: bool = True, vad_silence_duration_ms: int = 600):
        """
        Инициализация клиента Gemini Live API с инструментами управления Home Assistant.
        """
        if not api_key:
            logger.warning("Gemini API key is empty! Connection will likely fail.")
            
        self.client = genai.Client(
            http_options={"api_version": "v1alpha"},
            api_key=api_key
        )
        self.ha_api = ha_api
        
        # Если пользователь не указал префикс models/, добавляем его
        self.model = model if model.startswith("models/") else f"models/{model}"
        
        self.system_prompt = system_prompt
        self.voice_name = voice_name
        self.enable_google_search = enable_google_search
        self.vad_silence_duration_ms = vad_silence_duration_ms

    def _get_config(self) -> types.LiveConnectConfig:
        """Настройка конфигурации сессии (Промпт, Голос, Инструменты)."""
        ha_tool = types.FunctionDeclaration(
            name="call_ha_service",
            description="Call a Home Assistant service to control a smart home device or execute a script.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "domain": types.Schema(type=types.Type.STRING, description="The domain of the service, e.g. light, switch, cover, script, scene, climate, media_player"),
                    "service": types.Schema(type=types.Type.STRING, description="The service to call, e.g. turn_on, turn_off, toggle, open_cover, close_cover, stop_cover, set_cover_position"),
                    "entity_id": types.Schema(type=types.Type.STRING, description="The exact entity_id of the device from the provided context, e.g. light.kitchen, cover.living_room_curtains"),
                    "position": types.Schema(type=types.Type.INTEGER, description="Optional target position for cover (curtains/blinds) from 0 (closed) to 100 (open)."),
                    "temperature": types.Schema(type=types.Type.NUMBER, description="Optional target temperature for climate devices."),
                    "hvac_mode": types.Schema(type=types.Type.STRING, description="Optional HVAC mode for climate devices (e.g. heat, cool, off)."),
                },
                required=["domain", "service", "entity_id"]
            )
        )
        search_music_tool = types.FunctionDeclaration(
            name="search_music_assistant",
            description="Search for music (artists, albums, tracks, playlists) in Music Assistant. Returns a list of results with URIs. Use this to find the exact URI before playing.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "name": types.Schema(type=types.Type.STRING, description="Search query (e.g. 'Madonna')"),
                    "media_type": types.Schema(type=types.Type.STRING, description="Optional. Type to search: 'artist', 'album', 'track', 'playlist', 'radio'"),
                },
                required=["name"]
            )
        )
        
        play_music_tool = types.FunctionDeclaration(
            name="play_music_assistant",
            description="Play a music URI (obtained from search_music_assistant) on the smart speaker.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "uri": types.Schema(type=types.Type.STRING, description="The URI of the media to play"),
                    "player": types.Schema(type=types.Type.STRING, description="Optional. The media player entity_id. Leave empty to use default."),
                },
                required=["uri"]
            )
        )

        tool_args = {"function_declarations": [ha_tool, search_music_tool, play_music_tool]}
        if self.enable_google_search:
            tool_args["google_search"] = types.GoogleSearch()
            
        tool = types.Tool(**tool_args)
        tools = [tool]
            
        realtime_input_config = None
        if self.vad_silence_duration_ms:
            realtime_input_config = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    silence_duration_ms=self.vad_silence_duration_ms
                )
            )

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(parts=[types.Part.from_text(text=self.system_prompt)]),
            tools=tools,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice_name)
                )
            ),
            realtime_input_config=realtime_input_config
        )

    def connect(self):
        """Возвращает асинхронный контекстный менеджер сессии."""
        return self.client.aio.live.connect(model=self.model, config=self._get_config())
