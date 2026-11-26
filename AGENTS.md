# Porfiriy Voice Assistant - AI Memory & Project Conventions

Этот файл содержит память проекта, основные вехи разработки и правила для агента. **Агент обязан учитывать этот файл при каждом новом сеансе работы.**

---

# 🛑🛑🛑 ГЛАВНЫЕ ЖЕЛЕЗНЫЕ ПРАВИЛА ОБНОВЛЕНИЯ ДЛЯ АГЕНТА (ЧИТАТЬ В ПЕРВУЮ ОЧЕРЕДЬ!) 🛑🛑🛑

## 🚨 ПРАВИЛО №1: СКОМПИЛИРОВАТЬ И ПОЛОЖИТЬ ПРОШИВКУ ДЛЯ ESP32 ПЕРЕД PUSH!
**ЕСЛИ ХОТЬ КАК-ТО МЕНЯЛСЯ КОД ПРОШИВКИ ESP32 ИЛИ ДОБАВЛЯЛИСЬ НОВЫЕ ФИЧИ/КОМАНДЫ ДЛЯ КОЛОНОК:**
1. **ОБЯЗАТЕЛЬНО ПОДНЯТЬ ВЕРСИЮ В КОДЕ ПРОШИВКИ**: В файле `esp32_firmware/src/main.cpp` изменить `#define FIRMWARE_VERSION "X.X.XX"`.
2. **СКОМПИЛИРОВАТЬ ПРОШИВКУ ЧЕРЕЗ PLATFORMIO**: 
   Выполнить команду: `/Users/a111/.local/bin/pio run -d esp32_firmware`
3. **ОБЯЗАТЕЛЬНО СКОПИРОВАТЬ СКОМПИЛИРОВАННЫЙ БИНАРНИК В АДДОН**:
   Выполнить команду: `cp -fv esp32_firmware/.pio/build/esp32-s3-devkitc-1/firmware.bin backend/firmware/firmware.bin`
4. **СИНХРОНИЗИРОВАТЬ ЦЕЛЕВУЮ ВЕРСИЮ В БЭКЕНДЕ**: В файле `backend/device_manager.py` указать `TARGET_FIRMWARE_VERSION = "X.X.XX"`.
> **ПОЧЕМУ ЭТО КРИТИЧНО:** Аддон раздает файл `backend/firmware/firmware.bin` колонкам по воздуху (WebSocket OTA). Если агент не скомпилирует бинарник и не положит его в `backend/firmware/firmware.bin`, пользователь нажмет кнопку «⚡ OTA», но колонка прошьет старый код!

## 🚨 ПРАВИЛО №2: ВСЕГДА ПОДНИМАТЬ ВЕРСИЮ АДДОНА В CONFIG.YAML!
**ПЕРЕД КАЖДЫМ `git push` С ЛЮБЫМИ ИЗМЕНЕНИЯМИ ДЛЯ ПОЛЬЗОВАТЕЛЯ:**
1. **УВЕЛИЧИТЬ ВЕРСИЮ** в `config.yaml` (`version: "0.0.XX"`).
2. **ОБНОВИТЬ CACHE-BUSTER** в `backend/static/index.html` (`?v=0.0.XX` для css и js), чтобы браузер пользователя не брал старые скрипты из кэша.
> **ПОЧЕМУ ЭТО КРИТИЧНО:** Home Assistant проверяет обновления ТОЛЬКО по номеру `version` в `config.yaml`. Без увеличения версии пользователь НЕ СМОЖЕТ нажать кнопку «Обновить» в интерфейсе Home Assistant!

## 🚨 ПРАВИЛО №3: ВСЕГДА ПУШИТЬ В ВЕТКУ MAIN!
**ДЛЯ ОТПРАВКИ ИЗМЕНЕНИЙ В GITHUB:**
Выполнять: `/Users/a111/.local/bin/git push origin main`

## 🚨 ПРАВИЛО №4: ВСЕГДА И СТРОГО ИСПОЛЬЗОВАТЬ ТОЛЬКО GEMINI LIVE API (WEBSOCKET)!
**НИКОГДА НЕ ИСПОЛЬЗОВАТЬ ДЛЯ ОБЩЕНИЯ С СЕРВЕРОМ GOOGLE ЧТО-ТО КРОМЕ LIVE API:**
1. **ЗАПРЕЩЕНО** использовать REST-вызовы `generate_content` или сторонние текстовые эндпоинты Google API.
2. **ВСЕ ЗАПРОСЫ** (микрофон ESP32, PC streamer, OpenAI API Gateway для Яндекс Станции) обязаны идти **СТРОГО через WebSocket Gemini Live API (`bidiGenerateContent`)**!
3. **СТРОГО ТА МОДЕЛЬ, ЧТО ВЫБРАНА В НАСТРОЙКАХ**: Использовать только ту модель, которую пользователь выбрал в `options["gemini_model"]` (например, `models/gemini-3.8-live`). НИКАКИХ скрытых замен, подмен или фолбэков на flash/текстовые модели!
4. Live-модели Gemini (включая `gemini-3.8-live`) поддерживают исключительно модальность `response_modalities=["AUDIO"]` (модальность TEXT отклоняется сервером Google с ошибкой 1007). Для получения текстового ответа (режим `alice_tts`) в конфигурацию Live API включается `output_audio_transcription=types.AudioTranscriptionConfig()`, а текст ответа собирается в реальном времени из потока `server_content.output_transcription.text`.
5. Отправка сообщений клиентом выполняется через `session.send_client_content(turns=[types.Content(...)], turn_complete=True)`, а ответы на вызовы инструментов (`tool_call`) — через `session.send_tool_response(function_responses=...)`.

