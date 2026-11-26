from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

try:
    from ..plugin_registry import PorfiriyPlugin
    from ..db import PorfiriyDB
except ImportError:
    from plugin_registry import PorfiriyPlugin
    from db import PorfiriyDB

logger = logging.getLogger("plugin.kinowatch")

DEFAULT_API_BASE = "https://api.service-kp.com"


def _quality_sort_key(q: str) -> Tuple[int, str]:
    m = re.search(r"(\d{3,4})", str(q or ""))
    return (-(int(m.group(1)) if m else 0), str(q or ""))


def _extract_media_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    """Извлекает качество, количество и студии озвучки, субтитры и статус сезонов/серий."""
    videos: List[Dict[str, Any]] = []
    seasons_raw = item.get("seasons") or []
    seasons_count = len(seasons_raw) if isinstance(seasons_raw, list) else 0
    last_season_num: Optional[int] = None
    last_season_episodes: int = 0
    last_episode_num: Optional[int] = None

    if isinstance(item.get("videos"), list) and item["videos"]:
        videos = item["videos"]
    elif isinstance(seasons_raw, list) and seasons_raw:
        # Берём последний доступный сезон и его последний эпизод (актуальные дорожки и качество)
        last_s = seasons_raw[-1]
        if isinstance(last_s, dict):
            last_season_num = last_s.get("number") or seasons_count
            eps = last_s.get("episodes") or []
            if isinstance(eps, list) and eps:
                last_season_episodes = len(eps)
                last_ep = eps[-1]
                if isinstance(last_ep, dict):
                    last_episode_num = last_ep.get("number") or len(eps)
                    videos = [last_ep]

    qualities_set = set()
    voiceovers: List[str] = []
    voice_seen = set()
    subtitles_set = set()

    item_quality = str(item.get("quality") or "").strip()
    if item_quality:
        qualities_set.add(item_quality)

    for v in videos:
        if not isinstance(v, dict):
            continue
        for f in v.get("files") or []:
            if isinstance(f, dict):
                q = str(f.get("quality") or "").strip()
                if not q and f.get("h"):
                    q = f"{f['h']}p"
                if q:
                    qualities_set.add(q)

        for a in v.get("audios") or []:
            if not isinstance(a, dict):
                continue
            lang = str(a.get("lang") or "").strip()
            a_type = a.get("type")
            type_title = str(a_type.get("title") if isinstance(a_type, dict) else (a_type or "")).strip()
            author = a.get("author")
            author_title = str(author.get("title") if isinstance(author, dict) else (author or "")).strip()

            if author_title and type_title:
                label = f"{type_title} — {author_title}"
            elif author_title:
                label = author_title
            elif type_title:
                label = type_title
            else:
                label = lang or "Дорожка"
            if lang and lang.lower() not in label.lower() and lang.lower() != "rus":
                label = f"{label} ({lang})"
            key = label.lower()
            if key not in voice_seen:
                voice_seen.add(key)
                voiceovers.append(label)

        for s in v.get("subtitles") or []:
            if isinstance(s, dict):
                sl = str(s.get("lang") or "").strip()
                if sl:
                    subtitles_set.add(sl)

    qualities = sorted(qualities_set, key=_quality_sort_key)
    q_joined = " ".join(qualities + [item_quality]).lower()
    is_4k = any(tok in q_joined for tok in ("2160", "4k", "uhd"))
    is_hdr = any(tok in q_joined for tok in ("hdr", "dolby vision", "dv"))

    return {
        "qualities": qualities,
        "item_quality": item_quality,
        "is_4k": is_4k,
        "is_hdr": is_hdr,
        "voiceover_count": len(voiceovers),
        "voiceovers": voiceovers,
        "subtitles": sorted(subtitles_set),
        "seasons_count": seasons_count,
        "last_season_num": last_season_num,
        "last_season_episodes": last_season_episodes,
        "last_episode_num": last_episode_num,
        "finished": item.get("finished"),
    }


