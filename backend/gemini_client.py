import os
import asyncio
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

class GeminiProxyClient:
    def __init__(self, api_key: str, system_prompt: str, ha_api, voice_name: str = "Zephyr", model: str = "gemini-2.0-flash-exp", enable_google_search: bool = True, vad_silence_duration_ms: int = 600):
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
                type="OBJECT",
                properties={
                    "domain": types.Schema(type="STRING", description="The domain of the service, e.g. light, switch, script, scene, climate"),
                    "service": types.Schema(type="STRING", description="The service to call, e.g. turn_on, turn_off, toggle"),
                    "entity_id": types.Schema(type="STRING", description="The exact entity_id of the device from the provided context, e.g. light.kitchen"),
                },
                required=["domain", "service", "entity_id"]
            )
        )
        search_music_tool = types.FunctionDeclaration(
            name="search_music_assistant",
            description="Search for music (artists, albums, tracks, playlists) in Music Assistant. Returns a list of results with URIs. Use this to find the exact URI before playing.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "name": types.Schema(type="STRING", description="Search query (e.g. 'Madonna')"),
                    "media_type": types.Schema(type="STRING", description="Optional. Type to search: 'artist', 'album', 'track', 'playlist', 'radio'"),
                },
                required=["name"]
            )
        )
        
        play_music_tool = types.FunctionDeclaration(
            name="play_music_assistant",
            description="Play a music URI (obtained from search_music_assistant) on the smart speaker.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "uri": types.Schema(type="STRING", description="The URI of the media to play"),
                    "player": types.Schema(type="STRING", description="Optional. The media player entity_id. Leave empty to use default."),
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