---

## Архитектура проекта
*   **Backend (`backend/main.py` + общее ядро `config.py`, `audio_utils.py`, `net_utils.py`, `prompt_builder.py`, `tool_executor.py`)**: Работает как Home Assistant Add-on. Является мостом (WebSocket Server + OpenAI API Gateway + Ingress Web UI) между клиентами (ESP32, PC Streamer, Яндекс Станция) и Google Gemini Live API. Включает встроенный бинарник прошивки (`backend/firmware/firmware.bin`) для One-Click WebSocket OTA обновления колонок.
*   **Station / Quasar (`backend/quasar_manager.py`, `backend/station_ducking.py`, `backend/station_dist/*`)**: Управление Яндекс Станциями по SSH (Quasar Bridge, Squeezelite, Station AirPlay, Alice Watcher, ALSA `Master` / `music_duck`).
*   **Home Assistant API (`backend/ha_api.py`)**: Синглтон-клиент REST/WS Home Assistant и Music Assistant (exposed entities, `call_service`, `state_changed` listener).
*   **Client (`client/streamer.py`, `client/debug_client.py`)**: Python-клиенты для ПК (16kHz PCM по WebSocket, воспроизведение ответов Gemini).
*   **ESP32 Firmware (`esp32_firmware/src/main.cpp`)**: Нативная C++ прошивка для ESP32-S3 (PlatformIO). I2S захват/воспроизведение аудио, локальный детект вейкворда TFLite Micro (`model.h`), Captive Portal (`Porfiriy_Setup`).

## Особые правила общения с пользователем и процессом разработки
*   **Никаких самовольных правок и деплоя**: НИКОГДА не редактировать код, не устанавливать сервисы и не деплоить, пока пользователь явно не попросит или не утвердит план (на вопросы, идеи и обсуждения сначала отвечать текстом и предлагать план).
*   **Маркетплейсы без непрошеных тестов**: НЕ запускать тестовые поисковые запросы в маркетплейсы без прямой просьбы пользователя.
*   **Инфраструктура и доступы (строго вне git)**: Все IP-адреса, SSH-учётки, токены и заметки по домашним серверам хранятся на сервере Home Assistant в разделе `/share/.porfiriy_infra_notes.json` (чтение через SSH на HA: `cat /share/.porfiriy_infra_notes.json`, также примонтирован в контейнер Порфирия). **НИКОГДА не коммитить пароли, ключи и токены в `AGENTS.md` или репозиторий.**
*   **Выбор модели**: НИКОГДА не предлагайте пользователю поменять модель Gemini на другую. Проблема всегда должна решаться в рамках текущей выбранной модели.
*   **Прошивка ESP32**: Пользователь шьет плату на **другом компьютере** через веб-интерфейс, используя готовый файл `firmware.bin`. Агент просто компилирует файл и не запускает `pio run -t upload`.

## Аппаратная архитектура Яндекс Станции 2 (подтверждено SSH-разведкой)
*   **CPU / ОС**: 64-битное ядро Linux 4.9.68 `aarch64` (4 ядра ARM Cortex-A53 с NEON/asimd), но **32-битный userspace (`armhf`, `/lib/ld-linux-armhf.so.3`, `glibc 2.30`, `libstdc++.so.6.0.28`, BusyBox 1.31.1 без Python)**. При наличии изолированного `arm64` загрузчика (`ld-linux-aarch64.so.1` + `lib64`) на 64-битном ядре свободно запускаются нативные 64-битные бинарники и `onnxruntime` (ARMv8 NEON).
*   **Память и диск**: `489 МБ` RAM (`~310 МБ` доступно в простое), `/data` (`/bkupgrade`) — `198.8 МБ` (`~157 МБ` свободно), `/tmp` (`tmpfs`) — `245 МБ`. Температура SoC в простое `40–42 °C`.
*   **Аудио-захват и аппаратный AEC-loopback**:
    *   `hw:0,1` (`TDM-C-tlv320adc3101-adc`, 2 ch @ 48 кГц) — держится открытым сервисом `S40adc` (`/usr/bin/arecord -D hw:0,1 ... /dev/null`) для подачи аппаратного loopback динамика в PDM-мультиплексор.
    *   `hw:0,2` (`PDM-dummy`, 16 ch @ 48 кГц, `S16_LE`) — единый аппаратно синхронизированный поток: **4 микрофона** (`ch 0, 2, 4, 7` по `hw_feedback_and_mics_raw`, на сыром `hw:0,2` активны `0..7` с выраженным PDM DC-offset — обязателен DC-блокер/HPF!) + **2 канала аппаратного feedback динамика** (`ch 8, 9`) для программного AEC (WebRTC AEC3 / SpeexDSP).
    *   При работающей Алисе `hw:0,2` монопольно занят `maind` (`S99quasar` / `quasar_launcher2`). Системный watchdog (`/etc/init.d/S01watchdog`, `/dev/watchdog`) от `maind` **не зависит** — Алису можно безопасно останавливать (`killall quasar_launcher2 maind`; в BusyBox нет `pkill`), освобождая `hw:0,2`.
