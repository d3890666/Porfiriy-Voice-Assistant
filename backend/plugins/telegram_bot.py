from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

try:
    from ..plugin_registry import PorfiriyPlugin
except ImportError:
    from plugin_registry import PorfiriyPlugin

logger = logging.getLogger("plugins.telegram_bot")

_FAMILY_KEYWORDS = {"family", "home", "all", "дом", "домой", "семья", "семье", "общий", "общее", "всем"}


def _is_numeric_chat_id(val: str) -> bool:
    return bool(re.fullmatch(r"-?\d+", (val or "").strip()))


class TelegramBotPlugin(PorfiriyPlugin):
    """Встроенный плагин отправки сообщений через Telegram-бота (HA notify или прямой Bot API)."""

    provider_id = "builtin.telegram_bot"

    def get_tool_specs(self, db: Any, provider: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "name": "send_to_telegram",
                "description": (
                    "Send a text message, recipe, link, shopping list, or note to a household member's personal Telegram chat "
                    "or to the shared family Telegram chat."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "target": {
                            "type": "STRING",
                            "description": "Recipient user_id (e.g. 'denis', 'sveta'), resident name, 'family' for the shared home chat, or numeric chat_id.",
                        },
                        "text": {
                            "type": "STRING",
                            "description": "Message body in clean readable text (URLs are clickable automatically).",
                        },
                        "title": {
                            "type": "STRING",
                            "description": "Optional short title/header for the message.",
                        },
                    },
                    "required": ["target", "text"],
                },
            }
        ]

    def get_prompt_snippet(self, db: Any, provider: Dict[str, Any], options: Dict[str, Any]) -> str:
        users = db.list_users(active_only=True)
        family_ch = db.get_family_channels().get("telegram_chat", "")
        targets: List[str] = []
        for u in users:
            if u.get("telegram_chat_id"):
                targets.append(f"'{u['id']}' ({u['name']})")
        if family_ch:
            targets.append("'family' (Общий чат дома)")
        targets_str = ", ".join(targets) if targets else "получатели пока не настроены"
        return (
            "### PLUGIN: TELEGRAM BOT (send_to_telegram)\n"
            f"- Configured Telegram recipients: {targets_str}.\n"
            "- When the user asks to send/drop a recipe, link, code, list, or text to their phone or to another family member "
            "('скинь мне в телегу', 'отправь Свете', 'кинь в общий чат'), call send_to_telegram with the appropriate target ('denis', 'sveta', or 'family')."
        )

    @staticmethod
    def _resolve_chat_id(db: Any, target: str) -> Tuple[str, str]:
        raw = (target or "").strip()
        if not raw:
            return "", ""

        if _is_numeric_chat_id(raw):
            return raw, raw

        low = raw.lower()
        if low in _FAMILY_KEYWORDS:
            fam_id = (db.get_family_channels().get("telegram_chat") or "").strip()
            return fam_id, "Общий чат дома"

        users = db.list_users(active_only=True)
        # 1. Точное совпадение id или имени
        for u in users:
            uid = (u.get("id") or "").lower()
            uname = (u.get("name") or "").lower()
            if low == uid or low == uname:
                return (u.get("telegram_chat_id") or "").strip(), u.get("name") or uid

        # 2. Нечёткое совпадение по корню имени (учёт падежей: "Денису", "Свете", "Дениса")
        for u in users:
            uid = (u.get("id") or "").lower()
            uname = (u.get("name") or "").lower()
            stem = uname[:-1] if len(uname) >= 4 else uname
            if (uid and low.startswith(uid)) or (stem and len(stem) >= 3 and low.startswith(stem)):
                return (u.get("telegram_chat_id") or "").strip(), u.get("name") or uid

        return "", raw

    @classmethod
    async def send_message(
        cls,
        *,
        db: Any,
        provider: Optional[Dict[str, Any]],
        ha_api: Any,
        target: str,
        text: str,
        title: str = "",
    ) -> Dict[str, Any]:
        if not provider:
            return {"status": "error", "error": "Провайдер builtin.telegram_bot не найден в БД"}

        clean_text = (text or "").strip()
        if not clean_text:
            return {"status": "error", "error": "Текст сообщения не может быть пустым"}

        chat_id, display_name = cls._resolve_chat_id(db, target)
        if not chat_id:
            return {
                "status": "error",
                "error": f"Не найден Telegram chat_id для получателя '{target}'. Проверь вкладку «Пользователи».",
            }

        clean_title = (title or "").strip()
        full_text = f"{clean_title}\n\n{clean_text}" if clean_title else clean_text

        cfg = provider.get("config") or {}
        mode = (cfg.get("mode") or "ha_notify").strip()
        ha_service = (cfg.get("ha_service") or "notify.telegram").strip()
        bot_token = (cfg.get("bot_token") or "").strip()

        async def _send_direct() -> Dict[str, Any]:
            if not bot_token:
                return {"status": "error", "error": "Bot Token не задан в настройках плагина builtin.telegram_bot"}
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {"chat_id": chat_id, "text": full_text}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status == 200 and isinstance(body, dict) and body.get("ok"):
                        msg_id = (body.get("result") or {}).get("message_id")
                        return {
                            "status": "ok",
                            "mode": "direct_bot",
                            "target": display_name,
                            "chat_id": str(chat_id),
                            "message_id": msg_id,
                        }
                    desc = body.get("description") if isinstance(body, dict) else str(body)
                    return {
                        "status": "error",
                        "mode": "direct_bot",
                        "error": f"Telegram API HTTP {resp.status}: {desc}",
                    }

        async def _send_ha() -> Dict[str, Any]:
            if not ha_api:
                return {"status": "error", "mode": "ha_notify", "error": "HomeAssistantAPI недоступен"}
            if "." in ha_service:
                domain, service = ha_service.split(".", 1)
            else:
                domain, service = "notify", ha_service or "telegram"

            parsed_target: Any = int(chat_id) if _is_numeric_chat_id(chat_id) else chat_id
            service_data: Dict[str, Any] = {"message": full_text, "target": parsed_target}
            if clean_title:
                service_data["title"] = clean_title

            res = await ha_api.call_ha_action(domain=domain, service=service, service_data=service_data)
            if isinstance(res, dict) and res.get("status") == "error":
                return {
                    "status": "error",
                    "mode": "ha_notify",
                    "service": f"{domain}.{service}",
                    "error": str(res.get("error") or "HA service error"),
                }
            return {
                "status": "ok",
                "mode": "ha_notify",
                "service": f"{domain}.{service}",
                "target": display_name,
                "chat_id": str(chat_id),
            }

        if mode == "direct_bot":
            return await _send_direct()

        # В режиме ha_notify пробуем HA; если не сработало, но задан bot_token — аккуратный fallback на прямой Bot API
        ha_res = await _send_ha()
        if ha_res.get("status") == "ok":
            return ha_res
        if bot_token:
            logger.warning(f"HA notify failed ({ha_res.get('error')}), falling back to direct Telegram Bot API")
            direct_res = await _send_direct()
            if direct_res.get("status") == "ok":
                direct_res["fallback_from_ha_error"] = ha_res.get("error")
                return direct_res
        return ha_res

    async def execute_tool(self, tool_name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name != "send_to_telegram":
            return {"status": "error", "error": f"Unknown tool: {tool_name}"}
        return await self.send_message(
            db=ctx.get("db"),
            provider=ctx.get("provider"),
            ha_api=ctx.get("ha_api"),
            target=str(args.get("target") or ""),
            text=str(args.get("text") or ""),
            title=str(args.get("title") or ""),
        )
