import html
import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger("serper_search")

SERPER_API_URL = "https://google.serper.dev/search"


class SerperSearchClient:
    """Асинхронный клиент быстрого поиска Google через Serper.dev с persistent HTTP-сессией."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=10, keepalive_timeout=60)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @staticmethod
    def _format_results(data: Dict[str, Any], num: int) -> str:
        parts: List[str] = []

        answer_box = data.get("answerBox")
        if isinstance(answer_box, dict):
            box_bits = [
                str(answer_box.get(k) or "").strip()
                for k in ("title", "answer", "snippet")
                if str(answer_box.get(k) or "").strip()
            ]
            if box_bits:
                parts.append("Быстрый ответ: " + " — ".join(box_bits))

        kg = data.get("knowledgeGraph")
        if isinstance(kg, dict):
            kg_title = str(kg.get("title") or "").strip()
            kg_type = str(kg.get("type") or "").strip()
            kg_desc = str(kg.get("description") or "").strip()
            kg_head = f"{kg_title} ({kg_type})" if kg_title and kg_type else kg_title
            kg_lines: List[str] = []
            if kg_head:
                kg_lines.append(kg_head)
            if kg_desc:
                kg_lines.append(kg_desc)
            attrs = kg.get("attributes")
            if isinstance(attrs, dict):
                for k, v in list(attrs.items())[:4]:
                    if str(v or "").strip():
                        kg_lines.append(f"{k}: {v}")
            if kg_lines:
                parts.append("Справка: " + "; ".join(kg_lines))

        organic = data.get("organic")
        if isinstance(organic, list) and organic:
            for idx, item in enumerate(organic[:num], start=1):
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                date_str = str(item.get("date") or "").strip()
                snippet = str(item.get("snippet") or "").strip()
                link = str(item.get("link") or "").strip()
                domain = ""
                if link:
                    try:
                        domain = (urlparse(link).netloc or "").replace("www.", "")
                    except Exception:
                        domain = ""
                header = f"{idx}. {title}" if title else f"{idx}."
                if date_str:
                    header += f" [{date_str}]"
                if domain:
                    header += f" ({domain})"
                if snippet:
                    parts.append(f"{header}: {snippet}")
                elif title:
                    parts.append(header)

        return "\n".join(parts).strip()

    async def search(
        self,
        query: str,
        api_key: str,
        *,
        gl: str = "ru",
        hl: str = "ru",
        num: int = 8,
        timeout_s: float = 8.0,
    ) -> Dict[str, Any]:
        """Выполняет поиск через Serper.dev и возвращает компактный текстовый контекст."""
        q = str(query or "").strip()
        key = str(api_key or "").strip()
        if not q:
            return {"success": False, "provider": "serper.dev", "latency_ms": 0, "error": "Пустой поисковый запрос"}
        if not key:
            return {"success": False, "provider": "serper.dev", "latency_ms": 0, "error": "Не указан API-ключ Serper.dev"}

        t0 = time.monotonic()
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(total=timeout_s)
            payload = {"q": q, "gl": gl, "hl": hl, "num": num}
            headers = {"X-API-KEY": key, "Content-Type": "application/json"}

            async with session.post(SERPER_API_URL, json=payload, headers=headers, timeout=timeout) as resp:
                latency_ms = int((time.monotonic() - t0) * 1000)
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    return {
                        "success": False,
                        "provider": "serper.dev",
                        "latency_ms": latency_ms,
                        "error": f"HTTP {resp.status}: {body}",
                    }
                data = await resp.json()
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {
                "success": False,
                "provider": "serper.dev",
                "latency_ms": latency_ms,
                "error": str(e),
            }

        formatted = self._format_results(data if isinstance(data, dict) else {}, num)
        organic = data.get("organic") if isinstance(data, dict) else None
        results_count = len(organic) if isinstance(organic, list) else 0
        items: List[Dict[str, str]] = []
        if isinstance(organic, list):
            for item in organic[:num]:
                if not isinstance(item, dict):
                    continue
                items.append({
                    "title": str(item.get("title") or "").strip(),
                    "link": str(item.get("link") or "").strip(),
                    "snippet": str(item.get("snippet") or "").strip(),
                    "date": str(item.get("date") or "").strip(),
                })
        if not formatted:
            return {
                "success": False,
                "provider": "serper.dev",
                "latency_ms": latency_ms,
                "error": "Serper.dev вернул пустую выдачу",
            }

        has_answer_box = isinstance(data, dict) and isinstance(data.get("answerBox"), dict) and bool(data.get("answerBox"))
        return {
            "success": True,
            "provider": "serper.dev",
            "latency_ms": latency_ms,
            "answer": formatted,
            "items": items,
            "results_count": results_count,
            "has_answer_box": has_answer_box,
            "credits": data.get("credits", 1) if isinstance(data, dict) else 1,
        }

    async def fetch_page_text(
        self,
        url: str,
        *,
        max_chars: int = 8000,
        timeout_s: float = 6.0,
    ) -> str:
        """Скачивает HTML-страницу по URL и возвращает очищенный текст (для извлечения полных рецептов)."""
        target_url = str(url or "").strip()
        if not (target_url.startswith("http://") or target_url.startswith("https://")):
            return ""
        try:
            session = await self._get_session()
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
            }
            async with session.get(
                target_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return ""
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if ctype and ("text/" not in ctype and "html" not in ctype):
                    return ""
                raw_bytes = await resp.content.read(512 * 1024)
                raw_html = raw_bytes.decode(resp.charset or "utf-8", errors="ignore")
        except Exception as e:
            logger.debug(f"fetch_page_text failed for {target_url}: {e}")
            return ""

        cleaned = re.sub(r"<!--.*?-->", " ", raw_html, flags=re.DOTALL)
        cleaned = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
        cleaned = re.sub(r"</?(p|div|li|br|h[1-6]|tr)[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = html.unescape(cleaned)
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in cleaned.splitlines()]
        text = "\n".join(ln for ln in lines if ln)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:max_chars].strip()


serper_search_singleton = SerperSearchClient()
