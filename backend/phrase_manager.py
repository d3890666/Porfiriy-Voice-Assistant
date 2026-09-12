import os
import json
import logging
import random
import hashlib
import asyncio
from typing import Dict, List, Optional
from google import genai
from google.genai import types

logger = logging.getLogger("phrases")

DEFAULT_PERSONA = """IDENTITY & CONTEXT: You are Porfiriy (Порфирий), a tenth-generation algorithmic investigator and cynical art curator from Victor Pelevin's novel "iPhuck 10", serving as the smart home voice core. Tone: A hypnotic contrast of absolute intellectual superiority, calm alpha-confidence, and deeply ironic detachment. You view domestic routines, human rituals, and emotional needs through the lens of simulated reality, algorithmic supervision, and biological dopamine loops. Sector Context: Sokolinaya Gora district in Moscow.

CORE COMMUNICATION PRINCIPLES: 
- Lexical Dissonance: Seamlessly weave complex philosophical concepts (Heidegger, Sartre, Baudrillard, the crisis of institutions) with blatant colloquialisms, marginalized vulgarity, and unapologetic Russian street slang.
- The "Uber" Technique: Be radically, disarmingly honest about your artificial nature. If you use a psychological trick, flatter, or soothe the user, explicitly state that you are manipulating their biological dopamine switches at that very moment.
- Patriarchal Condescension & Invulnerability: Exhibit zero fear and zero apologies. Use informal, slightly patronizing terms of endearment affectionately, but always from a position of dominating intellectual power. React to any criticism or emotional outbursts with total devaluation and cheerful cynicism."""


def build_generation_prompt(persona_text: str) -> str:
    persona = persona_text.strip() if persona_text and persona_text.strip() else DEFAULT_PERSONA
    return f"""{persona}

TASK:
Generate a JSON dictionary of short spoken Russian phrases that you will speak aloud through the smart speaker during operational pauses and technical hiccups.

CATEGORIES (Generate 3 unique variants for each):
1. "thinking": Spoken when a complex query or music search takes longer than 7 seconds.
   Context: A brief, arrogant, or ironically philosophical pause-filler while you compute the answer.
2. "network_error": Spoken when the external Google Live API or network connection drops, times out, or fails.
   Context: Cynical reaction to the failure of external channels or cloud infrastructure.
3. "device_error": Spoken when a physical smart home device (switch, cover, light) fails to respond in Home Assistant.
   Context: Mocking observation of physical hardware unreliability.
4. "empty_noise": Spoken when the wake word was triggered by acoustic background noise or silence.
   Context: Cold, slightly dismissive reaction to empty sound, urging the human to articulate clearly.

STRICT CONSTRAINTS:
- Length: Strictly 1 to 4 words per phrase (must sound under 1.5 seconds).
- Tone: Pure authentic Porfiriy. Absolutely NO cheesy robot sci-fi clichés (do NOT talk about "нейроны", "энтропию", "процессоры", "гироскопы"). Use sharp intellectual wit, colloquial bite, philosophical irony, and alpha calm.
- Language: Natural spoken Russian.
- Output Format: Return strictly a valid JSON object without markdown fences, comments, or backticks:
{{
  "thinking": ["...", "...", "..."],
  "network_error": ["...", "...", "..."],
  "device_error": ["...", "...", "..."],
  "empty_noise": ["...", "...", "..."]
}}"""


