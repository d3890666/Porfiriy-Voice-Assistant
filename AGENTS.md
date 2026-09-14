# Porfiriy Voice Assistant - AI Memory & Project Conventions

Этот файл содержит память проекта, основные вехи разработки и правила для агента. **Агент обязан учитывать этот файл при каждом новом сеансе работы.**

## 🛑 КРИТИЧЕСКОЕ ПРАВИЛО: Обновление версии (Home Assistant Add-on)
**ВСЕГДА**, когда вносятся изменения в код и делается `git push` (особенно если ожидается, что пользователь проверит изменения в Home Assistant), **ОБЯЗАТЕЛЬНО** увеличивайте значение `version` в файле `config.yaml` (например, с `0.0.12` на `0.0.13`). 
*Причина:* Если версия в `config.yaml` не изменится, Home Assistant не увидит обновления в локальном репозитории аддонов, и пользователь не сможет установить новую версию кода.

## 🛑 КРИТИЧЕСКОЕ ПРАВИЛО: Копирование скомпилированной прошивки ESP32 в аддон
**ВСЕГДА**, когда изменяется код прошивки ESP32 (`esp32_firmware/src/main.cpp` и др.) и выполняется сборка PlatformIO, **ОБЯЗАТЕЛЬНО** копируйте свежий бинарник из `esp32_firmware/.pio/build/esp32-s3-devkitc-1/firmware.bin` в каталог аддона `backend/firmware/firmware.bin` перед коммитом и `git push`.
*Причина:* Аддон поставляет прошивку для автоматического OTA-обновления колонок по WebSocket. Если не скопировать файл в `backend/firmware/firmware.bin`, контейнер аддона не будет содержать актуальную версию прошивки для раздачи по воздуху.

## Архитектура проекта
*   **Backend (`backend/main.py`)**: Работает как Home Assistant Add-on. Является мостом (WebSocket Server) между клиентом (микрофон/динамик) и Google Gemini Live API. Включает встроенный бинарник прошивки (`backend/firmware/firmware.bin`) для One-Click WebSocket OTA обновления колонок.
*   **Client (`client/debug_client.py`)**: Отладочный Python-клиент для ПК. Читает микрофон и отправляет сырой 16kHz PCM аудио-поток по WebSocket, воспроизводит ответы Gemini. Принимает текстовые команды из консоли.
*   **ESP32 Firmware (`esp32_firmware/src/main.cpp`)**: Нативная C++ прошивка для ESP32-S3 (написана в PlatformIO). Реализует I2S захват и воспроизведение аудио, локальный детект вейкворда через TFLite Micro (`porfiriy.tflite` -> `model.h`) и имеет Web Captive Portal (сеть `Porfiriy_Setup`) для настройки Wi-Fi и WebSocket сервера.
*   **Home Assistant API (`backend/ha_api.py`)**: Отвечает за извлечение списка доступных устройств (exposed entities для Assist) и вызов сервисов (`call_service`).

## Особые правила общения с пользователем и процессом разработки
*   **Выбор модели**: НИКОГДА не предлагайте пользователю поменять модель Gemini на другую (например, не предлагайте перейти с `gemini-2.5-flash-native-audio-preview` на `gemini-2.0`). Знания ИИ ограничены старыми датами, а пользователь тестирует новейшие экспериментальные модели, которые еще не описаны в документации агента. Проблема всегда должна решаться в рамках текущей модели.
*   **Прошивка ESP32**: Пользователь шьет плату на **другом компьютере** через веб-интерфейс, используя готовый файл `firmware.bin`. Поэтому агент должен просто сообщать о том, что файл скомпилирован (в папке `.pio/build/.../firmware.bin`), и не пытаться запускать команды типа `pio run -t upload` самостоятельно.

## Важные вехи и принятые решения (Changelog)

