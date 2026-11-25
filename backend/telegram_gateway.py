from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp
try:
    from google.genai import types
except ImportError:
    types = None

from audio_utils import pcm16_to_wav
from config import DEFAULT_GEMINI_MODEL, get_active_api_key
from db import db_singleton
try:
    from gemini_client import GeminiProxyClient
except ImportError:
    GeminiProxyClient = None
from prompt_builder import build_system_prompt

logger = logging.getLogger("tg_gateway")


class TelegramBotGateway:
    """
    Двусторонний шлюз Telegram-бота для Порфирия 2.0 (Long Polling).
    Принимает текстовые и голосовые сообщения ТОЛЬКО из разрешённых чатов (пользователи и общий чат дома в БД),
    выполняет их строго через Gemini Live API (WebSocket) и возвращает ответ в исходный чат.
    """

    def __init__(
        self,
        ha_api: Any,
        tool_executor: Any,
        options_callback: Callable[[], Dict[str, Any]],
        device_manager: Any = None,
    ):
        self.ha_api = ha_api
        self.tool_executor = tool_executor
        self.options_callback = options_callback
        self.device_manager = device_manager
        self._offset: int = 0
        self._dialogue_history: Dict[str, Dict[str, Any]] = {}
        self._chat_locks: Dict[str, asyncio.Lock] = {}
        self._running: bool = False
        self._warned_conflict: bool = False

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    @staticmethod
    def get_allowed_chats(db: Any = db_singleton) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """
        Возвращает:
        1) allowed_chats: {chat_id: {"user_id": str|None, "user_name": str, "is_family": bool}}
        2) sender_by_tg_id: {tg_user_id: {"user_id": str, "user_name": str, "gender": str}}
        """
        allowed: Dict[str, Dict[str, Any]] = {}
        sender_map: Dict[str, Dict[str, Any]] = {}

        for u in db.list_users(active_only=True):
            tg_id = str(u.get("telegram_chat_id") or "").strip()
            if tg_id:
                allowed[tg_id] = {
                    "user_id": u["id"],
                    "user_name": u["name"],
                    "gender": u.get("gender", "unknown"),
                    "is_family": False,
                }
                sender_map[tg_id] = {
                    "user_id": u["id"],
                    "user_name": u["name"],
                    "gender": u.get("gender", "unknown"),
                }

        fam_chat = str(db.get_family_channels().get("telegram_chat") or "").strip()
        if fam_chat:
            allowed[fam_chat] = {
                "user_id": None,
                "user_name": "Общий чат дома",
                "gender": "unknown",
                "is_family": True,
            }
        return allowed, sender_map

    def _get_history_for_chat(self, chat_id: str, ttl_s: float = 600.0) -> List[Tuple[str, str]]:
        cached = self._dialogue_history.get(chat_id)
        if not cached:
            return []
        if time.time() - cached.get("timestamp", 0.0) > ttl_s:
            return []
        return list(cached.get("pairs", []))

    def _save_turn(self, chat_id: str, user_text: str, reply_text: str) -> None:
        if not chat_id or not user_text or not reply_text:
            return
        pairs = self._get_history_for_chat(chat_id, ttl_s=600.0)
        pairs.append((user_text[:400], reply_text[:500]))
        if len(pairs) > 4:
            pairs = pairs[-4:]
        self._dialogue_history[chat_id] = {"timestamp": time.time(), "pairs": pairs}

    async def start(self) -> None:
        self._running = True
        logger.info("[TG-GW] Telegram Bot Gateway task started.")
        logged_active = False

        while self._running:
            try:
                prov = db_singleton.get_provider("builtin.telegram_bot", mask_secrets=False)
                cfg = (prov or {}).get("config") or {}
                bot_token = (cfg.get("bot_token") or "").strip()
                polling_enabled = bool(cfg.get("polling_enabled", True))

                if not prov or not prov.get("enabled") or not bot_token or not polling_enabled:
                    logged_active = False
                    await asyncio.sleep(5)
                    continue

                if not logged_active:
                    logger.info("[TG-GW] Telegram long-polling started.")
                    logged_active = True

                url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
                params = {
                    "timeout": 25,
                    "offset": self._offset,
                    "allowed_updates": json.dumps(["message"]),
                }
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                        if resp.status == 409:
                            if not self._warned_conflict:
                                logger.error(
                                    "[TG-GW] HTTP 409 Conflict: этот же bot_token уже используется другим polling-клиентом (например, интеграцией telegram_bot в Home Assistant)."
                                )
                                self._warned_conflict = True
                            await asyncio.sleep(15)
                            continue
                        self._warned_conflict = False
                        body = await resp.json(content_type=None)
                        if resp.status != 200 or not isinstance(body, dict) or not body.get("ok"):
                            logger.warning(f"[TG-GW] getUpdates failed ({resp.status}): {body}")
                            await asyncio.sleep(5)
                            continue

                        updates = body.get("result") or []
                        for upd in updates:
                            uid = int(upd.get("update_id") or 0)
                            if uid >= self._offset:
                                self._offset = uid + 1
                            msg = upd.get("message")
                            if isinstance(msg, dict):
                                asyncio.create_task(self._dispatch_message(bot_token, cfg, msg))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[TG-GW] Polling error: {e}")
                await asyncio.sleep(5)

    async def _dispatch_message(self, bot_token: str, cfg: Dict[str, Any], msg: Dict[str, Any]) -> None:
        # Игнорируем слишком старые сообщения из бэклога (> 5 мин)
        msg_date = int(msg.get("date") or 0)
        if msg_date and (time.time() - msg_date > 300):
            return

        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id:
            return

        # Игнорируем сообщения от ботов
        from_obj = msg.get("from") or {}
        if from_obj.get("is_bot"):
            return

        allowed, sender_map = self.get_allowed_chats(db_singleton)
        chat_meta = allowed.get(chat_id)
        if not chat_meta:
            logger.info(f"[TG-GW] Ignored message from unauthorized chat_id={chat_id}")
            return

        # Определяем конкретного жильца (актуально для общего группового чата)
        sender_tg_id = str(from_obj.get("id") or "").strip()
        sender_info = sender_map.get(sender_tg_id)
        if sender_info:
            speaker_id = sender_info["user_id"]
            speaker_name = sender_info["user_name"]
        else:
            speaker_id = chat_meta.get("user_id")
            speaker_name = from_obj.get("first_name") or chat_meta.get("user_name") or chat_id

        lock = self._get_chat_lock(chat_id)
        async with lock:
            await self._process_message(bot_token, cfg, msg, chat_id, chat_meta, speaker_id, speaker_name)

    @staticmethod
    async def _run_ffmpeg(args: List[str], input_bytes: bytes) -> Optional[bytes]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(input=input_bytes), timeout=25)
            if proc.returncode != 0:
                logger.warning(f"[TG-GW] ffmpeg exited {proc.returncode}: {stderr.decode(errors='ignore')[:300]}")
                return None
            return stdout
        except FileNotFoundError:
            logger.error("[TG-GW] ffmpeg binary not found in container!")
            return None
        except Exception as e:
            logger.warning(f"[TG-GW] ffmpeg error: {e}")
            return None

    async def _download_tg_file(self, session: aiohttp.ClientSession, bot_token: str, file_id: str) -> Optional[bytes]:
        async with session.get(
            f"https://api.telegram.org/bot{bot_token}/getFile",
            params={"file_id": file_id},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            info = await resp.json(content_type=None)
            if resp.status != 200 or not isinstance(info, dict) or not info.get("ok"):
                logger.warning(f"[TG-GW] getFile error: {info}")
                return None
            file_path = (info.get("result") or {}).get("file_path")
            if not file_path:
                return None

        async with session.get(
            f"https://api.telegram.org/file/bot{bot_token}/{file_path}",
            timeout=aiohttp.ClientTimeout(total=30),
        ) as f_resp:
            if f_resp.status != 200:
                logger.warning(f"[TG-GW] File download HTTP {f_resp.status}")
                return None
            return await f_resp.read()

    async def _send_chat_action(self, session: aiohttp.ClientSession, bot_token: str, chat_id: str, action: str) -> None:
        try:
            await session.post(
                f"https://api.telegram.org/bot{bot_token}/sendChatAction",
                json={"chat_id": chat_id, "action": action},
                timeout=aiohttp.ClientTimeout(total=5),
            )
        except Exception:
            pass

    async def _send_tg_text(self, session: aiohttp.ClientSession, bot_token: str, chat_id: str, text: str) -> None:
        clean = (text or "").strip()
        if not clean:
            return
        # Разбиваем слишком длинные ответы на части <= 4000 символов
        chunks = [clean[i : i + 4000] for i in range(0, len(clean), 4000)]
        for chunk in chunks:
            async with session.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"[TG-GW] sendMessage failed ({resp.status}): {body[:300]}")

    async def _send_tg_voice(
        self, session: aiohttp.ClientSession, bot_token: str, chat_id: str, ogg_bytes: bytes, caption: str = ""
    ) -> bool:
        form = aiohttp.FormData()
        form.add_field("chat_id", chat_id)
        form.add_field("voice", ogg_bytes, filename="reply.ogg", content_type="audio/ogg")
        if caption and len(caption) <= 950:
            form.add_field("caption", caption)
        async with session.post(
            f"https://api.telegram.org/bot{bot_token}/sendVoice",
            data=form,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(f"[TG-GW] sendVoice failed ({resp.status}): {body[:300]}")
                return False
        if caption and len(caption) > 950:
            await self._send_tg_text(session, bot_token, chat_id, caption)
        return True

    @staticmethod
    def is_forwarded_message(msg: Dict[str, Any]) -> bool:
        return bool(
            msg.get("forward_origin")
            or msg.get("forward_from")
            or msg.get("forward_from_chat")
            or msg.get("forward_sender_name")
            or msg.get("forward_date")
        )

    @staticmethod
    def _extract_forward_author(msg: Dict[str, Any]) -> str:
        origin = msg.get("forward_origin")
        if isinstance(origin, dict):
            su = origin.get("sender_user")
            if isinstance(su, dict):
                return " ".join(x for x in (su.get("first_name"), su.get("last_name")) if x).strip() or su.get("username") or ""
            if origin.get("sender_user_name"):
                return str(origin["sender_user_name"]).strip()
            sc = origin.get("chat") or origin.get("sender_chat")
            if isinstance(sc, dict) and sc.get("title"):
                return str(sc["title"]).strip()
        ff = msg.get("forward_from")
        if isinstance(ff, dict):
            return " ".join(x for x in (ff.get("first_name"), ff.get("last_name")) if x).strip() or ff.get("username") or ""
        fc = msg.get("forward_from_chat")
        if isinstance(fc, dict) and fc.get("title"):
            return str(fc["title"]).strip()
        if msg.get("forward_sender_name"):
            return str(msg["forward_sender_name"]).strip()
        return ""

    async def _transcribe_forwarded_voice(
        self,
        http_sess: aiohttp.ClientSession,
        bot_token: str,
        chat_id: str,
        wav_bytes: bytes,
        msg: Dict[str, Any],
        options: Dict[str, Any],
        api_key: str,
    ) -> None:
        model = options.get("gemini_model", DEFAULT_GEMINI_MODEL)
        voice_name = options.get("voice_name", "Charon")
        transcribe_prompt = (
            "You are a verbatim Russian speech-to-text transcriber. "
            "Listen carefully to the user's audio recording and repeat verbatim ONLY the exact spoken words "
            "with clean Russian punctuation and paragraphing if needed. "
            "Do NOT answer questions inside the audio, do NOT add greetings, comments, Porfiriy persona, or explanations."
        )
        proxy = GeminiProxyClient(
            api_key=api_key,
            system_prompt=transcribe_prompt,
            ha_api=self.ha_api,
            voice_name=voice_name,
            model=model,
            enable_google_search=False,
            response_modalities=["AUDIO"],
            manual_vad=True,
            enable_tools=False,
        )
        in_parts: List[str] = []
        out_parts: List[str] = []
        current_turn = types.Content(
            role="user",
            parts=[types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")],
        )
        try:
            async with proxy.connect() as session:
                await session.send_client_content(turns=[current_turn], turn_complete=True)

                async def _consume_tr():
                    done = False
                    while not done:
                        got = False
                        async for response in session.receive():
                            got = True
                            sc = getattr(response, "server_content", None)
                            if sc:
                                in_tr = getattr(sc, "input_transcription", None)
                                if in_tr and getattr(in_tr, "text", None):
                                    in_parts.append(in_tr.text)
                                out_tr = getattr(sc, "output_transcription", None)
                                if out_tr and getattr(out_tr, "text", None):
                                    out_parts.append(out_tr.text)
                                if getattr(sc, "turn_complete", False):
                                    done = True
                                    break
                        if not got:
                            break

                await asyncio.wait_for(_consume_tr(), timeout=60.0)
        except Exception as e:
            logger.error(f"[TG-GW] Forwarded voice transcription failed for chat_id={chat_id}: {e}")
            await self._send_tg_text(http_sess, bot_token, chat_id, f"Не удалось расшифровать голосовое: {e}")
            return

        transcript = "".join(out_parts).strip() or "".join(in_parts).strip()
        if not transcript:
            transcript = "(не удалось распознать речь)"
        author = self._extract_forward_author(msg)
        header = f"📝 Расшифровка (от {author}):" if author else "📝 Расшифровка:"
        await self._send_tg_text(http_sess, bot_token, chat_id, f"{header}\n\n{transcript}")

    async def _process_message(
        self,
        bot_token: str,
        cfg: Dict[str, Any],
        msg: Dict[str, Any],
        chat_id: str,
        chat_meta: Dict[str, Any],
        speaker_id: Optional[str],
        speaker_name: str,
    ) -> None:
        text_in = (msg.get("text") or msg.get("caption") or "").strip()
        voice_obj = msg.get("voice") or msg.get("audio")
        is_voice = isinstance(voice_obj, dict) and bool(voice_obj.get("file_id"))

        if not text_in and not is_voice:
            return

        options = self.options_callback() if self.options_callback else {}
        api_key = get_active_api_key(options.get("gemini_api_key", ""))
        async with aiohttp.ClientSession() as http_sess:
            if not api_key:
                await self._send_tg_text(http_sess, bot_token, chat_id, "Не настроен API-ключ Gemini в аддоне Порфирий.")
                return

            wav_bytes: Optional[bytes] = None
            if is_voice:
                await self._send_chat_action(http_sess, bot_token, chat_id, "record_voice")
                raw_audio = await self._download_tg_file(http_sess, bot_token, voice_obj["file_id"])
                if not raw_audio:
                    await self._send_tg_text(http_sess, bot_token, chat_id, "Не удалось скачать голосовое сообщение из Telegram.")
                    return
                raw_pcm = await self._run_ffmpeg(
                    ["-y", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "s16le", "pipe:1"],
                    raw_audio,
                )
                if not raw_pcm:
                    await self._send_tg_text(http_sess, bot_token, chat_id, "Не удалось декодировать голосовое сообщение (ffmpeg).")
                    return
                wav_bytes = pcm16_to_wav(raw_pcm, sample_rate=16000)
                logger.info(
                    f"[TG-GW] Decoded voice for chat_id={chat_id}: {len(raw_pcm)} bytes PCM "
                    f"({len(raw_pcm)/32000.0:.2f}s), {len(wav_bytes)} bytes WAV"
                )
                if self.is_forwarded_message(msg):
                    await self._send_chat_action(http_sess, bot_token, chat_id, "typing")
                    logger.info(f"[TG-GW] Transcribing forwarded voice in chat_id={chat_id}")
                    await self._transcribe_forwarded_voice(http_sess, bot_token, chat_id, wav_bytes, msg, options, api_key)
                    return
            else:
                await self._send_chat_action(http_sess, bot_token, chat_id, "typing")

            logger.info(
                f"[TG-GW] Incoming {'voice' if is_voice else 'text'} from {speaker_name} (chat_id={chat_id}): "
                f"'{text_in[:100] if text_in else '<audio>'}'"
            )

            chat_desc = (
                f"Shared family Telegram group chat (chat_id='{chat_id}')"
                if chat_meta.get("is_family")
                else f"Personal Telegram chat with {chat_meta.get('user_name')} (user_id='{chat_meta.get('user_id')}', chat_id='{chat_id}')"
            )
            history_pairs = self._get_history_for_chat(chat_id)
            history_block = ""
            if history_pairs:
                hist_lines = "\n".join(f"- User: {u}\n  Porfiriy: {a}" for u, a in history_pairs)
                history_block = f"\n\n### RECENT TELEGRAM DIALOGUE (same chat):\n{hist_lines}"

            extra_context = (
                f"### CHANNEL CONTEXT (TELEGRAM):\n"
                f"- Source: {chat_desc}.\n"
                f"- Active Speaker / Sender: {speaker_name} (user_id='{speaker_id or 'family'}').\n"
                f"- RESPONSE ROUTING RULE: Your reply is automatically delivered back into this SAME Telegram chat ({chat_id}). "
                f"DO NOT call `send_to_telegram` to reply to the sender in this chat. "
                f"Call `send_to_telegram` ONLY if the user explicitly asks to forward/send something to a DIFFERENT recipient or chat "
                f"(e.g., 'отправь Свете', 'скинь в общий чат').\n"
                f"- Execute any smart home, music, timer, memory, list, or plugin requests normally just as if spoken to a smart speaker."
                f"{history_block}"
            )

            dev_ctx = {"mac": f"tg_{chat_id}", "name": f"Telegram ({speaker_name})", "area_name": ""}
            system_prompt = await build_system_prompt(
                options,
                self.ha_api,
                dev=dev_ctx,
                mode="satellite",
                extra_context=extra_context,
            )

            model = options.get("gemini_model", DEFAULT_GEMINI_MODEL)
            voice_name = options.get("voice_name", "Charon")

            proxy = GeminiProxyClient(
                api_key=api_key,
                system_prompt=system_prompt,
                ha_api=self.ha_api,
                voice_name=voice_name,
                model=model,
                enable_google_search=options.get("enable_google_search", True),
                response_modalities=["AUDIO"],
                manual_vad=True,
            )

            if is_voice and wav_bytes:
                user_parts = [types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")]
                if text_in:
                    user_parts.append(types.Part.from_text(text=text_in))
                current_turn = types.Content(role="user", parts=user_parts)
            else:
                current_turn = types.Content(role="user", parts=[types.Part.from_text(text=text_in)])

            payload_turns = [current_turn]
            input_transcript_parts: List[str] = []
            output_text_parts: List[str] = []
            pcm_chunks: List[bytes] = []
            turn_state = {"tool_executed": False, "playback_changed": False}
            sent_to_same_chat = False

            try:
                async with proxy.connect() as session:
                    await session.send_client_content(turns=payload_turns, turn_complete=True)

                    async def _consume():
                        nonlocal sent_to_same_chat
                        turn_done = False
                        while not turn_done:
                            got_any = False
                            async for response in session.receive():
                                got_any = True
                                if response.tool_call:
                                    fn_resps = []
                                    for call in response.tool_call.function_calls:
                                        call_args = dict(call.args) if call.args else {}
                                        res = await self.tool_executor.execute(
                                            call.name,
                                            call_args,
                                            options,
                                            dev=dev_ctx,
                                            turn_state=turn_state,
                                        )
                                        if call.name == "send_to_telegram" and str(res.get("chat_id") or "") == chat_id:
                                            sent_to_same_chat = True
                                        fn_resps.append(
                                            types.FunctionResponse(
                                                id=call.id,
                                                name=call.name,
                                                response=res,
                                            )
                                        )
                                    await session.send_tool_response(function_responses=fn_resps)
                                    continue

                                sc = getattr(response, "server_content", None)
                                if sc:
                                    in_tr = getattr(sc, "input_transcription", None)
                                    if in_tr and getattr(in_tr, "text", None):
                                        input_transcript_parts.append(in_tr.text)
                                    out_tr = getattr(sc, "output_transcription", None)
                                    if out_tr and getattr(out_tr, "text", None):
                                        output_text_parts.append(out_tr.text)
                                    mt = getattr(sc, "model_turn", None)
                                    if mt and getattr(mt, "parts", None):
                                        for part in mt.parts:
                                            inline = getattr(part, "inline_data", None)
                                            if inline and getattr(inline, "data", None):
                                                pcm_chunks.append(inline.data)
                                    if getattr(sc, "turn_complete", False):
                                        turn_done = True
                                        break
                            if not got_any:
                                break

                    await asyncio.wait_for(_consume(), timeout=60.0)

            except Exception as e:
                logger.error(f"[TG-GW] Gemini Live error for chat_id={chat_id}: {e}", exc_info=True)
                await self._send_tg_text(http_sess, bot_token, chat_id, f"Ошибка обработки запроса: {e}")
                return

            reply_text = "".join(output_text_parts).strip()
            user_history_text = (text_in or "".join(input_transcript_parts).strip() or "[Голосовое сообщение]").strip()

            if sent_to_same_chat and not reply_text:
                return

            if not reply_text and turn_state["tool_executed"]:
                reply_text = "Готово."
            elif not reply_text:
                reply_text = "Не расслышал запрос, повторите ещё раз."

            self._save_turn(chat_id, user_history_text, reply_text)

            reply_voice = bool(cfg.get("reply_voice_to_voice", True))
            if is_voice and reply_voice and pcm_chunks:
                pcm_all = b"".join(pcm_chunks)
                ogg_out = await self._run_ffmpeg(
                    ["-y", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0", "-ac", "2", "-c:a", "libopus", "-b:a", "48k", "-f", "ogg", "pipe:1"],
                    pcm_all,
                )
                if ogg_out:
                    sent_ok = await self._send_tg_voice(http_sess, bot_token, chat_id, ogg_out, caption=reply_text)
                    if sent_ok:
                        return

            await self._send_tg_text(http_sess, bot_token, chat_id, reply_text)
