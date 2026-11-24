import os
import logging
import json
import asyncio
from typing import Dict, Any, Optional

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
ALICE_WATCHER_DIR = "/data/opt/alice-watcher"
ALICE_WATCHER_SERVICE_PATH = "/data/opt/alice-watcher/service"
ALICE_WATCHER_INIT_PATH = "/etc/init.d/S96alice-watcher"
STATIOND_DIR = "/data/opt/porfiriy-stationd"
STATIOND_SERVICE_PATH = "/data/opt/porfiriy-stationd/service"
STATIOND_INIT_PATH = "/etc/init.d/S99porfiriy-stationd"
STATIOND_WIFI_SERVERS_PATH = "/data/opt/porfiriy-stationd/wifi_servers.json"
WPA_SUPPLICANT_PATH = "/bkupgrade/data/wpa_supplicant.conf"
DISABLE_ALICE_FLAG = "/data/opt/disable_alice"
SET_VOLUME_PATH = "/system/vendor/quasar/set_volume.sh"
ASOUND_CONF_PATH = "/etc/asound.conf"
STATION_DIST_DIR = os.path.join(os.path.dirname(__file__), "station_dist")

class QuasarManager:
    """Управление конфигурацией и маршрутизацией Quasar Bridge на Яндекс Станции через SSH."""

    def __init__(self):
        self._vol_tasks: Dict[str, asyncio.Task] = {}
        self._target_vols: Dict[str, int] = {}

    def set_station_hw_volume(self, dev: Dict[str, Any], volume_percent: int) -> None:
        """
        Запускает фоновую неблокирующую задачу синхронизации аппаратной громкости
        ALSA Master и /tmp/quasar_volume на Станции с дебаунсингом.
        """
        if not HAS_ASYNCSSH or not isinstance(dev, dict):
            return
        mac = (dev.get("mac") or "").strip().lower()
        if not mac:
            return

        target = max(0, min(100, int(volume_percent)))
        self._target_vols[mac] = target

        existing = self._vol_tasks.get(mac)
        if existing is None or existing.done():
            self._vol_tasks[mac] = asyncio.create_task(self._station_volume_worker(dev, mac))

    async def _station_volume_worker(self, dev: Dict[str, Any], mac: str) -> None:
        """Рабочий воркер установки аппаратной громкости на Станции через SSH."""
        try:
            cfg = dev.get("config", {}) if isinstance(dev, dict) else {}
            ip = (dev.get("ip") or cfg.get("ip") or "").strip()
            port = int(cfg.get("ssh_port", DEFAULT_SSH_PORT))
            if not ip or ip == "---" or ip.startswith("127."):
                return

            last_applied = None
            while True:
                target = self._target_vols.get(mac)
                if target is None or target == last_applied:
                    break

                vendor_step = max(1, min(10, int(round(target / 10.0))))
                cmd = (
                    f"echo '{vendor_step}' > /data/quasar/data/last_vendor_volume 2>/dev/null || true; "
                    f"echo '{vendor_step}' > /bkupgrade/data/quasar/data/last_vendor_volume 2>/dev/null || true; "
                    f"if [ -x {SET_VOLUME_PATH} ]; then "
                    f"sh {SET_VOLUME_PATH} {target}; "
                    f"else "
                    f"echo '{target}' > /tmp/quasar_volume; "
                    f"amixer set 'Master' '{target}%' >/dev/null 2>&1 || true; "
                    f"fi"
                )
                success = False
                for attempt in range(2):
                    try:
                        async with await self._open_ssh_connection(ip, port, timeout=3.0) as conn:
                            await conn.run(cmd, check=False)
                        last_applied = target
                        logger.info(f"[QUASAR] Synced hardware voice volume to {target}% on station {ip}:{port} (mac={mac})")
                        success = True
                        break
                    except Exception as e_ssh:
                        if attempt == 0:
                            await asyncio.sleep(0.5)
                        else:
                            logger.warning(f"[QUASAR] Error setting hardware volume on station {ip}:{port}: {e_ssh}")
                if not success:
                    break

                # Если пока выполнялась команда, целевой уровень не менялся — завершаем
                if self._target_vols.get(mac) == target:
                    break
        except Exception as e:
            logger.warning(f"[QUASAR] Hardware volume worker error for {mac}: {e}")

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
            "version": "1.2.4-porfiriy.19",
            "name": "Quasar Bridge Porfiriy Edition",
            "target": "yandex_station_2"
        }

    async def get_version_info(self, ip: str, port: int = DEFAULT_SSH_PORT) -> Dict[str, Any]:
        """Опрашивает версию Quasar Bridge, установленную на колонке, и сравнивает с бандлом аддона."""
        if not ip or ip == "---":
            return {"error": "IP-адрес колонки не указан"}

        bundled = self.get_bundled_version_info()
        bundled_ver = bundled.get("version", "1.2.4-porfiriy.19")

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

                # 2.1 Читаем текущий привязанный IP сервера Porfiriy
                host_check = await conn.run("cat /data/opt/porfiriy_host 2>/dev/null", check=False)
                bound_server_ip = host_check.stdout.strip() if host_check.exit_status == 0 else ""

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
                    "bound_server_ip": bound_server_ip,
                    "bundled_info": bundled
                }
        except Exception as e:
            logger.error(f"[QUASAR] Error getting station version from {ip}:{port}: {e}")
            return {
                "success": False,
                "error": str(e),
                "bundled_version": bundled_ver
            }

    async def _is_process_running(self, conn, pattern: str) -> bool:
        """Проверяет, запущен ли процесс на Станции по имени."""
        res = await conn.run(f"ps | grep -v grep | grep {pattern}", check=False)
        return res.exit_status == 0 and bool(res.stdout.strip())

    async def _install_station_service(
        self,
        conn,
        *,
        name: str,
        target_dir: str,
        service_path: str,
        init_path: str,
        service_dist_file: str,
        init_dist_file: str,
        hook_marker: str,
        require_existing_dir: bool = False,
        post_service_cmd: Optional[str] = None,
    ) -> bool:
        """Единый установщик сервисов Станции (squeezelite, station-airplay, alice-watcher)."""
        try:
            if require_existing_dir:
                dir_check = await conn.run(f"test -d {target_dir} && echo 'ok'", check=False)
                if dir_check.exit_status != 0 or dir_check.stdout.strip() != "ok":
                    return False
            else:
                await conn.run(f"mkdir -p {target_dir}", check=False)

            service_src = os.path.join(STATION_DIST_DIR, service_dist_file)
            if os.path.exists(service_src):
                with open(service_src, "r", encoding="utf-8") as f:
                    service_content = f.read()
                await conn.run(
                    f"cat << 'EOF' > {service_path}\n{service_content}\nEOF\nchmod 755 {service_path}",
                    check=False,
                )
            else:
                await conn.run(f"chmod 755 {service_path} 2>/dev/null || true", check=False)

            if post_service_cmd:
                await conn.run(post_service_cmd, check=False)

            hook_check = await conn.run(f"grep -q '{hook_marker}' {init_path} 2>/dev/null && echo 'ok'", check=False)
            if hook_check.exit_status != 0 or hook_check.stdout.strip() != "ok":
                init_src = os.path.join(STATION_DIST_DIR, init_dist_file)
                if os.path.exists(init_src):
                    with open(init_src, "r", encoding="utf-8") as f:
                        init_content = f.read()
                    logger.info(f"[QUASAR] Installing non-blocking {name} autostart hook to {init_path}...")
                    install_cmd = (
                        "mount -o remount,rw / && "
                        f"cat << 'EOF' > {init_path}\n{init_content}\nEOF\n"
                        f"chmod 755 {init_path} && "
                        "sync && "
                        "mount -o remount,ro /"
                    )
                    await conn.run(install_cmd, check=False)
            return True
        except Exception as e:
            logger.warning(f"[QUASAR] Could not ensure {name} installed: {e}")
            return False

    async def _control_station_service(
        self,
        ip: str,
        port: int,
        action: str,
        *,
        label: str,
        service_path: str,
        proc_pattern: str,
        running_key: str,
        ensure_fn,
    ) -> Dict[str, Any]:
        """Единый контроллер управления службами Станции (start, stop, restart, status) по SSH."""
        if not ip or ip == "---":
            return {"error": "IP-адрес колонки не указан"}

        action = action.strip().lower()
        if action not in ("start", "stop", "restart", "status"):
            return {"error": f"Неизвестное действие: {action}"}

        try:
            async with await self._open_ssh_connection(ip, port) as conn:
                await ensure_fn(conn)

                res = await conn.run(f"sh {service_path} {action}", check=False)
                out = res.stdout.strip()
                err = res.stderr.strip()

                await asyncio.sleep(0.5)
                is_running = await self._is_process_running(conn, proc_pattern)

                msg = out or err or f"Команда {action} выполнена"
                logger.info(f"[QUASAR] {label} {action} on {ip}:{port} -> running={is_running} ({msg})")

                return {
                    "success": True,
                    "action": action,
                    running_key: is_running,
                    "message": msg,
                    "output": out,
                }
        except Exception as e:
            logger.error(f"[QUASAR] Error executing {label} {action} on {ip}:{port}: {e}")
            return {"success": False, "error": str(e), running_key: False}

    async def ensure_squeezelite_installed(self, conn) -> bool:
        """Гарантирует установку и актуальность службы Squeezelite и скрипта автозапуска."""
        return await self._install_station_service(
            conn,
            name="Squeezelite",
            target_dir=SQUEEZELITE_DIR,
            service_path=SQUEEZELITE_SERVICE_PATH,
            init_path=SQUEEZELITE_INIT_PATH,
            service_dist_file="squeezelite_service.sh",
            init_dist_file="squeezelite_init.sh",
            hook_marker="Porfiriy & Music Assistant",
        )

    async def control_squeezelite(self, ip: str, port: int = DEFAULT_SSH_PORT, action: str = "restart") -> Dict[str, Any]:
        """Управляет службой Squeezelite (start, stop, restart, status) на Станции по SSH."""
        return await self._control_station_service(
            ip, port, action,
            label="Squeezelite",
            service_path=SQUEEZELITE_SERVICE_PATH,
            proc_pattern="squeezelite",
            running_key="squeezelite_running",
            ensure_fn=self.ensure_squeezelite_installed,
        )

    async def ensure_station_airplay_installed(self, conn) -> bool:
        """Гарантирует установку и актуальность службы Station AirPlay и скрипта автозапуска."""
        return await self._install_station_service(
            conn,
            name="AirPlay",
            target_dir=STATION_AIRPLAY_DIR,
            service_path=STATION_AIRPLAY_SERVICE_PATH,
            init_path=STATION_AIRPLAY_INIT_PATH,
            service_dist_file="station_airplay_service.sh",
            init_dist_file="station_airplay_init.sh",
            hook_marker="Station AirPlay autostart hook",
            require_existing_dir=True,
            post_service_cmd=f"echo 'music_duck' > {STATION_AIRPLAY_DIR}/device",
        )

    async def control_station_airplay(self, ip: str, port: int = DEFAULT_SSH_PORT, action: str = "restart") -> Dict[str, Any]:
        """Управляет службой Station AirPlay (start, stop, restart, status) на Станции по SSH."""
        return await self._control_station_service(
            ip, port, action,
            label="AirPlay",
            service_path=STATION_AIRPLAY_SERVICE_PATH,
            proc_pattern="station-airplay",
            running_key="airplay_running",
            ensure_fn=self.ensure_station_airplay_installed,
        )

    async def stop_station_airplay_if_playing(self, ip: str, port: int = DEFAULT_SSH_PORT) -> bool:
        """
        Проверяет, выводит ли сейчас AirPlay звук на Станции (station-pcm).
        Если да, мгновенно перезапускает службу Station AirPlay, закрывая сессию и ставя iPhone на паузу.
        """
        if not ip or ip == "---":
            return False

        try:
            async with await self._open_ssh_connection(ip, port) as conn:
                cmd = (
                    "if ps | grep -v grep | grep -q 'station-pcm'; then "
                    f"sh {STATION_AIRPLAY_SERVICE_PATH} restart >/dev/null 2>&1; echo 'stopped'; "
                    "else echo 'idle'; fi"
                )
                res = await asyncio.wait_for(conn.run(cmd, check=False), timeout=3.0)
                status = res.stdout.strip()
                if status == "stopped":
                    logger.info(f"[MUTUAL PLAYBACK] AirPlay active stream disconnected on {ip} to let Squeezelite play")
                    return True
                return False
        except Exception as e:
            logger.debug(f"[MUTUAL PLAYBACK] Could not check/stop station airplay on {ip}: {e}")
            return False

    async def _cleanup_legacy_volume_ducking(self, conn) -> None:
        """
        Останавливает и удаляет устаревший (pre-v0.0.135) сервис S96ducking со Станции.
        Актуальные asound.conf и set_volume.sh записываются далее в ensure_asound_music_duck / ensure_server_host_synced.
        """
        try:
            logger.info("[QUASAR] Cleaning up legacy S96ducking if present...")
            cleanup_cmd = (
                f"sh {STATION_DUCKING_SERVICE_PATH} stop 2>/dev/null || true; "
                "killall -9 ducking 2>/dev/null || true; "
                f"rm -rf {STATION_DUCKING_DIR} /tmp/station_ducking_*; "
                f"if [ -f {STATION_DUCKING_INIT_PATH} ]; then "
                f"mount -o remount,rw / 2>/dev/null && rm -f {STATION_DUCKING_INIT_PATH} && sync && mount -o remount,ro / 2>/dev/null || true; "
                "fi"
            )
            await conn.run(cleanup_cmd, check=False)
        except Exception as e:
            logger.warning(f"[QUASAR] Legacy volume/ducking cleanup warning: {e}")

    async def ensure_asound_music_duck(self, conn) -> bool:
        """
        Настраивает единую иерархию ALSA в /etc/asound.conf:
        Master (soft_volume) является единым корнем громкости всей колонки (Алиса, Порфирий, Squeezelite, AirPlay).
        music_duck (softvol) выводит в soft_volume и служит исключительно для дакинга музыки (-75%).
        """
        try:
            # 1. Исправляем экранирование, привязываем playback default и slave music_duck к soft_volume
            sync_asound_cmd = (
                "mount -o remount,rw / 2>/dev/null && ("
                "sed -i 's/\\\\\"/\"/g' /etc/asound.conf; "
                "sed -i 's/playback.pcm \"dmix_spk_plug\"/playback.pcm \"soft_volume\"/' /etc/asound.conf; "
                "sed -i '/pcm.music_duck/,/}/s/slave.pcm \"dmix_spk_plug\"/slave.pcm \"soft_volume\"/' /etc/asound.conf; "
                "sync; mount -o remount,ro / 2>/dev/null || true)"
            )
            await conn.run(sync_asound_cmd, check=False)

            check = await conn.run("grep -q 'pcm.music_duck' /etc/asound.conf 2>/dev/null && echo 'ok'", check=False)
            if check.exit_status == 0 and check.stdout.strip() == "ok":
                await conn.run("aplay -D music_duck -f S16_LE -c 2 -r 48000 -d 1 /dev/zero >/dev/null 2>&1 || true; amixer set 'Music' 100% >/dev/null 2>&1 || true", check=False)
                return True

            logger.info("[QUASAR] Configuring ALSA /etc/asound.conf with pcm.music_duck (softvol -> soft_volume)...")
            asound_cmd = (
                "mount -o remount,rw / 2>/dev/null && ("
                "cat << 'EOF' >> /etc/asound.conf\n"
                "\n"
                "ctl.Music {\n"
                "  type hw\n"
                "  card 0\n"
                "}\n"
                "\n"
                "pcm.music_duck {\n"
                "  type softvol\n"
                "  slave.pcm \"soft_volume\"\n"
                "  control {\n"
                "    name \"Music\"\n"
                "    card 0\n"
                "  }\n"
                "  min_dB -50.0\n"
                "  max_dB 0.0\n"
                "  hint {\n"
                "    show on\n"
                "    description \"Music with hardware ducking\"\n"
                "  }\n"
                "}\n"
                "EOF\n"
                "sync; mount -o remount,ro / 2>/dev/null || true); "
                "aplay -D music_duck -f S16_LE -c 2 -r 48000 -d 1 /dev/zero >/dev/null 2>&1 || true; "
                "amixer set 'Music' 100% >/dev/null 2>&1 || true"
            )
            await conn.run(asound_cmd, check=False)
            logger.info("[QUASAR] ALSA music_duck softvol configured successfully")
            return True
        except Exception as e:
            logger.warning(f"[QUASAR] Could not configure asound.conf music_duck: {e}")
            return False

    @staticmethod
    def _is_routable_lan_ip(host: str) -> bool:
        host = (host or "").strip()
        return bool(host) and not host.startswith(("127.", "172.17.", "172.30.", "169.254.")) and host not in ("localhost", "0.0.0.0", "::1")

    async def _resolve_porfiriy_host(self, conn) -> str:
        """Определяет реальный LAN IP хоста Porfiriy, игнорируя внутренний NAT Docker (172.30.x.x)."""
        try:
            ssh_res = await conn.run("echo \"$SSH_CLIENT\" | awk '{print $1}'", check=False)
            if ssh_res.exit_status == 0 and self._is_routable_lan_ip(ssh_res.stdout.strip()):
                return ssh_res.stdout.strip()
        except Exception:
            pass

        try:
            sockname = conn.get_extra_info('sockname')
            if sockname and self._is_routable_lan_ip(sockname[0]):
                return sockname[0]
        except Exception:
            pass

        try:
            sq_res = await conn.run(f"grep '^SQUEEZELITE_SERVER=' {SQUEEZELITE_DIR}/squeezelite.env 2>/dev/null | head -n 1 | cut -d= -f2- | tr -d '\"'\\'' ' | cut -d: -f1", check=False)
            if sq_res.exit_status == 0 and self._is_routable_lan_ip(sq_res.stdout.strip()):
                return sq_res.stdout.strip()
        except Exception:
            pass

        return ""

    async def ensure_server_host_synced(self, conn, server_ip: Optional[str] = None, openai_port: int = 8000) -> str:
        """
        Гарантирует синхронизацию и перепривязку актуального LAN IP сервера во всех env-файлах и конфигурациях Станции:
        1. /data/opt/porfiriy_host: адрес сервера для alice-watcher.
        2. /data/opt/squeezelite/squeezelite.env: SQUEEZELITE_SERVER="<server_ip>".
        3. /data/opt/quasar-bridge/runtime.env: LLM_BASE_URL="http://<server_ip>:<openai_port>/v1" и llm_base_url.
        4. /data/opt/quasar-bridge/config.json: секция llm.base_url / llm.url при наличии.
        """
        porfiriy_host = (server_ip or "").strip()
        if not porfiriy_host or not self._is_routable_lan_ip(porfiriy_host):
            porfiriy_host = await self._resolve_porfiriy_host(conn)

        if not porfiriy_host:
            logger.warning("[QUASAR] Could not resolve routable LAN IP for Porfiriy server")
            return ""

        try:
            logger.info(f"[QUASAR] Syncing server host IP {porfiriy_host}:{openai_port} to station env files...")

            # 1. /data/opt/porfiriy_host и /data/opt/porfiriy_port
            await conn.run(f"echo '{porfiriy_host}' > /data/opt/porfiriy_host && echo '{openai_port}' > /data/opt/porfiriy_port", check=False)

            # 2. /data/opt/squeezelite/squeezelite.env
            sq_cmd = (
                f"mkdir -p {SQUEEZELITE_DIR} && "
                f"if [ -f {SQUEEZELITE_DIR}/squeezelite.env ]; then "
                f"    if grep -q 'SQUEEZELITE_SERVER' {SQUEEZELITE_DIR}/squeezelite.env; then "
                f"        sed -i 's/^SQUEEZELITE_SERVER=.*/SQUEEZELITE_SERVER=\"{porfiriy_host}\"/' {SQUEEZELITE_DIR}/squeezelite.env; "
                f"    else "
                f"        echo 'SQUEEZELITE_SERVER=\"{porfiriy_host}\"' >> {SQUEEZELITE_DIR}/squeezelite.env; "
                f"    fi; "
                f"    if grep -q 'SQUEEZELITE_OUTPUT' {SQUEEZELITE_DIR}/squeezelite.env; then "
                f"        sed -i 's/^SQUEEZELITE_OUTPUT=.*/SQUEEZELITE_OUTPUT=\"music_duck\"/' {SQUEEZELITE_DIR}/squeezelite.env; "
                f"    else "
                f"        echo 'SQUEEZELITE_OUTPUT=\"music_duck\"' >> {SQUEEZELITE_DIR}/squeezelite.env; "
                f"    fi; "
                f"    if grep -q 'SQUEEZELITE_VOLUME_CONTROL' {SQUEEZELITE_DIR}/squeezelite.env; then "
                f"        sed -i 's/^SQUEEZELITE_VOLUME_CONTROL=.*/SQUEEZELITE_VOLUME_CONTROL=\"Master\"/' {SQUEEZELITE_DIR}/squeezelite.env; "
                f"    else "
                f"        echo 'SQUEEZELITE_VOLUME_CONTROL=\"Master\"' >> {SQUEEZELITE_DIR}/squeezelite.env; "
                f"    fi; "
                f"    if grep -q 'SQUEEZELITE_MIXER_DEVICE' {SQUEEZELITE_DIR}/squeezelite.env; then "
                f"        sed -i 's/^SQUEEZELITE_MIXER_DEVICE=.*/SQUEEZELITE_MIXER_DEVICE=\"default\"/' {SQUEEZELITE_DIR}/squeezelite.env; "
                f"    else "
                f"        echo 'SQUEEZELITE_MIXER_DEVICE=\"default\"' >> {SQUEEZELITE_DIR}/squeezelite.env; "
                f"    fi; "
                f"else "
                f"    echo 'SQUEEZELITE_SERVER=\"{porfiriy_host}\"' > {SQUEEZELITE_DIR}/squeezelite.env && "
                f"    echo 'SQUEEZELITE_OUTPUT=\"music_duck\"' >> {SQUEEZELITE_DIR}/squeezelite.env && "
                f"    echo 'SQUEEZELITE_VOLUME_CONTROL=\"Master\"' >> {SQUEEZELITE_DIR}/squeezelite.env && "
                f"    echo 'SQUEEZELITE_MIXER_DEVICE=\"default\"' >> {SQUEEZELITE_DIR}/squeezelite.env && "
                f"    echo 'SQUEEZELITE_NAME=\"Яндекс Станция\"' >> {SQUEEZELITE_DIR}/squeezelite.env; "
                f"fi"
            )
            await conn.run(sq_cmd, check=False)

            # 2.5 Гарантируем умный set_volume.sh (перевод шагов 1..10 в 10%..100% и синхронизация last_vendor_volume)
            set_vol_cmd = (
                f"mount -o remount,rw / 2>/dev/null && ("
                f"cat << 'EOF' > {SET_VOLUME_PATH}\n"
                "#!/bin/sh\n"
                'target="${1:-50}"\n'
                'case "$target" in *[!0-9]*|"") target=50 ;; esac\n'
                'if [ "$target" -ge 1 ] && [ "$target" -le 10 ]; then\n'
                '    step="$target"\n'
                '    target=$((target * 10))\n'
                'else\n'
                '    step=$((target / 10))\n'
                '    [ "$step" -lt 1 ] && step=1\n'
                '    [ "$step" -gt 10 ] && step=10\n'
                'fi\n'
                'echo "$step" > /data/quasar/data/last_vendor_volume 2>/dev/null || true\n'
                'echo "$step" > /bkupgrade/data/quasar/data/last_vendor_volume 2>/dev/null || true\n'
                'echo "$target" > /tmp/quasar_volume\n'
                "amixer set 'Master' \"${target}%\" >/dev/null 2>&1 || true\n"
                "EOF\n"
                f"chmod 755 {SET_VOLUME_PATH}; "
                "sync; mount -o remount,ro / 2>/dev/null || true)"
            )
            await conn.run(set_vol_cmd, check=False)

            # 3. /data/opt/quasar-bridge/runtime.env
            llm_url = f"http://{porfiriy_host}:{openai_port}/v1"
            rt_env_path = f"{QUASAR_DIR}/runtime.env"
            rt_cmd = (
                f"mkdir -p {QUASAR_DIR} && "
                f"if [ -f {rt_env_path} ]; then "
                f"    if grep -qi 'llm_base_url' {rt_env_path}; then "
                f"        sed -i 's|^[# ]*\\(export \\)\\?[Ll][Ll][Mm]_[Bb][Aa][Ss][Ee]_[Uu][Rr][Ll]=.*|\\1LLM_BASE_URL=\"{llm_url}\"|g' {rt_env_path}; "
                f"    else "
                f"        echo 'LLM_BASE_URL=\"{llm_url}\"' >> {rt_env_path}; "
                f"    fi; "
                f"    if grep -qi 'llm_api' {rt_env_path}; then "
                f"        sed -i 's|^[# ]*\\(export \\)\\?[Ll][Ll][Mm]_[Aa][Pp][Ii]=.*|\\1LLM_API=\"chat_completions\"|g' {rt_env_path}; "
                f"    else "
                f"        echo 'LLM_API=\"chat_completions\"' >> {rt_env_path}; "
                f"    fi; "
                f"    if grep -q 'QUASAR_BRIDGE_LLM_BASE_URL' {rt_env_path}; then "
                f"        sed -i 's|^[# ]*\\(export \\)\\?QUASAR_BRIDGE_LLM_BASE_URL=.*|\\1QUASAR_BRIDGE_LLM_BASE_URL=\"{llm_url}\"|g' {rt_env_path}; "
                f"    fi; "
                f"else "
                f"    echo 'LLM_BASE_URL=\"{llm_url}\"' > {rt_env_path} && "
                f"    echo 'llm_base_url=\"{llm_url}\"' >> {rt_env_path} && "
                f"    echo 'LLM_API=\"chat_completions\"' >> {rt_env_path} && "
                f"    echo 'llm_api=\"chat_completions\"' >> {rt_env_path}; "
                f"fi"
            )
            await conn.run(rt_cmd, check=False)

            # 4. /data/opt/quasar-bridge/config.json (если внутри есть секция llm / llm_base_url)
            cfg_check = await conn.run(f"cat {QUASAR_CONFIG_PATH} 2>/dev/null", check=False)
            if cfg_check.exit_status == 0 and cfg_check.stdout.strip():
                try:
                    c_data = json.loads(cfg_check.stdout.strip())
                    changed = False
                    if "llm" in c_data and isinstance(c_data["llm"], dict):
                        llm_obj = c_data["llm"]
                        if "base_url" in llm_obj and llm_obj["base_url"] != llm_url:
                            llm_obj["base_url"] = llm_url
                            changed = True
                        if "url" in llm_obj and llm_obj["url"] != llm_url:
                            llm_obj["url"] = llm_url
                            changed = True
                    if "llm_base_url" in c_data and c_data["llm_base_url"] != llm_url:
                        c_data["llm_base_url"] = llm_url
                        changed = True

                    if changed:
                        escaped = json.dumps(c_data, indent=2, ensure_ascii=False).replace("'", "'\\''")
                        await conn.run(f"echo '{escaped}' > {QUASAR_CONFIG_PATH}", check=False)
                        logger.info(f"[QUASAR] Updated llm base_url in {QUASAR_CONFIG_PATH} to {llm_url}")
                except Exception as ex:
                    logger.warning(f"[QUASAR] Could not check/update config.json LLM section: {ex}")

            logger.info(f"[QUASAR] Successfully synced Porfiriy server IP ({porfiriy_host}:{openai_port}) across station env files")
            return porfiriy_host
        except Exception as e:
            logger.warning(f"[QUASAR] Error syncing server host IP: {e}")
            return porfiriy_host

    async def ensure_alice_watcher_installed(self, conn, sync_host: bool = False) -> bool:
        """
        Устанавливает наблюдатель состояния Алисы (alice-watcher):
        - выполняет аппаратный дакинг ALSA Music (25% / 100%)
        - отправляет POST /api/station/alice_state и /api/station/airplay_state на сервер Порфирия
        - обеспечивает взаимную паузу Squeezelite и AirPlay.
        """
        try:
            if sync_host:
                await self.ensure_server_host_synced(conn)
            else:
                host_check = await conn.run("test -s /data/opt/porfiriy_host && echo 'ok'", check=False)
                if host_check.exit_status != 0 or host_check.stdout.strip() != "ok":
                    porfiriy_host = await self._resolve_porfiriy_host(conn)
                    if porfiriy_host:
                        await conn.run(f"echo '{porfiriy_host}' > /data/opt/porfiriy_host", check=False)
                        logger.info(f"[QUASAR] Saved Porfiriy host {porfiriy_host} for alice-watcher")

            return await self._install_station_service(
                conn,
                name="Alice Watcher",
                target_dir=ALICE_WATCHER_DIR,
                service_path=ALICE_WATCHER_SERVICE_PATH,
                init_path=ALICE_WATCHER_INIT_PATH,
                service_dist_file="alice_watcher_service.sh",
                init_dist_file="alice_watcher_init.sh",
                hook_marker="Alice Watcher autostart hook",
            )
        except Exception as e:
            logger.warning(f"[QUASAR] Could not install alice-watcher: {e}")
            return False

    async def _configure_routing_rules(self, conn) -> None:
        """
        Настраивает правила маршрутизации в /data/opt/quasar-bridge/config.json:
        - Команды громкости («громче», «тише», «потише», «погромче», «громкость», «звук») отдаются нативной Алисе (route: "yandex"),
          так как она аппаратно регулирует ALSA Master усилитель Станции, масштабируя AirPlay, Squeezelite и голос синхронно.
        - Команды воспроизведения («пауза», «стоп», «хватит», «продолжи», «дальше», «назад», «следующий», «предыдущий», «плей», «играй»)
          удаляются из yandex_system_controls, чтобы уходить в Порфирий (route: "llm"), который управляет Squeezelite и AirPlay.
        """
        try:
            read_res = await conn.run(f"cat {QUASAR_CONFIG_PATH}", check=False)
            if read_res.exit_status != 0 or not read_res.stdout.strip():
                return
            cfg = json.loads(read_res.stdout)
            rules = cfg.get("routing", {}).get("rules", [])
            sys_rule = next((r for r in rules if r.get("name") == "yandex_system_controls"), None)
            if not sys_rule:
                return

            playback_stems = ("пауз", "стоп", "хватит", "продолж", "дальш", "назад", "следующ", "предыдущ", "плей", "играй", "переключ", "скип")
            target_words = ["громче", "тише", "потише", "погромче", "громкость", "звук", "погода", "погоду"]

            match = sys_rule.get("match", {})
            current_words = match.get("contains_any", [])
            if not isinstance(current_words, list):
                current_words = []

            # Очищаем от команд воспроизведения (они должны обрабатываться Порфирием)
            filtered = [w for w in current_words if not any(ps in str(w).lower() for ps in playback_stems)]
            # Гарантируем наличие команд регулировки аппаратной громкости и погоды
            for tw in target_words:
                if tw not in filtered:
                    filtered.append(tw)

            if set(filtered) != set(current_words):
                match["contains_any"] = filtered
                new_json = json.dumps(cfg, indent=2, ensure_ascii=False).replace("'", "'\\''")
                await conn.run(f"echo '{new_json}' > {QUASAR_CONFIG_PATH}", check=False)
                logger.info("[QUASAR] Configured routing rules: volume -> yandex (ALSA Master), playback -> Porfiriy (LLM)")
        except Exception as e:
            logger.warning(f"[QUASAR] Could not update routing rules in config.json: {e}")

    async def update_station(self, ip: str, port: int = DEFAULT_SSH_PORT, server_ip: Optional[str] = None, openai_port: int = 8000) -> Dict[str, Any]:
        """
        Единое обновление станции (Quasar Bridge, Squeezelite, Station AirPlay, Alice Watcher):
        - Очищает устаревший блок локального ALSA auto-ducking / volume_sync
        - Записывает манифест версии version.json
        - Загружает обновленный бинарник из station_dist/bin/ (если присутствует)
        - Синхронизирует актуальный IP сервера в runtime.env, squeezelite.env и porfiriy_host
        - Обновляет службу и автозапуск Squeezelite, Station AirPlay и Alice Watcher
        - Настраивает маршрутизацию: громкость на Алису (ALSA Master), управление воспроизведением на Порфирий
        - Выполняет валидацию конфигурации -check-config и перезапускает службы
        """
        if not ip or ip == "---":
            return {"error": "IP-адрес колонки не указан"}

        bundled = self.get_bundled_version_info()
        bundled_ver = bundled.get("version", "1.2.4-porfiriy.19")

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

                # 4. Выпиливаем старый локальный ALSA auto-ducking / volume_sync / soft_volume
                await self._cleanup_legacy_volume_ducking(conn)

                # 4.5. Настраиваем виртуальный ALSA микшер music_duck для аппаратного дакинга AirPlay и Squeezelite
                await self.ensure_asound_music_duck(conn)

                # 5. Синхронизируем актуальный IP сервера во всех env-файлах колонки
                synced_host = await self.ensure_server_host_synced(conn, server_ip=server_ip, openai_port=openai_port)

                # 6. Настраиваем разделение маршрутизации: громкость на Алису (ALSA Master), плеер на Порфирий
                await self._configure_routing_rules(conn)

                # 7. Обновляем и страхуем службу Squeezelite
                await self.ensure_squeezelite_installed(conn)

                # 8. Обновляем и страхуем службу Station AirPlay
                await self.ensure_station_airplay_installed(conn)

                # 9. Устанавливаем легкий наблюдатель состояния Алисы для серверного дакинга
                await self.ensure_alice_watcher_installed(conn)

                # 10. Проверяем конфигурацию Quasar перед перезапуском
                check_cmd = f"cd {QUASAR_DIR} && set -a && . ./runtime.env && set +a && ./app/bin/quasar-bridge -config {QUASAR_CONFIG_PATH} -check-config"
                check_res = await conn.run(check_cmd, check=False)
                if check_res.exit_status != 0:
                    return {
                        "error": f"Конфигурация не прошла валидацию: {check_res.stderr.strip() or check_res.stdout.strip()}"
                    }

                # 11. Перезапускаем Quasar Bridge
                restart_cmd = f"sh {QUASAR_DIR}/app/control.sh restart"
                restart_res = await conn.run(restart_cmd, check=False)
                if restart_res.exit_status != 0:
                    return {
                        "error": f"Ошибка перезапуска Quasar Bridge: {restart_res.stderr.strip()}"
                    }

                # 12. Перезапускаем Squeezelite
                await conn.run(f"sh {SQUEEZELITE_SERVICE_PATH} restart", check=False)
                await asyncio.sleep(0.5)
                sq_check = await conn.run("ps | grep -v grep | grep squeezelite", check=False)
                sq_running = (sq_check.exit_status == 0 and bool(sq_check.stdout.strip()))

                # 13. Перезапускаем Station AirPlay
                await conn.run(f"sh {STATION_AIRPLAY_SERVICE_PATH} restart", check=False)
                await asyncio.sleep(0.5)
                ap_check = await conn.run("ps | grep -v grep | grep station-airplay", check=False)
                ap_running = (ap_check.exit_status == 0 and bool(ap_check.stdout.strip()))

                # 14. Перезапускаем Alice Watcher
                await conn.run(f"sh {ALICE_WATCHER_SERVICE_PATH} restart", check=False)

                logger.info(f"[QUASAR] Successfully updated station {ip} to {bundled_ver} (Server={synced_host}, Squeezelite running={sq_running}, AirPlay running={ap_running})")
                return {
                    "success": True,
                    "message": f"Единый пакет Яндекс Станции успешно обновлен до {bundled_ver} (сервер {synced_host or 'auto'})",
                    "version": bundled_ver,
                    "server_ip": synced_host,
                    "squeezelite_running": sq_running,
                    "airplay_running": ap_running,
                    "output": restart_res.stdout.strip()
                }
        except Exception as e:
            logger.error(f"[QUASAR] Error updating station {ip}:{port}: {e}")
            return {"error": str(e)}

    # ------------------------------------------------------------------ #
    # Нативный режим Порфирия на Яндекс Станции 2 (без Алисы + openWakeWord)
    # ------------------------------------------------------------------ #

    async def get_native_station_status(self, ip: str, port: int = DEFAULT_SSH_PORT) -> Dict[str, Any]:
        """Возвращает статус Алисы (maind) и локального демона porfiriy-stationd на Станции."""
        if not HAS_ASYNCSSH:
            return {"error": "asyncssh не установлен"}
        try:
            async with await self._open_ssh_connection(ip, port, timeout=4.0) as conn:
                cmd = (
                    f"echo \"ALICE_FLAG=$([ -f {DISABLE_ALICE_FLAG} ] && echo 1 || echo 0)\"; "
                    f"echo \"ALICE_RUN=$(ps | grep -v grep | grep -q 'maind --platformd' && echo 1 || echo 0)\"; "
                    f"echo \"STATIOND_RT=$([ -x {STATIOND_DIR}/lib64/ld-linux-aarch64.so.1 ] && [ -f {STATIOND_DIR}/porfiriy_stationd.py ] && echo 1 || echo 0)\"; "
                    f"echo \"STATIOND_RUN=$(ps | grep -v grep | grep -q 'porfiriy_stationd.py' && echo 1 || echo 0)\"; "
                    f"echo \"MODEL_SIZE=$(wc -c < {STATIOND_DIR}/models/porfiriy.onnx 2>/dev/null || echo 0)\""
                )
                res = await conn.run(cmd, check=False)
                kv = {}
                for line in (res.stdout or "").splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        kv[k.strip()] = v.strip()
                return {
                    "success": True,
                    "alice_disabled": kv.get("ALICE_FLAG") == "1",
                    "alice_running": kv.get("ALICE_RUN") == "1",
                    "stationd_installed": kv.get("STATIOND_RT") == "1",
                    "stationd_running": kv.get("STATIOND_RUN") == "1",
                    "model_size": int(kv.get("MODEL_SIZE", "0") or 0),
                }
        except Exception as e:
            logger.warning(f"[STATIOND] Status error ({ip}:{port}): {e}")
            return {"success": False, "error": str(e)}

    async def set_alice_disabled(self, ip: str, disabled: bool, port: int = DEFAULT_SSH_PORT) -> Dict[str, Any]:
        """
        Переключает Яндекс Станцию 2 между режимом Алисы (Quasar) и нативным режимом Порфирия (openWakeWord).
        При disabled=True: сохраняет резервную копию S99quasar, ставит флаг /data/opt/disable_alice,
        останавливает quasar_launcher2/maind (killall) и запускает porfiriy-stationd (если установлен).
        При disabled=False: останавливает porfiriy-stationd, снимает флаг и возвращает S99quasar.
        """
        if not HAS_ASYNCSSH:
            return {"error": "asyncssh не установлен"}
        try:
            async with await self._open_ssh_connection(ip, port, timeout=6.0) as conn:
                if disabled:
                    cmd = (
                        "mkdir -p /data/opt/backup; "
                        "[ -f /data/opt/backup/S99quasar.orig ] || cp -f /etc/init.d/S99quasar /data/opt/backup/S99quasar.orig 2>/dev/null || true; "
                        f"if ! grep -q 'disable_alice' /etc/init.d/S99quasar 2>/dev/null; then "
                        f"printf '#!/bin/sh\\n[ \"$1\" = \"start\" ] && [ -f {DISABLE_ALICE_FLAG} ] && exit 0\\n[ -x /system/vendor/quasar/onboot.sh ] && /system/vendor/quasar/onboot.sh \"$@\"\\n' > /etc/init.d/S99quasar; "
                        "chmod +x /etc/init.d/S99quasar; "
                        "fi; "
                        f"touch {DISABLE_ALICE_FLAG}; "
                        "killall quasar_launcher2 maind quasar-bridge 2>/dev/null || true; "
                        f"[ -x {ALICE_WATCHER_SERVICE_PATH} ] && sh {ALICE_WATCHER_SERVICE_PATH} stop >/dev/null 2>&1 || true; "
                        "sleep 1; "
                        f"[ -x {STATIOND_SERVICE_PATH} ] && sh {STATIOND_SERVICE_PATH} restart || true"
                    )
                    await conn.run(cmd, check=False)
                else:
                    cmd = (
                        f"[ -x {STATIOND_SERVICE_PATH} ] && sh {STATIOND_SERVICE_PATH} stop || true; "
                        f"rm -f {DISABLE_ALICE_FLAG}; "
                        "if [ -f /data/opt/backup/S99quasar.orig ]; then cp -f /data/opt/backup/S99quasar.orig /etc/init.d/S99quasar && chmod +x /etc/init.d/S99quasar; fi; "
                        "sh /etc/init.d/S99quasar start >/dev/null 2>&1 &"
                    )
                    await conn.run(cmd, check=False)
                    await asyncio.sleep(2.0)

            st = await self.get_native_station_status(ip, port=port)
            st["message"] = (
                "Алиса отключена, включён нативный режим Порфирия"
                if disabled
                else "Алиса и Quasar Bridge восстановлены"
            )
            return st
        except Exception as e:
            logger.error(f"[STATIOND] set_alice_disabled({disabled}) error for {ip}:{port}: {e}")
            return {"success": False, "error": str(e)}

    async def deploy_stationd_files(self, ip: str, port: int = DEFAULT_SSH_PORT, server_ip: Optional[str] = None) -> Dict[str, Any]:
        """Копирует скрипт porfiriy_stationd.py, init/service скрипты и 3 ONNX-модели на Станцию."""
        if not HAS_ASYNCSSH:
            return {"error": "asyncssh не установлен"}
        backend_dir = os.path.dirname(__file__)
        files_map = [
            (os.path.join(STATION_DIST_DIR, "porfiriy_stationd.py"), f"{STATIOND_DIR}/porfiriy_stationd.py", True),
            (os.path.join(STATION_DIST_DIR, "porfiriy_stationd_service.sh"), STATIOND_SERVICE_PATH, True),
            (os.path.join(STATION_DIST_DIR, "porfiriy_stationd_init.sh"), STATIOND_INIT_PATH, True),
            (os.path.join(backend_dir, "porfiriy.onnx"), f"{STATIOND_DIR}/models/porfiriy.onnx", False),
            (os.path.join(backend_dir, "models", "melspectrogram.onnx"), f"{STATIOND_DIR}/models/melspectrogram.onnx", False),
            (os.path.join(backend_dir, "models", "embedding_model.onnx"), f"{STATIOND_DIR}/models/embedding_model.onnx", False),
            (os.path.join(backend_dir, "models", "silero_vad.onnx"), f"{STATIOND_DIR}/models/silero_vad.onnx", False),
        ]
        try:
            async with await self._open_ssh_connection(ip, port, timeout=8.0) as conn:
                await conn.run(f"mkdir -p {STATIOND_DIR}/models", check=False)
                if server_ip:
                    await conn.run(f"echo '{server_ip}' > /data/opt/porfiriy_host", check=False)
                for src, dst, is_exec in files_map:
                    if os.path.exists(src):
                        # Не перезатираем уже существующий непустой porfiriy.onnx (дефолтная модель)
                        if dst.endswith("/models/porfiriy.onnx"):
                            chk = await conn.run(f"test -s {dst} && echo EXISTS || echo MISSING", check=False)
                            if "EXISTS" in (chk.stdout or ""):
                                continue
                        with open(src, "rb") as f:
                            raw = f.read()
                        if not raw:
                            continue
                        if dst.endswith((".sh", ".py")):
                            raw = raw.replace(b"\r\n", b"\n")
                        tmp_dst = f"{dst}.tmp"
                        proc = await conn.create_process(f"cat > {tmp_dst}", encoding=None)
                        proc.stdin.write(raw)
                        proc.stdin.write_eof()
                        await asyncio.wait_for(proc.wait(), timeout=20.0)
                        await conn.run(f"test -s {tmp_dst} && mv -f {tmp_dst} {dst} || rm -f {tmp_dst}", check=False)
                        if is_exec:
                            await conn.run(f"chmod +x {dst}", check=False)
                # Если Алиса уже отключена и рантайм стоит — перезапускаем сервис
                await conn.run(
                    f"if [ -f {DISABLE_ALICE_FLAG} ] && [ -x {STATIOND_DIR}/lib64/ld-linux-aarch64.so.1 ]; then sh {STATIOND_SERVICE_PATH} restart; fi",
                    check=False,
                )
            st = await self.get_native_station_status(ip, port=port)
            st["message"] = "Файлы porfiriy-stationd и модели ONNX обновлены на Станции"
            return st
        except Exception as e:
            logger.error(f"[STATIOND] deploy_stationd_files error ({ip}:{port}): {e}")
            return {"success": False, "error": str(e)}

    async def control_stationd(self, ip: str, action: str, port: int = DEFAULT_SSH_PORT) -> Dict[str, Any]:
        """Управляет службой porfiriy-stationd (start/stop/restart/status)."""
        if action not in ("start", "stop", "restart", "status"):
            return {"success": False, "error": f"Unknown action: {action}"}
        if not HAS_ASYNCSSH:
            return {"error": "asyncssh не установлен"}
        try:
            async with await self._open_ssh_connection(ip, port, timeout=6.0) as conn:
                res = await conn.run(f"sh {STATIOND_SERVICE_PATH} {action}", check=False)
                out = (res.stdout or res.stderr or "").strip()
            st = await self.get_native_station_status(ip, port=port)
            st["output"] = out
            return st
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def upload_station_ww_model(
        self,
        ip: str,
        model_bytes: bytes,
        filename: str = "custom.onnx",
        port: int = DEFAULT_SSH_PORT,
    ) -> Dict[str, Any]:
        """Загружает новый .onnx файл вейкворда под уникальным именем на Яндекс Станцию 2,
        атомарно сохраняет его имя в config.json (чтобы не затиралось при обновлениях) и перезапускает stationd."""
        if not HAS_ASYNCSSH:
            return {"error": "asyncssh не установлен"}
        if not model_bytes or len(model_bytes) < 1024:
            return {"success": False, "error": "Файл модели пустой или повреждён (< 1 КБ)"}

        import re
        from datetime import datetime

        base = os.path.basename(filename or "custom.onnx")
        stem = os.path.splitext(base)[0]
        clean_stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", stem).strip("_")[:32] or "custom"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_name = f"custom_{clean_stem}_{ts}.onnx"
        dst = f"{STATIOND_DIR}/models/{unique_name}"
        tmp_dst = f"{dst}.tmp"
        cfg_path = f"{STATIOND_DIR}/config.json"

        try:
            async with await self._open_ssh_connection(ip, port, timeout=10.0) as conn:
                await conn.run(f"mkdir -p {STATIOND_DIR}/models", check=False)
                proc = await conn.create_process(f"cat > {tmp_dst}", encoding=None)
                proc.stdin.write(model_bytes)
                proc.stdin.write_eof()
                await asyncio.wait_for(proc.wait(), timeout=20.0)
                mv_res = await conn.run(f"test -s {tmp_dst} && mv -f {tmp_dst} {dst}", check=False)
                if mv_res.exit_status != 0:
                    await conn.run(f"rm -f {tmp_dst}", check=False)
                    return {"success": False, "error": "Не удалось сохранить файл модели на Станцию"}

                # Обновляем ww_model в локальном config.json Станции, чтобы деплой stationd никогда её не сбрасывал
                cur_cfg = {}
                read_res = await conn.run(f"cat {cfg_path} 2>/dev/null || true", check=False)
                if read_res.stdout and read_res.stdout.strip():
                    try:
                        parsed = json.loads(read_res.stdout)
                        if isinstance(parsed, dict):
                            cur_cfg = parsed
                    except Exception:
                        pass
                cur_cfg["ww_model"] = unique_name
                cfg_bytes = json.dumps(cur_cfg, indent=2, ensure_ascii=False).encode("utf-8")
                cproc = await conn.create_process(f"cat > {cfg_path}.tmp", encoding=None)
                cproc.stdin.write(cfg_bytes)
                cproc.stdin.write_eof()
                await asyncio.wait_for(cproc.wait(), timeout=10.0)
                await conn.run(f"mv -f {cfg_path}.tmp {cfg_path}", check=False)

                await conn.run(
                    f"if [ -f {DISABLE_ALICE_FLAG} ] && [ -x {STATIOND_SERVICE_PATH} ]; then sh {STATIOND_SERVICE_PATH} restart; fi",
                    check=False,
                )
            return {
                "success": True,
                "size": len(model_bytes),
                "model_name": unique_name,
                "message": f"Модель {unique_name} загружена и закреплена в config.json Станции",
            }
        except Exception as e:
            logger.error(f"[STATIOND] upload_station_ww_model error ({ip}:{port}): {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------ #
    # Управление известными Wi-Fi сетями и серверами (Дом / Дача)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_wpa_supplicant(content: str):
        """Разбирает wpa_supplicant.conf на заголовок (до network={) и список словарей сетей."""
        import re
        header_lines = []
        networks = []
        blocks = list(re.finditer(r"network\s*=\s*\{([^}]*)\}", content, flags=re.DOTALL))
        if blocks:
            prefix = content[: blocks[0].start()].strip()
            if prefix:
                header_lines = [l for l in prefix.splitlines() if l.strip()]
        else:
            header_lines = [l for l in content.splitlines() if l.strip()]
        if not any("ctrl_interface=" in l for l in header_lines):
            header_lines.insert(0, "ctrl_interface=/var/run/wpa_supplicant")
        if not any("update_config=" in l for l in header_lines):
            header_lines.append("update_config=1")

        for m in blocks:
            body = m.group(1)
            net: Dict[str, Any] = {"raw_lines": []}
            for line in body.splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k == "ssid":
                    net["ssid"] = v.strip('"')
                elif k == "psk":
                    net["psk_raw"] = v
                elif k == "priority":
                    try:
                        net["priority"] = int(v)
                    except Exception:
                        net["priority"] = 0
                elif k == "bssid":
                    net["bssid"] = v
                else:
                    net["raw_lines"].append((k, v))
            if net.get("ssid"):
                networks.append(net)
        return header_lines, networks

    @staticmethod
    def _render_wpa_supplicant(header_lines, networks) -> str:
        out = list(header_lines) + [""]
        for n in networks:
            out.append("network={")
            ssid_esc = str(n["ssid"]).replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'\tssid="{ssid_esc}"')
            if n.get("bssid"):
                out.append(f'\tbssid={n["bssid"]}')
            if n.get("psk_raw"):
                out.append(f'\tpsk={n["psk_raw"]}')
            has_km = any(k == "key_mgmt" for k, _ in n.get("raw_lines", []))
            for k, v in n.get("raw_lines", []):
                out.append(f"\t{k}={v}")
            if not has_km:
                out.append("\tkey_mgmt=WPA-PSK" if n.get("psk_raw") else "\tkey_mgmt=NONE")
                out.append("\tauth_alg=OPEN")
            if n.get("priority") is not None:
                out.append(f'\tpriority={int(n["priority"])}')
            out.append("}")
            out.append("")
        return "\n".join(out)

    async def get_station_wifi_networks(self, ip: str, port: int = DEFAULT_SSH_PORT) -> Dict[str, Any]:
        """Читает известные Wi-Fi сети из wpa_supplicant.conf и карту серверов wifi_servers.json."""
        if not HAS_ASYNCSSH:
            return {"success": False, "error": "asyncssh не установлен"}
        try:
            async with await self._open_ssh_connection(ip, port, timeout=5.0) as conn:
                r_wpa = await conn.run(f"cat {WPA_SUPPLICANT_PATH} 2>/dev/null || true", check=False)
                r_srv = await conn.run(f"cat {STATIOND_WIFI_SERVERS_PATH} 2>/dev/null || echo '{{}}'", check=False)
                r_st = await conn.run("wpa_cli -i wlan0 status 2>/dev/null || true", check=False)
                r_def = await conn.run("cat /data/opt/porfiriy_host 2>/dev/null || true", check=False)

            current_ssid = ""
            for line in (r_st.stdout or "").splitlines():
                if line.startswith("ssid="):
                    current_ssid = line.split("=", 1)[1].strip()
                    break

            try:
                servers_map = json.loads(r_srv.stdout or "{}")
                if not isinstance(servers_map, dict):
                    servers_map = {}
            except Exception:
                servers_map = {}

            default_host = (r_def.stdout or "").strip()
            _, nets = self._parse_wpa_supplicant(r_wpa.stdout or "")

            # Дедупликация по SSID (игнорируем системные BSSID-blacklist записи с priority=-999)
            seen = {}
            result = []
            for n in nets:
                if n.get("bssid") and (n.get("priority") or 0) < 0:
                    continue
                ssid = n["ssid"]
                if ssid in seen:
                    continue
                seen[ssid] = True
                srv_entry = servers_map.get(ssid, {})
                result.append({
                    "ssid": ssid,
                    "priority": n.get("priority", 0),
                    "server_ip": srv_entry.get("server_ip", default_host if ssid == current_ssid else ""),
                    "ws_port": int(srv_entry.get("ws_port", 8765)),
                    "is_active": ssid == current_ssid,
                })
            result.sort(key=lambda x: (not x["is_active"], -int(x.get("priority") or 0), x["ssid"]))
            return {
                "success": True,
                "current_ssid": current_ssid,
                "default_host": default_host,
                "networks": result,
            }
        except Exception as e:
            logger.error(f"[WIFI] get_station_wifi_networks error ({ip}:{port}): {e}")
            return {"success": False, "error": str(e)}

    async def save_station_wifi_network(
        self,
        ip: str,
        ssid: str,
        password: str = "",
        server_ip: str = "",
        priority: int = 10,
        ws_port: int = 8765,
        port: int = DEFAULT_SSH_PORT,
    ) -> Dict[str, Any]:
        """Добавляет или обновляет Wi-Fi сеть в wpa_supplicant.conf и привязку IP сервера в wifi_servers.json."""
        ssid = (ssid or "").strip()
        if not ssid:
            return {"success": False, "error": "SSID не может быть пустым"}
        if not HAS_ASYNCSSH:
            return {"success": False, "error": "asyncssh не установлен"}
        try:
            async with await self._open_ssh_connection(ip, port, timeout=6.0) as conn:
                # Бэкап оригинального wpa_supplicant.conf перед первым изменением
                await conn.run(
                    f"mkdir -p /data/opt/backup {STATIOND_DIR}; "
                    f"[ -f /data/opt/backup/wpa_supplicant.conf.bak ] || cp -f {WPA_SUPPLICANT_PATH} /data/opt/backup/wpa_supplicant.conf.bak 2>/dev/null || true",
                    check=False,
                )
                r_wpa = await conn.run(f"cat {WPA_SUPPLICANT_PATH} 2>/dev/null || true", check=False)
                r_srv = await conn.run(f"cat {STATIOND_WIFI_SERVERS_PATH} 2>/dev/null || echo '{{}}'", check=False)

                header_lines, nets = self._parse_wpa_supplicant(r_wpa.stdout or "")
                updated = False
                for n in nets:
                    if n["ssid"] == ssid and not n.get("bssid"):
                        if password:
                            pwd_esc = password.replace("\\", "\\\\").replace('"', '\\"')
                            n["psk_raw"] = f'"{pwd_esc}"'
                        n["priority"] = int(priority)
                        updated = True
                        break
                if not updated:
                    if not password:
                        return {"success": False, "error": "Для новой Wi-Fi сети нужно указать пароль"}
                    pwd_esc = password.replace("\\", "\\\\").replace('"', '\\"')
                    nets.append({
                        "ssid": ssid,
                        "psk_raw": f'"{pwd_esc}"',
                        "priority": int(priority),
                        "raw_lines": [("key_mgmt", "WPA-PSK"), ("auth_alg", "OPEN")],
                    })

                new_wpa = self._render_wpa_supplicant(header_lines, nets)
                proc = await conn.create_process(f"cat > {WPA_SUPPLICANT_PATH}", encoding=None)
                proc.stdin.write(new_wpa.encode("utf-8"))
                proc.stdin.write_eof()
                await asyncio.wait_for(proc.wait(), timeout=10.0)

                try:
                    servers_map = json.loads(r_srv.stdout or "{}")
                    if not isinstance(servers_map, dict):
                        servers_map = {}
                except Exception:
                    servers_map = {}

                if server_ip.strip():
                    servers_map[ssid] = {"server_ip": server_ip.strip(), "ws_port": int(ws_port)}
                elif ssid in servers_map and server_ip == "":
                    servers_map.pop(ssid, None)

                srv_json = json.dumps(servers_map, indent=2, ensure_ascii=False)
                proc2 = await conn.create_process(f"cat > {STATIOND_WIFI_SERVERS_PATH}", encoding=None)
                proc2.stdin.write(srv_json.encode("utf-8"))
                proc2.stdin.write_eof()
                await asyncio.wait_for(proc2.wait(), timeout=10.0)

                await conn.run("wpa_cli -i wlan0 reconfigure >/dev/null 2>&1 || true", check=False)

            res = await self.get_station_wifi_networks(ip, port=port)
            res["message"] = f"Сеть «{ssid}» сохранена на Станции"
            return res
        except Exception as e:
            logger.error(f"[WIFI] save_station_wifi_network error ({ip}:{port}): {e}")
            return {"success": False, "error": str(e)}

    async def delete_station_wifi_network(self, ip: str, ssid: str, port: int = DEFAULT_SSH_PORT) -> Dict[str, Any]:
        """Удаляет Wi-Fi сеть из wpa_supplicant.conf (с защитой от удаления единственной/активной сети)."""
        ssid = (ssid or "").strip()
        if not ssid:
            return {"success": False, "error": "SSID не указан"}
        if not HAS_ASYNCSSH:
            return {"success": False, "error": "asyncssh не установлен"}
        try:
            cur = await self.get_station_wifi_networks(ip, port=port)
            if not cur.get("success"):
                return cur
            if len(cur.get("networks", [])) <= 1:
                return {"success": False, "error": "Нельзя удалить единственную сохранённую Wi-Fi сеть"}
            if ssid == cur.get("current_ssid"):
                return {"success": False, "error": "Нельзя удалить текущую активную сеть (чтобы не потерять связь)"}

            async with await self._open_ssh_connection(ip, port, timeout=6.0) as conn:
                r_wpa = await conn.run(f"cat {WPA_SUPPLICANT_PATH} 2>/dev/null || true", check=False)
                r_srv = await conn.run(f"cat {STATIOND_WIFI_SERVERS_PATH} 2>/dev/null || echo '{{}}'", check=False)

                header_lines, nets = self._parse_wpa_supplicant(r_wpa.stdout or "")
                nets = [n for n in nets if n["ssid"] != ssid]
                new_wpa = self._render_wpa_supplicant(header_lines, nets)
                proc = await conn.create_process(f"cat > {WPA_SUPPLICANT_PATH}", encoding=None)
                proc.stdin.write(new_wpa.encode("utf-8"))
                proc.stdin.write_eof()
                await asyncio.wait_for(proc.wait(), timeout=10.0)

                try:
                    servers_map = json.loads(r_srv.stdout or "{}")
                    if isinstance(servers_map, dict) and ssid in servers_map:
                        servers_map.pop(ssid, None)
                        proc2 = await conn.create_process(f"cat > {STATIOND_WIFI_SERVERS_PATH}", encoding=None)
                        proc2.stdin.write(json.dumps(servers_map, indent=2, ensure_ascii=False).encode("utf-8"))
                        proc2.stdin.write_eof()
                        await asyncio.wait_for(proc2.wait(), timeout=10.0)
                except Exception:
                    pass

                await conn.run("wpa_cli -i wlan0 reconfigure >/dev/null 2>&1 || true", check=False)

            res = await self.get_station_wifi_networks(ip, port=port)
            res["message"] = f"Сеть «{ssid}» удалена со Станции"
            return res
        except Exception as e:
            logger.error(f"[WIFI] delete_station_wifi_network error ({ip}:{port}): {e}")
            return {"success": False, "error": str(e)}

