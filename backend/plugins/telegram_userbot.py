from __future__ import annotations

import datetime
import logging
import re
import time
from typing import Any, Dict, List, Optional

try:
    from ..plugin_registry import PorfiriyPlugin
except ImportError:
    from plugin_registry import PorfiriyPlugin

try:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession
except ImportError:
    TelegramClient = None
    SessionPasswordNeededError = Exception
    StringSession = None

logger = logging.getLogger("plugins.telegram_userbot")


class TelegramUserbotPlugin(PorfiriyPlugin):
    """
    Встроенный плагин чтения личных Telegram-чатов и каналов через Telethon (MTProto StringSession).
    Поддерживает несколько семейных аккаунтов (Денис, Света, общие) с пошаговой авторизацией через Web UI.
    """

    provider_id = "builtin.telegram_userbot"
    _pending_auth: Dict[str, Dict[str, Any]] = {}

    def get_tool_specs(self, db: Any, provider: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "name": "tg_list_dialogs",
                "description": (
                    "List recent Telegram chats, personal dialogues, groups, or channels from a connected resident's Telegram account "
                    "along with unread counts and last message previews."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "owner": {
                            "type": "STRING",
                            "description": "Account owner user_id/name (e.g. 'denis', 'sveta'). Leave empty to check all authorized accounts.",
                        },
                        "unread_only": {
                            "type": "BOOLEAN",
                            "description": "Set true to return only chats with unread messages.",
                        },
                        "limit": {
                            "type": "INTEGER",
                            "description": "Max dialogs per account (default 15).",
                        },
                    },
                },
            },
            {
                "name": "tg_read_messages",
                "description": (
                    "Read messages from a specific Telegram chat, contact, group, or channel on a connected account. "
                    "Prefer specifying `days` when the user asks for a time period (e.g. 'за сегодня' -> days=1, 'за 3 дня' -> days=3, 'за неделю' -> days=7, 'за месяц' -> days=30)."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "chat_query": {
                            "type": "STRING",
                            "description": "Chat name, person's first/last name, or @username (e.g. 'Мама', 'Вольный общий', 'news_channel').",
                        },
                        "days": {
                            "type": "INTEGER",
                            "description": "Optional number of recent days to read (1=today/24h, 3=last 3 days, 7=week, 30=month; max 60). Leave unset if user just asks for latest messages.",
                        },
                        "owner": {
                            "type": "STRING",
                            "description": "Account owner user_id/name (e.g. 'denis', 'sveta'). Optional.",
                        },
                        "limit": {
                            "type": "INTEGER",
                            "description": "Optional max messages safety cap (default 30 without days, up to 250 with days).",
                        },
                    },
                    "required": ["chat_query"],
                },
            },
            {
                "name": "tg_search_messages",
                "description": (
                    "Search Telegram messages by keyword/phrase across all chats or within a specific chat on connected userbot accounts "
                    "(great for finding codes, links, addresses, order numbers)."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "query": {
                            "type": "STRING",
                            "description": "Search text / keyword.",
                        },
                        "chat_query": {
                            "type": "STRING",
                            "description": "Optional chat name/username to narrow the search.",
                        },
                        "owner": {
                            "type": "STRING",
                            "description": "Account owner user_id/name (e.g. 'denis', 'sveta'). Optional.",
                        },
                        "limit": {
                            "type": "INTEGER",
                            "description": "Max results (default 15).",
                        },
                    },
                    "required": ["query"],
                },
            },
        ]

    def get_prompt_snippet(self, db: Any, provider: Dict[str, Any], options: Dict[str, Any]) -> str:
        accounts = [a for a in (provider.get("accounts") or []) if a.get("enabled")]
        auth_accs = [
            f"{a['label']} (owner={a.get('owner_user_name') or 'Дом'}, id={a.get('owner_user_id') or 'family'})"
            for a in accounts
            if a.get("status") == "authorized" or (a.get("credentials") or {}).get("session_string")
        ]
        accs_text = ", ".join(auth_accs) if auth_accs else "авторизованных аккаунтов пока нет"
        return (
            "### PLUGIN: TELEGRAM USERBOT (READ-ONLY CHATS)\n"
            f"- Authorized Telegram accounts: {accs_text}.\n"
            "- Use `tg_list_dialogs`, `tg_read_messages`, and `tg_search_messages` when asked to check personal Telegram chats, "
            "unread messages, channels, or find confirmation codes/info in messages.\n"
            "- Family policy: any household resident can ask to read/search chats of any authorized family account."
        )

    @classmethod
    async def _cleanup_pending(cls) -> None:
        now = time.time()
        stale = [k for k, v in cls._pending_auth.items() if now - v.get("created_at", 0) > 600]
        for k in stale:
            entry = cls._pending_auth.pop(k, None)
            if entry and entry.get("client"):
                try:
                    await entry["client"].disconnect()
                except Exception:
                    pass

    @classmethod
    async def start_auth(cls, db: Any, account_id: str) -> Dict[str, Any]:
        if TelegramClient is None or StringSession is None:
            return {"success": False, "error": "Библиотека telethon не установлена в контейнере"}
        await cls._cleanup_pending()

        acc = db.get_provider_account(account_id, mask_secrets=False)
        if not acc:
            return {"success": False, "error": "Аккаунт не найден"}

        creds = acc.get("credentials") or {}
        try:
            api_id = int(str(creds.get("api_id") or "").strip())
        except Exception:
            return {"success": False, "error": "Укажите корректный числовой api_id в настройках аккаунта"}
        api_hash = str(creds.get("api_hash") or "").strip()
        phone = str(creds.get("phone") or "").strip()
        if not api_hash or not phone:
            return {"success": False, "error": "Заполните api_hash и номер телефона в настройках аккаунта"}

        # Закрываем предыдущую незавершённую попытку для этого аккаунта
        old = cls._pending_auth.pop(account_id, None)
        if old and old.get("client"):
            try:
                await old["client"].disconnect()
            except Exception:
                pass

        client = TelegramClient(StringSession(), api_id, api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            cls._pending_auth[account_id] = {
                "client": client,
                "phone": phone,
                "phone_code_hash": sent.phone_code_hash,
                "created_at": time.time(),
            }
            db.update_provider_account_status(account_id, "awaiting_code", "")
            return {
                "success": True,
                "status": "awaiting_code",
                "message": f"Код подтверждения отправлен в Telegram на номер {phone}",
            }
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            db.update_provider_account_status(account_id, "error", str(e))
            return {"success": False, "error": str(e)}

    @classmethod
    async def verify_auth(cls, db: Any, account_id: str, code: str = "", password: str = "") -> Dict[str, Any]:
        if TelegramClient is None:
            return {"success": False, "error": "Библиотека telethon не установлена"}
        pending = cls._pending_auth.get(account_id)
        if not pending or not pending.get("client"):
            return {"success": False, "error": "Сессия ввода кода истекла или не запущена. Нажмите «Отправить код» снова."}

        client: TelegramClient = pending["client"]
        phone = pending["phone"]
        phone_code_hash = pending["phone_code_hash"]
        clean_code = str(code or "").strip()
        clean_pwd = str(password or "")

        try:
            if clean_pwd and not clean_code:
                await client.sign_in(password=clean_pwd)
            else:
                try:
                    await client.sign_in(phone=phone, code=clean_code, phone_code_hash=phone_code_hash)
                except SessionPasswordNeededError:
                    if clean_pwd:
                        await client.sign_in(password=clean_pwd)
                    else:
                        db.update_provider_account_status(account_id, "awaiting_2fa", "")
                        return {
                            "success": False,
                            "needs_2fa": True,
                            "status": "awaiting_2fa",
                            "error": "Включена двухфакторная аутентификация — введите облачный пароль Telegram.",
                        }

            me = await client.get_me()
            session_str = client.session.save()
            username = getattr(me, "username", "") or ""
            tg_uid = str(getattr(me, "id", "") or "")
            db.update_provider_account_status(
                account_id,
                "authorized",
                "",
                extra_credentials={
                    "session_string": session_str,
                    "tg_username": username,
                    "tg_user_id": tg_uid,
                },
            )
            cls._pending_auth.pop(account_id, None)
            await client.disconnect()
            return {
                "success": True,
                "status": "authorized",
                "username": username,
                "message": f"Аккаунт авторизован (@{username or phone})",
            }
        except Exception as e:
            db.update_provider_account_status(account_id, "error", str(e))
            return {"success": False, "error": str(e)}

    @staticmethod
    def _select_accounts(db: Any, provider: Dict[str, Any], owner: str = "") -> List[Dict[str, Any]]:
        accounts = [
            a
            for a in (provider.get("accounts") or [])
            if a.get("enabled") and (a.get("credentials") or {}).get("session_string")
        ]
        raw = (owner or "").strip().lower()
        if not raw:
            return accounts

        matched: List[Dict[str, Any]] = []
        for a in accounts:
            oid = (a.get("owner_user_id") or "").lower()
            oname = (a.get("owner_user_name") or "").lower()
            lbl = (a.get("label") or "").lower()
            stem = oname[:-1] if len(oname) >= 4 else oname
            if (
                raw == oid
                or raw == oname
                or raw in lbl
                or (oid and raw.startswith(oid))
                or (stem and len(stem) >= 3 and raw.startswith(stem))
            ):
                matched.append(a)
        return matched

    @staticmethod
    async def _with_client(acc: Dict[str, Any], db: Any, fn):
        creds = acc.get("credentials") or {}
        try:
            api_id = int(str(creds.get("api_id") or "").strip())
        except Exception:
            return {"error": f"Некорректный api_id в аккаунте {acc.get('label')}"}
        api_hash = str(creds.get("api_hash") or "").strip()
        session_str = str(creds.get("session_string") or "").strip()
        if not session_str:
            return {"error": f"Аккаунт {acc.get('label')} не авторизован"}

        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                db.update_provider_account_status(acc["id"], "error", "Сессия Telegram отозвана или истекла")
                return {"error": f"Сессия аккаунта {acc.get('label')} устарела — авторизуйтесь заново в Web UI"}
            return await fn(client)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @staticmethod
    def _dialog_matches(title: str, uname: str, q: str, q_tokens: List[str]) -> int:
        """Возвращает ранг совпадения: 3 (точное), 2 (подстрока), 1 (все токены), 0 (нет)."""
        if q == uname or q == title:
            return 3
        if q in title or (uname and q in uname):
            return 2
        if q_tokens:
            hay = f"{title} {uname}"
            if all(tok in hay for tok in q_tokens):
                return 1
        return 0

    @classmethod
    async def _find_dialog(cls, client: Any, chat_query: str):
        q = (chat_query or "").strip().lower().lstrip("@")
        if not q:
            return None
        q_tokens = [t for t in re.findall(r"[0-9a-zа-яё]+", q) if len(t) >= 2]
        best = None
        best_rank = 0
        async for d in client.iter_dialogs(limit=150):
            title = (d.name or "").strip().lower()
            uname = (getattr(d.entity, "username", None) or "").strip().lower()
            rank = cls._dialog_matches(title, uname, q, q_tokens)
            if rank == 3:
                return d
            if rank > best_rank:
                best = d
                best_rank = rank
        return best

    async def execute_tool(self, tool_name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if TelegramClient is None:
            return {"status": "error", "error": "telethon is not installed"}
        db = ctx.get("db")
        provider = ctx.get("provider") or {}
        owner = str(args.get("owner") or "").strip()
        accounts = self._select_accounts(db, provider, owner=owner)
        if not accounts:
            return {
                "status": "error",
                "error": (
                    f"Не найдены авторизованные аккаунты Telegram Userbot"
                    + (f" для '{owner}'" if owner else ". Подключите и авторизуйте аккаунт во вкладке «Плагины».")
                ),
            }

        if tool_name == "tg_list_dialogs":
            limit = max(1, min(int(args.get("limit") or 15), 40))
            unread_only = bool(args.get("unread_only", False))
            results: List[Dict[str, Any]] = []
            for acc in accounts:
                async def _job(client):
                    items = []
                    async for d in client.iter_dialogs(limit=80 if unread_only else limit):
                        if unread_only and int(d.unread_count or 0) <= 0:
                            continue
                        last_msg = ""
                        if d.message:
                            last_msg = (d.message.message or "[медиа]")[:140]
                        items.append({
                            "chat": d.name or str(d.id),
                            "username": getattr(d.entity, "username", None) or "",
                            "unread": int(d.unread_count or 0),
                            "last_message": last_msg,
                        })
                        if len(items) >= limit:
                            break
                    return {"account": acc["label"], "owner": acc.get("owner_user_name"), "dialogs": items}

                results.append(await self._with_client(acc, db, _job))
            return {"status": "ok", "accounts": results}

        if tool_name == "tg_read_messages":
            chat_query = str(args.get("chat_query") or "").strip()
            raw_days = args.get("days")
            days = max(0, min(int(raw_days), 60)) if raw_days else 0
            if days > 0:
                fetch_limit = max(1, min(int(args.get("limit") or 250), 250))
                since_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
            else:
                fetch_limit = max(1, min(int(args.get("limit") or 30), 150))
                since_dt = None

            if not chat_query:
                return {"status": "error", "error": "Укажите chat_query"}

            for acc in accounts:
                async def _job(client):
                    dialog = await self._find_dialog(client, chat_query)
                    if not dialog:
                        return None
                    raw_msgs = []
                    async for m in client.iter_messages(dialog.entity, limit=fetch_limit):
                        if since_dt and m.date:
                            m_dt = m.date if m.date.tzinfo else m.date.replace(tzinfo=datetime.timezone.utc)
                            if m_dt < since_dt:
                                break
                        sender_name = ""
                        if m.out:
                            sender_name = acc.get("owner_user_name") or "Я"
                        elif m.sender:
                            s_fn = getattr(m.sender, "first_name", "") or ""
                            s_ln = getattr(m.sender, "last_name", "") or ""
                            sender_name = (f"{s_fn} {s_ln}").strip() or getattr(m.sender, "title", "") or ""
                        text = (m.message or "").strip() or "[медиа/вложение]"
                        raw_msgs.append({
                            "date": m.date.strftime("%Y-%m-%d %H:%M") if m.date else "",
                            "from": sender_name,
                            "text": text,
                        })
                    raw_msgs.reverse()
                    max_len = 220 if len(raw_msgs) > 50 else 350
                    for item in raw_msgs:
                        if len(item["text"]) > max_len:
                            item["text"] = item["text"][:max_len] + "…"
                    logger.info(
                        f"[TG-UB] Read {len(raw_msgs)} messages from '{dialog.name}' "
                        f"(days={days or 'latest'}, limit={fetch_limit}, owner={acc.get('owner_user_name')})"
                    )
                    return {
                        "status": "ok",
                        "account": acc["label"],
                        "owner": acc.get("owner_user_name"),
                        "chat": dialog.name,
                        "days": days or None,
                        "message_count": len(raw_msgs),
                        "messages": raw_msgs,
                    }

                res = await self._with_client(acc, db, _job)
                if res:
                    return res
            return {"status": "not_found", "error": f"Чат '{chat_query}' не найден среди доступных аккаунтов"}

        if tool_name == "tg_search_messages":
            query = str(args.get("query") or "").strip()
            chat_query = str(args.get("chat_query") or "").strip()
            limit = max(1, min(int(args.get("limit") or 15), 30))
            if not query:
                return {"status": "error", "error": "Укажите поисковый запрос query"}

            found: List[Dict[str, Any]] = []
            for acc in accounts:
                async def _job(client):
                    entity = None
                    chat_label = "Все чаты"
                    if chat_query:
                        dialog = await self._find_dialog(client, chat_query)
                        if not dialog:
                            return []
                        entity = dialog.entity
                        chat_label = dialog.name
                    items = []
                    async for m in client.iter_messages(entity, search=query, limit=limit):
                        c_name = chat_label
                        if entity is None and m.chat:
                            c_name = getattr(m.chat, "title", None) or getattr(m.chat, "first_name", None) or str(m.chat_id)
                        items.append({
                            "account": acc["label"],
                            "owner": acc.get("owner_user_name"),
                            "chat": c_name,
                            "date": m.date.strftime("%Y-%m-%d %H:%M") if m.date else "",
                            "text": (m.message or "")[:400],
                        })
                    return items

                res = await self._with_client(acc, db, _job)
                if isinstance(res, list):
                    found.extend(res)
            return {"status": "ok", "count": len(found), "results": found[:limit]}

        return {"status": "error", "error": f"Unknown tool: {tool_name}"}
