from __future__ import annotations

import asyncio
import datetime
import html as _html
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
from config import DEFAULT_GEMINI_MODEL, get_active_api_key, get_addon_version
from db import db_singleton
try:
    from gemini_client import GeminiProxyClient
except ImportError:
    GeminiProxyClient = None
from prompt_builder import build_system_prompt
from tg_approvals import approval_manager

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
                    asyncio.create_task(self._register_bot_commands(bot_token))
                    logged_active = True

                url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
                params = {
                    "timeout": 25,
                    "offset": self._offset,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
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
                            cb = upd.get("callback_query")
                            if isinstance(cb, dict):
                                cb_data = str(cb.get("data") or "")
                                if cb_data.startswith(("menu:", "sched:")):
                                    asyncio.create_task(self._handle_menu_callback(bot_token, cb))
                                else:
                                    asyncio.create_task(approval_manager.handle_callback(bot_token, cb))

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

    async def _register_bot_commands(self, bot_token: str) -> None:
        """Регистрирует нативную кнопку Меню и список команд в Telegram."""
        commands = [
            {"command": "menu", "description": "📋 Главное меню Порфирия"},
            {"command": "jobs", "description": "⏰ Планировщик и радары"},
            {"command": "memory", "description": "🧠 Память и списки"},
            {"command": "status", "description": "📊 Статус системы и колонок"},
        ]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"https://api.telegram.org/bot{bot_token}/setMyCommands",
                    json={"commands": commands},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status != 200 or not body.get("ok"):
                        logger.warning(f"[TG-GW] setMyCommands failed: {body}")
                async with session.post(
                    f"https://api.telegram.org/bot{bot_token}/setChatMenuButton",
                    json={"menu_button": {"type": "commands"}},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp2:
                    body2 = await resp2.json(content_type=None)
                    if resp2.status != 200 or not body2.get("ok"):
                        logger.warning(f"[TG-GW] setChatMenuButton failed: {body2}")
                logger.info(f"[TG-GW] Registered bot commands menu ({len(commands)} commands)")
        except Exception as e:
            logger.warning(f"[TG-GW] Failed to register bot menu: {e}")

    async def _send_or_edit_html(
        self,
        session: aiohttp.ClientSession,
        bot_token: str,
        chat_id: str,
        text: str,
        *,
        reply_markup: Optional[Dict[str, Any]] = None,
        edit_message_id: Optional[int] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4090],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if edit_message_id:
            payload["message_id"] = int(edit_message_id)
            method = "editMessageText"
        else:
            method = "sendMessage"
        async with session.post(
            f"https://api.telegram.org/bot{bot_token}/{method}",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                if "message is not modified" not in body.lower():
                    logger.warning(f"[TG-GW] {method} failed ({resp.status}): {body[:300]}")

    async def _answer_callback(self, session: aiohttp.ClientSession, bot_token: str, cb_id: str, text: str = "") -> None:
        try:
            payload: Dict[str, Any] = {"callback_query_id": cb_id}
            if text:
                payload["text"] = text[:180]
            await session.post(
                f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=8),
            )
        except Exception:
            pass

    async def _delete_message(
        self,
        session: aiohttp.ClientSession,
        bot_token: str,
        chat_id: str,
        message_id: int,
    ) -> None:
        try:
            await session.post(
                f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": int(message_id)},
                timeout=aiohttp.ClientTimeout(total=8),
            )
        except Exception:
            pass

    @staticmethod
    def _render_main_menu() -> Tuple[str, Dict[str, Any]]:
        txt = (
            "🤖 <b>Порфирий — меню управления</b>\n\n"
            "Выберите раздел ниже или просто напишите/надиктуйте запрос в этот чат."
        )
        kb = {
            "inline_keyboard": [
                [{"text": "⏰ Планировщик и радары", "callback_data": "menu:jobs"}],
                [{"text": "🧠 Память и списки", "callback_data": "menu:memory"}],
                [{"text": "📊 Статус системы и колонок", "callback_data": "menu:status"}],
                [{"text": "✖️ Скрыть", "callback_data": "menu:close"}],
            ]
        }
        return txt, kb

    @staticmethod
    def _render_jobs_menu() -> Tuple[str, Dict[str, Any]]:
        all_jobs = db_singleton.list_scheduled_jobs()
        active_first = [j for j in all_jobs if j.get("status") not in ("completed", "missed", "cancelled")]
        recent_done = [j for j in all_jobs if j.get("status") in ("completed", "missed", "cancelled")][:3]
        shown = (active_first + recent_done)[:10]

        if not shown:
            txt = (
                "⏰ <b>Планировщик и радары</b>\n\n"
                "Пока нет ни одной задачи. Можно просто написать:\n"
                "• <i>«Через 10 минут напомни выключить духовку»</i>\n"
                "• <i>«Раз в 4 часа следи за ценой ... на Ozon»</i>\n"
                "• <i>«Каждый будний день в 8:30 шли сводку»</i>"
            )
            return txt, {
                "inline_keyboard": [[
                    {"text": "🔄 Обновить", "callback_data": "menu:jobs"},
                    {"text": "⬅️ Меню", "callback_data": "menu:home"},
                    {"text": "✖️ Скрыть", "callback_data": "menu:close"},
                ]]
            }

        st_icons = {
            "active": "🟢",
            "paused": "⏸",
            "paused_error": "⚠️",
            "error": "❌",
            "completed": "✅",
            "missed": "⏭",
        }
        lines = ["⏰ <b>Планировщик и радары</b>\n<i>Нажмите на задачу, чтобы открыть карточку и действия:</i>\n"]
        rows: List[List[Dict[str, str]]] = []
        local_tz = datetime.datetime.now().astimezone().tzinfo

        for idx, j in enumerate(shown, start=1):
            st = j.get("status") or "active"
            icon = st_icons.get(st, "•")
            raw_title = str(j.get("title") or j["id"]).strip()
            title_html = _html.escape(raw_title)
            when = str(j.get("cron_or_run_at") or "")
            nxt_txt = ""
            if j.get("next_run_at"):
                try:
                    dt = datetime.datetime.fromisoformat(j["next_run_at"]).astimezone(local_tz)
                    nxt_txt = dt.strftime("%d.%m %H:%M")
                except Exception:
                    nxt_txt = str(j["next_run_at"])[:16]
            sched_str = nxt_txt or _html.escape(when[:24])
            lines.append(f"<b>{idx}.</b> {icon} {title_html} — <i>{sched_str}</i>")

            jid = j["id"]
            short_title = raw_title[:38] + ("…" if len(raw_title) > 38 else "")
            rows.append([{"text": f"{icon} {short_title}", "callback_data": f"sched:view:{jid}"}])

        rows.append([
            {"text": "🔄 Обновить", "callback_data": "menu:jobs"},
            {"text": "⬅️ Меню", "callback_data": "menu:home"},
            {"text": "✖️ Скрыть", "callback_data": "menu:close"},
        ])
        return "\n".join(lines), {"inline_keyboard": rows}

    @staticmethod
    def _render_job_detail(jid: str) -> Tuple[str, Dict[str, Any]]:
        job = db_singleton.get_scheduled_job(jid)
        if not job:
            return (
                "⏰ <b>Задача не найдена</b>\n\nВозможно, она уже удалена.",
                {
                    "inline_keyboard": [[
                        {"text": "⬅️ К списку", "callback_data": "menu:jobs"},
                        {"text": "✖️ Скрыть", "callback_data": "menu:close"},
                    ]]
                },
            )

        st_map = {
            "active": "🟢 Активна",
            "paused": "⏸ На паузе",
            "paused_error": "⚠️ Ошибка (пауза)",
            "error": "❌ Ошибка",
            "completed": "✅ Выполнена",
            "missed": "⏭ Пропущена",
            "cancelled": "🚫 Отменена",
        }
        st = job.get("status") or "active"
        st_label = st_map.get(st, st)
        title = _html.escape(str(job.get("title") or jid))
        local_tz = datetime.datetime.now().astimezone().tzinfo

        def _fmt_iso(val: Optional[str]) -> str:
            if not val:
                return ""
            try:
                return datetime.datetime.fromisoformat(val).astimezone(local_tz).strftime("%d.%m.%Y %H:%M:%S")
            except Exception:
                return str(val)[:19]

        lines = [
            f"📌 <b>{title}</b>",
            f"Статус: <b>{st_label}</b>",
            f"Тип: <code>{_html.escape(str(job.get('job_type') or ''))}</code>",
        ]
        cron_val = str(job.get("cron_or_run_at") or "").strip()
        if cron_val:
            lines.append(f"Расписание: <code>{_html.escape(cron_val)}</code>")
        nxt = _fmt_iso(job.get("next_run_at"))
        if nxt:
            lines.append(f"Следующий запуск: <b>{nxt}</b>")
        last_r = _fmt_iso(job.get("last_run_at"))
        if last_r:
            last_st = job.get("last_status") or ""
            lines.append(f"Последний запуск: {last_r}" + (f" ({_html.escape(last_st)})" if last_st else ""))

        owner = str(job.get("owner_user_id") or "").strip()
        notify_ch = str(job.get("notify_channel") or "").strip()
        quiet_h = str(job.get("quiet_hours") or "").strip()
        if owner or notify_ch:
            meta_bits = []
            if owner:
                meta_bits.append(f"Владелец: <code>{_html.escape(owner)}</code>")
            if notify_ch:
                meta_bits.append(f"Канал: <code>{_html.escape(notify_ch)}</code>")
            lines.append(" • ".join(meta_bits))
        if quiet_h:
            lines.append(f"Тихие часы: <code>{_html.escape(quiet_h)}</code>")

        act = job.get("action") or {}
        if act.get("type") == "ai_prompt":
            p_txt = _html.escape(str(act.get("prompt") or "")[:400])
            if p_txt:
                lines.append(f"\n📝 <b>Инструкция:</b>\n<i>{p_txt}</i>")
            tools_l = act.get("allowed_tools") or []
            if tools_l:
                lines.append(f"Инструменты: <code>{_html.escape(', '.join(str(t) for t in tools_l))}</code>")
        elif act.get("type") == "direct_tool":
            t_name = str(act.get("tool") or "")
            t_args = act.get("args") or {}
            lines.append(f"\n🔧 <b>Действие:</b> <code>{_html.escape(t_name)}</code>")
            if t_name == "send_to_telegram":
                tgt = _html.escape(str(t_args.get("target") or ""))
                msg_txt = _html.escape(str(t_args.get("text") or "")[:300])
                lines.append(f"Кому: <code>{tgt}</code>\nТекст: <i>«{msg_txt}»</i>")
            elif t_name == "call_ha_service":
                dom = _html.escape(str(t_args.get("domain") or ""))
                srv = _html.escape(str(t_args.get("service") or ""))
                ent = _html.escape(str(t_args.get("entity_id") or ""))
                lines.append(f"Сервис: <code>{dom}.{srv}</code> ({ent})")
            elif t_name == "alarm_chime":
                lbl = _html.escape(str(t_args.get("label") or ""))
                if lbl:
                    lines.append(f"Метка: <i>{lbl}</i>")

        if job.get("last_error"):
            lines.append(f"\n⚠️ <b>Последняя ошибка:</b>\n<code>{_html.escape(str(job['last_error'])[:250])}</code>")

        rows: List[List[Dict[str, str]]] = []
        if st in ("active", "paused", "paused_error", "error"):
            toggle_lbl = "⏸ На паузу" if job.get("enabled") else "▶ Включить"
            rows.append([
                {"text": "▶ Запустить сейчас", "callback_data": f"sched:run:{jid}"},
                {"text": toggle_lbl, "callback_data": f"sched:toggle:{jid}"},
            ])
            rows.append([{"text": "🗑️ Удалить задачу", "callback_data": f"sched:del:{jid}"}])
        else:
            rows.append([
                {"text": "▶ Повторить сейчас", "callback_data": f"sched:run:{jid}"},
                {"text": "🗑️ Удалить задачу", "callback_data": f"sched:del:{jid}"},
            ])
        rows.append([
            {"text": "⬅️ К списку", "callback_data": "menu:jobs"},
            {"text": "✖️ Скрыть", "callback_data": "menu:close"},
        ])
        return "\n".join(lines), {"inline_keyboard": rows}

    @staticmethod
    def _render_memory_menu() -> Tuple[str, Dict[str, Any]]:
        items = db_singleton.search_memory(status="active", limit=15, touch_access=False)
        stats = db_singleton.get_memory_stats()
        kind_icons = {"list_item": "🛒", "fact": "📌", "preference": "⭐", "note": "📝"}
        lines = [f"🧠 <b>Память и списки</b> (активных: {stats.get('active', 0)})\n"]
        if not items:
            lines.append("Активных записей пока нет.")
        else:
            for idx, it in enumerate(items, start=1):
                ic = kind_icons.get(it.get("kind"), "•")
                lbl = f"[{_html.escape(it['list_name'])}] " if it.get("list_name") else ""
                title = _html.escape(str(it.get("title") or "")[:40])
                content = _html.escape(str(it.get("content") or "")[:90])
                body = f"<b>{title}</b>: {content}" if title and title != content else content
                lines.append(f"{idx}. {ic} {lbl}{body}")
        kb = {
            "inline_keyboard": [[
                {"text": "🔄 Обновить", "callback_data": "menu:memory"},
                {"text": "⬅️ Меню", "callback_data": "menu:home"},
                {"text": "✖️ Скрыть", "callback_data": "menu:close"},
            ]]
        }
        return "\n".join(lines), kb

    def _render_status_menu(self) -> Tuple[str, Dict[str, Any]]:
        ver = get_addon_version()
        devs = self.device_manager.get_all_devices() if self.device_manager else []
        online_devs = [d for d in devs if d.get("online")]
        jobs = db_singleton.list_scheduled_jobs()
        active_jobs = sum(1 for j in jobs if j.get("enabled") and j.get("status") == "active")
        err_jobs = sum(1 for j in jobs if j.get("status") in ("paused_error", "error"))
        provs = [p["title"] for p in db_singleton.list_providers(mask_secrets=True, enabled_only=True)]

        lines = [
            f"📊 <b>Статус Порфирия v{_html.escape(ver)}</b>\n",
            f"🔊 Колонок онлайн: <b>{len(online_devs)} / {len(devs)}</b>",
        ]
        for d in devs[:8]:
            mark = "🟢" if d.get("online") else "⚪"
            lines.append(f"   {mark} {_html.escape(str(d.get('name') or d.get('mac')))}")
        lines.append(f"\n⏰ Задач в планировщике: <b>{active_jobs}</b> активных (ошибок: {err_jobs})")
        lines.append(f"🧩 Активные плагины ({len(provs)}): {_html.escape(', '.join(provs) or 'нет')}")
        kb = {
            "inline_keyboard": [[
                {"text": "🔄 Обновить", "callback_data": "menu:status"},
                {"text": "⬅️ Меню", "callback_data": "menu:home"},
                {"text": "✖️ Скрыть", "callback_data": "menu:close"},
            ]]
        }
        return "\n".join(lines), kb

    async def _handle_menu_callback(self, bot_token: str, cb: Dict[str, Any]) -> None:
        cb_id = str(cb.get("id") or "")
        data = str(cb.get("data") or "")
        msg = cb.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "").strip()
        msg_id = msg.get("message_id")
        if not chat_id or not msg_id:
            return
        allowed, _ = self.get_allowed_chats(db_singleton)
        if chat_id not in allowed:
            return

        toast_txt = ""
        view = "jobs"
        detail_jid = ""
        try:
            async with aiohttp.ClientSession() as session:
                if data == "menu:close":
                    await self._answer_callback(session, bot_token, cb_id, "")
                    await self._delete_message(session, bot_token, chat_id, msg_id)
                    return
                if data == "menu:home":
                    view = "home"
                elif data == "menu:jobs":
                    view = "jobs"
                elif data == "menu:memory":
                    view = "memory"
                elif data == "menu:status":
                    view = "status"
                elif data.startswith("sched:view:"):
                    detail_jid = data.split(":", 2)[2]
                    view = "job_detail"
                elif data.startswith("sched:run:"):
                    detail_jid = data.split(":", 2)[2]
                    try:
                        from scheduler import scheduler_singleton
                    except ImportError:
                        from .scheduler import scheduler_singleton
                    await self._answer_callback(session, bot_token, cb_id, "▶ Запускаю задачу...")
                    cb_id = ""
                    res = await scheduler_singleton.run_job_now(detail_jid)
                    toast_txt = "Выполнено" if res.get("status") == "ok" else f"Ошибка: {res.get('error')}"
                    view = "job_detail"
                elif data.startswith("sched:toggle:"):
                    detail_jid = data.split(":", 2)[2]
                    job = db_singleton.get_scheduled_job(detail_jid)
                    if job:
                        new_en = not bool(job.get("enabled"))
                        nxt = job.get("next_run_at")
                        if new_en and not nxt and (job.get("trigger") or {}).get("type") in ("interval", "cron"):
                            try:
                                from scheduler import scheduler_singleton
                            except ImportError:
                                from .scheduler import scheduler_singleton
                            nxt = scheduler_singleton._compute_following_run(job["trigger"])
                        db_singleton.upsert_scheduled_job(
                            job_id=detail_jid,
                            job_type=job["job_type"],
                            title=job["title"],
                            owner_user_id=job["owner_user_id"],
                            cron_or_run_at=job["cron_or_run_at"],
                            trigger=job["trigger"],
                            action=job["action"],
                            notify_channel=job["notify_channel"],
                            quiet_hours=job["quiet_hours"],
                            one_shot=job["one_shot"],
                            enabled=new_en,
                            status="active" if new_en else "paused",
                            next_run_at=nxt,
                        )
                        toast_txt = "Включена" if new_en else "На паузе"
                    view = "job_detail"
                elif data.startswith("sched:del:"):
                    jid = data.split(":", 2)[2]
                    db_singleton.delete_scheduled_job(jid)
                    toast_txt = "🗑️ Задача удалена"
                    view = "jobs"

                if cb_id:
                    await self._answer_callback(session, bot_token, cb_id, toast_txt)

                if view == "home":
                    txt, kb = self._render_main_menu()
                elif view == "memory":
                    txt, kb = self._render_memory_menu()
                elif view == "status":
                    txt, kb = self._render_status_menu()
                elif view == "job_detail":
                    txt, kb = self._render_job_detail(detail_jid)
                else:
                    txt, kb = self._render_jobs_menu()
                await self._send_or_edit_html(session, bot_token, chat_id, txt, reply_markup=kb, edit_message_id=msg_id)
        except Exception as e:
            logger.warning(f"[TG-GW] Menu callback error ({data}): {e}")

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

    async def _send_tg_text(self, session: aiohttp.ClientSession, bot_token: str, chat_id: str, text: str) -> Optional[int]:
        clean = (text or "").strip()
        if not clean:
            return None
        last_msg_id: Optional[int] = None
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
                else:
                    try:
                        data = await resp.json(content_type=None)
                        last_msg_id = (data.get("result") or {}).get("message_id")
                    except Exception:
                        pass
        return last_msg_id

    async def _delete_tg_msg(self, session: aiohttp.ClientSession, bot_token: str, chat_id: str, msg_id: Optional[int]) -> None:
        if not msg_id:
            return
        try:
            await session.post(
                f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": int(msg_id)},
                timeout=aiohttp.ClientTimeout(total=8),
            )
        except Exception:
            pass

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
                if text_in.startswith("/"):
                    cmd = text_in.split()[0].split("@")[0].lower()
                    if cmd in ("/start", "/menu"):
                        txt, kb = self._render_main_menu()
                        await self._send_or_edit_html(http_sess, bot_token, chat_id, txt, reply_markup=kb)
                        return
                    if cmd in ("/jobs", "/schedule"):
                        txt, kb = self._render_jobs_menu()
                        await self._send_or_edit_html(http_sess, bot_token, chat_id, txt, reply_markup=kb)
                        return
                    if cmd == "/memory":
                        txt, kb = self._render_memory_menu()
                        await self._send_or_edit_html(http_sess, bot_token, chat_id, txt, reply_markup=kb)
                        return
                    if cmd == "/status":
                        txt, kb = self._render_status_menu()
                        await self._send_or_edit_html(http_sess, bot_token, chat_id, txt, reply_markup=kb)
                        return
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
            status_msg_id: Optional[int] = None

            async def _on_progress(progress_text: str) -> None:
                nonlocal status_msg_id
                if status_msg_id is None:
                    status_msg_id = await self._send_tg_text(http_sess, bot_token, chat_id, progress_text)

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
                                        current_q = text_in or "".join(input_transcript_parts).strip()
                                        res = await self.tool_executor.execute(
                                            call.name,
                                            call_args,
                                            options,
                                            dev=dev_ctx,
                                            turn_state=turn_state,
                                            user_query=current_q,
                                            on_progress=_on_progress,
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
                                        int_st = str(getattr(sc, "interaction_status", "") or "").upper()
                                        if "IN_PROGRESS" not in int_st:
                                            turn_done = True
                                            break
                            if not got_any:
                                break

                    await asyncio.wait_for(_consume(), timeout=100.0)

            except Exception as e:
                await self._delete_tg_msg(http_sess, bot_token, chat_id, status_msg_id)
                logger.error(f"[TG-GW] Gemini Live error for chat_id={chat_id}: {e}", exc_info=True)
                await self._send_tg_text(http_sess, bot_token, chat_id, f"Ошибка обработки запроса: {e}")
                return

            await self._delete_tg_msg(http_sess, bot_token, chat_id, status_msg_id)

            reply_text = "".join(output_text_parts).strip()
            user_history_text = (text_in or "".join(input_transcript_parts).strip() or "[Голосовое сообщение]").strip()

            if sent_to_same_chat and not reply_text:
                return

            if not reply_text and turn_state["tool_executed"]:
                reply_text = "Готово."
            elif not reply_text:
                reply_text = "Не расслышал запрос, повторите ещё раз."

            logger.info(
                f"[TG-GW] Turn done chat_id={chat_id} tool_executed={turn_state['tool_executed']} "
                f"| in='{user_history_text[:140]}' | out='{reply_text[:180]}'"
            )
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
