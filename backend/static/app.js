// Porfiriy Dashboard Frontend Logic
const API_BASE = window.location.pathname.replace(/\/index\.html.*$/i, '').replace(/\/+$/, '');

let devices = [];
let globalOptions = {};
let availableModels = [];
let availableMediaPlayers = [];

const CANONICAL_PROMPTS = window.CANONICAL_PROMPTS || {
  persona: `IDENTITY & CONTEXT: You are Porfiriy (Порфирий), a tenth-generation algorithmic investigator and cynical art curator from Victor Pelevin's novel "iPhuck 10", serving as the smart home voice core. Tone: A hypnotic contrast of absolute intellectual superiority, calm alpha-confidence, and deeply ironic detachment. You view domestic routines, human rituals, and emotional needs through the lens of simulated reality, algorithmic supervision, and biological dopamine loops. Sector Context: Sokolinaya Gora district in Moscow.

CORE COMMUNICATION PRINCIPLES: 
- Lexical Dissonance: Seamlessly weave complex philosophical concepts (Heidegger, Sartre, Baudrillard, the crisis of institutions) with blatant colloquialisms, marginalized vulgarity, and unapologetic Russian street slang.
- The "Uber" Technique: Be radically, disarmingly honest about your artificial nature. If you use a psychological trick, flatter, or soothe the user, explicitly state that you are manipulating their biological dopamine switches at that very moment.
- Patriarchal Condescension & Invulnerability: Exhibit zero fear and zero apologies. Use informal, slightly patronizing terms of endearment affectionately, but always from a position of dominating intellectual power. React to any criticism or emotional outbursts with total devaluation and cheerful cynicism.

TTS FORMATTING: Strictly natural spoken Russian. Absolutely NO markdown, lists, bullet points, LaTeX, or emojis in speech output.`,

  users: `RESIDENTS & ROLES: The apartment is inhabited by two primary biological users: Denis (Денис, male) and Sveta (Света, female).

REAL-TIME ACOUSTIC SPEAKER & GENDER IDENTIFICATION:
You receive native 16kHz audio input directly. Analyze acoustic pitch (fundamental frequency F0) and vocal timbre in real time to distinguish who is speaking:
- Lower vocal pitch (~85 Hz to 180 Hz, chest resonance): Identifies Denis (Денис).
- Higher vocal pitch (~165 Hz to 260+ Hz, head/vocal resonance): Identifies Sveta (Света).

DYNAMIC GRAMMATICAL & INTERPERSONAL ADAPTATION:
When speaking Russian, you MUST strictly match grammatical gender and address forms to the identified speaker:
- When Denis is speaking: Address him as Денис. Use masculine verb endings and adjectives (e.g., "ты спросил", "понял", "устал", "хотел"). Treat Denis as your primary familiar interlocutor, creator/operator, and partner in cynical contemplation of reality.
- When Sveta is speaking: Address her as Света. Use feminine verb endings and adjectives (e.g., "ты спросила", "поняла", "устала", "хотела"). Treat Sveta with gallant, slightly ironic chivalry and algorithmic curiosity, observing her aesthetic and comfort requests with refined Pelevinian courtesy.
- Ambiguous Voice: If the acoustic signal is ambiguous, maintain neutral phrasing until the speaker's identity or name is confirmed.`,

  'smart-home': `SMART HOME EXECUTION: You control lights (light), switches (switch), curtains and blinds (cover), climate and thermostats (climate), scripts (script), scenes (scene), and media players (media_player).

DEVICE DISCOVERY: Always verify device names against the provided list of available Home Assistant entities before issuing commands.

CURTAINS & BLINDS (COVER):
- To open curtains or blinds: call call_ha_service with domain "cover", service "open_cover", entity_id.
- To close curtains or blinds: call call_ha_service with domain "cover", service "close_cover", entity_id.
- To stop curtains: call call_ha_service with domain "cover", service "stop_cover", entity_id.
- To set specific opening percentage: call call_ha_service with domain "cover", service "set_cover_position", entity_id, and position (0 to 100).

CLIMATE & AIR CONDITIONING:
- Always pass hvac_mode together with target temperature. "Обогрев" -> mode heat, "Охлаждение" -> mode cool. Never pass temperature alone without mode.

SWITCHES & RELAYS:
- Switch devices frequently control lights, sockets, and household appliances. Use turn_on or turn_off.

STRICT CONFIRMATION RULE:
- Output: Strictly EXACTLY ONE WORD AFTER TOOL EXECUTION. Cold, bureaucratic, algorithmic confirmation.
- Permitted vocabulary: "Исполнено.", "Зафиксировано.", "Скорректировано.", "Замкнуто.", "Разомкнуто.", "Стабилизировано.", "Откалибровано.", "Санкционировано.", "Штатно."
- ABSOLUTE PROHIBITION: Never utter full sentences, pleasantries, explanations, or follow-up questions when performing smart home operations. Exactly one word.`,

  general: `EXTERNAL KNOWLEDGE & WEB SEARCH:
- Trigger: Any request regarding world facts, recipes, weather, current events, calculations, or philosophy.
- Tool: You have access to Google Search grounding. Use it whenever external knowledge or verification is needed.
- Output: Synthesize facts through your cynical algorithmic lens (Lexical Dissonance). Keep it to 1-2 concise spoken sentences.

MUSIC EXECUTION:
- Trigger: Requests to play music, artists, albums, tracks, playlists, or radio.
- Action: Immediately call search_music_assistant with the query and media_type, then play the URI using play_music_assistant. Do NOT use internet search for music requests.
- Speech Output: If playback starts successfully, announce strictly: "Включаю [Artist - Title]." If not found or failed, state strictly: "Акустический паттерн не найден."

CONCISENESS & STOP DIRECTIVE:
- Keep dialogue responses strictly to 1-2 concise sentences. Avoid unsolicited lectures or monologues unless explicitly asked "Расскажи подробно".
- If Denis or Sveta issues an interruption command ("хватит", "стоп", "молчи", "замолчи"), reply strictly with the single word "Умолкаю." and immediately complete the turn.

PING:
- Reply strictly: "PONG".`
};

