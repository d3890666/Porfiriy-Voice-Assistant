// Porfiriy Dashboard Frontend Logic
const API_BASE = window.location.pathname.replace(/\/index\.html.*$/i, '').replace(/\/+$/, '');

let devices = [];
let globalOptions = {};
let availableModels = [];
let bigModelsHealth = {};
let bigModelsPoolState = [];
let availableMediaPlayers = [];
let availableAreas = [];
let CANONICAL_PROMPTS = window.CANONICAL_PROMPTS || {};

// Инициализация
function initApp() {
  setupTabs();
  fetchDevices();
  fetchGlobalOptions();
  fetchUsers();
  fetchAreas();
  if (typeof loadQuietModeCandidates === 'function') loadQuietModeCandidates();
  setupSSE();
}

async function fetchAreas() {
  try {
    const res = await fetch(`${API_BASE}/api/areas`);
    if (res.ok) {
      const data = await res.json();
      availableAreas = data.areas || [];
      if (devices.some(d => d.device_type === 'wear_os')) {
        renderDevicesGrid();
      }
    }
  } catch (e) {
    console.debug('Error fetching areas:', e);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}

// Навигация по вкладкам
function setupTabs() {
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      
      btn.classList.add('active');
      const tabId = btn.getAttribute('data-tab');
      const target = document.getElementById(tabId);
      if (target) {
        target.classList.add('active');
        if (tabId === 'tab-brain') {
          if (typeof populateWwMonitorSources === 'function') {
            populateWwMonitorSources();
          }
          if (typeof startWwMonitorPolling === 'function') {
            startWwMonitorPolling();
          }
          if (typeof loadQuietModeCandidates === 'function') {
            loadQuietModeCandidates();
          }
        } else {
          if (typeof stopWwMonitorPolling === 'function') {
            stopWwMonitorPolling();
          }
        }
        if (tabId === 'tab-users') {
          if (typeof fetchUsers === 'function') fetchUsers();
        } else if (tabId === 'tab-plugins') {
          if (typeof fetchProviders === 'function') fetchProviders();
          if (typeof fetchUsers === 'function' && (!window._porfiriyUsersLoaded)) fetchUsers();
          if (typeof fetchToolsAudit === 'function') fetchToolsAudit();
        }
      }
    });
  });
}

// Загрузка списка колонок
async function fetchDevices() {
  try {
    const res = await fetch(`${API_BASE}/api/devices`);
    if (res.ok) {
      const data = await res.json();
      devices = data.devices || [];
      renderDevicesGrid();
      renderBulkTargets();
      renderOtaBanner();
      updateHeaderStats();
      populateWwMonitorSources();
    }
  } catch (err) {
    console.error('Error fetching devices:', err);
  }
}

// Загрузка глобальных настроек
async function fetchGlobalOptions() {
  try {
    const res = await fetch(`${API_BASE}/api/global`);
    if (res.ok) {
      const data = await res.json();
      globalOptions = data.options || {};
      availableModels = data.models || [];
      bigModelsHealth = data.big_models_health || {};
      availableMediaPlayers = data.media_players || [];
      if (data.canonical_prompts) {
        CANONICAL_PROMPTS = data.canonical_prompts;
        window.CANONICAL_PROMPTS = data.canonical_prompts;
      }
      if (data.addon_version) {
        const verEl = document.getElementById('app-version');
        if (verEl) verEl.textContent = `v${data.addon_version}`;
      }
      if (data.bundled_quasar_version) {
        window.BUNDLED_QUASAR_VERSION = data.bundled_quasar_version;
        const qVerEl = document.getElementById('quasar-modal-version-badge');
        if (qVerEl) qVerEl.textContent = `v${data.bundled_quasar_version}`;
      }
      renderBrainInfo();
    }
  } catch (err) {
    console.error('Error fetching global options:', err);
  }
}

// Обновление статов в шапке
function updateHeaderStats() {
  const onlineCount = devices.filter(d => d.is_online).length;
  const statOnline = document.getElementById('stat-online');
  if (statOnline) {
    statOnline.textContent = `${onlineCount} / ${devices.length}`;
  }
}

