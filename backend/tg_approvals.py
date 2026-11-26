from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

import aiohttp

try:
    from .db import db_singleton
except ImportError:
    from db import db_singleton

logger = logging.getLogger("tg_approvals")

APPROVAL_TTL_S = 900.0  # 15 минут


class TelegramApprovalManager:
    """
    Менеджер интерактивных согласований через inline-кнопки Telegram («✅ Да» / «❌ Отмена»).
    Позволяет любому плагину (голосовому или текстовому) отправить карточку на подтверждение
    выбранному пользователю в Telegram и выполнить действие сразу по нажатию кнопки.
    """

    def __init__(self) -> None:
        self._pending: Dict[str, Dict[str, Any]] = {}

    def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self._pending.items() if now > v.get("expires_at", 0)]
        for k in expired:
            self._pending.pop(k, None)

    @staticmethod
    def _get_bot_token(db: Any = db_singleton) -> str:
        prov = db.get_provider("builtin.telegram_bot", mask_secrets=False) or {}
        cfg = prov.get("config") or {}
        return str(cfg.get("bot_token") or "").strip()

    @staticmethod
    def _resolve_target_chat(db: Any, target_user_id: Optional[str] = None) -> tuple[str, str]:
        users = db.list_users(active_only=True)
        if target_user_id:
            tid = target_user_id.strip().lower()
            for u in users:
                if u["id"].lower() == tid or u["name"].lower() == tid:
                    cid = str(u.get("telegram_chat_id") or "").strip()
                    if cid:
                        return cid, u["name"]
        # Фолбэк: первый активный жилец с Telegram Chat ID
        for u in users:
            cid = str(u.get("telegram_chat_id") or "").strip()
            if cid:
                return cid, u["name"]
        fam_chat = str(db.get_family_channels().get("telegram_chat") or "").strip()
        if fam_chat:
            return fam_chat, "Общий чат дома"
        return "", ""

    async def request_approval(
        self,
        title: str,
        details_text: str,
        on_approve: Callable[[], Awaitable[str]],
        target_user_id: Optional[str] = None,
        confirm_label: str = "✅ Да, выполнить",
        cancel_label: str = "❌ Отмена",
        db: Any = db_singleton,
    ) -> Dict[str, Any]:
        self._cleanup_expired()
        bot_token = self._get_bot_token(db)
        if not bot_token:
            return {
                "success": False,
                "error": "В плагине builtin.telegram_bot не задан bot_token — невозможно отправить кнопки согласования.",
            }

        chat_id, recipient_name = self._resolve_target_chat(db, target_user_id)
        if not chat_id:
            return {
                "success": False,
                "error": "Не найден Telegram Chat ID получателя согласований (укажите его во вкладке Пользователи).",
            }

        action_id = uuid.uuid4().hex[:10]
        full_text = f"{title}\n\n{details_text}".strip()
        if len(full_text) > 3800:
            full_text = full_text[:3780] + "\n…(список сокращён)"

        payload = {
            "chat_id": chat_id,
            "text": full_text,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": confirm_label, "callback_data": f"appr:yes:{action_id}"},
                    {"text": cancel_label, "callback_data": f"appr:no:{action_id}"},
                ]]
            },
        }

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status != 200 or not isinstance(data, dict) or not data.get("ok"):
                        return {"success": False, "error": f"Telegram sendMessage error: {data}"}
                    msg_id = (data.get("result") or {}).get("message_id")
        except Exception as e:
            return {"success": False, "error": f"Ошибка отправки в Telegram: {e}"}

        now = time.time()
        self._pending[action_id] = {
            "id": action_id,
            "title": title,
            "base_text": full_text,
            "chat_id": chat_id,
            "message_id": msg_id,
            "recipient": recipient_name,
            "created_at": now,
            "expires_at": now + APPROVAL_TTL_S,
            "on_approve": on_approve,
            "status": "pending",
        }
        logger.info(f"[APPROVAL] Sent approval request {action_id} to {recipient_name} (chat_id={chat_id})")
        return {
            "success": True,
            "action_id": action_id,
            "recipient": recipient_name,
            "chat_id": chat_id,
        }

    async def handle_callback(self, bot_token: str, cb: Dict[str, Any]) -> None:
        self._cleanup_expired()
        cb_id = str(cb.get("id") or "")
        data_str = str(cb.get("data") or "")
        msg = cb.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        message_id = msg.get("message_id")
        from_user = cb.get("from") or {}
        from_id = str(from_user.get("id") or "")

        if not data_str.startswith("appr:"):
            return

        parts = data_str.split(":", 2)
        if len(parts) != 3:
            return
        _, decision, action_id = parts

        allowed_ids = {
            str(u.get("telegram_chat_id") or "").strip()
            for u in db_singleton.list_users(active_only=True)
            if u.get("telegram_chat_id")
        }
        fam_chat = str(db_singleton.get_family_channels().get("telegram_chat") or "").strip()
        if fam_chat:
            allowed_ids.add(fam_chat)

        if from_id not in allowed_ids and chat_id not in allowed_ids:
            await self._answer_cb(bot_token, cb_id, "Нет доступа", show_alert=True)
            return

        item = self._pending.get(action_id)
        if not item:
            await self._answer_cb(bot_token, cb_id, "Запрос уже устарел или обработан", show_alert=False)
            if chat_id and message_id:
                await self._clear_markup(bot_token, chat_id, message_id)
            return

        if item.get("status") != "pending":
            await self._answer_cb(bot_token, cb_id, "Уже обработано", show_alert=False)
            return

        if decision == "no":
            item["status"] = "cancelled"
            self._pending.pop(action_id, None)
            await self._answer_cb(bot_token, cb_id, "Отменено")
            await self._edit_message(
                bot_token,
                chat_id,
                message_id,
                f"{item['base_text']}\n\n❌ Отменено пользователем.",
            )
            logger.info(f"[APPROVAL] Action {action_id} cancelled by {from_id}")
            return

        if decision == "yes":
            item["status"] = "running"
            self._pending.pop(action_id, None)
            await self._answer_cb(bot_token, cb_id, "Выполняю...")
            await self._edit_message(
                bot_token,
                chat_id,
                message_id,
                f"{item['base_text']}\n\n⏳ Выполняю...",
            )
            try:
                result_text = await item["on_approve"]()
                await self._edit_message(
                    bot_token,
                    chat_id,
                    message_id,
                    f"{item['base_text']}\n\n✅ {result_text}",
                )
                logger.info(f"[APPROVAL] Action {action_id} approved & completed: {result_text}")
            except Exception as e:
                logger.error(f"[APPROVAL] Action {action_id} failed: {e}")
                await self._edit_message(
                    bot_token,
                    chat_id,
                    message_id,
                    f"{item['base_text']}\n\n⚠️ Ошибка выполнения: {e}",
                )

    async def _answer_cb(self, bot_token: str, cb_id: str, text: str = "", show_alert: bool = False) -> None:
        if not cb_id:
            return
        url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    url,
                    json={"callback_query_id": cb_id, "text": text, "show_alert": show_alert},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
        except Exception:
            pass

    async def _edit_message(self, bot_token: str, chat_id: str, message_id: Any, text: str) -> None:
        if not chat_id or not message_id:
            return
        url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": text[:4096],
                        "reply_markup": {"inline_keyboard": []},
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                )
        except Exception as e:
            logger.debug(f"[APPROVAL] editMessageText error: {e}")

    async def _clear_markup(self, bot_token: str, chat_id: str, message_id: Any) -> None:
        url = f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup"
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    url,
                    json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
        except Exception:
            pass


approval_manager = TelegramApprovalManager()
