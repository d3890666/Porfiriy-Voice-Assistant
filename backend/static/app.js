// Porfiriy Dashboard Frontend Logic
const API_BASE = window.location.pathname.replace(/\/+$/, '');

let devices = [];
let globalOptions = {};

// Инициализация
document.addEventListener('DOMContentLoaded', () => {
  setupTabs();
  fetchDevices();
  fetchGlobalOptions();
  setupSSE();
  setupSelectAll();
});

// Навигация по вкладкам
function setupTabs() {
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      
      btn.classList.add('active');
      const tabId = btn.getAttribute('data-tab');
      const target = document.getElementById(tabId);
      if (target) target.classList.add('active');
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
      updateHeaderStats();
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
    const volumePercent = Math.round((cfg.speaker_volume || 1.0) * 100);

    // Условие: если ESP32 — отображаем все настройки, шкалу Wi-Fi, вейкворд и Web UI. Иначе — только базовые.
    const isEsp = (dev.device_type === 'esp32');

    return `
      <div class="device-card ${stateClass}" id="card-${dev.mac}">
        <div class="card-header">
          <div class="dev-title-block">
            <span class="dev-name">${escapeHtml(dev.name)}</span>
            <span class="area-badge">${roomText}</span>
          </div>
          <span class="state-badge ${stateClass}">${stateLabel}</span>
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
        ` : `
          <div class="virtual-client-banner">
            <span>💻 Клиент (${escapeHtml(dev.device_type || 'ПК')})</span>
            <span class="virtual-tag">Виртуальное устройство</span>
          </div>
        `}

        <div class="dev-meta">
          <div>IP: ${isEsp && dev.ip && dev.ip !== '---' ? `<a href="http://${dev.ip}" target="_blank" class="dev-ip-link" title="Открыть страницу колонки">${dev.ip} ↗</a>` : (dev.ip || '---')}</div>
          <div>MAC: ${dev.mac}</div>
          <div>Аптайм: ${formatUptime(dev.uptime)}</div>
          <div>Тип: ${dev.device_type || 'ESP32'}</div>
        </div>

        <div class="quick-controls">
          <div class="slider-row">
            <span>🔊 Громкость:</span>
            <input type="range" min="0.1" max="2.0" step="0.05" value="${cfg.speaker_volume || 1.0}" 
              onchange="updateSingleDeviceConfig('${dev.mac}', 'speaker_volume', parseFloat(this.value))"
              oninput="this.nextElementSibling.innerText = Math.round(this.value * 100) + '%'">
            <span style="min-width: 40px; font-family: monospace; font-size: 12px;">${volumePercent}%</span>
          </div>
          ${isEsp ? `
            <div class="slider-row">
              <span>🎯 Чувств. вейкворда:</span>
              <input type="range" min="0.80" max="0.99" step="0.01" value="${cfg.wake_word_threshold || 0.93}" 
                onchange="updateSingleDeviceConfig('${dev.mac}', 'wake_word_threshold', parseFloat(this.value))"
                oninput="this.nextElementSibling.innerText = this.value">
              <span style="min-width: 40px; font-family: monospace; font-size: 12px;">${cfg.wake_word_threshold || 0.93}</span>
            </div>
          ` : ''}
        </div>

        ${isEsp ? `
          <div class="card-actions">
            <button class="btn btn-secondary btn-icon" onclick="triggerDeviceAction('${dev.mac}', 'beep')" title="Проиграть звуковой сигнал">
              🔔 Звук
            </button>
            <button class="btn btn-secondary btn-icon" onclick="triggerDeviceAction('${dev.mac}', 'reboot')" title="Перезагрузить плату">
              🔄 Рестарт
            </button>
            ${dev.ip && dev.ip !== '---' ? `
              <a href="http://${dev.ip}" target="_blank" class="btn btn-secondary btn-icon dev-web-btn" title="Встроенный Web UI колонки">
                🌐 Web UI
              </a>
            ` : ''}
          </div>
        ` : ''}
      </div>
    `;
  }).join('');
}

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
}

function setupSelectAll() {
  // logic handled in renderBulkTargets
}

function toggleSelectAll(checked) {
  document.querySelectorAll('.bulk-dev-checkbox').forEach(cb => {
    cb.checked = checked;
  });
}

// Обработка группового сохранения
async function handleBulkSubmit(e) {
  e.preventDefault();
  const form = e.target;

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
  if (form.apply_wake_word_threshold.checked) {
    fields.wake_word_threshold = parseFloat(form.wake_word_threshold.value);
  }
  if (form.apply_mic_gain.checked) {
    fields.mic_gain = parseInt(form.mic_gain.value, 10);
  }
  if (form.apply_enable_barge_in.checked) {
    fields.enable_barge_in = form.enable_barge_in.value === 'true';
  }
  if (form.apply_silence_timeout_ms.checked) {
    fields.silence_timeout_ms = parseInt(form.silence_timeout_ms.value, 10);
  }
  if (form.apply_led_brightness.checked) {
    fields.led_brightness = parseInt(form.led_brightness.value, 10);
  }
  if (form.apply_led_color_idle.checked) {
    fields.led_color_idle = form.led_color_idle.value;
  }
  if (form.apply_led_color_listen.checked) {
    fields.led_color_listen = form.led_color_listen.value;
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

// Заполнение формы глобальных настроек
function renderBrainInfo() {
  populateGlobalSettingsForm();
  checkPhrasesStatus();
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

  setVal('cfg-model', globalOptions.gemini_model || 'models/gemini-3.1-flash-live-preview');
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

  // 4 Модульных промпта
  setVal('cfg-prompt-persona', globalOptions.prompt_persona || '');
  setVal('cfg-prompt-users', globalOptions.prompt_users || '');
  setVal('cfg-prompt-smart-home', globalOptions.prompt_smart_home || '');
  setVal('cfg-prompt-general', globalOptions.prompt_general || '');

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

function toggleKeyVisibility() {
  const input = document.getElementById('cfg-api-key');
  const btn = document.getElementById('btn-toggle-key');
  if (!input) return;
  if (input.type === 'password') {
    input.type = 'text';
    btn.textContent = '🔒';
  } else {
    input.type = 'password';
    btn.textContent = '👁️';
  }
}

// Сохранение глобальных настроек (Hot Reload)
async function saveGlobalSettings(e) {
  if (e) e.preventDefault();
  const btn = document.getElementById('btn-save-global');
  if (btn) btn.disabled = true;

  try {
    const payload = {
      gemini_model: document.getElementById('cfg-model')?.value?.trim(),
      voice_name: document.getElementById('cfg-voice')?.value,
      temperature: parseFloat(document.getElementById('cfg-temperature')?.value || 0.7),
      thinking_timeout_s: parseInt(document.getElementById('cfg-thinking-timeout')?.value || 7),
      vad_silence_duration_ms: parseInt(document.getElementById('cfg-vad-silence')?.value || 600),
      enable_google_search: document.getElementById('cfg-google-search')?.checked || false,
      prompt_persona: document.getElementById('cfg-prompt-persona')?.value || '',
      prompt_users: document.getElementById('cfg-prompt-users')?.value || '',
      prompt_smart_home: document.getElementById('cfg-prompt-smart-home')?.value || '',
      prompt_general: document.getElementById('cfg-prompt-general')?.value || ''
    };

    const newKey = document.getElementById('cfg-api-key')?.value?.trim();
    if (newKey) {
      payload.gemini_api_key = newKey;
    }

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
      showToast(`Ошибка: ${data.error || 'Не удалось сохранить'}`, true);
    }
  } catch (err) {
    showToast('Ошибка связи с сервером при сохранении', true);
  } finally {
    if (btn) btn.disabled = false;
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

// Server-Sent Events (Live Telemetry)
function setupSSE() {
  try {
    const evtSource = new EventSource(`${API_BASE}/api/events`);
    evtSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.device) {
          // Обновляем устройство в локальном массиве
          const idx = devices.findIndex(d => d.mac === data.device.mac);
          if (idx >= 0) {
            devices[idx] = data.device;
          } else {
            devices.push(data.device);
          }
          renderDevicesGrid();
          updateHeaderStats();
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
