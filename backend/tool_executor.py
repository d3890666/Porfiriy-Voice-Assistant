import asyncio
import datetime
import html as _html
import json
import logging
import re as _re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from audio_utils import get_earcon_pcm, is_simple_smarthome_action
from net_utils import get_device_ip
from prompt_builder import resolve_target_player

try:
    from .plugin_registry import plugin_registry_singleton
except ImportError:
    try:
        from plugin_registry import plugin_registry_singleton
    except ImportError:
        plugin_registry_singleton = None

tool_logger = logging.getLogger("tools")


def _format_fact_html(topic: str, answer: str, items: List[Dict[str, str]]) -> str:
    """Форматирует результат обычного веб-поиска в безопасный Telegram HTML со ссылками."""
    parts = [f"🔍 <b>{_html.escape(topic)}</b>", "", _html.escape(answer)]
    links: List[str] = []
    for item in (items or [])[:3]:
        link = str(item.get("link") or "").strip()
        title = str(item.get("title") or link).strip()
        if link.startswith("http://") or link.startswith("https://"):
            links.append(f"• <a href=\"{_html.escape(link, quote=True)}\">{_html.escape(title[:90])}</a>")
    if links:
        parts.append("")
        parts.append("🔗 <b>Источники:</b>")
        parts.extend(links)
    return "\n".join(parts).strip()