### Версия 0.0.61 (WebSocket Dual-Boot OTA Updates via Ingress Web UI)
*   **One-Click WebSocket OTA Updates**: Реализовано автоматическое обновление прошивок подключенных плат ESP32-S3 по воздуху прямо через существующий WebSocket-канал связи. Никаких ручных загрузок файлов: скомпилированный бинарник поставляется внутри аддона (`backend/firmware/firmware.bin`).
*   **Automatic Outdated Discovery & Bulk Banner**: Аддон автоматически сравнивает версии прошивок онлайн-колонок с целевой (`TARGET_FIRMWARE_VERSION = "0.0.61"`). Если в сети обнаружены устройства со старыми версиями, на вкладке «Групповая настройка» (`tab-bulk`) автоматически появляется виджет с кнопкой «⚡ Обновить все устаревшие ESP32». На карточках устройств также появляется индикатор `➔ v0.0.61` и индивидуальная кнопка OTA.
*   **Safe Dual-Boot & Resource Isolation on ESP32**: Прошивка ESP32 дополнена состоянием `STATE_OTA` на базе стандартной библиотеки `<Update.h>`. Перед стартом прошивки колонка полностью освобождает DMA-буферы I2S и глушит TFLite нейросеть, предотвращая нехватку RAM и срабатывание Watchdog. Новая прошивка пишется во второй OTA-слот (`app0` / `app1`) и активируется только при 100% целостности файла, полностью исключая окирпичивание при сбоях Wi-Fi или питания.
*   **Real-Time SSE Progress Streaming**: Бэкенд передает бинарные чанки (по 2 КБ) с плавным темпом и транслирует процент заливки (`0% → 100%`) через Server-Sent Events, отображая живой прогресс-бар в реальном времени.

### Версия 0.0.60 (Fix Ingress JS Caching, Auto-Inline app.js, Model Options & Save Button)
*   **Fix Ingress JS Caching via Auto-Inline**: В `handle_index` в `backend/web_server.py` добавлено автоматическое инлайнирование содержимого `app.js` прямо в тело HTML-ответа перед отправкой клиенту. Это навсегда устранило проблему Home Assistant Ingress, когда браузер или прокси кэшировали устаревший `app.js`, приводя к неработающим обработчикам и рассинхронизации логики с HTML.
*   **Fix Save Global Settings Button**: Кнопка «Сохранить и применить на лету» снабжена индикацией процесса (`⏳ Сохраняю...`), всплывающими тостами и подробным выводом в консоль. Все интерактивные функции явно привязаны к глобальному объекту `window`, исключая `ReferenceError`.
*   **Pre-filled Model Options**: Список моделей в `<select id="cfg-model">` заполнен базовыми проверенными Live-моделями прямо в разметке HTML, что полностью убрало надпись `(Загрузка...)` при открытии вкладки.
*   **ReadyState-Safe Init**: Инициализация скрипта дополнена проверкой `document.readyState` на случай, если событие `DOMContentLoaded` уже наступило к моменту старта скрипта.

### Версия 0.0.59 (Fix SSE Disconnect Error Logging)
*   **Fix SSE Disconnect Error Logging**: В `handle_events` в `backend/web_server.py` добавлен перехват `(ConnectionResetError, BrokenPipeError, aiohttp.ClientConnectionResetError, aiohttp.ClientPayloadError)`. Это устранило спам трейсбеками `ClientConnectionResetError: Cannot write to closing transport` в логах Home Assistant при перезагрузке страниц дашборда или сбросе соединения клиентом.

### Версия 0.0.58 (Fix 400 Bad Request on Save, Pre-fill Textareas & Head Inline Script)
*   **Fix 400 Bad Request on Save**: Кнопки сохранения переведены с `type="submit"` на `type="button"`, а формам добавлено `action="javascript:void(0);" onsubmit="event.preventDefault(); return false;"`. Это полностью устранило нативную отправку формы браузером через GET с многокилобайтным query string в URL, вызывавшую ошибку `400: Bad Request` / URI Too Long в Nginx Ingress Home Assistant.
*   **Pre-filled Prompt Textareas**: Каноничные тексты 4 промптов (Persona, Users, Smart Home, General) внедрены напрямую внутрь тегов `<textarea>` в `index.html`. Теперь поля никогда не отображаются пустыми, даже до инициализации JavaScript.
*   **Synchronous Head Restore Script**: Логика кнопки «↺ Сбросить к шаблону» (`restoreDefaultPrompt`) и словарь шаблонов вынесены в синхронный инлайн-скрипт в `<head>` страницы, что гарантирует их мгновенную работу без зависимости от внешнего `app.js`.
*   **Supervisor Schema & Options Sync**: В `config.yaml` схема `gemini_model` и `voice_name` расширена до произвольных строковых значений (`str`), добавлено поле `temperature: "float?"`, а отправляемые в Supervisor API параметры строго отфильтрованы по разрешенному списку, предотвращая отклонение настроек ядром Home Assistant.

