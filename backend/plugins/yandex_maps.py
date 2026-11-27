from __future__ import annotations

import html
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiohttp

try:
    from ..plugin_registry import PorfiriyPlugin
    from ..db import PorfiriyDB
    from ..serper_search import serper_search_singleton
except ImportError:
    from plugin_registry import PorfiriyPlugin
    from db import PorfiriyDB
    from serper_search import serper_search_singleton

logger = logging.getLogger("plugin.yandex_maps")

GEOCODER_URL = "https://geocode-maps.yandex.ru/1.x/"
GEOSUGGEST_URL = "https://suggest-maps.yandex.ru/v1/suggest"
DEFAULT_CITY = "Москва"
DEFAULT_LL = "37.6173,55.7558"


def _extract_oid(uri: str) -> str:
    m = re.search(r"oid=(\d+)", str(uri or ""))
    return m.group(1) if m else ""


def _build_org_url(oid: str) -> str:
    return f"https://yandex.ru/maps/org/{oid}/"


def _build_point_url(lon: str, lat: str) -> str:
    return f"https://yandex.ru/maps/?pt={quote(str(lon))},{quote(str(lat))}&z=17&l=map"


def _query_contains_city(query: str, city: str) -> bool:
    q = (query or "").lower()
    c = (city or "").strip().lower()
    if not c:
        return True
    # Проверяем по корню (первые 5 символов, чтобы ловить "Москве", "Казани", "Питере")
    stem = c[:5] if len(c) >= 5 else c
    if stem in q:
        return True
    if c == "санкт-петербург" and ("питер" in q or "спб" in q or "петербург" in q):
        return True
    if c == "москва" and "мск" in q:
        return True
    return False


