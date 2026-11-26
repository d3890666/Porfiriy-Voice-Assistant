from __future__ import annotations

import asyncio
import datetime
import json
import logging
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from .audio_utils import get_earcon_pcm
    from .big_models import big_model_pool_singleton
    from .db import PorfiriyDB, db_singleton
    from .plugin_registry import plugin_registry_singleton
    from .plugins.telegram_bot import TelegramBotPlugin
except ImportError:
    from audio_utils import get_earcon_pcm
    from big_models import big_model_pool_singleton
    from db import PorfiriyDB, db_singleton
    from plugin_registry import plugin_registry_singleton
    from plugins.telegram_bot import TelegramBotPlugin

logger = logging.getLogger("scheduler")

_DOW_MAP = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


def _parse_cron_field(expr: str, min_v: int, max_v: int, is_dow: bool = False) -> Set[int]:
    """Парсит одно поле 5-польного cron-выражения."""
    out: Set[int] = set()
    for part in expr.split(","):
        p = part.strip().lower()
        if not p:
            raise ValueError(f"Empty cron part in '{expr}'")
        step = 1
        if "/" in p:
            base, step_s = p.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"Invalid cron step in '{expr}'")
        else:
            base = p

        if base == "*":
            lo, hi = min_v, max_v
        elif "-" in base:
            lo_s, hi_s = base.split("-", 1)
            lo = _DOW_MAP[lo_s] if is_dow and lo_s in _DOW_MAP else int(lo_s)
            hi = _DOW_MAP[hi_s] if is_dow and hi_s in _DOW_MAP else int(hi_s)
        else:
            val = _DOW_MAP[base] if is_dow and base in _DOW_MAP else int(base)
            lo = hi = val

        if is_dow:
            if lo == 7:
                lo = 0
            if hi == 7:
                hi = 0
            if lo <= hi:
                vals = range(lo, hi + 1, step)
            else:
                vals = [v % 7 for v in range(lo, hi + 8, step)]
            for v in vals:
                if min_v <= v <= max_v:
                    out.add(v)
        else:
            if lo < min_v or hi > max_v or lo > hi:
                raise ValueError(f"Cron value out of range [{min_v}..{max_v}]: '{p}'")
            out.update(range(lo, hi + 1, step))

    if not out:
        raise ValueError(f"Empty cron set for '{expr}'")
    return out


