import os
import socket
from typing import Any, Dict, Optional


def normalize_mac(mac: str) -> str:
    """Нормализует MAC-адрес для сравнения (удаляет ':' и '-', приводит к нижнему регистру)."""
    if not mac:
        return ""
    return str(mac).replace(":", "").replace("-", "").strip().lower()


def clean_ip(ip: Optional[str]) -> str:
    """Очищает IP-адрес от IPv4-mapped IPv6 префикса (::ffff:) и пробелов."""
    if not ip:
        return ""
    val = str(ip).strip()
    if val.startswith("::ffff:"):
        val = val[7:]
    return val


def is_valid_lan_ip(ip: Optional[str]) -> bool:
    """Проверяет, является ли строка валидным не-loopback IP-адресом."""
    val = clean_ip(ip)
    if not val or val in ("---", "unknown", "localhost", "::1"):
        return False
    if val.startswith("127."):
        return False
    return True


def get_device_ip(device: Optional[Dict[str, Any]]) -> str:
    """Извлекает валидный IP из объекта устройства (top-level или config.ip)."""
    if not device or not isinstance(device, dict):
        return ""
    for candidate in (device.get("ip"), (device.get("config") or {}).get("ip")):
        val = clean_ip(candidate)
        if is_valid_lan_ip(val):
            return val
    return ""


def extract_client_ip(request: Any) -> str:
    """Безопасно извлекает IP клиента из aiohttp.web.Request (учитывая X-Forwarded-For и ::ffff:)."""
    if request is None:
        return ""
    try:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            first = clean_ip(xff.split(",")[0])
            if is_valid_lan_ip(first):
                return first
        xri = clean_ip(request.headers.get("X-Real-IP", ""))
        if is_valid_lan_ip(xri):
            return xri
        remote = clean_ip(getattr(request, "remote", ""))
        if remote:
            return remote
    except Exception:
        pass
    return ""


def get_local_addon_base_url(port: int = 8099) -> str:
    """Возвращает базовый HTTP URL аддона для локальных медиа-запросов (без хардкода, с фолбэком)."""
    env_url = os.environ.get("PORFIRIY_BASE_URL", "").strip().rstrip("/")
    if env_url:
        return env_url
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        finally:
            s.close()
        if is_valid_lan_ip(local_ip):
            return f"http://{local_ip}:{port}"
    except Exception:
        pass
    return f"http://homeassistant:{port}"
