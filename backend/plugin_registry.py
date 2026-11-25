from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

try:
    from .db import PorfiriyDB, db_singleton
except ImportError:
    from db import PorfiriyDB, db_singleton

logger = logging.getLogger("plugins")


class PorfiriyPlugin:
    """Базовый класс встроенного плагина Порфирия."""

    provider_id: str = ""

    def get_tool_specs(self, db: PorfiriyDB, provider: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Возвращает список спецификаций инструментов: [{'name', 'description', 'parameters'}]."""
        return []

    def get_prompt_snippet(self, db: PorfiriyDB, provider: Dict[str, Any], options: Dict[str, Any]) -> str:
        """Возвращает фрагмент системного промпта для активного плагина."""
        return ""

    async def execute_tool(self, tool_name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Выполняет вызов инструмента плагина."""
        raise NotImplementedError(f"{self.provider_id} does not implement {tool_name}")


def _safe_args_preview(args: Dict[str, Any]) -> str:
    if not args:
        return ""
    sanitized = {}
    for k, v in args.items():
        low = str(k).lower()
        if any(m in low for m in ("pass", "token", "secret", "hash", "key")):
            sanitized[k] = "***"
        else:
            sanitized[k] = v
    try:
        return json.dumps(sanitized, ensure_ascii=False)[:240]
    except Exception:
        return str(sanitized)[:240]


class PluginRegistry:
    """Единый реестр встроенных плагинов, внешних MCP-серверов и LLM-делегатов."""

    def __init__(self, db: PorfiriyDB):
        self.db = db
        self._builtins: Dict[str, PorfiriyPlugin] = {}
        self._tool_owner_map: Dict[str, str] = {}

    def register_builtin(self, plugin: PorfiriyPlugin) -> None:
        pid = (plugin.provider_id or "").strip()
        if not pid:
            raise ValueError("Plugin provider_id cannot be empty")
        self._builtins[pid] = plugin
        logger.info(f"Registered builtin plugin: {pid}")

    def get_active_tool_specs(self) -> List[Dict[str, Any]]:
        specs: List[Dict[str, Any]] = []
        new_map: Dict[str, str] = {}
        providers = self.db.list_providers(mask_secrets=False, enabled_only=True)

        for prov in providers:
            pid = prov["id"]
            kind = prov["kind"]
            cfg = prov.get("config") or {}

            if kind == "builtin":
                plugin = self._builtins.get(pid)
                if not plugin:
                    continue
                try:
                    p_specs = plugin.get_tool_specs(self.db, prov) or []
                except Exception as e:
                    logger.warning(f"Plugin {pid} get_tool_specs error: {e}")
                    continue
                for spec in p_specs:
                    name = (spec.get("name") or "").strip()
                    if not name or name in new_map:
                        continue
                    new_map[name] = pid
                    specs.append(spec)
            else:
                # mcp_http / mcp_stdio / llm_delegate — инструменты из кэшированной схемы
                whitelist = cfg.get("enabled_tools") or []
                if isinstance(whitelist, str):
                    whitelist = [x.strip() for x in whitelist.split(",") if x.strip()]
                whitelist_set = set(whitelist) if whitelist else None
                for spec in prov.get("cached_tools") or []:
                    name = (spec.get("name") or "").strip()
                    if not name or name in new_map:
                        continue
                    if whitelist_set is not None and name not in whitelist_set:
                        continue
                    new_map[name] = pid
                    specs.append(spec)

        self._tool_owner_map = new_map
        return specs

    def get_active_prompt_sections(self, options: Dict[str, Any]) -> str:
        sections: List[str] = []
        providers = self.db.list_providers(mask_secrets=False, enabled_only=True)
        for prov in providers:
            pid = prov["id"]
            kind = prov["kind"]
            if kind == "builtin":
                plugin = self._builtins.get(pid)
                if not plugin:
                    continue
                try:
                    snippet = (plugin.get_prompt_snippet(self.db, prov, options) or "").strip()
                except Exception as e:
                    logger.warning(f"Plugin {pid} get_prompt_snippet error: {e}")
                    continue
                if snippet:
                    sections.append(snippet)
            else:
                hint = ((prov.get("config") or {}).get("prompt_hint") or "").strip()
                if hint:
                    sections.append(f"### EXTERNAL PROVIDER ({prov['title']})\n{hint}")
        return "\n\n".join(sections)

    def handles_tool(self, tool_name: str) -> bool:
        if tool_name in self._tool_owner_map:
            return True
        # Лениво синхронизируем карту, если get_active_tool_specs ещё не вызывался
        self.get_active_tool_specs()
        return tool_name in self._tool_owner_map

    async def execute_tool(
        self,
        tool_name: str,
        args: Optional[Dict[str, Any]],
        ctx: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ctx_dict = dict(ctx or {})
        clean_args = dict(args or {})
        if tool_name not in self._tool_owner_map:
            self.get_active_tool_specs()

        pid = self._tool_owner_map.get(tool_name, "")
        prov = self.db.get_provider(pid, mask_secrets=False) if pid else None
        dev = ctx_dict.get("dev") or {}
        mac = dev.get("mac", "") if isinstance(dev, dict) else ""
        speaker = str(clean_args.get("owner") or clean_args.get("user_id") or ctx_dict.get("speaker_user_id") or "")

        t0 = time.monotonic()
        status = "ok"
        error_text = ""
        try:
            if not pid or not prov or not prov.get("enabled"):
                status = "error"
                error_text = f"Provider for tool '{tool_name}' is not active"
                return {"status": "error", "error": error_text}

            ctx_dict["db"] = self.db
            ctx_dict["provider"] = prov

            if prov["kind"] == "builtin":
                plugin = self._builtins.get(pid)
                if not plugin:
                    status = "error"
                    error_text = f"Builtin plugin '{pid}' is not registered"
                    return {"status": "error", "error": error_text}
                res = await plugin.execute_tool(tool_name, clean_args, ctx_dict)
                if isinstance(res, dict) and res.get("status") == "error":
                    status = "error"
                    error_text = str(res.get("error") or "")
                return res if isinstance(res, dict) else {"status": "ok", "result": res}

            status = "not_implemented"
            error_text = f"External runner ({prov['kind']}) will be enabled in the next milestone"
            return {"status": "not_implemented", "error": error_text}

        except Exception as e:
            status = "error"
            error_text = str(e)
            logger.exception(f"Plugin tool execution failed ({pid}/{tool_name}): {e}")
            return {"status": "error", "error": error_text}
        finally:
            dur_ms = int((time.monotonic() - t0) * 1000)
            try:
                self.db.log_tool_call(
                    tool_name=tool_name,
                    provider_id=pid,
                    speaker_user_id=speaker,
                    device_mac=mac,
                    args_preview=_safe_args_preview(clean_args),
                    status=status,
                    duration_ms=dur_ms,
                    error=error_text,
                )
            except Exception as le:
                logger.warning(f"Failed to write tool audit log: {le}")


plugin_registry_singleton = PluginRegistry(db_singleton)