def _build_schedule_trigger_and_action(
    args: Dict[str, Any],
    *,
    dev: Optional[Dict[str, Any]] = None,
    base_trigger: Optional[Dict[str, Any]] = None,
    base_action: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Собирает trigger и action как из плоских аргументов Gemini Live, так и из вложенных словарей."""
    raw_trig = args.get("trigger")
    if isinstance(raw_trig, str) and raw_trig.strip().startswith("{"):
        try:
            raw_trig = json.loads(raw_trig)
        except Exception:
            raw_trig = None
    trig: Dict[str, Any] = dict(raw_trig) if isinstance(raw_trig, dict) and raw_trig else dict(base_trigger or {})

    if args.get("trigger_type"):
        trig["type"] = str(args["trigger_type"]).strip().lower()
    if args.get("delay_seconds") is not None:
        trig["delay_seconds"] = args["delay_seconds"]
    if args.get("run_at"):
        trig["run_at"] = str(args["run_at"]).strip()
    if args.get("every_minutes") is not None:
        trig["every_minutes"] = args["every_minutes"]
    if args.get("jitter_minutes") is not None:
        trig["jitter_minutes"] = args["jitter_minutes"]
    if args.get("cron_expr"):
        trig["expr"] = str(args["cron_expr"]).strip()
    if args.get("entity_id"):
        trig["entity_id"] = str(args["entity_id"]).strip()
    if args.get("to_state"):
        trig["to_state"] = str(args["to_state"]).strip()
    if args.get("for_seconds") is not None:
        trig["for_seconds"] = args["for_seconds"]

    raw_act = args.get("action")
    if isinstance(raw_act, str) and raw_act.strip().startswith("{"):
        try:
            raw_act = json.loads(raw_act)
        except Exception:
            raw_act = None
    act: Dict[str, Any] = dict(raw_act) if isinstance(raw_act, dict) and raw_act else dict(base_action or {})

    if args.get("action_type"):
        act["type"] = str(args["action_type"]).strip().lower()
    elif not act.get("type"):
        if args.get("tool_name") or args.get("telegram_text") or (args.get("ha_domain") and args.get("ha_service")):
            act["type"] = "direct_tool"
        elif args.get("prompt"):
            act["type"] = "ai_prompt"

    if act.get("type") == "direct_tool":
        t_name = str(args.get("tool_name") or act.get("tool") or "").strip()
        if not t_name:
            if args.get("telegram_text"):
                t_name = "send_to_telegram"
            elif args.get("ha_domain") and args.get("ha_service"):
                t_name = "call_ha_service"
        if t_name:
            act["tool"] = t_name
        tool_args: Dict[str, Any] = dict(act.get("args") or {})
        raw_extra = args.get("tool_args_json")
        if isinstance(raw_extra, str) and raw_extra.strip().startswith("{"):
            try:
                parsed_extra = json.loads(raw_extra)
                if isinstance(parsed_extra, dict):
                    tool_args.update(parsed_extra)
            except Exception:
                pass
        if t_name == "send_to_telegram":
            if args.get("telegram_target"):
                tool_args["target"] = str(args["telegram_target"]).strip()
            elif not tool_args.get("target"):
                mac = (dev or {}).get("mac", "")
                if mac.startswith("tg_") and len(mac) > 3:
                    tool_args["target"] = mac[3:]
            if args.get("telegram_text"):
                tool_args["text"] = str(args["telegram_text"]).strip()
        elif t_name == "call_ha_service":
            if args.get("ha_domain"):
                tool_args["domain"] = str(args["ha_domain"]).strip()
            if args.get("ha_service"):
                tool_args["service"] = str(args["ha_service"]).strip()
            if args.get("ha_entity_id"):
                tool_args["entity_id"] = str(args["ha_entity_id"]).strip()
        elif t_name == "alarm_chime":
            if dev and dev.get("mac") and not tool_args.get("mac"):
                tool_args["mac"] = dev["mac"]
            if args.get("title") and not tool_args.get("label"):
                tool_args["label"] = str(args["title"]).strip()
        act["args"] = tool_args
    elif act.get("type") == "ai_prompt":
        if args.get("prompt"):
            act["prompt"] = str(args["prompt"]).strip()
        if args.get("allowed_tools") is not None:
            act["allowed_tools"] = args["allowed_tools"]

    return trig, act


class ToolExecutor:
    """Единый исполнитель инструментов Gemini Live (Home Assistant, Music Assistant, планировщик)."""

    def __init__(self, ha_api: Any, quasar_manager: Any = None, ducking_manager: Any = None, device_manager: Any = None):
        self.ha_api = ha_api
        self.quasar_manager = quasar_manager
        self.ducking_manager = ducking_manager
        self.device_manager = device_manager

    async def execute(
        self,
        name: str,
        raw_args: Optional[Dict[str, Any]],
        options: Dict[str, Any],
        *,
        dev: Optional[Dict[str, Any]] = None,
        turn_state: Optional[Dict[str, Any]] = None,
        on_feedback: Optional[Callable[[str, bool], Awaitable[None]]] = None,
        default_player_fallback: str = "media_player.living_room",
        user_query: str = "",
        on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """
        Выполняет инструмент и возвращает словарь ответа для Gemini FunctionResponse.
        on_feedback(kind, has_data): опциональный коллбэк для звуковых уведомлений (ESP32 earcons),
        где kind in ("success", "error").
        """
        args = dict(raw_args) if raw_args else {}
        target_default_player = resolve_target_player(dev, options) or default_player_fallback

        try:
            if name == "call_ha_service":
                domain = args.get("domain")
                service = args.get("service")
                entity_id = args.get("entity_id")

                service_data = {
                    k: v
                    for k, v in args.items()
                    if k not in ("domain", "service", "parameters") and v is not None
                }
                if isinstance(args.get("parameters"), dict):
                    service_data.update(args["parameters"])
                if entity_id and "entity_id" not in service_data:
                    service_data["entity_id"] = entity_id

                tool_logger.info(f"Gemini Calling Tool: {domain}.{service} with service_data: {service_data}")

                if turn_state is not None:
                    turn_state["tool_executed"] = True
                    if domain in ("media_player", "music_assistant", "mass"):
                        turn_state["playback_changed"] = True
                    if domain == "media_player" and service == "volume_set" and self.ducking_manager and dev:
                        vol_lvl = service_data.get("volume_level")
                        if vol_lvl is not None:
                            self.ducking_manager.set_explicit_volume(dev.get("mac", ""), float(vol_lvl))

                action_res = await self.ha_api.call_ha_action(
                    domain=domain,
                    service=service,
                    service_data=service_data,
                )
                tool_logger.info(f"HA Action Result: {action_res}")

                has_data = bool(action_res.get("has_response_data", False)) if isinstance(action_res, dict) else False
                if isinstance(action_res, dict) and action_res.get("status") == "error":
                    if on_feedback:
                        await on_feedback("error", has_data)
                    return {"status": "error", "error": action_res.get("error", "Unknown error")}

                if on_feedback:
                    await on_feedback("success", has_data)

                if turn_state is not None:
                    if domain == "media_player" and service in ("volume_set", "volume_up", "volume_down"):
                        turn_state["volume_changed"] = True
                    elif not has_data and is_simple_smarthome_action(domain, service):
                        turn_state["simple_smarthome_executed"] = True

                safe_result: Dict[str, Any] = {"status": "success"}
                if isinstance(action_res, dict):
                    if has_data:
                        safe_result["data"] = action_res.get("data")
                    elif action_res.get("result"):
                        safe_result["data"] = action_res.get("result")
                return safe_result

            if name == "get_ha_state":
                entity_id = args.get("entity_id")
                tool_logger.info(f"Gemini Calling get_ha_state for entity: {entity_id}")
                state_obj = await self.ha_api.get_entity_state(entity_id)
                if isinstance(state_obj, dict) and "state" in state_obj:
                    attrs = state_obj.get("attributes") or {}
                    safe_result = {
                        "status": "success",
                        "entity_id": entity_id,
                        "state": state_obj.get("state"),
                        "unit": attrs.get("unit_of_measurement"),
                        "friendly_name": attrs.get("friendly_name"),
                        "attributes": attrs,
                    }
                elif isinstance(state_obj, dict):
                    safe_result = state_obj
                else:
                    safe_result = {"status": "not_found", "entity_id": entity_id}
                tool_logger.info(f"HA get_ha_state result: {safe_result}")
                return safe_result

            if name == "search_music_assistant":
                query = args.get("name") or args.get("query") or ""
                artist = args.get("artist")
                media_type = args.get("media_type")
                tool_logger.info(f"Gemini Calling MA Search: name='{query}', artist='{artist or ''}', media_type={media_type}")
                res = await self.ha_api.search_music_assistant(
                    name=query,
                    media_type=media_type,
                    artist=artist,
                )
                tool_logger.info(f"MA Search Result: {res}")
                return res if isinstance(res, dict) else {"result": res}

            if name in ("play_music_assistant", "play_music"):
                uri_or_query = args.get("uri") or args.get("query") or args.get("name") or ""
                media_type = args.get("media_type")
                radio_mode = bool(args.get("radio_mode", False))
                target_player = args.get("player") or target_default_player

                if turn_state is not None:
                    turn_state["tool_executed"] = True
                    turn_state["playback_changed"] = True

                if self.quasar_manager and dev:
                    speaker_ip = get_device_ip(dev)
                    if speaker_ip:
                        asyncio.create_task(self.quasar_manager.stop_station_airplay_if_playing(speaker_ip))

                tool_logger.info(f"Gemini Playing MA URI: {uri_or_query} on {target_player}")
                res = await self.ha_api.play_music_assistant(
                    entity_id=target_player,
                    uri_or_query=uri_or_query,
                    media_type=media_type,
                    radio_mode=radio_mode,
                )
                tool_logger.info(f"MA Play Result: {res}")

                if on_feedback:
                    status = "error" if (isinstance(res, dict) and res.get("status") == "error") else "success"
                    await on_feedback(status, False)
                return res if isinstance(res, dict) else {"result": res}

            if name == "schedule_create":
                try:
                    from scheduler import scheduler_singleton
                except ImportError:
                    from .scheduler import scheduler_singleton
                trig, act = _build_schedule_trigger_and_action(args, dev=dev)
                title = str(args.get("title") or "").strip()
                caller_mac = (dev or {}).get("mac", "")
                default_owner = caller_mac[3:] if caller_mac.startswith("tg_") and len(caller_mac) > 3 else "family"
                owner = str(args.get("owner_user_id") or default_owner).strip() or "family"
                notify_ch = str(args.get("notify_channel") or "").strip()
                quiet_h = str(args.get("quiet_hours") or "").strip()
                one_shot = args.get("one_shot")
                if one_shot is not None:
                    one_shot = bool(one_shot)
                tool_logger.info(f"[SCHEDULER TOOL] schedule_create args={json.dumps(args, ensure_ascii=False)[:400]}")
                job = await scheduler_singleton.create_job(
                    title=title,
                    trigger=trig,
                    action=act,
                    owner_user_id=owner,
                    notify_channel=notify_ch,
                    quiet_hours=quiet_h,
                    one_shot=one_shot,
                )
                if turn_state is not None:
                    turn_state["tool_executed"] = True
                return {"status": "scheduled", "job": job}

            if name == "schedule_list":
                try:
                    from scheduler import scheduler_singleton
                except ImportError:
                    from .scheduler import scheduler_singleton
                status_filter = str(args.get("status") or "").strip() or None
                jobs = scheduler_singleton.db.list_scheduled_jobs(status=status_filter)
                compact = [
                    {
                        "id": j["id"],
                        "title": j["title"],
                        "job_type": j["job_type"],
                        "cron_or_run_at": j["cron_or_run_at"],
                        "enabled": j["enabled"],
                        "status": j["status"],
                        "next_run_at": j["next_run_at"],
                        "last_status": j["last_status"],
                        "last_error": j["last_error"],
                    }
                    for j in jobs
                ]
                return {"status": "ok", "count": len(compact), "jobs": compact}

            if name == "schedule_manage":
                try:
                    from scheduler import scheduler_singleton
                except ImportError:
                    from .scheduler import scheduler_singleton
                op = str(args.get("operation") or "").strip().lower()
                query = str(args.get("job_id") or args.get("query") or "").strip()
                matches = scheduler_singleton.find_jobs_by_query(query) if query else []
                if not matches:
                    return {"status": "not_found", "error": f"Задача '{query}' не найдена"}
                target_job = matches[0]
                jid = target_job["id"]

                if op == "delete":
                    ok = scheduler_singleton.db.delete_scheduled_job(jid)
                    return {"status": "ok" if ok else "not_found", "deleted_id": jid, "title": target_job["title"]}
                if op in ("pause", "resume"):
                    en = (op == "resume")
                    nxt = target_job.get("next_run_at")
                    if en and not nxt and (target_job.get("trigger") or {}).get("type") in ("interval", "cron"):
                        nxt = scheduler_singleton._compute_following_run(target_job["trigger"])
                    upd = scheduler_singleton.db.upsert_scheduled_job(
                        job_id=jid,
                        job_type=target_job["job_type"],
                        title=target_job["title"],
                        owner_user_id=target_job["owner_user_id"],
                        cron_or_run_at=target_job["cron_or_run_at"],
                        trigger=target_job["trigger"],
                        action=target_job["action"],
                        notify_channel=target_job["notify_channel"],
                        quiet_hours=target_job["quiet_hours"],
                        one_shot=target_job["one_shot"],
                        enabled=en,
                        status="active" if en else "paused",
                        next_run_at=nxt,
                    )
                    return {"status": "ok", "job": upd}
                if op == "run_now":
                    res = await scheduler_singleton.run_job_now(jid)
                    return {"status": "ok", "job_id": jid, "run_result": res}
                if op == "update":
                    new_title = str(args.get("title") or target_job["title"]).strip()
                    new_trig, new_act = _build_schedule_trigger_and_action(
                        args,
                        dev=dev,
                        base_trigger=target_job["trigger"],
                        base_action=target_job["action"],
                    )
                    new_ch = str(args.get("notify_channel") if args.get("notify_channel") is not None else target_job["notify_channel"])
                    new_qh = str(args.get("quiet_hours") if args.get("quiet_hours") is not None else target_job["quiet_hours"])
                    upd = await scheduler_singleton.create_job(
                        job_id=jid,
                        title=new_title,
                        trigger=new_trig,
                        action=new_act,
                        owner_user_id=str(args.get("owner_user_id") or target_job["owner_user_id"]),
                        notify_channel=new_ch,
                        quiet_hours=new_qh,
                        one_shot=target_job["one_shot"],
                        enabled=target_job["enabled"],
                    )
                    return {"status": "ok", "job": upd}
                return {"status": "error", "error": f"Unknown schedule_manage operation: {op}"}

            if name == "web_search":
                try:
                    from big_models import big_model_pool_singleton
                    from db import db_singleton
                    from serper_search import serper_search_singleton
                except ImportError:
                    from .big_models import big_model_pool_singleton
                    from .db import db_singleton
                    from .serper_search import serper_search_singleton

                q_txt = str(args.get("query") or "").strip()
                if not q_txt:
                    return {"status": "error", "error": "Укажите поисковый запрос query"}

                mode = str(args.get("mode") or "fact").strip().lower()
                if mode not in ("fact", "recipe", "cocktail"):
                    mode = "fact"
                if mode == "recipe" and "коктейл" in q_txt.lower():
                    mode = "cocktail"

                mac = (dev or {}).get("mac", "")
                serper_key = str(options.get("serper_api_key") or "").strip()
                pref_model = str(options.get("web_search_model") or "").strip()

                if mode in ("recipe", "cocktail"):
                    if on_progress:
                        try:
                            msg = "🍸 Ищу рецепт коктейля..." if mode == "cocktail" else "🍽️ Ищу точный рецепт..."
                            await on_progress(msg)
                        except Exception:
                            pass

                    serper_res: Dict[str, Any] = {}
                    items: List[Dict[str, str]] = []
                    if serper_key:
                        if mode == "cocktail":
                            serper_res = await serper_search_singleton.search(f"site:edim.tv {q_txt}", serper_key, num=5)
                            if not (serper_res.get("success") and serper_res.get("items")):
                                serper_res = await serper_search_singleton.search(q_txt, serper_key, num=5)
                        else:
                            serper_res = await serper_search_singleton.search(q_txt, serper_key, num=5)
                        if serper_res.get("success"):
                            items = serper_res.get("items") or []

                    page_text = ""
                    source_url = ""
                    source_title = ""
                    for it in items[:2]:
                        link = str(it.get("link") or "").strip()
                        if not link:
                            continue
                        fetched = await serper_search_singleton.fetch_page_text(link, max_chars=8000, timeout_s=6.0)
                        if len(fetched) >= 400:
                            page_text = fetched
                            source_url = link
                            source_title = str(it.get("title") or "").strip()
                            break
                    if not source_url and items:
                        source_url = str(items[0].get("link") or "").strip()
                        source_title = str(items[0].get("title") or "").strip()

                    icon = "🍸" if mode == "cocktail" else "🍽️"
                    src_line = f'\nИсточник (уже найденный URL): {source_url} ({source_title})' if source_url else ""
                    snippets_txt = serper_res.get("answer") or ""
                    has_serper_data = bool(page_text or snippets_txt)

                    card_prompt = (
                        f"Запрос пользователя: {q_txt}\n"
                        f"Собери и оформи точный рецепт строго в формате **Telegram HTML**.\n"
                        f"ПРАВИЛА:\n"
                        f"1. Разрешены ТОЛЬКО теги Telegram HTML: <b>, <i>, <a href=\"URL\">, <code>. "
                        f"НЕ используй Markdown (**, ##, []()), списки <ul>/<li> и <br>. Символ & экранируй как &amp;.\n"
                        f"2. Заголовок: {icon} <b>Рецепт: ...</b>\n"
                        f"3. Обязательно укажи точные ингредиенты с цифрами и единицами измерения (50 мл, 200 г, 1 ч.л., температура, время) — НЕ сокращай граммовки!\n"
                        f"4. Шаги приготовления пронумеруй (1., 2., 3. ...).\n"
                        f"5. При желании в конце добавь короткий совет по подаче/украшению.\n"
                        f"6. Если есть URL источника, добавь внизу строку: 🔗 <a href=\"{source_url or 'URL'}\">Источник</a>.{src_line}\n\n"
                        + (f"СНИППЕТЫ ПОИСКА:\n{snippets_txt}\n\n" if snippets_txt else "")
                        + (f"ТЕКСТ СТРАНИЦЫ:\n{page_text}" if page_text else "Найди точный рецепт через Google Поиск (для коктейлей приоритетно ориентируйся на edim.tv).")
                    )
                    big_res = await big_model_pool_singleton.run_text_task(
                        card_prompt,
                        options,
                        enable_google_search=not has_serper_data,
                        preferred_model=pref_model,
                    )
                    if not big_res.get("success") or not (big_res.get("text") or "").strip():
                        return {"status": "error", "error": big_res.get("error") or "Не удалось получить рецепт"}

                    card_html = big_res["text"].strip()
                    if card_html.startswith("```"):
                        card_html = card_html.split("\n", 1)[-1]
                        if card_html.endswith("```"):
                            card_html = card_html[:-3].strip()

                    plain_summary = _re.sub(r"<[^>]+>", "", card_html)
                    try:
                        db_singleton.save_rich_result(
                            kind="recipe",
                            topic=q_txt,
                            query=q_txt,
                            html_text=card_html,
                            plain_text=plain_summary,
                            device_mac=mac,
                        )
                    except Exception as cache_ex:
                        tool_logger.warning(f"Failed to cache recipe rich result: {cache_ex}")

                    tg_target = mac[3:] if mac.startswith("tg_") and len(mac) > 3 else "family"
                    tg_dest_label = "этот Telegram-чат" if mac.startswith("tg_") else "общий семейный Telegram-чат"
                    tg_sent = False
                    try:
                        try:
                            from plugins.telegram_bot import TelegramBotPlugin
                        except ImportError:
                            from .plugins.telegram_bot import TelegramBotPlugin
                        tg_prov = db_singleton.get_provider("builtin.telegram_bot", mask_secrets=False)
                        if tg_prov and tg_prov.get("enabled"):
                            tg_res = await TelegramBotPlugin.send_message(
                                db=db_singleton,
                                provider=tg_prov,
                                ha_api=self.ha_api,
                                target=tg_target,
                                text=card_html,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                            tg_sent = tg_res.get("status") == "ok"
                            if not tg_sent:
                                tool_logger.warning(f"Recipe Telegram send failed ({tg_target}): {tg_res.get('error')}")
                    except Exception as tg_ex:
                        tool_logger.warning(f"Recipe Telegram send exception: {tg_ex}")

                    if turn_state is not None:
                        turn_state["tool_executed"] = True
                    return {
                        "status": "ok",
                        "mode": mode,
                        "source_url": source_url,
                        "sent_to_telegram": tg_sent,
                        "card_sent_in_same_chat": bool(tg_sent and mac.startswith("tg_")),
                        "telegram_target": tg_dest_label,
                        "instruction": (
                            f"Полный оформленный рецепт со всеми точными граммовками и шагами УЖЕ отправлен в {tg_dest_label}. "
                            "НЕ зачитывай всю простыню целиком и НЕ читай ссылки. Кратко (2–3 предложения) назови ключевые ингредиенты/пропорции "
                            f"и главную суть приготовления, и сообщи, что подробный рецепт с граммовками отправлен в {tg_dest_label}."
                            if tg_sent
                            else "Озвучь рецепт с точными ингредиентами, граммовками и шагами из summary."
                        ),
                        "summary": plain_summary[:2500],
                    }

                # Обычный веб-поиск фактов, новостей, погоды (в ТГ автоматически НЕ отправляем, но кэшируем на 3 дня)
                if on_progress:
                    try:
                        await on_progress("🔍 Ищу актуальную информацию в интернете...")
                    except Exception:
                        pass

                if serper_key:
                    serper_res = await serper_search_singleton.search(q_txt, serper_key, num=8)
                    if serper_res.get("success") and serper_res.get("answer"):
                        tool_logger.info(
                            f"Web search via serper.dev completed in {serper_res.get('latency_ms')} ms "
                            f"({serper_res.get('results_count', 0)} results, answer_box={serper_res.get('has_answer_box')})"
                        )
                        items = serper_res.get("items") or []
                        raw_snippets = serper_res["answer"]
                        today_str = datetime.datetime.now().strftime("%Y-%m-%d (%d.%m.%Y)")
                        answer_txt = f"[Актуальная дата: {today_str}]\n{raw_snippets}"
                        verified_by = ""

                        # Умный слой фактчекинга: для нетривиальных запросов (без быстрого answerBox)
                        # прогоняем 8 сниппетов через пул текстовых моделей для сверки дат, статусов и противоречий
                        if not serper_res.get("has_answer_box") and options.get("big_models_enabled"):
                            verify_prompt = (
                                f"Текущая дата: {today_str}.\n"
                                f"Запрос пользователя: {q_txt}\n\n"
                                f"Ниже 8 результатов поиска Google с датами и доменами.\n"
                                f"Задача (строгий фактчек и синтез):\n"
                                f"1. Сопоставь ВСЕ источники относительно текущей даты ({today_str}).\n"
                                f"2. Актуальность — ключевое свойство факта: если старый источник пишет одно (например, ранний срок или закрытие), "
                                f"а более свежий или официальный — продление, перенос, обновление или иной статус, приоритет у актуального; "
                                f"если есть реальное противоречие — явно отметь его.\n"
                                f"3. Дай сжатую, точную, проверенную выжимку на русском языке: ключевые факты, точные даты, цифры, адреса, цены, режим работы и актуальный статус.\n"
                                f"4. Без воды, без приветствий и без выдумок — опирайся строго на факты из выдачи.\n\n"
                                f"РЕЗУЛЬТАТЫ ПОИСКА:\n{raw_snippets[:12000]}"
                            )
                            try:
                                v_res = await big_model_pool_singleton.run_text_task(
                                    verify_prompt,
                                    options,
                                    enable_google_search=False,
                                    preferred_model=pref_model,
                                )
                                if v_res.get("success") and (v_res.get("text") or "").strip():
                                    answer_txt = v_res["text"].strip()
                                    verified_by = str(v_res.get("model") or "big_model_pool")
                                    tool_logger.info(
                                        f"Web search fact-checked via {verified_by} ({len(answer_txt)} chars)"
                                    )
                            except Exception as v_ex:
                                tool_logger.warning(f"Web search fact-check skipped: {v_ex}")

                        try:
                            db_singleton.save_rich_result(
                                kind="web_search",
                                topic=q_txt,
                                query=q_txt,
                                html_text=_format_fact_html(q_txt, answer_txt, items),
                                plain_text=answer_txt,
                                device_mac=mac,
                            )
                        except Exception as cache_ex:
                            tool_logger.warning(f"Failed to cache serper rich result: {cache_ex}")
                        if turn_state is not None:
                            turn_state["tool_executed"] = True
                        out_payload: Dict[str, Any] = {
                            "status": "ok",
                            "provider": "serper.dev",
                            "latency_ms": serper_res.get("latency_ms"),
                            "today": today_str,
                            "answer": answer_txt,
                        }
                        if verified_by:
                            out_payload["verified_by"] = verified_by
                        return out_payload
                    tool_logger.warning(
                        f"Serper.dev failed ({serper_res.get('error')}), falling back to web_search_model / BigModelPool"
                    )

                search_prompt = (
                    f"Найди актуальную информацию в интернете и дай точный, краткий ответ на русском языке "
                    f"(с датами, цифрами и фактами без воды):\n{q_txt}"
                )
                big_res = await big_model_pool_singleton.run_text_task(
                    search_prompt,
                    options,
                    enable_google_search=True,
                    preferred_model=pref_model,
                )
                if turn_state is not None:
                    turn_state["tool_executed"] = True
                if big_res.get("success"):
                    answer_txt = big_res["text"]
                    try:
                        db_singleton.save_rich_result(
                            kind="web_search",
                            topic=q_txt,
                            query=q_txt,
                            html_text=_format_fact_html(q_txt, answer_txt, []),
                            plain_text=answer_txt,
                            device_mac=mac,
                        )
                    except Exception as cache_ex:
                        tool_logger.warning(f"Failed to cache fallback web_search rich result: {cache_ex}")
                    return {"status": "ok", "model": big_res["model"], "answer": answer_txt}
                return {"status": "error", "error": big_res.get("error") or "Web search failed"}

            if name == "ask_big_model":
                try:
                    from big_models import big_model_pool_singleton
                except ImportError:
                    from .big_models import big_model_pool_singleton
                if on_progress:
                    try:
                        await on_progress("🔍 Передаю сложную задачу большой модели, скоро вернусь...")
                    except Exception:
                        pass
                task_txt = str(args.get("task") or "").strip()
                ctx_txt = str(args.get("context") or "").strip()
                full_prompt = f"{task_txt}\n\nКонтекст:\n{ctx_txt}" if ctx_txt else task_txt
                big_res = await big_model_pool_singleton.run_text_task(full_prompt, options)
                if turn_state is not None:
                    turn_state["tool_executed"] = True
                if big_res.get("success"):
                    return {"status": "ok", "model": big_res["model"], "answer": big_res["text"]}
                return {"status": "error", "error": big_res.get("error") or "Big model pool failed"}

            if plugin_registry_singleton and plugin_registry_singleton.handles_tool(name):
                res = await plugin_registry_singleton.execute_tool(
                    name,
                    args,
                    {
                        "options": options,
                        "dev": dev,
                        "ha_api": self.ha_api,
                        "turn_state": turn_state,
                        "device_manager": self.device_manager,
                        "user_query": user_query,
                        "on_progress": on_progress,
                    },
                )
                if turn_state is not None:
                    turn_state["tool_executed"] = True
                return res

            tool_logger.warning(f"Unknown tool called: {name}")
            return {"status": "unknown_tool", "error": f"Unknown tool: {name}"}

        except Exception as e:
            tool_logger.error(f"Error executing tool {name}: {e}")
            if on_feedback:
                try:
                    await on_feedback("error", False)
                except Exception:
                    pass
            return {"status": "error", "error": str(e)}