### Версия 0.0.57 (Fix NameError List in web_server)
*   **Fix List import & Future Annotations**: В `backend/web_server.py` добавлен импорт `List` из `typing` и директива `from __future__ import annotations`, что устранило падение при старте контейнера (`NameError: name 'List' is not defined`).

### Версия 0.0.56 (Dynamic Models Discovery, Prompt Defaults & Restoration)
*   **Dynamic Gemini Models Discovery**: Реализовано автоматическое обнаружение доступных моделей Gemini на базе API-ключа пользователя (`fetch_available_gemini_models`) при старте аддона и при обновлении ключа. Включает сортировку с разделением моделей на группу Live Audio (нативный звук / `bidiGenerateContent`) и общие модели, кнопку `🔄 Обновить модели` в UI и возможность ручного ввода кастомных экспериментальных ID.
*   **Prompt Defaults & One-Click Restoration**: В веб-интерфейс добавлены кнопки «↺ Сбросить к шаблону» для каждого из 4 модульных промптов (Persona, Users, Smart Home, General) для мгновенного восстановления каноничных текстов из повести Пелевина. Если при загрузке страницы или в `options.json` поля оказываются пустыми, они автоматически подтягивают каноничные тексты.
*   **Legacy Prompt Migration**: Добавлена плавная миграция старого поля `system_prompt` в `prompt_persona` для пользователей, обновляющихся с ранних версий аддона.

### Версия 0.0.55 (Fix UI Caching, Embed Form Styles & Restore Default Prompts)
*   **Fix UI Caching**: Добавлены строгие заголовки `Cache-Control: no-cache, no-store, must-revalidate` для `index.html` и `/static/*`, а также версионированные query-параметры `?v=0.0.55` для устранения залипания старого CSS в браузере Ingress.
*   **Embedded Form Styles**: Стили конфигуратора продублированы непосредственно в тег `<style>` внутри `index.html` для 100% гарантированного применения темной темы, сетки параметров и редакторов промптов.
*   **Prompt Defaults Fallback**: В `backend/main.py` добавлены константы `DEFAULT_PERSONA`, `DEFAULT_USERS`, `DEFAULT_SMART_HOME`, `DEFAULT_GENERAL`. Если в `/data/options.json` поля промптов отсутствуют или пустые, они автоматически заполняются каноничными текстами.

### Версия 0.0.54 (Web Configurator, Hot Reload, Phrases Regeneration, ESP32 Web UI Links & Status Fix)
*   **Web Configurator & Hot Reload**: Все параметры аддона и 4 модульных промпта перенесены в интерактивную форму Ingress UI (`POST /api/global`). Настройки сохраняются на лету без перезапуска аддона и синхронизируются со штатным Home Assistant через Supervisor API.
*   **Manual Phrases Regeneration**: Добавлена кнопка и эндпоинт `POST /api/phrases/regenerate` для ручной перегенерации каталога системных фраз в фоне (без автоматического сброса при каждом сохранении настроек) и отслеживание статуса (`GET /api/phrases/status`).
*   **Strict ESP32 Device Filtering**: Логика карточек колонок и групповой настройки разделена по условию `isEsp = (dev.device_type === 'esp32')`. Для ESP доступны все аппаратные параметры, шкала Wi-Fi RSSI, порог вейкворда TFLite, кнопки звука и рестарта, а также ссылка на встроенный Web UI колонки (`http://<ip>/ ↗`). Для виртуального клиента (ПК) отображаются только базовые свойства.
*   **ESP32 Web Server & Status Fixes**: Исправлено залипание статуса подключения к серверу (`is_connected && client.available()`), расширен JSON `/status` (IP, RSSI, адрес сервера), устранены задержки `server.handleClient()` при переподключении и экранированы кавычки в JS.

