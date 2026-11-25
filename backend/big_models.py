from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any, Dict, List

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

logger = logging.getLogger("big_models")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _is_quota_or_rate_error(err_text: str) -> bool:
    low = (err_text or "").lower()
    return any(
        tok in low
        for tok in ("429", "quota", "resource_exhausted", "resourceexhausted", "rate limit", "too many requests")
    )


class BigModelPool:
    """
    Единый серверный пул больших моделей Google Gemini с приоритетами,
    ротацией API-ключей при 429/квоте и отслеживанием состояния здоровья моделей.
    """

    def __init__(self) -> None:
        self._health: Dict[str, Dict[str, Any]] = {}
        self._key_offset: int = 0

    def get_health_map(self) -> Dict[str, Dict[str, Any]]:
        return {k: dict(v) for k, v in self._health.items()}

    def _record_health(self, model_id: str, ok: bool, latency_ms: int = 0, error: str = "") -> None:
        self._health[model_id] = {
            "status": "ok" if ok else "error",
            "last_latency_ms": int(latency_ms),
            "last_error": (error or "")[:240],
            "last_checked_at": _now_iso(),
        }

    def get_api_keys(self, api_key_str: str) -> List[str]:
        keys = [k.strip() for k in (api_key_str or "").split(",") if k.strip()]
        if len(keys) <= 1:
            return keys
        start = self._key_offset % len(keys)
        self._key_offset = (self._key_offset + 1) % len(keys)
        return keys[start:] + keys[:start]

    async def _call_single_model(
        self,
        model_id: str,
        api_key: str,
        prompt: str,
        *,
        system_instruction: str = "",
        timeout_s: float = 45.0,
    ) -> str:
        if genai is None:
            raise RuntimeError("google-genai SDK is not available")
        client = genai.Client(api_key=api_key)
        cfg = None
        if system_instruction and types is not None and hasattr(types, "GenerateContentConfig"):
            try:
                cfg = types.GenerateContentConfig(system_instruction=system_instruction)
            except Exception:
                cfg = None

        coro = client.aio.models.generate_content(
            model=model_id,
            contents=prompt,
            config=cfg,
        )
        resp = await asyncio.wait_for(coro, timeout=timeout_s)
        text = getattr(resp, "text", None)
        if not text and getattr(resp, "candidates", None):
            parts_txt = []
            for cand in resp.candidates:
                content = getattr(cand, "content", None)
                for p in getattr(content, "parts", []) or []:
                    if getattr(p, "text", None):
                        parts_txt.append(p.text)
            text = "".join(parts_txt)
        clean = (text or "").strip()
        if not clean:
            raise RuntimeError(f"Model '{model_id}' returned an empty response")
        return clean

    async def test_model(self, model_id: str, api_key_str: str) -> Dict[str, Any]:
        mid = (model_id or "").strip()
        if not mid:
            return {"success": False, "error": "Не указана модель"}
        keys = self.get_api_keys(api_key_str)
        if not keys:
            err = "Не задан Gemini API Key"
            self._record_health(mid, False, 0, err)
            return {"success": False, "model": mid, "status": "error", "error": err, "health": self.get_health_map()}

        t0 = time.monotonic()
        last_err = ""
        for idx, key in enumerate(keys):
            try:
                txt = await self._call_single_model(
                    mid,
                    key,
                    "Ответь одним словом: ОК",
                    timeout_s=20.0,
                )
                dur = int((time.monotonic() - t0) * 1000)
                self._record_health(mid, True, dur, "")
                return {
                    "success": True,
                    "model": mid,
                    "status": "ok",
                    "latency_ms": dur,
                    "reply": txt[:80],
                    "key_index": idx + 1,
                    "health": self.get_health_map(),
                }
            except Exception as e:
                last_err = str(e)
                if _is_quota_or_rate_error(last_err) and idx + 1 < len(keys):
                    continue
                break

        dur = int((time.monotonic() - t0) * 1000)
        self._record_health(mid, False, dur, last_err)
        return {
            "success": False,
            "model": mid,
            "status": "error",
            "latency_ms": dur,
            "error": last_err,
            "health": self.get_health_map(),
        }

    async def run_text_task(
        self,
        prompt: str,
        options: Dict[str, Any],
        *,
        system_instruction: str = "",
        timeout_s: float = 45.0,
    ) -> Dict[str, Any]:
        pool_raw = options.get("big_models_pool") or []
        candidates = [
            item
            for item in pool_raw
            if isinstance(item, dict) and item.get("enabled", True) and str(item.get("model") or "").strip()
        ]
        candidates.sort(key=lambda x: int(x.get("priority", 10)))

        if not candidates:
            return {
                "success": False,
                "error": "Пул больших моделей пуст или отключён. Добавьте модели во вкладке «Мозг & Вейкворд».",
            }

        keys = self.get_api_keys(options.get("gemini_api_key", ""))
        if not keys:
            return {"success": False, "error": "Не задан Gemini API Key"}

        last_err = ""
        for item in candidates:
            mid = str(item["model"]).strip()
            t0 = time.monotonic()
            for k_idx, key in enumerate(keys):
                try:
                    txt = await self._call_single_model(
                        mid,
                        key,
                        prompt,
                        system_instruction=system_instruction,
                        timeout_s=timeout_s,
                    )
                    dur = int((time.monotonic() - t0) * 1000)
                    self._record_health(mid, True, dur, "")
                    logger.info(f"[BIG-POOL] Success via '{mid}' (prio={item.get('priority')}, {dur}ms, {len(txt)} chars)")
                    return {
                        "success": True,
                        "text": txt,
                        "model": mid,
                        "latency_ms": dur,
                    }
                except Exception as e:
                    last_err = f"{mid}: {e}"
                    dur = int((time.monotonic() - t0) * 1000)
                    logger.warning(f"[BIG-POOL] Attempt failed on '{mid}' (key #{k_idx + 1}/{len(keys)}): {e}")
                    self._record_health(mid, False, dur, str(e))
                    if _is_quota_or_rate_error(str(e)) and k_idx + 1 < len(keys):
                        continue
                    # При фатальной ошибке модели (404, 400, timeout, или все ключи в 429) переходим к следующей модели по приоритету
                    break

        return {
            "success": False,
            "error": f"Все большие модели из пула не ответили ({last_err})",
        }


big_model_pool_singleton = BigModelPool()
