from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("mcp_client")

MCP_PROTOCOL_VERSION = "2025-03-26"


class McpHttpClient:
    """Асинхронный клиент MCP Streamable HTTP (JSON и SSE ответы, Mcp-Session-Id, Bearer auth)."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._mcp_sessions: Dict[Tuple[str, str], str] = {}
        self._req_id: int = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=16, keepalive_timeout=60)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        self._mcp_sessions.clear()

    @staticmethod
    def _build_headers(api_token: str = "", session_id: str = "", extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        tok = (api_token or "").strip()
        if tok:
            headers["Authorization"] = tok if tok.lower().startswith("bearer ") else f"Bearer {tok}"
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        if isinstance(extra_headers, dict):
            for k, v in extra_headers.items():
                if k and v is not None:
                    headers[str(k)] = str(v)
        return headers

    @staticmethod
    async def _parse_jsonrpc_response(resp: aiohttp.ClientResponse, expected_id: Optional[int] = None) -> Dict[str, Any]:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        raw_text = await resp.text()
        if not raw_text.strip():
            return {}

        if "text/event-stream" in ctype or raw_text.lstrip().startswith("event:") or raw_text.lstrip().startswith("data:"):
            frames: List[Dict[str, Any]] = []
            data_buf: List[str] = []
            for line in raw_text.splitlines():
                if line.startswith("data:"):
                    data_buf.append(line[5:].lstrip())
                elif not line.strip() and data_buf:
                    chunk = "\n".join(data_buf).strip()
                    data_buf = []
                    if chunk and chunk != "[DONE]":
                        try:
                            obj = json.loads(chunk)
                            if isinstance(obj, dict):
                                frames.append(obj)
                        except Exception:
                            pass
            if data_buf:
                chunk = "\n".join(data_buf).strip()
                if chunk and chunk != "[DONE]":
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            frames.append(obj)
                    except Exception:
                        pass
            if expected_id is not None:
                for f in frames:
                    if f.get("id") == expected_id:
                        return f
            for f in reversed(frames):
                if "result" in f or "error" in f:
                    return f
            return frames[-1] if frames else {}

        try:
            parsed = json.loads(raw_text)
            return parsed if isinstance(parsed, dict) else {"result": parsed}
        except Exception:
            return {"error": {"code": -32700, "message": f"Invalid JSON response: {raw_text[:250]}"}}

    async def _initialize(
        self,
        url: str,
        api_token: str = "",
        timeout_s: float = 15.0,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        http_sess = await self._get_http_session()
        req_id = self._next_id()
        init_payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "porfiriy-voice-assistant", "version": "1.0"},
            },
        }
        headers = self._build_headers(api_token=api_token, extra_headers=extra_headers)
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with http_sess.post(url, json=init_payload, headers=headers, timeout=timeout) as resp:
            if resp.status >= 400:
                body = (await resp.text())[:300]
                raise RuntimeError(f"MCP initialize HTTP {resp.status}: {body}")
            session_id = (resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id") or "").strip()
            msg = await self._parse_jsonrpc_response(resp, expected_id=req_id)

        if "error" in msg:
            err = msg["error"]
            raise RuntimeError(f"MCP initialize error: {err.get('message') if isinstance(err, dict) else err}")

        if session_id:
            self._mcp_sessions[(url, api_token)] = session_id

        # Отправляем обязательное уведомление notifications/initialized
        notif_headers = self._build_headers(api_token=api_token, session_id=session_id, extra_headers=extra_headers)
        try:
            async with http_sess.post(
                url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                headers=notif_headers,
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as _:
                pass
        except Exception:
            pass

        server_info = (msg.get("result") or {}).get("serverInfo") if isinstance(msg.get("result"), dict) else {}
        return session_id, server_info or {}

    async def _rpc_call(
        self,
        url: str,
        method: str,
        params: Dict[str, Any],
        *,
        api_token: str = "",
        timeout_s: float = 30.0,
        extra_headers: Optional[Dict[str, str]] = None,
        force_init: bool = False,
    ) -> Dict[str, Any]:
        key = (url, api_token)
        session_id = "" if force_init else self._mcp_sessions.get(key, "")
        if not session_id:
            session_id, _ = await self._initialize(url, api_token=api_token, timeout_s=min(15.0, timeout_s), extra_headers=extra_headers)

        http_sess = await self._get_http_session()
        req_id = self._next_id()
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        headers = self._build_headers(api_token=api_token, session_id=session_id, extra_headers=extra_headers)
        timeout = aiohttp.ClientTimeout(total=timeout_s)

        async with http_sess.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status in (400, 404) and not force_init:
                # Сессия могла протухнуть после рестарта MCP-сервера — переинициализируем 1 раз
                self._mcp_sessions.pop(key, None)
                return await self._rpc_call(
                    url,
                    method,
                    params,
                    api_token=api_token,
                    timeout_s=timeout_s,
                    extra_headers=extra_headers,
                    force_init=True,
                )
            if resp.status >= 400:
                body = (await resp.text())[:300]
                raise RuntimeError(f"MCP {method} HTTP {resp.status}: {body}")
            new_sid = (resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id") or "").strip()
            if new_sid:
                self._mcp_sessions[key] = new_sid
            msg = await self._parse_jsonrpc_response(resp, expected_id=req_id)

        if "error" in msg:
            err = msg["error"]
            raise RuntimeError(f"MCP {method} error: {err.get('message') if isinstance(err, dict) else err}")
        return msg.get("result") if isinstance(msg.get("result"), dict) else {"raw": msg.get("result")}

    async def list_tools(
        self,
        url: str,
        *,
        api_token: str = "",
        timeout_s: float = 15.0,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Подключается к MCP-серверу и получает список всех доступных инструментов."""
        clean_url = (url or "").strip()
        if not clean_url:
            return {"success": False, "error": "Не указан URL MCP-сервера"}

        t0 = time.monotonic()
        try:
            _, server_info = await self._initialize(clean_url, api_token=api_token, timeout_s=timeout_s, extra_headers=extra_headers)
            tools: List[Dict[str, Any]] = []
            cursor: Optional[str] = None
            for _ in range(10):
                params = {"cursor": cursor} if cursor else {}
                res = await self._rpc_call(
                    clean_url,
                    "tools/list",
                    params,
                    api_token=api_token,
                    timeout_s=timeout_s,
                    extra_headers=extra_headers,
                )
                batch = res.get("tools") or []
                if isinstance(batch, list):
                    for item in batch:
                        if not isinstance(item, dict) or not item.get("name"):
                            continue
                        schema = item.get("inputSchema") or item.get("parameters") or {"type": "object", "properties": {}}
                        tools.append({
                            "name": str(item["name"]).strip(),
                            "description": str(item.get("description") or "").strip(),
                            "parameters": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
                        })
                cursor = res.get("nextCursor")
                if not cursor:
                    break
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {
                "success": True,
                "tools": tools,
                "count": len(tools),
                "latency_ms": latency_ms,
                "server_info": server_info,
            }
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(f"MCP list_tools failed for {clean_url}: {e}")
            return {"success": False, "error": str(e), "latency_ms": latency_ms}

    async def call_tool(
        self,
        url: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        api_token: str = "",
        timeout_s: float = 45.0,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Вызывает инструмент на внешнем MCP HTTP сервере."""
        clean_url = (url or "").strip()
        if not clean_url:
            return {"status": "error", "error": "Не указан URL MCP-сервера"}

        try:
            res = await self._rpc_call(
                clean_url,
                "tools/call",
                {"name": tool_name, "arguments": arguments or {}},
                api_token=api_token,
                timeout_s=timeout_s,
                extra_headers=extra_headers,
            )
        except Exception as e:
            return {"status": "error", "error": str(e)}

        is_error = bool(res.get("isError", False))
        structured = res.get("structuredContent")
        content_blocks = res.get("content") or []
        texts: List[str] = []
        if isinstance(content_blocks, list):
            for blk in content_blocks:
                if isinstance(blk, dict):
                    if blk.get("type") == "text" and blk.get("text") is not None:
                        texts.append(str(blk["text"]))
                    elif "text" in blk and blk["text"] is not None:
                        texts.append(str(blk["text"]))
                elif isinstance(blk, str):
                    texts.append(blk)
        joined = "\n".join(texts).strip()

        if is_error:
            return {"status": "error", "error": joined or json.dumps(structured or res, ensure_ascii=False)[:400]}

        if structured is not None:
            return {"status": "ok", "data": structured}

        if joined:
            if (joined.startswith("{") and joined.endswith("}")) or (joined.startswith("[") and joined.endswith("]")):
                try:
                    return {"status": "ok", "data": json.loads(joined)}
                except Exception:
                    pass
            return {"status": "ok", "text": joined}

        return {"status": "ok", "data": res}


mcp_http_client_singleton = McpHttpClient()
