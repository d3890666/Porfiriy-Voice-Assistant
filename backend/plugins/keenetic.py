from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import aiohttp

try:
    from ..plugin_registry import PorfiriyPlugin
    from ..db import PorfiriyDB
    from ..tg_approvals import approval_manager
except ImportError:
    from plugin_registry import PorfiriyPlugin
    from db import PorfiriyDB
    from tg_approvals import approval_manager

logger = logging.getLogger("plugin.keenetic")

DEFAULT_GEOSITE_URL = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/{name}"
DEFAULT_GEOIP_URL = "http://ip-api.com/json/{query}?fields=status,message,country,isp,org,as,query"

# Наиболее распространённые составные публичные суффиксы второго уровня (eTLD+1)
TWO_PART_SUFFIXES: Set[str] = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk",
    "com.au", "net.au", "org.au", "edu.au",
    "com.br", "net.br", "org.br",
    "com.tr", "net.tr", "org.tr", "gen.tr",
    "com.ua", "org.ua", "net.ua", "kiev.ua", "in.ua",
    "spb.ru", "msk.ru", "org.ru", "net.ru", "pp.ru", "gov.ru", "edu.ru", "ac.ru", "com.ru",
    "co.jp", "or.jp", "ne.jp", "ac.jp",
    "co.kr", "or.kr", "ne.kr",
    "com.cn", "net.cn", "org.cn", "gov.cn",
    "com.hk", "com.sg", "com.tw", "co.il", "co.nz", "co.za", "co.in",
    "com.mx", "com.ar", "com.pl", "com.es", "com.de", "co.id", "com.vn",
}

HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _as_ipv4_net(value: str) -> Optional[ipaddress.IPv4Network]:
    txt = (value or "").strip()
    if not txt or ":" in txt:
        return None
    try:
        net = ipaddress.ip_network(txt, strict=False)
        if isinstance(net, ipaddress.IPv4Network):
            return net
    except ValueError:
        pass
    return None


def normalize_ipv4_or_cidr(raw: str) -> Optional[str]:
    """
    Принимает одиночный IPv4 (1.2.3.4) или подсеть IPv4 CIDR (1.2.3.0/24).
    Явно отклоняет IPv6 и 0.0.0.0/0. /32 приводится к одиночному IPv4.
    """
    txt = (raw or "").strip()
    if not txt or ":" in txt:
        return None
    if "/" in txt:
        net = _as_ipv4_net(txt)
        if not net or net.prefixlen == 0:
            return None
        return str(net.network_address) if net.prefixlen == 32 else str(net)
    try:
        addr = ipaddress.ip_address(txt)
        if isinstance(addr, ipaddress.IPv4Address):
            return str(addr)
    except ValueError:
        return None
    return None


def normalize_domain(raw: str, keep_subdomain: bool = False) -> Optional[str]:
    """
    Приводит URL / домен к чистому виду, отбрасывая схему, путь, порт, '*.' и 'www.'.
    По правилам KeeneticOS по умолчанию берёт верхний регистрируемый домен (eTLD+1),
    который автоматически покрывает все поддомены.
    """
    txt = (raw or "").strip().lower()
    if not txt:
        return None
    if "://" in txt:
        parsed = urlparse(txt)
        host = parsed.hostname or ""
    else:
        txt = txt.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()
        if ":" in txt and not txt.startswith("["):
            host = txt.split(":", 1)[0].strip()
        else:
            host = txt

    host = host.strip(".")
    if host.startswith("*."):
        host = host[2:]
    if host.startswith("www."):
        host = host[4:]

    if not host or _is_ip(host) or "." not in host:
        return None

    try:
        host = host.encode("idna").decode("ascii").lower()
    except Exception:
        return None

    if not HOST_RE.match(host):
        return None

    if keep_subdomain:
        return host

    labels = host.split(".")
    if len(labels) <= 2:
        return host

    last_two = f"{labels[-2]}.{labels[-1]}"
    if last_two in TWO_PART_SUFFIXES and len(labels) >= 3:
        return f"{labels[-3]}.{last_two}"
    return last_two


