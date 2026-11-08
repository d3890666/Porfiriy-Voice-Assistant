import logging
import json
import asyncio
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import asyncssh
    HAS_ASYNCSSH = True
except ImportError:
    asyncssh = None
    HAS_ASYNCSSH = False

DEFAULT_SSH_PORT = 2222
QUASAR_CONFIG_PATH = "/data/opt/quasar-bridge/config.json"
QUASAR_DIR = "/data/opt/quasar-bridge"

class QuasarManager:
    """Управление конфигурацией и маршрутизацией Quasar Bridge на Яндекс Станции через SSH."""

    def __init__(self):
        pass

    async def _open_ssh_connection(self, ip: str, port: int = DEFAULT_SSH_PORT, timeout: float = 3.0):
        if not HAS_ASYNCSSH:
            raise RuntimeError("asyncssh library is not installed")
        return await asyncio.wait_for(
            asyncssh.connect(
                ip,
                port=port,
                username="root",
                known_hosts=None,
                client_keys=None
            ),
            timeout=timeout
        )

    async def get_config(self, ip: str, port: int = DEFAULT_SSH_PORT) -> Dict[str, Any]:
        """Считывает конфигурацию Quasar Bridge с колонки и возвращает структурированные правила."""
        if not ip or ip == "---":
            return {"error": "IP-адрес колонки не указан"}

        async with await self._open_ssh_connection(ip, port) as conn:
            result = await conn.run(f"cat {QUASAR_CONFIG_PATH}", check=False)
            if result.exit_status != 0:
                return {"error": f"Не удалось прочитать {QUASAR_CONFIG_PATH}: {result.stderr.strip()}"}

            try:
                config_json = json.loads(result.stdout)
            except Exception as e:
                return {"error": f"Ошибка парсинга JSON конфигурации: {e}"}

            routing = config_json.get("routing", {})
            rules = routing.get("rules", [])
            
            # Извлекаем правила
            smart_home_rule = next((r for r in rules if r.get("name") == "yandex_smart_home"), None)
            explicit_llm_rule = next((r for r in rules if r.get("name") == "explicit_llm"), None)
            timers_rule = next((r for r in rules if r.get("name") == "yandex_timers_and_alarms"), None)
            system_rule = next((r for r in rules if r.get("name") == "yandex_system_controls"), None)

            smart_home_keywords = []
            music_exceptions = []
            if smart_home_rule:
                match = smart_home_rule.get("match", {})
                if "contains_any" in match:
                    smart_home_keywords = match.get("contains_any", [])
                elif "contains_all_groups" in match:
                    # Разворачиваем группы для отображения
                    groups = match.get("contains_all_groups", [])
                    smart_home_keywords = [item for grp in groups for item in grp]
                music_exceptions = match.get("contains_none", [])

            return {
                "success": True,
                "ip": ip,
                "port": port,
                "default_route": routing.get("default_route", "llm"),
                "smart_home_keywords": smart_home_keywords,
                "music_exceptions": music_exceptions,
                "llm_keywords": explicit_llm_rule.get("match", {}).get("starts_with_any", []) if explicit_llm_rule else [],
                "timer_keywords": timers_rule.get("match", {}).get("contains_any", []) if timers_rule else [],
                "system_keywords": system_rule.get("match", {}).get("contains_any", []) if system_rule else [],
                "raw_config": config_json
            }

    async def save_config(self, ip: str, port: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Обновляет правила маршрутизации в Quasar Bridge:
        валидирует через quasar-bridge -check-config и перезапускает службу.
        """
        if not ip or ip == "---":
            return {"error": "IP-адрес колонки не указан"}

        async with await self._open_ssh_connection(ip, port) as conn:
            # 1. Читаем текущий оригинальный конфиг
            read_res = await conn.run(f"cat {QUASAR_CONFIG_PATH}", check=False)
            if read_res.exit_status != 0:
                return {"error": f"Не удалось прочитать {QUASAR_CONFIG_PATH}"}

            try:
                config_json = json.loads(read_res.stdout)
            except Exception as e:
                return {"error": f"Ошибка парсинга текущего конфига: {e}"}

            # 2. Обновляем секцию routing
            routing = config_json.setdefault("routing", {})
            if "default_route" in payload:
                routing["default_route"] = payload["default_route"]

            rules = routing.setdefault("rules", [])
            
            # А. Правило explicit_llm
            if "llm_keywords" in payload:
                llm_rule = next((r for r in rules if r.get("name") == "explicit_llm"), None)
                if not llm_rule:
                    llm_rule = {"name": "explicit_llm", "route": "llm", "match": {}}
                    rules.insert(0, llm_rule)
                llm_rule["match"]["starts_with_any"] = [w.strip().lower() for w in payload["llm_keywords"] if w.strip()]

            # Б. Правило yandex_timers_and_alarms
            if "timer_keywords" in payload:
                timer_rule = next((r for r in rules if r.get("name") == "yandex_timers_and_alarms"), None)
                if not timer_rule:
                    timer_rule = {"name": "yandex_timers_and_alarms", "route": "yandex", "match": {}}
                    rules.append(timer_rule)
                timer_rule["match"]["contains_any"] = [w.strip().lower() for w in payload["timer_keywords"] if w.strip()]

            # В. Правило yandex_smart_home (ключевые слова умного дома)
            if "smart_home_keywords" in payload:
                sh_rule = next((r for r in rules if r.get("name") == "yandex_smart_home"), None)
                if not sh_rule:
                    sh_rule = {"name": "yandex_smart_home", "route": "yandex", "match": {}}
                    rules.append(sh_rule)
                
                # Убираем устаревший contains_all_groups, выставляем contains_any
                sh_rule["match"] = {
                    "contains_any": sorted(list(set(w.strip().lower() for w in payload["smart_home_keywords"] if w.strip()))),
                    "contains_none": payload.get("music_exceptions", [
                        "музык", "песн", "радио", "трек", "альбом", "плейлист", "групп", "исполнител"
                    ])
                }

            # Г. Правило yandex_system_controls
            if "system_keywords" in payload:
                sys_rule = next((r for r in rules if r.get("name") == "yandex_system_controls"), None)
                if not sys_rule:
                    sys_rule = {"name": "yandex_system_controls", "route": "yandex", "match": {}}
                    rules.append(sys_rule)
                sys_rule["match"]["contains_any"] = [w.strip().lower() for w in payload["system_keywords"] if w.strip()]

            # 3. Записываем временный файл для валидации
            new_json_str = json.dumps(config_json, indent=2, ensure_ascii=False)
            tmp_path = "/tmp/quasar_config_check.json"
            
            # Экранируем одинарные кавычки для безопасной записи через shell
            escaped_json = new_json_str.replace("'", "'\\''")
            write_res = await conn.run(f"echo '{escaped_json}' > {tmp_path}", check=False)
            if write_res.exit_status != 0:
                return {"error": f"Не удалось записать {tmp_path}"}

            # 4. Валидация через start.sh -check-config
            check_cmd = f"cd {QUASAR_DIR} && set -a && . ./runtime.env && set +a && ./app/bin/quasar-bridge -config {tmp_path} -check-config"
            check_res = await conn.run(check_cmd, check=False)
            if check_res.exit_status != 0:
                err_msg = check_res.stderr.strip() or check_res.stdout.strip()
                await conn.run(f"rm -f {tmp_path}", check=False)
                return {"error": f"Конфигурация не прошла валидацию: {err_msg}"}

            # 5. Перемещаем в постоянный конфиг и перезапускаем службу
            apply_cmd = f"mv {tmp_path} {QUASAR_CONFIG_PATH} && sh {QUASAR_DIR}/app/control.sh restart"
            restart_res = await conn.run(apply_cmd, check=False)
            if restart_res.exit_status != 0:
                return {"error": f"Ошибка перезапуска Quasar Bridge: {restart_res.stderr.strip()}"}

            logger.info(f"[QUASAR] Successfully saved new config on {ip} and restarted bridge daemon")
            return {
                "success": True,
                "message": "Конфигурация успешно применена, Quasar Bridge перезапущен",
                "output": restart_res.stdout.strip()
            }

    async def test_route(self, ip: str, port: int, phrase: str) -> Dict[str, Any]:
        """Тестирует маршрутизацию конкретной фразы через quasar-bridge -route."""
        if not ip or ip == "---":
            return {"error": "IP-адрес колонки не указан"}
        if not phrase:
            return {"error": "Фраза для проверки пуста"}

        escaped_phrase = phrase.replace("'", "'\\''")
        cmd = f"cd {QUASAR_DIR} && set -a && . ./runtime.env && set +a && ./app/bin/quasar-bridge -route '{escaped_phrase}'"

        async with await self._open_ssh_connection(ip, port) as conn:
            res = await conn.run(cmd, check=False)
            out = res.stdout.strip()
            
            try:
                # Quasar Bridge выводит JSON вида: {"route":"yandex","rule":"yandex_smart_home","source":"rule"}
                parsed = json.loads(out)
                return {
                    "success": True,
                    "phrase": phrase,
                    "route": parsed.get("route"),
                    "rule": parsed.get("rule", "—"),
                    "source": parsed.get("source", "—"),
                    "raw": out
                }
            except Exception:
                return {
                    "success": True,
                    "phrase": phrase,
                    "route": "unknown",
                    "raw": out or res.stderr.strip()
                }