def _build_place_card_html(query: str, places: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Формирует Telegram HTML-карточку и plain-text сводку для кэша rich_results."""
    top = places[0]
    title = str(top.get("title") or query).strip()
    subtitle = str(top.get("subtitle") or "").strip()
    address = str(top.get("address") or "").strip()
    url = str(top.get("url") or "").strip()

    lines = [f"📍 <b>{html.escape(title, quote=False)}</b>"]
    plain_lines = [title]

    if subtitle:
        lines.append(html.escape(subtitle, quote=False))
        plain_lines.append(subtitle)
    if address and address.lower() not in subtitle.lower():
        lines.append(f"<i>{html.escape(address, quote=False)}</i>")
        plain_lines.append(address)
    if url:
        lines.append(f'🗺 <a href="{html.escape(url, quote=True)}">Открыть на Яндекс.Картах</a>')
        plain_lines.append(url)

    alts = [p for p in places[1:3] if p.get("url") and p.get("url") != url]
    if alts:
        lines.append("")
        lines.append("<b>Другие варианты:</b>")
        plain_lines.append("Другие варианты:")
        for p in alts:
            at = str(p.get("title") or "").strip()
            asub = str(p.get("subtitle") or p.get("address") or "").strip()
            aurl = str(p.get("url") or "").strip()
            if asub:
                lines.append(
                    f'• <a href="{html.escape(aurl, quote=True)}">{html.escape(at, quote=False)}</a>'
                    f" — {html.escape(asub, quote=False)}"
                )
                plain_lines.append(f"- {at} — {asub}: {aurl}")
            else:
                lines.append(f'• <a href="{html.escape(aurl, quote=True)}">{html.escape(at, quote=False)}</a>')
                plain_lines.append(f"- {at}: {aurl}")

    return "\n".join(lines), "\n".join(plain_lines)


class YandexMapsPlugin(PorfiriyPlugin):
    """Встроенный плагин Яндекс.Карт: проверенные ссылки на организации и адреса + авто-отправка в Telegram."""

    provider_id = "builtin.yandex_maps"

    def get_tool_specs(self, db: PorfiriyDB, provider: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "name": "yandex_maps_find_place",
                "description": (
                    "Находит организацию, заведение (ресторан, кафе, магазин, музей, клинику, парк) "
                    "или точный адрес на Яндекс.Картах через официальные API (Геосаджест + Геокодер), "
                    "возвращает проверенную рабочую ссылку (https://yandex.ru/maps/org/<oid>/ или с координатами) "
                    "и автоматически отправляет карточку со ссылкой в Telegram."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Название места, заведения или адрес (например: 'Кофемания на Покровке', 'Покровка 18', 'Третьяковская галерея').",
                        },
                        "city": {
                            "type": "string",
                            "description": "Город поиска, если отличается от города по умолчанию (например: 'Санкт-Петербург', 'Казань').",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["auto", "org", "address"],
                            "description": "Тип объекта: org — организация/заведение, address — точный адрес/улица/дом, auto — сначала организация, затем адрес.",
                        },
                    },
                    "required": ["query"],
                },
            }
        ]

    def get_prompt_snippet(self, db: PorfiriyDB, provider: Dict[str, Any], options: Dict[str, Any]) -> str:
        cfg = provider.get("config") or {}
        def_city = str(cfg.get("default_city") or DEFAULT_CITY).strip() or DEFAULT_CITY
        auto_tg = bool(cfg.get("auto_send_telegram", True))
        tg_note = (
            "Инструмент АВТОМАТИЧЕСКИ отправляет карточку с проверенной кликабельной ссылкой в Telegram "
            "(в текущий чат, если диалог идёт в Telegram, или в общий семейный чат при голосовом запросе)."
            if auto_tg
            else "Результат сохраняется в кэше и при просьбе может быть отправлен в Telegram."
        )
        return (
            "### YANDEX MAPS (VERIFIED PLACE LINKS)\n"
            f"- Город по умолчанию: {def_city}.\n"
            "- Когда пользователь спрашивает о месте, заведении, ресторане, магазине, достопримечательности, адресе "
            "или просит точку/ссылку на Яндекс.Картах — ВСЕГДА вызывай `yandex_maps_find_place`.\n"
            "- СТРОГО ЗАПРЕЩЕНО выдумывать URL вида `yandex.ru/maps/org/...` или координаты по памяти — используй только проверенную ссылку из ответа инструмента.\n"
            f"- {tg_note}\n"
            "- В голосовом ответе назови место, точный адрес и кратко сообщи, что проверенная ссылка на Яндекс.Карты уже отправлена в Telegram."
        )

    @classmethod
    async def _search_geosuggest(
        cls,
        api_key: str,
        query: str,
        city: str,
        default_city: str,
        default_ll: str,
        types_filter: str = "biz",
        results: int = 5,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        if not api_key:
            return [], None

        use_default_bias = bool(default_ll) and (not city or city.strip().lower() == default_city.strip().lower())
        text_q = query.strip()
        if city and not _query_contains_city(text_q, city):
            text_q = f"{text_q}, {city}"

        params: Dict[str, Any] = {
            "apikey": api_key,
            "text": text_q,
            "types": types_filter,
            "attrs": "uri",
            "lang": "ru_RU",
            "results": str(results),
        }
        if use_default_bias:
            params["ll"] = default_ll
            params["spn"] = "0.8,0.8"

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(GEOSUGGEST_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"Geosuggest HTTP {resp.status}: {body[:200]}")
                        return [], f"HTTP {resp.status}"
                    data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"Geosuggest request failed: {e}")
            return [], str(e)

        items: List[Dict[str, Any]] = []
        for r in (data.get("results") or []):
            if not isinstance(r, dict):
                continue
            title = str((r.get("title") or {}).get("text") or "").strip()
            subtitle = str((r.get("subtitle") or {}).get("text") or "").strip()
            uri = str(r.get("uri") or "").strip()
            addr = str((r.get("address") or {}).get("formatted_address") or "").strip()
            oid = _extract_oid(uri)
            if not oid:
                continue
            items.append({
                "source": "geosuggest",
                "title": title or query,
                "subtitle": subtitle,
                "address": addr,
                "oid": oid,
                "uri": uri,
                "url": _build_org_url(oid),
            })
        return items, None

    @classmethod
    async def _search_geocoder(
        cls,
        api_key: str,
        query: str,
        city: str,
        results: int = 3,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        if not api_key:
            return [], None

        geo_q = query.strip()
        if city and not _query_contains_city(geo_q, city):
            geo_q = f"{city}, {geo_q}"

        params = {
            "apikey": api_key,
            "geocode": geo_q,
            "format": "json",
            "results": str(results),
            "lang": "ru_RU",
        }
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(GEOCODER_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"Geocoder HTTP {resp.status}: {body[:200]}")
                        return [], f"HTTP {resp.status}"
                    data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"Geocoder request failed: {e}")
            return [], str(e)

        members = (
            data.get("response", {})
            .get("GeoObjectCollection", {})
            .get("featureMember")
            or []
        )
        items: List[Dict[str, Any]] = []
        for m in members:
            go = (m or {}).get("GeoObject") or {}
            if not isinstance(go, dict):
                continue
            pos = str((go.get("Point") or {}).get("pos") or "").strip()
            parts = pos.split()
            if len(parts) != 2:
                continue
            lon, lat = parts[0], parts[1]
            name = str(go.get("name") or "").strip()
            desc = str(go.get("description") or "").strip()
            meta = (go.get("metaDataProperty") or {}).get("GeocoderMetaData") or {}
            full_addr = str(meta.get("text") or "").strip()
            items.append({
                "source": "geocoder",
                "title": name or full_addr or query,
                "subtitle": desc,
                "address": full_addr,
                "lon": lon,
                "lat": lat,
                "url": _build_point_url(lon, lat),
            })
        return items, None

    @classmethod
    async def _search_serper_fallback(
        cls,
        serper_key: str,
        query: str,
        city: str,
    ) -> List[Dict[str, Any]]:
        if not serper_key:
            return []
        q = f"site:yandex.ru/maps/org {query} {city}".strip()
        try:
            res = await serper_search_singleton.search(q, serper_key, num=5)
        except Exception as e:
            logger.warning(f"Serper fallback for Yandex Maps failed: {e}")
            return []
        if not res.get("success"):
            return []
        items: List[Dict[str, Any]] = []
        for it in (res.get("items") or []):
            link = str(it.get("link") or "").strip()
            if "yandex.ru/maps" not in link:
                continue
            title = str(it.get("title") or query).strip()
            title = re.sub(r"\s*[—–-]\s*Яндекс\.?\s*Карты.*$", "", title, flags=re.IGNORECASE).strip() or query
            snippet = str(it.get("snippet") or "").strip()
            items.append({
                "source": "serper",
                "title": title,
                "subtitle": snippet[:140],
                "address": "",
                "url": link,
            })
        return items

    @classmethod
    async def test_connection(cls, db: PorfiriyDB) -> Dict[str, Any]:
        prov = db.get_provider(cls.provider_id, mask_secrets=False)
        if not prov:
            return {"success": False, "error": "Провайдер builtin.yandex_maps не найден в БД"}
        cfg = prov.get("config") or {}
        geocoder_key = str(cfg.get("geocoder_api_key") or "").strip()
        geosuggest_key = str(cfg.get("geosuggest_api_key") or "").strip()
        default_city = str(cfg.get("default_city") or DEFAULT_CITY).strip() or DEFAULT_CITY
        default_ll = str(cfg.get("default_ll") or DEFAULT_LL).strip() or DEFAULT_LL

        if not geocoder_key and not geosuggest_key:
            return {"success": False, "error": "Укажите хотя бы один API-ключ (Геокодер или Геосаджест) и сохраните настройки."}

        report: Dict[str, Any] = {}
        ok_any = False
        errors: List[str] = []

        if geosuggest_key:
            gs_items, gs_err = await cls._search_geosuggest(
                geosuggest_key, "Кофемания", default_city, default_city, default_ll, types_filter="biz", results=2
            )
            if gs_items:
                ok_any = True
                report["geosuggest"] = {
                    "ok": True,
                    "sample": f"{gs_items[0]['title']} ({gs_items[0]['url']})",
                }
            else:
                err_msg = gs_err or "Пустой ответ"
                report["geosuggest"] = {"ok": False, "error": err_msg}
                errors.append(f"Геосаджест: {err_msg}")

        if geocoder_key:
            gc_items, gc_err = await cls._search_geocoder(geocoder_key, "Покровка, 18", default_city, results=1)
            if gc_items:
                ok_any = True
                report["geocoder"] = {
                    "ok": True,
                    "sample": f"{gc_items[0]['title']} ({gc_items[0]['url']})",
                }
            else:
                err_msg = gc_err or "Пустой ответ"
                report["geocoder"] = {"ok": False, "error": err_msg}
                errors.append(f"Геокодер: {err_msg}")

        all_ok = ok_any and not errors
        msg_parts = []
        if "geosuggest" in report:
            msg_parts.append(
                f"Геосаджест: {'OK' if report['geosuggest']['ok'] else report['geosuggest']['error']}"
            )
        if "geocoder" in report:
            msg_parts.append(
                f"Геокодер: {'OK' if report['geocoder']['ok'] else report['geocoder']['error']}"
            )
        return {
            "success": all_ok,
            "partial": ok_any and bool(errors),
            "message": " | ".join(msg_parts),
            "error": "; ".join(errors) if errors else "",
            "details": report,
        }

    async def execute_tool(self, tool_name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name != "yandex_maps_find_place":
            return {"status": "error", "error": f"Unknown tool: {tool_name}"}

        db: PorfiriyDB = ctx["db"]
        prov = ctx.get("provider") or db.get_provider(self.provider_id, mask_secrets=False) or {}
        cfg = prov.get("config") or {}
        options = ctx.get("options") or {}

        query = str(args.get("query") or "").strip()
        if not query:
            return {"status": "error", "error": "Не указан поисковый запрос (query)."}

        default_city = str(cfg.get("default_city") or DEFAULT_CITY).strip() or DEFAULT_CITY
        default_ll = str(cfg.get("default_ll") or DEFAULT_LL).strip() or DEFAULT_LL
        city = str(args.get("city") or "").strip() or default_city
        kind = str(args.get("kind") or "auto").strip().lower()
        if kind not in ("auto", "org", "address"):
            kind = "auto"

        geocoder_key = str(cfg.get("geocoder_api_key") or "").strip()
        geosuggest_key = str(cfg.get("geosuggest_api_key") or "").strip()
        auto_send_tg = bool(cfg.get("auto_send_telegram", True))

        places: List[Dict[str, Any]] = []
        api_errors: List[str] = []

        # 1. Организации через Геосаджест (types=biz -> проверенный oid)
        if kind in ("auto", "org") and geosuggest_key:
            gs_items, gs_err = await self._search_geosuggest(
                geosuggest_key, query, city, default_city, default_ll, types_filter="biz", results=5
            )
            if gs_items:
                places.extend(gs_items)
            elif gs_err:
                api_errors.append(f"Geosuggest: {gs_err}")

        # 2. Точные адреса / фолбэк через Геокодер (lon, lat -> проверенный pt=)
        if (kind == "address" or not places) and geocoder_key:
            gc_items, gc_err = await self._search_geocoder(geocoder_key, query, city, results=3)
            if gc_items:
                if kind == "address":
                    places = gc_items + places
                else:
                    places.extend(gc_items)
            elif gc_err:
                api_errors.append(f"Geocoder: {gc_err}")

        # 3. Запасной фолбэк через Serper (site:yandex.ru/maps/org)
        if not places:
            serper_key = str(options.get("serper_api_key") or "").strip()
            if serper_key:
                sp_items = await self._search_serper_fallback(serper_key, query, city)
                if sp_items:
                    places.extend(sp_items)

        if not places:
            err_hint = f" ({'; '.join(api_errors)})" if api_errors else ""
            return {
                "status": "not_found",
                "query": query,
                "city": city,
                "message": f"Не удалось найти проверенную точку на Яндекс.Картах по запросу «{query}»{err_hint}.",
            }

        card_html, plain_text = _build_place_card_html(query, places)
        dev = ctx.get("dev")
        mac = str(getattr(dev, "mac", "") or ctx.get("device_mac") or "")

        # Сохраняем в единый кэш rich_results (TTL 72ч, поиск по теме, без новой таблицы)
        try:
            db.save_rich_result(
                kind="place",
                topic=query,
                query=query,
                html_text=card_html,
                plain_text=plain_text,
                device_mac=mac,
            )
        except Exception as e_cache:
            logger.warning(f"Failed to save place to rich_results: {e_cache}")

        # Авто-отправка карточки в Telegram каждый раз (если включено и бот активен)
        tg_sent = False
        tg_target = mac[3:] if mac.startswith("tg_") and len(mac) > 3 else "family"
        tg_dest_label = "этот Telegram-чат" if mac.startswith("tg_") else "общий семейный Telegram-чат"
        if auto_send_tg:
            try:
                try:
                    from .telegram_bot import TelegramBotPlugin
                except ImportError:
                    from plugins.telegram_bot import TelegramBotPlugin
                tg_prov = db.get_provider("builtin.telegram_bot", mask_secrets=False)
                if tg_prov and tg_prov.get("enabled"):
                    tg_res = await TelegramBotPlugin.send_message(
                        db=db,
                        provider=tg_prov,
                        ha_api=ctx.get("ha_api"),
                        target=tg_target,
                        text=card_html,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    tg_sent = bool(tg_res.get("success"))
            except Exception as e_tg:
                logger.warning(f"Failed to auto-send Yandex Maps card to Telegram: {e_tg}")

        top = places[0]
        return {
            "status": "ok",
            "title": top.get("title"),
            "subtitle": top.get("subtitle"),
            "address": top.get("address") or top.get("subtitle"),
            "url": top.get("url"),
            "source": top.get("source"),
            "alternatives": [
                {"title": p.get("title"), "subtitle": p.get("subtitle"), "url": p.get("url")}
                for p in places[1:3]
            ],
            "sent_to_telegram": tg_sent,
            "instruction": (
                f"Точка найдена и проверена. Карточка со ссылкой уже отправлена в {tg_dest_label}. "
                f"В голосовом ответе кратко назови место ({top.get('title')}), адрес и скажи, что ссылка на Яндекс.Карты отправлена в Telegram."
                if tg_sent
                else f"Точка найдена: {top.get('title')} ({top.get('address') or top.get('subtitle')}), ссылка: {top.get('url')}."
            ),
        }