### Версия 0.0.53 (Fix NameError Optional in ha_api)
*   **Fix Optional import**: В `ha_api.py` добавлен импорт `Optional` из `typing` для аннотации возвращаемого значения `get_device_area_name(mac) -> Optional[str]`.

### Версия 0.0.52 (MQTT Discovery, Ingress Web UI, Контекст комнат и Групповые настройки)
*   **Home Assistant MQTT Discovery (`mqtt_discovery.py`)**: Автоматическая регистрация подключаемых устройств (ESP32 и тестового PC-клиента) в Home Assistant с созданием полноценного Устройства и сущностей: сенсор RSSI Wi-Fi (dBm), статус речи, громкость динамика, усиление микрофона, порог вейкворда, переключатель Barge-in, кнопки перезагрузки и поиска звуком.
*   **Acoustic Room Awareness (Контекст вызова из комнаты)**: Определение комнаты расположения колонки через реестры Home Assistant (`area_registry` / `device_registry`). Динамическая передача контекста в Gemini (*«Тебя вызвали из комнаты ...»*) для приоритетного управления устройствами в этой комнате без необходимости называть её вслух.
*   **Ingress Web UI (`web_server.py`, `backend/static/`)**: Веб-панель управления парком колонок, встроенная прямо в боковое меню Home Assistant (порт 8099, Server-Sent Events).
*   **Bulk Configuration (Групповая настройка)**: Возможность пакетного изменения выбранных параметров колонок (громкость, порог вейкворда, цвета подсветки) с помощью чекбоксов-масок без затирания уникальных акустических настроек отдельных комнат.
*   **ESP32 Telemetry & Commands**: ESP32 отправляет стартовый пакет `register`, периодический heartbeat с уровнем `WiFi.RSSI()`, обрабатывает команды `set_config`, `beep` и `reboot`.
*   **PC Debug Client Emulation**: `debug_client.py` регистрируется как устройство в Home Assistant для полноценного тестирования комнатного контекста и MQTT без физической платы.

### Версия 0.0.51 (Темпирование воспроизведения фраз и защита от сброса)
*   **Paced Playback & Speaking Handshake**: Динамик ESP32 переводится в SPEAKING до передачи аудио; чанки идут с темпом ~38мс. Watchdog прерывает фразу-заполнитель при старте речи модели.

### Версия 0.0.50 (Замена Part.from_text на types.Part)
*   **Part Constructor Fix**: В `phrase_manager.py` все части контента создаются через `types.Part(text=...)`.

### Версия 0.0.49 (Исправление LiveConnectConfig для WebSocket-синтеза)
*   **LiveConnectConfig Fix**: Исправлен вызов `LiveConnectConfig` (передача `response_modalities=["AUDIO"]`) и именованный аргумент `Part.from_text(text=...)` в `phrase_manager.py`.

### Версия 0.0.48 (Синтез системных реплик через Live WebSocket)
*   **Live WebSocket Audio Synthesis in PhraseManager**: Синтез реплик переведён с не поддерживаемого REST API на нативный `client.aio.live.connect` (`bidiGenerateContent`), что полностью устранило ошибки 400 и квоты 429.

### Версия 0.0.47 (Таймаут размышлений 7 секунд)
*   **Thinking Timeout 7s**: Задержка срабатывания фразы ожидания увеличена с 2.5 до 7.0 секунд и вынесена в `thinking_timeout_s`.

### Версия 0.0.46 (Фикс модели генерации реплик и бамп версии)
*   **User Model Fix in PhraseManager**: Модель для генерации берется строго из `options["gemini_model"]` пользователя, вычищен устаревший `gemini-2.0-flash-exp`.