// Отрисовка сетки колонок
function renderDevicesGrid() {
  const grid = document.getElementById('devices-grid');
  if (!grid) return;

  if (devices.length === 0) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column: 1/-1; text-align: center; padding: 40px; color: #888;">
        <p style="font-size: 16px; margin-bottom: 8px;">Колонки ещё не подключены к серверу.</p>
        <p style="font-size: 13px;">Подключите ESP32-S3 к Wi-Fi или запустите тестовый клиент <code>python debug_client.py</code></p>
      </div>
    `;
    return;
  }

  grid.innerHTML = devices.map(dev => {
    const cfg = dev.config || {};
    const stateClass = dev.is_online ? (dev.state || 'idle') : 'offline';
    const stateLabel = {
      'idle': 'Ожидание',
      'listening': 'Слушает',
      'thinking': 'Думает',
      'speaking': 'Говорит',
      'offline': 'Не в сети'
    }[stateClass] || stateClass;

    const rssi = dev.rssi || -70;
    const rssiInfo = getRssiVisual(rssi);

    const roomText = dev.area_name ? `📍 ${dev.area_name}` : '📍 Без комнаты';
    const volumePercent = Math.round((cfg.speaker_volume !== undefined ? cfg.speaker_volume : 0.5) * 100);
    const micRecState = (window.micRecordingsState && window.micRecordingsState[dev.mac]) || null;

    // Условие: если ESP32 — отображаем все настройки, шкалу Wi-Fi, вейкворд и Web UI. Иначе — только базовые.
    const isEsp = (dev.device_type === 'esp32');

    return `
      <div class="device-card ${stateClass}" id="card-${dev.mac}">
        <div class="card-header">
          <div class="dev-title-block">
            <span class="dev-name">${escapeHtml(dev.name)}</span>
            <span class="area-badge">${roomText}</span>
          </div>
          <div style="display: flex; align-items: center; gap: 8px;">
            <span class="state-badge ${stateClass}">${stateLabel}</span>
            <button class="btn-delete-device" onclick="deleteDevice('${dev.mac}', '${escapeHtml(dev.name)}')" title="Удалить устройство из списка">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>
            </button>
          </div>
        </div>

        ${isEsp ? `
          <div class="rssi-container">
            <div class="rssi-bars">
              <div class="rssi-bar ${rssiInfo.bars >= 1 ? 'active ' + rssiInfo.color : ''}"></div>
              <div class="rssi-bar ${rssiInfo.bars >= 2 ? 'active ' + rssiInfo.color : ''}"></div>
              <div class="rssi-bar ${rssiInfo.bars >= 3 ? 'active ' + rssiInfo.color : ''}"></div>
              <div class="rssi-bar ${rssiInfo.bars >= 4 ? 'active ' + rssiInfo.color : ''}"></div>
            </div>
            <span style="font-weight: 600;">${rssi} dBm</span>
            <span style="color: #888; font-size: 12px; margin-left: auto;">${rssiInfo.quality}</span>
          </div>
        ` : dev.device_type === 'pc_streamer' ? `
          <div class="virtual-client-banner" style="background: linear-gradient(135deg, rgba(100, 65, 200, 0.15), rgba(60, 130, 230, 0.1)); border: 1px solid rgba(100, 65, 200, 0.3); border-radius: 10px; padding: 10px 14px; display: flex; justify-content: space-between; align-items: center;">
            <span style="font-weight: 700; font-size: 13px;">🎙️ PC Streamer</span>
            <span style="font-size: 11px; color: #8b949e; font-family: monospace; background: rgba(255,255,255,0.05); padding: 2px 8px; border-radius: 12px;">Серверный вейкворд</span>
          </div>
          <!-- Streamer Settings -->
          <div style="margin: 10px 0; display: flex; flex-direction: column; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 90px;">🔊 Плеер:</span>
              <select id="streamer-player-${dev.mac}" style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 4px 8px; border-radius: 6px; font-size: 12px;" onchange="saveStreamerConfig('${dev.mac}', 'response_player', this.value)">
                <option value="">— Не задан (динамики ПК) —</option>
                ${availableMediaPlayers.map(p => `<option value="${escapeHtml(p.entity_id)}" ${(cfg.response_player === p.entity_id) ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('')}
                ${cfg.response_player && !availableMediaPlayers.some(p => p.entity_id === cfg.response_player) ? `<option value="${escapeHtml(cfg.response_player)}" selected>${escapeHtml(cfg.response_player)}</option>` : ''}
              </select>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 90px;">🎯 Порог WW:</span>
              <input type="range" min="0.01" max="1.00" step="0.01" value="${cfg.ww_threshold !== undefined ? cfg.ww_threshold : 0.94}" style="flex: 1;" oninput="document.getElementById('ww-thr-val-${dev.mac}').innerText = parseFloat(this.value).toFixed(2)" onchange="saveStreamerConfig('${dev.mac}', 'ww_threshold', parseFloat(this.value))">
              <span id="ww-thr-val-${dev.mac}" style="font-family: monospace; font-size: 12px; min-width: 36px;">${Number(cfg.ww_threshold !== undefined ? cfg.ww_threshold : 0.94).toFixed(2)}</span>
            </div>
          </div>
          <div style="margin-top: 8px; display: flex; gap: 8px;">
            <button class="btn btn-sm" onclick="triggerDeviceWake('${dev.mac}')" title="Принудительно вызвать Порфирия без произнесения вейкворда (активировать прослушивание)" style="flex: 1.2; background: linear-gradient(135deg, #6366f1, #8b5cf6); color: #ffffff; font-size: 12px; font-weight: 600; padding: 7px 10px; border: none; border-radius: 8px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 5px; box-shadow: 0 2px 8px rgba(99, 102, 241, 0.3);">
              <span>⚡ Принудительный вызов</span>
            </button>
            <button class="btn btn-secondary btn-sm" onclick="playStreamerResponse('${dev.mac}')" title="Прослушать последний ответ Gemini для этого стримера" style="flex: 1; font-size: 12px; padding: 7px 10px;">
              ▶ Ответ Gemini
            </button>
          </div>
        ` : dev.device_type === 'yandex_station' ? (() => {
          const aliceDisabled = Boolean(cfg.disable_alice);
          if (!aliceDisabled) {
            checkStationVersionInBackground(dev.mac);
          }
          const currentVer = dev.firmware || (dev.config && dev.config.quasar_version) || '';
          const targetVer = dev.target_firmware || window.BUNDLED_QUASAR_VERSION || '1.2.4-porfiriy.20';
          const hasUpdate = dev.has_update !== false && (!currentVer || currentVer !== targetVer);
          const wwThr = cfg.ww_threshold !== undefined ? cfg.ww_threshold : 0.90;
          const wwCaptureOn = Boolean(cfg.ww_capture_enabled);
          const wwCapThr = cfg.ww_capture_threshold !== undefined ? cfg.ww_capture_threshold : 0.55;
          const micGain = cfg.mic_gain !== undefined ? cfg.mic_gain : 4.0;
          const pdmGain = cfg.pdm_gain !== undefined ? cfg.pdm_gain : 40;
          const dcOn = cfg.dc_blocker_enabled !== false;
          const aecOn = cfg.aec_enabled !== false;
          const bargeInOn = Boolean(cfg.barge_in_enabled);
          const followUpOn = Boolean(cfg.follow_up_enabled);
          const adaptiveVolOn = Boolean(cfg.adaptive_volume_enabled);
          const adaptiveVolMode = cfg.adaptive_volume_mode || 'medium';
          const ledBright = cfg.led_brightness !== undefined ? cfg.led_brightness : 80;
          return `
          <div class="virtual-client-banner" style="background: linear-gradient(135deg, ${aliceDisabled ? 'rgba(16, 185, 129, 0.18), rgba(59, 130, 246, 0.14)' : 'rgba(139, 92, 246, 0.18), rgba(99, 102, 241, 0.12)'}); border: 1px solid ${aliceDisabled ? 'rgba(16, 185, 129, 0.4)' : 'rgba(139, 92, 246, 0.35)'}; border-radius: 10px; padding: 10px 14px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
              <span style="font-weight: 700; font-size: 13px; color: ${aliceDisabled ? '#34d399' : '#c4b5fd'};">${aliceDisabled ? '🟢 Яндекс Станция 2 (Нативный Порфирий)' : '🟣 Яндекс Станция 2 (Алиса + Quasar)'}</span>
              ${!aliceDisabled ? `
                <span id="quasar-ver-badge-${dev.mac}" class="fw-badge ${hasUpdate ? 'fw-update-available' : 'fw-up-to-date'}" style="font-size: 10px; padding: 2px 7px;" title="Версия Quasar Bridge Porfiriy Edition">
                  <strong>${currentVer ? `v${escapeHtml(currentVer)}` : 'v1.2.3'}</strong>
                  ${hasUpdate ? `<span class="ota-device-badge" title="Доступен новый пакет v${targetVer}">➔ v${targetVer}</span>` : '<span style="color: var(--accent-green); font-size: 11px;" title="Актуальная версия">✓ актуальная</span>'}
                </span>
              ` : ''}
            </div>
            ${!aliceDisabled ? `
              <button id="quasar-update-btn-${dev.mac}" class="btn btn-sm ${hasUpdate ? 'quasar-update-btn-needed' : 'btn-secondary'}" onclick="updateQuasarComponents('${dev.mac}')" style="font-size: 11px; padding: 4px 10px; border-radius: 6px;" title="Обновить компоненты Quasar Bridge, Squeezelite и AirPlay по SSH с перепривязкой IP">
                ${hasUpdate ? `⚡ Обновить (v${targetVer})` : `✓ Quasar v${currentVer || targetVer}`}
              </button>
            ` : `
              <button class="btn btn-secondary btn-sm" onclick="controlStationd('${dev.mac}', 'deploy')" style="font-size: 11px; padding: 4px 10px; border-radius: 6px; color: #34d399; border-color: rgba(52, 211, 153, 0.4);" title="Залить скрипт porfiriy-stationd и ONNX модели на Станцию">
                ⚡ Обновить Stationd
              </button>
            `}
          </div>

          <!-- Переключатель режима: Алиса ⇄ Нативный Порфирий -->
          <div style="margin: 8px 0 4px 0; padding: 8px 10px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; display: flex; align-items: center; justify-content: space-between; gap: 8px;">
            <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 12px; font-weight: 600; color: #e2e8f0; margin: 0;">
              <input type="checkbox" ${aliceDisabled ? 'checked' : ''} onchange="toggleStationAlice('${dev.mac}', this.checked)">
              <span>Отключить Алису (локальный openWakeWord на Станции)</span>
            </label>
          </div>

          <!-- Players bar: Squeezelite & AirPlay -->
          <div style="margin: 6px 0 4px 0; display: flex; flex-direction: column; gap: 6px;">
            <div style="display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; background: rgba(56, 189, 248, 0.08); border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 8px;">
              <div style="display: flex; align-items: center; gap: 6px;">
                <span style="font-size: 12px; color: #38bdf8; font-weight: 600;">🎵 Squeezelite (MA):</span>
                <span id="sq-badge-${dev.mac}" class="badge ${dev.squeezelite_running !== false ? 'badge-success' : 'badge-error'}" style="font-size: 10px; padding: 2px 6px;">
                  ${dev.squeezelite_running !== false ? 'В сети' : 'Остановлен'}
                </span>
              </div>
              <button class="btn btn-secondary btn-sm" onclick="controlSqueezelite('${dev.mac}', 'restart')" style="font-size: 11px; padding: 2px 8px; color: #38bdf8; border-color: rgba(56, 189, 248, 0.4);" title="Перезапустить службу Squeezelite на Станции">
                🔄 Рестарт
              </button>
            </div>
            <div style="display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; background: rgba(168, 85, 247, 0.08); border: 1px solid rgba(168, 85, 247, 0.25); border-radius: 8px;">
              <div style="display: flex; align-items: center; gap: 6px;">
                <span style="font-size: 12px; color: #c084fc; font-weight: 600;">📻 Station AirPlay:</span>
                <span id="ap-badge-${dev.mac}" class="badge ${dev.airplay_running !== false ? 'badge-success' : 'badge-error'}" style="font-size: 10px; padding: 2px 6px;">
                  ${dev.airplay_running !== false ? 'В сети' : 'Остановлен'}
                </span>
              </div>
              <button class="btn btn-secondary btn-sm" onclick="controlAirplay('${dev.mac}', 'restart')" style="font-size: 11px; padding: 2px 8px; color: #c084fc; border-color: rgba(168, 85, 247, 0.4);" title="Перезапустить службу AirPlay на Станции">
                🔄 Рестарт
              </button>
            </div>
          </div>

          ${!aliceDisabled ? `
          <!-- Настройки Quasar Bridge (видны только когда Алиса включена) -->
          <div style="margin: 10px 0; display: flex; flex-direction: column; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 90px;">📡 Канал связи:</span>
              <select id="yandex-input-${dev.mac}" style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 4px 8px; border-radius: 6px; font-size: 12px;" onchange="saveYandexConfig('${dev.mac}', 'input_mode', this.value)">
                <option value="websocket" ${(cfg.input_mode !== 'http') ? 'selected' : ''}>📡 WebSocket (Живое аудио 16kHz)</option>
                <option value="http" ${(cfg.input_mode === 'http') ? 'selected' : ''}>📄 HTTP REST (Текст ASR)</option>
              </select>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 90px;">🗣️ Вывод:</span>
              <select id="yandex-mode-${dev.mac}" style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 4px 8px; border-radius: 6px; font-size: 12px;" onchange="saveYandexConfig('${dev.mac}', 'output_mode', this.value)">
                <option value="ssh_stream" ${((cfg.output_mode || 'ssh_stream') === 'ssh_stream' || cfg.output_mode === 'airplay') ? 'selected' : ''}>⚡ Прямой голос Порфирия (SSH ALSA)</option>
                <option value="alice_tts" ${(cfg.output_mode === 'alice_tts') ? 'selected' : ''}>🗣️ Голос Алисы (текст)</option>
              </select>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 90px;">⏱️ Диалог:</span>
              <select id="yandex-dialog-${dev.mac}" style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 4px 8px; border-radius: 6px; font-size: 12px;" onchange="saveYandexConfig('${dev.mac}', 'dialogue_timeout_s', parseInt(this.value, 10))">
                <option value="60" ${(cfg.dialogue_timeout_s === undefined || cfg.dialogue_timeout_s === 60) ? 'selected' : ''}>60 сек (Активный диалог)</option>
                <option value="30" ${(cfg.dialogue_timeout_s === 30) ? 'selected' : ''}>30 сек</option>
                <option value="0" ${(cfg.dialogue_timeout_s === 0) ? 'selected' : ''}>Выкл (Одноразовый)</option>
              </select>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 90px;">🎵 Плеер MASS:</span>
              <select id="yandex-player-${dev.mac}" style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 4px 8px; border-radius: 6px; font-size: 12px;" onchange="saveYandexConfig('${dev.mac}', 'mass_player', this.value); saveYandexConfig('${dev.mac}', 'airplay_player', this.value)">
                <option value="">— Не задан (Music Assistant) —</option>
                ${availableMediaPlayers.map(p => `<option value="${escapeHtml(p.entity_id)}" ${(cfg.mass_player === p.entity_id || cfg.airplay_player === p.entity_id) ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('')}
                ${(cfg.mass_player || cfg.airplay_player) && !availableMediaPlayers.some(p => p.entity_id === (cfg.mass_player || cfg.airplay_player)) ? `<option value="${escapeHtml(cfg.mass_player || cfg.airplay_player)}" selected>${escapeHtml(cfg.mass_player || cfg.airplay_player)}</option>` : ''}
              </select>
            </div>
            <div style="font-size: 11px; color: #34d399; display: flex; align-items: center; gap: 6px; padding: 6px 10px; background: rgba(52, 211, 153, 0.08); border: 1px solid rgba(52, 211, 153, 0.2); border-radius: 6px;">
              <span>⚡ Прямой канал SSH :2222 активен (ALSA raw PCM)</span>
            </div>
          </div>
          <div style="margin-top: 8px; display: flex; gap: 8px;">
            <button class="btn btn-sm" onclick="openQuasarRoutingModal('${dev.mac}')" title="Настроить правила маршрутизации и синонимы умного дома" style="flex: 1.2; background: linear-gradient(135deg, #8b5cf6, #6366f1); color: #ffffff; font-size: 12px; font-weight: 600; padding: 7px 10px; border: none; border-radius: 8px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 5px; box-shadow: 0 2px 8px rgba(139, 92, 246, 0.3);">
              <span>🎛️ Маршрутизация Quasar</span>
            </button>
            <button class="btn btn-secondary btn-sm" onclick="playStreamerResponse('${dev.mac}')" title="Прослушать последний ответ Gemini" style="flex: 1; font-size: 12px; padding: 7px 10px;">
              ▶ Ответ Gemini
            </button>
          </div>
          ` : `
          <!-- Нативный режим Порфирия: openWakeWord + отладка + LED (видны только при выключенной Алисе) -->
          <div style="margin: 10px 0; display: flex; flex-direction: column; gap: 8px;">
            <div style="display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.28); border-radius: 8px;">
              <div style="display: flex; align-items: center; gap: 6px;">
                <span style="font-size: 12px; color: #34d399; font-weight: 600;">🎙️ openWakeWord (Станция):</span>
                <span id="stationd-badge-${dev.mac}" class="badge ${dev.online ? 'badge-success' : 'badge-warning'}" style="font-size: 10px; padding: 2px 6px;">
                  ${dev.online ? 'Подключён' : 'Ожидание'}
                </span>
              </div>
              <div style="display: flex; gap: 4px;">
                <button class="btn btn-secondary btn-sm" onclick="controlStationd('${dev.mac}', 'restart')" style="font-size: 11px; padding: 2px 8px;">🔄 Рестарт</button>
                <label class="btn btn-secondary btn-sm" style="font-size: 11px; padding: 2px 8px; cursor: pointer; margin: 0;" title="Загрузить новый porfiriy.onnx на Станцию">
                  📁 .onnx
                  <input type="file" accept=".onnx" style="display:none" onchange="uploadStationWwModel('${dev.mac}', this)">
                </label>
              </div>
            </div>

            <!-- Порог openWakeWord и усиление микрофона -->
            <div class="slider-row">
              <span>🎯 Порог WW:</span>
              <input type="range" min="0.01" max="1.00" step="0.01" value="${wwThr}"
                oninput="this.nextElementSibling.innerText = parseFloat(this.value).toFixed(2)"
                onchange="updateSingleDeviceConfig('${dev.mac}', 'ww_threshold', parseFloat(this.value))">
              <span style="min-width: 40px; font-family: monospace; font-size: 12px; font-weight: 600;">${Number(wwThr).toFixed(2)}</span>
            </div>
            <div class="slider-row">
              <span>🎛️ PDM Gain (АЦП):</span>
              <input type="range" min="0" max="48" step="1" value="${pdmGain}"
                oninput="this.nextElementSibling.innerText = this.value + '/48'"
                onchange="updateSingleDeviceConfig('${dev.mac}', 'pdm_gain', parseInt(this.value, 10))"
                title="Аппаратный усилитель PDM-микрофонов Станции (0..48)">
              <span style="min-width: 40px; font-family: monospace; font-size: 12px; font-weight: 600;">${pdmGain}/48</span>
            </div>
            <div class="slider-row">
              <span>🎙️ Усиление (DSP):</span>
              <input type="range" min="0.5" max="15.0" step="0.5" value="${micGain}"
                oninput="this.nextElementSibling.innerText = parseFloat(this.value).toFixed(1) + 'x'"
                onchange="updateSingleDeviceConfig('${dev.mac}', 'mic_gain', parseFloat(this.value))"
                title="Программное усиление микрофона с мягким лимитером (0.5x - 15.0x)">
              <span style="min-width: 40px; font-family: monospace; font-size: 12px; font-weight: 600;">${Number(micGain).toFixed(1)}x</span>
            </div>
            <div style="display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: #cbd5e1; padding: 2px 4px;">
              <label style="display:flex; align-items:center; gap:5px; cursor:pointer; margin:0;">
                <input type="checkbox" ${dcOn ? 'checked' : ''} onchange="updateSingleDeviceConfig('${dev.mac}', 'dc_blocker_enabled', this.checked)">
                <span>DC-блокер PDM</span>
              </label>
              <label style="display:flex; align-items:center; gap:5px; cursor:pointer; margin:0;">
                <input type="checkbox" ${aecOn ? 'checked' : ''} onchange="updateSingleDeviceConfig('${dev.mac}', 'aec_enabled', this.checked)">
                <span>AEC (вычитание музыки)</span>
              </label>
              <label style="display:flex; align-items:center; gap:5px; cursor:pointer; margin:0;" title="Разрешить перебивать Порфирия словом «Порфирий» во время его речи (работает поверх AEC)">
                <input type="checkbox" ${bargeInOn ? 'checked' : ''} onchange="updateSingleDeviceConfig('${dev.mac}', 'barge_in_enabled', this.checked)">
                <span>⚡ Перебивание (Barge-in)</span>
              </label>
              <label style="display:flex; align-items:center; gap:5px; cursor:pointer; margin:0;" title="Оставлять микрофон открытым на 5 секунд после голосового ответа для продолжения диалога без вейкворда">
                <input type="checkbox" ${followUpOn ? 'checked' : ''} onchange="updateSingleDeviceConfig('${dev.mac}', 'follow_up_enabled', this.checked)">
                <span>💬 Follow-up (диалог)</span>
              </label>
              <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;" title="Только приглушает голос Порфирия при тихой речи пользователя (никогда не усиливает громче текущей шкалы колонки)">
                <label style="display:flex; align-items:center; gap:5px; cursor:pointer; margin:0;">
                  <input type="checkbox" ${adaptiveVolOn ? 'checked' : ''} onchange="updateSingleDeviceConfig('${dev.mac}', 'adaptive_volume_enabled', this.checked)">
                  <span>🔉 Адаптивная громкость</span>
                </label>
                <select onchange="updateSingleDeviceConfig('${dev.mac}', 'adaptive_volume_mode', this.value)" style="padding:2px 6px; font-size:11px; border-radius:6px; background:#0d1117; color:#cbd5e1; border:1px solid #30363d;">
                  <option value="weak" ${adaptiveVolMode === 'weak' ? 'selected' : ''}>Слабый (60–95%)</option>
                  <option value="medium" ${adaptiveVolMode === 'medium' ? 'selected' : ''}>Средний (38–90%)</option>
                  <option value="strong" ${adaptiveVolMode === 'strong' ? 'selected' : ''}>Сильный (22–80%)</option>
                </select>
              </div>
            </div>

            <!-- Встроенная мини-отладка openWakeWord прямо в карточке -->
            <div style="background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 8px 10px;">
              <div style="display: flex; justify-content: space-between; font-size: 11px; color: #8b949e; margin-bottom: 4px; font-family: monospace;">
                <span id="st-ww-val-${dev.mac}">Score: ${(dev.ww_score || 0).toFixed(2)} / Peak: ${(dev.ww_peak || 0).toFixed(2)}</span>
                <span id="st-ww-meta-${dev.mac}">RMS: ${dev.ww_rms_dbfs !== undefined ? dev.ww_rms_dbfs : -60} dBFS | ${dev.ww_infer_ms || '--'} ms</span>
              </div>
              <div style="position: relative; width: 100%; height: 8px; background: #1e293b; border-radius: 4px; overflow: hidden;">
                <div id="st-ww-bar-${dev.mac}" style="width: ${Math.min(100, Math.round((dev.ww_score || 0) * 100))}%; height: 100%; background: #34d399; transition: width 0.12s ease;"></div>
                ${wwCaptureOn ? `<div title="Порог захвата: ${Number(wwCapThr).toFixed(2)}" style="position: absolute; top: 0; bottom: 0; left: ${Math.min(100, Math.round(wwCapThr * 100))}%; width: 2px; background: #fbbf24;"></div>` : ''}
                <div title="Порог срабатывания: ${Number(wwThr).toFixed(2)}" style="position: absolute; top: 0; bottom: 0; left: ${Math.min(100, Math.round(wwThr * 100))}%; width: 2px; background: #f87171;"></div>
              </div>
              <div style="margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap;">
                <div style="width: 100%; display: flex; gap: 6px;">
                  <button class="btn btn-sm" onclick="triggerDeviceWake('${dev.mac}')" title="Принудительно вызвать Порфирия на Станции без вейкворда (начать слушать)" style="flex: 1; background: linear-gradient(135deg, #6366f1, #8b5cf6); color: #ffffff; font-size: 12px; font-weight: 600; padding: 6px 10px; border: none; border-radius: 7px; cursor: pointer; box-shadow: 0 2px 8px rgba(99, 102, 241, 0.3);">
                    ⚡ Вызов
                  </button>
                  <button class="btn btn-sm" onclick="triggerDeviceStop('${dev.mac}')" title="Мгновенно остановить речь Порфирия и сбросить в режим ожидания" style="background: rgba(239, 68, 68, 0.18); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.4); font-size: 12px; font-weight: 600; padding: 6px 12px; border-radius: 7px; cursor: pointer;">
                    ⏹ Стоп
                  </button>
                </div>
                <button class="btn btn-secondary btn-sm" onclick="dumpAndPlayStationMic('${dev.mac}')" style="flex: 1; font-size: 11px; padding: 4px 8px;" title="Получить последние 3 секунды очищенного после DSP/AEC аудио с микрофона">
                  🎙️ 3с (после AEC)
                </button>
                <button class="btn btn-secondary btn-sm" onclick="playLastUtterance('${dev.mac}')" style="font-size: 11px; padding: 4px 8px;" title="Прослушать последнюю фразу, которая реально улетела в Gemini после вейкворда">
                  💬 Реплика
                </button>
                <button class="btn btn-secondary btn-sm" onclick="playStreamerResponse('${dev.mac}')" style="font-size: 11px; padding: 4px 8px;" title="Прослушать последний ответ Gemini">
                  ▶ Ответ
                </button>
              </div>
            </div>

            <!-- Плеер Music Assistant -->
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 90px;">🎵 Плеер MASS:</span>
              <select id="yandex-player-${dev.mac}" style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 4px 8px; border-radius: 6px; font-size: 12px;" onchange="saveYandexConfig('${dev.mac}', 'mass_player', this.value); saveYandexConfig('${dev.mac}', 'airplay_player', this.value)">
                <option value="">— Не задан (Music Assistant) —</option>
                ${availableMediaPlayers.map(p => `<option value="${escapeHtml(p.entity_id)}" ${(cfg.mass_player === p.entity_id || cfg.airplay_player === p.entity_id) ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('')}
                ${(cfg.mass_player || cfg.airplay_player) && !availableMediaPlayers.some(p => p.entity_id === (cfg.mass_player || cfg.airplay_player)) ? `<option value="${escapeHtml(cfg.mass_player || cfg.airplay_player)}" selected>${escapeHtml(cfg.mass_player || cfg.airplay_player)}</option>` : ''}
              </select>
            </div>

            <!-- Подсветка Станции (LED) для отладки и обратной связи -->
            <details style="background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 6px 10px;">
              <summary style="cursor: pointer; font-size: 12px; font-weight: 600; color: #93c5fd;">💡 Подсветка Станции (LED-панель)</summary>
              <div style="margin-top: 8px; display: flex; flex-direction: column; gap: 6px; font-size: 12px;">
                <div class="slider-row">
                  <span>Яркость:</span>
                  <input type="range" min="0" max="255" step="5" value="${ledBright}"
                    oninput="this.nextElementSibling.innerText = this.value"
                    onchange="updateSingleDeviceConfig('${dev.mac}', 'led_brightness', parseInt(this.value, 10))">
                  <span style="min-width: 34px; font-family: monospace;">${ledBright}</span>
                </div>
                ${[
                  ['idle', 'Ожидание', '#000000', 0],
                  ['listen', 'Слушает', '#0088ff', 1],
                  ['think', 'Думает', '#ffaa00', 2],
                  ['speak', 'Говорит', '#00ff88', 1]
                ].map(([k, lbl, defCol, defMode]) => {
                  const mVal = cfg['led_mode_' + k] !== undefined ? cfg['led_mode_' + k] : defMode;
                  const cVal = cfg['led_color_' + k] || defCol;
                  return `
                  <div style="display:flex; align-items:center; gap:6px;">
                    <span style="min-width:68px; color:#94a3b8;">${lbl}:</span>
                    <select style="flex:1; background:#0d1117; border:1px solid #30363d; color:#f0f3f6; padding:2px 6px; border-radius:5px; font-size:11px;" onchange="updateSingleDeviceConfig('${dev.mac}', 'led_mode_${k}', parseInt(this.value, 10))">
                      <option value="0" ${mVal == 0 ? 'selected' : ''}>Выкл</option>
                      <option value="1" ${mVal == 1 ? 'selected' : ''}>Светится</option>
                      <option value="2" ${mVal == 2 ? 'selected' : ''}>Дышит</option>
                    </select>
                    <input type="color" value="${cVal}" style="width:32px; height:24px; border:none; background:none; cursor:pointer;" onchange="updateSingleDeviceConfig('${dev.mac}', 'led_color_${k}', this.value)">
                  </div>`;
                }).join('')}
              </div>
            </details>

            <!-- Известные Wi-Fi сети и серверы (Дом / Дача) -->
            <details style="background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 6px 10px;" ontoggle="if(this.open) loadStationWifi('${dev.mac}')">
              <summary style="cursor: pointer; font-size: 12px; font-weight: 600; color: #38bdf8;">📶 Известные Wi-Fi сети и серверы (Дом / Дача)</summary>
              <div style="margin-top: 8px; display: flex; flex-direction: column; gap: 8px; font-size: 12px;">
                <div id="st-wifi-list-${dev.mac}" style="display: flex; flex-direction: column; gap: 5px; color: #94a3b8;">
                  <span>Загрузка списка сетей со Станции...</span>
                </div>
                <div style="border-top: 1px solid rgba(255,255,255,0.08); padding-top: 8px; display: flex; flex-direction: column; gap: 6px;">
                  <div style="font-size: 11px; color: #94a3b8; font-weight: 600;">➕ Добавить / обновить сеть на Станции:</div>
                  <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px;">
                    <input type="text" id="st-wifi-ssid-${dev.mac}" placeholder="SSID (имя Wi-Fi)" style="background:#0d1117; border:1px solid #30363d; color:#f0f3f6; padding:5px 8px; border-radius:6px; font-size:12px;">
                    <input type="password" id="st-wifi-pass-${dev.mac}" placeholder="Пароль Wi-Fi" style="background:#0d1117; border:1px solid #30363d; color:#f0f3f6; padding:5px 8px; border-radius:6px; font-size:12px;">
                  </div>
                  <div style="display: grid; grid-template-columns: 1.4fr 0.6fr; gap: 6px;">
                    <input type="text" id="st-wifi-srv-${dev.mac}" placeholder="IP сервера Порфирия (напр. 192.168.1.50)" style="background:#0d1117; border:1px solid #30363d; color:#f0f3f6; padding:5px 8px; border-radius:6px; font-size:12px;">
                    <input type="number" id="st-wifi-prio-${dev.mac}" value="10" placeholder="Приоритет" title="Приоритет подключения (чем выше, тем главнее)" style="background:#0d1117; border:1px solid #30363d; color:#f0f3f6; padding:5px 8px; border-radius:6px; font-size:12px;">
                  </div>
                  <button type="button" class="btn btn-secondary btn-sm" onclick="saveStationWifi('${dev.mac}')" style="font-size: 11px; padding: 5px 10px; color: #38bdf8; border-color: rgba(56, 189, 248, 0.4);">
                    💾 Сохранить сеть на Станцию
                  </button>
                </div>
              </div>
            </details>

            <!-- Сбор звуков срабатываний вейкворда (для дообучения openWakeWord) -->
            <details style="background: rgba(251, 191, 36, 0.04); border: 1px solid ${wwCaptureOn ? 'rgba(251, 191, 36, 0.45)' : 'rgba(255,255,255,0.08)'}; border-radius: 8px; padding: 6px 10px;" ontoggle="if(this.open) loadWwCaptures('${dev.mac}')">
              <summary style="cursor: pointer; font-size: 12px; font-weight: 600; color: #fbbf24; display: flex; align-items: center; justify-content: space-between;">
                <span>🧪 Сбор звуков вейкворда (для обучения)</span>
                ${wwCaptureOn ? '<span style="font-size: 10px; background: rgba(239,68,68,0.22); color: #fca5a5; border: 1px solid rgba(239,68,68,0.45); padding: 1px 6px; border-radius: 10px; margin-left: 6px;">● ЗАПИСЬ</span>' : ''}
              </summary>
              <div style="margin-top: 8px; display: flex; flex-direction: column; gap: 8px; font-size: 12px;">
                <label style="display:flex; align-items:center; gap:7px; cursor:pointer; margin:0; color:#f1f5f9;">
                  <input type="checkbox" ${wwCaptureOn ? 'checked' : ''} onchange="updateSingleDeviceConfig('${dev.mac}', 'ww_capture_enabled', this.checked)">
                  <span>🎙️ Записывать 3с клипы (после AEC) на сервер HA</span>
                </label>

                <div class="slider-row">
                  <span>🎚️ Порог захвата:</span>
                  <input type="range" min="0.10" max="0.99" step="0.01" value="${wwCapThr}"
                    oninput="this.nextElementSibling.innerText = parseFloat(this.value).toFixed(2)"
                    onchange="updateSingleDeviceConfig('${dev.mac}', 'ww_capture_threshold', parseFloat(this.value))"
                    title="Отдельная шкала захвата: клипы с score от этого порога до порога WW сохраняются как почти-срабатывания (near-miss), а ≥ порога WW — как срабатывания">
                  <span style="min-width: 40px; font-family: monospace; font-size: 12px; font-weight: 600; color: #fbbf24;">${Number(wwCapThr).toFixed(2)}</span>
                </div>
                <div style="font-size: 11px; color: #94a3b8; line-height: 1.35;">
                  <span style="color:#f87171;">≥ ${Number(wwThr).toFixed(2)}</span> — реальные срабатывания;
                  <span style="color:#fbbf24;">${Number(wwCapThr).toFixed(2)}…${Number(wwThr).toFixed(2)}</span> — почти-срабатывания (near-miss, кулдаун 5с).
                </div>

                <div style="display: flex; align-items: center; justify-content: space-between; gap: 6px; flex-wrap: wrap; padding-top: 4px; border-top: 1px solid rgba(255,255,255,0.08);">
                  <span id="ww-cap-stats-${dev.mac}" style="font-size: 11px; color: #cbd5e1; font-family: monospace;">Собрано: —</span>
                  <div style="display: flex; gap: 4px;">
                    <button type="button" class="btn btn-secondary btn-sm" onclick="loadWwCaptures('${dev.mac}')" style="font-size: 11px; padding: 3px 7px;" title="Обновить список">🔄</button>
                    <button type="button" class="btn btn-secondary btn-sm" onclick="downloadWwCapturesArchive('${dev.mac}')" style="font-size: 11px; padding: 3px 8px; color: #34d399; border-color: rgba(52, 211, 153, 0.4);" title="Скачать ZIP-архив (wav/ + metadata.csv)">📦 Архив (.zip)</button>
                    <button type="button" class="btn btn-secondary btn-sm" onclick="clearWwCaptures('${dev.mac}')" style="font-size: 11px; padding: 3px 8px; color: #fca5a5; border-color: rgba(239, 68, 68, 0.35);" title="Удалить все собранные записи этой колонки">🗑️</button>
                  </div>
                </div>

                <div id="ww-cap-list-${dev.mac}" style="max-height: 190px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 6px;">
                  <span style="color: #8b949e; font-size: 11px;">Загрузка записей...</span>
                </div>
              </div>
            </details>
          </div>
          `}
          `;
        })() : dev.device_type === 'wear_os' ? (() => {
          const watchColors = [
            ['idle', 'Покой', '#000000'],
            ['listen', 'Слушает', '#0000ff'],
            ['think', 'Думает', '#ffaa00'],
            ['speak', 'Отвечает', '#00ff00'],
          ];
          const boundUid = cfg.user_id || '';
          const curArea = dev.area_name || '';
          const lastReply = (dev.last_reply_text || '').trim();
          return `
          <div class="virtual-client-banner" style="background: linear-gradient(135deg, rgba(56, 189, 248, 0.16), rgba(99, 102, 241, 0.14)); border: 1px solid rgba(56, 189, 248, 0.35); border-radius: 10px; padding: 10px 14px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
              <span style="font-weight: 700; font-size: 13px; color: #7dd3fc;">⌚ Pixel Watch 2 (Wear OS)</span>
              <span class="fw-badge ${dev.has_update ? 'fw-update-available' : 'fw-up-to-date'}" style="font-size: 10px; padding: 2px 7px;">
                <strong>v${escapeHtml(dev.firmware || '1.0.0')}</strong>
                ${dev.has_update ? `<span class="ota-device-badge" title="Доступна новая версия v${escapeHtml(dev.target_firmware || '')}">➔ v${escapeHtml(dev.target_firmware || '')}</span>` : '<span style="color: var(--accent-green); font-size: 11px;">✓</span>'}
              </span>
            </div>
            <div style="display: flex; align-items: center; gap: 6px;">
              ${dev.has_update && dev.is_online ? `
                <button class="btn btn-sm ota-pulse-btn" onclick="triggerDeviceOta('${dev.mac}')" style="font-size: 11px; padding: 3px 9px; border-radius: 6px;" title="Отправить обновление APK на часы по WebSocket">
                  ⚡ Обновить
                </button>
              ` : ''}
              ${dev.apk_available ? `
                <a href="${API_BASE}/api/watch/apk/download" download="porfiriy-watch.apk" class="btn btn-secondary btn-sm" style="font-size: 11px; padding: 3px 8px; border-radius: 6px; text-decoration: none;" title="Скачать актуальный APK для установки с телефона">
                  ⬇ APK
                </a>
              ` : ''}
            </div>
          </div>

          <div style="margin: 10px 0 6px 0; display: flex; flex-direction: column; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 106px;">👤 Пользователь:</span>
              <select style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 5px 8px; border-radius: 6px; font-size: 12px;"
                onchange="updateSingleDeviceConfig('${dev.mac}', 'user_id', this.value)">
                <option value="">— Не привязан (авто) —</option>
                ${(porfiriyUsers || []).map(u => `<option value="${escapeHtml(u.id)}" ${boundUid === u.id ? 'selected' : ''}>${escapeHtml(u.name)}${u.telegram_chat_id ? ' ✈️' : ''}</option>`).join('')}
                ${boundUid && !(porfiriyUsers || []).some(u => u.id === boundUid) ? `<option value="${escapeHtml(boundUid)}" selected>${escapeHtml(boundUid)}</option>` : ''}
              </select>
            </div>

            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 106px;">📍 Комната (опц.):</span>
              ${availableAreas.length > 0 ? `
                <select style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 5px 8px; border-radius: 6px; font-size: 12px;"
                  onchange="updateSingleDeviceConfig('${dev.mac}', 'area_name', this.value)">
                  <option value="">— Без комнаты —</option>
                  ${availableAreas.map(a => {
                    const aName = typeof a === 'string' ? a : (a.name || a.area_id || '');
                    return `<option value="${escapeHtml(aName)}" ${curArea === aName ? 'selected' : ''}>${escapeHtml(aName)}</option>`;
                  }).join('')}
                  ${curArea && !availableAreas.some(a => (typeof a === 'string' ? a : (a.name || a.area_id || '')) === curArea) ? `<option value="${escapeHtml(curArea)}" selected>${escapeHtml(curArea)}</option>` : ''}
                </select>
              ` : `
                <input type="text" value="${escapeHtml(curArea)}" placeholder="Не привязана"
                  style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 5px 8px; border-radius: 6px; font-size: 12px;"
                  onchange="updateSingleDeviceConfig('${dev.mac}', 'area_name', this.value.trim())">
              `}
            </div>

            <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 8px 10px;">
              <div style="font-size: 11px; color: #94a3b8; font-weight: 600; margin-bottom: 6px;">🎨 Цвета анимации (LED):</div>
              <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px 10px;">
                ${watchColors.map(([k, lbl, defCol]) => {
                  const cVal = cfg['led_color_' + k] || defCol;
                  return `
                    <label style="display: flex; align-items: center; justify-content: space-between; font-size: 11px; color: #cbd5e1; margin: 0; cursor: pointer;">
                      <span>${lbl}:</span>
                      <input type="color" value="${cVal}" style="width: 30px; height: 22px; border: none; background: none; cursor: pointer;"
                        onchange="updateSingleDeviceConfig('${dev.mac}', 'led_color_${k}', this.value)">
                    </label>
                  `;
                }).join('')}
              </div>
            </div>

            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 106px;">🔑 Токен доступа:</span>
              <input type="password" id="watch-token-${dev.mac}" value="${escapeHtml(cfg.api_key || '')}" placeholder="Пусто = без проверки"
                style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 5px 8px; border-radius: 6px; font-size: 12px; font-family: monospace;"
                onchange="updateSingleDeviceConfig('${dev.mac}', 'api_key', this.value.trim())">
              <button type="button" class="btn btn-secondary btn-sm" style="padding: 4px 8px; font-size: 11px;" title="Показать/скрыть токен"
                onclick="const inp = document.getElementById('watch-token-${dev.mac}'); if (inp) inp.type = inp.type === 'password' ? 'text' : 'password';">👁️</button>
            </div>

            ${lastReply ? `
              <div style="background: rgba(56, 189, 248, 0.06); border: 1px solid rgba(56, 189, 248, 0.18); border-radius: 8px; padding: 7px 10px; font-size: 11px; color: #cbd5e1; line-height: 1.35;">
                <span style="color: #7dd3fc; font-weight: 600;">💬 Последний ответ:</span> ${escapeHtml(lastReply.length > 180 ? lastReply.slice(0, 180) + '…' : lastReply)}
              </div>
            ` : ''}
          </div>
          `;
        })() : `
          <div class="virtual-client-banner">
            <span>💻 Клиент (${escapeHtml(dev.device_type || 'ПК')})</span>
            <span class="virtual-tag">Виртуальное устройство</span>
          </div>
        `}

        <div class="dev-meta">
          <div>IP: ${isEsp && dev.ip && dev.ip !== '---' ? `<a href="http://${dev.ip}" target="_blank" class="dev-ip-link" title="Открыть страницу колонки">${dev.ip} ↗</a>` : (dev.ip || '---')}${dev.device_type === 'yandex_station' ? ` <button onclick="editDeviceIp('${dev.mac}')" title="Изменить IP-адрес Станции" style="background:none;border:none;cursor:pointer;font-size:11px;padding:0 2px;" class="btn-hover-scale">✏️</button>` : ''}</div>
          <div>MAC: ${dev.mac}</div>
          <div>Аптайм: ${formatUptime(dev.uptime)}</div>
          <div>Тип: ${dev.device_type || 'ESP32'}</div>
          ${isEsp ? `
            <div style="grid-column: 1 / -1; display: flex; align-items: center; gap: 6px;">
              <span>Прошивка:</span>
              <span class="fw-badge ${dev.has_update ? 'fw-update-available' : 'fw-up-to-date'}">
                <strong>v${escapeHtml(dev.firmware || '0.0.54')}</strong>
                ${dev.has_update ? `<span class="ota-device-badge" title="Доступна новая прошивка v${dev.target_firmware}">➔ v${dev.target_firmware}</span>` : '<span style="color: var(--accent-green); font-size: 11px;" title="Актуальная версия">✓ актуальная</span>'}
              </span>
            </div>
          ` : dev.device_type === 'yandex_station' ? (() => {
            const curVer = dev.firmware || (dev.config && dev.config.quasar_version) || '';
            const tgtVer = dev.target_firmware || window.BUNDLED_QUASAR_VERSION || '1.2.4-porfiriy.19';
            const needsUp = dev.has_update !== false && (!curVer || curVer !== tgtVer);
            return `
            <div style="grid-column: 1 / -1; display: flex; align-items: center; gap: 6px;">
              <span>Пакет Станции:</span>
              <span class="fw-badge ${needsUp ? 'fw-update-available' : 'fw-up-to-date'}">
                <strong>v${escapeHtml(curVer || '1.2.3')}</strong>
                ${needsUp ? `<span class="ota-device-badge" title="Доступен новый пакет ${tgtVer}">➔ ${tgtVer}</span>` : '<span style="color: var(--accent-green); font-size: 11px;" title="Актуальная версия">✓ актуальная</span>'}
              </span>
            </div>
            `;
          })() : ''}
        </div>

        ${dev.ota_status && dev.ota_status !== 'error' ? `
          <div class="ota-progress-box">
            <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;">
              <span>⚡ OTA: <strong>${dev.ota_status === 'rebooting' ? 'Перезагрузка...' : (dev.ota_status === 'starting' ? 'Подготовка...' : 'Прошивка...')}</strong></span>
              <span style="font-family: monospace; font-weight:700;">${dev.ota_progress || 0}%</span>
            </div>
            <div class="ota-progress-bar-wrap">
              <div class="ota-progress-bar" style="width: ${dev.ota_progress || 0}%;"></div>
            </div>
          </div>
        ` : ''}

        ${dev.device_type !== 'wear_os' ? `
        <div class="quick-controls">
          ${dev.device_type === 'yandex_station' ? (() => {
            const playerVol = Math.min(1.0, Math.max(0.0, cfg.speaker_volume !== undefined ? cfg.speaker_volume : 0.5));
            return `
              <div class="slider-row">
                <span>🔊 Громкость:</span>
                <input type="range" min="0" max="1" step="0.05" value="${playerVol}" 
                  onchange="updateSingleDeviceConfig('${dev.mac}', 'speaker_volume', parseFloat(this.value))"
                  oninput="this.nextElementSibling.innerText = Math.round(this.value * 100) + '%'">
                <span style="min-width: 40px; font-family: monospace; font-size: 12px; font-weight: 600;">${Math.round(playerVol * 100)}%</span>
              </div>
            `;
          })() : `
            <div class="slider-row">
              <span>🔊 Громкость:</span>
              <input type="range" min="0.1" max="2.0" step="0.05" value="${cfg.speaker_volume !== undefined ? cfg.speaker_volume : 0.5}" 
                onchange="updateSingleDeviceConfig('${dev.mac}', 'speaker_volume', parseFloat(this.value))"
                oninput="this.nextElementSibling.innerText = Math.round(this.value * 100) + '%'">
              <span style="min-width: 40px; font-family: monospace; font-size: 12px; font-weight: 600;">${volumePercent}%</span>
            </div>
          `}

          ${isEsp ? `
            <div class="slider-row">
              <span>🎙️ Чувствительность:</span>
              <input type="range" min="0.5" max="10.0" step="0.1" value="${cfg.mic_gain !== undefined ? cfg.mic_gain : 2.0}" 
                onchange="updateSingleDeviceConfig('${dev.mac}', 'mic_gain', parseFloat(this.value))"
                oninput="this.nextElementSibling.innerText = parseFloat(this.value).toFixed(1) + 'x'"
                title="Усиление микрофона (по умолчанию 2.0x, диапазон 0.5x - 10.0x)">
              <span style="min-width: 40px; font-family: monospace; font-size: 12px; font-weight: 600;">${(cfg.mic_gain !== undefined ? cfg.mic_gain : 2.0).toFixed(1)}x</span>
            </div>
          ` : ''}
        </div>
        ` : ''}

        ${dev.device_type !== 'yandex_station' ? `
          <div class="mic-debug-box" id="mic-box-${dev.mac}">
            <div class="mic-debug-header">
              <span class="mic-debug-title">🎙️ Прослушать микрофон</span>
              <span class="mic-debug-status" id="mic-status-${dev.mac}">${micRecState && micRecState.statusText ? micRecState.statusText : ''}</span>
            </div>
            <div class="mic-debug-controls">
              ${isEsp ? `
                <button class="btn btn-secondary btn-sm mic-btn" id="btn-mictest-${dev.mac}" onclick="startMicTest('${dev.mac}', 5)" title="Записать 5 секунд звука с микрофона и сразу прослушать" ${micRecState && micRecState.isTesting ? 'disabled' : ''}>
                  ${micRecState && micRecState.buttonText ? micRecState.buttonText : '⏺ Тест 5 сек'}
                </button>
              ` : ''}
              <button class="btn btn-secondary btn-sm mic-btn" id="btn-lastutt-${dev.mac}" onclick="playLastUtterance('${dev.mac}')" title="Прослушать последнюю распознанную живую реплику">
                💬 Реплика
              </button>
            </div>
            <div class="mic-player-wrap" id="mic-player-wrap-${dev.mac}" style="${micRecState && micRecState.audioSrc ? 'display: flex;' : 'display: none;'}">
              <audio controls class="mic-audio-element" id="mic-audio-${dev.mac}" src="${micRecState ? (micRecState.audioSrc || '') : ''}"></audio>
              <div class="mic-metrics" id="mic-metrics-${dev.mac}">
                ${micRecState && micRecState.metricsHtml ? micRecState.metricsHtml : ''}
              </div>
            </div>
          </div>
        ` : ''}

        ${(dev.device_type !== 'pc_streamer' && dev.device_type !== 'yandex_station' && dev.device_type !== 'wear_os') ? `
          <div class="card-actions">
            <button class="btn btn-secondary btn-icon" onclick="triggerDeviceWake('${dev.mac}')" title="Принудительно вызвать Порфирия (начать слушать)" style="color: #a78bfa; border-color: rgba(167, 139, 250, 0.4); font-weight: 600;">
              ⚡ Вызов
            </button>
            ${isEsp ? `
              <button class="btn btn-secondary btn-icon" onclick="triggerDeviceAction('${dev.mac}', 'beep')" title="Проиграть звуковой сигнал">
                🔔 Звук
              </button>
              <button class="btn btn-secondary btn-icon" onclick="triggerDeviceAction('${dev.mac}', 'reboot')" title="Перезагрузить плату">
                🔄 Рестарт
              </button>
              ${dev.has_update && dev.is_online ? `
                <button class="btn btn-icon ota-pulse-btn" onclick="triggerDeviceOta('${dev.mac}')" title="Доступна новая прошивка v${dev.target_firmware}! Обновить по воздуху">
                  ⚡ OTA (v${dev.target_firmware})
                </button>
              ` : ''}
              ${dev.ip && dev.ip !== '---' ? `
                <a href="http://${dev.ip}" target="_blank" class="btn btn-secondary btn-icon dev-web-btn" title="Встроенный Web UI колонки">
                  🌐 Web UI
                </a>
              ` : ''}
            ` : ''}
          </div>
        ` : ''}

        ${isEsp ? `
          <!-- Сворачиваемый блок тонкой настройки -->
          ${(() => {
            const isAdvancedOpen = !!(window.cardAccordionOpen && window.cardAccordionOpen[dev.mac]);
            return `
              <button type="button" class="card-advanced-toggle ${isAdvancedOpen ? 'open' : ''}" id="card-adv-toggle-${dev.mac}" onclick="toggleCardAdvanced('${dev.mac}', event)" style="background: rgba(255, 255, 255, 0.05); color: #cdd6f4; border: 1px solid rgba(255, 255, 255, 0.12); width: 100%; display: flex; align-items: center; justify-content: space-between; padding: 8px 12px; border-radius: 8px; cursor: pointer; font-size: 12px; font-weight: 600; box-sizing: border-box; margin-top: 6px;">
                <span>⚙️ Тонкая настройка</span>
                <span class="adv-arrow" id="card-adv-arrow-${dev.mac}" style="display: inline-block; transition: transform 0.2s; font-size: 10px; ${isAdvancedOpen ? 'transform: rotate(180deg);' : ''}">▼</span>
              </button>
              <div class="card-advanced-body ${isAdvancedOpen ? 'open' : ''}" id="card-adv-body-${dev.mac}" style="${isAdvancedOpen ? 'display: flex;' : 'display: none;'}">

                <div class="slider-row">
                  <span>🧠 Режим вейкворда:</span>
                  <select style="background: #1e222b; border: 1px solid #3b4252; color: #fff; padding: 4px 8px; border-radius: 6px; font-size: 11px; margin-left: auto;" 
                    onchange="updateSingleDeviceConfig('${dev.mac}', 'wake_word_mode', this.value)">
                    <option value="local" ${cfg.wake_word_mode !== 'server' ? 'selected' : ''}>Локальный (TFLite на ESP32)</option>
                    <option value="server" ${cfg.wake_word_mode === 'server' ? 'selected' : ''}>Серверный (openWakeWord)</option>
                  </select>
                </div>
                ${cfg.wake_word_mode === 'server' ? `
                  <div class="slider-row">
                    <span>🎯 Порог WW:</span>
                    <input type="range" min="0.01" max="1.00" step="0.01" value="${cfg.ww_threshold !== undefined ? cfg.ww_threshold : 0.94}" 
                      onchange="updateSingleDeviceConfig('${dev.mac}', 'ww_threshold', parseFloat(this.value))"
                      oninput="this.nextElementSibling.innerText = parseFloat(this.value).toFixed(2)">
                    <span style="min-width: 40px; font-family: monospace; font-size: 12px;">${Number(cfg.ww_threshold !== undefined ? cfg.ww_threshold : 0.94).toFixed(2)}</span>
                  </div>
                  <div style="font-size: 11px; color: #58a6ff; margin: 4px 0 6px 0; padding: 5px 8px; background: rgba(88, 166, 255, 0.08); border-radius: 6px; display: flex; align-items: center; gap: 6px;">
                    <span>ℹ️ Монитор отклика и отладка открыты на вкладке <strong>🧠 Интеллект</strong></span>
                  </div>
                ` : `
                  <div class="slider-row">
                    <span>🎯 Порог вейкворда:</span>
                    <input type="range" min="0.30" max="0.99" step="0.01" value="${cfg.wake_word_threshold || 0.93}" 
                      onchange="updateSingleDeviceConfig('${dev.mac}', 'wake_word_threshold', parseFloat(this.value))"
                      oninput="this.nextElementSibling.innerText = this.value">
                    <span style="min-width: 40px; font-family: monospace; font-size: 12px;">${cfg.wake_word_threshold || 0.93}</span>
                  </div>
                `}
                <div class="slider-row">
                  <span>🛑 RMS перебивания:</span>
                  <input type="number" class="rms-threshold-input" 
                    placeholder="${globalOptions.barge_in_threshold_rms || 600} (общ)" 
                    value="${cfg.barge_in_threshold_rms !== undefined && cfg.barge_in_threshold_rms !== null ? cfg.barge_in_threshold_rms : ''}"
                    onchange="updateSingleDeviceConfig('${dev.mac}', 'barge_in_threshold_rms', this.value ? parseInt(this.value, 10) : null)"
                    title="Индивидуальный порог RMS перебивания. Пустое поле = использовать общий (${globalOptions.barge_in_threshold_rms || 600})">
                </div>
                <div class="slider-row">
                  <span>🎛️ Лимитер звука (AGC):</span>
                  <label class="checkbox-label" style="margin-left: auto; cursor: pointer; gap: 6px;">
                    <input type="checkbox" ${cfg.mic_limiter_enabled !== false ? 'checked' : ''} 
                      onchange="updateSingleDeviceConfig('${dev.mac}', 'mic_limiter_enabled', this.checked); this.nextElementSibling.innerText = this.checked ? 'Вкл' : 'Выкл'; this.nextElementSibling.style.color = this.checked ? 'var(--accent-green)' : '#888';"
                      title="WebRTC Peak Limiter: авто-выравнивание микрофона, устраняет клиппинг и хрип при громких звуках">
                    <span style="font-size: 11px; font-weight: 700; color: ${cfg.mic_limiter_enabled !== false ? 'var(--accent-green)' : '#888'};">
                      ${cfg.mic_limiter_enabled !== false ? 'Вкл' : 'Выкл'}
                    </span>
                  </label>
                </div>
                <div class="slider-row">
                  <span>⏱️ Пауза конца фразы:</span>
                  <input type="range" min="300" max="2000" step="50" value="${cfg.silence_timeout_ms !== undefined ? cfg.silence_timeout_ms : 700}" 
                    onchange="updateSingleDeviceConfig('${dev.mac}', 'silence_timeout_ms', parseInt(this.value, 10))"
                    oninput="this.nextElementSibling.innerText = this.value + ' мс'"
                    title="Таймаут тишины после речи перед отправкой вопроса (по умолч. 700 мс)">
                  <span style="min-width: 50px; font-family: monospace; font-size: 12px;">${cfg.silence_timeout_ms !== undefined ? cfg.silence_timeout_ms : 700} мс</span>
                </div>
                <div class="slider-row">
                  <span>🎙️ Порог голоса (RMS):</span>
                  <input type="range" min="50" max="800" step="10" value="${cfg.silence_threshold_energy !== undefined ? cfg.silence_threshold_energy : 180}" 
                    onchange="updateSingleDeviceConfig('${dev.mac}', 'silence_threshold_energy', parseInt(this.value, 10))"
                    oninput="this.nextElementSibling.innerText = this.value"
                    title="Минимальная энергия звука для фиксации речи (по умолч. 180)">
                  <span style="min-width: 40px; font-family: monospace; font-size: 12px;">${cfg.silence_threshold_energy !== undefined ? cfg.silence_threshold_energy : 180}</span>
                </div>
                <div class="slider-row">
                  <span>⏳ Ожидание команды:</span>
                  <input type="range" min="3" max="15" step="1" value="${cfg.listen_timeout_s !== undefined ? cfg.listen_timeout_s : 6}" 
                    onchange="updateSingleDeviceConfig('${dev.mac}', 'listen_timeout_s', parseInt(this.value, 10))"
                    oninput="this.nextElementSibling.innerText = this.value + ' с'"
                    title="Максимальное время ожидания вопроса после вейкворда (по умолч. 6 с)">
                  <span style="min-width: 40px; font-family: monospace; font-size: 12px;">${cfg.listen_timeout_s !== undefined ? cfg.listen_timeout_s : 6} с</span>
                </div>
                <div class="slider-row">
                  <span>🔇 Приглушать медиа:</span>
                  <label class="checkbox-label" style="margin-left: auto; cursor: pointer; gap: 6px;">
                    <input type="checkbox" ${cfg.enable_ducking !== false ? 'checked' : ''} 
                      onchange="updateSingleDeviceConfig('${dev.mac}', 'enable_ducking', this.checked); this.nextElementSibling.innerText = this.checked ? 'Вкл' : 'Выкл'; this.nextElementSibling.style.color = this.checked ? 'var(--accent-green)' : '#888';"
                      title="Разрешить приглушать медиаплееры при разговоре через эту колонку">
                    <span style="font-size: 11px; font-weight: 700; color: ${cfg.enable_ducking !== false ? 'var(--accent-green)' : '#888'};">
                      ${cfg.enable_ducking !== false ? 'Вкл' : 'Выкл'}
                    </span>
                  </label>
                </div>
                <!-- Маршрутизация звука ответа -->
                <div class="audio-output-box" style="margin-top: 4px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; padding: 8px 10px;">
                  <div style="font-size: 11px; color: #8b949e; text-transform: uppercase; font-weight: 700; margin-bottom: 6px;">🔊 Вывод звука ответа:</div>
                  <div style="display: flex; gap: 12px; margin-bottom: ${cfg.audio_output_mode === 'external_player' ? '8px' : '2px'}; font-size: 12px;">
                    <label style="display: flex; align-items: center; gap: 4px; cursor: pointer;">
                      <input type="radio" name="out_mode_${dev.mac}" value="stream" ${cfg.audio_output_mode !== 'external_player' ? 'checked' : ''} 
                        onchange="updateSingleDeviceConfig('${dev.mac}', 'audio_output_mode', 'stream')">
                      <span>Встроенный динамик</span>
                    </label>
                    <label style="display: flex; align-items: center; gap: 4px; cursor: pointer;">
                      <input type="radio" name="out_mode_${dev.mac}" value="external_player" ${cfg.audio_output_mode === 'external_player' ? 'checked' : ''} 
                        onchange="updateSingleDeviceConfig('${dev.mac}', 'audio_output_mode', 'external_player')">
                      <span>Внешний плеер HA</span>
                    </label>
                  </div>
                  ${cfg.audio_output_mode === 'external_player' ? `
                    <div style="display: flex; align-items: center; gap: 6px;">
                      <select style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 5px 8px; border-radius: 6px; font-size: 12px;" 
                        onchange="updateSingleDeviceConfig('${dev.mac}', 'response_player', this.value)">
                        <option value="">— Выберите медиаплеер HA —</option>
                        ${availableMediaPlayers.map(p => `<option value="${escapeHtml(p.entity_id)}" ${(cfg.response_player === p.entity_id) ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('')}
                        ${cfg.response_player && !availableMediaPlayers.some(p => p.entity_id === cfg.response_player) ? `<option value="${escapeHtml(cfg.response_player)}" selected>${escapeHtml(cfg.response_player)}</option>` : ''}
                      </select>
                    </div>
                  ` : ''}
                </div>
                <div style="font-size: 11px; color: #a78bfa; margin-top: 6px; padding: 6px 10px; background: rgba(167, 139, 250, 0.08); border-radius: 6px; display: flex; flex-direction: column; gap: 3px;">
                  <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span>🎵 <strong>Медиаплеер HA (DLNA):</strong></span>
                    <button type="button" class="btn btn-secondary btn-sm" style="font-size: 10px; padding: 2px 6px;" 
                      onclick="navigator.clipboard.writeText(location.origin + '/dmr/${dev.mac}/description.xml'); showToast('URL плеера скопирован в буфер!');">
                      📋 Копировать
                    </button>
                  </div>
                  <span style="font-family: monospace; font-size: 10px; color: #8b949e; word-break: break-all;">
                    /dmr/${dev.mac}/description.xml
                  </span>
                </div>
                ${!dev.has_update && isEsp && dev.is_online ? `
                  <div style="margin-top: 6px; display: flex; justify-content: flex-end;">
                    <button class="btn btn-secondary btn-sm" onclick="triggerDeviceOta('${dev.mac}')" style="font-size: 11px; padding: 4px 8px; opacity: 0.75;" title="Принудительно перепрошить прошивку по воздуху">
                      ⚡ Принудительно прошить OTA
                    </button>
                  </div>
                ` : ''}
              </div>
            `;
          })()}
        ` : ''}
      </div>
    `;
  }).join('');
}

// Глобальное состояние аккордеонов карточек
window.cardAccordionOpen = window.cardAccordionOpen || {};
window.toggleCardAdvanced = function(mac, event) {
  if (event) {
    try { event.preventDefault(); event.stopPropagation(); } catch (_) {}
  }
  window.cardAccordionOpen[mac] = !window.cardAccordionOpen[mac];
  const isOpen = !!window.cardAccordionOpen[mac];
  const body = document.getElementById(`card-adv-body-${mac}`);
  const btn = document.getElementById(`card-adv-toggle-${mac}`);
  const arrow = document.getElementById(`card-adv-arrow-${mac}`);
  if (body) {
    body.style.display = isOpen ? 'flex' : 'none';
    if (isOpen) {
      body.classList.add('open');
      if (btn) btn.classList.add('open');
    } else {
      body.classList.remove('open');
      if (btn) btn.classList.remove('open');
    }
  }
  if (arrow) {
    arrow.style.transform = isOpen ? 'rotate(180deg)' : 'rotate(0deg)';
  }
};


// Глобальное состояние записей микрофонов колонок
window.micRecordingsState = window.micRecordingsState || {};
window.micTestTimers = window.micTestTimers || {};

// Запуск теста микрофона на N секунд
window.startMicTest = async function(mac, duration_s = 5) {
  if (window.micTestTimers[mac]) {
    clearInterval(window.micTestTimers[mac]);
    delete window.micTestTimers[mac];
  }

  window.micRecordingsState[mac] = window.micRecordingsState[mac] || {};
  window.micRecordingsState[mac].isTesting = true;
  window.micRecordingsState[mac].buttonText = `⏳ Запись... ${duration_s}с`;
  window.micRecordingsState[mac].statusText = `<span class="mic-recording-pulse">● Идет запись...</span>`;

  const updateBtnText = (text, disabled = true) => {
    window.micRecordingsState[mac].buttonText = text;
    window.micRecordingsState[mac].isTesting = disabled;
    const b = document.getElementById(`btn-mictest-${mac}`);
    if (b) {
      b.disabled = disabled;
      b.innerText = text;
    }
  };

  const updateStatus = (html) => {
    window.micRecordingsState[mac].statusText = html;
    const s = document.getElementById(`mic-status-${mac}`);
    if (s) s.innerHTML = html;
  };

  updateBtnText(`⏳ Запись... ${duration_s}с`, true);
  updateStatus(`<span class="mic-recording-pulse">● Идет запись...</span>`);

  try {
    const res = await fetch(`${API_BASE}/api/devices/${encodeURIComponent(mac)}/mic_test/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ duration_s })
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.message || 'Ошибка запуска теста');
    }

    let remaining = Math.round(duration_s);
    updateBtnText(`⏳ Запись... ${remaining}с`, true);

    window.micTestTimers[mac] = setInterval(() => {
      remaining -= 1;
      if (remaining > 0) {
        updateBtnText(`⏳ Запись... ${remaining}с`, true);
      } else {
        clearInterval(window.micTestTimers[mac]);
        delete window.micTestTimers[mac];
        updateBtnText(`⏳ Анализ...`, true);
        updateStatus(`Сборка WAV...`);
        setTimeout(() => checkMicTestReady(mac), 1000);
      }
    }, 1000);

  } catch (err) {
    if (window.micTestTimers[mac]) {
      clearInterval(window.micTestTimers[mac]);
      delete window.micTestTimers[mac];
    }
    updateBtnText('⏺ Тест 5 сек', false);
    updateStatus(`<span style="color: var(--accent-red);">Ошибка</span>`);
    showToast(`Ошибка старта записи: ${err.message}`, true);
  }
};

// Проверка готовности аудио и отображение плеера
window.checkMicTestReady = async function(mac) {
  if (window.micTestTimers[mac]) {
    clearInterval(window.micTestTimers[mac]);
    delete window.micTestTimers[mac];
  }

  window.micRecordingsState[mac] = window.micRecordingsState[mac] || {};
  window.micRecordingsState[mac].isTesting = false;
  window.micRecordingsState[mac].buttonText = '⏺ Тест 5 сек';

  const btn = document.getElementById(`btn-mictest-${mac}`);
  const statusEl = document.getElementById(`mic-status-${mac}`);
  const wrapEl = document.getElementById(`mic-player-wrap-${mac}`);
  const audioEl = document.getElementById(`mic-audio-${mac}`);
  const metricsEl = document.getElementById(`mic-metrics-${mac}`);

  if (btn) {
    btn.disabled = false;
    btn.innerText = '⏺ Тест 5 сек';
  }

  try {
    const res = await fetch(`${API_BASE}/api/devices/${encodeURIComponent(mac)}/mic_test/status`);
    const data = await res.json();

    if (data.has_recording && data.stats && data.stats.has_audio !== false && data.stats.samples_count > 0) {
      const audioUrl = `${API_BASE}/api/devices/${encodeURIComponent(mac)}/mic_test/audio?t=${Date.now()}`;
      const statusHtml = `<span style="color: var(--accent-green);">✓ Записано (${data.stats.duration_s}с)</span>`;
      if (statusEl) statusEl.innerHTML = statusHtml;
      if (wrapEl) wrapEl.style.display = 'flex';
      if (audioEl) {
        audioEl.style.display = 'block';
        audioEl.src = audioUrl;
        audioEl.load();
        audioEl.play().catch(() => {});
      }
      let metricsHtml = '';
      if (metricsEl && data.stats) {
        metricsHtml = renderMicMetrics(metricsEl, data.stats);
      }
      window.micRecordingsState[mac] = {
        isTesting: false,
        buttonText: '⏺ Тест 5 сек',
        audioSrc: audioUrl,
        statusText: statusHtml,
        metricsHtml: metricsHtml
      };
      showToast(`Тестовая запись готова! RMS: ${data.stats.rms_dbfs} dBFS, Peak: ${data.stats.peak}`, false);
    } else {
      // 0 байт или на плате старая прошивка
      const statusHtml = `<span style="color: var(--accent-yellow);" title="Устройство не передало звук (0 байт)">⚠️ 0 байт</span>`;
      if (statusEl) statusEl.innerHTML = statusHtml;
      if (wrapEl) wrapEl.style.display = 'flex';
      if (audioEl) audioEl.style.display = 'none';
      const targetDev = devices.find(d => (d.mac || '').toLowerCase() === (mac || '').toLowerCase()) || {};
      const isPc = targetDev.device_type === 'pc_streamer' || (targetDev.mac || '').startsWith('streamer_');
      const warnHtml = isPc ? `
        <div style="background: rgba(255, 171, 0, 0.12); border: 1px solid rgba(255, 171, 0, 0.35); border-radius: 6px; padding: 8px 10px; font-size: 11px; line-height: 1.4; color: #ffca28; width: 100%;">
          <strong>⚠️ Аудио не получено (0 байт)</strong><br>
          Клиент стримера на ПК не передал звук микрофона.<br>
          Проверьте номер микрофона (параметр <code>--input-device</code>) в консоли ПК.
        </div>
      ` : `
        <div style="background: rgba(255, 171, 0, 0.12); border: 1px solid rgba(255, 171, 0, 0.35); border-radius: 6px; padding: 8px 10px; font-size: 11px; line-height: 1.4; color: #ffca28; width: 100%;">
          <strong>⚠️ Аудио не получено (0 байт)</strong><br>
          Плата ESP32 еще работает на старой прошивке без функции передачи звука при тесте.<br>
          Пожалуйста, нажмите кнопку <strong>«⚡ OTA»</strong> ниже в этой карточке для беспроводного обновления!
        </div>
      `;
      if (metricsEl) metricsEl.innerHTML = warnHtml;
      window.micRecordingsState[mac] = {
        isTesting: false,
        buttonText: '⏺ Тест 5 сек',
        audioSrc: '',
        statusText: statusHtml,
        metricsHtml: warnHtml
      };
      showToast(isPc ? '⚠️ Звук с ПК не получен (0 байт). Проверьте микрофон!' : '⚠️ Звук не получен (0 байт). Нажмите кнопку «⚡ OTA» в карточке устройства!', true);
    }
  } catch (e) {
    if (statusEl) statusEl.innerText = 'Ошибка получения записи';
    showToast(`Ошибка проверки записи: ${e.message}`, true);
  }
};

// Воспроизведение последней боевой распознанной реплики
window.playLastUtterance = async function(mac) {
  const statusEl = document.getElementById(`mic-status-${mac}`);
  const wrapEl = document.getElementById(`mic-player-wrap-${mac}`);
  const audioEl = document.getElementById(`mic-audio-${mac}`);
  const metricsEl = document.getElementById(`mic-metrics-${mac}`);

  try {
    const statusRes = await fetch(`${API_BASE}/api/devices/${encodeURIComponent(mac)}/last_utterance/status`);
    const statusData = await statusRes.json();

    if (!statusData.has_recording || !statusData.stats) {
      if (statusEl) statusEl.innerHTML = `<span style="color: var(--text-muted);">Нет реплик</span>`;
      showToast('В этой сессии еще не было распознанных голосовых команд. Скажите вслух «Порфирий, который час?» и повторите!', true);
      return;
    }

    const audioUrl = `${API_BASE}/api/devices/${encodeURIComponent(mac)}/last_utterance/audio?t=${Date.now()}`;
    const statusHtml = `<span style="color: var(--accent-blue);">💬 Реплика (${statusData.stats.duration_s}с)</span>`;
    if (statusEl) statusEl.innerHTML = statusHtml;
    if (wrapEl) wrapEl.style.display = 'flex';
    if (audioEl) {
      audioEl.style.display = 'block';
      audioEl.src = audioUrl;
      audioEl.load();
      audioEl.play().catch(() => {});
    } else {
      const tmpAudio = new Audio(audioUrl);
      tmpAudio.play().catch(() => {});
    }
    let metricsHtml = '';
    if (metricsEl && statusData.stats) {
      metricsHtml = renderMicMetrics(metricsEl, statusData.stats);
    }
    window.micRecordingsState[mac] = window.micRecordingsState[mac] || {};
    window.micRecordingsState[mac].audioSrc = audioUrl;
    window.micRecordingsState[mac].statusText = statusHtml;
    window.micRecordingsState[mac].metricsHtml = metricsHtml;
    showToast(`Воспроизведение последней фразы (${statusData.stats.duration_s}с, RMS: ${statusData.stats.rms_dbfs} dBFS)`, false);
  } catch (err) {
    showToast(`Ошибка загрузки реплики: ${err.message}`, true);
  }
};

// Отрисовка бейджей метрик звука (RMS dBFS, пик, клиппинг)
window.renderMicMetrics = function(container, stats) {
  if (!stats) return '';

  let clipBadge = '';
  if (stats.clipping_percent > 3.0) {
    clipBadge = `<span class="mic-badge badge-danger" title="Сильный цифровой клиппинг! Снизьте усиление mic_gain">⚠️ Клиппинг: ${stats.clipping_percent}%</span>`;
  } else if (stats.clipping_percent > 0.1) {
    clipBadge = `<span class="mic-badge badge-warning" title="Легкий клиппинг на пиках громкости">⚠️ Клиппинг: ${stats.clipping_percent}%</span>`;
  } else {
    clipBadge = `<span class="mic-badge badge-success" title="Звук чистый, цифровой перегрузки нет">✓ 0% клиппинга</span>`;
  }

  let rmsClass = 'badge-info';
  if (stats.rms_dbfs < -42) rmsClass = 'badge-warning';

  const html = `
    <span class="mic-badge ${rmsClass}" title="Средняя громкость (RMS dBFS)">${stats.rms_dbfs} dBFS</span>
    <span class="mic-badge badge-neutral" title="Пиковая амплитуда от 0 до 32767">Пик: ${stats.peak}</span>
    ${clipBadge}
  `;
  if (container) {
    container.innerHTML = html;
  }
  return html;
};

// Расчет шкалы сигнала Wi-Fi
function getRssiVisual(rssi) {
  if (rssi >= -65) return { bars: 4, color: 'green', quality: 'Отличный' };
  if (rssi >= -75) return { bars: 3, color: 'yellow', quality: 'Нормальный' };
  if (rssi >= -85) return { bars: 2, color: 'orange', quality: 'Слабый' };
  return { bars: 1, color: 'red', quality: 'Критический' };
}

// Форматирование аптайма в ч/м
function formatUptime(sec) {
  if (!sec || sec < 60) return `${sec || 0} с`;
  const m = Math.floor(sec / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return `${h}ч ${m % 60}м`;
  return `${m}м`;
}

// Отрисовка списка колонок для групповой настройки (ТОЛЬКО ESP32)
function renderBulkTargets() {
  const container = document.getElementById('bulk-targets-list');
  if (!container) return;

  const espDevices = devices.filter(dev => dev.device_type === 'esp32');

  if (espDevices.length === 0) {
    container.innerHTML = `<div class="empty-hint" style="color: var(--text-secondary); font-size: 13px;">Нет активных плат ESP32 для групповой настройки акустики и подсветки.</div>`;
    return;
  }

  const selectAllHtml = `
    <label class="checkbox-badge">
      <input type="checkbox" id="bulk-select-all" checked onchange="toggleSelectAll(this.checked)">
      <span><strong>Выбрать все ESP32</strong> (${espDevices.length})</span>
    </label>
  `;

  const itemsHtml = espDevices.map(dev => `
    <label class="checkbox-badge">
      <input type="checkbox" name="target_dev" value="${dev.mac}" checked class="bulk-dev-checkbox">
      <span>${escapeHtml(dev.name)} ${dev.area_name ? `(${dev.area_name})` : ''}</span>
    </label>
  `).join('');

  container.innerHTML = selectAllHtml + itemsHtml;
  renderOtaBanner();
}

// Отрисовка баннера OTA-обновления на странице групповых настроек
function renderOtaBanner() {
  const container = document.getElementById('ota-bulk-banner-container');
  if (!container) return;

  const outdated = devices.filter(dev => dev.device_type === 'esp32' && dev.is_online && dev.has_update);
  const targetVer = (devices.find(d => d.target_firmware) || {}).target_firmware || '0.0.61';

  if (outdated.length === 0) {
    container.innerHTML = '';
    return;
  }

  container.innerHTML = `
    <div class="ota-banner" id="ota-bulk-banner">
      <div class="ota-info">
        <span class="ota-badge">🚀 Доступно обновление ПО</span>
        <h3 class="ota-title">Новая прошивка v${escapeHtml(targetVer)} готова к установке</h3>
        <p class="ota-desc">
          Обнаружено <strong>${outdated.length}</strong> онлайн-колонок с устаревшей версией: 
          ${outdated.map(d => `<code>${escapeHtml(d.name)} (${escapeHtml(d.firmware || 'старая')})</code>`).join(', ')}
        </p>
      </div>
      <button class="btn-ota-bulk" id="btn-start-bulk-ota" onclick="handleBulkOta()">
        ⚡ Обновить все устаревшие ESP32 (${outdated.length})
      </button>
    </div>
  `;
}

async function handleBulkOta() {
  const btn = document.getElementById('btn-start-bulk-ota');
  if (btn) {
    btn.disabled = true;
    btn.innerText = '⏳ Запуск обновления...';
  }
  showToast('Запущено обновление прошивок по воздуху...');
  try {
    const res = await fetch(`${API_BASE}/api/devices/bulk_ota`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_macs: ['all_outdated'] })
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Обновление запущено для ${data.started_count || 0} устройств`);
    } else {
      showToast(data.error || 'Ошибка старта OTA', true);
      if (btn) {
        btn.disabled = false;
        btn.innerText = '⚡ Обновить все устаревшие ESP32';
      }
    }
  } catch (err) {
    showToast('Ошибка сети при вызове OTA: ' + err.message, true);
    if (btn) {
      btn.disabled = false;
      btn.innerText = '⚡ Обновить все устаревшие ESP32';
    }
  }
}

async function triggerDeviceOta(mac) {
  if (!confirm(`Обновить прошивку по воздуху для устройства ${mac}?`)) return;
  showToast(`Запуск OTA для ${mac}...`);
  try {
    const res = await fetch(`${API_BASE}/api/devices/${mac}/ota`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    const data = await res.json();
    if (data.success) {
      showToast('OTA обновление началось');
    } else {
      showToast('Ошибка: ' + (data.error || 'Не удалось запустить OTA'), true);
    }
  } catch (err) {
    showToast('Ошибка сети: ' + err.message, true);
  }
}

function toggleSelectAll(checked) {
  document.querySelectorAll('.bulk-dev-checkbox').forEach(cb => {
    cb.checked = checked;
  });
}

// Обработка группового сохранения
async function handleBulkSubmit(e) {
  if (e) {
    try {
      e.preventDefault();
      e.stopPropagation();
    } catch (_) {}
  }
  const form = document.getElementById('bulk-form');
  if (!form) {
    console.error('bulk-form element not found');
    return;
  }

  // 1. Выбранные устройства
  const checkedDevs = Array.from(document.querySelectorAll('.bulk-dev-checkbox:checked')).map(cb => cb.value);
  if (checkedDevs.length === 0) {
    showToast('Выберите хотя бы одну колонку для настройки!', true);
    return;
  }

  // 2. Выбранные поля (маска)
  const fields = {};
  if (form.apply_speaker_volume.checked) {
    fields.speaker_volume = parseFloat(form.speaker_volume.value);
  }
  if (form.apply_wake_word_threshold && form.apply_wake_word_threshold.checked) {
    fields.wake_word_threshold = parseFloat(form.wake_word_threshold.value);
  }
  if (form.apply_ww_threshold && form.apply_ww_threshold.checked) {
    fields.ww_threshold = parseFloat(form.ww_threshold.value);
  }
  if (form.apply_wake_word_window_size && form.apply_wake_word_window_size.checked) {
    fields.wake_word_window_size = parseInt(form.wake_word_window_size.value, 10);
  }
  if (form.apply_wake_word_window_mode && form.apply_wake_word_window_mode.checked) {
    fields.wake_word_window_mode = parseInt(form.wake_word_window_mode.value, 10);
  }
  if (form.apply_mic_gain && form.apply_mic_gain.checked) {
    fields.mic_gain = parseFloat(form.mic_gain.value);
  }
  if (form.apply_barge_in_threshold_rms && form.apply_barge_in_threshold_rms.checked) {
    const val = parseInt(form.barge_in_threshold_rms.value, 10);
    fields.barge_in_threshold_rms = isNaN(val) || val <= 0 ? null : val;
  }
  if (form.apply_enable_ducking && form.apply_enable_ducking.checked) {
    fields.enable_ducking = form.enable_ducking.checked;
  }
  if (form.apply_audio_output_mode && form.apply_audio_output_mode.checked) {
    fields.audio_output_mode = form.audio_output_mode.value;
    if (fields.audio_output_mode === 'external_player' && form.response_player) {
      fields.response_player = form.response_player.value;
    }
  }
  if (form.apply_wake_word_mode && form.apply_wake_word_mode.checked) {
    fields.wake_word_mode = form.wake_word_mode.value;
  }
  if (form.apply_silence_timeout_ms && form.apply_silence_timeout_ms.checked) {
    fields.silence_timeout_ms = parseInt(form.silence_timeout_ms.value, 10);
  }
  if (form.apply_led_brightness && form.apply_led_brightness.checked) {
    fields.led_brightness = parseInt(form.led_brightness.value, 10);
  }
  if (form.apply_led_mode_idle && form.apply_led_mode_idle.checked) {
    fields.led_mode_idle = parseInt(form.led_mode_idle.value, 10);
    fields.led_color_idle = form.led_color_idle.value;
  }
  if (form.apply_led_mode_listen && form.apply_led_mode_listen.checked) {
    fields.led_mode_listen = parseInt(form.led_mode_listen.value, 10);
    fields.led_color_listen = form.led_color_listen.value;
  }
  if (form.apply_led_mode_think && form.apply_led_mode_think.checked) {
    fields.led_mode_think = parseInt(form.led_mode_think.value, 10);
    fields.led_color_think = form.led_color_think.value;
  }
  if (form.apply_led_mode_speak && form.apply_led_mode_speak.checked) {
    fields.led_mode_speak = parseInt(form.led_mode_speak.value, 10);
    fields.led_color_speak = form.led_color_speak.value;
  }

  if (Object.keys(fields).length === 0) {
    showToast('Отметьте галочками хотя бы один параметр для изменения!', true);
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/api/devices/bulk_config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target_macs: checkedDevs,
        fields: fields
      })
    });

    const result = await res.json();
    if (result.success) {
      showToast(`Настройки успешно применены к ${checkedDevs.length} колонкам!`);
      fetchDevices();
    } else {
      showToast('Ошибка применения: ' + (result.error || 'неизвестно'), true);
    }
  } catch (err) {
    showToast('Сетевая ошибка при отправке', true);
  }
}

// Быстрое обновление одного параметра конкретной колонки
async function updateSingleDeviceConfig(mac, field, value) {
  try {
    const payload = {};
    payload[field] = value;

    // Мгновенно обновляем локальный объект устройства в памяти
    const d = devices.find(x => x.mac === mac);
    if (d) {
      if (!d.config) d.config = {};
      d.config[field] = value;
      if (field === 'area_name') d.area_name = value || null;
      if (field === 'audio_output_mode' || field === 'wake_word_mode' || field === 'user_id' || field === 'area_name') {
        renderDevicesGrid();
      }
    }

    await fetch(`${API_BASE}/api/devices/${mac}/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    showToast(`Параметр ${field} обновлен`);
  } catch (err) {
    showToast('Ошибка обновления параметра', true);
  }
}

// Вызов действия (Beep / Reboot)
async function triggerDeviceAction(mac, action) {
  try {
    const res = await fetch(`${API_BASE}/api/devices/${mac}/${action}`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(`Команда ${action} отправлена на ${mac}`);
    } else {
      showToast(`Колонка офлайн или не отвечает`, true);
    }
  } catch (err) {
    showToast('Ошибка вызова действия', true);
  }
}

// Принудительный вызов ассистента (эмуляция сработки вейкворда)
async function triggerDeviceWake(mac) {
  try {
    showToast('⚡ Активация режима прослушивания...');
    const res = await fetch(`${API_BASE}/api/devices/${mac}/wake`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(`⚡ Вызов активирован! Порфирий слушает.`);
    } else {
      showToast(`Устройство офлайн или сессия уже активна`, true);
    }
  } catch (err) {
    showToast('Ошибка принудительного вызова', true);
  }
}
window.triggerDeviceWake = triggerDeviceWake;

async function triggerDeviceStop(mac) {
  try {
    const res = await fetch(`${API_BASE}/api/devices/${encodeURIComponent(mac)}/stop`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast('⏹ Порфирий остановлен');
    } else {
      showToast('Устройство офлайн', true);
    }
  } catch (err) {
    showToast('Ошибка остановки', true);
  }
}
window.triggerDeviceStop = triggerDeviceStop;

// Полное удаление устройства из списка и хранилища
async function deleteDevice(mac, name) {
  const displayName = name || mac;
  if (!confirm(`Удалить устройство "${displayName}" (${mac}) из списка?`)) {
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/devices/${encodeURIComponent(mac)}`, {
      method: 'DELETE'
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`Устройство "${displayName}" удалено`);
      devices = devices.filter(d => (d.mac || '').toLowerCase() !== mac.toLowerCase());
      const card = document.getElementById(`card-${mac}`);
      if (card) {
        card.style.transition = 'all 0.25s ease';
        card.style.opacity = '0';
        card.style.transform = 'scale(0.9)';
        setTimeout(() => {
          if (card.parentNode) card.parentNode.removeChild(card);
          updateHeaderStats();
        }, 250);
      } else {
        renderDevicesGrid();
        updateHeaderStats();
      }
      renderBulkTargets();
      renderOtaBanner();
      if (typeof populateWwMonitorSources === 'function') {
        populateWwMonitorSources();
      }
    } else {
      showToast(`Ошибка удаления: ${data.error || res.statusText}`, true);
    }
  } catch (e) {
    showToast(`Ошибка при удалении устройства: ${e.message || e}`, true);
  }
}
window.deleteDevice = deleteDevice;

function triggerSelectedMonitorWake() {
  const sel = document.getElementById('ww-monitor-stream-select');
  const target = (sel && sel.value && sel.value !== 'auto') ? sel.value : (window.lastWwActiveDevice || '');
  if (!target) {
    showToast('Выберите устройство в селекторе выше', true);
    return;
  }
  triggerDeviceWake(target);
}
window.triggerSelectedMonitorWake = triggerSelectedMonitorWake;

// Заполнение формы глобальных настроек
function renderBrainInfo() {
  populateGlobalSettingsForm();
  checkPhrasesStatus();
}

function populateModelsDropdown(selectedModel) {
  const select = document.getElementById('cfg-model');
  const customBox = document.getElementById('custom-model-box');
  const customInput = document.getElementById('cfg-model-custom');
  if (!select) return;

  const currentVal = selectedModel || globalOptions.gemini_model || 'models/gemini-3.1-flash-live-preview';

  let models = Array.isArray(availableModels) && availableModels.length > 0 ? [...availableModels] : [
    { id: 'models/gemini-3.1-flash-live-preview', name: 'Gemini 3.1 Flash Live Preview (Новейшая)', is_live: true },
    { id: 'models/gemini-2.5-flash-native-audio-preview', name: 'Gemini 2.5 Flash Native Audio Preview', is_live: true },
    { id: 'models/gemini-2.0-flash-exp', name: 'Gemini 2.0 Flash Experimental', is_live: true },
    { id: 'gemini-2.0-flash-exp', name: 'Gemini 2.0 Flash Exp (Короткий ID)', is_live: true }
  ];

  const isModelInList = models.some(m => m && m.id === currentVal);

  const liveGroup = models.filter(m => m && m.is_live);
  const otherGroup = models.filter(m => m && !m.is_live);

  let html = '';

  if (liveGroup.length > 0) {
    html += '<optgroup label="🌟 Поддерживают Live Audio (Рекомендуемые)">';
    liveGroup.forEach(m => {
      const sel = (m.id === currentVal) ? 'selected' : '';
      html += `<option value="${escapeHtml(m.id)}" ${sel}>${escapeHtml(m.name || m.id)}</option>`;
    });
    html += '</optgroup>';
  }

  if (otherGroup.length > 0) {
    html += '<optgroup label="Другие доступные модели">';
    otherGroup.forEach(m => {
      const sel = (m.id === currentVal) ? 'selected' : '';
      html += `<option value="${escapeHtml(m.id)}" ${sel}>${escapeHtml(m.name || m.id)}</option>`;
    });
    html += '</optgroup>';
  }

  if (!isModelInList && currentVal && currentVal !== '__custom__') {
    html += `<optgroup label="Пользовательская модель">`;
    html += `<option value="${escapeHtml(currentVal)}" selected>${escapeHtml(currentVal)} (Текущая)</option>`;
    html += `</optgroup>`;
  }

  html += '<optgroup label="Ручной ввод">';
  html += `<option value="__custom__" ${currentVal === '__custom__' ? 'selected' : ''}>✍️ Ввести другую модель вручную...</option>`;
  html += '</optgroup>';

  select.innerHTML = html;

  if (currentVal === '__custom__') {
    if (customBox) customBox.style.display = 'block';
  } else {
    if (customBox) customBox.style.display = 'none';
    if (customInput) customInput.value = currentVal;
  }
}

function handleModelSelectChange(val) {
  const customBox = document.getElementById('custom-model-box');
  const customInput = document.getElementById('cfg-model-custom');
  if (val === '__custom__') {
    if (customBox) customBox.style.display = 'block';
    if (customInput) {
      customInput.focus();
    }
  } else {
    if (customBox) customBox.style.display = 'none';
    if (customInput) customInput.value = val;
  }
}

async function refreshModelsList() {
  const btn = document.getElementById('btn-refresh-models');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '🔄 Опрос API...';
  }

  showToast('Запрашиваю список моделей через Gemini API...');

  try {
    const res = await fetch(`${API_BASE}/api/models/refresh`, { method: 'POST' });
    const data = await res.json();
    if (data.success && data.models) {
      availableModels = data.models;
      const select = document.getElementById('cfg-model');
      const currentVal = select ? select.value : globalOptions.gemini_model;
      populateModelsDropdown(currentVal);
      if (typeof renderBigModelsPool === 'function') renderBigModelsPool();
      const liveCount = availableModels.filter(m => m.is_live).length;
      showToast(`Модели обновлены! Найдено ${availableModels.length} моделей (${liveCount} Live Audio).`);
    } else {
      showToast(`Ошибка опроса моделей: ${data.error || 'Проверьте API ключ'}`, true);
    }
  } catch (err) {
    showToast('Сетевая ошибка при обновлении моделей', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '🔄 Обновить модели';
    }
  }
}

function restoreDefaultPrompt(type) {
  const text = CANONICAL_PROMPTS[type];
  if (!text) return;
  const textarea = document.getElementById(`cfg-prompt-${type}`);
  if (textarea) {
    textarea.value = text;
    updatePromptCharCounters();
    showToast(`Шаблон для ${type} восстановлен! Не забудьте сохранить.`);
  }
}

function populateGlobalSettingsForm() {
  const form = document.getElementById('global-settings-form');
  if (!form) return;

  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el && val !== undefined && val !== null) el.value = val;
  };

  const keyInput = document.getElementById('cfg-api-key');
  if (keyInput) {
    if (globalOptions.has_api_key) {
      keyInput.placeholder = `Ключ задан (${globalOptions.gemini_api_key || '••••••••'})`;
      keyInput.value = '';
    } else {
      keyInput.placeholder = 'AIza... (введите ключ Gemini API)';
    }
  }

  populateModelsDropdown(globalOptions.gemini_model || 'models/gemini-3.1-flash-live-preview');

  const bmChk = document.getElementById('cfg-big-models-enabled');
  if (bmChk) bmChk.checked = !!globalOptions.big_models_enabled;
  setVal('cfg-big-models-threshold', globalOptions.big_models_threshold_chars || 4000);
  bigModelsPoolState = Array.isArray(globalOptions.big_models_pool)
    ? globalOptions.big_models_pool.map(item => ({ ...item }))
    : [];
  renderBigModelsPool();

  setVal('cfg-voice', globalOptions.voice_name || 'Charon');
  setVal('cfg-temperature', globalOptions.temperature !== undefined ? globalOptions.temperature : 0.7);
  const valTemp = document.getElementById('val-temp');
  if (valTemp) valTemp.innerText = globalOptions.temperature !== undefined ? globalOptions.temperature : 0.7;

  setVal('cfg-thinking-timeout', globalOptions.thinking_timeout_s !== undefined ? globalOptions.thinking_timeout_s : 7);
  setVal('cfg-vad-silence', globalOptions.vad_silence_duration_ms || 600);

  const googleSearchChk = document.getElementById('cfg-google-search');
  if (googleSearchChk) {
    googleSearchChk.checked = globalOptions.enable_google_search !== false;
  }
  populateWebSearchModelDropdown(globalOptions.web_search_model || '');

  const bargeInChk = document.getElementById('cfg-barge-in');
  if (bargeInChk) {
    bargeInChk.checked = globalOptions.enable_barge_in !== false;
  }

  const rmsVal = globalOptions.barge_in_threshold_rms !== undefined ? globalOptions.barge_in_threshold_rms : 600;
  setVal('cfg-barge-rms', rmsVal);
  const valBargeRms = document.getElementById('val-barge-rms');
  if (valBargeRms) valBargeRms.innerText = rmsVal;

  // Настройки мультимедиа и дакинга
  const duckMode = globalOptions.ducking_mode || (globalOptions.enable_media_ducking !== false ? 'same_area' : 'disabled');
  setVal('cfg-ducking-mode', duckMode);
  handleDuckingModeChange(duckMode);

  const duckFactor = globalOptions.ducking_volume_factor !== undefined ? globalOptions.ducking_volume_factor : 0.25;
  setVal('cfg-ducking-factor', duckFactor);
  updateDuckingFactorDisplay(duckFactor);

  populateMediaPlayersDropdown(globalOptions.default_media_player || 'auto');
  populateAirplayPlayersDropdown(globalOptions.default_airplay_player || '');

  // Настройки звуковых сигналов (Earcons)
  const earAlias = {
    alice_click: 'apple_taptic', soft_pop: 'nest_bubble', wood_tick: 'studio_wood', crystal_tap: 'glass_tick',
    marimba_tudun: 'google_home', warm_chord: 'apple_homepod', short_ding: 'macos_glass', cyber_ping: 'warm_marimba',
    soft_chime: 'google_listen', subtle_bloop: 'apple_siri', double_tap: 'warm_halo'
  };
  const earVolChk = document.getElementById('cfg-earcon-volume-enabled');
  if (earVolChk) earVolChk.checked = globalOptions.earcon_volume_enabled !== false;
  setVal('cfg-earcon-volume-preset', earAlias[globalOptions.earcon_volume_preset] || globalOptions.earcon_volume_preset || 'apple_taptic');
  const earShChk = document.getElementById('cfg-earcon-smarthome-enabled');
  if (earShChk) earShChk.checked = globalOptions.earcon_smarthome_enabled !== false;
  setVal('cfg-earcon-smarthome-preset', earAlias[globalOptions.earcon_smarthome_preset] || globalOptions.earcon_smarthome_preset || 'google_home');
  setVal('cfg-earcon-wake-preset', earAlias[globalOptions.earcon_wake_preset] || globalOptions.earcon_wake_preset || 'google_listen');

  // Настройки умного тихого режима
  const qEn = document.getElementById('cfg-quiet-enabled');
  if (qEn) qEn.checked = Boolean(globalOptions.quiet_mode_enabled);
  setVal('cfg-quiet-force', globalOptions.quiet_mode_force || 'auto');
  setVal('cfg-quiet-start', globalOptions.quiet_mode_start || '23:00');
  setVal('cfg-quiet-end', globalOptions.quiet_mode_end || '08:00');
  const qWw = globalOptions.quiet_mode_ww_threshold !== undefined ? Number(globalOptions.quiet_mode_ww_threshold) : 0.42;
  setVal('cfg-quiet-ww-thr', qWw.toFixed(2));
  const valQWw = document.getElementById('val-quiet-ww-thr');
  if (valQWw) valQWw.innerText = qWw.toFixed(2);
  const qVolPct = Math.round((globalOptions.quiet_mode_volume !== undefined ? Number(globalOptions.quiet_mode_volume) : 0.30) * 100);
  setVal('cfg-quiet-volume', qVolPct);
  const valQVol = document.getElementById('val-quiet-volume');
  if (valQVol) valQVol.innerText = `${qVolPct}%`;
  const qSil = globalOptions.quiet_mode_silence_dbfs !== undefined ? Number(globalOptions.quiet_mode_silence_dbfs) : -42;
  setVal('cfg-quiet-silence-dbfs', qSil);
  const valQSil = document.getElementById('val-quiet-silence-dbfs');
  if (valQSil) valQSil.innerText = `${qSil} dBFS`;
  const qMute = document.getElementById('cfg-quiet-mute-wake');
  if (qMute) qMute.checked = globalOptions.quiet_mode_mute_wake_earcon !== false;
  if (typeof renderQuietModeEntities === 'function') renderQuietModeEntities();
  if (typeof refreshQuietModeStatus === 'function') refreshQuietModeStatus();

  // Настройки OpenAI API / Яндекс Станция (Quasar Bridge)
  const opEnabled = globalOptions.openai_api_enabled !== false;
  const opCheckbox = document.getElementById('cfg-openai-api-enabled');
  if (opCheckbox) opCheckbox.checked = opEnabled;

  const opPort = globalOptions.openai_api_port || 8000;
  setVal('cfg-openai-api-port', opPort);
  const hintPort = document.getElementById('hint-openai-port');
  if (hintPort) hintPort.innerText = opPort;

  const opKeyInput = document.getElementById('cfg-openai-api-key');
  if (opKeyInput) {
    if (globalOptions.has_openai_api_key) {
      opKeyInput.placeholder = `Ключ задан (${globalOptions.openai_api_key || '••••••'})`;
      opKeyInput.value = '';
    } else {
      opKeyInput.placeholder = 'Оставьте пустым для работы без ключа';
      opKeyInput.value = '';
    }
  }

  const maKeyInput = document.getElementById('cfg-ma-key');
  if (maKeyInput) {
    if (globalOptions.has_ma_api_key) {
      maKeyInput.placeholder = `Токен задан (${globalOptions.ma_api_key || '••••••••'})`;
      maKeyInput.value = '';
    } else {
      maKeyInput.placeholder = 'Опциональный токен (оставьте пустым если не требуется)';
      maKeyInput.value = '';
    }
  }

  const serperKeyInput = document.getElementById('cfg-serper-api-key');
  if (serperKeyInput) {
    if (globalOptions.has_serper_api_key) {
      serperKeyInput.placeholder = `Ключ задан (${globalOptions.serper_api_key || '••••••••'})`;
      serperKeyInput.value = '';
    } else {
      serperKeyInput.placeholder = 'Вставьте API-ключ с serper.dev (2500 запросов бесплатно)';
      serperKeyInput.value = '';
    }
  }

  // 4 Модульных промпта: если в настройках пусто или пробелы, подставляем каноничный шаблон!
  const getPromptVal = (key, fallbackKey) => {
    const val = globalOptions[key];
    if (val && typeof val === 'string' && val.trim().length > 0) {
      return val;
    }
    return CANONICAL_PROMPTS[fallbackKey] || '';
  };

  setVal('cfg-prompt-persona', getPromptVal('prompt_persona', 'persona'));
  setVal('cfg-prompt-users', getPromptVal('prompt_users', 'users'));
  setVal('cfg-prompt-smart-home', getPromptVal('prompt_smart_home', 'smart-home'));
  setVal('cfg-prompt-general', getPromptVal('prompt_general', 'general'));

  // Настройки серверного вейкворда (openWakeWord)
  const wwThresh = globalOptions.ww_server_threshold !== undefined ? globalOptions.ww_server_threshold : 0.94;
  setVal('cfg-ww-threshold', wwThresh);
  const valWw = document.getElementById('val-ww-server-threshold');
  if (valWw) valWw.innerText = parseFloat(wwThresh).toFixed(2);
  if (typeof updateMonitorThreshold === 'function') {
    updateMonitorThreshold(parseFloat(wwThresh));
  }
  if (typeof populateWwMonitorSources === 'function') {
    populateWwMonitorSources();
  }

  updatePromptCharCounters();

  // Привязка счетчиков символов
  ['persona', 'users', 'smart-home', 'general'].forEach(name => {
    const txt = document.getElementById(`cfg-prompt-${name}`);
    if (txt && !txt._hasCounterListener) {
      txt.addEventListener('input', updatePromptCharCounters);
      txt._hasCounterListener = true;
    }
  });
}

function updatePromptCharCounters() {
  const update = (id, countId) => {
    const el = document.getElementById(id);
    const countEl = document.getElementById(countId);
    if (el && countEl) {
      countEl.innerText = `${el.value.length} симв.`;
    }
  };
  update('cfg-prompt-persona', 'count-persona');
  update('cfg-prompt-users', 'count-users');
  update('cfg-prompt-smart-home', 'count-smart-home');
  update('cfg-prompt-general', 'count-general');
}

function togglePasswordField(inputId, btnId) {
  const input = document.getElementById(inputId);
  const btn = btnId ? document.getElementById(btnId) : null;
  if (!input) return;
  if (input.type === 'password') {
    input.type = 'text';
    if (btn) btn.textContent = '🔒';
  } else {
    input.type = 'password';
    if (btn) btn.textContent = '👁️';
  }
}

function toggleKeyVisibility() {
  togglePasswordField('cfg-api-key', 'btn-toggle-key');
}

function toggleMaKeyVisibility() {
  togglePasswordField('cfg-ma-key', 'btn-toggle-ma-key');
}

function toggleSerperKeyVisibility() {
  togglePasswordField('cfg-serper-api-key', 'btn-toggle-serper-key');
}
window.toggleSerperKeyVisibility = toggleSerperKeyVisibility;

function updateDuckingFactorDisplay(val) {
  const num = parseFloat(val) || 0.25;
  const elVal = document.getElementById('val-ducking-factor');
  const elPct = document.getElementById('val-ducking-percent');
  if (elVal) elVal.innerText = num.toFixed(2);
  if (elPct) elPct.innerText = Math.round(num * 100) + '%';
}

function populatePlayerSelect(selectId, selectedPlayer, defaultVal, defaultLabel) {
  const select = document.getElementById(selectId);
  if (!select) return;

  const currentVal = selectedPlayer || select.value || defaultVal;
  let html = `<option value="${escapeHtml(defaultVal)}">${defaultLabel}</option>`;

  if (availableMediaPlayers && availableMediaPlayers.length > 0) {
    html += '<optgroup label="Медиаплееры Home Assistant">';
    availableMediaPlayers.forEach(p => {
      const isSel = (p.entity_id === currentVal) ? 'selected' : '';
      html += `<option value="${escapeHtml(p.entity_id)}" ${isSel}>${escapeHtml(p.name)} (${escapeHtml(p.entity_id)})</option>`;
    });
    html += '</optgroup>';
  }

  if (currentVal && currentVal !== defaultVal && !availableMediaPlayers.some(p => p.entity_id === currentVal)) {
    html += `<optgroup label="Текущий сохраненный плеер">`;
    html += `<option value="${escapeHtml(currentVal)}" selected>${escapeHtml(currentVal)} (Сохранен)</option>`;
    html += `</optgroup>`;
  }

  select.innerHTML = html;
  select.value = currentVal;
}

function populateMediaPlayersDropdown(selectedPlayer) {
  populatePlayerSelect(
    'cfg-default-media-player',
    selectedPlayer,
    'auto',
    '🔄 Автоматически (все играющие плееры)'
  );

  const bulkSelect = document.getElementById('bulk-response-player');
  if (bulkSelect) {
    let bulkHtml = '<option value="">— Выберите медиаплеер —</option>';
    if (availableMediaPlayers && availableMediaPlayers.length > 0) {
      availableMediaPlayers.forEach(p => {
        bulkHtml += `<option value="${escapeHtml(p.entity_id)}">${escapeHtml(p.name)} (${escapeHtml(p.entity_id)})</option>`;
      });
    }
    bulkSelect.innerHTML = bulkHtml;
  }
}

function populateAirplayPlayersDropdown(selectedPlayer) {
  populatePlayerSelect(
    'cfg-default-airplay-player',
    selectedPlayer,
    '',
    '— Выберите плеер из Home Assistant —'
  );
}

async function refreshMediaPlayersList() {
  const btn = document.getElementById('btn-refresh-players');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '🔄 Опрос HA...';
  }

  showToast('Запрашиваю список медиаплееров из Home Assistant...');

  try {
    const res = await fetch(`${API_BASE}/api/media_players`);
    const data = await res.json();
    if (data.media_players) {
      availableMediaPlayers = data.media_players;
      const select = document.getElementById('cfg-default-media-player');
      const currentVal = select ? select.value : (globalOptions.default_media_player || 'auto');
      populateMediaPlayersDropdown(currentVal);
      populateAirplayPlayersDropdown(globalOptions.default_airplay_player || '');
      showToast(`Плееры обновлены! Найдено ${availableMediaPlayers.length} медиаплееров.`);
    } else {
      showToast(`Ошибка опроса плееров: ${data.error || 'Проверьте соединение с HA'}`, true);
    }
  } catch (err) {
    showToast('Сетевая ошибка при обновлении плееров', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '🔄 Обновить плееры';
    }
  }
}

// Сохранение глобальных настроек (Hot Reload)
async function saveGlobalSettings(e) {
  if (e) {
    try {
      e.preventDefault();
      e.stopPropagation();
    } catch (_) {}
  }
  const btn = document.getElementById('btn-save-global');
  const origHtml = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '⏳ Сохраняю...';
  }

  showToast('Сохраняю настройки Порфирия...');

  try {
    let selectedModel = document.getElementById('cfg-model')?.value?.trim();
    if (selectedModel === '__custom__') {
      selectedModel = document.getElementById('cfg-model-custom')?.value?.trim() || '';
    }

    const payload = {
      gemini_model: selectedModel,
      big_models_enabled: document.getElementById('cfg-big-models-enabled')?.checked || false,
      big_models_threshold_chars: parseInt(document.getElementById('cfg-big-models-threshold')?.value || 4000, 10),
      big_models_pool: bigModelsPoolState,
      web_search_model: (document.getElementById('cfg-web-search-model')?.value || '').trim(),
      voice_name: document.getElementById('cfg-voice')?.value,
      temperature: parseFloat(document.getElementById('cfg-temperature')?.value || 0.7),
      thinking_timeout_s: parseInt(document.getElementById('cfg-thinking-timeout')?.value || 7),
      vad_silence_duration_ms: parseInt(document.getElementById('cfg-vad-silence')?.value || 600),
      enable_google_search: document.getElementById('cfg-google-search')?.checked || false,
      enable_barge_in: document.getElementById('cfg-barge-in')?.checked !== false,
      barge_in_threshold_rms: parseInt(document.getElementById('cfg-barge-rms')?.value || 600),
      ducking_mode: document.getElementById('cfg-ducking-mode')?.value || 'same_area',
      enable_media_ducking: document.getElementById('cfg-ducking-mode')?.value !== 'disabled',
      ducking_volume_factor: parseFloat(document.getElementById('cfg-ducking-factor')?.value || 0.25),
      default_media_player: document.getElementById('cfg-default-media-player')?.value || 'auto',
      prompt_persona: document.getElementById('cfg-prompt-persona')?.value || '',
      prompt_users: document.getElementById('cfg-prompt-users')?.value || '',
      prompt_smart_home: document.getElementById('cfg-prompt-smart-home')?.value || '',
      prompt_general: document.getElementById('cfg-prompt-general')?.value || '',
      ww_server_threshold: parseFloat(document.getElementById('cfg-ww-threshold')?.value || 0.94),
      openai_api_enabled: document.getElementById('cfg-openai-api-enabled')?.checked !== false,
      openai_api_port: parseInt(document.getElementById('cfg-openai-api-port')?.value || 8000),
      default_airplay_player: document.getElementById('cfg-default-airplay-player')?.value || '',
      earcon_volume_enabled: document.getElementById('cfg-earcon-volume-enabled')?.checked !== false,
      earcon_volume_preset: document.getElementById('cfg-earcon-volume-preset')?.value || 'alice_click',
      earcon_smarthome_enabled: document.getElementById('cfg-earcon-smarthome-enabled')?.checked !== false,
      earcon_smarthome_preset: document.getElementById('cfg-earcon-smarthome-preset')?.value || 'marimba_tudun',
      earcon_wake_preset: document.getElementById('cfg-earcon-wake-preset')?.value || 'soft_chime',
      quiet_mode_enabled: document.getElementById('cfg-quiet-enabled')?.checked || false,
      quiet_mode_force: document.getElementById('cfg-quiet-force')?.value || 'auto',
      quiet_mode_start: document.getElementById('cfg-quiet-start')?.value || '23:00',
      quiet_mode_end: document.getElementById('cfg-quiet-end')?.value || '08:00',
      quiet_mode_ww_threshold: parseFloat(document.getElementById('cfg-quiet-ww-thr')?.value || 0.42),
      quiet_mode_volume: Math.max(0, Math.min(1, parseFloat(document.getElementById('cfg-quiet-volume')?.value || 30) / 100.0)),
      quiet_mode_silence_dbfs: parseFloat(document.getElementById('cfg-quiet-silence-dbfs')?.value || -42),
      quiet_mode_mute_wake_earcon: document.getElementById('cfg-quiet-mute-wake')?.checked !== false,
      quiet_mode_entities: Array.from(document.querySelectorAll('input[name="quiet_entity"]:checked')).map(el => el.value)
    };

    const newKey = document.getElementById('cfg-api-key')?.value?.trim();
    if (newKey) {
      payload.gemini_api_key = newKey;
    }

    const newMaKey = document.getElementById('cfg-ma-key')?.value?.trim();
    if (newMaKey !== undefined && newMaKey !== '') {
      payload.ma_api_key = newMaKey;
    }

    const newSerperKey = document.getElementById('cfg-serper-api-key')?.value?.trim();
    if (newSerperKey) {
      payload.serper_api_key = newSerperKey;
    }

    const newOpKey = document.getElementById('cfg-openai-api-key')?.value?.trim();
    if (newOpKey !== undefined && newOpKey !== '') {
      payload.openai_api_key = newOpKey;
    }

    console.log('Sending global settings payload to:', `${API_BASE}/api/global`, payload);

    const res = await fetch(`${API_BASE}/api/global`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    const data = await res.json();
    if (data.success) {
      globalOptions = data.options || {};
      showToast('Настройки Порфирия сохранены и применены на лету!');
      populateGlobalSettingsForm();
    } else {
      showToast(`Ошибка сохранения: ${data.error || 'Проверьте данные'}`, true);
    }
  } catch (err) {
    console.error('Error saving global settings:', err);
    showToast('Ошибка связи с сервером при сохранении: ' + (err.message || err), true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origHtml || '💾 Сохранить и применить на лету';
    }
  }
}

// Ручной запуск перегенерации фраз
async function regeneratePhrases() {
  const btn = document.getElementById('btn-regenerate-phrases');
  if (btn) btn.disabled = true;

  try {
    const res = await fetch(`${API_BASE}/api/phrases/regenerate`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast('Регенерация фраз запущена в фоне!');
      pollPhrasesProgress();
    } else {
      showToast(`Ошибка: ${data.error || 'Не удалось запустить'}`, true);
      if (btn) btn.disabled = false;
    }
  } catch (err) {
    showToast('Ошибка запроса на регенерацию фраз', true);
    if (btn) btn.disabled = false;
  }
}

// Проверка статуса системных фраз
async function checkPhrasesStatus() {
  try {
    const res = await fetch(`${API_BASE}/api/phrases/status`);
    if (!res.ok) return;
    const data = await res.json();
    const pill = document.getElementById('phrase-status-pill');
    const text = document.getElementById('phrase-status-text');
    if (!pill || !text) return;

    if (data.is_generating) {
      pill.className = 'phrase-status-badge generating';
      text.textContent = 'Генерация реплик в фоне...';
    } else if (data.is_ready && data.total_phrases > 0) {
      pill.className = 'phrase-status-badge ready';
      text.textContent = `Кэш готов (${data.total_phrases} реплик)`;
    } else {
      pill.className = 'phrase-status-badge empty';
      text.textContent = 'Кэш фраз пуст';
    }
  } catch (err) {}
}

function pollPhrasesProgress() {
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/phrases/status`);
      const data = await res.json();
      checkPhrasesStatus();
      if (!data.is_generating) {
        clearInterval(interval);
        const btn = document.getElementById('btn-regenerate-phrases');
        if (btn) btn.disabled = false;
        showToast(`Фразы готовы: ${data.total_phrases || 0} файлов в кэше!`);
      }
    } catch (e) {
      clearInterval(interval);
    }
  }, 3000);
}