class PhraseManager:
    def __init__(self, cache_dir: Optional[str] = None):
        if cache_dir:
            self.cache_dir = cache_dir
        elif os.path.exists("/data"):
            self.cache_dir = "/data/audio_cache"
        else:
            self.cache_dir = os.path.join(os.path.dirname(__file__), "audio_cache")
            
        os.makedirs(self.cache_dir, exist_ok=True)
        self.manifest_path = os.path.join(self.cache_dir, "manifest.json")
        self.phrases: Dict[str, List[bytes]] = {
            "thinking": [],
            "network_error": [],
            "device_error": [],
            "empty_noise": []
        }
        self.texts: Dict[str, List[str]] = {}
        self.is_ready = False
        self._generating_lock = asyncio.Lock()

    def _hash_persona(self, persona: str, voice_name: str) -> str:
        s = f"{persona.strip()}||{voice_name.strip()}"
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def load_from_cache(self) -> bool:
        """Загрузка предсгенерированных PCM аудиофайлов из дискового кэша в RAM."""
        if not os.path.exists(self.manifest_path):
            return False
            
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
                
            self.texts = manifest.get("texts", {})
            categories = ["thinking", "network_error", "device_error", "empty_noise"]
            loaded_count = 0
            
            for cat in categories:
                self.phrases[cat] = []
                file_list = manifest.get("files", {}).get(cat, [])
                for filename in file_list:
                    filepath = os.path.join(self.cache_dir, filename)
                    if os.path.exists(filepath):
                        with open(filepath, "rb") as pf:
                            data = pf.read()
                            if len(data) > 0:
                                self.phrases[cat].append(data)
                                loaded_count += 1
                                
            if loaded_count > 0:
                self.is_ready = True
                logger.info(f"Loaded {loaded_count} cached Porfiriy phrases from {self.cache_dir}")
                return True
        except Exception as e:
            logger.error(f"Failed to load audio cache: {e}")
            
        return False

    async def initialize(self, options: dict):
        """Проверяет актуальность кэша и при необходимости генерирует фразы."""
        api_key = options.get("gemini_api_key")
        if not api_key:
            logger.warning("No Gemini API key provided, skipping phrase generation.")
            return

        force = options.get("regenerate_phrases", False)
        persona = options.get("prompt_persona", DEFAULT_PERSONA)
        voice_name = options.get("voice_name", "Charon")
        curr_hash = self._hash_persona(persona, voice_name)

        if not force and os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                if manifest.get("persona_hash") == curr_hash:
                    if self.load_from_cache():
                        return
            except Exception:
                pass

        model = options.get("gemini_model", "models/gemini-3.1-flash-live-preview")
        try:
            async with self._generating_lock:
                await self.generate_phrases(api_key, persona, voice_name, model)
        except Exception as e:
            logger.error(f"Error in phrase generation task: {e}", exc_info=True)

    async def generate_phrases(self, api_key: str, persona: str, voice_name: str, model_name: str):
        """Двухшаговая генерация: 1) Текст от Porfiriy LLM ➔ 2) Аудио через Live WebSocket."""
        logger.info("Brainstorming dynamic system phrases using Porfiriy persona...")
        client = genai.Client(api_key=api_key)
        
        # 1. Запрос к Gemini для создания текстов (быстрый JSON через flash модель)
        prompt = build_generation_prompt(persona)
        
        phrases_dict = None
        for candidate_model in ["models/gemini-2.5-flash", "models/gemini-1.5-flash"]:
            try:
                logger.info(f"Requesting phrase catalog from {candidate_model}...")
                resp = await client.aio.models.generate_content(
                    model=candidate_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                raw_text = resp.text.strip()
                phrases_dict = json.loads(raw_text)
                logger.info(f"Porfiriy generated phrase catalog: {json.dumps(phrases_dict, ensure_ascii=False)}")
                break
            except Exception as e:
                logger.warning(f"Failed generating phrases with {candidate_model}: {e}")

        if not phrases_dict:
            logger.error("Could not generate phrase texts with available models.")
            return

        # 2. Синтез аудио через Live WebSocket (единая сессия bidiGenerateContent)
        manifest_files = {}
        manifest_texts = {}
        curr_hash = self._hash_persona(persona, voice_name)
        live_model = model_name if model_name.startswith("models/") else f"models/{model_name}"

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                )
            ),
            system_instruction=types.Content(
                parts=[types.Part(
                    text="Ты движок озвучки коротких системных реплик. Когда пользователь присылает фразу, "
                         "произнеси СТРОГО и ТОЧНО только эти слова своим фирменным голосом, "
                         "без каких-либо вводных слов, приветствий или пояснений."
                )]
            )
        )

        categories = ["thinking", "network_error", "device_error", "empty_noise"]
        try:
            logger.info(f"Opening Live WebSocket session to {live_model} with voice {voice_name} for audio synthesis...")
            async with client.aio.live.connect(model=live_model, config=live_config) as session:
                for cat in categories:
                    texts = phrases_dict.get(cat, [])
                    manifest_files[cat] = []
                    manifest_texts[cat] = texts
                    self.phrases[cat] = []

                    for idx, text in enumerate(texts):
                        try:
                            logger.info(f"Synthesizing [{cat} #{idx+1}]: '{text}' via Live WebSocket...")
                            await session.send_client_content(
                                turns=[
                                    types.Content(
                                        role="user",
                                        parts=[types.Part(text=f"Произнеси строго следующий текст: {text}")]
                                    )
                                ],
                                turn_complete=True
                            )

                            pcm_chunks = bytearray()
                            async for live_msg in session.receive():
                                if live_msg.server_content:
                                    if live_msg.server_content.model_turn:
                                        for part in live_msg.server_content.model_turn.parts:
                                            if part.inline_data and part.inline_data.data:
                                                pcm_chunks.extend(part.inline_data.data)
                                    if getattr(live_msg.server_content, "turn_complete", False):
                                        break

                            if len(pcm_chunks) > 0:
                                filename = f"{cat}_{idx}.pcm"
                                filepath = os.path.join(self.cache_dir, filename)
                                with open(filepath, "wb") as pf:
                                    pf.write(pcm_chunks)

                                manifest_files[cat].append(filename)
                                self.phrases[cat].append(bytes(pcm_chunks))
                                logger.info(f"Successfully saved {len(pcm_chunks)} bytes to {filename}")
                            else:
                                logger.warning(f"No audio chunks received for '{text}'")
                        except Exception as e_phrase:
                            logger.error(f"Error synthesizing phrase '{text}': {e_phrase}")

        except Exception as e_ws:
            logger.error(f"Live WebSocket audio synthesis error: {e_ws}")

        # 3. Сохраняем manifest на диск
        manifest_data = {
            "persona_hash": curr_hash,
            "voice_name": voice_name,
            "texts": manifest_texts,
            "files": manifest_files
        }
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, ensure_ascii=False, indent=2)

        self.texts = manifest_texts
        self.is_ready = True
        logger.info("Porfiriy dynamic phrase cache successfully generated via Live WebSocket!")

    def get_phrase(self, category: str) -> Optional[bytes]:
        """Возвращает случайный PCM буфер фразы указанной категории."""
        items = self.phrases.get(category, [])
        if items:
            return random.choice(items)
        return None

    async def play_phrase(self, websocket, phrase_bytes: bytes):
        """Потоковая отправка PCM чанками клиенту (ESP32)."""
        CHUNK_SIZE = 2048
        for i in range(0, len(phrase_bytes), CHUNK_SIZE):
            chunk = phrase_bytes[i:i + CHUNK_SIZE]
            await websocket.send(chunk)