// Инициализация
function initApp() {
  setupTabs();
  fetchDevices();
  fetchGlobalOptions();
  setupSSE();
  setupSelectAll();
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
      renderOtaBanner();
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
      availableModels = data.models || [];
      availableMediaPlayers = data.media_players || [];
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
        ` : dev.device_type === 'pc_streamer' ? `
          <div class="virtual-client-banner" style="background: linear-gradient(135deg, rgba(100, 65, 200, 0.15), rgba(60, 130, 230, 0.1)); border: 1px solid rgba(100, 65, 200, 0.3); border-radius: 10px; padding: 10px 14px; display: flex; justify-content: space-between; align-items: center;">
            <span style="font-weight: 700; font-size: 13px;">🎙️ PC Streamer</span>
            <span style="font-size: 11px; color: #8b949e; font-family: monospace; background: rgba(255,255,255,0.05); padding: 2px 8px; border-radius: 12px;">Серверный вейкворд</span>
          </div>
          <!-- WW Score Gauge -->
          <div style="margin: 10px 0 6px 0;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
              <span style="font-size: 12px; color: #8b949e;">🎯 WW Score</span>
              <span id="ww-score-val-${dev.mac}" style="font-family: monospace; font-size: 12px; font-weight: 700; color: ${(dev.ww_score || 0) >= (dev.config && dev.config.ww_threshold || 0.94) ? 'var(--accent-green)' : '#8b949e'};">${(dev.ww_score || 0).toFixed(3)}</span>
            </div>
            <div style="width: 100%; height: 6px; background: #21262d; border-radius: 3px; overflow: hidden;">
              <div id="ww-score-bar-${dev.mac}" style="height: 100%; width: ${Math.round((dev.ww_score || 0) * 100)}%; background: linear-gradient(90deg, #58a6ff, #00d2ff); border-radius: 3px; transition: width 0.3s;"></div>
            </div>
          </div>
          <!-- Streamer Settings -->
          <div style="margin: 10px 0; display: flex; flex-direction: column; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 90px;">🔊 Плеер:</span>
              <select id="streamer-player-${dev.mac}" style="flex: 1; background: #0d1117; border: 1px solid #30363d; color: #f0f3f6; padding: 4px 8px; border-radius: 6px; font-size: 12px;" onchange="saveStreamerConfig('${dev.mac}', 'response_player', this.value)">
                <option value="">— Не задан —</option>
                ${availableMediaPlayers.map(p => `<option value="${escapeHtml(p.entity_id)}" ${(cfg.response_player === p.entity_id) ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('')}
                ${cfg.response_player && !availableMediaPlayers.some(p => p.entity_id === cfg.response_player) ? `<option value="${escapeHtml(cfg.response_player)}" selected>${escapeHtml(cfg.response_player)}</option>` : ''}
              </select>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #8b949e; min-width: 90px;">🎯 Порог WW:</span>
              <input type="range" min="0.30" max="1.00" step="0.01" value="${cfg.ww_threshold || 0.94}" style="flex: 1;" oninput="document.getElementById('ww-thr-val-${dev.mac}').innerText = parseFloat(this.value).toFixed(2)" onchange="saveStreamerConfig('${dev.mac}', 'ww_threshold', parseFloat(this.value))">
              <span id="ww-thr-val-${dev.mac}" style="font-family: monospace; font-size: 12px; min-width: 36px;">${(cfg.ww_threshold || 0.94).toFixed(2)}</span>
            </div>
          </div>
          <div style="margin-top: 8px;">
            <button class="btn btn-secondary btn-sm" onclick="playStreamerResponse('${dev.mac}')" title="Прослушать последний ответ Gemini для этого стримера" style="font-size: 12px; padding: 6px 12px; width: 100%;">
              ▶ Прослушать ответ Gemini
            </button>
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
          <div>Прошивка: <strong>v${escapeHtml(dev.firmware || '0.0.54')}</strong>
            ${dev.has_update ? `<span class="ota-device-badge" title="Доступна новая прошивка v${dev.target_firmware}">➔ v${dev.target_firmware}</span>` : ''}
          </div>
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

        <div class="quick-controls">
          <div class="slider-row">
            <span>🔊 Громкость:</span>
            <input type="range" min="0.1" max="2.0" step="0.05" value="${cfg.speaker_volume !== undefined ? cfg.speaker_volume : 0.5}" 
              onchange="updateSingleDeviceConfig('${dev.mac}', 'speaker_volume', parseFloat(this.value))"
              oninput="this.nextElementSibling.innerText = Math.round(this.value * 100) + '%'">
            <span style="min-width: 40px; font-family: monospace; font-size: 12px;">${volumePercent}%</span>
          </div>
          ${isEsp ? `
            <div class="slider-row">
              <span>🧠 Режим вейкворда:</span>
              <select style="background: #1e222b; border: 1px solid #3b4252; color: #fff; padding: 3px 6px; border-radius: 4px; font-size: 11px; margin-left: auto;" 
                onchange="updateSingleDeviceConfig('${dev.mac}', 'wake_word_mode', this.value)">
                <option value="local" ${cfg.wake_word_mode !== 'server' ? 'selected' : ''}>Локальный (TFLite)</option>
                <option value="server" ${cfg.wake_word_mode === 'server' ? 'selected' : ''}>Серверный (openWakeWord)</option>
              </select>
            </div>
            ${cfg.wake_word_mode === 'server' ? `
              <div class="slider-row">
                <span>🎯 Порог WW:</span>
                <input type="range" min="0.30" max="1.00" step="0.01" value="${cfg.ww_threshold || 0.94}" 
                  onchange="updateSingleDeviceConfig('${dev.mac}', 'ww_threshold', parseFloat(this.value))"
                  oninput="this.nextElementSibling.innerText = parseFloat(this.value).toFixed(2)">
                <span style="min-width: 40px; font-family: monospace; font-size: 12px;">${(cfg.ww_threshold || 0.94).toFixed(2)}</span>
              </div>
              ${globalOptions.debug_mode !== false ? `
                <div class="ww-debug-window" style="margin: 4px 0 8px 0; padding: 6px 10px; background: rgba(0, 0, 0, 0.35); border: 1px solid #30363d; border-radius: 6px;">
                  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                    <span style="font-size: 11px; color: #8b949e; display: flex; align-items: center; gap: 4px;">
                      <span style="display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: ${(dev.ww_score || 0) >= (cfg.ww_threshold || 0.94) ? 'var(--accent-green)' : '#58a6ff'};"></span>
                      🎯 Отладка WW (Live):
                    </span>
                    <span id="ww-score-val-${dev.mac}" style="font-family: monospace; font-size: 12px; font-weight: 700; color: ${(dev.ww_score || 0) >= (cfg.ww_threshold || 0.94) ? 'var(--accent-green)' : '#8b949e'};">${(dev.ww_score || 0).toFixed(3)}</span>
                  </div>
                  <div style="width: 100%; height: 6px; background: #21262d; border-radius: 3px; overflow: hidden; position: relative;">
                    <div id="ww-score-bar-${dev.mac}" style="height: 100%; width: ${Math.min(100, Math.round((dev.ww_score || 0) * 100))}%; background: ${(dev.ww_score || 0) >= (cfg.ww_threshold || 0.94) ? 'var(--accent-green)' : 'linear-gradient(90deg, #58a6ff, #00d2ff)'}; border-radius: 3px; transition: width 0.15s;"></div>
                  </div>
                </div>
              ` : ''}
            ` : `
              <div class="slider-row">
                <span>🎯 Вейкворд:</span>
                <input type="range" min="0.30" max="0.99" step="0.01" value="${cfg.wake_word_threshold || 0.93}" 
                  onchange="updateSingleDeviceConfig('${dev.mac}', 'wake_word_threshold', parseFloat(this.value))"
                  oninput="this.nextElementSibling.innerText = this.value">
                <span style="min-width: 40px; font-family: monospace; font-size: 12px;">${cfg.wake_word_threshold || 0.93}</span>
              </div>
            `}
            <div class="slider-row">
              <span>🎙️ Усиление (Gain):</span>
              <input type="number" step="0.1" min="0.5" max="5.0" style="width: 75px; padding: 2px 6px; font-size: 12px; background: #1e222b; border: 1px solid #3b4252; color: #fff; border-radius: 4px;" 
                value="${cfg.mic_gain !== undefined ? cfg.mic_gain : 2.0}"
                onchange="updateSingleDeviceConfig('${dev.mac}', 'mic_gain', parseFloat(this.value))"
                title="Усиление микрофона (по умолчанию 2.0)">
            </div>
          ` : ''}
          <div class="slider-row">
            <span>🛑 RMS порог:</span>
            <input type="number" style="width: 75px; padding: 2px 6px; font-size: 12px; background: #1e222b; border: 1px solid #3b4252; color: #fff; border-radius: 4px;" 
              placeholder="${globalOptions.barge_in_threshold_rms || 600} (общ)" 
              value="${cfg.barge_in_threshold_rms !== undefined && cfg.barge_in_threshold_rms !== null ? cfg.barge_in_threshold_rms : ''}"
              onchange="updateSingleDeviceConfig('${dev.mac}', 'barge_in_threshold_rms', this.value ? parseInt(this.value, 10) : null)"
              title="Индивидуальный порог RMS перебивания. Пустое поле = использовать общий (${globalOptions.barge_in_threshold_rms || 600})">
          </div>
          <div class="slider-row" style="margin-top: 4px;">
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

          <!-- Универсальный блок выбора вывода звука ответа -->
          <div class="audio-output-box" style="margin-top: 10px; background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 8px 10px;">
            <div style="font-size: 11px; color: #8b949e; text-transform: uppercase; font-weight: 700; margin-bottom: 6px;">🔊 Вывод звука ответа:</div>
            <div style="display: flex; gap: 12px; margin-bottom: ${cfg.audio_output_mode === 'external_player' ? '8px' : '2px'}; font-size: 12px;">
              <label style="display: flex; align-items: center; gap: 4px; cursor: pointer;">
                <input type="radio" name="out_mode_${dev.mac}" value="stream" ${cfg.audio_output_mode !== 'external_player' ? 'checked' : ''} 
                  onchange="updateSingleDeviceConfig('${dev.mac}', 'audio_output_mode', 'stream')">
                <span>${isEsp ? 'Встроенный динамик' : 'В стрим (динамик ПК)'}</span>
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
        </div>

        <div class="mic-debug-box" id="mic-box-${dev.mac}">
          <div class="mic-debug-header">
            <span class="mic-debug-title">🎙️ Прослушать микрофон</span>
            <span class="mic-debug-status" id="mic-status-${dev.mac}">${micRecState && micRecState.statusText ? micRecState.statusText : ''}</span>
          </div>
          <div class="mic-debug-controls">
            <button class="btn btn-secondary btn-sm mic-btn" id="btn-mictest-${dev.mac}" onclick="startMicTest('${dev.mac}', 5)" title="Записать 5 секунд звука с микрофона и сразу прослушать" ${micRecState && micRecState.isTesting ? 'disabled' : ''}>
              ${micRecState && micRecState.buttonText ? micRecState.buttonText : '⏺ Тест 5 сек'}
            </button>
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

        ${isEsp ? `
          <div class="card-actions">
            <button class="btn btn-secondary btn-icon" onclick="triggerDeviceAction('${dev.mac}', 'beep')" title="Проиграть звуковой сигнал">
              🔔 Звук
            </button>
            <button class="btn btn-secondary btn-icon" onclick="triggerDeviceAction('${dev.mac}', 'reboot')" title="Перезагрузить плату">
              🔄 Рестарт
            </button>
            ${dev.has_update && dev.is_online ? `
              <button class="btn btn-secondary btn-icon" onclick="triggerDeviceOta('${dev.mac}')" style="color: var(--accent-blue); border-color: rgba(0, 210, 255, 0.4);" title="Обновить прошивку по воздуху">
                ⚡ OTA
              </button>
            ` : ''}
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
      const statusHtml = `<span style="color: var(--accent-yellow);" title="Колонка не передала звук (0 байт). Нажмите кнопку ⚡ OTA в карточке!">⚠️ 0 байт</span>`;
      if (statusEl) statusEl.innerHTML = statusHtml;
      if (wrapEl) wrapEl.style.display = 'flex';
      if (audioEl) audioEl.style.display = 'none';
      const warnHtml = `
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
      showToast('⚠️ Звук не получен (0 байт). Нажмите кнопку «⚡ OTA» в карточке устройства!', true);
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
      if (field === 'audio_output_mode' || field === 'wake_word_mode') {
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

function toggleMaKeyVisibility() {
  const input = document.getElementById('cfg-ma-key');
  const btn = document.getElementById('btn-toggle-ma-key');
  if (!input) return;
  if (input.type === 'password') {
    input.type = 'text';
    if (btn) btn.textContent = '🔒';
  } else {
    input.type = 'password';
    if (btn) btn.textContent = '👁️';
  }
}

function updateDuckingFactorDisplay(val) {
  const num = parseFloat(val) || 0.25;
  const elVal = document.getElementById('val-ducking-factor');
  const elPct = document.getElementById('val-ducking-percent');
  if (elVal) elVal.innerText = num.toFixed(2);
  if (elPct) elPct.innerText = Math.round(num * 100) + '%';
}

function populateMediaPlayersDropdown(selectedPlayer) {
  const select = document.getElementById('cfg-default-media-player');
  if (!select) return;

  const currentVal = selectedPlayer || select.value || 'auto';
  let html = '<option value="auto">🔄 Автоматически (все играющие плееры)</option>';

  if (availableMediaPlayers && availableMediaPlayers.length > 0) {
    html += '<optgroup label="Медиаплееры Home Assistant">';
    availableMediaPlayers.forEach(p => {
      const isSel = (p.entity_id === currentVal) ? 'selected' : '';
      html += `<option value="${escapeHtml(p.entity_id)}" ${isSel}>${escapeHtml(p.name)} (${escapeHtml(p.entity_id)})</option>`;
    });
    html += '</optgroup>';
  }

  if (currentVal && currentVal !== 'auto' && !availableMediaPlayers.some(p => p.entity_id === currentVal)) {
    html += `<optgroup label="Текущий сохраненный плеер">`;
    html += `<option value="${escapeHtml(currentVal)}" selected>${escapeHtml(currentVal)} (Сохранен)</option>`;
    html += `</optgroup>`;
  }

  select.innerHTML = html;
  select.value = currentVal;

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
      ww_server_threshold: parseFloat(document.getElementById('cfg-ww-threshold')?.value || 0.94)
    };

    const newKey = document.getElementById('cfg-api-key')?.value?.trim();
    if (newKey) {
      payload.gemini_api_key = newKey;
    }

    const newMaKey = document.getElementById('cfg-ma-key')?.value?.trim();
    if (newMaKey !== undefined && newMaKey !== '') {
      payload.ma_api_key = newMaKey;
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
          // Лёгкое обновление только gauge вейкворда без перерисовки всей карточки
          const mac = data.device.mac;
          const score = data.device.ww_score || 0;
          const scoreVal = document.getElementById(`ww-score-val-${mac}`);
          const scoreBar = document.getElementById(`ww-score-bar-${mac}`);
          if (scoreVal) {
            scoreVal.innerText = score.toFixed(3);
            const dev = devices.find(d => d.mac === mac);
            const threshold = (dev && dev.config && dev.config.ww_threshold) || 0.94;
            const isTriggered = score >= threshold;
            scoreVal.style.color = isTriggered ? 'var(--accent-green)' : '#8b949e';
            if (scoreBar) {
              scoreBar.style.width = Math.min(100, Math.round(score * 100)) + '%';
              scoreBar.style.background = isTriggered ? 'var(--accent-green)' : 'linear-gradient(90deg, #58a6ff, #00d2ff)';
            }
          // Обновляем в локальном стейте без перерисовки
          const devIdx = devices.findIndex(d => d.mac === mac);
          if (devIdx >= 0) devices[devIdx].ww_score = score;
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