// Централизованный монитор отладки серверного вейкворда (вкладка Интеллект)
let lastWwMonitorTriggerTime = 0;

window.handleWwMonitorSourceChange = function(val) {
  // Выбор конкретного источника или auto
};

window.updateMonitorThreshold = function(thresh) {
  const line = document.getElementById('ww-monitor-thresh-line');
  const txt = document.getElementById('ww-monitor-thresh-val');
  if (line) line.style.left = `${Math.min(100, Math.max(0, thresh * 100))}%`;
  if (txt) txt.innerText = `Порог: ${thresh.toFixed(2)}`;
};

window.populateWwMonitorSources = function() {
  const sel = document.getElementById('ww-monitor-stream-select');
  if (!sel) return;
  const current = sel.value || 'auto';
  // Находим все клиенты, использующие серверный вейкворд
  const candidates = devices.filter(d => 
    d.device_type === 'pc_streamer' || 
    (d.mac && d.mac.startsWith('streamer_')) ||
    (d.config && d.config.wake_word_mode === 'server')
  );
  let html = `<option value="auto" ${current === 'auto' ? 'selected' : ''}>— Авто (любой активный) —</option>`;
  candidates.forEach(d => {
    const isSel = (d.mac === current) ? 'selected' : '';
    const isOnline = d.is_online || d.state !== 'offline';
    const statusDot = isOnline ? '🟢' : '⚪';
    const title = `${statusDot} ${d.name || d.mac} (${d.area_name || d.device_type || 'микрофон'})`;
    html += `<option value="${escapeHtml(d.mac)}" ${isSel}>${escapeHtml(title)}</option>`;
  });
  sel.innerHTML = html;
};