def normalize_entry(raw: str, keep_subdomain: bool = False) -> Optional[str]:
    """
    Универсальная нормализация записи для Netcraze/KeeneticOS 5.x object-group fqdn:
    - Сначала проверяет IPv4 или IPv4 CIDR (отсекает IPv6).
    - Иначе нормализует домен (eTLD+1 по умолчанию).
    """
    ip_val = normalize_ipv4_or_cidr(raw)
    if ip_val:
        return ip_val
    return normalize_domain(raw, keep_subdomain=keep_subdomain)


def check_domain_in_list(domain: str, existing: List[str]) -> Dict[str, Any]:
    """Проверяет точное наличие и перекрытие родителем/подсетью (как для доменов, так и для IPv4/CIDR)."""
    entry = domain.strip().lower()
    ex_set = {d.strip().lower() for d in existing if d and d.strip()}
    exact = entry in ex_set

    entry_net = _as_ipv4_net(entry)
    if entry_net is not None:
        covered_by: Optional[str] = None
        children: List[str] = []
        for ex in sorted(ex_set):
            ex_net = _as_ipv4_net(ex)
            if ex_net is None or ex == entry:
                continue
            if entry_net.subnet_of(ex_net):
                if covered_by is None:
                    covered_by = ex
            elif ex_net.subnet_of(entry_net):
                children.append(ex)
        return {
            "domain": entry,
            "kind": "ipv4",
            "exact": exact,
            "covered_by_parent": covered_by,
            "existing_children": children,
            "already_routed": bool(exact or covered_by),
        }

    covered_by = None
    parts = entry.split(".")
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in ex_set:
            covered_by = parent
            break

    children = sorted(d for d in ex_set if d.endswith("." + entry))
    return {
        "domain": entry,
        "kind": "domain",
        "exact": exact,
        "covered_by_parent": covered_by,
        "existing_children": children,
        "already_routed": bool(exact or covered_by),
    }


def _parse_fqdn_groups_meta(rci_payload: Any) -> Dict[str, Dict[str, Any]]:
    """Извлекает {group_name: {'domains': [...], 'description': str, 'count': int}} из ответа GET /rci/object-group/fqdn."""
    groups: Dict[str, Dict[str, Any]] = {}
    if not isinstance(rci_payload, dict):
        return groups

    # Возможна обёртка {"object-group": {"fqdn": {...}}} или сразу {"GroupName": {...}}
    root = rci_payload
    if "object-group" in root and isinstance(root["object-group"], dict):
        og = root["object-group"]
        if "fqdn" in og and isinstance(og["fqdn"], dict):
            root = og["fqdn"]
    elif "fqdn" in root and isinstance(root["fqdn"], dict) and "include" not in root:
        root = root["fqdn"]

    for gname, gdata in root.items():
        if not isinstance(gdata, dict):
            continue
        inc = gdata.get("include")
        domains: Set[str] = set()
        if isinstance(inc, list):
            for entry in inc:
                if isinstance(entry, str):
                    val = entry.strip().lower()
                elif isinstance(entry, dict):
                    val = str(entry.get("address") or entry.get("name") or entry.get("domain") or "").strip().lower()
                else:
                    val = ""
                if val:
                    domains.add(val)
        elif isinstance(inc, dict):
            for k in inc.keys():
                if k:
                    domains.add(str(k).strip().lower())
        desc = str(gdata.get("description") or gdata.get("comment") or "").strip()
        sorted_domains = sorted(domains)
        groups[str(gname)] = {
            "domains": sorted_domains,
            "description": desc,
            "count": len(sorted_domains),
        }
    return groups


def _parse_fqdn_groups(rci_payload: Any) -> Dict[str, List[str]]:
    """Извлекает {group_name: [domains]} из ответа GET /rci/object-group/fqdn."""
    meta = _parse_fqdn_groups_meta(rci_payload)
    return {g: info["domains"] for g, info in meta.items()}