### Версия 0.0.45 (Динамический генератор системных реплик Порфирия)
*   **PhraseManager Module (`phrase_manager.py`)**: Реализован автономный менеджер динамических реплик. Сначала Gemini с системным промптом `prompt_persona` генерирует каталог фраз в формате JSON (до 4 слов каждая, без роботических клише), затем батчем синтезирует их в PCM 16-bit 24kHz и кэширует в `/data/audio_cache/`.
*   **Runtime Event Hooks in `main.py`**:
    * Сторожевой таймер размышлений (2.5 с): проигрывание случайной фразы `thinking`, если Gemini Live задерживает генерацию аудио (например, при поиске музыки).
    * Обработка ошибок сети и 1011: озвучка фразы `network_error` вместо падения в тишине.
    * Ошибка вызова устройства в Home Assistant: озвучка фразы `device_error`.
    * Таймаут / пустой шум: озвучка фразы `empty_noise`.
*   **Persona Hash Invalidation**: Кэш автоматически пересоздается при изменении пользователем `prompt_persona` в настройках Home Assistant, обеспечивая 100% динамичность роли.

### Версия 0.0.44 (Шторы cover, распознавание пользователей Денис/Света и 4 модульных промпта)
*   **Cover Domain & Curtains Control**: Добавлен домен `cover` в `ha_api.py` `allowed_domains`. Расширен `call_ha_service` в `gemini_client.py` (добавлены сервисы `open_cover`, `close_cover`, `stop_cover`, `set_cover_position` и опциональные параметры `position`, `temperature`, `hvac_mode`). В `backend/main.py` исправлена передача `service_data` — теперь все параметры из `args` пробрасываются в Home Assistant, а не затираются `entity_id`.
*   **Acoustic Speaker Recognition**: За счет нативного 16 kHz PCM аудиопотока в Gemini Live промпт инструктирует модель определять пол говорящего по F0: мужской низкий голос (~85-180 Гц) -> Денис (мужской род глаголов), женский высокий голос (~165-260+ Гц) -> Света (женский род глаголов).
*   **4 Modular Prompt Fields**: Промпт разделен на 4 англоязычных поля в `config.yaml` (`prompt_persona`, `prompt_users`, `prompt_smart_home`, `prompt_general`). В `backend/main.py` выполняется их модульная конкатенация с сохранением обратной совместимости со старым `system_prompt`.
*   **Add-on Translations**: Созданы `translations/en.yaml` и `translations/ru.yaml` для красивого отображения всех опций и подсказок в UI Home Assistant.

### Версия 0.0.1 - 0.0.9 (Базовая интеграция)
*   Создан базовый каркас аддона для Home Assistant.
*   Реализована двунаправленная потоковая передача аудио по WebSocket.
*   Переход на новые методы `google-genai` SDK: вместо устаревшего `session.send()` используются `send_realtime_input`, `send_client_content` и `send_tool_response`.
*   Настроен ReAct: Gemini вызывает инструмент `call_ha_service` для управления умным домом.

### Версия 0.0.10 - 0.0.11 (Grounding, VAD и Earcons)
*   **Google Search Grounding**: Добавлен инструмент `google_search`, позволяющий Порфирию искать информацию в интернете.
*   **VAD Settings**: В настройки `config.yaml` вынесен параметр `vad_silence_duration_ms` для тонкой настройки прерывания.
*   **Earcons (Звуковые уведомления)**: Бэкенд генерирует PCM-синусоиды ("Дзинь" и "Бум") и шлет их клиенту сразу после успешного/неуспешного выполнения `call_ha_service`.
*   **Улучшение ошибок ReAct**: `ha_api.py` возвращает развернутый текстовый ответ с ошибкой HTTP, чтобы Gemini мог сам исправить запрос.

