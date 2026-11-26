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


def _is_live_model(model_id: str) -> bool:
    low = (model_id or "").lower()
    return ("live" in low) or ("native-audio" in low)


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

    async def _call_live_model(
        self,
        model_id: str,
        api_key: str,
        prompt: str,
        *,
        system_instruction: str = "",
        enable_google_search: bool = False,
        timeout_s: float = 45.0,
    ) -> str:
        if genai is None or types is None:
            raise RuntimeError("google-genai SDK is not available")
        client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})
        cfg_kwargs: Dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            "output_audio_transcription": types.AudioTranscriptionConfig(),
        }
        if system_instruction:
            cfg_kwargs["system_instruction"] = system_instruction
        if enable_google_search and hasattr(types, "Tool") and hasattr(types, "GoogleSearch"):
            cfg_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

        live_cfg = types.LiveConnectConfig(**cfg_kwargs)
        out_parts: List[str] = []

        async def _run_live() -> str:
            async with client.aio.live.connect(model=model_id, config=live_cfg) as session:
                await session.send_client_content(
                    turns=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                    turn_complete=True,
                )
                done = False
                while not done:
                    got = False
                    async for response in session.receive():
                        got = True
                        sc = getattr(response, "server_content", None)
                        if not sc:
                            continue
                        out_tr = getattr(sc, "output_transcription", None)
                        if out_tr and getattr(out_tr, "text", None):
                            out_parts.append(out_tr.text)
                        mt = getattr(sc, "model_turn", None)
                        if mt and getattr(mt, "parts", None):
                            for p in mt.parts:
                                if getattr(p, "thought", False):
                                    continue
                                if getattr(p, "text", None):
                                    out_parts.append(p.text)
                        if getattr(sc, "turn_complete", False):
                            done = True
                            break
                    if not got:
                        break
            return "".join(out_parts).strip()

        clean = await asyncio.wait_for(_run_live(), timeout=timeout_s)
        if not clean:
            raise RuntimeError(f"Live model '{model_id}' returned an empty response")
        return clean

    async def _call_single_model(
        self,
        model_id: str,
        api_key: str,
        prompt: str,
        *,
        system_instruction: str = "",
        enable_google_search: bool = False,
        timeout_s: float = 45.0,
    ) -> str:
        if _is_live_model(model_id):
            return await self._call_live_model(
                model_id,
                api_key,
                prompt,
                system_instruction=system_instruction,
                enable_google_search=enable_google_search,
                timeout_s=timeout_s,
            )

        if genai is None:
            raise RuntimeError("google-genai SDK is not available")
        client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
        cfg = None
        if types is not None and hasattr(types, "GenerateContentConfig"):
            cfg_kwargs: Dict[str, Any] = {}
            if system_instruction:
                cfg_kwargs["system_instruction"] = system_instruction
            if enable_google_search and hasattr(types, "Tool") and hasattr(types, "GoogleSearch"):
                cfg_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
            if cfg_kwargs:
                try:
                    cfg = types.GenerateContentConfig(**cfg_kwargs)
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

    async def test_model(
        self,
        model_id: str,
        api_key_str: str,
        *,
        enable_google_search: bool = False,
    ) -> Dict[str, Any]:
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
        test_prompt = (
            "Какая сегодня дата и какой курс доллара к рублю? Ответь одним коротким предложением."
            if enable_google_search
            else "Ответь одним словом: ОК"
        )
        test_timeout = 30.0 if enable_google_search else 20.0
        for idx, key in enumerate(keys):
            try:
                txt = await self._call_single_model(
                    mid,
                    key,
                    test_prompt,
                    enable_google_search=enable_google_search,
                    timeout_s=test_timeout,
                )
                dur = int((time.monotonic() - t0) * 1000)
                self._record_health(mid, True, dur, "")
                return {
                    "success": True,
                    "model": mid,
                    "status": "ok",
                    "latency_ms": dur,
                    "reply": txt[:140],
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
        enable_google_search: bool = False,
        timeout_s: float = 45.0,
        preferred_model: str = "",
    ) -> Dict[str, Any]:
        pool_raw = options.get("big_models_pool") or []
        pool_candidates = [
            item
            for item in pool_raw
            if isinstance(item, dict) and item.get("enabled", True) and str(item.get("model") or "").strip()
        ]
        pool_candidates.sort(key=lambda x: int(x.get("priority", 10)))

        candidates: List[Dict[str, Any]] = []
        seen: set[str] = set()
        pref = (preferred_model or "").strip()
        if pref:
            candidates.append({"model": pref, "priority": 0})
            seen.add(pref)
        for item in pool_candidates:
            m = str(item.get("model") or "").strip()
            if m and m not in seen:
                candidates.append(item)
                seen.add(m)

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
                        enable_google_search=enable_google_search,
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