class KeeneticPlugin(PorfiriyPlugin):
    """Плагин управления маршрутизацией доменов Keenetic / Netcraze строго через keenetic_api в HA."""

    provider_id = "builtin.keenetic"

    @staticmethod
    async def resolve_entry_id(ha_api: Any, cfg: Dict[str, Any]) -> str:
        eid = str(cfg.get("entry_id") or "").strip()
        if eid:
            return eid
        if ha_api:
            entries = await ha_api.get_config_entries("keenetic_api")
            for e in entries:
                if e.get("entry_id"):
                    return str(e["entry_id"]).strip()
        return ""

    @staticmethod
    async def rci_call(
        ha_api: Any,
        entry_id: str,
        method: str,
        endpoint: str,
        data_json: Any = None,
    ) -> Any:
        if not ha_api:
            raise RuntimeError("Home Assistant API не инициализирован")
        if not entry_id:
            raise RuntimeError("Не задан entry_id интеграции keenetic_api")
        res = await ha_api.call_service_ws(
            "keenetic_api",
            "request_api",
            {
                "entry_id": entry_id,
                "method": method.upper(),
                "endpoint": endpoint,
                "data_json": data_json if data_json is not None else {},
            },
            return_response=True,
        )
        if isinstance(res, dict) and res.get("status") == "error":
            raise RuntimeError(str(res.get("error") or "Ошибка keenetic_api.request_api"))
        payload = res.get("response", res) if isinstance(res, dict) else res
        if isinstance(payload, dict) and "response" in payload and len(payload) == 1:
            payload = payload["response"]
        return payload

    @classmethod
    async def fetch_fqdn_groups_meta(cls, ha_api: Any, entry_id: str) -> Dict[str, Dict[str, Any]]:
        raw = await cls.rci_call(ha_api, entry_id, "GET", "/rci/object-group/fqdn")
        meta = _parse_fqdn_groups_meta(raw)
        # Если description пуст в object-group/fqdn, пробуем подтянуть описание из dns-proxy/route
        if meta and any(not info.get("description") for info in meta.values()):
            try:
                dns_routes = await cls.rci_call(ha_api, entry_id, "GET", "/rci/dns-proxy/route")
                if isinstance(dns_routes, list):
                    for item in dns_routes:
                        if not isinstance(item, dict):
                            continue
                        gname = str(item.get("group") or item.get("object-group") or "").strip()
                        cmt = str(item.get("comment") or item.get("description") or "").strip()
                        if gname and cmt and gname in meta and not meta[gname].get("description"):
                            meta[gname]["description"] = cmt
                elif isinstance(dns_routes, dict):
                    for gname, item in dns_routes.items():
                        if isinstance(item, dict):
                            cmt = str(item.get("comment") or item.get("description") or "").strip()
                            if cmt and gname in meta and not meta[gname].get("description"):
                                meta[gname]["description"] = cmt
            except Exception:
                pass
        return meta

    @classmethod
    async def fetch_fqdn_groups(cls, ha_api: Any, entry_id: str) -> Dict[str, List[str]]:
        meta = await cls.fetch_fqdn_groups_meta(ha_api, entry_id)
        return {g: info["domains"] for g, info in meta.items()}

    @classmethod
    async def apply_add_domains(cls, ha_api: Any, entry_id: str, group_name: str, domains: List[str]) -> str:
        payload = {group_name: {"include": [{"address": d} for d in domains]}}
        await cls.rci_call(ha_api, entry_id, "POST", "/rci/object-group/fqdn", payload)
        await cls.rci_call(ha_api, entry_id, "POST", "/rci/system/configuration/save", {})
        return f"Добавлено в группу «{group_name}» ({len(domains)}): {', '.join(domains[:10])}{'…' if len(domains) > 10 else ''}. Конфиг роутера сохранён."

    @classmethod
    async def apply_remove_domain(cls, ha_api: Any, entry_id: str, group_name: str, domain: str) -> str:
        payload = {group_name: {"include": [{"address": domain, "no": True}]}}
        await cls.rci_call(ha_api, entry_id, "POST", "/rci/object-group/fqdn", payload)
        await cls.rci_call(ha_api, entry_id, "POST", "/rci/system/configuration/save", {})
        return f"Удалён домен {domain} из группы «{group_name}». Конфиг роутера сохранён."

    @classmethod
    async def test_connection(cls, db: PorfiriyDB, ha_api: Any) -> Dict[str, Any]:
        prov = db.get_provider(cls.provider_id, mask_secrets=False)
        if not prov:
            return {"success": False, "error": "Провайдер builtin.keenetic не найден в БД"}
        cfg = dict(prov.get("config") or {})
        try:
            entry_id = await cls.resolve_entry_id(ha_api, cfg)
            if not entry_id:
                return {"success": False, "error": "Не найдена интеграция keenetic_api в Home Assistant"}
            ver = await cls.rci_call(ha_api, entry_id, "GET", "/rci/show/version")
            groups_meta = await cls.fetch_fqdn_groups_meta(ha_api, entry_id)

            compact_groups = {
                k: {"description": v.get("description", ""), "count": v.get("count", 0)}
                for k, v in groups_meta.items()
            }
            changed = False
            if not cfg.get("entry_id"):
                cfg["entry_id"] = entry_id
                changed = True
            if not cfg.get("default_group") and groups_meta:
                cfg["default_group"] = next(iter(groups_meta.keys()))
                changed = True
            if cfg.get("cached_groups") != compact_groups:
                cfg["cached_groups"] = compact_groups
                changed = True
            if changed:
                db.upsert_provider(
                    provider_id=cls.provider_id,
                    kind="builtin",
                    title=prov["title"],
                    description=prov["description"],
                    enabled=prov["enabled"],
                    config=cfg,
                )

            model_str = ""
            if isinstance(ver, dict):
                model_str = f"{ver.get('model') or ver.get('device') or 'Keenetic'} (OS {ver.get('release') or ver.get('title') or '?'})"
            return {
                "success": True,
                "entry_id": entry_id,
                "router": model_str or "Keenetic / Netcraze",
                "default_group": cfg.get("default_group", ""),
                "groups": {k: v["count"] for k, v in groups_meta.items()},
                "groups_meta": compact_groups,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    async def _fetch_geosite_domains(
        url_template: str,
        name: str,
        depth: int = 0,
        visited: Optional[Set[str]] = None,
    ) -> List[str]:
        if visited is None:
            visited = set()
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "", (name or "").strip().lower())
        if not slug or slug in visited or depth > 2:
            return []
        visited.add(slug)

        url = url_template.format(name=slug)
        raw_text = ""
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"GeoSite '{slug}' не найден (HTTP {resp.status})")
                raw_text = await resp.text()

        collected: List[str] = []
        includes: List[str] = []
        for line in raw_text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            token = line.split()[0]
            if token.startswith("include:"):
                includes.append(token.split(":", 1)[1])
                continue
            if token.startswith("regexp:"):
                continue
            if ":" in token:
                prefix, val = token.split(":", 1)
                if prefix not in ("domain", "full", "keyword"):
                    continue
                if prefix == "keyword":
                    continue
                token = val
            norm = normalize_entry(token, keep_subdomain=False)
            if norm and norm not in collected:
                collected.append(norm)
            if len(collected) >= 200:
                break

        if depth < 2 and len(collected) < 200:
            for inc in includes[:5]:
                try:
                    sub = await KeeneticPlugin._fetch_geosite_domains(url_template, inc, depth + 1, visited)
                    for d in sub:
                        if d not in collected:
                            collected.append(d)
                        if len(collected) >= 200:
                            break
                except Exception:
                    pass
                if len(collected) >= 200:
                    break
        return collected

    @staticmethod
    def _dns_resolve(host: str) -> List[str]:
        try:
            infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
            ips = sorted({str(info[4][0]) for info in infos if info[4]})
            return ips[:10]
        except Exception:
            return []

    def get_tool_specs(self, db: PorfiriyDB, provider: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "name": "keenetic_list_groups",
                "description": (
                    "Получить список групп доменов (object-group fqdn) с роутера Keenetic/Netcraze "
                    "и количество доменов в каждой группе."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "keenetic_check_domain",
                "description": (
                    "Проверить, есть ли указанный домен, URL, IPv4-адрес или IPv4-подсеть CIDR в списках роутера Keenetic/Netcraze. "
                    "Для доменов извлекает верхний домен (eTLD+1), для IPv4 проверяет вхождение в подсети."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "Домен, URL, IPv4-адрес или IPv4 CIDR для проверки"},
                        "group": {"type": "string", "description": "Имя группы fqdn (если не указано — проверит все группы)"},
                    },
                    "required": ["domain"],
                },
            },
            {
                "name": "keenetic_resolve_pool",
                "description": (
                    "Найти пул доменов/IPv4 сервиса через GeoSite (например 'openai', 'youtube', 'discord', 'instagram') "
                    "или определить IPv4/ASN/страну и верхний домен через GeoIP/DNS, сверив с роутером."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Имя сервиса/категории GeoSite (openai, youtube...) или домен/IPv4 для GeoIP"},
                        "mode": {"type": "string", "enum": ["geosite", "geoip"], "description": "Режим поиска: geosite (по умолчанию) или geoip"},
                        "group": {"type": "string", "description": "Группа fqdn на роутере для сверки дубликатов"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "keenetic_add_domains",
                "description": (
                    "Подготовить и отправить на согласование в Telegram (кнопками Да/Нет) или сразу добавить "
                    "домены, IPv4-адреса или IPv4 CIDR-подсети (пулом или по одному) в список маршрутизации роутера Netcraze/Keenetic (IPv6 не поддерживается и отбрасывается). "
                    "Берёт верхний домен (для URL/доменов) и отсекает всё, что уже покрыто на роутере."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Список доменов, URL, IPv4-адресов или IPv4 CIDR-подсетей для добавления",
                        },
                        "group": {"type": "string", "description": "Имя группы fqdn (по умолчанию из настроек плагина)"},
                        "keep_subdomain": {"type": "boolean", "description": "True только если пользователь прямо попросил добавить точный поддомен, а не верхний домен"},
                        "reason": {"type": "string", "description": "Короткое описание цели (напр. 'Для ChatGPT')"},
                    },
                    "required": ["domains"],
                },
            },
            {
                "name": "keenetic_remove_domain",
                "description": "Удалить домен, IPv4 или IPv4 CIDR из группы маршрутизации fqdn на роутере Keenetic (с согласованием в Telegram при настройке confirm_mode=always).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "Домен, URL, IPv4 или IPv4 CIDR для удаления"},
                        "group": {"type": "string", "description": "Имя группы fqdn (по умолчанию из настроек)"},
                    },
                    "required": ["domain"],
                },
            },
        ]

    def get_prompt_snippet(self, db: PorfiriyDB, provider: Dict[str, Any], options: Dict[str, Any]) -> str:
        cfg = provider.get("config") or {}
        def_group = cfg.get("default_group") or "не задана"
        cached_groups = cfg.get("cached_groups") or {}
        groups_hint = ""
        if isinstance(cached_groups, dict) and cached_groups:
            parts = []
            for gname, gmeta in cached_groups.items():
                desc = (gmeta or {}).get("description") or ""
                cnt = (gmeta or {}).get("count", 0)
                parts.append(f"`{gname}` ({desc}, {cnt})" if desc else f"`{gname}` ({cnt})")
            groups_hint = "\n- Known router fqdn groups: " + ", ".join(parts) + "."
        return (
            "### KEENETIC / NETCRAZE DOMAIN ROUTING (`builtin.keenetic`):\n"
            f"- Default fqdn group: `{def_group}`.{groups_hint}\n"
            "- Router lists (`object-group fqdn` / `domain-list*`) support BOTH domain names AND IPv4 addresses / IPv4 CIDR subnets (IPv6 is NOT supported and must be ignored).\n"
            "- Use `keenetic_*` tools whenever the user asks about router routing, VPN lists, domains, IPv4 addresses/subnets, Keenetic/Netcraze, GeoSite/GeoIP pools, checking or adding entries.\n"
            "- If a domain or URL is given, normalize to top registrable domain unless the user explicitly requested a subdomain.\n"
            "- If `keenetic_add_domains` or `keenetic_remove_domain` returns `status='awaiting_approval'`: follow its `message` field! When already inside Telegram chat, NEVER say 'проверяйте Telegram' — simply say the confirmation buttons were sent above."
        )

    async def execute_tool(self, tool_name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        db: PorfiriyDB = ctx["db"]
        ha_api = ctx.get("ha_api")
        prov = ctx["provider"]
        cfg = prov.get("config") or {}
        from_telegram = str((ctx.get("dev") or {}).get("mac") or "").startswith("tg_")

        entry_id = await self.resolve_entry_id(ha_api, cfg)
        if not entry_id:
            return {"status": "error", "error": "Не найдена интеграция keenetic_api в Home Assistant"}

        default_group = str(cfg.get("default_group") or "").strip()

        if tool_name == "keenetic_list_groups":
            meta = await self.fetch_fqdn_groups_meta(ha_api, entry_id)
            return {
                "status": "ok",
                "default_group": default_group,
                "groups": {
                    k: {
                        "description": v.get("description", ""),
                        "count": v.get("count", 0),
                        "sample": (v.get("domains") or [])[:10],
                    }
                    for k, v in meta.items()
                },
            }

        if tool_name == "keenetic_check_domain":
            raw_dom = str(args.get("domain") or "").strip()
            dom = normalize_entry(raw_dom, keep_subdomain=False)
            exact_sub = normalize_entry(raw_dom, keep_subdomain=True)
            if not dom:
                return {"status": "error", "error": f"Некорректный домен, URL или IPv4/CIDR (IPv6 не поддерживается): {raw_dom}"}
            groups = await self.fetch_fqdn_groups(ha_api, entry_id)
            target_group = str(args.get("group") or "").strip()
            if target_group:
                if target_group not in groups:
                    return {"status": "error", "error": f"Группа '{target_group}' не найдена. Доступны: {', '.join(groups.keys())}"}
                check_groups = {target_group: groups[target_group]}
            else:
                check_groups = groups
            results = {}
            for gname, gdomains in check_groups.items():
                info_top = check_domain_in_list(dom, gdomains)
                info_sub = check_domain_in_list(exact_sub, gdomains) if exact_sub and exact_sub != dom else info_top
                results[gname] = {
                    "top_domain": dom,
                    "sub_domain": exact_sub,
                    "top_check": info_top,
                    "sub_check": info_sub,
                }
            return {"status": "ok", "domain_input": raw_dom, "top_domain": dom, "groups": results}

        if tool_name == "keenetic_resolve_pool":
            query = str(args.get("query") or "").strip()
            mode = str(args.get("mode") or "geosite").strip().lower()
            target_group = str(args.get("group") or default_group).strip()
            groups = await self.fetch_fqdn_groups(ha_api, entry_id)
            existing = groups.get(target_group, []) if target_group else []

            if mode == "geoip":
                host = normalize_domain(query, keep_subdomain=True) or query
                top = normalize_domain(query, keep_subdomain=False)
                ips = await asyncio.to_thread(self._dns_resolve, host) if not _is_ip(host) else [host]
                geo_target = ips[0] if ips else host
                geo_tpl = str(cfg.get("geoip_url_template") or DEFAULT_GEOIP_URL)
                geo_info: Dict[str, Any] = {}
                try:
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(geo_tpl.format(query=geo_target), timeout=aiohttp.ClientTimeout(total=12)) as resp:
                            if resp.status == 200:
                                geo_info = await resp.json(content_type=None)
                except Exception as e:
                    geo_info = {"error": str(e)}
                chk = check_domain_in_list(top, existing) if (top and target_group) else None
                ips_check = [check_domain_in_list(ip, existing) for ip in ips] if (ips and target_group) else []
                return {
                    "status": "ok",
                    "mode": "geoip",
                    "query": query,
                    "host": host,
                    "top_domain": top,
                    "resolved_ips": ips,
                    "geoip": geo_info,
                    "router_group": target_group,
                    "router_check": chk,
                    "router_ips_check": ips_check,
                }

            geosite_tpl = str(cfg.get("geosite_url_template") or DEFAULT_GEOSITE_URL)
            domains = await self._fetch_geosite_domains(geosite_tpl, query)
            new_list: List[str] = []
            already_list: List[Dict[str, Any]] = []
            for d in domains:
                chk = check_domain_in_list(d, existing) if existing else {"already_routed": False}
                if chk["already_routed"]:
                    already_list.append({"domain": d, "covered_by": chk.get("covered_by_parent") or d})
                else:
                    new_list.append(d)
            return {
                "status": "ok",
                "mode": "geosite",
                "query": query,
                "router_group": target_group,
                "total_found": len(domains),
                "new_count": len(new_list),
                "already_count": len(already_list),
                "new_domains": new_list[:60],
                "already_covered_sample": already_list[:15],
            }

        if tool_name == "keenetic_add_domains":
            raw_domains = args.get("domains") or []
            if isinstance(raw_domains, str):
                raw_domains = [x.strip() for x in re.split(r"[\s,;]+", raw_domains) if x.strip()]
            keep_sub = bool(args.get("keep_subdomain", False))
            target_group = str(args.get("group") or default_group).strip()
            reason = str(args.get("reason") or "").strip()

            groups_meta = await self.fetch_fqdn_groups_meta(ha_api, entry_id)
            groups = {k: v["domains"] for k, v in groups_meta.items()}
            if not target_group:
                if len(groups) == 1:
                    target_group = next(iter(groups.keys()))
                else:
                    return {
                        "status": "error",
                        "error": f"Не указана группа fqdn. Доступные группы: {', '.join(groups.keys()) or 'нет'}",
                    }
            if target_group not in groups:
                return {"status": "error", "error": f"Группа '{target_group}' не найдена на роутере. Доступны: {', '.join(groups.keys())}"}

            group_desc = str((groups_meta.get(target_group) or {}).get("description") or "").strip()
            group_label = f"{target_group} ({group_desc})" if group_desc else target_group

            existing = groups[target_group]
            normalized: List[str] = []
            invalid: List[str] = []
            for raw in raw_domains:
                d = normalize_entry(str(raw), keep_subdomain=keep_sub)
                if not d:
                    invalid.append(str(raw))
                elif d not in normalized:
                    normalized.append(d)

            if not normalized:
                return {"status": "error", "error": f"Не удалось распознать валидные домены или IPv4/CIDR (IPv6 отклонён): {invalid}"}

            to_add: List[str] = []
            skipped: List[str] = []
            children_note: List[str] = []
            for d in normalized:
                chk = check_domain_in_list(d, existing)
                if chk["already_routed"]:
                    reason_skip = chk["covered_by_parent"] or "точное совпадение"
                    skipped.append(f"{d} (уже есть: {reason_skip})")
                else:
                    to_add.append(d)
                    if chk["existing_children"]:
                        children_note.append(f"{d} покроет существующие поддомены {', '.join(chk['existing_children'][:3])}")

            if not to_add:
                return {
                    "status": "ok",
                    "action": "already_present",
                    "group": target_group,
                    "skipped": skipped,
                    "message": f"Все указанные домены уже покрыты правилами в группе «{target_group}»: {', '.join(skipped)}",
                }

            confirm_mode = str(cfg.get("confirm_mode") or "always").strip()
            need_confirm = (confirm_mode == "always") or (confirm_mode == "pool_only" and len(to_add) >= 2)

            if not need_confirm:
                done_msg = await self.apply_add_domains(ha_api, entry_id, target_group, to_add)
                return {
                    "status": "ok",
                    "action": "added",
                    "group": target_group,
                    "added": to_add,
                    "skipped": skipped,
                    "message": done_msg,
                }

            approver = str(cfg.get("approver_user_id") or ctx.get("speaker_user_id") or "").strip() or None
            lines = ["Роутер: Keenetic / Netcraze", f"Группа fqdn: {group_label}"]
            if reason:
                lines.append(f"Повод: {reason}")
            lines.append(f"К добавлению ({len(to_add)}):")
            for d in to_add[:40]:
                lines.append(f"  + {d}")
            if len(to_add) > 40:
                lines.append(f"  …и ещё {len(to_add) - 40}")
            if skipped:
                lines.append(f"\nУже есть/покрыто ({len(skipped)}): {', '.join(skipped[:8])}")
            if children_note:
                lines.append(f"\nПримечание: {'; '.join(children_note[:3])}")

            async def _approve_cb() -> str:
                return await self.apply_add_domains(ha_api, entry_id, target_group, to_add)

            ap_res = await approval_manager.request_approval(
                title="🌐 Согласование маршрута Keenetic",
                details_text="\n".join(lines),
                on_approve=_approve_cb,
                target_user_id=approver,
                confirm_label="✅ Да, добавить",
                cancel_label="❌ Отмена",
                db=db,
            )
            if not ap_res.get("success"):
                return {"status": "error", "error": ap_res.get("error") or "Не удалось отправить запрос на согласование"}

            msg_text = (
                f"Карточка с кнопками Да/Нет уже отправлена выше в этот чат для группы «{group_label}» ({len(to_add)} шт.). Не пиши 'проверяйте Telegram', просто кратко предложи подтвердить кнопкой выше."
                if from_telegram
                else f"Подготовлено к добавлению в «{group_label}»: {len(to_add)} шт. ({', '.join(to_add[:5])}). Отправлено на согласование пользователю {ap_res.get('recipient')} в Telegram с кнопками Да/Нет."
            )
            return {
                "status": "awaiting_approval",
                "group": target_group,
                "recipient": ap_res.get("recipient"),
                "to_add": to_add,
                "skipped": skipped,
                "message": msg_text,
            }

        if tool_name == "keenetic_remove_domain":
            raw_dom = str(args.get("domain") or "").strip()
            dom = normalize_entry(raw_dom, keep_subdomain=True)
            target_group = str(args.get("group") or default_group).strip()
            if not dom or not target_group:
                return {"status": "error", "error": "Укажите корректный domain/IPv4/CIDR и group"}

            groups_meta = await self.fetch_fqdn_groups_meta(ha_api, entry_id)
            groups = {k: v["domains"] for k, v in groups_meta.items()}
            if target_group not in groups:
                return {"status": "error", "error": f"Группа '{target_group}' не найдена"}
            if dom not in groups[target_group]:
                return {"status": "ok", "action": "not_found", "message": f"Домена {dom} нет в группе «{target_group}»."}
            group_desc = str((groups_meta.get(target_group) or {}).get("description") or "").strip()
            group_label = f"{target_group} ({group_desc})" if group_desc else target_group

            confirm_mode = str(cfg.get("confirm_mode") or "always").strip()
            if confirm_mode != "always":
                done_msg = await self.apply_remove_domain(ha_api, entry_id, target_group, dom)
                return {"status": "ok", "action": "removed", "message": done_msg}

            approver = str(cfg.get("approver_user_id") or ctx.get("speaker_user_id") or "").strip() or None

            async def _approve_del() -> str:
                return await self.apply_remove_domain(ha_api, entry_id, target_group, dom)

            ap_res = await approval_manager.request_approval(
                title="🗑️ Удаление домена из Keenetic",
                details_text=f"Группа fqdn: {group_label}\nУдалить домен: {dom}",
                on_approve=_approve_del,
                target_user_id=approver,
                confirm_label="✅ Да, удалить",
                cancel_label="❌ Отмена",
                db=db,
            )
            if not ap_res.get("success"):
                return {"status": "error", "error": ap_res.get("error")}
            msg_text = (
                f"Карточка на удаление {dom} из «{group_label}» отправлена выше в этот чат. Не пиши 'проверяйте Telegram', просто предложи подтвердить кнопкой выше."
                if from_telegram
                else f"Запрос на удаление {dom} из «{group_label}» отправлен в Telegram ({ap_res.get('recipient')})."
            )
            return {
                "status": "awaiting_approval",
                "recipient": ap_res.get("recipient"),
                "message": msg_text,
            }

        return {"status": "error", "error": f"Unknown keenetic tool: {tool_name}"}