class KinoWatchPlugin(PorfiriyPlugin):
    """Домашний плагин KinoWatch / KinoPub: поиск, качество, озвучки, закладки и «Буду смотреть»."""

    provider_id = "builtin.kinowatch"

    @classmethod
    def _save_cfg(cls, db: PorfiriyDB, prov: Dict[str, Any], cfg: Dict[str, Any]) -> None:
        db.upsert_provider(
            provider_id=cls.provider_id,
            kind="builtin",
            title=prov["title"],
            description=prov["description"],
            enabled=prov["enabled"],
            config=cfg,
        )

    @classmethod
    async def _oauth_post(cls, api_base: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{api_base.rstrip('/')}{path}"
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, data=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    return {"error": "invalid_response", "error_description": f"HTTP {resp.status}"}
                if resp.status >= 400 and "error" not in data:
                    data["error"] = f"http_{resp.status}"
                return data

    @classmethod
    async def start_device_auth(cls, db: PorfiriyDB) -> Dict[str, Any]:
        prov = db.get_provider(cls.provider_id, mask_secrets=False)
        if not prov:
            return {"success": False, "error": "Провайдер builtin.kinowatch не найден в БД"}
        cfg = dict(prov.get("config") or {})
        api_base = str(cfg.get("api_base") or DEFAULT_API_BASE).strip() or DEFAULT_API_BASE
        client_id = str(cfg.get("client_id") or "").strip()
        client_secret = str(cfg.get("client_secret") or "").strip()

        if not client_id or not client_secret:
            return {
                "success": False,
                "error": "Укажите Client ID и Client Secret в «⚙️ Настройки API» ниже и нажмите Сохранить.",
            }

        try:
            data = await cls._oauth_post(
                api_base,
                "/oauth2/device",
                {
                    "grant_type": "device_code",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            if not data.get("code") or not data.get("user_code"):
                err = data.get("error_description") or data.get("message") or data.get("error") or str(data)
                return {"success": False, "error": f"Не удалось получить код устройства: {err}"}

            cfg["pending_device_code"] = data["code"]
            cfg["pending_user_code"] = data["user_code"]
            cfg["verification_uri"] = data.get("verification_uri") or "https://kino.watch/device"
            cls._save_cfg(db, prov, cfg)
            return {
                "success": True,
                "user_code": data["user_code"],
                "verification_uri": cfg["verification_uri"],
                "expires_in": data.get("expires_in", 600),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @classmethod
    async def poll_device_auth(cls, db: PorfiriyDB) -> Dict[str, Any]:
        prov = db.get_provider(cls.provider_id, mask_secrets=False)
        if not prov:
            return {"success": False, "error": "Провайдер builtin.kinowatch не найден в БД"}
        cfg = dict(prov.get("config") or {})
        api_base = str(cfg.get("api_base") or DEFAULT_API_BASE).strip() or DEFAULT_API_BASE
        client_id = str(cfg.get("client_id") or "").strip()
        client_secret = str(cfg.get("client_secret") or "").strip()
        device_code = str(cfg.get("pending_device_code") or "").strip()

        if not device_code:
            return {"success": False, "error": "Сначала нажмите «Получить код входа»."}

        try:
            data = await cls._oauth_post(
                api_base,
                "/oauth2/device",
                {
                    "grant_type": "device_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": device_code,
                },
            )
            if not data.get("access_token"):
                err = str(data.get("error") or "")
                if err in ("authorization_pending", "slow_down"):
                    return {
                        "success": False,
                        "pending": True,
                        "error": f"Код {cfg.get('pending_user_code', '')} ещё не подтверждён на сайте. Введите его на {cfg.get('verification_uri') or 'kino.watch/device'} и нажмите снова.",
                    }
                desc = data.get("error_description") or data.get("message") or err or str(data)
                return {"success": False, "error": f"Ошибка авторизации: {desc}"}

            cfg["access_token"] = data["access_token"]
            if data.get("refresh_token"):
                cfg["refresh_token"] = data["refresh_token"]
            expires_in = int(data.get("expires_in") or 3600)
            cfg["token_expires_at"] = int(time.time()) + expires_in - 60
            cfg["auth_status"] = "authorized"
            cfg["pending_device_code"] = ""
            cfg["pending_user_code"] = ""
            cls._save_cfg(db, prov, cfg)

            sync_res = await cls.sync_folders(db)
            return {
                "success": True,
                "message": "Аккаунт KinoWatch привязан к дому!",
                "folders": sync_res.get("folders", []),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @classmethod
    async def refresh_access_token(cls, db: PorfiriyDB) -> str:
        prov = db.get_provider(cls.provider_id, mask_secrets=False)
        if not prov:
            raise RuntimeError("Провайдер builtin.kinowatch не найден")
        cfg = dict(prov.get("config") or {})
        api_base = str(cfg.get("api_base") or DEFAULT_API_BASE).strip() or DEFAULT_API_BASE
        client_id = str(cfg.get("client_id") or "").strip()
        client_secret = str(cfg.get("client_secret") or "").strip()
        refresh_tok = str(cfg.get("refresh_token") or "").strip()
        if not refresh_tok or not client_id or not client_secret:
            raise RuntimeError("Аккаунт KinoWatch не авторизован (нет refresh_token)")

        data = await cls._oauth_post(
            api_base,
            "/oauth2/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_tok,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        if not data.get("access_token"):
            cfg["auth_status"] = "unauthorized"
            cls._save_cfg(db, prov, cfg)
            raise RuntimeError(f"Не удалось обновить токен KinoWatch: {data.get('error_description') or data}")

        cfg["access_token"] = data["access_token"]
        if data.get("refresh_token"):
            cfg["refresh_token"] = data["refresh_token"]
        cfg["token_expires_at"] = int(time.time()) + int(data.get("expires_in") or 3600) - 60
        cfg["auth_status"] = "authorized"
        cls._save_cfg(db, prov, cfg)
        return cfg["access_token"]

    @classmethod
    async def api_request(
        cls,
        db: PorfiriyDB,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        prov = db.get_provider(cls.provider_id, mask_secrets=False)
        if not prov:
            raise RuntimeError("Провайдер builtin.kinowatch не найден")
        cfg = dict(prov.get("config") or {})
        api_base = str(cfg.get("api_base") or DEFAULT_API_BASE).strip() or DEFAULT_API_BASE
        token = str(cfg.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("Аккаунт KinoWatch не привязан. Привяжите его во вкладке Плагины.")

        if int(cfg.get("token_expires_at") or 0) and time.time() >= int(cfg["token_expires_at"]):
            token = await cls.refresh_access_token(db)

        url = f"{api_base.rstrip('/')}{path}"
        for attempt in range(2):
            q_params = dict(params or {})
            q_params["access_token"] = token
            headers = {"Authorization": f"Bearer {token}"}
            async with aiohttp.ClientSession() as sess:
                async with sess.request(
                    method.upper(),
                    url,
                    params=q_params,
                    data=data,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        token = await cls.refresh_access_token(db)
                        continue
                    body = await resp.json(content_type=None)
                    if resp.status >= 400:
                        err_msg = body.get("message") or body.get("error") or str(body) if isinstance(body, dict) else f"HTTP {resp.status}"
                        raise RuntimeError(f"KinoWatch API error ({resp.status}): {err_msg}")
                    if not isinstance(body, dict):
                        raise RuntimeError(f"Неожиданный ответ KinoWatch API: {body}")
                    return body
        raise RuntimeError("KinoWatch API request failed after retry")

    @classmethod
    async def sync_folders(cls, db: PorfiriyDB) -> Dict[str, Any]:
        prov = db.get_provider(cls.provider_id, mask_secrets=False)
        if not prov:
            return {"success": False, "error": "Провайдер builtin.kinowatch не найден"}
        cfg = dict(prov.get("config") or {})
        try:
            body = await cls.api_request(db, "GET", "/v1/bookmarks")
            raw_items = body.get("items") or []
            folders = [
                {
                    "id": str(f.get("id")),
                    "title": str(f.get("title") or f"Папка {f.get('id')}").strip(),
                    "count": int(f.get("count") or 0),
                }
                for f in raw_items
                if isinstance(f, dict) and f.get("id") is not None
            ]
            # Re-read prov/cfg in case api_request refreshed tokens
            prov_now = db.get_provider(cls.provider_id, mask_secrets=False) or prov
            cfg_now = dict(prov_now.get("config") or cfg)
            cfg_now["cached_folders"] = folders
            if not str(cfg_now.get("default_folder_id") or "").strip() and folders:
                fav = next((f for f in folders if any(w in f["title"].lower() for w in ("избран", "смотреть", "fav", "посм"))), folders[0])
                cfg_now["default_folder_id"] = fav["id"]
            cls._save_cfg(db, prov_now, cfg_now)
            return {"success": True, "folders": folders, "default_folder_id": cfg_now.get("default_folder_id", "")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_tool_specs(self, db: PorfiriyDB, provider: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "name": "kinowatch_search",
                "description": (
                    "Поиск фильмов, сериалов, мультфильмов и шоу на KinoWatch / KinoPub (КиноПаб / КиноВотч). "
                    "Возвращает список найденного с id, названием, годом, типом (movie/serial), рейтингом КП/IMDb и базовым качеством."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Название фильма или сериала"},
                        "type": {
                            "type": "string",
                            "enum": ["movie", "serial", "documovie", "docuserial", "concert", "tvshow"],
                            "description": "Опциональный фильтр типа",
                        },
                        "year": {"type": "integer", "description": "Опциональный фильтр по году выхода"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "kinowatch_get_details",
                "description": (
                    "Получить подробную карточку фильма или сериала на KinoWatch/KinoPub по item_id: "
                    "доступное качество видео (4K/HDR/1080p/экранка), количество и список студий озвучки, субтитры, "
                    "количество сезонов и последняя вышедшая серия, в каких закладках/«Буду смотреть» уже находится."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "ID фильма или сериала из kinowatch_search"},
                    },
                    "required": ["item_id"],
                },
            },
            {
                "name": "kinowatch_list_bookmarks",
                "description": (
                    "Показать папки закладок/избранного на KinoWatch/KinoPub или содержимое конкретной папки."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "folder": {"type": "string", "description": "Опционально: ID или название папки (например 'Избранное'). Если пусто — вернёт список папок."},
                    },
                },
            },
            {
                "name": "kinowatch_add_to_bookmarks",
                "description": (
                    "Добавить фильм или сериал в избранное / папку закладок на KinoWatch/KinoPub. "
                    "Если папка не указана — добавляет в папку по умолчанию из настроек дома."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "ID фильма или сериала из kinowatch_search"},
                        "folder": {"type": "string", "description": "ID или название папки закладок (если не указано — используется папка по умолчанию)"},
                    },
                    "required": ["item_id"],
                },
            },
            {
                "name": "kinowatch_toggle_watching",
                "description": (
                    "Добавить сериал/фильм в список «Буду смотреть» (подписка на новые серии) или убрать из него на KinoWatch/KinoPub."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "ID сериала или фильма"},
                    },
                    "required": ["item_id"],
                },
            },
        ]

    def get_prompt_snippet(self, db: PorfiriyDB, provider: Dict[str, Any], options: Dict[str, Any]) -> str:
        cfg = provider.get("config") or {}
        folders = cfg.get("cached_folders") or []
        def_fid = str(cfg.get("default_folder_id") or "").strip()
        def_title = next((f.get("title") for f in folders if str(f.get("id")) == def_fid), def_fid or "не выбрана")
        folder_names = ", ".join(f"`{f.get('title')}` (id={f.get('id')})" for f in folders[:12]) if folders else "не загружены"
        return (
            "### KINOWATCH / KINOPUB (`builtin.kinowatch`, Shared Home Account):\n"
            "- Synonyms: **КиноПаб, КиноВотч, KinoPub, KinoWatch, кино, фильм, сериал, вышел ли фильм, качество, озвучки, в избранное, в закладки, буду смотреть**.\n"
            "- Primary Rule: For ANY question about movies/series (whether released, what video quality/4K/HDR, how many/which voiceover studios, seasons/episodes, bookmarks/watchlist), ALWAYS use `kinowatch_*` tools instead of web_search.\n"
            f"- Default bookmark folder: `{def_title}` (id=`{def_fid or 'auto'}`). Known folders: {folder_names}.\n"
            "- Flow: To add a movie/series by title, call `kinowatch_search` first -> if one clear match, call `kinowatch_add_to_bookmarks` (or `kinowatch_get_details` if the user asked about quality/voiceovers/seasons)."
        )

    async def execute_tool(self, tool_name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        db: PorfiriyDB = ctx["db"]
        prov = ctx["provider"]
        cfg = prov.get("config") or {}

        if tool_name == "kinowatch_search":
            q = str(args.get("query") or "").strip()
            if not q:
                return {"status": "error", "error": "Укажите название фильма или сериала (query)"}
            params: Dict[str, Any] = {"q": q, "perpage": 10}
            if args.get("type"):
                params["type"] = str(args["type"]).strip()
            body = await self.api_request(db, "GET", "/v1/items/search", params=params)
            items_raw = body.get("items") or []
            req_year = args.get("year")
            results = []
            for it in items_raw:
                if not isinstance(it, dict):
                    continue
                if req_year and str(it.get("year")) != str(req_year):
                    continue
                results.append({
                    "id": it.get("id"),
                    "title": it.get("title"),
                    "type": it.get("type"),
                    "year": it.get("year"),
                    "quality": it.get("quality"),
                    "voice": it.get("voice"),
                    "kp_rating": it.get("rating"),
                    "imdb_rating": it.get("imdb_rating"),
                    "finished": it.get("finished"),
                })
            # Если фильтр по году отсёк всё, вернём без фильтра по году
            if not results and req_year and items_raw:
                for it in items_raw[:8]:
                    if isinstance(it, dict):
                        results.append({
                            "id": it.get("id"),
                            "title": it.get("title"),
                            "type": it.get("type"),
                            "year": it.get("year"),
                            "quality": it.get("quality"),
                            "voice": it.get("voice"),
                            "kp_rating": it.get("rating"),
                            "imdb_rating": it.get("imdb_rating"),
                            "finished": it.get("finished"),
                        })
            return {"status": "ok", "count": len(results), "items": results[:10]}

        if tool_name == "kinowatch_get_details":
            item_id = int(args.get("item_id") or 0)
            if not item_id:
                return {"status": "error", "error": "Укажите item_id"}
            body = await self.api_request(db, "GET", f"/v1/items/{item_id}")
            item = body.get("item") if isinstance(body.get("item"), dict) else body
            summary = _extract_media_summary(item)
            return {
                "status": "ok",
                "id": item.get("id") or item_id,
                "title": item.get("title"),
                "type": item.get("type"),
                "year": item.get("year"),
                "kp_rating": item.get("rating"),
                "imdb_rating": item.get("imdb_rating"),
                "in_watchlist": bool(item.get("in_watchlist")),
                "bookmarks": [
                    {"id": b.get("id"), "title": b.get("title")}
                    for b in (item.get("bookmarks") or [])
                    if isinstance(b, dict)
                ],
                "plot": str(item.get("plot") or "")[:400],
                **summary,
            }

        if tool_name == "kinowatch_list_bookmarks":
            folder_q = str(args.get("folder") or "").strip()
            sync_res = await self.sync_folders(db)
            if not sync_res.get("success"):
                return {"status": "error", "error": sync_res.get("error")}
            folders = sync_res.get("folders") or []
            if not folder_q:
                return {
                    "status": "ok",
                    "default_folder_id": sync_res.get("default_folder_id"),
                    "folders": folders,
                }
            target = next(
                (f for f in folders if f["id"] == folder_q or folder_q.lower() in f["title"].lower()),
                None,
            )
            if not target:
                return {"status": "error", "error": f"Папка '{folder_q}' не найдена. Доступны: {', '.join(f['title'] for f in folders)}"}
            body = await self.api_request(db, "GET", f"/v1/bookmarks/{target['id']}", params={"perpage": 25})
            items = [
                {
                    "id": it.get("id"),
                    "title": it.get("title"),
                    "year": it.get("year"),
                    "type": it.get("type"),
                    "quality": it.get("quality"),
                }
                for it in (body.get("items") or [])
                if isinstance(it, dict)
            ]
            return {"status": "ok", "folder": target, "count": len(items), "items": items}

        if tool_name == "kinowatch_add_to_bookmarks":
            item_id = int(args.get("item_id") or 0)
            if not item_id:
                return {"status": "error", "error": "Укажите item_id"}
            folder_arg = str(args.get("folder") or "").strip()
            folders = cfg.get("cached_folders") or []
            if not folders:
                sync_res = await self.sync_folders(db)
                folders = sync_res.get("folders") or []
                cfg = (db.get_provider(self.provider_id, mask_secrets=False) or {}).get("config") or cfg

            target = None
            if folder_arg:
                target = next(
                    (f for f in folders if str(f.get("id")) == folder_arg or folder_arg.lower() in str(f.get("title") or "").lower()),
                    None,
                )
            else:
                def_fid = str(cfg.get("default_folder_id") or "").strip()
                if def_fid:
                    target = next((f for f in folders if str(f.get("id")) == def_fid), {"id": def_fid, "title": def_fid})
                elif folders:
                    target = folders[0]

            if not target or not target.get("id"):
                return {"status": "error", "error": "Не выбрана папка закладок (укажите папку по умолчанию в настройках плагина KinoWatch)."}

            await self.api_request(
                db,
                "POST",
                "/v1/bookmarks/add",
                data={"item": str(item_id), "folder": str(target["id"])},
            )
            return {
                "status": "ok",
                "item_id": item_id,
                "folder_id": target["id"],
                "folder_title": target.get("title") or target["id"],
                "message": f"Добавлено в папку «{target.get('title') or target['id']}».",
            }

        if tool_name == "kinowatch_toggle_watching":
            item_id = int(args.get("item_id") or 0)
            if not item_id:
                return {"status": "error", "error": "Укажите item_id"}
            await self.api_request(db, "GET", "/v1/watching/togglewatchlist", params={"id": str(item_id)})
            return {"status": "ok", "item_id": item_id, "message": "Статус в списке «Буду смотреть» переключён."}

        return {"status": "error", "error": f"Unknown kinowatch tool: {tool_name}"}