*   **Производительность `openWakeWord` (ONNX) на Станции**: полная тройка `melspectrogram.onnx` + `embedding_model.onnx` + `porfiriy.onnx` (IR v10 ➔ `onnxruntime >= 1.19`) на **1 потоке** выполняется за `40.7–45.8 мс` (`max 52.6 мс` при бюджете `80 мс`) — всего **~14% суммарного CPU** (4 ядра), `Temp 40–44 °C`.
*   **Подсветка / периферия**: LED-панель инициализируется `/etc/init.d/S29led_panel_midi`; кнопки и Bluetooth в нативном режиме Порфирия не требуются.

---

## Последние ключевые релизы (полная история — в `CHANGELOG.md`)

### Версия 0.0.150 (Non-blocking SSH Audio Queue & Long Response Timeout Fix)
*   В `backend/openai_service.py` запись PCM в SSH `aplay` вынесена в неблокирующую очередь `asyncio.Queue` + фоновую задачу `_ssh_writer_loop` с динамическим таймаутом доигрывания, `keepalive_interval=5` и отменой активной речи при новом запросе/стопе; таймаут `_consume_live_session` увеличен до 60с. Устранён `TimeoutError` на `ssh_proc.stdin.drain()` при длинных ответах.

### Версия 0.0.149 (Backend & Frontend Decomposition + HA Connection Pool)
*   Вынесен `backend/fast_commands.py` из `openai_service.py`; в `ha_api.py` добавлен общий `aiohttp.ClientSession` и `_ws_authenticate`; в `quasar_manager.py` унифицированы установщики и контроллеры сервисов Станции.
*   Вынесено 443 строки инлайн-CSS и дубль `CANONICAL_PROMPTS` из `index.html` в `style.css` (`index.html` 1496 ➔ 961 строка); в `app.js` убраны дубли переключателей паролей и дропдаунов плееров.

### Версия 0.0.147 (Refactoring, Bugfixes & Repo Hygiene)
*   Убран из git `scratch_venv/` (2 441 файл), удалён `esp32_ww_test/`, убран дубликат `porfiriy.onnx`, разгружен `AGENTS.md`.
*   Выделено единое ядро бэкенда: `config.py`, `audio_utils.py`, `net_utils.py`, `prompt_builder.py`, `tool_executor.py`, синглтон `HomeAssistantAPI`.
*   Исправлено 11+ реальных багов (`phrase_manager.regenerate`, `openai_service._extract_ip`, `nonlocal` и `await` в `handle_pc_streamer`, `mqtt_discovery` v2 `_on_disconnect`, `ha_api` `NoneType` state listener, `dlna_player` `import re`, `quasar_manager` двойной sync порта, `station_airplay_service.sh` проверка `music_duck`, хардкод IP `192.168.1.50`, `app.js` `renderDevicesGrid`, дубль `chk-ww-mode`).

### Версия 0.0.146 (Fix Alice 1..10 Step Scale Mapping & Vendor Volume Sync)
*   В `set_volume.sh` реализован маппинг шагов Алисы 1..10 ➔ 10%..100% ALSA `Master` и синхронизация `last_vendor_volume`. В `alice_watcher_service.sh` добавлена проверка `/data/opt/disable_ducking`.

### Версия 0.0.145 (Unified ALSA Master Architecture & Anti-Clipping Voice Fix)
*   Убран буст `1.8x` в `OpenAIService._stream_chunk`, выстроена единая иерархия ALSA (`dmix_spk_plug` ➔ `soft_volume` (`Master`) ➔ `pcm.!default` + `pcm.music_duck`). Дистрибутив Станции `v1.2.4-porfiriy.19`.

### Версия 0.0.144 (Fix Squeezelite mmap_commit Error on Softvol Ducking)
*   В `squeezelite_service.sh` добавлен флаг `-a :::0` (безопасный `snd_pcm_writei` без mmap).