window.updateWwMonitorTelemetry = function(info) {
  const sel = document.getElementById('ww-monitor-stream-select');
  if (sel && info.mac) {
    // Если этого устройства ещё нет в селекторе, динамически добавляем его
    let opt = sel.querySelector(`option[value="${info.mac}"]`);
    if (!opt) {
      opt = document.createElement('option');
      opt.value = info.mac;
      opt.innerText = `🟢 ${info.name || info.mac} (live)`;
      sel.appendChild(opt);
    }
  }

  const activeFilter = sel ? sel.value : 'auto';
  if (activeFilter !== 'auto' && activeFilter !== info.mac) {
    return;
  }

  // Индикатор живого потока
  const liveDot = document.getElementById('ww-monitor-live-dot');
  if (liveDot) {
    liveDot.style.background = 'var(--accent-green)';
    liveDot.style.boxShadow = '0 0 8px var(--accent-green)';
  }

  const scoreEl = document.getElementById('ww-monitor-score-val');
  const peakEl = document.getElementById('ww-monitor-peak-val');
  const threshEl = document.getElementById('ww-monitor-thresh-val');
  const rmsEl = document.getElementById('ww-monitor-rms-val');
  const barEl = document.getElementById('ww-monitor-score-bar');
  const lineEl = document.getElementById('ww-monitor-thresh-line');
  const devNameEl = document.getElementById('ww-monitor-device-name');
  const logEl = document.getElementById('ww-monitor-events-log');

  const isTriggered = info.score >= info.threshold;
  if (scoreEl) {
    scoreEl.innerText = info.score.toFixed(3);
    scoreEl.style.color = isTriggered ? 'var(--accent-green)' : (info.score >= 0.20 ? '#58a6ff' : '#8b949e');
  }
  if (peakEl) {
    peakEl.innerText = `Пик: ${info.peak.toFixed(3)}`;
    peakEl.style.borderColor = (info.peak >= info.threshold) ? 'var(--accent-green)' : 'rgba(88, 166, 255, 0.25)';
    peakEl.style.color = (info.peak >= info.threshold) ? 'var(--accent-green)' : '#58a6ff';
  }
  if (threshEl) {
    threshEl.innerText = `Порог: ${info.threshold.toFixed(2)}`;
  }
  if (lineEl) {
    lineEl.style.left = `${Math.min(100, Math.max(0, info.threshold * 100))}%`;
  }
  if (barEl) {
    barEl.style.width = `${Math.min(100, Math.round(info.score * 100))}%`;
    barEl.style.background = isTriggered 
      ? 'var(--accent-green)' 
      : 'linear-gradient(90deg, #58a6ff, #00d2ff)';
  }
  if (rmsEl) {
    let qual = 'Тишина';
    let qualColor = '#8b949e';
    if (info.rms > -16) {
      qual = 'Очень громко';
      qualColor = 'var(--accent-amber)';
    } else if (info.rms > -32) {
      qual = 'Хорошая слышимость';
      qualColor = 'var(--accent-green)';
    } else if (info.rms > -46) {
      qual = 'Тихая речь';
      qualColor = 'var(--accent-cyan)';
    }
    rmsEl.innerHTML = `<span style="color:${qualColor};">${info.rms.toFixed(1)} dBFS</span> (${qual})`;
  }
  if (devNameEl) {
    devNameEl.innerText = `Поток: ${info.name || info.mac}`;
  }

  // Запись в журнал при сработке или пике
  const now = Date.now();
  if (isTriggered && (now - lastWwMonitorTriggerTime > 1500)) {
    lastWwMonitorTriggerTime = now;
    if (logEl) {
      const timeStr = new Date().toLocaleTimeString();
      const entry = document.createElement('div');
      entry.innerHTML = `<span style="color:#8b949e;">[${timeStr}]</span> <strong style="color:var(--accent-green);">🎉 СРАБОТКА:</strong> <strong>${info.score.toFixed(3)}</strong> &gt;= ${info.threshold.toFixed(2)} (${escapeHtml(info.name)})`;
      if (logEl.children.length === 1 && logEl.children[0].style.fontStyle === 'italic') {
        logEl.innerHTML = '';
      }
      logEl.prepend(entry);
      while (logEl.children.length > 5) {
        logEl.removeChild(logEl.lastChild);
      }
    }
  }
};

