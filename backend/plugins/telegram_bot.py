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
                            "description": "Message body in clean readable text (URLs are clickable automatically). Optional if use_last_result=true or result_query is set.",
                        },
                        "title": {
                            "type": "STRING",
                            "description": "Optional short title/header for the message.",
                        },
                        "use_last_result": {
                            "type": "BOOLEAN",
                            "description": "Set true to send the last cached formatted search/recipe/marketplace HTML card (with clickable links and exact numbers) instead of writing new text.",
                        },
                        "result_query": {
                            "type": "STRING",
                            "description": "Optional topic keyword to find a cached search/recipe/marketplace result from the last 3 days (e.g. 'негрони', 'бородино', 'наушники'). Automatically enables use_last_result.",
                        },
                    },
                    "required": ["target"],
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
            "('скинь мне в телегу', 'отправь Свете', 'кинь в общий чат'), call send_to_telegram with the appropriate target ('denis', 'sveta', or 'family').\n"
            "- When the user asks to send the last search result or something searched earlier/yesterday ('отправь мне это в телегу', 'скинь в телегу результат поиска про ...'), "
            "call send_to_telegram with use_last_result=true (and result_query='<topic>' if they mention a specific topic) so the original formatted card with links and exact numbers is sent verbatim."
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
        parse_mode: str = "",
        disable_web_page_preview: bool = False,
        disable_notification: bool = False,
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
            payload: Dict[str, Any] = {"chat_id": chat_id, "text": full_text[:4090]}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if disable_web_page_preview:
                payload["disable_web_page_preview"] = True
            if disable_notification:
                payload["disable_notification"] = True
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
                    # Если Telegram отклонил HTML-разметку — повторяем обычным текстом без тегов
                    if parse_mode and resp.status == 400:
                        plain_fallback = re.sub(r"<[^>]+>", "", full_text)[:4090]
                        async with session.post(
                            url,
                            json={"chat_id": chat_id, "text": plain_fallback, "disable_web_page_preview": disable_web_page_preview},
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp2:
                            body2 = await resp2.json(content_type=None)
                            if resp2.status == 200 and isinstance(body2, dict) and body2.get("ok"):
                                return {
                                    "status": "ok",
                                    "mode": "direct_bot_plain_fallback",
                                    "target": display_name,
                                    "chat_id": str(chat_id),
                                    "message_id": (body2.get("result") or {}).get("message_id"),
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

        def _record_sent_msg(res: Dict[str, Any]) -> Dict[str, Any]:
            if db and isinstance(res, dict) and res.get("status") == "ok" and res.get("message_id"):
                try:
                    plain_body = re.sub(r"<[^>]+>", "", full_text).strip()
                    db.save_dialogue_message(
                        channel="tg",
                        scope_id=str(chat_id),
                        external_msg_id=int(res["message_id"]),
                        role="model",
                        sender_name="Порфирий",
                        text=plain_body[:3000],
                    )
                except Exception as rec_ex:
                    logger.debug(f"Failed to record Telegram bot message in dialogue_messages: {rec_ex}")
            return res

        if mode == "direct_bot":
            return _record_sent_msg(await _send_direct())

        # В режиме ha_notify пробуем HA; если не сработало, но задан bot_token — аккуратный fallback на прямой Bot API
        ha_res = await _send_ha()
        if ha_res.get("status") == "ok":
            return _record_sent_msg(ha_res)
        if bot_token:
            logger.warning(f"HA notify failed ({ha_res.get('error')}), falling back to direct Telegram Bot API")
            direct_res = await _send_direct()
            if direct_res.get("status") == "ok":
                direct_res["fallback_from_ha_error"] = ha_res.get("error")
                return _record_sent_msg(direct_res)
        return ha_res

    async def execute_tool(self, tool_name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name != "send_to_telegram":
            return {"status": "error", "error": f"Unknown tool: {tool_name}"}
        db = ctx.get("db")
        mac = (ctx.get("dev") or {}).get("mac", "")
        target = str(args.get("target") or "").strip()
        if not target:
            target = mac[3:] if mac.startswith("tg_") and len(mac) > 3 else "family"
        text = str(args.get("text") or "").strip()
        title = str(args.get("title") or "").strip()
        use_last = bool(args.get("use_last_result", False))
        res_query = str(args.get("result_query") or "").strip()

        if db and (use_last or (res_query and not text)):
            cached = db.find_rich_result(query=res_query, device_mac=mac, ttl_hours=72)
            if cached:
                send_res = await self.send_message(
                    db=db,
                    provider=ctx.get("provider"),
                    ha_api=ctx.get("ha_api"),
                    target=target,
                    text=cached.get("html_text") or cached.get("plain_text") or "",
                    title="",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                if send_res.get("status") == "ok":
                    send_res["resent_topic"] = cached.get("topic")
                    send_res["resent_kind"] = cached.get("kind")
                return send_res
            if not text:
                return {
                    "status": "not_found",
                    "error": f"В кэше за последние 3 дня не найден сохранённый результат по теме '{res_query or 'последний'}'.",
                }

        return await self.send_message(
            db=db,
            provider=ctx.get("provider"),
            ha_api=ctx.get("ha_api"),
            target=target,
            text=text,
            title=title,
        )
