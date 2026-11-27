import os
import re
import json
import logging
import random
import hashlib
import asyncio
from typing import Dict, List, Optional, Callable
from google import genai
from google.genai import types

from audio_utils import scale_pcm16
from config import DEFAULT_GEMINI_MODEL, DEFAULT_PERSONA, get_active_api_key

logger = logging.getLogger("phrases")

FALLBACK_PHRASE_CATALOG: Dict[str, List[str]] = {
    "thinking": [
        "Секунду, перевариваю.",
        "Дай подумать.",
        "Обрабатываю запрос.",
        "Сейчас раскопаю.",
        "Терпение, смотрю.",
        "Минутку, сверяюсь.",
        "Уже ищу.",
        "Проверяю факты.",
        "Держи паузу.",
        "Секунду, уточняю.",
    ],
    "network_error": ["Канал отвалился.", "Облако молчит.", "Связь пропала."],
    "device_error": ["Железо не отвечает.", "Устройство зависло.", "Сбой команды."],
    "empty_noise": ["Говори внятнее.", "Один шум.", "Ничего не слышу."],
}


def build_generation_prompt(persona_text: str) -> str:
    persona = persona_text.strip() if persona_text and persona_text.strip() else DEFAULT_PERSONA
    return f"""{persona}

TASK:
Generate a JSON dictionary of short spoken Russian phrases that you will speak aloud through the smart speaker during operational pauses and technical hiccups.

CATEGORIES:
1. "thinking" (Generate 10 DISTINCT, varied variants): Spoken when a complex query, search, or tool call takes longer than 5 seconds.
   Context: Brief, varied, ironically calm or witty pause-fillers while you compute or look up the answer. Make all 10 clearly different in wording and rhythm so they never feel repetitive.
2. "network_error" (Generate 3 unique variants): Spoken when the external Google Live API or network connection drops, times out, or fails.
   Context: Cynical reaction to the failure of external channels or cloud infrastructure.
3. "device_error" (Generate 3 unique variants): Spoken when a physical smart home device (switch, cover, light) fails to respond in Home Assistant.
   Context: Mocking observation of physical hardware unreliability.
4. "empty_noise" (Generate 3 unique variants): Spoken when the wake word was triggered by acoustic background noise or silence.
   Context: Cold, slightly dismissive reaction to empty sound, urging the human to articulate clearly.

STRICT CONSTRAINTS:
- Length: Strictly 1 to 4 words per phrase (must sound under 1.5 seconds).
- Tone: Pure authentic Porfiriy. Absolutely NO cheesy robot sci-fi clichés (do NOT talk about "нейроны", "энтропию", "процессоры", "гироскопы"). Use sharp intellectual wit, colloquial bite, philosophical irony, and alpha calm.
- Language: Natural spoken Russian.
- Output Format: Return strictly a valid JSON object without markdown fences, comments, or backticks:
{{
  "thinking": ["...", "...", "...", "...", "...", "...", "...", "...", "...", "..."],
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
        self._bags: Dict[str, List[int]] = {}
        self._last_idx: Dict[str, int] = {}
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
                self._bags.clear()
                self._last_idx.clear()
                self.is_ready = True
                logger.info(f"Loaded {loaded_count} cached Porfiriy phrases from {self.cache_dir}")
                return True
        except Exception as e:
            logger.error(f"Failed to load audio cache: {e}")
            
        return False

    async def initialize(self, options: dict):
        """Проверяет актуальность кэша и при необходимости генерирует фразы."""
        api_key = get_active_api_key(options.get("gemini_api_key", ""))
        if not api_key:
            logger.warning("No valid Gemini API key provided, skipping phrase generation.")
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
                        if len(self.phrases.get("thinking", [])) >= 8:
                            return
                        logger.info(
                            f"Cached 'thinking' phrases count is {len(self.phrases.get('thinking', []))} (< 8); "
                            "regenerating expanded 10-phrase catalog..."
                        )
            except Exception:
                pass

        model = options.get("gemini_model", DEFAULT_GEMINI_MODEL)
        try:
            async with self._generating_lock:
                await self.generate_phrases(api_key, persona, voice_name, model)
        except Exception as e:
            logger.error(f"Error in phrase generation task: {e}", exc_info=True)

    async def generate_phrases(self, api_key: str, persona: str, voice_name: str, model_name: str):
        """Генерация и синтез системных фраз строго через Gemini Live API (bidiGenerateContent)."""
        logger.info("Generating dynamic system phrases via Gemini Live API...")
        client = genai.Client(http_options={"api_version": "v1alpha"}, api_key=api_key)
        live_model = model_name if model_name.startswith("models/") else f"models/{model_name}"

        # 1. Получаем каталог текстов через Live WebSocket (output_audio_transcription) строго на выбранной модели
        phrases_dict = None
        try:
            text_live_config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                output_audio_transcription=types.AudioTranscriptionConfig(),
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                    )
                ),
            )
            async with client.aio.live.connect(model=live_model, config=text_live_config) as text_session:
                await text_session.send_client_content(
                    turns=[types.Content(role="user", parts=[types.Part(text=build_generation_prompt(persona))])],
                    turn_complete=True,
                )

                async def _collect_transcript() -> str:
                    parts: List[str] = []
                    async for live_msg in text_session.receive():
                        sc = getattr(live_msg, "server_content", None)
                        if not sc:
                            continue
                        ot = getattr(sc, "output_transcription", None)
                        if ot and getattr(ot, "text", None):
                            parts.append(ot.text)
                        if getattr(sc, "turn_complete", False):
                            break
                    return "".join(parts).strip()

                raw_text = await asyncio.wait_for(_collect_transcript(), timeout=20.0)
                match = re.search(r"\{.*\}", raw_text, re.DOTALL)
                if match:
                    parsed = json.loads(match.group(0))
                    if isinstance(parsed, dict) and any(parsed.get(c) for c in FALLBACK_PHRASE_CATALOG):
                        phrases_dict = parsed
                        logger.info(f"Porfiriy generated phrase catalog via Live API: {json.dumps(phrases_dict, ensure_ascii=False)}")
        except Exception as e:
            logger.warning(f"Live API phrase catalog generation fell back to built-in catalog: {e}")

        if not phrases_dict:
            phrases_dict = {k: list(v) for k, v in FALLBACK_PHRASE_CATALOG.items()}

        # 2. Синтез аудио через Live WebSocket (единая сессия bidiGenerateContent)
        manifest_files = {}
        manifest_texts = {}
        curr_hash = self._hash_persona(persona, voice_name)

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
        synthesized_total = 0
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

                            async def _collect_turn() -> bytearray:
                                buf = bytearray()
                                async for live_msg in session.receive():
                                    if live_msg.server_content:
                                        if live_msg.server_content.model_turn:
                                            for part in live_msg.server_content.model_turn.parts:
                                                if part.inline_data and part.inline_data.data:
                                                    buf.extend(part.inline_data.data)
                                        if getattr(live_msg.server_content, "turn_complete", False):
                                            break
                                return buf

                            pcm_chunks = await asyncio.wait_for(_collect_turn(), timeout=15.0)

                            if len(pcm_chunks) > 0:
                                filename = f"{cat}_{idx}.pcm"
                                filepath = os.path.join(self.cache_dir, filename)
                                with open(filepath, "wb") as pf:
                                    pf.write(pcm_chunks)

                                manifest_files[cat].append(filename)
                                self.phrases[cat].append(bytes(pcm_chunks))
                                synthesized_total += 1
                                logger.info(f"Successfully saved {len(pcm_chunks)} bytes to {filename}")
                            else:
                                logger.warning(f"No audio chunks received for '{text}'")
                        except Exception as e_phrase:
                            logger.error(f"Error synthesizing phrase '{text}': {e_phrase}")

        except Exception as e_ws:
            logger.error(f"Live WebSocket audio synthesis error: {e_ws}")

        if synthesized_total == 0:
            self.is_ready = False
            logger.error("Phrase synthesis produced 0 audio files; cache not marked ready.")
            return

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
        logger.info(f"Porfiriy dynamic phrase cache successfully generated via Live WebSocket ({synthesized_total} files)!")

    def get_phrase(self, category: str) -> Optional[bytes]:
        """Возвращает PCM буфер фразы без повторов до исчерпания всей колоды (shuffle-bag)."""
        items = self.phrases.get(category, [])
        n = len(items)
        if n == 0:
            return None
        if n == 1:
            return items[0]

        bag = self._bags.get(category)
        if not bag:
            bag = list(range(n))
            random.shuffle(bag)
            last = self._last_idx.get(category)
            if last is not None and bag[-1] == last:
                bag[0], bag[-1] = bag[-1], bag[0]
            self._bags[category] = bag

        idx = bag.pop()
        self._last_idx[category] = idx
        return items[idx]

    async def play_phrase(
        self,
        websocket,
        phrase_bytes: bytes,
        volume: float = 1.0,
        cancel_check: Optional[Callable[[], bool]] = None,
        send_speaking_event: bool = True,
    ):
        """Потоковая отправка PCM чанками клиенту с корректным темпом (24кГц, 16 бит)."""
        if send_speaking_event:
            try:
                await websocket.send(json.dumps({"type": "speaking"}))
            except Exception:
                pass

        if volume != 1.0:
            phrase_bytes = scale_pcm16(phrase_bytes, volume)

        CHUNK_SIZE = 2048
        # 24000 Hz * 2 bytes = 48000 bytes/sec -> 2048 bytes ≈ 42.6 ms
        CHUNK_SLEEP = (CHUNK_SIZE / 48000.0) * 0.9  # ~38.4ms
        for i in range(0, len(phrase_bytes), CHUNK_SIZE):
            if cancel_check and cancel_check():
                break
            chunk = phrase_bytes[i:i + CHUNK_SIZE]
            await websocket.send(chunk)
            await asyncio.sleep(CHUNK_SLEEP)

    def get_status(self) -> dict:
        """Возвращает текущее состояние кэша фраз."""
        counts = {cat: len(self.phrases.get(cat, [])) for cat in self.phrases}
        total = sum(counts.values())
        return {
            "is_ready": self.is_ready,
            "total_phrases": total,
            "categories": counts,
            "is_generating": self._generating_lock.locked(),
            "texts": self.texts
        }

    async def regenerate(self, options: dict):
        """Ручная очистка кэша и принудительная регенерация всех системных фраз."""
        async with self._generating_lock:
            logger.info("Manual phrase cache regeneration requested...")
            for cat in self.phrases:
                self.phrases[cat] = []
            self._bags.clear()
            self._last_idx.clear()
            self.texts = {}
            self.is_ready = False
            
            if os.path.exists(self.manifest_path):
                try:
                    os.remove(self.manifest_path)
                except Exception:
                    pass
            if os.path.exists(self.cache_dir):
                for fname in os.listdir(self.cache_dir):
                    if fname.endswith(".pcm"):
                        try:
                            os.remove(os.path.join(self.cache_dir, fname))
                        except Exception:
                            pass
                            
            api_key = get_active_api_key(options.get("gemini_api_key", ""))
            if not api_key:
                logger.warning("Cannot regenerate phrases: Gemini API key is missing.")
                return
            persona = options.get("prompt_persona", DEFAULT_PERSONA)
            voice_name = options.get("voice_name", "Charon")
            model = options.get("gemini_model", DEFAULT_GEMINI_MODEL)
            await self.generate_phrases(api_key, persona, voice_name, model)
            logger.info("Manual phrase regeneration completed.")