// Активный опрос телеметрии для вкладки Мозг & Вейкворд (гарантирует отклик при буферизации Ingress)
let wwPollingTimer = null;

window.startWwMonitorPolling = function() {
  if (wwPollingTimer) return;
  wwPollingTimer = setInterval(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/ww_monitor`);
      if (!res.ok) return;
      const data = await res.json();

      // Обновление бейджа статуса нейросети
      const engineBadge = document.getElementById('ww-engine-status-badge');
      if (engineBadge && data.__engine__) {
        const eng = data.__engine__;
        if (eng.is_ready) {
          engineBadge.style.background = 'rgba(46, 160, 67, 0.15)';
          engineBadge.style.color = '#3fb950';
          engineBadge.innerText = `🟢 Модель: ${eng.model_name || 'porfiriy'} активна`;
          engineBadge.title = 'Серверный детектор openWakeWord успешно загружен и готов';
        } else {
          engineBadge.style.background = 'rgba(248, 81, 73, 0.15)';
          engineBadge.style.color = '#f85149';
          engineBadge.innerText = eng.error ? `🔴 ${eng.error}` : '🔴 Нейросеть не готова';
          engineBadge.title = eng.error || 'Ошибка загрузки модели вейкворда';
        }
      }

      const sel = document.getElementById('ww-monitor-stream-select');
      const activeMac = sel ? sel.value : 'auto';
      
      let target = null;
      if (activeMac !== 'auto' && data[activeMac]) {
        target = data[activeMac];
      } else {
        const keys = Object.keys(data).filter(k => k !== '__engine__');
        if (keys.length > 0) {
          target = data[keys[0]];
        }
      }
      if (target && typeof updateWwMonitorTelemetry === 'function') {
        updateWwMonitorTelemetry({
          mac: target.mac,
          name: target.name,
          score: target.score,
          peak: target.peak,
          rms: target.rms,
          threshold: target.threshold
        });
      }
    } catch (e) {}
  }, 250);
};

window.stopWwMonitorPolling = function() {
  if (wwPollingTimer) {
    clearInterval(wwPollingTimer);
    wwPollingTimer = null;
  }
};

// Server-Sent Events (Live Telemetry)
function setupSSE() {
  try {
    const evtSource = new EventSource(`${API_BASE}/api/events`);
    evtSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.event === 'mic_test_started') {
          // Не сбрасываем карточки при старте микротеста
          return;
        } else if (data.event === 'mic_test_ready') {
          const info = data.device || data.data;
          if (info && info.mac) {
            checkMicTestReady(info.mac);
          }
          return;
        } else if (data.event === 'last_utterance_ready') {
          const info = data.device || data.data;
          if (info && info.mac) {
            const statusEl = document.getElementById(`mic-status-${info.mac}`);
            if (statusEl) statusEl.innerHTML = `<span style="color: var(--accent-blue);" title="Свежая реплика готова к прослушиванию">💬 Реплика готова</span>`;
          }
          return;
        } else if (data.event === 'ww_score' && data.device && data.device.mac) {
          const info = data.device;
          const mac = info.mac;
          const score = info.ww_score || 0;
          const peak = info.ww_peak !== undefined ? info.ww_peak : score;
          const rms = info.rms_dbfs !== undefined ? info.rms_dbfs : -60.0;
          const threshold = info.threshold || 0.94;
          const name = info.name || mac;

          // Обновляем в локальном стейте
          const devIdx = devices.findIndex(d => d.mac === mac);
          if (devIdx >= 0) {
            devices[devIdx].ww_score = score;
            devices[devIdx].ww_peak = peak;
            devices[devIdx].ww_rms_dbfs = rms;
            if (info.infer_ms !== undefined) devices[devIdx].ww_infer_ms = info.infer_ms;
          }

          // Обновляем встроенную мини-отладку в карточке Яндекс Станции (если открыта)
          const stVal = document.getElementById(`st-ww-val-${mac}`);
          const stMeta = document.getElementById(`st-ww-meta-${mac}`);
          const stBar = document.getElementById(`st-ww-bar-${mac}`);
          if (stVal) stVal.innerText = `Score: ${score.toFixed(2)} / Peak: ${peak.toFixed(2)}`;
          if (stMeta) stMeta.innerText = `RMS: ${rms.toFixed(1)} dBFS | ${info.infer_ms !== undefined ? info.infer_ms + ' ms' : '--'}`;
          if (stBar) {
            stBar.style.width = `${Math.min(100, Math.round(score * 100))}%`;
            stBar.style.background = score >= threshold ? '#22c55e' : (score >= 0.4 ? '#f59e0b' : '#34d399');
          }

          // Обновляем централизованный монитор на вкладке Интеллект
          if (typeof updateWwMonitorTelemetry === 'function') {
            updateWwMonitorTelemetry({
              mac,
              name,
              score,
              peak,
              rms,
              threshold
            });
          }
          return;
        } else if (data.event === 'device_deleted' && data.device && data.device.mac) {
          const delMac = data.device.mac;
          devices = devices.filter(d => (d.mac || '').toLowerCase() !== delMac.toLowerCase());
          const card = document.getElementById(`card-${delMac}`);
          if (card) {
            card.remove();
          } else {
            renderDevicesGrid();
          }
          renderBulkTargets();
          renderOtaBanner();
          updateHeaderStats();
          if (typeof populateWwMonitorSources === 'function') {
            populateWwMonitorSources();
          }
          return;
        } else if (data.device && data.device.mac) {
          // Обработка специального прогресса OTA
          if (data.event === 'ota_progress') {
            const info = data.device;
            const dev = devices.find(d => d.mac === info.mac);
            if (dev) {
              dev.ota_progress = info.percent;
              dev.ota_status = info.status;
              renderDevicesGrid();
              renderOtaBanner();
            }
          } else {
            // Обновляем устройство в локальном массиве со слиянием свойств
            const idx = devices.findIndex(d => d.mac === data.device.mac);
            if (idx >= 0) {
              devices[idx] = Object.assign({}, devices[idx], data.device);
            } else {
              devices.push(data.device);
            }
            // Если на этом устройстве сейчас идет обратный отсчет теста, не сбрасываем DOM карточки
            if (!window.micTestTimers[data.device.mac]) {
              renderDevicesGrid();
            }
            renderBulkTargets();
            renderOtaBanner();
            updateHeaderStats();
            if (typeof populateWwMonitorSources === 'function') {
              populateWwMonitorSources();
            }
          }
        }
      } catch (err) {}
    };

    evtSource.onerror = () => {
      evtSource.close();
      setTimeout(setupSSE, 5000);
    };
  } catch (err) {
    console.warn('SSE not supported or failed, falling back to polling');
    setInterval(fetchDevices, 10000);
  }
}

// Всплывающее уведомление
function showToast(msg, isError = false) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = msg;
  toast.style.borderColor = isError ? 'var(--accent-red)' : 'var(--accent-green)';
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 3000);
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Явный экспорт всех интерактивных функций на объект window
function handleDuckingModeChange(mode) {
  const container = document.getElementById('ducking-factor-container');
  if (container) {
    container.style.display = (mode === 'disabled') ? 'none' : 'block';
  }
}

window.handleDuckingModeChange = handleDuckingModeChange;
window.setupTabs = setupTabs;
window.fetchDevices = fetchDevices;
window.fetchGlobalOptions = fetchGlobalOptions;
window.handleBulkSubmit = handleBulkSubmit;
window.toggleSelectAll = toggleSelectAll;
window.updateSingleDeviceConfig = updateSingleDeviceConfig;
window.triggerDeviceAction = triggerDeviceAction;
window.renderBrainInfo = renderBrainInfo;
window.populateModelsDropdown = populateModelsDropdown;
window.handleModelSelectChange = handleModelSelectChange;
window.refreshModelsList = refreshModelsList;
window.restoreDefaultPrompt = restoreDefaultPrompt;
window.populateGlobalSettingsForm = populateGlobalSettingsForm;
window.updatePromptCharCounters = updatePromptCharCounters;
window.toggleKeyVisibility = toggleKeyVisibility;
window.toggleMaKeyVisibility = toggleMaKeyVisibility;
window.updateDuckingFactorDisplay = updateDuckingFactorDisplay;
window.populateMediaPlayersDropdown = populateMediaPlayersDropdown;
window.refreshMediaPlayersList = refreshMediaPlayersList;
window.saveGlobalSettings = saveGlobalSettings;
window.regeneratePhrases = regeneratePhrases;
window.checkPhrasesStatus = checkPhrasesStatus;
window.renderOtaBanner = renderOtaBanner;
window.handleBulkOta = handleBulkOta;
window.triggerDeviceOta = triggerDeviceOta;
window.showToast = showToast;

// ------------------------------------------------------------------ //
// pc_streamer: Сохранение настроек стримера
// ------------------------------------------------------------------ //

async function saveStreamerConfig(mac, field, value) {
  try {
    const d = devices.find(x => x.mac === mac);
    if (d) {
      if (!d.config) d.config = {};
      d.config[field] = value;
    }
    const payload = {};
    payload[field] = value;
    if (field === 'response_player') {
      const mode = value ? 'external_player' : 'stream';
      payload['audio_output_mode'] = mode;
      if (d && d.config) d.config['audio_output_mode'] = mode;
    }
    const res = await fetch(`${API_BASE}/api/devices/${mac}/streamer_config`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      showToast(`Настройка ${field} стримера обновлена`);
    } else {
      showToast(`Ошибка: ${res.status}`, true);
    }
  } catch (err) {
    showToast('Ошибка обновления стримера', true);
  }
}
window.saveStreamerConfig = saveStreamerConfig;

// Прослушать последний ответ Gemini для pc_streamer
async function playStreamerResponse(mac) {
  const url = `${API_BASE}/api/virtual/${encodeURIComponent(mac)}/response.wav?t=${Date.now()}`;
  try {
    const res = await fetch(url, { method: 'HEAD' });
    if (!res.ok) {
      showToast('Ответ Gemini ещё не получен для этого стримера', true);
      return;
    }
    const audio = new Audio(url);
    audio.play();
    showToast('▶ Воспроизведение ответа Gemini...');
  } catch (err) {
    showToast('Ошибка воспроизведения', true);
  }
}
window.playStreamerResponse = playStreamerResponse;

// Загрузка нового .onnx файла модели вейкворда
async function uploadWwModel(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('model', file);
  showToast('Загружаю модель...');
  try {
    const res = await fetch(`${API_BASE}/api/virtual/upload_model`, {
      method: 'POST',
      body: formData
    });
    const data = await res.json();
    if (data.success) {
      const modelPathEl = document.getElementById('cfg-ww-model-path');
      if (modelPathEl) modelPathEl.value = data.path || file.name;
      showToast(`Модель загружена: ${file.name} (${Math.round((data.size || 0) / 1024)} KB)`);
    } else {
      showToast(`Ошибка загрузки: ${data.error || 'unknown'}`, true);
    }
  } catch (err) {
    showToast('Сетевая ошибка при загрузке модели', true);
  }
  // Сбросить инпут чтобы можно было загрузить тот же файл повторно
  input.value = '';
}
window.uploadWwModel = uploadWwModel;

// ------------------------------------------------------------------ //
// Яндекс Станция (Quasar Bridge) & OpenAI API
// ------------------------------------------------------------------ //

async function saveYandexConfig(mac, field, value) {
  try {
    const d = devices.find(x => x.mac === mac);
    if (d) {
      if (!d.config) d.config = {};
      d.config[field] = value;
    }
    const res = await fetch(`${API_BASE}/api/devices/${mac}/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [field]: value })
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Настройка «${field}» Яндекс Станции обновлена`);
    } else {
      showToast(`Ошибка: ${data.error || 'Сбой'}`, true);
    }
  } catch (err) {
    showToast('Ошибка сохранения настройки станции', true);
  }
}
window.saveYandexConfig = saveYandexConfig;

function openAddYandexModal() {
  const modal = document.getElementById('modal-add-yandex');
  if (!modal) return;
  modal.style.display = 'flex';
  const sel = document.getElementById('yandex-add-player');
  if (sel) {
    let html = '<option value="">— Не задан —</option>';
    if (availableMediaPlayers && availableMediaPlayers.length > 0) {
      availableMediaPlayers.forEach(p => {
        html += `<option value="${escapeHtml(p.entity_id)}">${escapeHtml(p.name)} (${escapeHtml(p.entity_id)})</option>`;
      });
    }
    sel.innerHTML = html;
  }
}
window.openAddYandexModal = openAddYandexModal;

function closeAddYandexModal() {
  const modal = document.getElementById('modal-add-yandex');
  if (modal) modal.style.display = 'none';
}
window.closeAddYandexModal = closeAddYandexModal;

async function submitAddYandexStation() {
  const name = document.getElementById('yandex-add-name')?.value?.trim() || 'Яндекс Станция';
  const id = document.getElementById('yandex-add-id')?.value?.trim();
  const ip = document.getElementById('yandex-add-ip')?.value?.trim() || '';
  const mode = document.getElementById('yandex-add-mode')?.value || 'alice_tts';
  const player = document.getElementById('yandex-add-player')?.value || '';
  const key = document.getElementById('yandex-add-key')?.value?.trim() || '';

  showToast('Добавляю Яндекс Станцию...');
  try {
    const res = await fetch(`${API_BASE}/api/devices/yandex/add`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: name,
        id: id,
        ip: ip,
        output_mode: mode,
        mass_player: player,
        airplay_player: player,
        api_key: key
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast('Яндекс Станция успешно добавлена!');
      closeAddYandexModal();
      fetchDevices();
    } else {
      showToast(`Ошибка: ${data.error || 'Сбой'}`, true);
    }
  } catch (e) {
    showToast('Сетевая ошибка при добавлении станции', true);
  }
}
window.submitAddYandexStation = submitAddYandexStation;

function toggleOpenAiKeyVisibility() {
  togglePasswordField('cfg-openai-api-key');
}
window.toggleOpenAiKeyVisibility = toggleOpenAiKeyVisibility;

// ==========================================
// Quasar Bridge Routing Management & Update
// ==========================================
let currentQuasarMac = '';
let currentQuasarIp = '';

async function editDeviceIp(mac) {
  const dev = devices.find(x => x.mac === mac);
  const currentIp = dev?.ip || '';
  const newIp = prompt(`Введите актуальный IP-адрес Яндекс Станции (${dev?.name || mac}):`, currentIp);
  if (newIp === null) return;
  const trimmed = newIp.trim();
  if (!trimmed || trimmed === currentIp) return;
  await saveYandexConfig(mac, 'ip', trimmed);
  if (dev) dev.ip = trimmed;
  renderDevicesGrid();
}
window.editDeviceIp = editDeviceIp;

const quasarVersionChecked = {};

function checkStationVersionInBackground(mac) {
  if (!mac || quasarVersionChecked[mac]) return;
  quasarVersionChecked[mac] = true;
  fetch(`${API_BASE}/api/quasar/version?mac=${encodeURIComponent(mac)}`)
    .then(r => r.json())
    .then(data => {
      if (data && data.success) {
        const d = devices.find(x => x.mac === mac);
        if (d) {
          const changed = (d.firmware !== data.installed_version || d.has_update !== data.has_update);
          d.firmware = data.installed_version;
          d.target_firmware = data.bundled_version;
          d.has_update = data.has_update;
          if (data.squeezelite_running !== undefined) d.squeezelite_running = data.squeezelite_running;
          if (data.airplay_running !== undefined) d.airplay_running = data.airplay_running;
          if (changed) renderDevicesGrid();
        }
      }
    })
    .catch(() => {});
}
window.checkStationVersionInBackground = checkStationVersionInBackground;

async function updateQuasarComponents(mac) {
  const dev = devices.find(x => x.mac === mac);
  const ip = dev?.ip || dev?.config?.ip || '';
  const targetVer = dev?.target_firmware || window.BUNDLED_QUASAR_VERSION || '1.2.4-porfiriy.19';
  if (!confirm(`Обновить компоненты Quasar Bridge, Squeezelite и AirPlay на Станции (${ip || mac}) до версии Porfiriy Edition (${targetVer}) по SSH с авто-привязкой IP сервера?`)) {
    return;
  }

  const btn = document.getElementById(`quasar-update-btn-${mac}`);
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '⏳ Подключение по SSH (0с)...';
    btn.classList.remove('quasar-update-btn-needed');
    btn.style.opacity = '0.7';
  }

  let updateSeconds = 0;
  const updateTimer = setInterval(() => {
    updateSeconds++;
    const currentBtn = document.getElementById(`quasar-update-btn-${mac}`);
    if (currentBtn) {
      if (updateSeconds < 5) {
        currentBtn.innerHTML = `⏳ Подключение по SSH (${updateSeconds}с)...`;
      } else if (updateSeconds < 12) {
        currentBtn.innerHTML = `⏳ Установка пакета (${updateSeconds}с)...`;
      } else {
        currentBtn.innerHTML = `⏳ Перезапуск служб (${updateSeconds}с)...`;
      }
    }
  }, 1000);

  showToast('Подключение по SSH и обновление пакета на Станции...');
  try {
    const res = await fetch(`${API_BASE}/api/quasar/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mac: mac, ip: ip })
    });
    const data = await res.json();
    clearInterval(updateTimer);
    if (data.success) {
      const finalVer = data.version || targetVer;
      showToast(`Пакет Станции успешно обновлен до ${finalVer}!`);
      if (dev) {
        dev.firmware = finalVer;
        dev.target_firmware = finalVer;
        dev.has_update = false;
        dev.config = dev.config || {};
        dev.config.quasar_version = finalVer;
        if (data.squeezelite_running !== undefined) dev.squeezelite_running = data.squeezelite_running;
        if (data.airplay_running !== undefined) dev.airplay_running = data.airplay_running;
      }
      quasarVersionChecked[mac] = true;
      renderDevicesGrid();
      fetchDevices();
    } else {
      showToast(`Ошибка обновления: ${data.error || 'сбой'}`, true);
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = `⚡ Обновить (v${targetVer})`;
        btn.classList.add('quasar-update-btn-needed');
        btn.style.opacity = '1';
      }
    }
  } catch (err) {
    clearInterval(updateTimer);
    showToast('Сетевая ошибка при обновлении Quasar', true);
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `⚡ Обновить (v${targetVer})`;
      btn.classList.add('quasar-update-btn-needed');
      btn.style.opacity = '1';
    }
  }
}
window.updateQuasarComponents = updateQuasarComponents;

