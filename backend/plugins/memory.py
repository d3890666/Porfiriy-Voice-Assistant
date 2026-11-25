from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

try:
    from ..plugin_registry import PorfiriyPlugin
except ImportError:
    from plugin_registry import PorfiriyPlugin

logger = logging.getLogger("plugins.memory")

_SHARED_OWNER_WORDS = {"", "family", "home", "house", "all", "shared", "дом", "семья", "общий", "общее", "наш"}


def _resolve_owner_user_id(db: Any, raw_owner: Optional[str]) -> Optional[str]:
    low = (raw_owner or "").strip().lower()
    if low in _SHARED_OWNER_WORDS:
        return None
    for u in db.list_users(active_only=True):
        uid = (u.get("id") or "").lower()
        uname = (u.get("name") or "").lower()
        stem = uname[:-1] if len(uname) >= 4 else uname
        if low == uid or low == uname or (uid and low.startswith(uid)) or (stem and len(stem) >= 3 and low.startswith(stem)):
            return u.get("id")
    return None


class MemoryPlugin(PorfiriyPlugin):
    """Встроенный плагин долгосрочной памяти, фактов, заметок и списков (SQLite + FTS5 + TTL)."""

    provider_id = "builtin.memory"

    def get_tool_specs(self, db: Any, provider: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "name": "remember_info",
                "description": (
                    "Save a persistent fact, user preference, note, or task into long-term memory "
                    "(e.g. 'запомни код от домофона 1234', 'запомни, что у Светы аллергия на орехи', 'запиши заметку...')."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "content": {"type": "STRING", "description": "What to remember in clear Russian."},
                        "kind": {
                            "type": "STRING",
                            "description": "Type: 'fact' (permanent/long-term fact), 'preference' (taste/habit), or 'note' (temporary note).",
                        },
                        "title": {"type": "STRING", "description": "Optional short key/title for updating/deduplicating facts (e.g. 'Код от домофона')."},
                        "owner": {"type": "STRING", "description": "Owner user_id/name (e.g. 'denis', 'sveta') or 'family' for shared household info."},
                        "tags": {"type": "STRING", "description": "Optional comma-separated keywords/tags."},
                        "is_pinned": {"type": "BOOLEAN", "description": "Set true for critical permanent facts (codes, allergies, Wi-Fi, addresses) that must never expire."},
                        "ttl_days": {"type": "INTEGER", "description": "Optional auto-expiration in days for temporary notes/reminders (0 = use default decay)."},
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "recall_memory",
                "description": (
                    "Search and retrieve saved facts, preferences, notes, or list items from long-term memory "
                    "(e.g. 'какой код от домофона?', 'что я просил запомнить вчера?')."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "query": {"type": "STRING", "description": "Search keywords (Russian or Latin). Leave empty to list recent active memories."},
                        "kind": {"type": "STRING", "description": "Optional filter: 'fact', 'preference', 'note', 'list_item'."},
                        "list_name": {"type": "STRING", "description": "Optional list name filter."},
                        "owner": {"type": "STRING", "description": "Optional user_id/name filter."},
                        "limit": {"type": "INTEGER", "description": "Max results (default 15)."},
                    },
                },
            },
            {
                "name": "manage_list",
                "description": (
                    "Manage household or personal lists such as shopping lists ('покупки'), to-do lists ('дела'), packing lists, etc. "
                    "Supports getting items, adding one or multiple items, marking items done, clearing a list, or listing all lists."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "action": {
                            "type": "STRING",
                            "description": "Action: 'get' (read items), 'add' (add item/items), 'done' (mark item bought/completed), 'clear' (mark whole list done), 'lists' (show all list names).",
                        },
                        "list_name": {"type": "STRING", "description": "List name in Russian (default 'покупки', e.g. 'покупки', 'дела', 'аптека')."},
                        "item": {"type": "STRING", "description": "Single item text (for 'add' or keyword for 'done')."},
                        "items": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                            "description": "Multiple items to add at once (e.g. ['молоко', 'хлеб', 'яйца']).",
                        },
                        "owner": {"type": "STRING", "description": "Owner user_id/name or 'family' (default shared)."},
                    },
                    "required": ["action"],
                },
            },
            {
                "name": "forget_memory",
                "description": "Delete or forget a saved memory item by its ID or search query ('забудь это', 'удали заметку про...').",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "item_id": {"type": "INTEGER", "description": "Exact ID of memory item if known."},
                        "query": {"type": "STRING", "description": "Keywords to find and remove matching active memory items."},
                        "list_name": {"type": "STRING", "description": "Optional list name when forgetting list items."},
                    },
                },
            },
        ]

    def get_prompt_snippet(self, db: Any, provider: Dict[str, Any], options: Dict[str, Any]) -> str:
        lists = db.list_memory_lists()
        active_lists = [f"'{l['list_name']}' ({l['active_count']})" for l in lists if l.get("active_count", 0) > 0]
        pinned_items = db.search_memory(query="", status="active", limit=12, touch_access=False)
        pinned_only = [m for m in pinned_items if m.get("is_pinned")][:8]

        lines = [
            "### PLUGIN: LONG-TERM MEMORY & LISTS (builtin.memory)",
            "- Use `manage_list` for shopping/to-do/packing lists ('добавь молоко в покупки', 'что купить?', 'мы купили молоко').",
            "- Use `remember_info` for facts, codes, preferences, and notes ('запомни...', 'запиши...'); set `is_pinned=true` for permanent codes/preferences.",
            "- Use `recall_memory` BEFORE answering when the user asks about previously saved personal/household information, codes, or past notes.",
            "- Use `forget_memory` when asked to delete/forget stored information.",
        ]
        if active_lists:
            lines.append(f"- Active household lists: {', '.join(active_lists)}.")
        if pinned_only:
            facts_str = "; ".join(
                f"[{m['owner_user_name']}] {(m['title'] + ': ') if m['title'] else ''}{m['content']}"
                for m in pinned_only
            )
            lines.append(f"- Pinned household facts: {facts_str}")
        return "\n".join(lines)

    async def execute_tool(self, tool_name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        db = ctx.get("db")
        if not db:
            return {"status": "error", "error": "Database not available"}

        if tool_name == "remember_info":
            owner_id = _resolve_owner_user_id(db, args.get("owner"))
            kind = str(args.get("kind") or "fact").strip().lower()
            if kind == "list_item":
                kind = "note"
            ttl = args.get("ttl_days")
            item = db.add_memory_item(
                kind=kind,
                content=str(args.get("content") or ""),
                owner_user_id=owner_id,
                title=str(args.get("title") or ""),
                tags=str(args.get("tags") or ""),
                is_pinned=bool(args.get("is_pinned", kind in ("fact", "preference") and bool(args.get("title")))),
                ttl_days=int(ttl) if ttl else None,
            )
            return {"status": "ok", "item": item}

        if tool_name == "recall_memory":
            owner_raw = args.get("owner")
            owner_id = _resolve_owner_user_id(db, owner_raw) if owner_raw else None
            items = db.search_memory(
                query=str(args.get("query") or ""),
                kind=str(args.get("kind") or "") or None,
                list_name=str(args.get("list_name") or "") or None,
                owner_user_id=owner_id,
                include_shared=True,
                status="active",
                limit=int(args.get("limit") or 15),
            )
            return {"status": "ok", "count": len(items), "items": items}

        if tool_name == "manage_list":
            action = str(args.get("action") or "get").strip().lower()
            list_name = str(args.get("list_name") or "покупки").strip() or "покупки"
            owner_id = _resolve_owner_user_id(db, args.get("owner"))

            if action == "lists":
                lists = db.list_memory_lists()
                return {"status": "ok", "lists": lists}

            if action == "get":
                items = db.search_memory(
                    kind="list_item",
                    list_name=list_name,
                    owner_user_id=owner_id,
                    include_shared=True,
                    status="active",
                    limit=50,
                )
                return {
                    "status": "ok",
                    "list_name": list_name,
                    "count": len(items),
                    "items": [{"id": it["id"], "content": it["content"], "owner": it["owner_user_name"]} for it in items],
                }

            if action == "add":
                to_add: List[str] = []
                if isinstance(args.get("items"), list):
                    to_add.extend(str(x).strip() for x in args["items"] if str(x).strip())
                if args.get("item") and str(args["item"]).strip():
                    to_add.append(str(args["item"]).strip())
                if not to_add:
                    return {"status": "error", "error": "Не указан элемент для добавления в список"}
                added = [
                    db.add_memory_item(
                        kind="list_item",
                        list_name=list_name,
                        content=txt,
                        owner_user_id=owner_id,
                    )
                    for txt in to_add
                ]
                return {"status": "ok", "list_name": list_name, "added_count": len(added), "items": added}

            if action == "done":
                q = str(args.get("item") or "").strip()
                if not q and isinstance(args.get("items"), list) and args["items"]:
                    res_list = [db.complete_list_items(list_name=list_name, item_query=str(x)) for x in args["items"]]
                    total = sum(r["updated_count"] for r in res_list)
                    return {"status": "ok", "list_name": list_name, "updated_count": total}
                res = db.complete_list_items(list_name=list_name, item_query=q)
                return {"status": "ok", "list_name": list_name, **res}

            if action == "clear":
                res = db.complete_list_items(list_name=list_name, clear_all=True)
                return {"status": "ok", "list_name": list_name, **res}

            return {"status": "error", "error": f"Unknown manage_list action: {action}"}

        if tool_name == "forget_memory":
            item_id = args.get("item_id")
            if item_id:
                ok = db.delete_memory_item(int(item_id))
                return {"status": "ok" if ok else "not_found", "deleted_count": 1 if ok else 0}
            q = str(args.get("query") or "").strip()
            ln = str(args.get("list_name") or "").strip() or None
            if not q and not ln:
                return {"status": "error", "error": "Укажите item_id или query для удаления"}
            matches = db.search_memory(query=q, list_name=ln, status="active", limit=3, touch_access=False)
            deleted = 0
            for m in matches:
                if db.delete_memory_item(m["id"]):
                    deleted += 1
            return {"status": "ok" if deleted else "not_found", "deleted_count": deleted}

        return {"status": "error", "error": f"Unknown tool: {tool_name}"}