### Версия 0.0.12 (Починка эха и Barge-in)
*   **Отказ от локального VAD**: Изначально `debug_client.py` использовал `webrtcvad` для перебивания ассистента. Из-за акустического эха происходили ложные срабатывания. **Решение:** Локальный VAD полностью удален. Теперь звук льется непрерывно, а детектированием перебивания (Barge-in) занимается исключительно серверный VAD самого Gemini.
*   **Текстовый ввод**: В `debug_client.py` добавлена поддержка асинхронного ввода текста с консоли (отправляется как JSON `{"text": "..."}`), бэкенд шлет это в Gemini через `send_client_content`.
*   **Многострочный системный промпт**: Оставлен в стандартном `config.yaml` Home Assistant (редактируется через режим YAML `|`).

### Версия 0.0.13 (Нативная прошивка ESP32-S3 и TFLite)
*   **Отказ от ESPHome**: Переход на голую C++ прошивку PlatformIO для полного контроля над I2S-микрофоном (INMP441) и динамиком (MAX98357A).
*   **Локальный Wake Word**: Конвертация `porfiriy.tflite` в C-массив `model.h`. Вейкворд детектируется локально с использованием `esp-micro-speech-features` (нормализация INCEPTION).

### Версия 0.0.14 (Умный Сон, Pre-roll Буфер и Web UI)
*   **Умный режим сна**: Если ассистент закончил ответ (`turn_complete`), сервер принудительно шлет клиенту WebSocket команду `{"type": "sleep"}`. ESP32 прекращает стриминг звука с микрофона и возвращается к ожиданию вейкворда. Это устраняет круглосуточную трансляцию.
*   **Буфер предзаписи (Pre-roll)**: ESP32 постоянно хранит последнюю 1 секунду сырого звука (16000 сэмплов). При срабатывании вейкворда плата сначала отправляет на сервер этот буфер, а затем открывает стрим. Это позволяет произносить команды слитно, без паузы (например, "Порфирийвключисвет").
*   **Полноценный Web UI**: На ESP32 постоянно крутится веб-сервер, доступный по IP-адресу от роутера. На странице можно настраивать усиление микрофона, громкость динамика, чувствительность вейкворда и **цвета/анимацию RGB-диода (FastLED)**. Настройки применяются мгновенно без перезагрузки платы (через AJAX `POST /save` и хранятся в `Preferences`). Web-морда также имеет AJAX-статус, отображающий текущее состояние колонки.

### Версия 0.0.40 (Стабилизация серверного VAD)
*   **Adaptive Silence Padding**: При получении `end_of_speech` от ESP32 бэкенд динамически рассчитывает объем тишины (`vad_silence_duration_ms + 300ms`) и отправляет не менее 28 чанков PCM тишины (~896 мс). Это устранило проблему, когда Gemini зависал в ожидании продолжения речи при обрыве стрима с микрофона.

### Версия 0.0.41 (Полноценный Multi-turn в сессиях Gemini Live)
*   **Persistent Multi-Turn Listener**: В SDK `google-genai` метод `session.receive()` прерывается по `break` при `turn_complete=True`. В `receive_from_gemini()` добавлен внешний цикл `while not websocket.closed:`, чтобы сокет чтения Gemini переоткрывался на каждый последующий ход. Это полностью решило проблему зависания ассистента на 2-м и последующих запросах.

### Версия 0.0.42 (Исправление совместимости с websockets v13+)
*   **Fix ServerConnection State**: В новых версиях `websockets` у объекта `ServerConnection` нет атрибута `.closed`. Заменено на `while True:`, что устранило падение задачи чтения ответов Gemini Live при старте.

### Версия 0.0.43 (Media Ducking и голосовое прерывание Barge-In)
*   **Media Ducking**: Бэкенд автоматически определяет активные плееры в Home Assistant (`media_player` в состоянии `playing`) и понижает их громкость при срабатывании вейкворда, восстанавливая обратно после `turn_complete` / `sleep`.
*   **Wake Word Barge-In**: В прошивке ESP32 реализована возможность прерывания говорящего ассистента повторным вейквордом (настраивается в Web UI через чекбокс `barge_in`).
*   **Anti-Verbosity Directive**: В промпт добавлено ограничение длины монологов и реакция на слова «хватит», «стоп», «молчи».