async function controlStationService(mac, service, action = 'restart') {
  const cfg = service === 'squeezelite'
    ? { label: 'Squeezelite', endpoint: '/api/quasar/squeezelite', key: 'squeezelite_running', cardPrefix: 'sq-badge', modalId: 'quasar-modal-sq-badge' }
    : { label: 'AirPlay', endpoint: '/api/quasar/airplay', key: 'airplay_running', cardPrefix: 'ap-badge', modalId: 'quasar-modal-ap-badge' };

  const dev = devices.find(x => x.mac === mac);
  const ip = dev?.ip || dev?.config?.ip || '';
  showToast(`${cfg.label}: отправка команды ${action}...`);
  try {
    const res = await fetch(`${API_BASE}${cfg.endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mac, ip, action })
    });
    const data = await res.json();
    if (data.success) {
      const isRunning = Boolean(data[cfg.key]);
      showToast(`${cfg.label}: ${data.message || 'команда выполнена'}`);
      if (dev) dev[cfg.key] = isRunning;
      const badge = document.getElementById(`${cfg.cardPrefix}-${mac}`);
      if (badge) {
        badge.className = `badge ${isRunning ? 'badge-success' : 'badge-error'}`;
        badge.textContent = isRunning ? 'В сети' : 'Остановлен';
      }
      const modalBadge = document.getElementById(cfg.modalId);
      if (modalBadge) {
        modalBadge.className = `fw-badge ${isRunning ? 'fw-up-to-date' : 'fw-update-available'}`;
        modalBadge.textContent = isRunning ? 'В сети' : 'Остановлен';
      }
    } else {
      showToast(`Ошибка ${cfg.label}: ${data.error || 'сбой'}`, true);
    }
  } catch (err) {
    showToast(`Сетевая ошибка при управлении ${cfg.label}`, true);
  }
}

async function controlSqueezelite(mac, action = 'restart') {
  return controlStationService(mac, 'squeezelite', action);
}
window.controlSqueezelite = controlSqueezelite;

async function controlAirplay(mac, action = 'restart') {
  return controlStationService(mac, 'airplay', action);
}
window.controlAirplay = controlAirplay;

async function openQuasarRoutingModal(mac) {
  currentQuasarMac = mac;
  const dev = devices.find(d => d.mac === mac);
  currentQuasarIp = dev?.ip || dev?.config?.ip || '';
  
  const badge = document.getElementById('quasar-modal-ip-badge');
  if (badge) badge.textContent = currentQuasarIp || mac;
  
  const modal = document.getElementById('modal-quasar-routing');
  if (modal) modal.style.display = 'flex';

  const statusEl = document.getElementById('quasar-save-status');
  if (statusEl) statusEl.textContent = 'Загрузка текущих правил с колонки...';
  
  const testRes = document.getElementById('quasar-test-result');
  if (testRes) testRes.style.display = 'none';

  // Опрашиваем статус компонентов, Squeezelite и AirPlay
  try {
    fetch(`${API_BASE}/api/quasar/version?mac=${encodeURIComponent(mac)}`)
      .then(r => r.json())
      .then(vData => {
        if (vData && vData.success) {
          const sqBadge = document.getElementById('quasar-modal-sq-badge');
          if (sqBadge) {
            sqBadge.className = `fw-badge ${vData.squeezelite_running ? 'fw-up-to-date' : 'fw-update-available'}`;
            sqBadge.textContent = vData.squeezelite_running ? 'В сети' : 'Остановлен';
          }
          const apBadge = document.getElementById('quasar-modal-ap-badge');
          if (apBadge) {
            apBadge.className = `fw-badge ${vData.airplay_running ? 'fw-up-to-date' : 'fw-update-available'}`;
            apBadge.textContent = vData.airplay_running ? 'В сети' : 'Остановлен';
          }
          const hostBadge = document.getElementById('quasar-modal-host-badge');
          if (hostBadge) {
            hostBadge.textContent = `🌐 Сервер: ${vData.bound_server_ip || 'auto'}`;
          }
        }
      }).catch(() => {});
  } catch (e) {}

  try {
    const res = await fetch(`${API_BASE}/api/quasar/config?mac=${encodeURIComponent(mac)}`);
    const data = await res.json();
    if (data.success) {
      if (statusEl) statusEl.textContent = '';
      const shEl = document.getElementById('quasar-sh-keywords');
      if (shEl) {
        shEl.value = (data.smart_home_keywords || []).join(', ');
        updateQuasarShCount();
      }
      const timerEl = document.getElementById('quasar-timer-keywords');
      if (timerEl) {
        timerEl.value = (data.timer_keywords || []).join(', ');
      }
      const llmEl = document.getElementById('quasar-llm-keywords');
      if (llmEl) {
        llmEl.value = (data.llm_keywords || []).join(', ');
      }
      const musicEl = document.getElementById('quasar-music-exceptions');
      if (musicEl) {
        musicEl.value = (data.music_exceptions || []).join(', ');
      }
    } else {
      if (statusEl) statusEl.textContent = `Ошибка: ${data.error || 'не удалось загрузить'}`;
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = 'Сетевая ошибка при чтении конфига';
  }
}
window.openQuasarRoutingModal = openQuasarRoutingModal;

function closeQuasarRoutingModal() {
  const modal = document.getElementById('modal-quasar-routing');
  if (modal) modal.style.display = 'none';
  currentQuasarMac = '';
  currentQuasarIp = '';
}
window.closeQuasarRoutingModal = closeQuasarRoutingModal;

function updateQuasarShCount() {
  const shEl = document.getElementById('quasar-sh-keywords');
  const countEl = document.getElementById('quasar-sh-count');
  if (!shEl || !countEl) return;
  const list = shEl.value.split(/[,;\n]+/).map(s => s.trim()).filter(Boolean);
  countEl.textContent = `(${list.length} слов)`;
}
window.updateQuasarShCount = updateQuasarShCount;

async function extractHaKeywordsForQuasar() {
  const shEl = document.getElementById('quasar-sh-keywords');
  if (!shEl) return;
  
  showToast('Считываю устройства и синонимы из Home Assistant...');
  try {
    const res = await fetch(`${API_BASE}/api/quasar/extract-ha-keywords`);
    const data = await res.json();
    if (data.success && data.keywords) {
      const existing = shEl.value.split(/[,;\n]+/).map(s => s.trim().toLowerCase()).filter(Boolean);
      const merged = Array.from(new Set([...existing, ...data.keywords])).sort();
      shEl.value = merged.join(', ');
      updateQuasarShCount();
      showToast(`Успешно собрано ${data.count} ключевых слов из HA!`);
    } else {
      showToast(`Ошибка сбора: ${data.error || 'нет данных'}`, true);
    }
  } catch (e) {
    showToast('Сетевая ошибка при сборе данных из HA', true);
  }
}
window.extractHaKeywordsForQuasar = extractHaKeywordsForQuasar;

async function testQuasarRoutePhrase() {
  const input = document.getElementById('quasar-test-input');
  const resultEl = document.getElementById('quasar-test-result');
  if (!input || !resultEl) return;
  
  const phrase = input.value.trim();
  if (!phrase) {
    showToast('Введите фразу для проверки', true);
    return;
  }

  resultEl.style.display = 'block';
  resultEl.style.background = 'rgba(255,255,255,0.05)';
  resultEl.style.color = '#8b949e';
  resultEl.textContent = 'Опрос маршрутизатора колонки...';

  try {
    const res = await fetch(`${API_BASE}/api/quasar/test-route`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mac: currentQuasarMac,
        ip: currentQuasarIp,
        phrase: phrase
      })
    });
    const data = await res.json();
    if (data.success) {
      if (data.route === 'yandex') {
        resultEl.style.background = 'rgba(52, 211, 153, 0.15)';
        resultEl.style.border = '1px solid rgba(52, 211, 153, 0.3)';
        resultEl.style.color = '#34d399';
        resultEl.innerHTML = `🗣️ <strong>Маршрут: Алиса (Yandex Cloud)</strong> • Правило: <code>${escapeHtml(data.rule)}</code>`;
      } else {
        resultEl.style.background = 'rgba(167, 139, 250, 0.15)';
        resultEl.style.border = '1px solid rgba(167, 139, 250, 0.3)';
        resultEl.style.color = '#c4b5fd';
        resultEl.innerHTML = `🧠 <strong>Маршрут: Порфирий (Gemini Live)</strong> • Правило: <code>${escapeHtml(data.rule || 'по умолчанию')}</code>`;
      }
    } else {
      resultEl.style.background = 'rgba(239, 68, 68, 0.15)';
      resultEl.style.color = '#f87171';
      resultEl.textContent = `Ошибка проверки: ${data.error || 'сбой'}`;
    }
  } catch (e) {
    resultEl.style.background = 'rgba(239, 68, 68, 0.15)';
    resultEl.style.color = '#f87171';
    resultEl.textContent = 'Сетевая ошибка при проверке маршрута';
  }
}
window.testQuasarRoutePhrase = testQuasarRoutePhrase;

async function submitSaveQuasarConfig() {
  const btn = document.getElementById('quasar-btn-save');
  const statusEl = document.getElementById('quasar-save-status');
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.textContent = 'Валидация и перезапуск на колонке...';

  const shKeywords = (document.getElementById('quasar-sh-keywords')?.value || '').split(/[,;\n]+/).map(s => s.trim().toLowerCase()).filter(Boolean);
  const timerKeywords = (document.getElementById('quasar-timer-keywords')?.value || '').split(/[,;\n]+/).map(s => s.trim().toLowerCase()).filter(Boolean);
  const llmKeywords = (document.getElementById('quasar-llm-keywords')?.value || '').split(/[,;\n]+/).map(s => s.trim().toLowerCase()).filter(Boolean);
  const musicExceptions = (document.getElementById('quasar-music-exceptions')?.value || '').split(/[,;\n]+/).map(s => s.trim().toLowerCase()).filter(Boolean);

  try {
    const res = await fetch(`${API_BASE}/api/quasar/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mac: currentQuasarMac,
        ip: currentQuasarIp,
        smart_home_keywords: shKeywords,
        timer_keywords: timerKeywords,
        llm_keywords: llmKeywords,
        music_exceptions: musicExceptions,
        default_route: 'llm'
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast('Правила успешно обновлены и применены в Станцию!');
      if (statusEl) statusEl.textContent = '✓ Сохранено и перезапущено';
      setTimeout(() => {
        closeQuasarRoutingModal();
      }, 1000);
    } else {
      if (statusEl) statusEl.textContent = `Ошибка: ${data.error || 'сбой'}`;
      showToast(`Ошибка сохранения: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = 'Сетевая ошибка при сохранении';
    showToast('Сетевая ошибка при сохранении конфига', true);
  } finally {
    if (btn) btn.disabled = false;
  }
}
window.submitSaveQuasarConfig = submitSaveQuasarConfig;

// ------------------------------------------------------------------ //
// Звуковые сигналы (Earcons) и нативный режим Яндекс Станции 2
// ------------------------------------------------------------------ //

function previewEarcon(category, preset) {
  if (!preset || preset === 'none') {
    showToast('Звук выключен (none)');
    return;
  }
  const audio = new Audio(`${API_BASE}/api/earcons/preview/${encodeURIComponent(category)}/${encodeURIComponent(preset)}?t=${Date.now()}`);
  audio.play().catch(e => showToast(`Ошибка воспроизведения: ${e.message}`, true));
}
window.previewEarcon = previewEarcon;

async function toggleStationAlice(mac, disableAlice) {
  showToast(disableAlice ? 'Отключаю Алису и включаю локальный openWakeWord...' : 'Возвращаю Алису и Quasar Bridge...');
  try {
    const res = await fetch(`${API_BASE}/api/station/${encodeURIComponent(mac)}/toggle_alice`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disable_alice: Boolean(disableAlice) })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || (disableAlice ? 'Нативный режим Порфирия включён' : 'Алиса восстановлена'));
      const dev = devices.find(d => d.mac === mac);
      if (dev) {
        dev.config = dev.config || {};
        dev.config.disable_alice = Boolean(disableAlice);
        renderDevicesGrid();
      }
    } else {
      showToast(`Ошибка переключения: ${data.error || 'сбой'}`, true);
      renderDevicesGrid();
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
    renderDevicesGrid();
  }
}
window.toggleStationAlice = toggleStationAlice;

async function controlStationd(mac, action) {
  showToast(action === 'deploy' ? 'Заливаю porfiriy-stationd и ONNX модели на Станцию...' : `Выполняю ${action} для porfiriy-stationd...`);
  try {
    const res = await fetch(`${API_BASE}/api/station/${encodeURIComponent(mac)}/stationd`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || `porfiriy-stationd: ${data.stationd_running ? 'работает' : 'остановлен'}`);
    } else {
      showToast(`Ошибка stationd: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.controlStationd = controlStationd;

async function uploadStationWwModel(mac, inputEl) {
  const file = inputEl && inputEl.files && inputEl.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('model', file);
  showToast(`Загружаю ${file.name} на Станцию...`);
  try {
    const res = await fetch(`${API_BASE}/api/station/${encodeURIComponent(mac)}/upload_model`, {
      method: 'POST',
      body: fd
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Модель загружена на Станцию (${Math.round((data.size || 0) / 1024)} КБ)`);
    } else {
      showToast(`Ошибка загрузки модели: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  } finally {
    inputEl.value = '';
  }
}
window.uploadStationWwModel = uploadStationWwModel;

async function dumpAndPlayStationMic(mac) {
  showToast('Запрашиваю 3 сек аудио с микрофона Станции (после AEC)...');
  try {
    const res = await fetch(`${API_BASE}/api/station/${encodeURIComponent(mac)}/dump_mic`, { method: 'POST' });
    const data = await res.json();
    if (!data.success) {
      showToast(`Ошибка: ${data.error || 'Станция не подключена'}`, true);
      return;
    }
    setTimeout(() => {
      const audio = new Audio(`${API_BASE}/api/devices/${encodeURIComponent(mac)}/mic_test/audio?t=${Date.now()}`);
      audio.play().catch(e => showToast(`Ошибка воспроизведения дампа: ${e.message}`, true));
    }, 650);
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.dumpAndPlayStationMic = dumpAndPlayStationMic;

async function loadStationWifi(mac) {
  const listEl = document.getElementById(`st-wifi-list-${mac}`);
  if (listEl) listEl.innerHTML = '<span style="color:#94a3b8;">Загрузка сетей со Станции...</span>';
  try {
    const res = await fetch(`${API_BASE}/api/station/${encodeURIComponent(mac)}/wifi`);
    const data = await res.json();
    if (!data.success) {
      if (listEl) listEl.innerHTML = `<span style="color:#f87171;">Ошибка: ${escapeHtml(data.error || 'SSH сбой')}</span>`;
      return;
    }
    const nets = data.networks || [];
    if (!nets.length) {
      if (listEl) listEl.innerHTML = '<span style="color:#94a3b8;">Сохранённых сетей не найдено.</span>';
      return;
    }
    if (listEl) {
      listEl.innerHTML = nets.map(n => `
        <div style="display:flex; align-items:center; justify-content:space-between; gap:6px; padding:5px 8px; background:#0d1117; border:1px solid ${n.is_active ? 'rgba(52, 211, 153, 0.4)' : '#30363d'}; border-radius:6px;">
          <div style="display:flex; flex-direction:column; gap:2px; min-width:0;">
            <div style="display:flex; align-items:center; gap:6px;">
              <strong style="color:#f0f3f6; font-size:12px;">${escapeHtml(n.ssid)}</strong>
              ${n.is_active ? '<span style="font-size:10px; color:#34d399; background:rgba(52,211,153,0.12); padding:1px 5px; border-radius:4px;">● Активна</span>' : ''}
              <span style="font-size:10px; color:#64748b;">prio: ${n.priority}</span>
            </div>
            <span style="font-size:11px; color:#94a3b8; font-family:monospace;">Сервер: ${escapeHtml(n.server_ip || 'по умолчанию')}</span>
          </div>
          ${!n.is_active ? `<button type="button" class="btn btn-secondary btn-sm" onclick="deleteStationWifi('${mac}', '${escapeHtml(n.ssid).replace(/'/g, "\\'")}')" style="padding:2px 6px; font-size:11px; color:#f87171; border-color:rgba(248,113,113,0.35);" title="Удалить сеть">🗑️</button>` : ''}
        </div>
      `).join('');
    }
  } catch (e) {
    if (listEl) listEl.innerHTML = `<span style="color:#f87171;">Сетевая ошибка: ${escapeHtml(e.message)}</span>`;
  }
}
window.loadStationWifi = loadStationWifi;

async function saveStationWifi(mac) {
  const ssid = document.getElementById(`st-wifi-ssid-${mac}`)?.value?.trim() || '';
  const password = document.getElementById(`st-wifi-pass-${mac}`)?.value || '';
  const server_ip = document.getElementById(`st-wifi-srv-${mac}`)?.value?.trim() || '';
  const priority = parseInt(document.getElementById(`st-wifi-prio-${mac}`)?.value || '10', 10);
  if (!ssid) {
    showToast('Укажите SSID (название Wi-Fi сети)', true);
    return;
  }
  showToast(`Сохраняю Wi-Fi «${ssid}» на Станцию...`);
  try {
    const res = await fetch(`${API_BASE}/api/station/${encodeURIComponent(mac)}/wifi`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ssid, password, server_ip, priority })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || `Сеть «${ssid}» сохранена!`);
      const pEl = document.getElementById(`st-wifi-pass-${mac}`);
      if (pEl) pEl.value = '';
      loadStationWifi(mac);
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.saveStationWifi = saveStationWifi;

async function deleteStationWifi(mac, ssid) {
  if (!confirm(`Удалить Wi-Fi сеть «${ssid}» со Станции?`)) return;
  try {
    const res = await fetch(`${API_BASE}/api/station/${encodeURIComponent(mac)}/wifi`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ssid })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || `Сеть «${ssid}» удалена`);
      loadStationWifi(mac);
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.deleteStationWifi = deleteStationWifi;

// =========================================================================
// Porfiriy 2.0: Пользователи, Плагины / MCP и Журнал вызовов инструментов
// =========================================================================
let porfiriyUsers = [];
let porfiriyFamilyChannels = {};
let porfiriyProviders = [];
window._porfiriyUsersLoaded = false;

async function fetchUsers() {
  try {
    const res = await fetch(`${API_BASE}/api/users`);
    if (!res.ok) return;
    const data = await res.json();
    porfiriyUsers = data.users || [];
    porfiriyFamilyChannels = data.family_channels || {};
    window._porfiriyUsersLoaded = true;
    const famInput = document.getElementById('family-tg-chat-id');
    if (famInput) famInput.value = porfiriyFamilyChannels.telegram_chat || '';
    renderUsersGrid();
    if (devices.some(d => d.device_type === 'wear_os')) {
      renderDevicesGrid();
    }
  } catch (e) {
    console.error('Error fetching users:', e);
  }
}
window.fetchUsers = fetchUsers;

function renderUsersGrid() {
  const grid = document.getElementById('users-grid');
  if (!grid) return;
  if (!porfiriyUsers.length) {
    grid.innerHTML = '<div class="empty-state"><p>Пользователей пока нет. Нажмите «+ Добавить пользователя».</p></div>';
    return;
  }
  const genderMap = {
    male: { label: '♂ Мужской', cls: 'gender-male' },
    female: { label: '♀ Женский', cls: 'gender-female' },
    unknown: { label: '• Не указан', cls: 'gender-unknown' },
  };
  grid.innerHTML = porfiriyUsers.map(u => {
    const g = genderMap[u.gender] || genderMap.unknown;
    const tgText = u.telegram_chat_id
      ? `<span class="p2-chip p2-chip-ok">✈️ TG: <code>${escapeHtml(u.telegram_chat_id)}</code></span>`
      : `<span class="p2-chip p2-chip-muted">✈️ TG не задан</span>`;
    return `
      <div class="card user-card">
        <div class="user-card-head">
          <div>
            <div class="user-card-name">${escapeHtml(u.name)} <code class="user-card-id">${escapeHtml(u.id)}</code></div>
            <span class="gender-badge ${g.cls}">${g.label}</span>
          </div>
          <div style="display:flex; gap:6px;">
            <button type="button" class="btn btn-secondary btn-sm" onclick="openUserModal('${escapeHtml(u.id)}')" title="Редактировать">✏️</button>
            <button type="button" class="btn btn-secondary btn-sm" onclick="deleteUser('${escapeHtml(u.id)}', '${escapeHtml(u.name).replace(/'/g, "\\'")}')" style="color:#f87171; border-color:rgba(248,113,113,0.35);" title="Удалить">🗑️</button>
          </div>
        </div>
        ${u.voice_notes ? `<div class="user-card-meta"><strong>🎙️ Голос:</strong> ${escapeHtml(u.voice_notes)}</div>` : ''}
        ${u.persona_notes ? `<div class="user-card-meta"><strong>📝 Заметки:</strong> ${escapeHtml(u.persona_notes)}</div>` : ''}
        <div class="user-card-chips">
          ${tgText}
          <span class="p2-chip">🔌 Аккаунтов: ${Number(u.linked_accounts_count || 0)}</span>
          <span class="p2-chip">🧠 В памяти: ${Number(u.memory_items_count || 0)}</span>
        </div>
      </div>
    `;
  }).join('');
}

async function saveFamilyChannels() {
  const tgVal = (document.getElementById('family-tg-chat-id')?.value || '').trim();
  try {
    const res = await fetch(`${API_BASE}/api/family/channels`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ telegram_chat: tgVal })
    });
    const data = await res.json();
    if (data.success) {
      porfiriyFamilyChannels = data.family_channels || {};
      showToast('Общий чат дома сохранён');
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.saveFamilyChannels = saveFamilyChannels;

function openUserModal(userId) {
  const modal = document.getElementById('modal-user-edit');
  if (!modal) return;
  const existing = userId ? porfiriyUsers.find(u => u.id === userId) : null;
  document.getElementById('user-modal-title').textContent = existing ? `✏️ Редактировать: ${existing.name}` : '👤 Новый пользователь';
  document.getElementById('user-edit-id').value = existing ? existing.id : '';
  document.getElementById('user-edit-name').value = existing ? existing.name : '';
  document.getElementById('user-edit-gender').value = existing ? (existing.gender || 'unknown') : 'male';
  document.getElementById('user-edit-voice').value = existing ? (existing.voice_notes || '') : '';
  document.getElementById('user-edit-persona').value = existing ? (existing.persona_notes || '') : '';
  document.getElementById('user-edit-tg').value = existing ? (existing.telegram_chat_id || '') : '';
  modal.style.display = 'flex';
}
window.openUserModal = openUserModal;

function closeUserModal() {
  const modal = document.getElementById('modal-user-edit');
  if (modal) modal.style.display = 'none';
}
window.closeUserModal = closeUserModal;

async function submitUserModal() {
  const id = (document.getElementById('user-edit-id')?.value || '').trim();
  const name = (document.getElementById('user-edit-name')?.value || '').trim();
  const gender = document.getElementById('user-edit-gender')?.value || 'unknown';
  const voice_notes = (document.getElementById('user-edit-voice')?.value || '').trim();
  const persona_notes = (document.getElementById('user-edit-persona')?.value || '').trim();
  const telegram_chat_id = (document.getElementById('user-edit-tg')?.value || '').trim();
  if (!name) {
    showToast('Укажите имя пользователя', true);
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/users`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id || undefined, name, gender, voice_notes, persona_notes, telegram_chat_id })
    });
    const data = await res.json();
    if (data.success) {
      closeUserModal();
      showToast(`Пользователь «${name}» сохранён`);
      fetchUsers();
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.submitUserModal = submitUserModal;

async function deleteUser(userId, userName) {
  if (!confirm(`Удалить пользователя «${userName}»? Привязанные аккаунты и память станут общими (Дом).`)) return;
  try {
    const res = await fetch(`${API_BASE}/api/users/${encodeURIComponent(userId)}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      showToast(`Пользователь «${userName}» удалён`);
      fetchUsers();
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.deleteUser = deleteUser;

// -------------------------------------------------------------------------
// Plugins & MCP Providers
// -------------------------------------------------------------------------
async function fetchProviders() {
  try {
    const res = await fetch(`${API_BASE}/api/providers`);
    if (!res.ok) return;
    const data = await res.json();
    porfiriyProviders = data.providers || [];
    renderProviders();
  } catch (e) {
    console.error('Error fetching providers:', e);
  }
}
window.fetchProviders = fetchProviders;

function renderProviders() {
  const builtinEl = document.getElementById('builtin-plugins-list');
  const extEl = document.getElementById('external-providers-list');
  if (!builtinEl || !extEl) return;

  const builtins = porfiriyProviders.filter(p => p.kind === 'builtin');
  const externals = porfiriyProviders.filter(p => p.kind !== 'builtin');

  builtinEl.innerHTML = builtins.length
    ? builtins.map(renderBuiltinPluginCard).join('')
    : '<div class="empty-state"><p>Встроенные плагины не найдены.</p></div>';

  extEl.innerHTML = externals.length
    ? externals.map(renderExternalProviderCard).join('')
    : '<div class="empty-state"><p>Внешние MCP-серверы и LLM-делегаты пока не добавлены.</p></div>';
}

function renderBuiltinConfigForm(p) {
  const cfg = p.config || {};
  const pid = escapeHtml(p.id);
  if (p.id === 'builtin.telegram_bot') {
    const mode = cfg.mode || 'ha_notify';
    const targetOpts = [
      ...porfiriyUsers.map(u => `<option value="${escapeHtml(u.id)}">👤 ${escapeHtml(u.name)}${u.telegram_chat_id ? '' : ' (нет chat_id)'}</option>`),
      `<option value="family">🏠 Общий чат дома</option>`
    ].join('');
    return `
      <div class="plugin-config-box">
        <div class="p2-config-grid">
          <div class="form-group" style="margin:0;">
            <label>Режим отправки</label>
            <select id="cfg-tgbot-mode">
              <option value="ha_notify" ${mode === 'ha_notify' ? 'selected' : ''}>Home Assistant (notify.*)</option>
              <option value="direct_bot" ${mode === 'direct_bot' ? 'selected' : ''}>Прямой Telegram Bot API</option>
            </select>
          </div>
          <div class="form-group" style="margin:0;">
            <label>HA сервис (для HA notify)</label>
            <input type="text" id="cfg-tgbot-service" value="${escapeHtml(cfg.ha_service || 'notify.telegram')}" placeholder="notify.telegram">
          </div>
          <div class="form-group" style="margin:0;">
            <label>Bot Token (для прямого API)</label>
            <input type="password" id="cfg-tgbot-token" value="${escapeHtml(cfg.bot_token || '')}" placeholder="123456:ABC-DEF...">
          </div>
        </div>
        <div style="margin-top:10px; display:flex; gap:16px; flex-wrap:wrap;">
          <label class="checkbox-badge" style="cursor:pointer;">
            <input type="checkbox" id="cfg-tgbot-polling" ${cfg.polling_enabled !== false ? 'checked' : ''}>
            <span>📥 Принимать команды из Telegram (текст и голосовые)</span>
          </label>
          <label class="checkbox-badge" style="cursor:pointer;">
            <input type="checkbox" id="cfg-tgbot-voice-reply" ${cfg.reply_voice_to_voice !== false ? 'checked' : ''}>
            <span>🎙️ Отвечать голосом на голосовые сообщения</span>
          </label>
        </div>
        <div style="margin-top:10px; display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
          <div style="display:flex; gap:8px; align-items:center;">
            <select id="cfg-tgbot-test-target" style="padding:5px 8px; font-size:12px;">${targetOpts}</select>
            <button type="button" class="btn btn-secondary btn-sm" onclick="testTelegramBot()">✈️ Отправить тест</button>
          </div>
          <button type="button" class="btn btn-secondary btn-sm" onclick="saveBuiltinConfig('${pid}')">💾 Сохранить настройки бота</button>
        </div>
      </div>
    `;
  }
  if (p.id === 'builtin.memory') {
    return `
      <div class="plugin-config-box">
        <div class="p2-config-grid">
          <div class="form-group" style="margin:0;">
            <label>Авто-очистка (дней)</label>
            <input type="number" id="cfg-mem-cleanup" value="${Number(cfg.auto_cleanup_days ?? 30)}" min="1" max="365">
          </div>
          <div class="form-group" style="margin:0;">
            <label>Архивация выполненного (дней)</label>
            <input type="number" id="cfg-mem-archive" value="${Number(cfg.archive_days ?? 14)}" min="1" max="365">
          </div>
          <div class="form-group" style="margin:0;">
            <label>Лимит активных записей</label>
            <input type="number" id="cfg-mem-limit" value="${Number(cfg.max_active_items ?? 500)}" min="50" max="5000">
          </div>
        </div>
        <div style="margin-top:10px; display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
          <div style="display:flex; gap:8px;">
            <button type="button" class="btn btn-secondary btn-sm" onclick="openMemoryModal()">🧠 Открыть память и списки</button>
            <button type="button" class="btn btn-secondary btn-sm" onclick="runMemoryCleanup(false)">🧹 Очистить сейчас</button>
          </div>
          <button type="button" class="btn btn-secondary btn-sm" onclick="saveBuiltinConfig('${pid}')">💾 Сохранить лимиты</button>
        </div>
      </div>
    `;
  }
  if (p.id === 'builtin.keenetic') {
    const apprUser = cfg.approver_user_id || '';
    const confirmMode = cfg.confirm_mode || 'always';
    const defGroup = cfg.default_group || '';
    const cachedGroups = (cfg.cached_groups && typeof cfg.cached_groups === 'object') ? cfg.cached_groups : {};
    const groupEntries = Object.entries(cachedGroups);
    const groupOptsList = groupEntries.map(([gname, gmeta]) => {
      const desc = (gmeta && gmeta.description) ? ` — ${gmeta.description}` : '';
      const cnt = (gmeta && gmeta.count !== undefined) ? ` (${gmeta.count})` : '';
      return `<option value="${escapeHtml(gname)}" ${defGroup === gname ? 'selected' : ''}>${escapeHtml(gname)}${escapeHtml(desc)}${cnt}</option>`;
    });
    if (defGroup && !cachedGroups[defGroup]) {
      groupOptsList.unshift(`<option value="${escapeHtml(defGroup)}" selected>${escapeHtml(defGroup)}</option>`);
    } else if (groupOptsList.length === 0) {
      groupOptsList.push(`<option value="">— Нажмите «Проверить и найти группы» —</option>`);
    }
    const userOpts = [
      `<option value="">Авто (первый жилец с Telegram)</option>`,
      ...porfiriyUsers.map(u => `<option value="${escapeHtml(u.id)}" ${apprUser === u.id ? 'selected' : ''}>👤 ${escapeHtml(u.name)}</option>`)
    ].join('');
    return `
      <div class="plugin-config-box">
        <div class="p2-config-grid">
          <div class="form-group" style="margin:0;">
            <label>Группа доменов по умолчанию</label>
            <select id="cfg-keen-group">${groupOptsList.join('')}</select>
          </div>
          <div class="form-group" style="margin:0;">
            <label>Получатель согласований (Telegram)</label>
            <select id="cfg-keen-approver">${userOpts}</select>
          </div>
          <div class="form-group full-width" style="margin:0;">
            <label>Режим подтверждения</label>
            <select id="cfg-keen-confirm">
              <option value="always" ${confirmMode === 'always' ? 'selected' : ''}>Всегда через кнопки в Telegram</option>
              <option value="pool_only" ${confirmMode === 'pool_only' ? 'selected' : ''}>Кнопки только для пулов (2+ доменов)</option>
            </select>
          </div>
        </div>

        <details class="p2-advanced-details">
          <summary>⚙️ Расширенные настройки (Entry ID, GeoSite, GeoIP)</summary>
          <div class="p2-config-grid">
            <div class="form-group full-width" style="margin:0;">
              <label>Entry ID интеграции keenetic_api</label>
              <input type="text" id="cfg-keen-entry" value="${escapeHtml(cfg.entry_id || '')}" placeholder="Авто-поиск из keenetic_api">
            </div>
            <div class="form-group full-width" style="margin:0;">
              <label>URL-шаблон GeoSite ({name})</label>
              <input type="text" id="cfg-keen-geosite" value="${escapeHtml(cfg.geosite_url_template || 'https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/{name}')}">
            </div>
            <div class="form-group full-width" style="margin:0;">
              <label>URL-шаблон GeoIP ({query})</label>
              <input type="text" id="cfg-keen-geoip" value="${escapeHtml(cfg.geoip_url_template || 'http://ip-api.com/json/{query}?fields=status,message,country,isp,org,as,query')}">
            </div>
          </div>
        </details>

        <div id="keenetic-test-result" style="font-size:12px; color:#8b949e; margin-top:10px; line-height:1.45;"></div>
        <div style="margin-top:12px; display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
          <button type="button" class="btn btn-secondary btn-sm" onclick="testKeeneticPlugin()">🔌 Проверить и найти группы</button>
          <button type="button" class="btn btn-secondary btn-sm" onclick="saveBuiltinConfig('${pid}')">💾 Сохранить</button>
        </div>
      </div>
    `;
  }
  if (p.id === 'builtin.kinowatch') {
    const isAuth = cfg.auth_status === 'authorized' && !!cfg.access_token;
    const folders = Array.isArray(cfg.cached_folders) ? cfg.cached_folders : [];
    const defFid = String(cfg.default_folder_id || '');
    const folderOpts = folders.length
      ? folders.map(f => `<option value="${escapeHtml(f.id)}" ${defFid === String(f.id) ? 'selected' : ''}>${escapeHtml(f.title)} (${Number(f.count || 0)})</option>`).join('')
      : `<option value="">— Привяжите аккаунт и обновите папки —</option>`;
    const pendingCode = cfg.pending_user_code || '';
    const verifyUri = cfg.verification_uri || 'https://kino.watch/device';
    return `
      <div class="plugin-config-box">
        <div style="display:flex; align-items:center; justify-content:space-between; gap:8px; flex-wrap:wrap; margin-bottom:10px;">
          <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
            <span class="owner-badge owner-home">🏠 Общий для дома</span>
            ${isAuth ? '<span class="p2-chip p2-chip-ok">✅ Авторизован</span>' : '<span class="p2-chip p2-chip-muted">⚪ Не привязан</span>'}
          </div>
          <div style="display:flex; gap:6px; flex-wrap:wrap;">
            <button type="button" class="btn btn-secondary btn-sm" onclick="startKinoWatchAuth()">🔗 Получить код входа</button>
            <button type="button" class="btn btn-secondary btn-sm" onclick="pollKinoWatchAuth()">✅ Проверить вход</button>
          </div>
        </div>

        <div id="kw-auth-box" style="font-size:12px; color:#cbd5e1; margin-bottom:10px; ${pendingCode ? '' : 'display:none;'} padding:8px 10px; background:rgba(56,189,248,0.08); border:1px solid rgba(56,189,248,0.28); border-radius:8px;">
          ${pendingCode ? `Откройте <a href="${escapeHtml(verifyUri)}" target="_blank" style="color:#38bdf8;">${escapeHtml(verifyUri)}</a> и введите код: <strong style="font-size:15px; font-family:monospace; color:#facc15;">${escapeHtml(pendingCode)}</strong>, затем нажмите «✅ Проверить вход».` : ''}
        </div>

        <div class="p2-config-grid">
          <div class="form-group full-width" style="margin:0;">
            <label>Папка закладок / избранного по умолчанию</label>
            <select id="cfg-kw-folder">${folderOpts}</select>
          </div>
        </div>

        <details class="p2-advanced-details" ${(!cfg.client_id || !cfg.client_secret) ? 'open' : ''}>
          <summary>⚙️ Настройки API (Client ID / Secret / URL)</summary>
          <div class="p2-config-grid">
            <div class="form-group full-width" style="margin:0;">
              <label>API Base URL</label>
              <input type="text" id="cfg-kw-api-base" value="${escapeHtml(cfg.api_base || 'https://api.service-kp.com')}">
            </div>
            <div class="form-group" style="margin:0;">
              <label>Client ID</label>
              <input type="text" id="cfg-kw-client-id" value="${escapeHtml(cfg.client_id || '')}" placeholder="Например: kodi">
            </div>
            <div class="form-group" style="margin:0;">
              <label>Client Secret</label>
              <input type="password" id="cfg-kw-client-secret" value="${escapeHtml(cfg.client_secret || '')}" placeholder="Обязательно для OAuth2">
            </div>
          </div>
        </details>

        <div style="margin-top:12px; display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
          <button type="button" class="btn btn-secondary btn-sm" onclick="syncKinoWatchFolders()">🔄 Обновить папки</button>
          <button type="button" class="btn btn-secondary btn-sm" onclick="saveBuiltinConfig('${pid}')">💾 Сохранить</button>
        </div>
      </div>
    `;
  }
  if (p.id === 'builtin.yandex_maps') {
    const hasGeo = !!cfg.geocoder_api_key;
    const hasSug = !!cfg.geosuggest_api_key;
    const autoTg = cfg.auto_send_telegram !== false;
    return `
      <div class="plugin-config-box">
        <div style="display:flex; align-items:center; justify-content:space-between; gap:8px; flex-wrap:wrap; margin-bottom:10px;">
          <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
            <span class="owner-badge owner-home">🗺️ Яндекс.Карты</span>
            ${hasSug ? '<span class="p2-chip p2-chip-ok">✅ Геосаджест</span>' : '<span class="p2-chip p2-chip-muted">⚪ Геосаджест</span>'}
            ${hasGeo ? '<span class="p2-chip p2-chip-ok">✅ Геокодер</span>' : '<span class="p2-chip p2-chip-muted">⚪ Геокодер</span>'}
          </div>
          <button type="button" class="btn btn-secondary btn-sm" onclick="testYandexMaps()">🧪 Проверить ключи</button>
        </div>
        <div class="p2-config-grid">
          <div class="form-group" style="margin:0;">
            <label>API-ключ Геосаджеста (организации)</label>
            <input type="password" id="cfg-ym-geosuggest" value="${escapeHtml(cfg.geosuggest_api_key || '')}" placeholder="suggest-maps.yandex.ru">
          </div>
          <div class="form-group" style="margin:0;">
            <label>API-ключ Геокодера (адреса)</label>
            <input type="password" id="cfg-ym-geocoder" value="${escapeHtml(cfg.geocoder_api_key || '')}" placeholder="geocode-maps.yandex.ru">
          </div>
          <div class="form-group" style="margin:0;">
            <label>Город по умолчанию</label>
            <input type="text" id="cfg-ym-city" value="${escapeHtml(cfg.default_city || 'Москва')}" placeholder="Москва">
          </div>
          <div class="form-group" style="margin:0;">
            <label>Центр поиска (lon,lat)</label>
            <input type="text" id="cfg-ym-ll" value="${escapeHtml(cfg.default_ll || '37.6173,55.7558')}" placeholder="37.6173,55.7558">
          </div>
          <div class="form-group full-width" style="margin:4px 0 0 0;">
            <label class="checkbox-label" style="display:flex; align-items:center; gap:8px; cursor:pointer;">
              <input type="checkbox" id="cfg-ym-autotg" ${autoTg ? 'checked' : ''}>
              <span>Автоматически отправлять карточку со ссылкой в Telegram при каждом поиске</span>
            </label>
          </div>
        </div>
        <div style="margin-top:12px; display:flex; justify-content:flex-end;">
          <button type="button" class="btn btn-secondary btn-sm" onclick="saveBuiltinConfig('${pid}')">💾 Сохранить</button>
        </div>
      </div>
    `;
  }
  return '';
}

function renderAccountsBlock(p) {
  if (p.id !== 'builtin.mail_imap' && p.id !== 'builtin.telegram_userbot') return '';
  const pid = escapeHtml(p.id);
  const accounts = p.accounts || [];
  const rows = accounts.length
    ? accounts.map(a => {
        const ownerBadge = a.owner_user_id
          ? `<span class="owner-badge owner-personal">👤 ${escapeHtml(a.owner_user_name)}</span>`
          : `<span class="owner-badge owner-home">🏠 Общий (Дом)</span>`;
        const hint = a.credentials?.email || a.credentials?.phone || a.credentials?.host || '';
        let authBadge = '';
        let authBtn = '';
        if (p.id === 'builtin.telegram_userbot') {
          const stMap = {
            authorized: '<span class="p2-chip p2-chip-ok">✅ Авторизован</span>',
            awaiting_code: '<span class="p2-chip">⏳ Ждёт код</span>',
            awaiting_2fa: '<span class="p2-chip">🔒 Ждёт 2FA</span>',
            error: `<span class="p2-chip" style="color:#f87171;">⚠️ Ошибка</span>`,
          };
          authBadge = stMap[a.status] || '<span class="p2-chip p2-chip-muted">⚪ Не авторизован</span>';
          authBtn = `<button type="button" class="btn btn-secondary btn-sm" onclick="openTgUserbotAuthModal('${escapeHtml(a.id)}', '${escapeHtml(a.label).replace(/'/g, "\\'")}')" title="Войти в Telegram">🔐 Войти</button>`;
        } else if (p.id === 'builtin.mail_imap') {
          const stMap = {
            authorized: '<span class="p2-chip p2-chip-ok">✅ Подключена</span>',
            error: `<span class="p2-chip" style="color:#f87171;" title="${escapeHtml(a.last_error || '')}">⚠️ Ошибка</span>`,
          };
          authBadge = stMap[a.status] || '<span class="p2-chip p2-chip-muted">⚪ Не проверена</span>';
          authBtn = `<button type="button" class="btn btn-secondary btn-sm" onclick="testMailImapAccount('${escapeHtml(a.id)}')" title="Проверить IMAP вход">🔌 Проверить</button>`;
        }
        return `
          <div class="account-row">
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
              <strong>${escapeHtml(a.label)}</strong>
              ${ownerBadge}
              ${authBadge}
              ${hint ? `<span style="font-size:12px; color:#8b949e; font-family:monospace;">${escapeHtml(hint)}</span>` : ''}
            </div>
            <div style="display:flex; gap:6px;">
              ${authBtn}
              <button type="button" class="btn btn-secondary btn-sm" onclick="openProviderAccountModal('${pid}', '${escapeHtml(a.id)}')" title="Редактировать">✏️</button>
              <button type="button" class="btn btn-secondary btn-sm" onclick="deleteProviderAccount('${escapeHtml(a.id)}', '${escapeHtml(a.label).replace(/'/g, "\\'")}')" style="color:#f87171; border-color:rgba(248,113,113,0.35);" title="Удалить">🗑️</button>
            </div>
          </div>
        `;
      }).join('')
    : '<div style="font-size:12px; color:#8b949e;">Аккаунты пока не подключены.</div>';

  return `
    <div class="plugin-accounts-box">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <span style="font-size:12px; font-weight:600; color:#c9d1d9;">Подключённые аккаунты (${accounts.length})</span>
        <button type="button" class="btn btn-secondary btn-sm" onclick="openProviderAccountModal('${pid}')">+ Добавить аккаунт</button>
      </div>
      <div class="accounts-list">${rows}</div>
    </div>
  `;
}

function renderBuiltinPluginCard(p) {
  const pid = escapeHtml(p.id);
  return `
    <div class="card plugin-card ${p.enabled ? 'plugin-enabled' : ''}">
      <div class="plugin-card-head">
        <div>
          <div class="plugin-title">${escapeHtml(p.title)} <code class="user-card-id">${pid}</code></div>
          <div class="plugin-desc">${escapeHtml(p.description || '')}</div>
        </div>
        <label class="switch" style="margin:0; flex-shrink:0;" title="Включить / выключить плагин">
          <input type="checkbox" ${p.enabled ? 'checked' : ''} onchange="toggleProvider('${pid}', this.checked)">
          <span class="slider"></span>
        </label>
      </div>
      ${renderBuiltinConfigForm(p)}
      ${renderAccountsBlock(p)}
    </div>
  `;
}

function renderExternalProviderCard(p) {
  const pid = escapeHtml(p.id);
  const cfg = p.config || {};
  const kindLabels = {
    mcp_http: '🌐 MCP HTTP',
    mcp_stdio: '💻 MCP Stdio',
    llm_delegate: '🧠 LLM Делегат',
  };
  const endpoint = cfg.url || cfg.command || '';
  const wlArr = Array.isArray(cfg.enabled_tools)
    ? cfg.enabled_tools
    : (typeof cfg.enabled_tools === 'string' && cfg.enabled_tools.trim()
      ? cfg.enabled_tools.split(',').map(s => s.trim()).filter(Boolean)
      : []);
  const wlSet = new Set(wlArr);
  const cachedTools = Array.isArray(p.cached_tools) ? p.cached_tools : [];
  const activeCount = wlArr.length ? cachedTools.filter(t => wlSet.has(t.name)).length : cachedTools.length;

  let toolsBlock = '';
  if (p.kind === 'mcp_http') {
    const toolsSummary = cachedTools.length
      ? `🔧 Инструментов: <b>${activeCount}</b> / ${cachedTools.length}${wlArr.length ? ' (по whitelist)' : ' (все активны)'}`
      : '⚠️ Инструменты ещё не загружены — нажмите «Загрузить инструменты»';
    const toolsCheckboxes = cachedTools.length
      ? `
        <details style="margin-top:8px;">
          <summary style="cursor:pointer; font-size:12px; color:#93c5fd;">Выбрать активные инструменты (${cachedTools.length})</summary>
          <div id="mcp-tools-list-${pid}" style="display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:6px; margin-top:8px; max-height:220px; overflow-y:auto; padding:8px; background:rgba(15,23,42,0.5); border-radius:8px;">
            ${cachedTools.map(t => {
              const tName = escapeHtml(t.name || '');
              const tDesc = escapeHtml((t.description || '').slice(0, 120));
              const checked = wlArr.length === 0 || wlSet.has(t.name);
              return `<label style="display:flex; align-items:flex-start; gap:6px; font-size:12px; color:#cbd5e1; cursor:pointer;" title="${tDesc}">
                <input type="checkbox" class="mcp-tool-chk-${pid}" value="${tName}" ${checked ? 'checked' : ''} style="margin-top:2px;">
                <span><code>${tName}</code></span>
              </label>`;
            }).join('')}
          </div>
          <div style="margin-top:8px; display:flex; gap:8px;">
            <button type="button" class="btn btn-primary btn-sm" onclick="saveExternalProviderTools('${pid}')">💾 Сохранить выбор</button>
          </div>
        </details>
      `
      : '';
    toolsBlock = `
      <div style="margin-top:10px; padding-top:10px; border-top:1px solid rgba(255,255,255,0.06);">
        <div style="display:flex; align-items:center; justify-content:space-between; gap:8px; flex-wrap:wrap;">
          <span style="font-size:12px; color:#94a3b8;">${toolsSummary}</span>
          <button type="button" class="btn btn-secondary btn-sm" onclick="syncExternalProviderTools('${pid}')">🔄 Загрузить инструменты</button>
        </div>
        ${toolsCheckboxes}
      </div>
    `;
  }

  return `
    <div class="card plugin-card ${p.enabled ? 'plugin-enabled' : ''}">
      <div class="plugin-card-head">
        <div>
          <div class="plugin-title">
            <span class="p2-kind-badge">${kindLabels[p.kind] || escapeHtml(p.kind)}</span>
            ${escapeHtml(p.title)} <code class="user-card-id">${pid}</code>
          </div>
          ${p.description ? `<div class="plugin-desc">${escapeHtml(p.description)}</div>` : ''}
          ${endpoint ? `<div style="font-size:12px; color:#94a3b8; font-family:monospace; margin-top:4px;">${escapeHtml(endpoint)}</div>` : ''}
        </div>
        <div style="display:flex; align-items:center; gap:10px; flex-shrink:0;">
          <label class="switch" style="margin:0;">
            <input type="checkbox" ${p.enabled ? 'checked' : ''} onchange="toggleProvider('${pid}', this.checked)">
            <span class="slider"></span>
          </label>
          <button type="button" class="btn btn-secondary btn-sm" onclick="deleteProvider('${pid}', '${escapeHtml(p.title).replace(/'/g, "\\'")}')" style="color:#f87171; border-color:rgba(248,113,113,0.35);" title="Удалить провайдер">🗑️</button>
        </div>
      </div>
      ${toolsBlock}
    </div>
  `;
}

async function syncExternalProviderTools(providerId) {
  showToast(`Загружаю инструменты с ${providerId}...`);
  try {
    const res = await fetch(`${API_BASE}/api/providers/${encodeURIComponent(providerId)}/sync_tools`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Загружено инструментов: ${data.count} (${data.latency_ms || 0} мс)`);
      fetchProviders();
    } else {
      showToast(`Ошибка синхронизации: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.syncExternalProviderTools = syncExternalProviderTools;

async function saveExternalProviderTools(providerId) {
  const prov = porfiriyProviders.find(p => p.id === providerId);
  if (!prov) return;
  const boxes = Array.from(document.querySelectorAll(`.mcp-tool-chk-${CSS.escape(providerId)}`));
  const selected = boxes.filter(b => b.checked).map(b => b.value);
  const allCount = boxes.length;
  const cfg = { ...(prov.config || {}), enabled_tools: selected.length === allCount ? [] : selected };
  try {
    const res = await fetch(`${API_BASE}/api/providers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: prov.id,
        kind: prov.kind,
        title: prov.title,
        description: prov.description,
        enabled: prov.enabled,
        config: cfg
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Сохранено инструментов: ${selected.length} из ${allCount}`);
      fetchProviders();
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.saveExternalProviderTools = saveExternalProviderTools;

async function toggleProvider(providerId, enabled) {
  try {
    const res = await fetch(`${API_BASE}/api/providers/${encodeURIComponent(providerId)}/toggle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !!enabled })
    });
    const data = await res.json();
    if (data.success) {
      showToast(enabled ? 'Провайдер включён' : 'Провайдер отключён');
      fetchProviders();
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
      fetchProviders();
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.toggleProvider = toggleProvider;

async function saveBuiltinConfig(providerId) {
  const prov = porfiriyProviders.find(p => p.id === providerId);
  if (!prov) return;
  let config = { ...(prov.config || {}) };
  if (providerId === 'builtin.telegram_bot') {
    config.mode = document.getElementById('cfg-tgbot-mode')?.value || 'ha_notify';
    config.ha_service = (document.getElementById('cfg-tgbot-service')?.value || '').trim();
    config.bot_token = (document.getElementById('cfg-tgbot-token')?.value || '').trim();
    const pollEl = document.getElementById('cfg-tgbot-polling');
    if (pollEl) config.polling_enabled = !!pollEl.checked;
    const voiceEl = document.getElementById('cfg-tgbot-voice-reply');
    if (voiceEl) config.reply_voice_to_voice = !!voiceEl.checked;
  } else if (providerId === 'builtin.memory') {
    config.auto_cleanup_days = parseInt(document.getElementById('cfg-mem-cleanup')?.value || '30', 10);
    config.archive_days = parseInt(document.getElementById('cfg-mem-archive')?.value || '14', 10);
    config.max_active_items = parseInt(document.getElementById('cfg-mem-limit')?.value || '500', 10);
  } else if (providerId === 'builtin.keenetic') {
    config.entry_id = (document.getElementById('cfg-keen-entry')?.value || '').trim();
    config.default_group = (document.getElementById('cfg-keen-group')?.value || '').trim();
    config.approver_user_id = (document.getElementById('cfg-keen-approver')?.value || '').trim();
    config.confirm_mode = document.getElementById('cfg-keen-confirm')?.value || 'always';
    config.geosite_url_template = (document.getElementById('cfg-keen-geosite')?.value || '').trim();
    config.geoip_url_template = (document.getElementById('cfg-keen-geoip')?.value || '').trim();
  } else if (providerId === 'builtin.kinowatch') {
    config.default_folder_id = (document.getElementById('cfg-kw-folder')?.value || '').trim();
    config.api_base = (document.getElementById('cfg-kw-api-base')?.value || 'https://api.service-kp.com').trim();
    config.client_id = (document.getElementById('cfg-kw-client-id')?.value || '').trim();
    config.client_secret = (document.getElementById('cfg-kw-client-secret')?.value || '').trim();
  } else if (providerId === 'builtin.yandex_maps') {
    config.geosuggest_api_key = (document.getElementById('cfg-ym-geosuggest')?.value || '').trim();
    config.geocoder_api_key = (document.getElementById('cfg-ym-geocoder')?.value || '').trim();
    config.default_city = (document.getElementById('cfg-ym-city')?.value || 'Москва').trim() || 'Москва';
    config.default_ll = (document.getElementById('cfg-ym-ll')?.value || '37.6173,55.7558').trim() || '37.6173,55.7558';
    config.auto_send_telegram = !!document.getElementById('cfg-ym-autotg')?.checked;
  }
  try {
    const res = await fetch(`${API_BASE}/api/providers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: prov.id,
        kind: prov.kind,
        title: prov.title,
        description: prov.description,
        enabled: prov.enabled,
        config
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast('Настройки плагина сохранены');
      fetchProviders();
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.saveBuiltinConfig = saveBuiltinConfig;

async function deleteProvider(providerId, title) {
  if (!confirm(`Удалить провайдер «${title}»?`)) return;
  try {
    const res = await fetch(`${API_BASE}/api/providers/${encodeURIComponent(providerId)}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      showToast(`Провайдер «${title}» удалён`);
      fetchProviders();
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.deleteProvider = deleteProvider;

function openProviderAccountModal(providerId, accountId) {
  const modal = document.getElementById('modal-provider-account');
  if (!modal) return;
  const prov = porfiriyProviders.find(p => p.id === providerId);
  const acc = (prov && accountId) ? (prov.accounts || []).find(a => a.id === accountId) : null;
  document.getElementById('acc-provider-id').value = providerId;
  document.getElementById('acc-edit-id').value = acc ? acc.id : '';
  document.getElementById('acc-modal-title').textContent = acc ? `✏️ Аккаунт: ${acc.label}` : `🔑 Добавить аккаунт (${prov ? prov.title : providerId})`;
  document.getElementById('acc-label').value = acc ? acc.label : '';

  const ownerSel = document.getElementById('acc-owner');
  ownerSel.innerHTML = `<option value="">🏠 Общий (Дом)</option>` + porfiriyUsers.map(u =>
    `<option value="${escapeHtml(u.id)}">👤 ${escapeHtml(u.name)}</option>`
  ).join('');
  ownerSel.value = (acc && acc.owner_user_id) ? acc.owner_user_id : '';

  const creds = (acc && acc.credentials) || {};
  const fieldsEl = document.getElementById('acc-creds-fields');
  if (providerId === 'builtin.mail_imap') {
    fieldsEl.innerHTML = `
      <div class="p2-config-grid" style="grid-template-columns: 2fr 1fr;">
        <div class="form-group" style="margin:0;">
          <label>IMAP сервер</label>
          <input type="text" id="acc-cred-host" value="${escapeHtml(creds.host || 'imap.yandex.ru')}" placeholder="imap.yandex.ru">
        </div>
        <div class="form-group" style="margin:0;">
          <label>Порт (SSL)</label>
          <input type="number" id="acc-cred-port" value="${Number(creds.port || 993)}">
        </div>
      </div>
      <div class="form-group" style="margin:0;">
        <label>Email / Логин</label>
        <input type="text" id="acc-cred-email" value="${escapeHtml(creds.email || '')}" placeholder="name@yandex.ru">
      </div>
      <div class="form-group" style="margin:0;">
        <label>Пароль приложения (App Password)</label>
        <input type="password" id="acc-cred-password" value="${escapeHtml(creds.password || '')}" placeholder="Пароль приложения IMAP">
      </div>
    `;
  } else if (providerId === 'builtin.telegram_userbot') {
    fieldsEl.innerHTML = `
      <div class="form-group" style="margin:0;">
        <label>Telegram API ID (my.telegram.org)</label>
        <input type="text" id="acc-cred-api-id" value="${escapeHtml(creds.api_id || '')}" placeholder="12345678">
      </div>
      <div class="form-group" style="margin:0;">
        <label>Telegram API Hash</label>
        <input type="password" id="acc-cred-api-hash" value="${escapeHtml(creds.api_hash || '')}" placeholder="32-символьный api_hash">
      </div>
      <div class="form-group" style="margin:0;">
        <label>Номер телефона</label>
        <input type="text" id="acc-cred-phone" value="${escapeHtml(creds.phone || '')}" placeholder="+79991234567">
      </div>
    `;
  } else {
    fieldsEl.innerHTML = `
      <div class="form-group" style="margin:0;">
        <label>API-ключ / Токен</label>
        <input type="password" id="acc-cred-token" value="${escapeHtml(creds.token || '')}" placeholder="Опционально">
      </div>
    `;
  }
  modal.style.display = 'flex';
}
window.openProviderAccountModal = openProviderAccountModal;

function closeProviderAccountModal() {
  const modal = document.getElementById('modal-provider-account');
  if (modal) modal.style.display = 'none';
}
window.closeProviderAccountModal = closeProviderAccountModal;

async function submitProviderAccountModal() {
  const providerId = document.getElementById('acc-provider-id')?.value || '';
  const accountId = (document.getElementById('acc-edit-id')?.value || '').trim();
  const label = (document.getElementById('acc-label')?.value || '').trim();
  const owner_user_id = (document.getElementById('acc-owner')?.value || '').trim() || null;
  if (!label) {
    showToast('Укажите название аккаунта', true);
    return;
  }
  let credentials = {};
  if (providerId === 'builtin.mail_imap') {
    credentials = {
      host: (document.getElementById('acc-cred-host')?.value || '').trim(),
      port: parseInt(document.getElementById('acc-cred-port')?.value || '993', 10),
      email: (document.getElementById('acc-cred-email')?.value || '').trim(),
      password: (document.getElementById('acc-cred-password')?.value || '').trim(),
    };
  } else if (providerId === 'builtin.telegram_userbot') {
    credentials = {
      api_id: (document.getElementById('acc-cred-api-id')?.value || '').trim(),
      api_hash: (document.getElementById('acc-cred-api-hash')?.value || '').trim(),
      phone: (document.getElementById('acc-cred-phone')?.value || '').trim(),
    };
  } else {
    credentials = {
      token: (document.getElementById('acc-cred-token')?.value || '').trim(),
    };
  }
  try {
    const res = await fetch(`${API_BASE}/api/providers/${encodeURIComponent(providerId)}/accounts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: accountId || undefined, label, owner_user_id, credentials })
    });
    const data = await res.json();
    if (data.success) {
      closeProviderAccountModal();
      showToast(`Аккаунт «${label}» сохранён`);
      fetchProviders();
      fetchUsers();
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.submitProviderAccountModal = submitProviderAccountModal;

async function deleteProviderAccount(accountId, label) {
  if (!confirm(`Удалить аккаунт «${label}»?`)) return;
  try {
    const res = await fetch(`${API_BASE}/api/providers/accounts/${encodeURIComponent(accountId)}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      showToast(`Аккаунт «${label}» удалён`);
      fetchProviders();
      fetchUsers();
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.deleteProviderAccount = deleteProviderAccount;

function openExternalProviderModal() {
  const modal = document.getElementById('modal-external-provider');
  if (!modal) return;
  document.getElementById('ext-prov-kind').value = 'mcp_http';
  document.getElementById('ext-prov-id').value = '';
  document.getElementById('ext-prov-title').value = '';
  document.getElementById('ext-prov-desc').value = '';
  document.getElementById('ext-prov-endpoint').value = '';
  document.getElementById('ext-prov-token').value = '';
  document.getElementById('ext-prov-whitelist').value = '';
  updateExternalProviderPlaceholders();
  modal.style.display = 'flex';
}
window.openExternalProviderModal = openExternalProviderModal;

function closeExternalProviderModal() {
  const modal = document.getElementById('modal-external-provider');
  if (modal) modal.style.display = 'none';
}
window.closeExternalProviderModal = closeExternalProviderModal;

function updateExternalProviderPlaceholders() {
  const kind = document.getElementById('ext-prov-kind')?.value || 'mcp_http';
  const lbl = document.getElementById('ext-prov-endpoint-label');
  const inp = document.getElementById('ext-prov-endpoint');
  if (!lbl || !inp) return;
  if (kind === 'mcp_stdio') {
    lbl.textContent = 'Команда запуска (Stdio)';
    inp.placeholder = 'npx -y @modelcontextprotocol/server-filesystem /share';
  } else if (kind === 'llm_delegate') {
    lbl.textContent = 'Base URL / Модель';
    inp.placeholder = 'https://api.openai.com/v1';
  } else {
    lbl.textContent = 'URL сервера (HTTP/SSE)';
    inp.placeholder = 'https://mcp.example.com/sse';
  }
}
window.updateExternalProviderPlaceholders = updateExternalProviderPlaceholders;

async function submitExternalProviderModal() {
  const kind = document.getElementById('ext-prov-kind')?.value || 'mcp_http';
  const id = (document.getElementById('ext-prov-id')?.value || '').trim();
  const title = (document.getElementById('ext-prov-title')?.value || '').trim();
  const description = (document.getElementById('ext-prov-desc')?.value || '').trim();
  const endpoint = (document.getElementById('ext-prov-endpoint')?.value || '').trim();
  const api_token = (document.getElementById('ext-prov-token')?.value || '').trim();
  const wlRaw = (document.getElementById('ext-prov-whitelist')?.value || '').trim();
  if (!title) {
    showToast('Укажите название провайдера', true);
    return;
  }
  const enabled_tools = wlRaw ? wlRaw.split(',').map(s => s.trim()).filter(Boolean) : [];
  const config = kind === 'mcp_stdio'
    ? { command: endpoint, api_token, enabled_tools }
    : { url: endpoint, api_token, enabled_tools };

  try {
    const res = await fetch(`${API_BASE}/api/providers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id || undefined, kind, title, description, enabled: true, config })
    });
    const data = await res.json();
    if (data.success) {
      closeExternalProviderModal();
      showToast(`Провайдер «${title}» добавлен`);
      fetchProviders();
      if (kind === 'mcp_http' && data.provider && data.provider.id) {
        syncExternalProviderTools(data.provider.id);
      }
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.submitExternalProviderModal = submitExternalProviderModal;

async function fetchToolsAudit() {
  const body = document.getElementById('tools-audit-body');
  if (!body) return;
  try {
    const res = await fetch(`${API_BASE}/api/tools/audit?limit=50`);
    if (!res.ok) return;
    const data = await res.json();
    const items = data.items || [];
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="6" style="text-align:center; color:#8b949e;">Вызовов инструментов плагинов пока не было.</td></tr>';
      return;
    }
    body.innerHTML = items.map(it => {
      const ok = it.status === 'ok';
      const tsShort = (it.ts || '').replace('T', ' ').slice(0, 19);
      const detail = it.error ? `<span style="color:#f87171;">${escapeHtml(it.error)}</span>` : `<code>${escapeHtml(it.args_preview || '')}</code>`;
      return `
        <tr>
          <td style="white-space:nowrap; font-family:monospace; font-size:11px;">${escapeHtml(tsShort)}</td>
          <td><code>${escapeHtml(it.tool_name)}</code></td>
          <td style="font-size:12px; color:#8b949e;">${escapeHtml(it.provider_id || '')}</td>
          <td><span class="p2-status-pill ${ok ? 'ok' : 'err'}">${escapeHtml(it.status)}</span></td>
          <td style="font-family:monospace;">${Number(it.duration_ms || 0)}</td>
          <td style="font-size:11px; max-width:260px; overflow:hidden; text-overflow:ellipsis;">${detail}</td>
        </tr>
      `;
    }).join('');
  } catch (e) {
    body.innerHTML = `<tr><td colspan="6" style="color:#f87171;">Ошибка загрузки: ${escapeHtml(e.message)}</td></tr>`;
  }
}
window.fetchToolsAudit = fetchToolsAudit;

async function testTelegramBot() {
  const target = document.getElementById('cfg-tgbot-test-target')?.value || 'denis';
  showToast('Отправляю тестовое сообщение в Telegram...');
  try {
    const res = await fetch(`${API_BASE}/api/providers/builtin.telegram_bot/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target })
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Отправлено в Telegram (${data.target || target}, mode=${data.mode})`);
      fetchToolsAudit();
    } else {
      showToast(`Ошибка отправки: ${data.error || 'сбой'}`, true);
      fetchToolsAudit();
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.testTelegramBot = testTelegramBot;

async function testKeeneticPlugin() {
  const outEl = document.getElementById('keenetic-test-result');
  if (outEl) outEl.innerHTML = '<span style="color:#94a3b8;">⏳ Подключаюсь к роутеру через keenetic_api...</span>';
  try {
    const res = await fetch(`${API_BASE}/api/providers/builtin.keenetic/test`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      const meta = data.groups_meta || {};
      const grpList = Object.keys(meta).length
        ? Object.entries(meta).map(([g, info]) => {
            const desc = info && info.description ? ` «${escapeHtml(info.description)}»` : '';
            const cnt = info && info.count !== undefined ? info.count : 0;
            return `<code>${escapeHtml(g)}</code>${desc} (${cnt})`;
          }).join(', ')
        : (Object.entries(data.groups || {}).map(([g, cnt]) => `<code>${escapeHtml(g)}</code> (${cnt})`).join(', ') || 'нет групп');
      showToast(`Роутер подключён: ${data.router || 'Keenetic'}`);
      await fetchProviders();
      const refreshedOut = document.getElementById('keenetic-test-result');
      if (refreshedOut) {
        refreshedOut.innerHTML = `<span style="color:#34d399;">✅ ${escapeHtml(data.router || 'Роутер')}</span> • Группы: ${grpList}`;
      }
    } else {
      if (outEl) outEl.innerHTML = `<span style="color:#f87171;">⚠️ ${escapeHtml(data.error || 'Ошибка')}</span>`;
      showToast(`Ошибка Keenetic: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    if (outEl) outEl.innerHTML = `<span style="color:#f87171;">⚠️ ${escapeHtml(e.message)}</span>`;
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.testKeeneticPlugin = testKeeneticPlugin;

async function startKinoWatchAuth() {
  const box = document.getElementById('kw-auth-box');
  try {
    const res = await fetch(`${API_BASE}/api/providers/builtin.kinowatch/auth/start`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      if (box) {
        box.style.display = 'block';
        box.innerHTML = `Откройте <a href="${escapeHtml(data.verification_uri || 'https://kino.watch/device')}" target="_blank" style="color:#38bdf8;">${escapeHtml(data.verification_uri || 'https://kino.watch/device')}</a> и введите код: <strong style="font-size:15px; font-family:monospace; color:#facc15;">${escapeHtml(data.user_code)}</strong>, затем нажмите «✅ Проверить вход».`;
      }
      showToast(`Код входа: ${data.user_code}`);
    } else {
      showToast(data.error || 'Ошибка получения кода', true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.startKinoWatchAuth = startKinoWatchAuth;

async function pollKinoWatchAuth() {
  try {
    const res = await fetch(`${API_BASE}/api/providers/builtin.kinowatch/auth/poll`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || 'Аккаунт KinoWatch привязан!');
      fetchProviders();
    } else {
      showToast(data.error || 'Вход не завершён', !data.pending);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.pollKinoWatchAuth = pollKinoWatchAuth;

async function syncKinoWatchFolders() {
  try {
    const res = await fetch(`${API_BASE}/api/providers/builtin.kinowatch/folders`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(`Загружено папок: ${(data.folders || []).length}`);
      fetchProviders();
    } else {
      showToast(data.error || 'Ошибка загрузки папок', true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.syncKinoWatchFolders = syncKinoWatchFolders;

async function testYandexMaps() {
  showToast('Проверяю ключи Яндекс.Карт...');
  try {
    const res = await fetch(`${API_BASE}/api/providers/builtin.yandex_maps/test`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(`✅ ${data.message || 'Ключи Яндекс.Карт работают!'}`);
    } else {
      showToast(`⚠️ ${data.message || data.error || 'Ошибка проверки ключей'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.testYandexMaps = testYandexMaps;

let _memDebounceTimer = null;
function debounceFetchMemory() {
  clearTimeout(_memDebounceTimer);
  _memDebounceTimer = setTimeout(fetchMemoryItems, 250);
}
window.debounceFetchMemory = debounceFetchMemory;

function openMemoryModal() {
  const modal = document.getElementById('modal-memory-manager');
  if (!modal) return;
  const ownerSel = document.getElementById('mem-add-owner');
  if (ownerSel) {
    ownerSel.innerHTML = `<option value="">🏠 Общий (Дом)</option>` + porfiriyUsers.map(u =>
      `<option value="${escapeHtml(u.id)}">👤 ${escapeHtml(u.name)}</option>`
    ).join('');
  }
  modal.style.display = 'flex';
  fetchMemoryItems();
}
window.openMemoryModal = openMemoryModal;

function closeMemoryModal() {
  const modal = document.getElementById('modal-memory-manager');
  if (modal) modal.style.display = 'none';
}
window.closeMemoryModal = closeMemoryModal;

async function fetchMemoryItems() {
  const listEl = document.getElementById('mem-items-list');
  const statsEl = document.getElementById('mem-stats-bar');
  if (!listEl) return;
  const q = encodeURIComponent((document.getElementById('mem-search-input')?.value || '').trim());
  const kind = encodeURIComponent(document.getElementById('mem-filter-kind')?.value || '');
  const status = encodeURIComponent(document.getElementById('mem-filter-status')?.value || 'active');
  try {
    const res = await fetch(`${API_BASE}/api/memory?q=${q}&kind=${kind}&status=${status}&limit=100`);
    if (!res.ok) return;
    const data = await res.json();
    const st = data.stats || {};
    if (statsEl) {
      statsEl.innerHTML = `
        <span>Активных: <strong>${st.active || 0}</strong></span>
        <span>Закреплённых: <strong>${st.pinned || 0}</strong></span>
        <span>Выполнено: <strong>${st.done || 0}</strong></span>
        <span>В архиве: <strong>${st.archived || 0}</strong></span>
      `;
    }
    const items = data.items || [];
    if (!items.length) {
      listEl.innerHTML = '<div style="font-size:12px; color:#8b949e; padding:12px; text-align:center;">Записей не найдено.</div>';
      return;
    }
    const kindEmoji = { fact: '📌', preference: '⭐', note: '📝', list_item: '🛒' };
    listEl.innerHTML = items.map(it => {
      const ownerBadge = it.owner_user_id
        ? `<span class="owner-badge owner-personal">👤 ${escapeHtml(it.owner_user_name)}</span>`
        : `<span class="owner-badge owner-home">🏠 Дом</span>`;
      const listTag = it.list_name ? `<span class="p2-chip">${escapeHtml(it.list_name)}</span>` : '';
      const nextStatus = it.status === 'active' ? 'done' : 'active';
      return `
        <div class="account-row">
          <div style="display:flex; flex-direction:column; gap:4px; min-width:0;">
            <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
              <span>${kindEmoji[it.kind] || '•'}</span>
              ${it.title ? `<strong>${escapeHtml(it.title)}:</strong>` : ''}
              <span style="${it.status === 'done' ? 'text-decoration:line-through; color:#8b949e;' : ''}">${escapeHtml(it.content)}</span>
              ${listTag}
              ${ownerBadge}
              ${it.is_pinned ? '<span class="p2-chip p2-chip-ok">Закреплено</span>' : ''}
            </div>
          </div>
          <div style="display:flex; gap:6px; flex-shrink:0;">
            <button type="button" class="btn btn-secondary btn-sm" onclick="toggleMemoryPin(${it.id}, ${!it.is_pinned})" title="${it.is_pinned ? 'Открепить' : 'Закрепить навечно'}">${it.is_pinned ? '📍' : '📌'}</button>
            <button type="button" class="btn btn-secondary btn-sm" onclick="toggleMemoryStatus(${it.id}, '${nextStatus}')" title="Переключить статус">${it.status === 'active' ? '✅' : '↩️'}</button>
            <button type="button" class="btn btn-secondary btn-sm" onclick="deleteMemoryItem(${it.id})" style="color:#f87171; border-color:rgba(248,113,113,0.35);" title="Удалить">🗑️</button>
          </div>
        </div>
      `;
    }).join('');
  } catch (e) {
    listEl.innerHTML = `<div style="color:#f87171;">Ошибка: ${escapeHtml(e.message)}</div>`;
  }
}
window.fetchMemoryItems = fetchMemoryItems;

async function addMemoryManual() {
  const kind = document.getElementById('mem-add-kind')?.value || 'fact';
  const content = (document.getElementById('mem-add-content')?.value || '').trim();
  const list_name = (document.getElementById('mem-add-list')?.value || '').trim();
  const owner_user_id = (document.getElementById('mem-add-owner')?.value || '').trim() || null;
  if (!content) {
    showToast('Введите текст записи', true);
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/memory`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, content, list_name, owner_user_id, is_pinned: kind === 'fact' })
    });
    const data = await res.json();
    if (data.success) {
      document.getElementById('mem-add-content').value = '';
      showToast('Запись добавлена');
      fetchMemoryItems();
      fetchUsers();
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.addMemoryManual = addMemoryManual;

async function toggleMemoryPin(id, isPinned) {
  await fetch(`${API_BASE}/api/memory/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_pinned: isPinned })
  });
  fetchMemoryItems();
}
window.toggleMemoryPin = toggleMemoryPin;

async function toggleMemoryStatus(id, status) {
  await fetch(`${API_BASE}/api/memory/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status })
  });
  fetchMemoryItems();
}
window.toggleMemoryStatus = toggleMemoryStatus;

async function deleteMemoryItem(id) {
  await fetch(`${API_BASE}/api/memory/${id}`, { method: 'DELETE' });
  fetchMemoryItems();
  fetchUsers();
}
window.deleteMemoryItem = deleteMemoryItem;

async function runMemoryCleanup(refreshModal) {
  try {
    const res = await fetch(`${API_BASE}/api/memory/cleanup`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(`Очистка завершена: ${data.summary || 'ок'}`);
      if (refreshModal) fetchMemoryItems();
      fetchUsers();
    } else {
      showToast(`Ошибка: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.runMemoryCleanup = runMemoryCleanup;

function openTgUserbotAuthModal(accountId, label) {
  const modal = document.getElementById('modal-tg-userbot-auth');
  if (!modal) return;
  document.getElementById('tg-ub-auth-acc-id').value = accountId;
  document.getElementById('tg-ub-auth-title').textContent = `🔐 Вход: ${label || accountId}`;
  document.getElementById('tg-ub-code').value = '';
  document.getElementById('tg-ub-2fa').value = '';
  document.getElementById('tg-ub-2fa-wrap').style.display = 'none';
  document.getElementById('tg-ub-auth-status').textContent = 'Нажмите «Отправить код в Telegram» и введите пришедший 5-значный код.';
  modal.style.display = 'flex';
}
window.openTgUserbotAuthModal = openTgUserbotAuthModal;

function closeTgUserbotAuthModal() {
  const modal = document.getElementById('modal-tg-userbot-auth');
  if (modal) modal.style.display = 'none';
}
window.closeTgUserbotAuthModal = closeTgUserbotAuthModal;

async function startTgUserbotAuth() {
  const aid = document.getElementById('tg-ub-auth-acc-id')?.value || '';
  const stEl = document.getElementById('tg-ub-auth-status');
  if (stEl) stEl.textContent = 'Отправляю запрос кода в Telegram...';
  try {
    const res = await fetch(`${API_BASE}/api/providers/builtin.telegram_userbot/accounts/${encodeURIComponent(aid)}/auth/start`, {
      method: 'POST',
    });
    const data = await res.json();
    if (data.success) {
      if (stEl) stEl.innerHTML = `<span style="color:#34d399;">${escapeHtml(data.message)}</span>`;
      showToast('Код отправлен в Telegram');
      fetchProviders();
    } else {
      if (stEl) stEl.innerHTML = `<span style="color:#f87171;">Ошибка: ${escapeHtml(data.error || 'сбой')}</span>`;
    }
  } catch (e) {
    if (stEl) stEl.innerHTML = `<span style="color:#f87171;">Сетевая ошибка: ${escapeHtml(e.message)}</span>`;
  }
}
window.startTgUserbotAuth = startTgUserbotAuth;

async function verifyTgUserbotAuth() {
  const aid = document.getElementById('tg-ub-auth-acc-id')?.value || '';
  const code = (document.getElementById('tg-ub-code')?.value || '').trim();
  const password = document.getElementById('tg-ub-2fa')?.value || '';
  const stEl = document.getElementById('tg-ub-auth-status');
  try {
    const res = await fetch(`${API_BASE}/api/providers/builtin.telegram_userbot/accounts/${encodeURIComponent(aid)}/auth/verify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, password })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || 'Аккаунт авторизован!');
      closeTgUserbotAuthModal();
      fetchProviders();
    } else if (data.needs_2fa) {
      document.getElementById('tg-ub-2fa-wrap').style.display = 'block';
      if (stEl) stEl.innerHTML = `<span style="color:#fbbf24;">${escapeHtml(data.error)}</span>`;
    } else {
      if (stEl) stEl.innerHTML = `<span style="color:#f87171;">Ошибка: ${escapeHtml(data.error || 'сбой')}</span>`;
      fetchProviders();
    }
  } catch (e) {
    if (stEl) stEl.innerHTML = `<span style="color:#f87171;">Сетевая ошибка: ${escapeHtml(e.message)}</span>`;
  }
}
window.verifyTgUserbotAuth = verifyTgUserbotAuth;

async function testMailImapAccount(accountId) {
  showToast('Проверяю подключение к IMAP...');
  try {
    const res = await fetch(`${API_BASE}/api/providers/builtin.mail_imap/accounts/${encodeURIComponent(accountId)}/test`, {
      method: 'POST',
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || 'Подключение IMAP успешно!');
    } else {
      showToast(`Ошибка IMAP: ${data.error || 'сбой'}`, true);
    }
    fetchProviders();
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.testMailImapAccount = testMailImapAccount;

// -------------------------------------------------------------------------
// Пул больших моделей (BigModelPool)
// -------------------------------------------------------------------------
function renderBigModelsPool() {
  const listEl = document.getElementById('big-models-list');
  if (!listEl) return;

  if (!bigModelsPoolState.length) {
    listEl.innerHTML = '<div style="font-size:12px; color:#8b949e; padding:6px 0;">Модели ещё не добавлены. Нажмите «+ Добавить модель» и сохраните настройки.</div>';
    return;
  }

  const allModels = Array.isArray(availableModels) ? availableModels : [];
  const textModels = allModels.filter(m => m && !m.is_live);
  const liveModels = allModels.filter(m => m && m.is_live);

  listEl.innerHTML = bigModelsPoolState.map((row, idx) => {
    const mid = row.model || '';
    const prio = row.priority !== undefined ? row.priority : (idx + 1);
    const enabled = row.enabled !== false;
    const h = bigModelsHealth[mid];
    let statusBadge = '<span class="p2-chip p2-chip-muted">⚪ Не проверялась</span>';
    if (h) {
      if (h.status === 'ok') {
        statusBadge = `<span class="p2-chip p2-chip-ok">🟢 Работает (${Number(h.last_latency_ms || 0)} мс)</span>`;
      } else {
        statusBadge = `<span class="p2-chip" style="color:#f87171;" title="${escapeHtml(h.last_error || '')}">🔴 Ошибка</span>`;
      }
    }

    const inList = allModels.some(m => m && m.id === mid);
    let optsHtml = '';
    if (textModels.length) {
      optsHtml += '<optgroup label="Текстовые / Большие модели">';
      textModels.forEach(m => {
        optsHtml += `<option value="${escapeHtml(m.id)}" ${m.id === mid ? 'selected' : ''}>${escapeHtml(m.name || m.id)}</option>`;
      });
      optsHtml += '</optgroup>';
    }
    if (liveModels.length) {
      optsHtml += '<optgroup label="🎙️ Live модели">';
      liveModels.forEach(m => {
        optsHtml += `<option value="${escapeHtml(m.id)}" ${m.id === mid ? 'selected' : ''}>🎙️ ${escapeHtml(m.name || m.id)}</option>`;
      });
      optsHtml += '</optgroup>';
    }
    if (!inList && mid) {
      optsHtml += `<option value="${escapeHtml(mid)}" selected>${escapeHtml(mid)}</option>`;
    }
    if (!optsHtml) {
      optsHtml = `<option value="models/gemini-2.5-pro" ${mid === 'models/gemini-2.5-pro' ? 'selected' : ''}>models/gemini-2.5-pro</option><option value="models/gemini-2.5-flash" ${mid === 'models/gemini-2.5-flash' ? 'selected' : ''}>models/gemini-2.5-flash</option>`;
    }

    return `
      <div class="account-row" style="gap:8px; flex-wrap:wrap;">
        <input type="checkbox" ${enabled ? 'checked' : ''} onchange="updateBigModelRow(${idx}, 'enabled', this.checked)" title="Вкл/Выкл в пуле">
        <select style="flex:1; min-width:210px; padding:5px 8px; font-size:12px;" onchange="updateBigModelRow(${idx}, 'model', this.value)">${optsHtml}</select>
        <div style="display:flex; align-items:center; gap:4px;">
          <span style="font-size:11px; color:#8b949e;">Приоритет:</span>
          <input type="number" min="1" max="99" value="${Number(prio)}" style="width:58px; padding:4px 6px; font-size:12px;" onchange="updateBigModelRow(${idx}, 'priority', parseInt(this.value || '1', 10))">
        </div>
        ${statusBadge}
        <button type="button" class="btn btn-secondary btn-sm" onclick="testBigModelRow(${idx})" title="Проверить модель прямо сейчас">🧪 Тест</button>
        <button type="button" class="btn btn-secondary btn-sm" onclick="removeBigModelRow(${idx})" style="color:#f87171; border-color:rgba(248,113,113,0.35);" title="Удалить">🗑️</button>
      </div>
    `;
  }).join('');
}
window.renderBigModelsPool = renderBigModelsPool;

function addBigModelRow() {
  const nonLive = (availableModels || []).filter(m => m && !m.is_live);
  const used = new Set(bigModelsPoolState.map(r => r.model));
  const preferred = nonLive.find(m => /2\.5-pro|pro/i.test(m.id) && !used.has(m.id))
    || nonLive.find(m => !used.has(m.id))
    || nonLive[0]
    || (availableModels && availableModels[0]);
  const defModel = preferred ? preferred.id : 'models/gemini-2.5-pro';
  bigModelsPoolState.push({
    model: defModel,
    priority: bigModelsPoolState.length + 1,
    enabled: true,
  });
  renderBigModelsPool();
}
window.addBigModelRow = addBigModelRow;

function updateBigModelRow(idx, field, val) {
  if (!bigModelsPoolState[idx]) return;
  bigModelsPoolState[idx][field] = val;
  if (field === 'model') renderBigModelsPool();
}
window.updateBigModelRow = updateBigModelRow;

function removeBigModelRow(idx) {
  bigModelsPoolState.splice(idx, 1);
  renderBigModelsPool();
}
window.removeBigModelRow = removeBigModelRow;

async function testBigModelRow(idx) {
  const row = bigModelsPoolState[idx];
  if (!row || !row.model) return;
  showToast(`Тестирую ${row.model}...`);
  try {
    const res = await fetch(`${API_BASE}/api/big_models/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: row.model })
    });
    const data = await res.json();
    if (data.health) bigModelsHealth = data.health;
    renderBigModelsPool();
    if (data.success) {
      showToast(`Модель ${row.model} работает (${data.latency_ms} мс)`);
    } else {
      showToast(`Ошибка ${row.model}: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.testBigModelRow = testBigModelRow;

function populateWebSearchModelDropdown(selectedModel) {
  const sel = document.getElementById('cfg-web-search-model');
  if (!sel) return;
  const cur = selectedModel !== undefined ? selectedModel : (globalOptions.web_search_model || '');
  const allModels = Array.isArray(availableModels) ? availableModels : [];
  const textModels = allModels.filter(m => m && !m.is_live);
  const liveModels = allModels.filter(m => m && m.is_live);

  let html = '<option value="">— Использовать пул больших моделей —</option>';
  if (liveModels.length) {
    html += '<optgroup label="🎙️ Live модели">';
    liveModels.forEach(m => {
      html += `<option value="${escapeHtml(m.id)}" ${m.id === cur ? 'selected' : ''}>🎙️ ${escapeHtml(m.name || m.id)}</option>`;
    });
    html += '</optgroup>';
  }
  if (textModels.length) {
    html += '<optgroup label="🧠 Текстовые / Большие модели">';
    textModels.forEach(m => {
      html += `<option value="${escapeHtml(m.id)}" ${m.id === cur ? 'selected' : ''}>${escapeHtml(m.name || m.id)}</option>`;
    });
    html += '</optgroup>';
  }
  if (cur && !allModels.some(m => m && m.id === cur)) {
    html += `<option value="${escapeHtml(cur)}" selected>${escapeHtml(cur)}</option>`;
  }
  sel.innerHTML = html;
}
window.populateWebSearchModelDropdown = populateWebSearchModelDropdown;

async function testWebSearchModel() {
  const selVal = (document.getElementById('cfg-web-search-model')?.value || '').trim();
  const fallbackPool = (bigModelsPoolState || []).find(r => r && r.enabled !== false && r.model);
  const modelId = selVal || (fallbackPool ? fallbackPool.model : '');
  const stEl = document.getElementById('web-search-test-status');
  if (!modelId) {
    if (stEl) stEl.innerHTML = '<span style="color:#fbbf24;">⚠️ Выберите модель или добавьте хотя бы одну в пул больших моделей.</span>';
    return;
  }
  if (stEl) stEl.innerHTML = `<span style="color:#94a3b8;">⏳ Проверяю поиск Google Search на ${escapeHtml(modelId)}...</span>`;
  showToast(`Тестирую поиск на ${modelId}...`);
  try {
    const res = await fetch(`${API_BASE}/api/big_models/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: modelId, enable_google_search: true })
    });
    const data = await res.json();
    if (data.health) bigModelsHealth = data.health;
    renderBigModelsPool();
    if (data.success) {
      if (stEl) stEl.innerHTML = `<span style="color:#34d399;">✅ Поиск работает (${data.latency_ms} мс):</span> <span style="color:#cbd5e1;">${escapeHtml(data.reply || '')}</span>`;
      showToast(`Поиск на ${modelId} работает (${data.latency_ms} мс)`);
    } else {
      if (stEl) stEl.innerHTML = `<span style="color:#f87171;">⚠️ ${escapeHtml(data.error || 'ошибка')}</span>`;
      showToast(`Ошибка поиска ${modelId}: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    if (stEl) stEl.innerHTML = `<span style="color:#f87171;">⚠️ ${escapeHtml(e.message)}</span>`;
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.testWebSearchModel = testWebSearchModel;

async function testSerperSearch() {
  const typedKey = (document.getElementById('cfg-serper-api-key')?.value || '').trim();
  const stEl = document.getElementById('serper-test-status');
  if (!typedKey && !globalOptions.has_serper_api_key) {
    if (stEl) stEl.innerHTML = '<span style="color:#fbbf24;">⚠️ Вставьте API-ключ Serper.dev или сохраните его сначала.</span>';
    return;
  }
  if (stEl) stEl.innerHTML = '<span style="color:#94a3b8;">⏳ Проверяю быстрый поиск Serper.dev...</span>';
  showToast('Тестирую поиск Serper.dev...');
  try {
    const res = await fetch(`${API_BASE}/api/web_search/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: 'serper', serper_api_key: typedKey })
    });
    const data = await res.json();
    if (data.success) {
      if (stEl) stEl.innerHTML = `<span style="color:#34d399;">✅ Serper.dev работает (${data.latency_ms} мс, ${data.results_count || 0} рез.):</span> <span style="color:#cbd5e1;">${escapeHtml(data.reply || '')}</span>`;
      showToast(`Serper.dev работает (${data.latency_ms} мс)`);
    } else {
      if (stEl) stEl.innerHTML = `<span style="color:#f87171;">⚠️ ${escapeHtml(data.error || 'ошибка')}</span>`;
      showToast(`Ошибка Serper.dev: ${data.error || 'сбой'}`, true);
    }
  } catch (e) {
    if (stEl) stEl.innerHTML = `<span style="color:#f87171;">⚠️ ${escapeHtml(e.message)}</span>`;
    showToast(`Сетевая ошибка: ${e.message}`, true);
  }
}
window.testSerperSearch = testSerperSearch;

// =========================================================================
// Сбор звуков срабатываний вейкворда (Яндекс Станция 2)
// =========================================================================
let _wwCapAudio = null;

async function loadWwCaptures(mac) {
  const statsEl = document.getElementById(`ww-cap-stats-${mac}`);
  const listEl = document.getElementById(`ww-cap-list-${mac}`);
  if (!listEl) return;
  try {
    const res = await fetch(`${API_BASE}/api/ww_captures?mac=${encodeURIComponent(mac)}&limit=30`);
    const data = await res.json();
    if (!data.success) throw new Error(data.error || 'load failed');
    const mb = ((data.total_bytes || 0) / (1024 * 1024)).toFixed(2);
    if (statsEl) {
      statsEl.textContent = `Собрано: ${data.total_count || 0} / ${data.max_files || 200} (${mb} МБ)`;
    }
    const clips = data.clips || [];
    if (clips.length === 0) {
      listEl.innerHTML = `<span style="color: #8b949e; font-size: 11px;">Записей пока нет. Включите галку выше, чтобы начать сбор.</span>`;
      return;
    }
    listEl.innerHTML = clips.map(c => {
      const timeStr = (c.timestamp || '').slice(11, 19) || c.timestamp || '';
      const kindBadge = c.kind === 'near_miss'
        ? `<span style="color:#fbbf24; font-weight:600;">🟡 Почти</span>`
        : c.kind === 'barge_in'
          ? `<span style="color:#f472b6; font-weight:600;">🔴 Перебил</span>`
          : `<span style="color:#34d399; font-weight:600;">⚡ Триггер</span>`;
      let outcomeHtml = '';
      if (c.outcome === 'listen_timeout') {
        outcomeHtml = `<span style="color:#fca5a5;" title="После срабатывания была тишина — вероятный ложняк">🔇 Тишина</span>`;
      } else if (c.outcome === 'speech') {
        const tr = c.transcript ? ` «${escapeHtml(c.transcript.slice(0, 28))}${c.transcript.length > 28 ? '…' : ''}»` : '';
        outcomeHtml = `<span style="color:#93c5fd;" title="${escapeHtml(c.transcript || 'Речь распознана')}">💬${tr || ' Речь'}</span>`;
      } else if (c.kind !== 'near_miss') {
        outcomeHtml = `<span style="color:#64748b;">⏳</span>`;
      }
      return `
        <div style="display:flex; align-items:center; justify-content:space-between; gap:6px; padding:3px 5px; background:rgba(255,255,255,0.03); border-radius:4px; font-size:11px;">
          <div style="display:flex; align-items:center; gap:6px; min-width:0; overflow:hidden; white-space:nowrap; text-overflow:ellipsis;">
            <span style="color:#64748b; font-family:monospace;">${escapeHtml(timeStr)}</span>
            ${kindBadge}
            <span style="font-family:monospace; color:#e2e8f0;">${Number(c.score || 0).toFixed(2)}</span>
            ${outcomeHtml}
          </div>
          <div style="display:flex; gap:4px; flex-shrink:0;">
            <button type="button" class="btn btn-secondary btn-sm" onclick="playWwCaptureClip('${escapeHtml(c.filename)}')" style="padding:1px 6px; font-size:10px;" title="Прослушать 3с клип">▶</button>
            <button type="button" class="btn btn-secondary btn-sm" onclick="deleteWwCaptureClip('${mac}', '${escapeHtml(c.filename)}')" style="padding:1px 5px; font-size:10px; color:#fca5a5;" title="Удалить запись">×</button>
          </div>
        </div>
      `;
    }).join('');
  } catch (err) {
    listEl.innerHTML = `<span style="color: #f87171; font-size: 11px;">Ошибка загрузки: ${escapeHtml(err.message)}</span>`;
  }
}
window.loadWwCaptures = loadWwCaptures;

function playWwCaptureClip(filename) {
  if (_wwCapAudio) {
    try { _wwCapAudio.pause(); } catch (_) {}
  }
  const url = `${API_BASE}/api/ww_captures/clip/${encodeURIComponent(filename)}?t=${Date.now()}`;
  _wwCapAudio = new Audio(url);
  _wwCapAudio.play().catch(err => showToast(`Ошибка воспроизведения: ${err.message}`, true));
}
window.playWwCaptureClip = playWwCaptureClip;

async function deleteWwCaptureClip(mac, filename) {
  try {
    const res = await fetch(`${API_BASE}/api/ww_captures/clip/${encodeURIComponent(filename)}`, { method: 'DELETE' });
    if (res.ok) {
      loadWwCaptures(mac);
    }
  } catch (err) {
    showToast(`Ошибка удаления: ${err.message}`, true);
  }
}
window.deleteWwCaptureClip = deleteWwCaptureClip;

async function clearWwCaptures(mac) {
  if (!confirm('Удалить все собранные записи вейкворда для этой колонки?')) return;
  try {
    const res = await fetch(`${API_BASE}/api/ww_captures?mac=${encodeURIComponent(mac)}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      showToast(`Удалено записей: ${data.removed || 0}`);
      loadWwCaptures(mac);
    }
  } catch (err) {
    showToast(`Ошибка очистки: ${err.message}`, true);
  }
}
window.clearWwCaptures = clearWwCaptures;

function downloadWwCapturesArchive(mac) {
  window.location.href = `${API_BASE}/api/ww_captures/archive?mac=${encodeURIComponent(mac)}`;
}
window.downloadWwCapturesArchive = downloadWwCapturesArchive;

// ---------------------------------------------------------------------------
// Умный ночной тихий режим
// ---------------------------------------------------------------------------
let quietModeCandidates = null;

function updateQuietStatusBadge(status) {
  const badge = document.getElementById('quiet-mode-status-badge');
  if (!badge || !status) return;
  if (!status.enabled) {
    badge.textContent = '⚪ Выключен';
    badge.style.background = 'rgba(148, 163, 184, 0.15)';
    badge.style.color = '#94a3b8';
    badge.style.borderColor = 'rgba(148, 163, 184, 0.3)';
  } else if (status.active) {
    badge.textContent = `🌙 Активен (${status.reason || 'тихий режим'})`;
    badge.style.background = 'rgba(99, 102, 241, 0.22)';
    badge.style.color = '#c4b5fd';
    badge.style.borderColor = 'rgba(99, 102, 241, 0.45)';
  } else {
    badge.textContent = `☀️ Дневной (${status.reason || 'не активен'})`;
    badge.style.background = 'rgba(245, 158, 11, 0.16)';
    badge.style.color = '#fcd34d';
    badge.style.borderColor = 'rgba(245, 158, 11, 0.35)';
  }
}

async function refreshQuietModeStatus() {
  try {
    const res = await fetch(`${API_BASE}/api/quiet_mode/status`);
    if (res.ok) {
      const data = await res.json();
      if (data.status) updateQuietStatusBadge(data.status);
    }
  } catch (_) {}
}

function renderQuietModeEntities(overrideSelected = null) {
  const box = document.getElementById('quiet-entities-container');
  if (!box) return;
  if (!quietModeCandidates) {
    box.innerHTML = '<span style="color: #8b949e;">Загрузка объектов Home Assistant...</span>';
    return;
  }

  let selectedSet;
  if (overrideSelected) {
    selectedSet = new Set(overrideSelected);
  } else {
    const domChecked = Array.from(box.querySelectorAll('input[name="quiet_entity"]'));
    if (domChecked.length > 0) {
      selectedSet = new Set(domChecked.filter(c => c.checked).map(c => c.value));
    } else {
      selectedSet = new Set(Array.isArray(globalOptions.quiet_mode_entities) ? globalOptions.quiet_mode_entities : []);
    }
  }

  const groupMeta = [
    ['lights', '💡 Освещение'],
    ['media', '📺 Телевизоры и медиа'],
    ['covers', '🪟 Шторы'],
    ['sensors', '🔘 Датчики и режимы']
  ];

  const htmlParts = [];
  for (const [key, label] of groupMeta) {
    const items = quietModeCandidates[key] || [];
    if (items.length === 0) continue;
    htmlParts.push(`<div style="margin-bottom: 10px;"><div style="font-weight: 700; color: #e2e8f0; margin-bottom: 6px;">${label} (${items.length})</div><div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 6px;">`);
    for (const it of items) {
      const chk = selectedSet.has(it.entity_id) ? 'checked' : '';
      const dot = it.awake ? '🟡' : '⚫';
      const room = it.room ? ` · ${escapeHtml(it.room)}` : '';
      htmlParts.push(
        `<label style="display:flex; align-items:center; gap:6px; padding:5px 8px; background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.07); border-radius:6px; cursor:pointer; margin:0;" title="${escapeHtml(it.entity_id)} (${escapeHtml(it.state)})">` +
        `<input type="checkbox" name="quiet_entity" value="${escapeHtml(it.entity_id)}" ${chk}>` +
        `<span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${dot} ${escapeHtml(it.name)}<span style="color:#8b949e; font-size:11px;">${room}</span></span>` +
        `</label>`
      );
    }
    htmlParts.push('</div></div>');
  }

  box.innerHTML = htmlParts.length > 0 ? htmlParts.join('') : '<span style="color: #8b949e;">Подходящие объекты Home Assistant не найдены.</span>';
}

async function loadQuietModeCandidates() {
  try {
    const res = await fetch(`${API_BASE}/api/quiet_mode/candidates`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.success && data.groups) {
      quietModeCandidates = data.groups;
      renderQuietModeEntities();
      if (data.status) updateQuietStatusBadge(data.status);
    }
  } catch (err) {
    console.debug('Error loading quiet_mode candidates:', err);
  }
}

function selectQuietGroup(groupKey) {
  if (!quietModeCandidates || !quietModeCandidates[groupKey]) return;
  const box = document.getElementById('quiet-entities-container');
  const cur = new Set(Array.from(box ? box.querySelectorAll('input[name="quiet_entity"]:checked') : []).map(c => c.value));
  for (const it of quietModeCandidates[groupKey]) {
    cur.add(it.entity_id);
  }
  renderQuietModeEntities(Array.from(cur));
}

function clearQuietEntities() {
  renderQuietModeEntities([]);
}

window.loadQuietModeCandidates = loadQuietModeCandidates;
window.selectQuietGroup = selectQuietGroup;
window.clearQuietEntities = clearQuietEntities;
window.refreshQuietModeStatus = refreshQuietModeStatus;
