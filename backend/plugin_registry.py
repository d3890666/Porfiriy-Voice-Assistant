from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

try:
    from .db import PorfiriyDB, db_singleton
    from .big_models import big_model_pool_singleton
    from .mcp_client import mcp_http_client_singleton
except ImportError:
    from db import PorfiriyDB, db_singleton
    from big_models import big_model_pool_singleton
    from mcp_client import mcp_http_client_singleton

logger = logging.getLogger("plugins")

CONTEXT_GATE_TOOLS = {"tg_read_messages", "tg_search_messages", "mail_search", "mail_read_message"}


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

    def get_builtin(self, provider_id: str) -> Optional[PorfiriyPlugin]:
        return self._builtins.get((provider_id or "").strip())

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
        res_summary = ""
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
                if tool_name in CONTEXT_GATE_TOOLS and ctx_dict.get("on_progress"):
                    try:
                        await ctx_dict["on_progress"]("🔍 Пошёл искать данные, скоро вернусь...")
                    except Exception:
                        pass
                res = await plugin.execute_tool(tool_name, clean_args, ctx_dict)
                if isinstance(res, dict):
                    if res.get("status") == "error":
                        status = "error"
                        error_text = str(res.get("error") or "")
                    for cnt_key in ("message_count", "count", "added_count", "updated_count", "deleted_count"):
                        if cnt_key in res and res[cnt_key] is not None:
                            res_summary = f" -> {cnt_key}={res[cnt_key]}"
                            break

                # Context Gate: автоматическая передача крупного результата в пул больших моделей
                opts = ctx_dict.get("options") or {}
                if (
                    opts.get("big_models_enabled")
                    and tool_name in CONTEXT_GATE_TOOLS
                    and isinstance(res, dict)
                    and res.get("status") == "ok"
                ):
                    raw_json = json.dumps(res, ensure_ascii=False)
                    threshold = max(500, int(opts.get("big_models_threshold_chars") or 4000))
                    if len(raw_json) > threshold:
                        on_progress = ctx_dict.get("on_progress")
                        if on_progress:
                            try:
                                await on_progress("🔍 Данных много, передаю на анализ большой модели, скоро вернусь...")
                            except Exception:
                                pass
                        user_q = str(ctx_dict.get("user_query") or "").strip()
                        big_prompt = (
                            f"Исходный запрос пользователя: {user_q or 'Проанализируй и кратко перескажи данные.'}\n\n"
                            f"Инструмент `{tool_name}` вернул большой объём данных ({len(raw_json)} символов).\n"
                            f"Сделай точную, структурированную выжимку для ответа пользователю на русском языке: "
                            f"сохрани ключевые факты, даты, имена, ссылки, пароли и коды.\n\n"
                            f"ДАННЫЕ:\n{raw_json[:180000]}"
                        )
                        big_res = await big_model_pool_singleton.run_text_task(big_prompt, opts)
                        if big_res.get("success"):
                            res_summary += f" -> big={big_res['model']}"
                            return {
                                "status": "ok",
                                "delegated_to_big_model": big_res["model"],
                                "original_chars": len(raw_json),
                                "summary": big_res["text"],
                            }
                        status = "error"
                        error_text = str(big_res.get("error") or "Big model pool failed")
                        return {
                            "status": "error",
                            "error": f"Не удалось обработать большой объём данных: {error_text}",
                        }

                return res if isinstance(res, dict) else {"status": "ok", "result": res}

            if prov["kind"] == "mcp_http":
                cfg = prov.get("config") or {}
                url = str(cfg.get("url") or "").strip()
                api_token = str(cfg.get("api_token") or "").strip()
                extra_headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else None
                timeout_s = float(cfg.get("timeout_s") or 45.0)

                if ctx_dict.get("on_progress"):
                    try:
                        await ctx_dict["on_progress"](f"🔍 Обращаюсь к {prov.get('title') or pid}...")
                    except Exception:
                        pass

                res = await mcp_http_client_singleton.call_tool(
                    url,
                    tool_name,
                    clean_args,
                    api_token=api_token,
                    timeout_s=timeout_s,
                    extra_headers=extra_headers,
                )
                if isinstance(res, dict) and res.get("status") == "error":
                    status = "error"
                    error_text = str(res.get("error") or "")
                    return res

                # Специальный режим для маркетплейсов: всегда формируем красивую HTML-карточку
                # с ценами цифрами, кликабельными ссылками и рекомендацией и отправляем в Telegram
                # (в тот же чат, если запрос из Telegram, или в общий семейный чат, если запрос голосом)
                opts = ctx_dict.get("options") or {}
                is_marketplace_product_tool = (
                    pid == "mcp.marketplaces"
                    and tool_name not in ("marketplace_health", "marketplace_sources", "compare_sources", "compare_browser_snapshot")
                )
                if is_marketplace_product_tool and isinstance(res, dict) and res.get("status") == "ok":
                    raw_json = json.dumps(res, ensure_ascii=False)
                    user_q = str(ctx_dict.get("user_query") or clean_args.get("query") or "").strip()
                    if ctx_dict.get("on_progress"):
                        try:
                            await ctx_dict["on_progress"]("🔍 Обрабатываю результаты и готовлю подборку со ссылками...")
                        except Exception:
                            pass
                    card_prompt = (
                        f"Исходный запрос пользователя: {user_q or 'Найди лучшие предложения.'}\n\n"
                        f"Ниже JSON-ответ маркетплейсов от инструмента `{tool_name}`.\n"
                        f"Сформируй компактную, красивую карточку для Telegram строго в формате **Telegram HTML**.\n"
                        f"ПРАВИЛА:\n"
                        f"1. Разрешены ТОЛЬКО теги Telegram HTML: <b>, <i>, <a href=\"URL\">, <code>. НЕ используй Markdown (**, ##, []()), списки <ul>/<li> и <br>. Символ & в тексте экранируй как &amp;.\n"
                        f"2. Все цены пиши СТРОГО ЦИФРАМИ (например: <b>490 ₽</b>), никогда прописью.\n"
                        f"3. У пользователя есть Ozon Карта, WB Кошелёк и Яндекс Пэй — в первую очередь указывай цену по карте/кошельку (если есть в данных, подпиши в скобках, например «Ozon Карта» / «WB Кошелёк»).\n"
                        f"4. Выбери 3–5 лучших релевантных предложений (отсеивай мусор и нерелевантные аксессуары). Формат каждой строки:\n"
                        f"   1. <a href=\"URL\">Короткое название</a> — <b>ЦЕНА ₽</b> (площадка, рейтинг/доставка если есть)\n"
                        f"   Если URL нет в данных — пиши просто название без ссылки.\n"
                        f"5. В начале заголовок: 🛒 <b>Подборка: {user_q or 'товары'}</b>\n"
                        f"6. В конце добавь 1–2 предложения: 💡 <b>Рекомендация:</b> что лучше взять и почему.\n\n"
                        f"ДАННЫЕ:\n{raw_json[:150000]}"
                    )
                    big_res = await big_model_pool_singleton.run_text_task(card_prompt, opts)
                    if big_res.get("success") and (big_res.get("text") or "").strip():
                        card_html = big_res["text"].strip()
                        if card_html.startswith("```"):
                            card_html = card_html.split("\n", 1)[-1]
                            if card_html.endswith("```"):
                                card_html = card_html[:-3].strip()

                        tg_target = mac[3:] if mac.startswith("tg_") and len(mac) > 3 else "family"
                        tg_sent = False
                        tg_dest_label = "этот Telegram-чат" if mac.startswith("tg_") else "общий семейный Telegram-чат"
                        try:
                            try:
                                from .plugins.telegram_bot import TelegramBotPlugin
                            except ImportError:
                                from plugins.telegram_bot import TelegramBotPlugin
                            tg_prov = self.db.get_provider("builtin.telegram_bot", mask_secrets=False)
                            if tg_prov and tg_prov.get("enabled"):
                                tg_res = await TelegramBotPlugin.send_message(
                                    db=self.db,
                                    provider=tg_prov,
                                    ha_api=ctx_dict.get("ha_api"),
                                    target=tg_target,
                                    text=card_html,
                                    parse_mode="HTML",
                                    disable_web_page_preview=True,
                                )
                                tg_sent = tg_res.get("status") == "ok"
                                if not tg_sent:
                                    logger.warning(f"Marketplace Telegram card send failed ({tg_target}): {tg_res.get('error')}")
                        except Exception as tg_ex:
                            logger.warning(f"Marketplace Telegram card send exception: {tg_ex}")

                        import re as _re
                        plain_summary = _re.sub(r"<[^>]+>", "", card_html)
                        res_summary = f" -> mcp={len(raw_json)}ch -> big={big_res['model']} -> tg={'ok' if tg_sent else 'skip'}({tg_target})"
                        return {
                            "status": "ok",
                            "sent_to_telegram": tg_sent,
                            "telegram_target": tg_dest_label,
                            "instruction": (
                                f"Подробная карточка с ценами цифрами и кликабельными ссылками УЖЕ отправлена в {tg_dest_label}. "
                                "НЕ перечисляй весь список и НЕ читай ссылки. Кратко (1–2 предложения) назови лучший вариант, "
                                f"главную цену и скажи, что подробности со ссылками отправлены в {tg_dest_label}."
                                if tg_sent
                                else "Кратко озвучь лучшие варианты и цены из summary."
                            ),
                            "summary": plain_summary[:2500],
                        }

                # Context Gate для крупных ответов внешних MCP-серверов (маркетплейсы и т.п.)
                if opts.get("big_models_enabled") and isinstance(res, dict) and res.get("status") == "ok":
                    raw_json = json.dumps(res, ensure_ascii=False)
                    threshold = max(500, int(opts.get("big_models_threshold_chars") or 4000))
                    if len(raw_json) > threshold:
                        if ctx_dict.get("on_progress"):
                            try:
                                await ctx_dict["on_progress"]("🔍 Данных много, передаю на анализ большой модели, скоро вернусь...")
                            except Exception:
                                pass
                        user_q = str(ctx_dict.get("user_query") or "").strip()
                        big_prompt = (
                            f"Исходный запрос пользователя: {user_q or 'Проанализируй и кратко перескажи данные.'}\n\n"
                            f"Внешний MCP-инструмент `{tool_name}` ({prov.get('title') or pid}) вернул большой объём данных ({len(raw_json)} символов).\n"
                            f"Сделай точную, структурированную выжимку для ответа пользователю на русском языке: "
                            f"сохрани названия, цены, магазины/площадки, рейтинги, ключевые факты и ссылки.\n\n"
                            f"ДАННЫЕ:\n{raw_json[:180000]}"
                        )
                        big_res = await big_model_pool_singleton.run_text_task(big_prompt, opts)
                        if big_res.get("success"):
                            res_summary = f" -> mcp={len(raw_json)}ch -> big={big_res['model']}"
                            return {
                                "status": "ok",
                                "delegated_to_big_model": big_res["model"],
                                "original_chars": len(raw_json),
                                "summary": big_res["text"],
                            }

                return res if isinstance(res, dict) else {"status": "ok", "result": res}

            status = "not_implemented"
            error_text = f"External runner ({prov['kind']}) is not supported yet"
            return {"status": "not_implemented", "error": error_text}

        except Exception as e:
            status = "error"
            error_text = str(e)
            logger.exception(f"Plugin tool execution failed ({pid}/{tool_name}): {e}")
            return {"status": "error", "error": error_text}
        finally:
            dur_ms = int((time.monotonic() - t0) * 1000)
            try:
                preview = (_safe_args_preview(clean_args) + res_summary)[:240]
                self.db.log_tool_call(
                    tool_name=tool_name,
                    provider_id=pid,
                    speaker_user_id=speaker,
                    device_mac=mac,
                    args_preview=preview,
                    status=status,
                    duration_ms=dur_ms,
                    error=error_text,
                )
            except Exception as le:
                logger.warning(f"Failed to write tool audit log: {le}")


plugin_registry_singleton = PluginRegistry(db_singleton)

try:
    from .plugins import register_builtin_plugins
except ImportError:
    try:
        from plugins import register_builtin_plugins
    except ImportError:
        register_builtin_plugins = None

if register_builtin_plugins:
    register_builtin_plugins(plugin_registry_singleton)
