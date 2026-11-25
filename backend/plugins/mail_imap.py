from __future__ import annotations

import asyncio
import datetime
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import html
import imaplib
import logging
import re
from typing import Any, Dict, List, Optional

try:
    from ..plugin_registry import PorfiriyPlugin
except ImportError:
    from plugin_registry import PorfiriyPlugin

logger = logging.getLogger("plugins.mail_imap")

_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _format_imap_date(dt: datetime.datetime) -> str:
    return f"{dt.day:02d}-{_IMAP_MONTHS[dt.month - 1]}-{dt.year}"


def _decode_mime_header(raw_val: Optional[str]) -> str:
    if not raw_val:
        return ""
    parts = []
    for chunk, charset in decode_header(raw_val):
        if isinstance(chunk, bytes):
            enc = (charset or "utf-8").strip()
            try:
                parts.append(chunk.decode(enc, errors="replace"))
            except LookupError:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _html_to_text(raw_html: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style|head).*?>.*?</\1>", " ", raw_html or "")
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in cleaned.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _extract_text_body(msg: email.message.Message, max_len: int = 1500) -> str:
    plain_parts: List[str] = []
    html_parts: List[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            cdisp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in cdisp:
                continue
            ctype = (part.get_content_type() or "").lower()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain_parts.append(decoded)
            else:
                html_parts.append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            if (msg.get_content_type() or "").lower() == "text/html":
                html_parts.append(decoded)
            else:
                plain_parts.append(decoded)

    text = "\n".join(plain_parts).strip()
    if not text and html_parts:
        text = _html_to_text("\n".join(html_parts)).strip()
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def _format_msg_date(raw_date: Optional[str]) -> str:
    if not raw_date:
        return ""
    try:
        dt = parsedate_to_datetime(raw_date)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(raw_date)[:32]


def _extract_bytes_from_fetch(fetch_data: Any) -> bytes:
    if not fetch_data:
        return b""
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return b""


class MailImapPlugin(PorfiriyPlugin):
    """Встроенный read-only плагин чтения почты и поиска кодов подтверждения по IMAP (SSL)."""

    provider_id = "builtin.mail_imap"

    def get_tool_specs(self, db: Any, provider: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "name": "mail_list_recent",
                "description": (
                    "List recent incoming emails (sender, subject, date, uid) from connected IMAP mailboxes without downloading full bodies or marking them read."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "owner": {
                            "type": "STRING",
                            "description": "Mailbox owner user_id/name (e.g. 'denis', 'sveta'). Leave empty to check all connected mailboxes.",
                        },
                        "days": {
                            "type": "INTEGER",
                            "description": "Recent days window (default 3, e.g. 1=today, 7=week, max 30).",
                        },
                        "unread_only": {
                            "type": "BOOLEAN",
                            "description": "Set true to list only unread emails.",
                        },
                        "limit": {
                            "type": "INTEGER",
                            "description": "Max emails per mailbox (default 10, max 25).",
                        },
                    },
                },
            },
            {
                "name": "mail_search",
                "description": (
                    "Search incoming emails by sender, subject, or keyword (great for finding verification/OTP codes, delivery notifications, receipts)."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "query": {
                            "type": "STRING",
                            "description": "Search keyword, service name, or sender (e.g. 'Ozon', 'код', 'Госуслуги', 'Wildberries').",
                        },
                        "owner": {
                            "type": "STRING",
                            "description": "Mailbox owner user_id/name (e.g. 'denis', 'sveta'). Optional.",
                        },
                        "days": {
                            "type": "INTEGER",
                            "description": "Optional recent days filter. Omit for progressive search (last 30 days first, auto-expanding to all time if nothing found), or pass 0 to force all-time search immediately.",
                        },
                        "limit": {
                            "type": "INTEGER",
                            "description": "Max matching emails to return with body text (default 8, max 15).",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "mail_read_message",
                "description": "Read the full cleaned text body of a specific email by its UID obtained from mail_list_recent or mail_search.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "uid": {
                            "type": "STRING",
                            "description": "Email UID string returned by mail_list_recent or mail_search.",
                        },
                        "account_id": {
                            "type": "STRING",
                            "description": "Optional mailbox account_id if known.",
                        },
                        "owner": {
                            "type": "STRING",
                            "description": "Optional mailbox owner user_id/name.",
                        },
                    },
                    "required": ["uid"],
                },
            },
        ]

    def get_prompt_snippet(self, db: Any, provider: Dict[str, Any], options: Dict[str, Any]) -> str:
        accounts = [a for a in (provider.get("accounts") or []) if a.get("enabled")]
        items = [
            f"{a['label']} (owner={a.get('owner_user_name') or 'Дом'}, email={(a.get('credentials') or {}).get('email', '')})"
            for a in accounts
        ]
        accs_str = ", ".join(items) if items else "почтовые ящики пока не добавлены"
        return (
            "### PLUGIN: EMAIL IMAP (READ-ONLY)\n"
            f"- Connected mailboxes: {accs_str}.\n"
            "- Use `mail_list_recent`, `mail_search`, and `mail_read_message` to check incoming mail, find OTP/confirmation codes, or read letters.\n"
            "- Strictly read-only (never marks mail as read). Family policy: any resident can ask to check any connected family mailbox."
        )

    @staticmethod
    def _select_accounts(provider: Dict[str, Any], owner: str = "", account_id: str = "") -> List[Dict[str, Any]]:
        accounts = [
            a
            for a in (provider.get("accounts") or [])
            if a.get("enabled")
            and (a.get("credentials") or {}).get("host")
            and (a.get("credentials") or {}).get("email")
            and (a.get("credentials") or {}).get("password")
        ]
        if account_id:
            by_id = [a for a in accounts if a.get("id") == account_id]
            if by_id:
                return by_id
        raw = (owner or "").strip().lower()
        if not raw:
            return accounts

        matched = []
        for a in accounts:
            oid = (a.get("owner_user_id") or "").lower()
            oname = (a.get("owner_user_name") or "").lower()
            lbl = (a.get("label") or "").lower()
            mail = ((a.get("credentials") or {}).get("email") or "").lower()
            stem = oname[:-1] if len(oname) >= 4 else oname
            if (
                raw == oid
                or raw == oname
                or raw in lbl
                or raw in mail
                or (oid and raw.startswith(oid))
                or (stem and len(stem) >= 3 and raw.startswith(stem))
            ):
                matched.append(a)
        return matched

    @staticmethod
    def _open_imap(acc: Dict[str, Any]) -> imaplib.IMAP4_SSL:
        creds = acc.get("credentials") or {}
        host = str(creds.get("host") or "").strip()
        port = int(creds.get("port") or 993)
        user = str(creds.get("email") or "").strip()
        pwd = str(creds.get("password") or "")
        imap = imaplib.IMAP4_SSL(host, port, timeout=15)
        imap.login(user, pwd)
        imap.select("INBOX", readonly=True)
        return imap

    @classmethod
    def _sync_test_account(cls, acc: Dict[str, Any]) -> Dict[str, Any]:
        imap = None
        try:
            imap = cls._open_imap(acc)
            typ, data = imap.uid("search", None, "ALL")
            count = len((data[0] or b"").split()) if typ == "OK" and data else 0
            return {"success": True, "inbox_count": count, "message": f"Подключение успешно! Входящих писем: {count}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except Exception:
                    pass

    @classmethod
    async def test_connection(cls, db: Any, account_id: str) -> Dict[str, Any]:
        acc = db.get_provider_account(account_id, mask_secrets=False)
        if not acc:
            return {"success": False, "error": "Аккаунт не найден"}
        res = await asyncio.to_thread(cls._sync_test_account, acc)
        if res.get("success"):
            db.update_provider_account_status(account_id, "authorized", "")
        else:
            db.update_provider_account_status(account_id, "error", str(res.get("error") or ""))
        return res

    @classmethod
    def _sync_list_recent(cls, acc: Dict[str, Any], days: int, limit: int, unread_only: bool) -> Dict[str, Any]:
        imap = None
        try:
            imap = cls._open_imap(acc)
            criteria: List[str] = []
            if unread_only:
                criteria.append("UNSEEN")
            if days > 0:
                since = _format_imap_date(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days))
                criteria.extend(["SINCE", since])
            if not criteria:
                criteria = ["ALL"]

            typ, data = imap.uid("search", None, *criteria)
            uids = (data[0] or b"").split() if typ == "OK" and data else []
            selected = uids[-limit:][::-1]

            messages = []
            for uid_b in selected:
                uid_str = uid_b.decode("ascii", errors="ignore")
                ftyp, fdata = imap.uid("fetch", uid_b, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if ftyp != "OK":
                    continue
                raw_hdr = _extract_bytes_from_fetch(fdata)
                msg = email.message_from_bytes(raw_hdr)
                messages.append({
                    "uid": uid_str,
                    "account_id": acc["id"],
                    "from": _decode_mime_header(msg.get("From")),
                    "subject": _decode_mime_header(msg.get("Subject")) or "(без темы)",
                    "date": _format_msg_date(msg.get("Date")),
                })
            return {
                "account": acc["label"],
                "owner": acc.get("owner_user_name"),
                "count": len(messages),
                "messages": messages,
            }
        except Exception as e:
            return {"account": acc["label"], "owner": acc.get("owner_user_name"), "error": str(e), "messages": []}
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except Exception:
                    pass

    @classmethod
    def _run_imap_search_pass(cls, imap: imaplib.IMAP4_SSL, q_clean: str, q_tokens: List[str], days: int, limit: int) -> List[bytes]:
        since = _format_imap_date(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)) if days > 0 else None
        candidate_uids: List[bytes] = []

        if q_clean.isascii():
            ascii_words = [w.replace('"', "") for w in re.findall(r"[A-Za-z0-9@._-]+", q_clean) if len(w) >= 2]
            if ascii_words:
                try:
                    criteria: List[str] = []
                    if since:
                        criteria.extend(["SINCE", since])
                    for w in ascii_words[:5]:
                        criteria.extend(["TEXT", f'"{w}"'])
                    typ, data = imap.uid("search", None, *criteria)
                    if typ == "OK" and data and data[0]:
                        candidate_uids = data[0].split()[-limit:][::-1]
                except Exception:
                    candidate_uids = []

        if not candidate_uids:
            if since:
                typ, data = imap.uid("search", None, "SINCE", since)
            else:
                typ, data = imap.uid("search", None, "ALL")
            all_uids = (data[0] or b"").split() if typ == "OK" and data else []
            recent_uids = all_uids[-80:][::-1]
            hdr_matched: List[bytes] = []
            body_fallback_pool: List[bytes] = []

            for uid_b in recent_uids:
                ftyp, fdata = imap.uid("fetch", uid_b, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if ftyp != "OK":
                    continue
                msg = email.message_from_bytes(_extract_bytes_from_fetch(fdata))
                hay = f"{_decode_mime_header(msg.get('From'))} {_decode_mime_header(msg.get('Subject'))}".lower()
                if q_tokens and all(tok in hay for tok in q_tokens):
                    hdr_matched.append(uid_b)
                    if len(hdr_matched) >= limit:
                        break
                elif len(body_fallback_pool) < 18:
                    body_fallback_pool.append(uid_b)

            candidate_uids = hdr_matched
            if len(candidate_uids) < limit and body_fallback_pool:
                for uid_b in body_fallback_pool:
                    ftyp, fdata = imap.uid("fetch", uid_b, "(BODY.PEEK[])")
                    if ftyp != "OK":
                        continue
                    msg = email.message_from_bytes(_extract_bytes_from_fetch(fdata))
                    body = _extract_text_body(msg, max_len=1200).lower()
                    if q_tokens and all(tok in body for tok in q_tokens):
                        candidate_uids.append(uid_b)
                        if len(candidate_uids) >= limit:
                            break
        return candidate_uids

    @classmethod
    def _sync_search(cls, acc: Dict[str, Any], query: str, days: Optional[int], limit: int) -> List[Dict[str, Any]]:
        imap = None
        try:
            imap = cls._open_imap(acc)
            q_clean = (query or "").strip()
            q_tokens = [t for t in re.findall(r"[0-9a-zа-яё@._-]+", q_clean.lower()) if len(t) >= 2]

            if days is None:
                # Прогрессивный поиск: сначала последние 30 дней; если меньше limit — добираем из всего ящика
                candidate_uids = cls._run_imap_search_pass(imap, q_clean, q_tokens, 30, limit)
                if len(candidate_uids) < limit:
                    older_uids = cls._run_imap_search_pass(imap, q_clean, q_tokens, 0, limit)
                    seen = set(candidate_uids)
                    for uid_b in older_uids:
                        if uid_b not in seen:
                            candidate_uids.append(uid_b)
                            seen.add(uid_b)
                            if len(candidate_uids) >= limit:
                                break
            else:
                candidate_uids = cls._run_imap_search_pass(imap, q_clean, q_tokens, days, limit)

            results: List[Dict[str, Any]] = []
            for uid_b in candidate_uids[:limit]:
                ftyp, fdata = imap.uid("fetch", uid_b, "(BODY.PEEK[])")
                if ftyp != "OK":
                    continue
                msg = email.message_from_bytes(_extract_bytes_from_fetch(fdata))
                results.append({
                    "uid": uid_b.decode("ascii", errors="ignore"),
                    "account_id": acc["id"],
                    "account": acc["label"],
                    "owner": acc.get("owner_user_name"),
                    "from": _decode_mime_header(msg.get("From")),
                    "subject": _decode_mime_header(msg.get("Subject")) or "(без темы)",
                    "date": _format_msg_date(msg.get("Date")),
                    "snippet": _extract_text_body(msg, max_len=2500),
                })
            return results
        except Exception as e:
            logger.warning(f"[MAIL] Search error in {acc.get('label')}: {e}")
            return [{"account": acc["label"], "owner": acc.get("owner_user_name"), "error": str(e)}]
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except Exception:
                    pass

    @classmethod
    def _sync_read_message(cls, acc: Dict[str, Any], uid: str) -> Optional[Dict[str, Any]]:
        imap = None
        try:
            imap = cls._open_imap(acc)
            ftyp, fdata = imap.uid("fetch", uid.encode("ascii"), "(BODY.PEEK[])")
            raw = _extract_bytes_from_fetch(fdata) if ftyp == "OK" else b""
            if not raw:
                return None
            msg = email.message_from_bytes(raw)
            return {
                "status": "ok",
                "uid": uid,
                "account": acc["label"],
                "owner": acc.get("owner_user_name"),
                "from": _decode_mime_header(msg.get("From")),
                "to": _decode_mime_header(msg.get("To")),
                "subject": _decode_mime_header(msg.get("Subject")) or "(без темы)",
                "date": _format_msg_date(msg.get("Date")),
                "body": _extract_text_body(msg, max_len=2500),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except Exception:
                    pass

    async def execute_tool(self, tool_name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        provider = ctx.get("provider") or {}
        owner = str(args.get("owner") or "").strip()
        account_id = str(args.get("account_id") or "").strip()
        accounts = self._select_accounts(provider, owner=owner, account_id=account_id)
        if not accounts:
            return {
                "status": "error",
                "error": (
                    "Не найдены настроенные почтовые ящики IMAP"
                    + (f" для '{owner}'" if owner else ". Добавьте ящик во вкладке «Плагины → Почта (IMAP)».")
                ),
            }

        if tool_name == "mail_list_recent":
            days = max(0, min(int(args.get("days") if args.get("days") is not None else 3), 30))
            limit = max(1, min(int(args.get("limit") or 10), 25))
            unread_only = bool(args.get("unread_only", False))
            mailboxes = await asyncio.gather(
                *(asyncio.to_thread(self._sync_list_recent, acc, days, limit, unread_only) for acc in accounts)
            )
            total = sum(int(m.get("count") or 0) for m in mailboxes)
            return {"status": "ok", "count": total, "mailboxes": list(mailboxes)}

        if tool_name == "mail_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return {"status": "error", "error": "Укажите поисковый запрос query"}
            raw_days = args.get("days")
            days = max(0, min(int(raw_days), 3650)) if raw_days is not None else None
            limit = max(1, min(int(args.get("limit") or 10), 15))
            nested = await asyncio.gather(
                *(asyncio.to_thread(self._sync_search, acc, query, days, limit) for acc in accounts)
            )
            flat = [item for sub in nested for item in sub]
            return {"status": "ok", "count": len(flat), "results": flat[:limit]}

        if tool_name == "mail_read_message":
            uid = str(args.get("uid") or "").strip()
            if not uid:
                return {"status": "error", "error": "Укажите uid письма"}
            for acc in accounts:
                res = await asyncio.to_thread(self._sync_read_message, acc, uid)
                if res and res.get("status") == "ok":
                    return res
            return {"status": "not_found", "error": f"Письмо с UID '{uid}' не найдено"}

        return {"status": "error", "error": f"Unknown tool: {tool_name}"}
