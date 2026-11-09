import os
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
SQUEEZELITE_DIR = "/data/opt/squeezelite"
SQUEEZELITE_SERVICE_PATH = "/data/opt/squeezelite/service"
SQUEEZELITE_INIT_PATH = "/etc/init.d/S97squeezelite"
STATION_AIRPLAY_DIR = "/data/opt/station-airplay"
STATION_AIRPLAY_SERVICE_PATH = "/data/opt/station-airplay/service"
STATION_AIRPLAY_INIT_PATH = "/etc/init.d/S98station-airplay"
STATION_DUCKING_DIR = "/data/opt/station-ducking"
STATION_DUCKING_SERVICE_PATH = "/data/opt/station-ducking/service"
STATION_DUCKING_INIT_PATH = "/etc/init.d/S96ducking"
SET_VOLUME_PATH = "/system/vendor/quasar/set_volume.sh"
ASOUND_CONF_PATH = "/etc/asound.conf"
STATION_DIST_DIR = os.path.join(os.path.dirname(__file__), "station_dist")

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

    def get_bundled_version_info(self) -> Dict[str, Any]:
        """Возвращает информацию о встроенной в аддон версии Quasar Bridge Porfiriy Edition."""
        manifest_path = os.path.join(STATION_DIST_DIR, "version.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"[QUASAR] Failed to read bundled version manifest: {e}")
        return {
            "version": "1.2.4-porfiriy.1",
            "name": "Quasar Bridge Porfiriy Edition",
            "target": "yandex_station_2"
        }

    async def get_version_info(self, ip: str, port: int = DEFAULT_SSH_PORT) -> Dict[str, Any]:
        """Опрашивает версию Quasar Bridge, установленную на колонке, и сравнивает с бандлом аддона."""
        if not ip or ip == "---":
            return {"error": "IP-адрес колонки не указан"}

        bundled = self.get_bundled_version_info()
        bundled_ver = bundled.get("version", "1.2.4-porfiriy.1")

        try:
            async with await self._open_ssh_connection(ip, port) as conn:
                # 1. Проверяем файл version.json на колонке
                ver_check = await conn.run("cat /data/opt/quasar-bridge/version.json 2>/dev/null", check=False)
                installed_ver = ""
                is_native = False

                if ver_check.exit_status == 0 and ver_check.stdout.strip():
                    try:
                        v_data = json.loads(ver_check.stdout.strip())
                        installed_ver = v_data.get("version", "")
                        is_native = True
                    except Exception:
                        installed_ver = ver_check.stdout.strip()

                # 2. Если нет version.json, запускаем бинарник с ключом -version или проверяем файл
                if not installed_ver:
                    bin_check = await conn.run(f"{QUASAR_DIR}/app/bin/quasar-bridge -version 2>/dev/null", check=False)
                    if bin_check.exit_status == 0 and bin_check.stdout.strip():
                        installed_ver = bin_check.stdout.strip()
                    else:
                        installed_ver = "1.2.3 (сторонний)"

                # 3. Проверка статуса squeezelite и автозапуска
                sq_check = await conn.run("ps | grep -v grep | grep squeezelite", check=False)
                sq_running = (sq_check.exit_status == 0 and bool(sq_check.stdout.strip()))

                sq_autostart_check = await conn.run(f"test -x {SQUEEZELITE_INIT_PATH} && echo 'ok'", check=False)
                sq_autostart = (sq_autostart_check.exit_status == 0 and sq_autostart_check.stdout.strip() == "ok")

                # 4. Проверка статуса station-airplay и автозапуска
                ap_check = await conn.run("ps | grep -v grep | grep station-airplay", check=False)
                ap_running = (ap_check.exit_status == 0 and bool(ap_check.stdout.strip()))

                ap_autostart_check = await conn.run(f"test -x {STATION_AIRPLAY_INIT_PATH} && echo 'ok'", check=False)
                ap_autostart = (ap_autostart_check.exit_status == 0 and ap_autostart_check.stdout.strip() == "ok")

                # 5. Проверка статуса station-ducking и автозапуска
                duck_check = await conn.run("ps | grep -v grep | grep ducking", check=False)
                duck_running = (duck_check.exit_status == 0 and bool(duck_check.stdout.strip()))

                duck_autostart_check = await conn.run(f"test -x {STATION_DUCKING_INIT_PATH} && echo 'ok'", check=False)
                duck_autostart = (duck_autostart_check.exit_status == 0 and duck_autostart_check.stdout.strip() == "ok")

                has_update = (installed_ver != bundled_ver)

                return {
                    "success": True,
                    "ip": ip,
                    "port": port,
                    "installed_version": installed_ver,
                    "bundled_version": bundled_ver,
                    "is_native": is_native or "-porfiriy" in installed_ver,
                    "has_update": has_update,
                    "squeezelite_running": sq_running,
                    "squeezelite_autostart": sq_autostart,
                    "airplay_running": ap_running,
                    "airplay_autostart": ap_autostart,
                    "ducking_running": duck_running,
                    "ducking_autostart": duck_autostart,
                    "bundled_info": bundled
                }
        except Exception as e:
            logger.error(f"[QUASAR] Error getting station version from {ip}:{port}: {e}")
            return {
                "success": False,
                "error": str(e),
                "bundled_version": bundled_ver
            }

    async def ensure_squeezelite_installed(self, conn) -> bool:
        """
        Гарантирует установку и актуальность службы Squeezelite и скрипта автозапуска в /etc/init.d/S97squeezelite.
        Устраняет зависания при загрузке (фоновый запуск) и ложное срабатывание на пустой pidfile.
        """
        try:
            # 1. Создаем директорию если не существует
            await conn.run(f"mkdir -p {SQUEEZELITE_DIR}", check=False)

            # 2. Считываем и заливаем надежный скрипт службы service
            service_src = os.path.join(STATION_DIST_DIR, "squeezelite_service.sh")
            service_content = ""
            if os.path.exists(service_src):
                with open(service_src, "r", encoding="utf-8") as f:
                    service_content = f.read()

            if service_content:
                # Экранируем одинарные кавычки для безопасного echo
                escaped = service_content.replace("'", "'\\''")
                await conn.run(f"cat << 'EOF' > {SQUEEZELITE_SERVICE_PATH}\n{service_content}\nEOF\nchmod 755 {SQUEEZELITE_SERVICE_PATH}", check=False)
            else:
                await conn.run(f"chmod 755 {SQUEEZELITE_SERVICE_PATH} 2>/dev/null || true", check=False)

            # 3. Проверяем наличие неблокирующего автозапуска в /etc/init.d/S97squeezelite
            hook_check = await conn.run(f"grep -q 'Porfiriy & Music Assistant' {SQUEEZELITE_INIT_PATH} 2>/dev/null && echo 'ok'", check=False)
            if hook_check.exit_status != 0 or hook_check.stdout.strip() != "ok":
                init_src = os.path.join(STATION_DIST_DIR, "squeezelite_init.sh")
                init_content = ""
                if os.path.exists(init_src):
                    with open(init_src, "r", encoding="utf-8") as f:
                        init_content = f.read()

                if not init_content:
                    init_content = (
                        "#!/bin/sh\n"
                        "# Squeezelite autostart hook for Porfiriy & Music Assistant\n"
                        "case \"${1:-start}\" in\n"
                        "    start)\n"
                        "        sh /data/opt/squeezelite/service start >/dev/null 2>&1 &\n"
                        "        ;;\n"
                        "    stop)\n"
                        "        sh /data/opt/squeezelite/service stop\n"
                        "        ;;\n"
                        "    restart)\n"
                        "        sh /data/opt/squeezelite/service restart\n"
                        "        ;;\n"
                        "    status)\n"
                        "        exec sh /data/opt/squeezelite/service status\n"
                        "        ;;\n"
                        "    *)\n"
                        "        exec sh /data/opt/squeezelite/service \"$@\"\n"
                        "        ;;\n"
                        "esac\n"
                    )

                logger.info("[QUASAR] Installing non-blocking Squeezelite autostart hook to /etc/init.d/S97squeezelite...")
                install_cmd = (
                    "mount -o remount,rw / && "
                    f"cat << 'EOF' > {SQUEEZELITE_INIT_PATH}\n{init_content}\nEOF\n"
                    f"chmod 755 {SQUEEZELITE_INIT_PATH} && "
                    "sync && "
                    "mount -o remount,ro /"
                )
                await conn.run(install_cmd, check=False)

            return True
        except Exception as e:
            logger.warning(f"[QUASAR] Could not ensure squeezelite installed: {e}")
            return False

    async def control_squeezelite(self, ip: str, port: int = DEFAULT_SSH_PORT, action: str = "restart") -> Dict[str, Any]:
        """
        Управляет службой Squeezelite (start, stop, restart, status) на Станции по SSH.
        Перед выполнением команды автоматически гарантирует целостность службы и автозапуска.
        """
        if not ip or ip == "---":
            return {"error": "IP-адрес колонки не указан"}

        action = action.strip().lower()
        if action not in ["start", "stop", "restart", "status"]:
            return {"error": f"Неизвестное действие: {action}"}

        try:
            async with await self._open_ssh_connection(ip, port) as conn:
                # 1. Проверяем и обновляем скрипты службы при необходимости
                await self.ensure_squeezelite_installed(conn)

                # 2. Выполняем команду управления
                cmd = f"sh {SQUEEZELITE_SERVICE_PATH} {action}"
                res = await conn.run(cmd, check=False)
                out = res.stdout.strip()
                err = res.stderr.strip()

                # 3. Проверяем итоговый статус процесса
                await asyncio.sleep(0.5)
                sq_check = await conn.run("ps | grep -v grep | grep squeezelite", check=False)
                sq_running = (sq_check.exit_status == 0 and bool(sq_check.stdout.strip()))

                msg = out or err or f"Команда {action} выполнена"
                logger.info(f"[QUASAR] Squeezelite {action} on {ip}:{port} -> running={sq_running} ({msg})")

                return {
                    "success": True,
                    "action": action,
                    "squeezelite_running": sq_running,
                    "message": msg,
                    "output": out
                }
        except Exception as e:
            logger.error(f"[QUASAR] Error executing Squeezelite {action} on {ip}:{port}: {e}")
            return {"success": False, "error": str(e), "squeezelite_running": False}

    async def ensure_station_airplay_installed(self, conn) -> bool:
        """
        Гарантирует установку и актуальность службы Station AirPlay и скрипта автозапуска в /etc/init.d/S98station-airplay.
        """
        try:
            # 1. Проверяем наличие каталога
            dir_check = await conn.run(f"test -d {STATION_AIRPLAY_DIR} && echo 'ok'", check=False)
            if dir_check.exit_status != 0 or dir_check.stdout.strip() != "ok":
                return False

            # 2. Считываем и заливаем надежный скрипт службы service
            service_src = os.path.join(STATION_DIST_DIR, "station_airplay_service.sh")
            service_content = ""
            if os.path.exists(service_src):
                with open(service_src, "r", encoding="utf-8") as f:
                    service_content = f.read()

            if service_content:
                await conn.run(f"cat << 'EOF' > {STATION_AIRPLAY_SERVICE_PATH}\n{service_content}\nEOF\nchmod 755 {STATION_AIRPLAY_SERVICE_PATH}", check=False)
            else:
                await conn.run(f"chmod 755 {STATION_AIRPLAY_SERVICE_PATH} 2>/dev/null || true", check=False)

            # 3. Проверяем наличие неблокирующего автозапуска в /etc/init.d/S98station-airplay
            hook_check = await conn.run(f"grep -q 'Station AirPlay autostart hook' {STATION_AIRPLAY_INIT_PATH} 2>/dev/null && echo 'ok'", check=False)
            if hook_check.exit_status != 0 or hook_check.stdout.strip() != "ok":
                init_src = os.path.join(STATION_DIST_DIR, "station_airplay_init.sh")
                init_content = ""
                if os.path.exists(init_src):
                    with open(init_src, "r", encoding="utf-8") as f:
                        init_content = f.read()

                if not init_content:
                    init_content = (
                        "#!/bin/sh\n"
                        "# Station AirPlay autostart hook for Porfiriy & Music Assistant\n"
                        "case \"${1:-start}\" in\n"
                        "    start)\n"
                        "        sh /data/opt/station-airplay/service start >/dev/null 2>&1 &\n"
                        "        ;;\n"
                        "    stop)\n"
                        "        sh /data/opt/station-airplay/service stop\n"
                        "        ;;\n"
                        "    restart)\n"
                        "        sh /data/opt/station-airplay/service restart\n"
                        "        ;;\n"
                        "    status)\n"
                        "        exec sh /data/opt/station-airplay/service status\n"
                        "        ;;\n"
                        "    *)\n"
                        "        exec sh /data/opt/station-airplay/service \"$@\"\n"
                        "        ;;\n"
                        "esac\n"
                    )

                logger.info("[QUASAR] Installing non-blocking AirPlay autostart hook to /etc/init.d/S98station-airplay...")
                install_cmd = (
                    "mount -o remount,rw / && "
                    f"cat << 'EOF' > {STATION_AIRPLAY_INIT_PATH}\n{init_content}\nEOF\n"
                    f"chmod 755 {STATION_AIRPLAY_INIT_PATH} && "
                    "sync && "
                    "mount -o remount,ro /"
                )
                await conn.run(install_cmd, check=False)

            return True
        except Exception as e:
            logger.warning(f"[QUASAR] Could not ensure station airplay installed: {e}")
            return False

    async def control_station_airplay(self, ip: str, port: int = DEFAULT_SSH_PORT, action: str = "restart") -> Dict[str, Any]:
        """
        Управляет службой Station AirPlay (start, stop, restart, status) на Станции по SSH.
        """
        if not ip or ip == "---":
            return {"error": "IP-адрес колонки не указан"}

        action = action.strip().lower()
        if action not in ["start", "stop", "restart", "status"]:
            return {"error": f"Неизвестное действие: {action}"}

        try:
            async with await self._open_ssh_connection(ip, port) as conn:
                await self.ensure_station_airplay_installed(conn)

                cmd = f"sh {STATION_AIRPLAY_SERVICE_PATH} {action}"
                res = await conn.run(cmd, check=False)
                out = res.stdout.strip()
                err = res.stderr.strip()

                await asyncio.sleep(0.5)
                ap_check = await conn.run("ps | grep -v grep | grep station-airplay", check=False)
                ap_running = (ap_check.exit_status == 0 and bool(ap_check.stdout.strip()))

                msg = out or err or f"Команда {action} выполнена"
                logger.info(f"[QUASAR] AirPlay {action} on {ip}:{port} -> running={ap_running} ({msg})")

                return {
                    "success": True,
                    "action": action,
                    "airplay_running": ap_running,
                    "message": msg,
                    "output": out
                }
        except Exception as e:
            logger.error(f"[QUASAR] Error executing AirPlay {action} on {ip}:{port}: {e}")
            return {"success": False, "error": str(e), "airplay_running": False}

    async def ensure_asound_configured(self, conn) -> bool:
        """
        Гарантирует правильную конфигурацию ALSA (/etc/asound.conf):
        1. Наличие ctl.soft_volume { type hw card 0 }
        2. Маршрутизацию pcm.!default через soft_volume (чтобы все аудиопотоки подчинялись Master громкости и авто-дакингу)
        """
        try:
            check = await conn.run("grep -q 'ctl.soft_volume' /etc/asound.conf 2>/dev/null && grep -q 'playback.pcm \"soft_volume\"' /etc/asound.conf 2>/dev/null && echo 'ok'", check=False)
            if check.exit_status == 0 and check.stdout.strip() == "ok":
                return True

            logger.info("[QUASAR] Configuring ALSA /etc/asound.conf for Master volume & Auto-Ducking...")
            asound_cmd = (
                "mount -o remount,rw / && "
                "sed -i 's/playback.pcm \"dmix_spk_plug\"/playback.pcm \"soft_volume\"/' /etc/asound.conf && "
                "grep -q 'ctl.soft_volume' /etc/asound.conf || echo -e '\\nctl.soft_volume {\\n  type hw\\n  card 0\\n}\\n' >> /etc/asound.conf && "
                "sync && "
                "mount -o remount,ro /"
            )
            await conn.run(asound_cmd, check=False)
            return True
        except Exception as e:
            logger.warning(f"[QUASAR] Could not configure ALSA asound.conf: {e}")
            return False

    async def ensure_set_volume_wrapped(self, conn) -> bool:
        """
        Гарантирует установку обертки /system/vendor/quasar/set_volume.sh для координации громкости с авто-дакингом.
        """
        try:
            check = await conn.run(f"grep -q 'station_ducking_saved_vol' {SET_VOLUME_PATH} 2>/dev/null && echo 'ok'", check=False)
            if check.exit_status == 0 and check.stdout.strip() == "ok":
                return True

            set_vol_src = os.path.join(STATION_DIST_DIR, "set_volume.sh")
            content = ""
            if os.path.exists(set_vol_src):
                with open(set_vol_src, "r", encoding="utf-8") as f:
                    content = f.read()

            if not content:
                content = (
                    "#!/bin/sh\n"
                    "target=\"${1:-50}\"\n"
                    "case \"$target\" in *[!0-9]*) target=50 ;; esac\n"
                    "echo \"$target\" > /tmp/quasar_volume\n"
                    "if [ -f /tmp/station_ducking_active ]; then\n"
                    "    echo \"$target\" > /tmp/station_ducking_saved_vol\n"
                    "    echo \"$(date '+%Y/%m/%d %H:%M:%S') [SET_VOL] Ducking active: queued restoration to ${target}%\" >> /data/opt/station-ducking/ducking.log 2>/dev/null || true\n"
                    "else\n"
                    "    amixer set 'Master' \"${target}%\" >/dev/null 2>&1 || true\n"
                    "    echo \"$(date '+%Y/%m/%d %H:%M:%S') [SET_VOL] Master set to ${target}%\" >> /data/opt/station-ducking/ducking.log 2>/dev/null || true\n"
                    "fi\n"
                )

            logger.info("[QUASAR] Installing set_volume.sh wrapper to /system/vendor/quasar/set_volume.sh...")
            install_cmd = (
                "mount -o remount,rw / && "
                f"cat << 'EOF' > {SET_VOLUME_PATH}\n{content}\nEOF\n"
                f"chmod 755 {SET_VOLUME_PATH} && "
                "sync && "
                "mount -o remount,ro /"
            )
            await conn.run(install_cmd, check=False)
            return True
        except Exception as e:
            logger.warning(f"[QUASAR] Could not wrap set_volume.sh: {e}")
            return False

    async def ensure_station_ducking_installed(self, conn) -> bool:
        """
        Гарантирует установку и актуальность службы Station Ducking и скрипта автозапуска в /etc/init.d/S96ducking.
        """
        try:
            await conn.run(f"mkdir -p {STATION_DUCKING_DIR}", check=False)

            service_src = os.path.join(STATION_DIST_DIR, "ducking_service.sh")
            service_content = ""
            if os.path.exists(service_src):
                with open(service_src, "r", encoding="utf-8") as f:
                    service_content = f.read()

            if service_content:
                await conn.run(f"cat << 'EOF' > {STATION_DUCKING_SERVICE_PATH}\n{service_content}\nEOF\nchmod 755 {STATION_DUCKING_SERVICE_PATH}", check=False)
            else:
                await conn.run(f"chmod 755 {STATION_DUCKING_SERVICE_PATH} 2>/dev/null || true", check=False)

            hook_check = await conn.run(f"grep -q 'Station Ducking autostart hook' {STATION_DUCKING_INIT_PATH} 2>/dev/null && echo 'ok'", check=False)
            if hook_check.exit_status != 0 or hook_check.stdout.strip() != "ok":
                init_src = os.path.join(STATION_DIST_DIR, "ducking_init.sh")
                init_content = ""
                if os.path.exists(init_src):
                    with open(init_src, "r", encoding="utf-8") as f:
                        init_content = f.read()

                if not init_content:
                    init_content = (
                        "#!/bin/sh\n"
                        "# Station Ducking autostart hook for Porfiriy & Music Assistant\n"
                        "case \"${1:-start}\" in\n"
                        "    start)\n"
                        "        sh /data/opt/station-ducking/service start >/dev/null 2>&1 &\n"
                        "        ;;\n"
                        "    stop)\n"
                        "        sh /data/opt/station-ducking/service stop\n"
                        "        ;;\n"
                        "    restart)\n"
                        "        sh /data/opt/station-ducking/service restart\n"
                        "        ;;\n"
                        "    status)\n"
                        "        exec sh /data/opt/station-ducking/service status\n"
                        "        ;;\n"
                        "    *)\n"
                        "        exec sh /data/opt/station-ducking/service \"$@\"\n"
                        "        ;;\n"
                        "esac\n"
                    )

                logger.info("[QUASAR] Installing non-blocking Ducking autostart hook to /etc/init.d/S96ducking...")
                install_cmd = (
                    "mount -o remount,rw / && "
                    f"cat << 'EOF' > {STATION_DUCKING_INIT_PATH}\n{init_content}\nEOF\n"
                    f"chmod 755 {STATION_DUCKING_INIT_PATH} && "
                    "sync && "
                    "mount -o remount,ro /"
                )
                await conn.run(install_cmd, check=False)

            return True
        except Exception as e:
            logger.warning(f"[QUASAR] Could not ensure station ducking installed: {e}")
            return False

    async def control_station_ducking(self, ip: str, port: int = DEFAULT_SSH_PORT, action: str = "restart") -> Dict[str, Any]:
        """
        Управляет службой Station Ducking (start, stop, restart, status) на Станции по SSH.
        """
        if not ip or ip == "---":
            return {"error": "IP-адрес колонки не указан"}

        action = action.strip().lower()
        if action not in ["start", "stop", "restart", "status"]:
            return {"error": f"Неизвестное действие: {action}"}

        try:
            async with await self._open_ssh_connection(ip, port) as conn:
                await self.ensure_station_ducking_installed(conn)

                cmd = f"sh {STATION_DUCKING_SERVICE_PATH} {action}"
                res = await conn.run(cmd, check=False)
                out = res.stdout.strip()
                err = res.stderr.strip()

                await asyncio.sleep(0.5)
                duck_check = await conn.run("ps | grep -v grep | grep ducking", check=False)
                duck_running = (duck_check.exit_status == 0 and bool(duck_check.stdout.strip()))

                msg = out or err or f"Команда {action} выполнена"
                logger.info(f"[QUASAR] Ducking {action} on {ip}:{port} -> running={duck_running} ({msg})")

                return {
                    "success": True,
                    "action": action,
                    "ducking_running": duck_running,
                    "message": msg,
                    "output": out
                }
        except Exception as e:
            logger.error(f"[QUASAR] Error executing Ducking {action} on {ip}:{port}: {e}")
            return {"success": False, "error": str(e), "ducking_running": False}

    async def update_station(self, ip: str, port: int = DEFAULT_SSH_PORT) -> Dict[str, Any]:
        """
        Единое обновление станции (Quasar Bridge, Squeezelite, Station AirPlay, Auto-Ducking, ALSA Master Volume sync):
        - Записывает манифест версии version.json
        - Загружает обновленный бинарник из station_dist/bin/ (если присутствует)
        - Конфигурирует ALSA asound.conf (soft_volume)
        - Устанавливает обертку set_volume.sh
        - Обновляет службу и автозапуск Squeezelite
        - Обновляет службу и автозапуск Station AirPlay
        - Обновляет службу и автозапуск Station Ducking
        - Выполняет валидацию конфигурации -check-config
        - Перезапускает Quasar Bridge, Squeezelite, Station AirPlay и Station Ducking
        """
        if not ip or ip == "---":
            return {"error": "IP-адрес колонки не указан"}

        bundled = self.get_bundled_version_info()
        bundled_ver = bundled.get("version", "1.2.4-porfiriy.4")

        try:
            async with await self._open_ssh_connection(ip, port) as conn:
                # 1. Проверяем наличие каталога Quasar Bridge
                dir_check = await conn.run(f"test -d {QUASAR_DIR} && echo 'ok'", check=False)
                if dir_check.exit_status != 0 or dir_check.stdout.strip() != "ok":
                    return {"error": f"Каталог {QUASAR_DIR} не найден на Станции {ip}"}

                # 2. Если в аддоне есть свежий бинарник, передаем его по SFTP/SSH
                bin_src = os.path.join(STATION_DIST_DIR, "bin", "quasar-bridge")
                if os.path.exists(bin_src) and os.path.getsize(bin_src) > 1000:
                    logger.info(f"[QUASAR] Uploading updated binary to {ip}...")
                    async with conn.start_sftp_client() as sftp:
                        await sftp.put(bin_src, f"{QUASAR_DIR}/app/bin/quasar-bridge")
                    await conn.run(f"chmod +x {QUASAR_DIR}/app/bin/quasar-bridge", check=False)

                # 3. Записываем version.json на колонку
                ver_json_str = json.dumps(bundled, indent=2, ensure_ascii=False)
                escaped_ver = ver_json_str.replace("'", "'\\''")
                await conn.run(f"echo '{escaped_ver}' > {QUASAR_DIR}/version.json", check=False)

                # 4. Гарантируем ALSA маршрутизацию soft_volume и Master громкости
                await self.ensure_asound_configured(conn)

                # 5. Устанавливаем обертку set_volume.sh для координации с авто-дакингом
                await self.ensure_set_volume_wrapped(conn)

                # 6. Обновляем и страхуем службу Squeezelite
                await self.ensure_squeezelite_installed(conn)

                # 7. Обновляем и страхуем службу Station AirPlay
                await self.ensure_station_airplay_installed(conn)

                # 8. Обновляем и страхуем службу Station Ducking
                await self.ensure_station_ducking_installed(conn)

                # 9. Проверяем конфигурацию Quasar перед перезапуском
                check_cmd = f"cd {QUASAR_DIR} && set -a && . ./runtime.env && set +a && ./app/bin/quasar-bridge -config {QUASAR_CONFIG_PATH} -check-config"
                check_res = await conn.run(check_cmd, check=False)
                if check_res.exit_status != 0:
                    return {
                        "error": f"Конфигурация не прошла валидацию: {check_res.stderr.strip() or check_res.stdout.strip()}"
                    }

                # 10. Перезапускаем Quasar Bridge
                restart_cmd = f"sh {QUASAR_DIR}/app/control.sh restart"
                restart_res = await conn.run(restart_cmd, check=False)
                if restart_res.exit_status != 0:
                    return {
                        "error": f"Ошибка перезапуска Quasar Bridge: {restart_res.stderr.strip()}"
                    }

                # 11. Перезапускаем Squeezelite
                await conn.run(f"sh {SQUEEZELITE_SERVICE_PATH} restart", check=False)
                await asyncio.sleep(0.5)
                sq_check = await conn.run("ps | grep -v grep | grep squeezelite", check=False)
                sq_running = (sq_check.exit_status == 0 and bool(sq_check.stdout.strip()))

                # 12. Перезапускаем Station AirPlay
                await conn.run(f"sh {STATION_AIRPLAY_SERVICE_PATH} restart", check=False)
                await asyncio.sleep(0.5)
                ap_check = await conn.run("ps | grep -v grep | grep station-airplay", check=False)
                ap_running = (ap_check.exit_status == 0 and bool(ap_check.stdout.strip()))

                # 13. Перезапускаем Station Ducking
                await conn.run(f"sh {STATION_DUCKING_SERVICE_PATH} restart", check=False)
                await asyncio.sleep(0.5)
                duck_check = await conn.run("ps | grep -v grep | grep ducking", check=False)
                duck_running = (duck_check.exit_status == 0 and bool(duck_check.stdout.strip()))

                logger.info(f"[QUASAR] Successfully updated station {ip} to {bundled_ver} (Squeezelite running={sq_running}, AirPlay running={ap_running}, Ducking running={duck_running})")
                return {
                    "success": True,
                    "message": f"Единый пакет Яндекс Станции успешно обновлен до {bundled_ver}",
                    "version": bundled_ver,
                    "squeezelite_running": sq_running,
                    "airplay_running": ap_running,
                    "ducking_running": duck_running,
                    "output": restart_res.stdout.strip()
                }
        except Exception as e:
            logger.error(f"[QUASAR] Error updating station {ip}:{port}: {e}")
            return {"error": str(e)}

