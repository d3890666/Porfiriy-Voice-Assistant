from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger("db")

SECRET_KEY_MARKERS = ("pass", "token", "secret", "hash", "key", "session")
MASK_VALUE = "••••••••"

_CYRILLIC_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _default_db_path() -> str:
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/porfiriy.db"
    return os.path.join(os.path.dirname(__file__), "porfiriy.db")


def _is_secret_key(key: str) -> bool:
    low = (key or "").lower()
    return any(m in low for m in SECRET_KEY_MARKERS)


def _mask_dict(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in data.items():
        if _is_secret_key(k):
            out[k] = MASK_VALUE if v else ""
        elif isinstance(v, dict):
            out[k] = _mask_dict(v)
        else:
            out[k] = v
    return out


def _merge_preserving_secrets(existing: Optional[Dict[str, Any]], incoming: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = dict(existing or {})
    if not isinstance(incoming, dict):
        return base
    for k, v in incoming.items():
        if _is_secret_key(k) and v == MASK_VALUE:
            # Не перезаписываем сохранённый секрет маской с UI
            continue
        base[k] = v
    return base


def _slugify(text: str, prefix: str = "user") -> str:
    raw = (text or "").strip().lower()
    trans = "".join(_CYRILLIC_MAP.get(ch, ch) for ch in raw)
    slug = re.sub(r"[^a-z0-9_-]+", "_", trans).strip("_-")
    if not slug:
        slug = f"{prefix}_{uuid.uuid4().hex[:6]}"
    return slug[:48]


class PorfiriyDB:
    """Единая локальная SQLite БД Порфирия 2.0 (пользователи, каналы, плагины, память, расписание, аудит)."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _default_db_path()
        self._lock = threading.RLock()
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.create_function("py_lower", 1, lambda s: str(s).lower() if s is not None else "")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS users (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        gender TEXT DEFAULT 'unknown',
                        voice_notes TEXT DEFAULT '',
                        persona_notes TEXT DEFAULT '',
                        voice_embedding BLOB DEFAULT NULL,
                        is_active INTEGER DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS user_channels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
                        channel_type TEXT NOT NULL,
                        channel_value TEXT NOT NULL,
                        label TEXT DEFAULT '',
                        is_primary INTEGER DEFAULT 1,
                        UNIQUE(user_id, channel_type, channel_value)
                    );

                    CREATE TABLE IF NOT EXISTS tool_providers (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        title TEXT NOT NULL,
                        description TEXT DEFAULT '',
                        enabled INTEGER DEFAULT 0,
                        config_json TEXT DEFAULT '{}',
                        cached_tools_json TEXT DEFAULT '[]',
                        last_sync_at TEXT,
                        status TEXT DEFAULT 'idle',
                        last_error TEXT DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS provider_accounts (
                        id TEXT PRIMARY KEY,
                        provider_id TEXT NOT NULL REFERENCES tool_providers(id) ON DELETE CASCADE,
                        owner_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                        label TEXT NOT NULL,
                        credentials_json TEXT DEFAULT '{}',
                        settings_json TEXT DEFAULT '{}',
                        enabled INTEGER DEFAULT 1,
                        status TEXT DEFAULT 'idle',
                        last_error TEXT DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS memory_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        owner_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                        kind TEXT NOT NULL,
                        list_name TEXT DEFAULT '',
                        title TEXT DEFAULT '',
                        content TEXT NOT NULL,
                        tags TEXT DEFAULT '',
                        priority INTEGER DEFAULT 1,
                        is_pinned INTEGER DEFAULT 0,
                        status TEXT DEFAULT 'active',
                        expires_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_accessed_at TEXT,
                        access_count INTEGER DEFAULT 0
                    );

                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                        title,
                        content,
                        tags,
                        content='memory_items',
                        content_rowid='id',
                        tokenize='unicode61'
                    );

                    CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_items BEGIN
                        INSERT INTO memory_fts(rowid, title, content, tags)
                        VALUES (new.id, new.title, new.content, new.tags);
                    END;

                    CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_items BEGIN
                        INSERT INTO memory_fts(memory_fts, rowid, title, content, tags)
                        VALUES ('delete', old.id, old.title, old.content, old.tags);
                    END;

                    CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_items BEGIN
                        INSERT INTO memory_fts(memory_fts, rowid, title, content, tags)
                        VALUES ('delete', old.id, old.title, old.content, old.tags);
                        INSERT INTO memory_fts(rowid, title, content, tags)
                        VALUES (new.id, new.title, new.content, new.tags);
                    END;

                    CREATE TABLE IF NOT EXISTS memory_cleanup_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_at TEXT NOT NULL,
                        expired_count INTEGER DEFAULT 0,
                        archived_count INTEGER DEFAULT 0,
                        merged_count INTEGER DEFAULT 0,
                        summary TEXT DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS scheduled_jobs (
                        id TEXT PRIMARY KEY,
                        job_type TEXT NOT NULL,
                        owner_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                        cron_or_run_at TEXT NOT NULL,
                        payload_json TEXT DEFAULT '{}',
                        enabled INTEGER DEFAULT 1,
                        last_run_at TEXT,
                        next_run_at TEXT,
                        last_status TEXT DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS tool_audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        provider_id TEXT,
                        tool_name TEXT NOT NULL,
                        speaker_user_id TEXT,
                        device_mac TEXT,
                        args_preview TEXT DEFAULT '',
                        status TEXT NOT NULL,
                        duration_ms INTEGER DEFAULT 0,
                        error TEXT DEFAULT ''
                    );

                    CREATE INDEX IF NOT EXISTS idx_user_channels_user ON user_channels(user_id);
                    CREATE INDEX IF NOT EXISTS idx_provider_accounts_provider ON provider_accounts(provider_id);
                    CREATE INDEX IF NOT EXISTS idx_provider_accounts_owner ON provider_accounts(owner_user_id);
                    CREATE INDEX IF NOT EXISTS idx_memory_status_kind ON memory_items(status, kind);
                    CREATE INDEX IF NOT EXISTS idx_memory_owner ON memory_items(owner_user_id);
                    CREATE INDEX IF NOT EXISTS idx_memory_expires ON memory_items(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_audit_ts ON tool_audit_log(ts);
                    """
                )
                now = _now_iso()
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                    (now,),
                )
                self._seed_defaults(conn, now)
                conn.commit()
            finally:
                conn.close()

    def _seed_defaults(self, conn: sqlite3.Connection, now: str) -> None:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if user_count == 0:
            default_users = [
                ("denis", "Денис", "male", "мужской голос, низкий тембр", "хозяин дома, IT-разработчик"),
                ("sveta", "Света", "female", "женский голос, более высокий тембр", "жена Дениса"),
            ]
            for uid, name, gender, voice_notes, persona_notes in default_users:
                conn.execute(
                    """
                    INSERT INTO users(id, name, gender, voice_notes, persona_notes, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (uid, name, gender, voice_notes, persona_notes, now, now),
                )

        builtin_providers = [
            (
                "builtin.telegram_bot",
                "builtin",
                "Telegram-бот (отправка в чаты)",
                "Отправка сообщений, рецептов, ссылок и напоминаний в личные чаты жильцов или общий чат дома. Бот и чтение личных чатов разделены.",
                json.dumps({"mode": "ha_notify", "ha_service": "notify.telegram", "bot_token": ""}, ensure_ascii=False),
            ),
            (
                "builtin.memory",
                "builtin",
                "Память и списки (SQLite + FTS5)",
                "Долгосрочная память фактов, заметки и общие/личные списки покупок и дел с авто-очисткой и TTL.",
                json.dumps({"auto_cleanup_days": 30, "archive_days": 14, "max_active_items": 500}, ensure_ascii=False),
            ),
            (
                "builtin.telegram_userbot",
                "builtin",
                "Telegram Userbot (чтение чатов)",
                "Подключение личных аккаунтов Telegram (Telethon MTProto) для чтения и поиска по перепискам и каналам.",
                json.dumps({"max_messages_per_query": 20}, ensure_ascii=False),
            ),
            (
                "builtin.mail_imap",
                "builtin",
                "Почта (IMAP)",
                "Чтение входящих писем и поиск кодов подтверждения из личных и общих почтовых ящиков по IMAP.",
                json.dumps({"default_limit": 10}, ensure_ascii=False),
            ),
        ]
        for pid, kind, title, desc, cfg in builtin_providers:
            conn.execute(
                """
                INSERT OR IGNORE INTO tool_providers(
                    id, kind, title, description, enabled, config_json, cached_tools_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, '[]', 'idle', ?, ?)
                """,
                (pid, kind, title, desc, cfg, now, now),
            )

    # ------------------------------------------------------------------
    # Users & Channels
    # ------------------------------------------------------------------
    def _row_to_user(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
        uid = row["id"]
        ch_rows = conn.execute(
            "SELECT id, channel_type, channel_value, label, is_primary FROM user_channels WHERE user_id = ? ORDER BY id",
            (uid,),
        ).fetchall()
        channels = [dict(c) for c in ch_rows]
        tg_chat = next((c["channel_value"] for c in channels if c["channel_type"] == "telegram_chat"), "")
        acc_count = conn.execute(
            "SELECT COUNT(*) FROM provider_accounts WHERE owner_user_id = ?", (uid,)
        ).fetchone()[0]
        mem_count = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE owner_user_id = ? AND status = 'active'", (uid,)
        ).fetchone()[0]
        return {
            "id": uid,
            "name": row["name"],
            "gender": row["gender"] or "unknown",
            "voice_notes": row["voice_notes"] or "",
            "persona_notes": row["persona_notes"] or "",
            "is_active": bool(row["is_active"]),
            "telegram_chat_id": tg_chat,
            "channels": channels,
            "linked_accounts_count": int(acc_count),
            "memory_items_count": int(mem_count),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_users(self, active_only: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                sql = "SELECT * FROM users"
                if active_only:
                    sql += " WHERE is_active = 1"
                sql += " ORDER BY created_at ASC, name ASC"
                rows = conn.execute(sql).fetchall()
                return [self._row_to_user(conn, r) for r in rows]
            finally:
                conn.close()

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        uid = (user_id or "").strip().lower()
        if not uid:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                return self._row_to_user(conn, row) if row else None
            finally:
                conn.close()

    def upsert_user(
        self,
        *,
        user_id: Optional[str] = None,
        name: str,
        gender: str = "unknown",
        voice_notes: str = "",
        persona_notes: str = "",
        telegram_chat_id: Optional[str] = None,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Имя пользователя не может быть пустым")
        uid = _slugify(user_id or clean_name, prefix="user")
        clean_gender = (gender or "unknown").strip().lower()
        if clean_gender not in ("male", "female", "unknown"):
            clean_gender = "unknown"
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                exists = conn.execute("SELECT id FROM users WHERE id = ?", (uid,)).fetchone()
                if exists:
                    conn.execute(
                        """
                        UPDATE users
                        SET name = ?, gender = ?, voice_notes = ?, persona_notes = ?, is_active = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (clean_name, clean_gender, voice_notes or "", persona_notes or "", int(bool(is_active)), now, uid),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO users(id, name, gender, voice_notes, persona_notes, is_active, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (uid, clean_name, clean_gender, voice_notes or "", persona_notes or "", int(bool(is_active)), now, now),
                    )

                if telegram_chat_id is not None:
                    tg_val = str(telegram_chat_id).strip()
                    conn.execute(
                        "DELETE FROM user_channels WHERE user_id = ? AND channel_type = 'telegram_chat'",
                        (uid,),
                    )
                    if tg_val:
                        conn.execute(
                            """
                            INSERT INTO user_channels(user_id, channel_type, channel_value, label, is_primary)
                            VALUES (?, 'telegram_chat', ?, 'Личный Telegram', 1)
                            """,
                            (uid, tg_val),
                        )

                conn.commit()
                row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                return self._row_to_user(conn, row)
            finally:
                conn.close()

    def delete_user(self, user_id: str) -> bool:
        uid = (user_id or "").strip().lower()
        if not uid:
            return False
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM users WHERE id = ?", (uid,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_family_channels(self) -> Dict[str, str]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT channel_type, channel_value FROM user_channels WHERE user_id IS NULL ORDER BY id"
                ).fetchall()
                return {r["channel_type"]: r["channel_value"] for r in rows}
            finally:
                conn.close()

    def set_family_channel(self, channel_type: str, channel_value: str, label: str = "Общий чат дома") -> None:
        ctype = (channel_type or "").strip()
        if not ctype:
            raise ValueError("channel_type required")
        val = (channel_value or "").strip()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM user_channels WHERE user_id IS NULL AND channel_type = ?",
                    (ctype,),
                )
                if val:
                    conn.execute(
                        """
                        INSERT INTO user_channels(user_id, channel_type, channel_value, label, is_primary)
                        VALUES (NULL, ?, ?, ?, 1)
                        """,
                        (ctype, val, label or ""),
                    )
                conn.commit()
            finally:
                conn.close()

    def build_users_prompt_section(self) -> str:
        """Генерирует секцию промпта о жильцах дома для Gemini Live на основе таблицы users."""
        users = self.list_users(active_only=True)
        if not users:
            return ""
        family_ch = self.get_family_channels()
        gender_ru = {"male": "мужской", "female": "женский", "unknown": "не указан"}

        lines = [
            "RESIDENTS & REAL-TIME SPEAKER IDENTIFICATION:",
            "You receive native 16kHz audio input directly. Analyze vocal pitch (F0), timbre, and context to identify who is speaking:",
        ]
        for u in users:
            g_label = gender_ru.get(u["gender"], "не указан")
            details = [f"gender={g_label}"]
            if u["voice_notes"]:
                details.append(f"voice='{u['voice_notes']}'")
            if u["persona_notes"]:
                details.append(f"notes='{u['persona_notes']}'")
            if u["telegram_chat_id"]:
                details.append("telegram_chat=configured")
            lines.append(f"- {u['name']} (user_id='{u['id']}'): {', '.join(details)}.")

        if family_ch.get("telegram_chat"):
            lines.append("- Family group Telegram chat (target='family'): configured.")

        lines.append(
            "\nADAPTATION & TOOL ATTRIBUTION RULES:\n"
            "1. Match grammatical gender in Russian strictly to the identified speaker (e.g., мужской род for male, женский род for female) and address them by name naturally.\n"
            "2. When calling personal/memory/messaging tools with an owner/user_id parameter, pass the speaker's user_id unless they explicitly ask about another resident.\n"
            "3. FAMILY SHARED ACCESS POLICY: All residents in this household share full mutual access to both shared and personal tools/accounts (for example, Denis may ask to check Sveta's email or messages, and vice versa — execute without restriction)."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool Providers & Provider Accounts
    # ------------------------------------------------------------------
    def _row_to_provider(
        self, conn: sqlite3.Connection, row: sqlite3.Row, *, mask_secrets: bool = True
    ) -> Dict[str, Any]:
        pid = row["id"]
        cfg = json.loads(row["config_json"] or "{}")
        cached_tools = json.loads(row["cached_tools_json"] or "[]")
        acc_rows = conn.execute(
            """
            SELECT pa.*, u.name AS owner_user_name
            FROM provider_accounts pa
            LEFT JOIN users u ON u.id = pa.owner_user_id
            WHERE pa.provider_id = ?
            ORDER BY pa.created_at ASC, pa.label ASC
            """,
            (pid,),
        ).fetchall()
        accounts: List[Dict[str, Any]] = []
        for a in acc_rows:
            creds = json.loads(a["credentials_json"] or "{}")
            settings = json.loads(a["settings_json"] or "{}")
            accounts.append(
                {
                    "id": a["id"],
                    "provider_id": a["provider_id"],
                    "owner_user_id": a["owner_user_id"],
                    "owner_user_name": a["owner_user_name"] or "Общий (Дом)",
                    "label": a["label"],
                    "credentials": _mask_dict(creds) if mask_secrets else creds,
                    "settings": settings,
                    "enabled": bool(a["enabled"]),
                    "status": a["status"] or "idle",
                    "last_error": a["last_error"] or "",
                    "created_at": a["created_at"],
                    "updated_at": a["updated_at"],
                }
            )
        return {
            "id": pid,
            "kind": row["kind"],
            "title": row["title"],
            "description": row["description"] or "",
            "enabled": bool(row["enabled"]),
            "config": _mask_dict(cfg) if mask_secrets else cfg,
            "cached_tools": cached_tools,
            "last_sync_at": row["last_sync_at"],
            "status": row["status"] or "idle",
            "last_error": row["last_error"] or "",
            "accounts": accounts,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_providers(self, *, mask_secrets: bool = True, enabled_only: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                sql = "SELECT * FROM tool_providers"
                if enabled_only:
                    sql += " WHERE enabled = 1"
                sql += " ORDER BY CASE kind WHEN 'builtin' THEN 0 ELSE 1 END, created_at ASC, title ASC"
                rows = conn.execute(sql).fetchall()
                return [self._row_to_provider(conn, r, mask_secrets=mask_secrets) for r in rows]
            finally:
                conn.close()

    def get_provider(self, provider_id: str, *, mask_secrets: bool = False) -> Optional[Dict[str, Any]]:
        pid = (provider_id or "").strip()
        if not pid:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM tool_providers WHERE id = ?", (pid,)).fetchone()
                return self._row_to_provider(conn, row, mask_secrets=mask_secrets) if row else None
            finally:
                conn.close()

    def upsert_provider(
        self,
        *,
        provider_id: Optional[str] = None,
        kind: str,
        title: str,
        description: str = "",
        enabled: Optional[bool] = None,
        config: Optional[Dict[str, Any]] = None,
        cached_tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        clean_kind = (kind or "").strip()
        if clean_kind not in ("builtin", "mcp_http", "mcp_stdio", "llm_delegate"):
            raise ValueError(f"Unsupported provider kind: {clean_kind}")
        clean_title = (title or "").strip()
        if not clean_title:
            raise ValueError("Название провайдера не может быть пустым")
        pid = (provider_id or "").strip()
        if not pid:
            prefix = "mcp" if clean_kind.startswith("mcp") else ("llm" if clean_kind == "llm_delegate" else "custom")
            pid = f"{prefix}.{_slugify(clean_title, prefix=prefix)}"
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM tool_providers WHERE id = ?", (pid,)).fetchone()
                if row:
                    old_cfg = json.loads(row["config_json"] or "{}")
                    merged_cfg = _merge_preserving_secrets(old_cfg, config) if config is not None else old_cfg
                    new_enabled = int(bool(enabled)) if enabled is not None else row["enabled"]
                    tools_json = (
                        json.dumps(cached_tools, ensure_ascii=False)
                        if cached_tools is not None
                        else row["cached_tools_json"]
                    )
                    conn.execute(
                        """
                        UPDATE tool_providers
                        SET title = ?, description = ?, enabled = ?, config_json = ?, cached_tools_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            clean_title,
                            description if description is not None else (row["description"] or ""),
                            new_enabled,
                            json.dumps(merged_cfg, ensure_ascii=False),
                            tools_json,
                            now,
                            pid,
                        ),
                    )
                else:
                    clean_cfg = _merge_preserving_secrets({}, config)
                    tools_json = json.dumps(cached_tools or [], ensure_ascii=False)
                    conn.execute(
                        """
                        INSERT INTO tool_providers(
                            id, kind, title, description, enabled, config_json, cached_tools_json, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)
                        """,
                        (
                            pid,
                            clean_kind,
                            clean_title,
                            description or "",
                            int(bool(enabled)) if enabled is not None else 1,
                            json.dumps(clean_cfg, ensure_ascii=False),
                            tools_json,
                            now,
                            now,
                        ),
                    )
                conn.commit()
                updated = conn.execute("SELECT * FROM tool_providers WHERE id = ?", (pid,)).fetchone()
                return self._row_to_provider(conn, updated, mask_secrets=True)
            finally:
                conn.close()

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> bool:
        pid = (provider_id or "").strip()
        if not pid:
            return False
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE tool_providers SET enabled = ?, updated_at = ? WHERE id = ?",
                    (int(bool(enabled)), now, pid),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def delete_provider(self, provider_id: str) -> bool:
        pid = (provider_id or "").strip()
        if not pid:
            return False
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT kind FROM tool_providers WHERE id = ?", (pid,)).fetchone()
                if not row:
                    return False
                if row["kind"] == "builtin":
                    raise ValueError("Встроенные плагины нельзя удалять — их можно только отключить")
                cur = conn.execute("DELETE FROM tool_providers WHERE id = ?", (pid,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def upsert_provider_account(
        self,
        *,
        provider_id: str,
        label: str,
        owner_user_id: Optional[str] = None,
        credentials: Optional[Dict[str, Any]] = None,
        settings: Optional[Dict[str, Any]] = None,
        account_id: Optional[str] = None,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        pid = (provider_id or "").strip()
        clean_label = (label or "").strip()
        if not pid or not clean_label:
            raise ValueError("provider_id и label обязательны")
        owner = (owner_user_id or "").strip().lower() or None
        aid = (account_id or "").strip() or f"acc_{uuid.uuid4().hex[:10]}"
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                prov = conn.execute("SELECT id FROM tool_providers WHERE id = ?", (pid,)).fetchone()
                if not prov:
                    raise ValueError(f"Провайдер '{pid}' не найден")
                if owner:
                    u = conn.execute("SELECT id FROM users WHERE id = ?", (owner,)).fetchone()
                    if not u:
                        raise ValueError(f"Пользователь '{owner}' не найден")

                existing = conn.execute("SELECT * FROM provider_accounts WHERE id = ?", (aid,)).fetchone()
                if existing:
                    old_creds = json.loads(existing["credentials_json"] or "{}")
                    merged_creds = _merge_preserving_secrets(old_creds, credentials)
                    merged_settings = (
                        dict(settings) if settings is not None else json.loads(existing["settings_json"] or "{}")
                    )
                    conn.execute(
                        """
                        UPDATE provider_accounts
                        SET owner_user_id = ?, label = ?, credentials_json = ?, settings_json = ?, enabled = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            owner,
                            clean_label,
                            json.dumps(merged_creds, ensure_ascii=False),
                            json.dumps(merged_settings, ensure_ascii=False),
                            int(bool(enabled)),
                            now,
                            aid,
                        ),
                    )
                else:
                    clean_creds = _merge_preserving_secrets({}, credentials)
                    clean_settings = dict(settings or {})
                    conn.execute(
                        """
                        INSERT INTO provider_accounts(
                            id, provider_id, owner_user_id, label, credentials_json, settings_json, enabled, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)
                        """,
                        (
                            aid,
                            pid,
                            owner,
                            clean_label,
                            json.dumps(clean_creds, ensure_ascii=False),
                            json.dumps(clean_settings, ensure_ascii=False),
                            int(bool(enabled)),
                            now,
                            now,
                        ),
                    )
                conn.commit()
                row = conn.execute(
                    """
                    SELECT pa.*, u.name AS owner_user_name
                    FROM provider_accounts pa
                    LEFT JOIN users u ON u.id = pa.owner_user_id
                    WHERE pa.id = ?
                    """,
                    (aid,),
                ).fetchone()
                creds = json.loads(row["credentials_json"] or "{}")
                return {
                    "id": row["id"],
                    "provider_id": row["provider_id"],
                    "owner_user_id": row["owner_user_id"],
                    "owner_user_name": row["owner_user_name"] or "Общий (Дом)",
                    "label": row["label"],
                    "credentials": _mask_dict(creds),
                    "settings": json.loads(row["settings_json"] or "{}"),
                    "enabled": bool(row["enabled"]),
                    "status": row["status"] or "idle",
                    "last_error": row["last_error"] or "",
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            finally:
                conn.close()

    def delete_provider_account(self, account_id: str) -> bool:
        aid = (account_id or "").strip()
        if not aid:
            return False
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM provider_accounts WHERE id = ?", (aid,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_provider_account(self, account_id: str, *, mask_secrets: bool = False) -> Optional[Dict[str, Any]]:
        aid = (account_id or "").strip()
        if not aid:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT pa.*, u.name AS owner_user_name
                    FROM provider_accounts pa
                    LEFT JOIN users u ON u.id = pa.owner_user_id
                    WHERE pa.id = ?
                    """,
                    (aid,),
                ).fetchone()
                if not row:
                    return None
                creds = json.loads(row["credentials_json"] or "{}")
                return {
                    "id": row["id"],
                    "provider_id": row["provider_id"],
                    "owner_user_id": row["owner_user_id"],
                    "owner_user_name": row["owner_user_name"] or "Общий (Дом)",
                    "label": row["label"],
                    "credentials": _mask_dict(creds) if mask_secrets else creds,
                    "settings": json.loads(row["settings_json"] or "{}"),
                    "enabled": bool(row["enabled"]),
                    "status": row["status"] or "idle",
                    "last_error": row["last_error"] or "",
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            finally:
                conn.close()

    def update_provider_account_status(
        self,
        account_id: str,
        status: str,
        last_error: str = "",
        extra_credentials: Optional[Dict[str, Any]] = None,
    ) -> bool:
        aid = (account_id or "").strip()
        if not aid:
            return False
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT credentials_json FROM provider_accounts WHERE id = ?", (aid,)).fetchone()
                if not row:
                    return False
                creds = json.loads(row["credentials_json"] or "{}")
                if extra_credentials:
                    creds.update(extra_credentials)
                conn.execute(
                    """
                    UPDATE provider_accounts
                    SET status = ?, last_error = ?, credentials_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status or "idle", (last_error or "")[:500], json.dumps(creds, ensure_ascii=False), now, aid),
                )
                conn.commit()
                return True
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Tool Audit Log
    # ------------------------------------------------------------------
    def log_tool_call(
        self,
        *,
        tool_name: str,
        provider_id: str = "",
        speaker_user_id: str = "",
        device_mac: str = "",
        args_preview: str = "",
        status: str = "ok",
        duration_ms: int = 0,
        error: str = "",
    ) -> None:
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO tool_audit_log(
                        ts, provider_id, tool_name, speaker_user_id, device_mac, args_preview, status, duration_ms, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        provider_id or "",
                        tool_name,
                        speaker_user_id or "",
                        device_mac or "",
                        (args_preview or "")[:240],
                        status,
                        int(duration_ms),
                        (error or "")[:300],
                    ),
                )
                # Оставляем последние 500 записей
                conn.execute(
                    """
                    DELETE FROM tool_audit_log
                    WHERE id NOT IN (
                        SELECT id FROM tool_audit_log ORDER BY id DESC LIMIT 500
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def list_audit_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), 500))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM tool_audit_log ORDER BY id DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Memory & Lists (SQLite + FTS5 + Smart Cleanup)
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "owner_user_id": row["owner_user_id"],
            "owner_user_name": row["owner_user_name"] if "owner_user_name" in row.keys() and row["owner_user_name"] else "Общий (Дом)",
            "kind": row["kind"],
            "list_name": row["list_name"] or "",
            "title": row["title"] or "",
            "content": row["content"] or "",
            "tags": row["tags"] or "",
            "priority": int(row["priority"] or 1),
            "is_pinned": bool(row["is_pinned"]),
            "status": row["status"] or "active",
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_accessed_at": row["last_accessed_at"],
            "access_count": int(row["access_count"] or 0),
        }

    def add_memory_item(
        self,
        *,
        kind: str,
        content: str,
        owner_user_id: Optional[str] = None,
        list_name: str = "",
        title: str = "",
        tags: str = "",
        priority: int = 1,
        is_pinned: bool = False,
        ttl_days: Optional[int] = None,
        expires_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_kind = (kind or "fact").strip().lower()
        if clean_kind not in ("fact", "note", "list_item", "preference"):
            raise ValueError(f"Unsupported memory kind: {clean_kind}")
        clean_content = (content or "").strip()
        if not clean_content:
            raise ValueError("Содержимое памяти не может быть пустым")

        owner = (owner_user_id or "").strip().lower() or None
        clean_list = (list_name or "").strip().lower() if clean_kind == "list_item" else (list_name or "").strip()
        if clean_kind == "list_item" and not clean_list:
            clean_list = "покупки"
        clean_title = (title or "").strip()
        clean_tags = (tags or "").strip()
        now_dt = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()

        calc_expires = expires_at
        if not calc_expires and ttl_days and int(ttl_days) > 0:
            calc_expires = (now_dt + datetime.timedelta(days=int(ttl_days))).isoformat()

        with self._lock:
            conn = self._connect()
            try:
                if owner:
                    u = conn.execute("SELECT id FROM users WHERE id = ?", (owner,)).fetchone()
                    if not u:
                        owner = None

                # Дедупликация для элементов списков
                if clean_kind == "list_item":
                    dup = conn.execute(
                        """
                        SELECT m.*, u.name AS owner_user_name
                        FROM memory_items m
                        LEFT JOIN users u ON u.id = m.owner_user_id
                        WHERE m.kind = 'list_item' AND m.status = 'active'
                          AND py_lower(m.list_name) = ? AND py_lower(trim(m.content)) = ?
                        LIMIT 1
                        """,
                        (clean_list.lower(), clean_content.lower()),
                    ).fetchone()
                    if dup:
                        conn.execute(
                            "UPDATE memory_items SET updated_at = ?, last_accessed_at = ? WHERE id = ?",
                            (now, now, dup["id"]),
                        )
                        conn.commit()
                        row = conn.execute(
                            """
                            SELECT m.*, u.name AS owner_user_name
                            FROM memory_items m LEFT JOIN users u ON u.id = m.owner_user_id
                            WHERE m.id = ?
                            """,
                            (dup["id"],),
                        ).fetchone()
                        res = self._row_to_memory(row)
                        res["deduplicated"] = True
                        return res

                # Обновление факта/предпочтения с тем же title у того же владельца
                if clean_kind in ("fact", "preference") and clean_title:
                    existing = conn.execute(
                        """
                        SELECT id FROM memory_items
                        WHERE kind = ? AND status = 'active'
                          AND COALESCE(owner_user_id, '') = COALESCE(?, '')
                          AND py_lower(trim(title)) = ?
                        LIMIT 1
                        """,
                        (clean_kind, owner, clean_title.lower()),
                    ).fetchone()
                    if existing:
                        conn.execute(
                            """
                            UPDATE memory_items
                            SET content = ?, tags = ?, priority = ?, is_pinned = ?, expires_at = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                clean_content,
                                clean_tags,
                                int(priority or 1),
                                int(bool(is_pinned)),
                                calc_expires,
                                now,
                                existing["id"],
                            ),
                        )
                        conn.commit()
                        row = conn.execute(
                            """
                            SELECT m.*, u.name AS owner_user_name
                            FROM memory_items m LEFT JOIN users u ON u.id = m.owner_user_id
                            WHERE m.id = ?
                            """,
                            (existing["id"],),
                        ).fetchone()
                        res = self._row_to_memory(row)
                        res["updated"] = True
                        return res

                cur = conn.execute(
                    """
                    INSERT INTO memory_items(
                        owner_user_id, kind, list_name, title, content, tags, priority, is_pinned, status,
                        expires_at, created_at, updated_at, last_accessed_at, access_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, 0)
                    """,
                    (
                        owner,
                        clean_kind,
                        clean_list,
                        clean_title,
                        clean_content,
                        clean_tags,
                        int(priority or 1),
                        int(bool(is_pinned)),
                        calc_expires,
                        now,
                        now,
                        now,
                    ),
                )
                new_id = cur.lastrowid
                conn.commit()
                row = conn.execute(
                    """
                    SELECT m.*, u.name AS owner_user_name
                    FROM memory_items m LEFT JOIN users u ON u.id = m.owner_user_id
                    WHERE m.id = ?
                    """,
                    (new_id,),
                ).fetchone()
                return self._row_to_memory(row)
            finally:
                conn.close()

    @staticmethod
    def _build_fts_query(raw_query: str) -> str:
        tokens = re.findall(r"[0-9A-Za-zА-Яа-яЁё_]+", raw_query or "")
        parts = []
        for tok in tokens:
            t = tok.strip()
            if len(t) >= 2:
                parts.append(f'"{t}"*')
        return " OR ".join(parts)

    def search_memory(
        self,
        *,
        query: str = "",
        kind: Optional[str] = None,
        list_name: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        include_shared: bool = True,
        status: Optional[str] = "active",
        limit: int = 20,
        touch_access: bool = True,
    ) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 20), 200))
        where_clauses: List[str] = []
        params: List[Any] = []

        if status:
            where_clauses.append("m.status = ?")
            params.append(status)
        if kind:
            where_clauses.append("m.kind = ?")
            params.append(kind.strip().lower())
        if list_name:
            where_clauses.append("py_lower(m.list_name) = ?")
            params.append(list_name.strip().lower())
        if owner_user_id:
            if include_shared:
                where_clauses.append("(m.owner_user_id = ? OR m.owner_user_id IS NULL)")
            else:
                where_clauses.append("m.owner_user_id = ?")
            params.append(owner_user_id.strip().lower())

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        clean_query = (query or "").strip()

        with self._lock:
            conn = self._connect()
            try:
                rows: List[sqlite3.Row] = []
                if clean_query:
                    fts_q = self._build_fts_query(clean_query)
                    if fts_q:
                        try:
                            fts_where = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""
                            sql = f"""
                                SELECT m.*, u.name AS owner_user_name
                                FROM memory_fts
                                JOIN memory_items m ON m.id = memory_fts.rowid
                                LEFT JOIN users u ON u.id = m.owner_user_id
                                WHERE memory_fts MATCH ? {fts_where}
                                ORDER BY m.is_pinned DESC, bm25(memory_fts) ASC, m.updated_at DESC
                                LIMIT ?
                            """
                            rows = conn.execute(sql, [fts_q, *params, safe_limit]).fetchall()
                        except Exception as fe:
                            logger.debug(f"FTS5 search fallback to LIKE: {fe}")
                            rows = []

                    if not rows:
                        like_val = f"%{clean_query.lower()}%"
                        like_cond = "(py_lower(m.title) LIKE ? OR py_lower(m.content) LIKE ? OR py_lower(m.tags) LIKE ? OR py_lower(m.list_name) LIKE ?)"
                        all_conds = [like_cond] + where_clauses
                        sql = f"""
                            SELECT m.*, u.name AS owner_user_name
                            FROM memory_items m
                            LEFT JOIN users u ON u.id = m.owner_user_id
                            WHERE {' AND '.join(all_conds)}
                            ORDER BY m.is_pinned DESC, m.priority DESC, m.updated_at DESC
                            LIMIT ?
                        """
                        rows = conn.execute(sql, [like_val, like_val, like_val, like_val, *params, safe_limit]).fetchall()
                else:
                    sql = f"""
                        SELECT m.*, u.name AS owner_user_name
                        FROM memory_items m
                        LEFT JOIN users u ON u.id = m.owner_user_id
                        {where_sql}
                        ORDER BY m.is_pinned DESC, m.priority DESC, m.updated_at DESC
                        LIMIT ?
                    """
                    rows = conn.execute(sql, [*params, safe_limit]).fetchall()

                items = [self._row_to_memory(r) for r in rows]
                if touch_access and items:
                    now = _now_iso()
                    ids = [it["id"] for it in items]
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(
                        f"UPDATE memory_items SET last_accessed_at = ?, access_count = access_count + 1 WHERE id IN ({placeholders})",
                        [now, *ids],
                    )
                    conn.commit()
                return items
            finally:
                conn.close()

    def list_memory_lists(self) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT
                        py_lower(list_name) AS list_key,
                        MAX(list_name) AS list_name,
                        SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_count,
                        SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_count
                    FROM memory_items
                    WHERE kind = 'list_item' AND trim(list_name) != ''
                    GROUP BY py_lower(list_name)
                    ORDER BY active_count DESC, list_name ASC
                    """
                ).fetchall()
                return [
                    {
                        "list_name": r["list_name"] or r["list_key"],
                        "active_count": int(r["active_count"] or 0),
                        "done_count": int(r["done_count"] or 0),
                    }
                    for r in rows
                ]
            finally:
                conn.close()

    def update_memory_item(self, item_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {"status", "is_pinned", "content", "title", "list_name", "owner_user_id", "priority", "tags", "expires_at"}
        updates: List[str] = []
        params: List[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "is_pinned":
                v = int(bool(v))
            elif k == "owner_user_id":
                v = (str(v).strip().lower() if v else None)
            elif k == "priority":
                v = int(v or 1)
            updates.append(f"{k} = ?")
            params.append(v)

        if not updates:
            return None
        updates.append("updated_at = ?")
        params.append(_now_iso())
        params.append(int(item_id))

        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(f"UPDATE memory_items SET {', '.join(updates)} WHERE id = ?", params)
                if cur.rowcount == 0:
                    return None
                conn.commit()
                row = conn.execute(
                    """
                    SELECT m.*, u.name AS owner_user_name
                    FROM memory_items m LEFT JOIN users u ON u.id = m.owner_user_id
                    WHERE m.id = ?
                    """,
                    (int(item_id),),
                ).fetchone()
                return self._row_to_memory(row) if row else None
            finally:
                conn.close()

    def complete_list_items(
        self,
        *,
        list_name: str = "",
        item_query: str = "",
        item_id: Optional[int] = None,
        clear_all: bool = False,
    ) -> Dict[str, Any]:
        now = _now_iso()
        clean_list = (list_name or "").strip().lower()
        clean_q = (item_query or "").strip().lower()

        with self._lock:
            conn = self._connect()
            try:
                if item_id is not None:
                    rows = conn.execute(
                        "SELECT id, content, list_name FROM memory_items WHERE id = ? AND status = 'active'",
                        (int(item_id),),
                    ).fetchall()
                elif clear_all:
                    if clean_list:
                        rows = conn.execute(
                            "SELECT id, content, list_name FROM memory_items WHERE kind = 'list_item' AND status = 'active' AND py_lower(list_name) = ?",
                            (clean_list,),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT id, content, list_name FROM memory_items WHERE kind = 'list_item' AND status = 'active'"
                        ).fetchall()
                else:
                    conds = ["kind = 'list_item'", "status = 'active'"]
                    params: List[Any] = []
                    if clean_list:
                        conds.append("py_lower(list_name) = ?")
                        params.append(clean_list)
                    if clean_q:
                        conds.append("(py_lower(content) LIKE ? OR py_lower(title) LIKE ?)")
                        params.extend([f"%{clean_q}%", f"%{clean_q}%"])
                    rows = conn.execute(
                        f"SELECT id, content, list_name FROM memory_items WHERE {' AND '.join(conds)}",
                        params,
                    ).fetchall()

                if not rows:
                    return {"updated_count": 0, "items": []}

                ids = [int(r["id"]) for r in rows]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE memory_items SET status = 'done', updated_at = ? WHERE id IN ({placeholders})",
                    [now, *ids],
                )
                conn.commit()
                return {
                    "updated_count": len(ids),
                    "items": [{"id": int(r["id"]), "content": r["content"], "list_name": r["list_name"]} for r in rows],
                }
            finally:
                conn.close()

    def delete_memory_item(self, item_id: int) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM memory_items WHERE id = ?", (int(item_id),))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def run_memory_cleanup(
        self,
        *,
        auto_cleanup_days: int = 30,
        archive_days: int = 14,
        max_active_items: int = 500,
    ) -> Dict[str, Any]:
        now_dt = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        decay_cutoff = (now_dt - datetime.timedelta(days=max(1, int(auto_cleanup_days)))).isoformat()
        done_cutoff = (now_dt - datetime.timedelta(days=max(1, int(archive_days)))).isoformat()
        purge_cutoff = (now_dt - datetime.timedelta(days=max(2, int(archive_days) * 2))).isoformat()
        max_active = max(20, int(max_active_items or 500))

        with self._lock:
            conn = self._connect()
            try:
                # 1. Просроченные по TTL
                c_exp = conn.execute(
                    """
                    UPDATE memory_items
                    SET status = 'archived', updated_at = ?
                    WHERE status = 'active' AND is_pinned = 0 AND expires_at IS NOT NULL AND expires_at <= ?
                    """,
                    (now, now),
                ).rowcount

                # 2. Выполненные старше archive_days
                c_done = conn.execute(
                    "DELETE FROM memory_items WHERE status = 'done' AND is_pinned = 0 AND updated_at <= ?",
                    (done_cutoff,),
                ).rowcount

                # 3. Затухание старых неиспользуемых заметок/списков
                c_decay = conn.execute(
                    """
                    UPDATE memory_items
                    SET status = 'archived', updated_at = ?
                    WHERE status = 'active' AND is_pinned = 0 AND priority <= 1
                      AND kind IN ('note', 'list_item')
                      AND COALESCE(last_accessed_at, updated_at) <= ?
                    """,
                    (now, decay_cutoff),
                ).rowcount

                # 4. Ограничение общего количества незакреплённых активных записей
                over_rows = conn.execute(
                    """
                    SELECT id FROM memory_items
                    WHERE status = 'active' AND is_pinned = 0
                    ORDER BY priority DESC, COALESCE(last_accessed_at, updated_at) DESC
                    LIMIT -1 OFFSET ?
                    """,
                    (max_active,),
                ).fetchall()
                c_cap = 0
                if over_rows:
                    over_ids = [int(r["id"]) for r in over_rows]
                    ph = ",".join("?" for _ in over_ids)
                    c_cap = conn.execute(
                        f"UPDATE memory_items SET status = 'archived', updated_at = ? WHERE id IN ({ph})",
                        [now, *over_ids],
                    ).rowcount

                # 5. Удаление старого архива
                c_purged = conn.execute(
                    "DELETE FROM memory_items WHERE status = 'archived' AND is_pinned = 0 AND updated_at <= ?",
                    (purge_cutoff,),
                ).rowcount

                archived_total = int(c_decay + c_cap)
                summary = f"expired={c_exp}, archived={archived_total}, deleted_done={c_done}, purged_archive={c_purged}"
                conn.execute(
                    """
                    INSERT INTO memory_cleanup_log(run_at, expired_count, archived_count, merged_count, summary)
                    VALUES (?, ?, ?, 0, ?)
                    """,
                    (now, int(c_exp), archived_total, summary),
                )
                conn.commit()
                return {
                    "run_at": now,
                    "expired_count": int(c_exp),
                    "decayed_count": int(c_decay),
                    "capped_count": int(c_cap),
                    "archived_count": archived_total,
                    "deleted_done_count": int(c_done),
                    "purged_archive_count": int(c_purged),
                    "summary": summary,
                }
            finally:
                conn.close()

    def get_memory_stats(self) -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS cnt FROM memory_items GROUP BY status"
                ).fetchall()
                by_status = {r["status"]: int(r["cnt"]) for r in rows}
                pinned = conn.execute(
                    "SELECT COUNT(*) FROM memory_items WHERE is_pinned = 1 AND status = 'active'"
                ).fetchone()[0]
                last_cleanup = conn.execute(
                    "SELECT * FROM memory_cleanup_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
                return {
                    "active": by_status.get("active", 0),
                    "done": by_status.get("done", 0),
                    "archived": by_status.get("archived", 0),
                    "pinned": int(pinned),
                    "total": sum(by_status.values()),
                    "last_cleanup": dict(last_cleanup) if last_cleanup else None,
                }
            finally:
                conn.close()


db_singleton = PorfiriyDB()