def compute_next_cron_utc(expr: str, after_utc: Optional[datetime.datetime] = None) -> datetime.datetime:
    """Вычисляет следующее срабатывание 5-польного cron в локальной TZ, возвращает UTC datetime."""
    fields = (expr or "").strip().split()
    if len(fields) != 5:
        raise ValueError("Cron должен содержать ровно 5 полей: '<мин> <час> <день> <месяц> <день_недели>'")

    minutes = _parse_cron_field(fields[0], 0, 59)
    hours = _parse_cron_field(fields[1], 0, 23)
    doms = _parse_cron_field(fields[2], 1, 31)
    months = _parse_cron_field(fields[3], 1, 12)
    dows = _parse_cron_field(fields[4], 0, 7, is_dow=True)

    dom_wild = fields[2].strip() == "*"
    dow_wild = fields[4].strip() == "*"

    now_utc = after_utc or datetime.datetime.now(datetime.timezone.utc)
    local_tz = datetime.datetime.now().astimezone().tzinfo
    cand = now_utc.astimezone(local_tz).replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)

    for _ in range(366 * 24 * 60):
        if cand.month not in months:
            # Перескакиваем на 1 число следующего месяца
            if cand.month == 12:
                cand = cand.replace(year=cand.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                cand = cand.replace(month=cand.month + 1, day=1, hour=0, minute=0)
            continue

        # Стандартный cron: в Python Monday=0..Sunday=6 -> cron Sunday=0..Saturday=6
        cron_dow = (cand.weekday() + 1) % 7
        if dom_wild and dow_wild:
            day_ok = True
        elif dom_wild:
            day_ok = cron_dow in dows
        elif dow_wild:
            day_ok = cand.day in doms
        else:
            day_ok = (cand.day in doms) or (cron_dow in dows)

        if not day_ok:
            cand = (cand + datetime.timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if cand.hour not in hours:
            cand = (cand + datetime.timedelta(hours=1)).replace(minute=0)
            continue
        if cand.minute not in minutes:
            cand += datetime.timedelta(minutes=1)
            continue
        return cand.astimezone(datetime.timezone.utc)

    raise ValueError(f"Cron expression '{expr}' has no trigger within 366 days")


def _parse_iso_utc(raw: Optional[str]) -> Optional[datetime.datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        local_tz = datetime.datetime.now().astimezone().tzinfo
        dt = dt.replace(tzinfo=local_tz)
    return dt.astimezone(datetime.timezone.utc)


def _is_in_quiet_hours(quiet_hours: str, now_local: Optional[datetime.datetime] = None) -> bool:
    qh = (quiet_hours or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", qh)
    if not m:
        return False
    h1, m1, h2, m2 = map(int, m.groups())
    if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59):
        return False
    cur = now_local or datetime.datetime.now().astimezone()
    cur_min = cur.hour * 60 + cur.minute
    start_min = h1 * 60 + m1
    end_min = h2 * 60 + m2
    if start_min <= end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min


class SchedulerEngine:
    """Универсальный персистентный планировщик задач (SQLite, HA events, direct_tool + ai_prompt)."""

    def __init__(self) -> None:
        self.db: PorfiriyDB = db_singleton
        self.ha_api: Any = None
        self.tool_executor: Any = None
        self.device_manager: Any = None
        self.options_callback: Optional[Callable[[], Dict[str, Any]]] = None

        self._loop_task: Optional[asyncio.Task] = None
        self._running_jobs: Set[str] = set()
        self._ha_hold_tasks: Dict[str, asyncio.Task] = {}
        self._chrome_sem = asyncio.Semaphore(1)
        self._stop_event = asyncio.Event()

    def configure(
        self,
        *,
        ha_api: Any,
        tool_executor: Any,
        device_manager: Any = None,
        options_callback: Optional[Callable[[], Dict[str, Any]]] = None,
        db: Optional[PorfiriyDB] = None,
    ) -> None:
        if db is not None:
            self.db = db
        self.ha_api = ha_api
        self.tool_executor = tool_executor
        self.device_manager = device_manager
        self.options_callback = options_callback

    async def start(self) -> None:
        if self._loop_task and not self._loop_task.done():
            return
        self._stop_event.clear()
        await self._reconcile_on_startup()
        self._loop_task = asyncio.create_task(self._tick_loop(), name="porfiriy_scheduler_loop")
        logger.info("[SCHEDULER] Started persistent scheduler engine")

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
        for t in list(self._ha_hold_tasks.values()):
            t.cancel()
        self._ha_hold_tasks.clear()

    # ------------------------------------------------------------------ #
    # Валидация и вычисление next_run_at (Fail-Fast)
    # ------------------------------------------------------------------ #
    def validate_and_prepare(
        self,
        *,
        trigger: Dict[str, Any],
        action: Dict[str, Any],
        owner_user_id: str = "family",
        notify_channel: str = "",
        quiet_hours: str = "",
        one_shot: Optional[bool] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], str, Optional[str], bool]:
        """Валидирует trigger и action до записи в БД. Возвращает (trigger, action, cron_or_run_at, next_run_at, one_shot)."""
        if not isinstance(trigger, dict):
            raise ValueError("trigger должен быть JSON-объектом")
        if not isinstance(action, dict):
            raise ValueError("action должен быть JSON-объектом")

        trig = dict(trigger)
        act = dict(action)
        t_type = str(trig.get("type") or "").strip().lower()
        a_type = str(act.get("type") or "").strip().lower()

        if a_type not in ("direct_tool", "ai_prompt"):
            raise ValueError("action.type должен быть 'direct_tool' или 'ai_prompt'")

        if a_type == "direct_tool":
            tool_name = str(act.get("tool") or "").strip()
            if not tool_name:
                raise ValueError("Для action.type='direct_tool' обязательно поле action.tool")
            args = act.get("args")
            if args is not None and not isinstance(args, dict):
                raise ValueError("action.args должен быть словарём")
            act["args"] = dict(args or {})
            if tool_name == "call_ha_service":
                if not act["args"].get("domain") or not act["args"].get("service"):
                    raise ValueError("Для call_ha_service обязательны args.domain и args.service")
            elif tool_name == "send_to_telegram":
                if not act["args"].get("text"):
                    raise ValueError("Для send_to_telegram обязательно args.text")
                if not act["args"].get("target"):
                    act["args"]["target"] = owner_user_id or "family"
        else:
            if not str(act.get("prompt") or "").strip():
                raise ValueError("Для action.type='ai_prompt' обязательно непустое поле action.prompt")
            allowed = act.get("allowed_tools") or []
            if isinstance(allowed, str):
                allowed = [x.strip() for x in allowed.split(",") if x.strip()]
            act["allowed_tools"] = list(allowed)

        if quiet_hours and not re.fullmatch(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}", quiet_hours.strip()):
            raise ValueError("quiet_hours должен быть в формате 'HH:MM-HH:MM', например '23:00-08:00'")

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        uses_marketplace = (
            a_type == "ai_prompt"
            and any(
                t in ("compare_prices", "compare_verify_offer") or t.startswith(("ozon_", "avito_", "wb_", "dns_", "citilink_", "lamoda_"))
                for t in act.get("allowed_tools", [])
            )
        )

        resolved_one_shot = bool(one_shot) if one_shot is not None else (t_type == "once")
        cron_or_run_at = ""
        next_run_iso: Optional[str] = None

        if t_type == "once":
            delay_s = trig.get("delay_seconds")
            if delay_s is not None and str(delay_s).strip() != "":
                sec = int(delay_s)
                if sec <= 0 or sec > 30 * 86400:
                    raise ValueError("trigger.delay_seconds должен быть от 1 до 2592000 (30 дней)")
                run_dt = now_utc + datetime.timedelta(seconds=sec)
            else:
                run_dt = _parse_iso_utc(str(trig.get("run_at") or ""))
                if not run_dt:
                    raise ValueError("Для trigger.type='once' укажите delay_seconds или ISO-время run_at")
                if (now_utc - run_dt).total_seconds() > 60:
                    raise ValueError("Время срабатывания run_at уже в прошлом")
            next_run_iso = run_dt.replace(microsecond=0).isoformat()
            trig["run_at"] = next_run_iso
            cron_or_run_at = next_run_iso
            if one_shot is None:
                resolved_one_shot = True

        elif t_type == "interval":
            every_min = int(trig.get("every_minutes") or 0)
            if every_min < 1 or every_min > 43200:
                raise ValueError("trigger.every_minutes должен быть от 1 до 43200")
            if uses_marketplace and every_min < 60:
                raise ValueError("Для задач маркетплейсов минимальный интервал — 60 минут (защита от антибота)")
            if a_type == "ai_prompt" and every_min < 15:
                raise ValueError("Для ai_prompt задач минимальный интервал — 15 минут")
            jitter_min = max(0, min(60, int(trig.get("jitter_minutes") or 0)))
            trig["every_minutes"] = every_min
            trig["jitter_minutes"] = jitter_min
            jitter_s = random.randint(0, jitter_min * 60) if jitter_min > 0 else 0
            next_dt = now_utc + datetime.timedelta(minutes=every_min, seconds=jitter_s)
            next_run_iso = next_dt.replace(microsecond=0).isoformat()
            cron_or_run_at = f"every {every_min}m"

        elif t_type == "cron":
            expr = str(trig.get("expr") or "").strip()
            next_dt = compute_next_cron_utc(expr, after_utc=now_utc)
            trig["expr"] = expr
            next_run_iso = next_dt.replace(microsecond=0).isoformat()
            cron_or_run_at = expr

        elif t_type == "ha_event":
            eid = str(trig.get("entity_id") or "").strip()
            to_st = str(trig.get("to_state") or "").strip()
            if not eid or "." not in eid:
                raise ValueError("Для trigger.type='ha_event' укажите корректный entity_id (например, person.denis)")
            if not to_st:
                raise ValueError("Для trigger.type='ha_event' обязательно поле to_state (например, 'home')")
            trig["entity_id"] = eid
            trig["to_state"] = to_st
            trig["for_seconds"] = max(0, min(86400, int(trig.get("for_seconds") or 0)))
            trig["cooldown_seconds"] = max(0, min(86400, int(trig.get("cooldown_seconds") or 300)))
            cron_or_run_at = f"{eid}->{to_st}"
            next_run_iso = None
        else:
            raise ValueError("trigger.type должен быть одним из: 'once', 'interval', 'cron', 'ha_event'")

        return trig, act, cron_or_run_at, next_run_iso, resolved_one_shot

    def _compute_following_run(self, trigger: Dict[str, Any], from_utc: Optional[datetime.datetime] = None) -> Optional[str]:
        now_utc = from_utc or datetime.datetime.now(datetime.timezone.utc)
        t_type = str(trigger.get("type") or "").lower()
        if t_type == "interval":
            every_min = max(1, int(trigger.get("every_minutes") or 60))
            jitter_min = max(0, min(60, int(trigger.get("jitter_minutes") or 0)))
            jitter_s = random.randint(0, jitter_min * 60) if jitter_min > 0 else 0
            return (now_utc + datetime.timedelta(minutes=every_min, seconds=jitter_s)).replace(microsecond=0).isoformat()
        if t_type == "cron":
            expr = str(trigger.get("expr") or "").strip()
            if expr:
                return compute_next_cron_utc(expr, after_utc=now_utc).replace(microsecond=0).isoformat()
        return None

    # ------------------------------------------------------------------ #
    # CRUD & управление задачами
    # ------------------------------------------------------------------ #
    async def create_job(
        self,
        *,
        title: str,
        trigger: Dict[str, Any],
        action: Dict[str, Any],
        owner_user_id: str = "family",
        notify_channel: str = "",
        quiet_hours: str = "",
        one_shot: Optional[bool] = None,
        job_id: Optional[str] = None,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        # Доп. проверка HA entity_id если ha_api доступен
        if str(trigger.get("type") or "").lower() == "ha_event" and self.ha_api:
            eid = str(trigger.get("entity_id") or "").strip()
            st = await self.ha_api.get_entity_state(eid)
            if not st:
                raise ValueError(f"Сущность '{eid}' не найдена в Home Assistant")

        trig, act, cron_or_run_at, next_run_iso, resolved_one_shot = self.validate_and_prepare(
            trigger=trigger,
            action=action,
            owner_user_id=owner_user_id,
            notify_channel=notify_channel,
            quiet_hours=quiet_hours,
            one_shot=one_shot,
        )
        job_type = f"{trig.get('type')}:{act.get('type')}"
        job = self.db.upsert_scheduled_job(
            job_id=job_id,
            job_type=job_type,
            title=(title or cron_or_run_at).strip()[:160],
            owner_user_id=owner_user_id,
            cron_or_run_at=cron_or_run_at,
            trigger=trig,
            action=act,
            notify_channel=notify_channel.strip(),
            quiet_hours=quiet_hours.strip(),
            one_shot=resolved_one_shot,
            enabled=enabled,
            status="active",
            next_run_at=next_run_iso,
        )
        logger.info(f"[SCHEDULER] Created job {job['id']} '{job['title']}' ({job_type}, next={job.get('next_run_at')})")
        return job

    def find_jobs_by_query(self, query: str) -> List[Dict[str, Any]]:
        q = (query or "").strip().lower()
        jobs = self.db.list_scheduled_jobs()
        if not q:
            return jobs
        exact = [j for j in jobs if j["id"].lower() == q]
        if exact:
            return exact
        return [
            j for j in jobs
            if q in (j.get("title") or "").lower()
            or q in json.dumps(j.get("action") or {}, ensure_ascii=False).lower()
            or q in (j.get("cron_or_run_at") or "").lower()
        ]

    # ------------------------------------------------------------------ #
    # Misfire recovery & Тикер
    # ------------------------------------------------------------------ #
    async def _reconcile_on_startup(self) -> None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        jobs = self.db.list_scheduled_jobs(enabled_only=True, status="active")
        for job in jobs:
            trig = job.get("trigger") or {}
            t_type = str(trig.get("type") or "").lower()
            next_dt = _parse_iso_utc(job.get("next_run_at"))
            if not next_dt:
                continue
            if next_dt < now_utc:
                overdue_s = (now_utc - next_dt).total_seconds()
                if t_type == "once":
                    if overdue_s > 15 * 60:
                        self.db.record_job_run(
                            job["id"],
                            status="missed",
                            summary=f"Пропущено из-за простоя ({int(overdue_s // 60)} мин)",
                            error="Misfire > 15m",
                            next_run_at=None,
                            new_status="missed",
                            enabled=False,
                        )
                        await self._deliver_notification(
                            job,
                            html_text=f"⏰ <b>Пропущена задача:</b> {job['title']}\n<i>Сервер был выключен дольше 15 минут.</i>",
                            voice_text="",
                        )
                elif t_type in ("interval", "cron"):
                    new_next = self._compute_following_run(trig, from_utc=now_utc)
                    self.db.upsert_scheduled_job(
                        job_id=job["id"],
                        job_type=job["job_type"],
                        title=job["title"],
                        owner_user_id=job["owner_user_id"],
                        cron_or_run_at=job["cron_or_run_at"],
                        trigger=trig,
                        action=job["action"],
                        notify_channel=job["notify_channel"],
                        quiet_hours=job["quiet_hours"],
                        one_shot=job["one_shot"],
                        enabled=True,
                        status="active",
                        next_run_at=new_next,
                    )

    async def _tick_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                due = []
                for job in self.db.list_scheduled_jobs(enabled_only=True, status="active"):
                    if job["id"] in self._running_jobs:
                        continue
                    next_dt = _parse_iso_utc(job.get("next_run_at"))
                    if next_dt and next_dt <= now_utc:
                        due.append(job)

                for job in due:
                    self._running_jobs.add(job["id"])
                    asyncio.create_task(self._run_job_safe(job, reason="schedule"))
            except Exception as e:
                logger.warning(f"[SCHEDULER] Tick error: {e}")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ #
    # Обработка событий Home Assistant (ha_event + for_seconds)
    # ------------------------------------------------------------------ #
    async def on_ha_state_changed(self, entity_id: str, new_state: str, old_state: str) -> None:
        if not entity_id:
            return
        jobs = self.db.list_scheduled_jobs(enabled_only=True, status="active")
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        for job in jobs:
            trig = job.get("trigger") or {}
            if str(trig.get("type") or "").lower() != "ha_event":
                continue
            if str(trig.get("entity_id") or "").strip() != entity_id:
                continue

            to_target = str(trig.get("to_state") or "").strip()
            from_target = str(trig.get("from_state") or "").strip()
            for_s = int(trig.get("for_seconds") or 0)
            cooldown_s = int(trig.get("cooldown_seconds") or 300)
            jid = job["id"]

            if new_state == to_target and (not from_target or old_state == from_target):
                last_dt = _parse_iso_utc(job.get("last_run_at"))
                if cooldown_s > 0 and last_dt and (now_utc - last_dt).total_seconds() < cooldown_s:
                    continue
                old_task = self._ha_hold_tasks.pop(jid, None)
                if old_task and not old_task.done():
                    old_task.cancel()

                async def _delayed_fire(captured_job: Dict[str, Any], wait_s: int, target_eid: str, expected_state: str):
                    try:
                        if wait_s > 0:
                            await asyncio.sleep(wait_s)
                        if self.ha_api and wait_s > 0:
                            cur = await self.ha_api.get_entity_state(target_eid)
                            if isinstance(cur, dict) and cur.get("state") != expected_state:
                                return
                        if captured_job["id"] not in self._running_jobs:
                            self._running_jobs.add(captured_job["id"])
                            await self._run_job_safe(captured_job, reason="ha_event")
                    except asyncio.CancelledError:
                        logger.info(f"[SCHEDULER] Hold timer cancelled for {captured_job['id']} ({target_eid} left '{expected_state}')")
                    finally:
                        self._ha_hold_tasks.pop(captured_job["id"], None)

                self._ha_hold_tasks[jid] = asyncio.create_task(_delayed_fire(job, for_s, entity_id, to_target))
            elif new_state != to_target:
                pending = self._ha_hold_tasks.pop(jid, None)
                if pending and not pending.done():
                    pending.cancel()

    # ------------------------------------------------------------------ #
    # Выполнение задачи + изоляция ошибок + доставка
    # ------------------------------------------------------------------ #
    async def run_job_now(self, job_id: str) -> Dict[str, Any]:
        job = self.db.get_scheduled_job(job_id)
        if not job:
            return {"status": "error", "error": f"Задача '{job_id}' не найдена"}
        if job["id"] in self._running_jobs:
            return {"status": "error", "error": "Задача уже выполняется"}
        self._running_jobs.add(job["id"])
        return await self._run_job_safe(job, reason="manual")

    async def _run_job_safe(self, job: Dict[str, Any], *, reason: str = "schedule") -> Dict[str, Any]:
        jid = job["id"]
        t0 = time.monotonic()
        trig = job.get("trigger") or {}
        act = job.get("action") or {}
        a_type = str(act.get("type") or "").lower()
        t_type = str(trig.get("type") or "").lower()
        timeout_s = 90.0 if a_type == "ai_prompt" else 25.0

        try:
            result = await asyncio.wait_for(self._execute_action(job), timeout=timeout_s)
            dur_ms = int((time.monotonic() - t0) * 1000)

            done = bool(result.get("done")) or (bool(job.get("one_shot")) and reason != "manual")
            if reason == "manual" and not bool(result.get("done")) and t_type in ("interval", "cron"):
                next_run = job.get("next_run_at")
            elif done:
                next_run = None
            elif t_type == "once":
                next_run = None if reason != "manual" else job.get("next_run_at")
            else:
                next_run = self._compute_following_run(trig)

            new_status = "completed" if done else "active"
            new_enabled = False if done else True

            if result.get("notify"):
                await self._deliver_notification(
                    job,
                    html_text=str(result.get("message_html") or result.get("summary") or "").strip(),
                    voice_text=str(result.get("message_voice") or "").strip(),
                )

            self.db.record_job_run(
                jid,
                status="ok",
                duration_ms=dur_ms,
                summary=str(result.get("summary") or "Выполнено")[:300],
                next_state=result.get("next_state"),
                next_run_at=next_run,
                new_status=new_status,
                consecutive_errors=0,
                enabled=new_enabled,
            )
            logger.info(f"[SCHEDULER] Job {jid} '{job['title']}' OK ({dur_ms} ms, reason={reason})")
            return {"status": "ok", "duration_ms": dur_ms, **result}

        except Exception as e:
            dur_ms = int((time.monotonic() - t0) * 1000)
            err_txt = str(e) or type(e).__name__
            errs = int(job.get("consecutive_errors") or 0) + 1
            logger.warning(f"[SCHEDULER] Job {jid} '{job['title']}' FAILED ({errs}/5): {err_txt}")

            if errs >= 5:
                self.db.record_job_run(
                    jid,
                    status="error",
                    duration_ms=dur_ms,
                    error=err_txt,
                    next_run_at=None,
                    new_status="paused_error",
                    consecutive_errors=errs,
                    enabled=False,
                )
                await self._deliver_notification(
                    job,
                    html_text=(
                        f"⚠️ <b>Задача поставлена на паузу после 5 сбоев:</b> {job['title']}\n"
                        f"<code>{err_txt[:220]}</code>"
                    ),
                    voice_text="",
                )
            else:
                backoff_min = 10 if errs <= 2 else 30
                retry_next = (
                    datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=backoff_min)
                ).replace(microsecond=0).isoformat() if t_type in ("interval", "cron") else None
                new_st = "error" if t_type == "once" else "active"
                new_en = False if t_type == "once" else True
                self.db.record_job_run(
                    jid,
                    status="error",
                    duration_ms=dur_ms,
                    error=err_txt,
                    next_run_at=retry_next,
                    new_status=new_st,
                    consecutive_errors=errs,
                    enabled=new_en,
                )
            return {"status": "error", "error": err_txt, "consecutive_errors": errs}
        finally:
            self._running_jobs.discard(jid)

    async def _execute_action(self, job: Dict[str, Any]) -> Dict[str, Any]:
        act = job.get("action") or {}
        a_type = str(act.get("type") or "").lower()
        opts = self.options_callback() if self.options_callback else {}

        if a_type == "direct_tool":
            tool_name = str(act.get("tool") or "").strip()
            args = dict(act.get("args") or {})

            if tool_name == "alarm_chime":
                mac = str(args.get("mac") or "").strip()
                label = str(args.get("label") or job.get("title") or "Таймер").strip()
                rang = False
                if self.device_manager and mac:
                    ws = self.device_manager.active_sockets.get(mac)
                    if ws and not getattr(ws, "closed", False):
                        chime = get_earcon_pcm("smarthome", "apple_homepod", sample_rate=24000, volume=1.0)
                        if chime:
                            for _ in range(3):
                                await ws.send(chime)
                                await asyncio.sleep(0.45)
                            await ws.send(json.dumps({"type": "turn_complete"}))
                            rang = True
                return {
                    "notify": not rang,
                    "message_html": f"⏰ <b>Таймер сработал:</b> {label}",
                    "summary": f"Будильник '{label}' ({'сигнал на колонку' if rang else 'в Telegram'})",
                    "done": job.get("one_shot", True),
                }

            if tool_name == "send_to_telegram":
                tg_prov = self.db.get_provider("builtin.telegram_bot", mask_secrets=False)
                if not tg_prov or not tg_prov.get("enabled"):
                    raise RuntimeError("Провайдер builtin.telegram_bot отключён")
                target = str(args.get("target") or job.get("owner_user_id") or "family").strip()
                text = str(args.get("text") or "").strip()
                title = str(args.get("title") or "").strip()
                res = await TelegramBotPlugin.send_message(
                    db=self.db,
                    provider=tg_prov,
                    ha_api=self.ha_api,
                    target=target,
                    text=text,
                    title=title,
                    parse_mode="HTML" if ("<b>" in text or "<a " in text) else "",
                )
                if res.get("status") != "ok":
                    raise RuntimeError(res.get("error") or "Ошибка отправки в Telegram")
                return {
                    "notify": False,
                    "summary": f"Отправлено в Telegram ({target}): {text[:80]}",
                    "done": job.get("one_shot", False),
                }

            if not self.tool_executor:
                raise RuntimeError("ToolExecutor не подключён к планировщику")
            res = await self.tool_executor.execute(tool_name, args, opts)
            if isinstance(res, dict) and res.get("status") == "error":
                raise RuntimeError(res.get("error") or f"Ошибка инструмента {tool_name}")
            return {
                "notify": False,
                "summary": f"{tool_name}({json.dumps(args, ensure_ascii=False)[:120]}) -> ok",
                "done": job.get("one_shot", False),
            }

        # Универсальный ai_prompt с доступом к разрешённым инструментам (до 4 шагов)
        prompt = str(act.get("prompt") or "").strip()
        allowed_tools = list(act.get("allowed_tools") or [])
        prev_state = job.get("state") or {}
        tool_catalog = self._describe_allowed_tools(allowed_tools)
        observations: List[Dict[str, Any]] = []

        for step_idx in range(4):
            full_prompt = (
                f"Ты — фоновый планировщик Порфирия. Выполни задачу по расписанию.\n"
                f"ЗАДАЧА: {prompt}\n\n"
                f"ПРОШЛОЕ СОСТОЯНИЕ (state_json): {json.dumps(prev_state, ensure_ascii=False)}\n"
                f"УЖЕ ПОЛУЧЕННЫЕ ДАННЫЕ ПО ШАГАМ: {json.dumps(observations, ensure_ascii=False)[:40000]}\n\n"
                f"ДОСТУПНЫЕ ИНСТРУМЕНТЫ:\n{tool_catalog}\n\n"
                "ПРАВИЛА ОТВЕТА (ОТВЕЧАЙ СТРОГО ОДНИМ JSON-ОБЪЕКТОМ БЕЗ ЛИШНЕГО ТЕКСТА):\n"
                "- Если нужно вызвать инструмент (макс. 3 вызова), верни:\n"
                "  {\"call_tool\": {\"name\": \"...\", \"args\": {...}}}\n"
                "- Когда данных достаточно для итогового решения, верни:\n"
                "  {\"notify\": true/false, \"message_html\": \"текст для Telegram HTML (<b>, <a href=...>, цены цифрами)\", "
                "\"message_voice\": \"короткий текст 1-2 фразы\", \"next_state\": {...}, \"done\": true/false, \"summary\": \"краткий лог\"}\n"
                "- Если ничего важного не изменилось/условие не сработало — ставь \"notify\": false и обнови \"next_state\"."
            )
            llm_res = await big_model_pool_singleton.run_text_task(full_prompt, opts)
            if not llm_res.get("success"):
                raise RuntimeError(llm_res.get("error") or "BigModelPool failed")
            parsed = self._extract_json_object(llm_res.get("text") or "")

            call_spec = parsed.get("call_tool")
            if isinstance(call_spec, dict) and call_spec.get("name") and step_idx < 3:
                t_name = str(call_spec["name"]).strip()
                if allowed_tools and t_name not in allowed_tools:
                    observations.append({"tool": t_name, "error": f"Инструмент '{t_name}' не разрешён для этой задачи"})
                    continue
                t_args = call_spec.get("args") if isinstance(call_spec.get("args"), dict) else {}
                t_res = await self._call_scheduler_tool(t_name, t_args, opts)
                observations.append({"tool": t_name, "args": t_args, "result": t_res})
                continue

            next_state = parsed.get("next_state")
            if not isinstance(next_state, dict):
                next_state = prev_state
            return {
                "notify": bool(parsed.get("notify", False)),
                "message_html": str(parsed.get("message_html") or "").strip(),
                "message_voice": str(parsed.get("message_voice") or "").strip(),
                "next_state": next_state,
                "done": bool(parsed.get("done", False)),
                "summary": str(parsed.get("summary") or f"notify={bool(parsed.get('notify'))}")[:240],
            }

        raise RuntimeError("Превышен лимит шагов ai_prompt (4)")

    async def _call_scheduler_tool(self, tool_name: str, args: Dict[str, Any], opts: Dict[str, Any]) -> Any:
        if not self.tool_executor:
            return {"status": "error", "error": "ToolExecutor unavailable"}
        is_marketplace = tool_name in ("compare_prices", "compare_verify_offer") or tool_name.startswith(
            ("ozon_", "avito_", "wb_", "yandex_", "dns_", "citilink_", "lamoda_")
        )
        if is_marketplace:
            async with self._chrome_sem:
                return await self.tool_executor.execute(tool_name, args, opts)
        return await self.tool_executor.execute(tool_name, args, opts)

    @staticmethod
    def _describe_allowed_tools(allowed: List[str]) -> str:
        builtins = {
            "get_ha_state": "get_ha_state(entity_id) -> текущее состояние/атрибуты сущности Home Assistant",
            "call_ha_service": "call_ha_service(domain, service, entity_id?, parameters?) -> управление Home Assistant",
            "web_search": "web_search(query) -> быстрый поиск в интернете",
        }
        lines: List[str] = []
        active_specs = {s["name"]: s for s in plugin_registry_singleton.get_active_tool_specs()} if plugin_registry_singleton else {}
        names = allowed or (list(builtins.keys()) + list(active_specs.keys()))
        for name in names:
            if name in builtins:
                lines.append(f"- {builtins[name]}")
            elif name in active_specs:
                spec = active_specs[name]
                desc = (spec.get("description") or "").splitlines()[0][:140]
                props = list(((spec.get("parameters") or {}).get("properties") or {}).keys())
                lines.append(f"- {name}({', '.join(props)}): {desc}")
        return "\n".join(lines) if lines else "(инструменты не заданы)"

    @staticmethod
    def _extract_json_object(raw: str) -> Dict[str, Any]:
        txt = (raw or "").strip()
        if txt.startswith("```"):
            txt = txt.split("\n", 1)[-1]
            if txt.endswith("```"):
                txt = txt[:-3].strip()
        try:
            obj = json.loads(txt)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        m = re.search(r"\{[\s\S]*\}", txt)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
        raise ValueError(f"LLM вернула невалидный JSON: {txt[:180]}")

    async def _deliver_notification(
        self,
        job: Dict[str, Any],
        *,
        html_text: str = "",
        voice_text: str = "",
    ) -> None:
        in_quiet = _is_in_quiet_hours(job.get("quiet_hours") or "")
        channel = (job.get("notify_channel") or "").strip().lower()
        owner = (job.get("owner_user_id") or "family").strip().lower()

        if html_text:
            tg_prov = self.db.get_provider("builtin.telegram_bot", mask_secrets=False)
            if tg_prov and tg_prov.get("enabled"):
                target = owner
                if channel.startswith("tg_"):
                    target = channel[3:] or owner
                elif channel == "tg_family":
                    target = "family"
                await TelegramBotPlugin.send_message(
                    db=self.db,
                    provider=tg_prov,
                    ha_api=self.ha_api,
                    target=target,
                    text=html_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    disable_notification=in_quiet,
                )

        # Голосовое оповещение на активные устройства вне тихих часов пока реализуем через earcon/позже TTS
        if voice_text and not in_quiet and channel in ("voice", "tg_and_voice"):
            logger.info(f"[SCHEDULER] Voice notification ready for job {job['id']}: {voice_text[:120]}")


scheduler_singleton = SchedulerEngine()
