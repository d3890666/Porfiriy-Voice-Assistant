#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <ArduinoWebsockets.h>
#include <driver/i2s.h>
#include <FastLED.h>
#include <nvs_flash.h>
#include <Update.h>
#include <esp_wifi.h>

#define FIRMWARE_VERSION "0.0.103"

#include "model.h"
// TFLite
#include <TensorFlowLite_ESP32.h>
#include "tensorflow/lite/micro/all_ops_resolver.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/schema/schema_generated.h"
#include "tensorflow/lite/micro/micro_error_reporter.h"
#include "tensorflow/lite/micro/micro_resource_variable.h"
#include "tensorflow/lite/micro/micro_allocator.h"

// ESP-Micro-Speech-Features
#include "frontend.h"
#include "frontend_util.h"

// --- Объекты ---
Preferences preferences;
WebServer server(80);
DNSServer dnsServer;
using namespace websockets;
WebsocketsClient client;

// --- Настройки по умолчанию ---
String ssid = "";
String password = "";
String ws_host = "192.168.1.50";
uint16_t ws_port = 8765;
float mic_gain = 1.3f;
float speaker_volume = 1.0;
float wake_word_threshold = 0.93;
String wake_word_mode = "local";     // "local" (TFLite на ESP32) или "server" (openWakeWord на сервере)
int silence_timeout_ms = 700;        // таймаут тишины после фразы (мс)
int listen_timeout_s = 6;            // макс. время ожидания команды (сек)
int silence_threshold_energy = 180;  // порог энергии звука для детекции голоса
int led_brightness = 50;
String led_color_idle = "#000000";
String led_color_listen = "#0000ff";
String led_color_think = "#ffaa00";
String led_color_speak = "#00ff00";
int led_mode_idle = 0;   // 0=Off, 1=Solid, 2=Breathing
int led_mode_listen = 1; // 0=Off, 1=Solid, 2=Breathing
int led_mode_think = 2;  // 0=Off, 1=Solid, 2=Breathing
int led_mode_speak = 1;  // 0=Off, 1=Solid, 2=Breathing
int reconnect_interval = 5; // в секундах
bool enable_barge_in = false; // прерывание речи вейквордом (Barge-in)

// --- Скользящее окно вейкворда (Sliding Window для слитной речи) ---
#define MAX_WINDOW_SIZE 8
int wake_word_window_size = 3;  // 1..8 срезов (по умолчанию 3 = ~30мс)
int wake_word_window_mode = 1;  // 0=Average, 1=Peak Hold (для слитной речи), 2=Majority
float prob_window[MAX_WINDOW_SIZE] = {0.0f};
int prob_window_head = 0;

// Живой монитор скора вейкворда (для Web UI и умного логирования)
float recent_peak_score = 0.0f;
unsigned long last_score_log_time = 0;
unsigned long last_peak_reset_time = 0;

// --- Настройки пинов ---
#define I2S_MIC_BCLK 3
#define I2S_MIC_LRCLK 8
#define I2S_MIC_DIN 10

#define I2S_SPK_BCLK 4
#define I2S_SPK_LRCLK 5
#define I2S_SPK_DOUT 6

#define LED_ONBOARD_PIN 21
#define LED_BOARD_PIN 48
#define NUM_LEDS 1

CRGB leds[NUM_LEDS];

// --- Конечный автомат (State Machine) ---
enum DeviceState {
    STATE_IDLE,       // 0: Ожидание вейкворда (микрофон идет только в TFLite)
    STATE_LISTENING,  // 1: Запись команды (микрофон стримится в WebSocket)
    STATE_THINKING,   // 2: Ожидание ответа от сервера (микрофон заглушен)
    STATE_SPEAKING,   // 3: Воспроизведение звука динамиком (микрофон заглушен)
    STATE_OTA,        // 4: Прием и запись прошивки по воздуху (все аудио заглушено)
    STATE_MIC_TEST    // 5: Тест микрофона (звук стримится на сервер для прослушивания)
};
DeviceState current_state = STATE_IDLE;
unsigned long state_enter_time = 0;
unsigned long mic_test_end_time = 0;

void set_state(DeviceState new_state) {
    if (current_state != new_state) {
        Serial.printf("[STATE] %d -> %d\n", current_state, new_state);
        current_state = new_state;
        state_enter_time = millis();
    }
}

// Inline геттеры для совместимости
inline bool is_listening() { return current_state == STATE_LISTENING; }
inline bool is_thinking() { return current_state == STATE_THINKING; }
inline bool is_speaking() { return current_state == STATE_SPEAKING; }
inline bool is_idle() { return current_state == STATE_IDLE; }
inline bool is_ota() { return current_state == STATE_OTA; }
inline bool is_mic_testing() { return current_state == STATE_MIC_TEST; }

bool is_connected = false;
bool user_has_spoken = false;
bool captive_portal = false;
unsigned long last_reconnect_time = 0;
unsigned long listening_start_time = 0;
unsigned long last_speech_time = 0;
unsigned long last_sleep_time = 0;

// OTA переменные
size_t ota_total_size = 0;
size_t ota_written_size = 0;
unsigned long ota_last_packet_time = 0;

// --- Буферы для микрофона ---
#define SAMPLE_RATE 16000
#define BUFFER_SAMPLES 512
int32_t mic_buffer_32[BUFFER_SAMPLES];
int16_t mic_buffer_16[BUFFER_SAMPLES];

// Буфер для пакетной отправки (батчинга) в WebSocket (1024 байта = 512 сэмплов = 32 мс)
#define WS_TX_BUFFER_SIZE 1024
uint8_t ws_tx_buffer[WS_TX_BUFFER_SIZE];
size_t ws_tx_buffer_len = 0;

// Pre-roll буфер (последняя 1 секунда звука = 16000 сэмплов)
#define PRE_ROLL_SAMPLES 16000
int16_t pre_roll_buffer[PRE_ROLL_SAMPLES];
int pre_roll_head = 0;

// Классический DSP пиковый лимитер с быстрой атакой и плавным релизом (WebRTC/ITU-T style)
class AudioLimiter {
private:
    float envelope = 0.0f;
    float attack_coeff = 0.0f;
    float release_coeff = 0.0f;
    float target_threshold = 26000.0f; // Порог мягкого сжатия (-2 dBFS)
    float base_gain = 3.6f;

public:
    void init(float sample_rate, float attack_ms = 2.0f, float release_ms = 200.0f, float threshold = 26000.0f, float gain = 3.6f) {
        attack_coeff = expf(-1.0f / (sample_rate * (attack_ms / 1000.0f)));
        release_coeff = expf(-1.0f / (sample_rate * (release_ms / 1000.0f)));
        target_threshold = threshold;
        base_gain = gain;
        envelope = 0.0f;
    }

    void set_gain(float g) {
        base_gain = g;
    }

    inline int16_t process(float sample_after_dc) {
        float boosted = sample_after_dc * base_gain;
        float abs_s = fabsf(boosted);

        if (abs_s > envelope) {
            envelope = attack_coeff * envelope + (1.0f - attack_coeff) * abs_s;
        } else {
            envelope = release_coeff * envelope + (1.0f - release_coeff) * abs_s;
        }

        float gain_reduction = 1.0f;
        if (envelope > target_threshold) {
            gain_reduction = target_threshold / envelope;
        }

        float out = boosted * gain_reduction;

        if (out > 32767.0f) out = 32767.0f;
        if (out < -32768.0f) out = -32768.0f;

        return (int16_t)roundf(out);
    }
};

AudioLimiter limiter;

// --- TFLite и Препроцессор ---
#define PREPROCESSOR_FEATURE_SIZE 40
#define FEATURE_DURATION_MS 30
#define FEATURE_STEP_SIZE_MS 10 // According to porfiriy.json (version 2)

int num_slices = 0; 
int feature_buffer_index = 0;
int8_t* feature_ring_buffer = nullptr;

struct FrontendState frontend_state;
struct FrontendConfig frontend_config;

const tflite::Model* model = nullptr;
tflite::MicroInterpreter* interpreter = nullptr;
TfLiteTensor* input_tensor = nullptr;
TfLiteTensor* output_tensor = nullptr;

constexpr int kTensorArenaSize = 40 * 1024;
uint8_t tensor_arena[kTensorArenaSize];
tflite::MicroErrorReporter micro_error_reporter;
tflite::ErrorReporter* error_reporter = &micro_error_reporter;
tflite::MicroResourceVariables* resource_variables = nullptr;

// Функция преобразования HEX цвета (#RRGGBB) в CRGB
CRGB hexToCRGB(String hex) {
    if (hex.startsWith("#")) hex.remove(0, 1);
    long number = strtol(hex.c_str(), nullptr, 16);
    int r = number >> 16;
    int g = (number >> 8) & 0xFF;
    int b = number & 0xFF;
    return CRGB(r, g, b);
}

// Управление светодиодом
void update_led() {
    static unsigned long last_led_update = 0;
    if (millis() - last_led_update < 33) return; // Ограничение ~30 FPS для разгрузки CPU и I2S DMA
    last_led_update = millis();

    int active_mode = led_mode_idle;
    String active_color = led_color_idle;
    
    switch (current_state) {
        case STATE_OTA:
            {
                // Быстрое дыхание бирюзовым цветом во время OTA прошивки
                float fast_breathe = (exp(sin(millis() / 400.0 * PI)) - 0.36787944) * 108.0;
                int b = (int)((fast_breathe / 255.0) * 80);
                leds[0] = CRGB::Cyan;
                FastLED.setBrightness(b > 10 ? (b > 255 ? 255 : b) : 10);
                FastLED.show();
                return;
            }
        case STATE_MIC_TEST:
            {
                // Пульсация пурпурным (Magenta) во время теста микрофона
                float pulse = (exp(sin(millis() / 300.0 * PI)) - 0.36787944) * 108.0;
                int b = (int)((pulse / 255.0) * led_brightness);
                leds[0] = CRGB::Magenta;
                FastLED.setBrightness(b > 15 ? (b > 255 ? 255 : b) : 15);
                FastLED.show();
                return;
            }
        case STATE_SPEAKING:
            active_mode = led_mode_speak;
            active_color = led_color_speak;
            break;
        case STATE_THINKING:
            active_mode = led_mode_think;
            active_color = led_color_think;
            break;
        case STATE_LISTENING:
            active_mode = led_mode_listen;
            active_color = led_color_listen;
            break;
        case STATE_IDLE:
        default:
            active_mode = led_mode_idle;
            active_color = led_color_idle;
            break;
    }
    
    if (active_mode == 0) {
        leds[0] = CRGB::Black;
        FastLED.show();
        return;
    }
    
    CRGB target_color = hexToCRGB(active_color);
    if (active_mode == 1) { // Solid
        leds[0] = target_color;
        FastLED.setBrightness(led_brightness);
    } else if (active_mode == 2) { // Breathing
        float breathe = (exp(sin(millis()/2000.0*PI)) - 0.36787944)*108.0; 
        int b = (int)((breathe / 255.0) * led_brightness);
        if (b < 0) b = 0;
        if (b > 255) b = 255;
        leds[0] = target_color;
        FastLED.setBrightness(b);
    }
    FastLED.show();
}

// Отправка аудио батчами для снижения нагрузки на Wi-Fi
void send_mic_data_batched(const int16_t* data, int samples) {
    if (!is_connected) return;
    int bytes_to_copy = samples * 2;
    const uint8_t* ptr = (const uint8_t*)data;
    
    while (bytes_to_copy > 0) {
        int space_left = WS_TX_BUFFER_SIZE - ws_tx_buffer_len;
        int chunk = (bytes_to_copy < space_left) ? bytes_to_copy : space_left;
        
        memcpy(&ws_tx_buffer[ws_tx_buffer_len], ptr, chunk);
        ws_tx_buffer_len += chunk;
        ptr += chunk;
        bytes_to_copy -= chunk;
        
        if (ws_tx_buffer_len >= WS_TX_BUFFER_SIZE) {
            client.sendBinary((const char*)ws_tx_buffer, WS_TX_BUFFER_SIZE);
            ws_tx_buffer_len = 0;
        }
    }
}

// HTML страница настроек (с дашбордом и AJAX)
const char index_html[] PROGMEM = R"rawliteral(
<!DOCTYPE html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Настройка Porfiriy</title>
<style>
  body { font-family: sans-serif; display:flex; flex-direction: column; align-items:center; background:#222; color:#fff; margin:0; padding: 20px;}
  form, .dashboard { background:#333; padding:20px; border-radius:10px; box-shadow:0 0 15px rgba(0,0,0,0.5); width:90%; max-width:500px; margin-bottom: 20px;}
  label { display:block; margin-bottom:5px; color:#aaa; font-size:14px;}
  input, select { display:block; width:100%; margin-bottom:15px; padding:10px; box-sizing:border-box; border-radius:5px; border:none; font-size:16px;}
  input[type=submit] { background:#007bff; color:white; font-weight:bold; cursor:pointer; margin-top:10px;}
  input[type=submit]:hover { background:#0056b3; }
  h2 { text-align: center; margin-top:0; color:#4dabf7; margin-bottom: 15px;}
  .status-badge { display: inline-block; padding: 5px 10px; border-radius: 5px; font-weight: bold; background: #555; }
</style>
</head><body>

<div class="dashboard">
  <h2>Статус Колонки</h2>
  <div style="display:flex; justify-content:space-between; margin-bottom:12px; font-size:13px; color:#aaa;">
    <span>IP: <strong id="dev-ip" style="color:#4dabf7;">%LOCAL_IP%</strong></span>
    <span>Wi-Fi: <strong id="dev-rssi" style="color:#4dabf7;">--- dBm</strong></span>
  </div>
  <p>Состояние: <span id="status-text" class="status-badge">Загрузка...</span></p>
  <p style="margin-top:8px;">Связь с сервером: <span id="conn-text" class="status-badge">Загрузка...</span></p>
  
  <div style="margin-top:14px; background:#222; padding:10px; border-radius:6px; border:1px solid #444;">
    <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:5px;">
      <span>🎯 Скор вейкворда (Realtime):</span>
      <span id="ww-score-val" style="font-family:monospace; font-weight:bold; color:#4dabf7;">0.00 / %WW_THRES%</span>
    </div>
    <div style="width:100%; height:8px; background:#444; border-radius:4px; overflow:hidden;">
      <div id="ww-score-bar" style="width:0%; height:100%; background:#4dabf7; transition:width 0.15s ease;"></div>
    </div>
  </div>

  <button type="button" onclick="forceReconnect()" style="margin-top:12px; padding:6px 12px; background:#ffc107; color:#000; border:none; border-radius:5px; cursor:pointer; font-weight:bold;">Переподключить сейчас</button>
</div>

<form id="settingsForm" action="/save" method="POST">
  <h2>Настройки Wi-Fi и Сервера</h2>
  <label>Название Wi-Fi (SSID)</label>
  <input type="text" name="ssid" placeholder="MyWiFi" required value="%SSID%">
  <label>Пароль Wi-Fi</label>
  <input type="password" name="password" placeholder="Оставьте пустым если не меняли">
  <label>IP-адрес Сервера (Home Assistant)</label>
  <input type="text" name="host" required value="%HOST%">
  <label>Порт WebSocket</label>
  <input type="number" name="port" required value="%PORT%">
  <label>Автопереподключение (сек)</label>
  <input type="number" name="reconnect_interval" required value="%RECONNECT%">
  <label style="color:#ff5555; font-size:12px;">* Изменение сети/сервера требует перезагрузки платы</label>

  <h2 style="margin-top:30px;">Тонкая настройка (На лету)</h2>
  <label>Усиление микрофона (0.5 - 15.0, по умолч. 2.0)</label>
  <input type="number" step="0.1" name="mic_gain" value="%MIC_GAIN%" min="0.5" max="15.0">
  <label>Таймаут тишины после фразы (мс, 300-3000)</label>
  <input type="number" name="silence_timeout_ms" value="%SILENCE_MS%" min="300" max="3000" step="50">
  <label>Макс. ожидание команды (сек, 3-15)</label>
  <input type="number" name="listen_timeout_s" value="%LISTEN_TO%" min="3" max="15">
  <label>Порог детекции голоса (RMS, 50-1000)</label>
  <input type="number" name="silence_threshold_energy" value="%SIL_THRES%" min="50" max="1000">
  <label>Громкость динамика (0.1 - 2.0)</label>
  <input type="number" step="0.1" name="speaker_volume" value="%SPK_VOL%">
  <label>Порог вейкворда (0.0 - 1.0)</label>
  <input type="number" step="0.01" name="wake_word_threshold" value="%WW_THRES%">
  
  <h2 style="margin-top:30px;">Настройки Подсветки (На лету)</h2>
  <label>Яркость (0 - 255)</label>
  <input type="range" name="led_brightness" value="%LED_BRIGHT%" min="0" max="255" style="padding:0">

  <label style="color:#4dabf7; font-weight:bold; margin-top:10px;">1. Ожидание (Idle)</label>
  <select name="led_mode_idle">
    <option value="0" %LED_IDLE_M0%>Выключен</option>
    <option value="1" %LED_IDLE_M1%>Светится</option>
    <option value="2" %LED_IDLE_M2%>Дышит</option>
  </select>
  <input type="color" name="led_color_idle" value="%LED_CIDLE%" style="height:35px; padding:0;">

  <label style="color:#4dabf7; font-weight:bold; margin-top:10px;">2. Слушает (Listening)</label>
  <select name="led_mode_listen">
    <option value="0" %LED_LISTEN_M0%>Выключен</option>
    <option value="1" %LED_LISTEN_M1%>Светится</option>
    <option value="2" %LED_LISTEN_M2%>Дышит</option>
  </select>
  <input type="color" name="led_color_listen" value="%LED_CLISTEN%" style="height:35px; padding:0;">

  <label style="color:#4dabf7; font-weight:bold; margin-top:10px;">3. Думает / Обработка (Thinking)</label>
  <select name="led_mode_think">
    <option value="0" %LED_THINK_M0%>Выключен</option>
    <option value="1" %LED_THINK_M1%>Светится</option>
    <option value="2" %LED_THINK_M2%>Дышит</option>
  </select>
  <input type="color" name="led_color_think" value="%LED_CTHINK%" style="height:35px; padding:0;">

  <label style="color:#4dabf7; font-weight:bold; margin-top:10px;">4. Отвечает (Speaking)</label>
  <select name="led_mode_speak">
    <option value="0" %LED_SPEAK_M0%>Выключен</option>
    <option value="1" %LED_SPEAK_M1%>Светится</option>
    <option value="2" %LED_SPEAK_M2%>Дышит</option>
  </select>
  <input type="color" name="led_color_speak" value="%LED_CSPEAK%" style="height:35px; padding:0;">

  <input type="submit" value="Сохранить настройки">
</form>

<script>
setInterval(() => {
  fetch("/status").then(r => r.json()).then(data => {
    let st = document.getElementById("status-text");
    let ct = document.getElementById("conn-text");
    let devIp = document.getElementById("dev-ip");
    let devRssi = document.getElementById("dev-rssi");
    if(devIp && data.ip) devIp.innerText = data.ip;
    if(devRssi && data.rssi !== undefined) devRssi.innerText = data.rssi + " dBm";
    
    if(data.is_speaking) { st.innerText = "Отвечаю..."; st.style.background = "#28a745"; st.style.color = "#fff"; }
    else if(data.is_thinking) { st.innerText = "Думаю..."; st.style.background = "#ffc107"; st.style.color = "#000"; }
    else if(data.is_listening) { st.innerText = "Слушаю вас..."; st.style.background = "#007bff"; st.style.color = "#fff"; }
    else { st.innerText = "Ожидание слова"; st.style.background = "#6c757d"; st.style.color = "#fff"; }
    
    let srv = data.server_url || "сервер";
    if(data.is_connected) { 
      ct.innerText = "● Подключено: " + srv; 
      ct.style.background = "#28a745"; 
      ct.style.color = "#fff";
    } else { 
      ct.innerText = "○ Отключено (" + srv + ")"; 
      ct.style.background = "#dc3545"; 
      ct.style.color = "#fff";
    }

    if(data.ww_score !== undefined) {
      let pct = Math.min(100, Math.round(data.ww_score * 100));
      let valEl = document.getElementById("ww-score-val");
      let barEl = document.getElementById("ww-score-bar");
      if(valEl) valEl.innerText = Number(data.ww_score).toFixed(2) + " / " + Number(data.ww_threshold).toFixed(2);
      if(barEl) {
        barEl.style.width = pct + "%";
        barEl.style.background = (data.ww_score >= data.ww_threshold) ? "#28a745" : (data.ww_score >= 0.4 ? "#ffc107" : "#4dabf7");
      }
    }
  }).catch(() => {
    let st = document.getElementById("status-text");
    if(st) { st.innerText = "Плата занята / переподключение"; st.style.background = "#6c757d"; }
  });
}, 400);

// Перехват отправки формы для сохранения "на лету" без ребута (если wifi/сервер не менялись)
document.getElementById("settingsForm").addEventListener("submit", function(e) {
  e.preventDefault();
  let fd = new FormData(this);
  fetch("/save", {
    method: "POST",
    body: new URLSearchParams(fd)
  }).then(r => r.text()).then(t => {
    if(t.includes("перезагружается")) {
      document.body.innerHTML = "<h2 style=\"text-align:center;margin-top:20vh;\">Настройки сети изменены. Перезагрузка...</h2>";
    } else {
      alert("Настройки успешно применены на лету!");
    }
  });
});

function forceReconnect() {
  fetch('/reconnect').then(r => r.text()).then(t => {
    alert("Команда переподключения отправлена на плату!");
  });
}
</script>
</body></html>
)rawliteral";

void handleRoot() {
    String html = String(index_html);
    html.replace("%SSID%", ssid);
    html.replace("%HOST%", ws_host);
    html.replace("%PORT%", String(ws_port));
    html.replace("%RECONNECT%", String(reconnect_interval));
    html.replace("%MIC_GAIN%", String(mic_gain, 1));
    html.replace("%SILENCE_MS%", String(silence_timeout_ms));
    html.replace("%LISTEN_TO%", String(listen_timeout_s));
    html.replace("%SIL_THRES%", String(silence_threshold_energy));
    html.replace("%SPK_VOL%", String(speaker_volume, 1));
    html.replace("%WW_THRES%", String(wake_word_threshold, 2));
    
    html.replace("%LED_BRIGHT%", String(led_brightness));

    html.replace("%LED_IDLE_M0%", led_mode_idle == 0 ? "selected" : "");
    html.replace("%LED_IDLE_M1%", led_mode_idle == 1 ? "selected" : "");
    html.replace("%LED_IDLE_M2%", led_mode_idle == 2 ? "selected" : "");
    html.replace("%LED_CIDLE%", led_color_idle);

    html.replace("%LED_LISTEN_M0%", led_mode_listen == 0 ? "selected" : "");
    html.replace("%LED_LISTEN_M1%", led_mode_listen == 1 ? "selected" : "");
    html.replace("%LED_LISTEN_M2%", led_mode_listen == 2 ? "selected" : "");
    html.replace("%LED_CLISTEN%", led_color_listen);

    html.replace("%LED_THINK_M0%", led_mode_think == 0 ? "selected" : "");
    html.replace("%LED_THINK_M1%", led_mode_think == 1 ? "selected" : "");
    html.replace("%LED_THINK_M2%", led_mode_think == 2 ? "selected" : "");
    html.replace("%LED_CTHINK%", led_color_think);

    html.replace("%LED_SPEAK_M0%", led_mode_speak == 0 ? "selected" : "");
    html.replace("%LED_SPEAK_M1%", led_mode_speak == 1 ? "selected" : "");
    html.replace("%LED_SPEAK_M2%", led_mode_speak == 2 ? "selected" : "");
    html.replace("%LED_CSPEAK%", led_color_speak);
    
    html.replace("%LOCAL_IP%", WiFi.localIP().toString());
    
    server.send(200, "text/html", html);
}

void handleStatus() {
    bool connected = is_connected && client.available();
    String json = "{";
    json += "\"is_connected\":" + String(connected ? "true" : "false") + ",";
    json += "\"is_listening\":" + String(is_listening() ? "true" : "false") + ",";
    json += "\"is_thinking\":" + String(is_thinking() ? "true" : "false") + ",";
    json += "\"is_speaking\":" + String(is_speaking() ? "true" : "false") + ",";
    json += "\"server_url\":\"ws://" + ws_host + ":" + String(ws_port) + "\",";
    json += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
    json += "\"rssi\":" + String(WiFi.RSSI()) + ",";
    json += "\"uptime\":" + String(millis() / 1000) + ",";
    json += "\"ww_score\":" + String(recent_peak_score, 2) + ",";
    json += "\"ww_threshold\":" + String(wake_word_threshold, 2) + ",";
    json += "\"ww_window\":" + String(wake_word_window_size) + ",";
    json += "\"ww_mode\":" + String(wake_word_window_mode);
    json += "}";
    server.send(200, "application/json", json);
}

void handleReconnect() {
    server.send(200, "text/plain", "ok");
    if (!is_connected) {
        Serial.println("Manual reconnect triggered.");
        last_reconnect_time = 0; // Сброс таймера для немедленного переподключения
    }
}

void handleSave() {
    bool network_changed = false;
    
    preferences.begin("porfiriy", false);
    
    // Проверка сети
    if (server.hasArg("ssid") && server.arg("ssid") != ssid) { network_changed = true; ssid = server.arg("ssid"); preferences.putString("ssid", ssid); }
    if (server.hasArg("password") && server.arg("password") != "") { network_changed = true; password = server.arg("password"); preferences.putString("password", password); }
    if (server.hasArg("host") && server.arg("host") != ws_host) { network_changed = true; ws_host = server.arg("host"); preferences.putString("host", ws_host); }
    if (server.hasArg("port") && server.arg("port").toInt() != ws_port) { network_changed = true; ws_port = server.arg("port").toInt(); preferences.putUInt("port", ws_port); }
    
    // Тонкие настройки (На лету)
    if (server.hasArg("mic_gain")) { mic_gain = server.arg("mic_gain").toFloat(); preferences.putFloat("mic_gain_f", mic_gain); limiter.set_gain(mic_gain * 1.8f); }
    if (server.hasArg("silence_timeout_ms")) { silence_timeout_ms = server.arg("silence_timeout_ms").toInt(); preferences.putInt("sil_ms", silence_timeout_ms); }
    if (server.hasArg("listen_timeout_s")) { listen_timeout_s = server.arg("listen_timeout_s").toInt(); preferences.putInt("listen_to", listen_timeout_s); }
    if (server.hasArg("silence_threshold_energy")) { silence_threshold_energy = server.arg("silence_threshold_energy").toInt(); preferences.putInt("sil_thres", silence_threshold_energy); }
    if (server.hasArg("speaker_volume")) { speaker_volume = server.arg("speaker_volume").toFloat(); preferences.putFloat("spk_vol", speaker_volume); }
    if (server.hasArg("wake_word_threshold")) { wake_word_threshold = server.arg("wake_word_threshold").toFloat(); preferences.putFloat("ww_thres", wake_word_threshold); }
    if (server.hasArg("wake_word_mode")) { wake_word_mode = server.arg("wake_word_mode"); preferences.putString("ww_mode_str", wake_word_mode); }
    if (server.hasArg("reconnect_interval")) { reconnect_interval = server.arg("reconnect_interval").toInt(); preferences.putInt("reconn_int", reconnect_interval); }
    // Локальный Barge-in на ESP32 принудительно отключен (прерывание обрабатывается на сервере)
    enable_barge_in = false;
    
    // Настройки LED
    if (server.hasArg("led_brightness")) { led_brightness = server.arg("led_brightness").toInt(); preferences.putInt("led_bright", led_brightness); }
    if (server.hasArg("led_mode_idle")) { led_mode_idle = server.arg("led_mode_idle").toInt(); preferences.putInt("led_m_idle", led_mode_idle); }
    if (server.hasArg("led_color_idle")) { led_color_idle = server.arg("led_color_idle"); preferences.putString("led_cidle", led_color_idle); }
    if (server.hasArg("led_mode_listen")) { led_mode_listen = server.arg("led_mode_listen").toInt(); preferences.putInt("led_m_listen", led_mode_listen); }
    if (server.hasArg("led_color_listen")) { led_color_listen = server.arg("led_color_listen"); preferences.putString("led_clisten", led_color_listen); }
    if (server.hasArg("led_mode_think")) { led_mode_think = server.arg("led_mode_think").toInt(); preferences.putInt("led_m_think", led_mode_think); }
    if (server.hasArg("led_color_think")) { led_color_think = server.arg("led_color_think"); preferences.putString("led_cthink", led_color_think); }
    if (server.hasArg("led_mode_speak")) { led_mode_speak = server.arg("led_mode_speak").toInt(); preferences.putInt("led_m_speak", led_mode_speak); }
    if (server.hasArg("led_color_speak")) { led_color_speak = server.arg("led_color_speak"); preferences.putString("led_cspeak", led_color_speak); }
    
    preferences.end();
    
    if (network_changed) {
        server.send(200, "text/plain", "перезагружается");
        delay(1000);
        ESP.restart();
    } else {
        server.send(200, "text/plain", "ok");
    }
}

void load_preferences() {
    preferences.begin("porfiriy", true);
    ssid = preferences.getString("ssid", "");
    password = preferences.getString("password", "");
    ws_host = preferences.getString("host", "192.168.1.50");
    ws_port = preferences.getUInt("port", 8765);
    mic_gain = preferences.getFloat("mic_gain_f", 1.3f);
    silence_timeout_ms = preferences.getInt("sil_ms", 700);
    listen_timeout_s = preferences.getInt("listen_to", 6);
    silence_threshold_energy = preferences.getInt("sil_thres", 180);
    speaker_volume = preferences.getFloat("spk_vol", 1.0);
    wake_word_threshold = preferences.getFloat("ww_thres", 0.93);
    wake_word_mode = preferences.getString("ww_mode_str", "local");
    reconnect_interval = preferences.getInt("reconn_int", 5);
    // Локальный Barge-in на ESP32 принудительно отключен (прерывание обрабатывается на сервере)
    enable_barge_in = false;
    wake_word_window_size = preferences.getInt("ww_win", 3);
    if (wake_word_window_size < 1) wake_word_window_size = 1;
    if (wake_word_window_size > MAX_WINDOW_SIZE) wake_word_window_size = MAX_WINDOW_SIZE;
    wake_word_window_mode = preferences.getInt("ww_mode", 1);
    
    led_brightness = preferences.getInt("led_bright", 50);
    led_mode_idle = preferences.getInt("led_m_idle", 0);
    led_color_idle = preferences.getString("led_cidle", "#000000");
    led_mode_listen = preferences.getInt("led_m_listen", 1);
    led_color_listen = preferences.getString("led_clisten", "#0000ff");
    led_mode_think = preferences.getInt("led_m_think", 2);
    led_color_think = preferences.getString("led_cthink", "#ffaa00");
    led_mode_speak = preferences.getInt("led_m_speak", 1);
    led_color_speak = preferences.getString("led_cspeak", "#00ff00");
    preferences.end();
}

void setup_wifi() {
    if (ssid != "") {
        Serial.print("Connecting to WiFi: ");
        Serial.println(ssid);
        WiFi.begin(ssid.c_str(), password.c_str());
        WiFi.setTxPower(WIFI_POWER_11dBm); // Снижает импульсные броски тока с 450мА до ~180мА
        esp_wifi_set_ps(WIFI_PS_MIN_MODEM); // Энергосбережение модема между пакетами
        
        int attempts = 0;
        while (WiFi.status() != WL_CONNECTED && attempts < 20) {
            delay(500);
            Serial.print(".");
            attempts++;
        }
    }

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("\nWiFi connection failed. Starting Captive Portal...");
        captive_portal = true;
        WiFi.mode(WIFI_AP);
        WiFi.softAP("Porfiriy_Setup");
        dnsServer.start(53, "*", WiFi.softAPIP());
    } else {
        Serial.println("\nConnected to WiFi!");
        Serial.print("IP Address: ");
        Serial.println(WiFi.localIP());
    }
    
    // Запускаем Web-сервер в любом случае (и в AP, и в STA режиме)
    server.on("/", handleRoot);
    server.on("/save", HTTP_POST, handleSave);
    server.on("/status", handleStatus);
    server.on("/reconnect", handleReconnect);
    server.onNotFound([](){
        if(captive_portal) {
            server.sendHeader("Location", "http://192.168.4.1/", true);
            server.send(302, "text/plain", "");
        } else {
            server.send(404, "text/plain", "Not found");
        }
    });
    server.begin();
    if(captive_portal) Serial.println("AP started. Connect to 'Porfiriy_Setup' and open http://192.168.4.1");
}

void setup_i2s() {
    i2s_config_t i2s_mic_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate = SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
        .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 16,
        .dma_buf_len = BUFFER_SAMPLES,
        .use_apll = false,
        .tx_desc_auto_clear = false,
        .fixed_mclk = 0
    };
    i2s_pin_config_t i2s_mic_pins = {
        .bck_io_num = I2S_MIC_BCLK,
        .ws_io_num = I2S_MIC_LRCLK,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num = I2S_MIC_DIN
    };
    i2s_driver_install(I2S_NUM_0, &i2s_mic_config, 0, NULL);
    i2s_set_pin(I2S_NUM_0, &i2s_mic_pins);
    pinMode(I2S_MIC_DIN, INPUT_PULLDOWN); // Предотвращает плавающий Z-state шины данных INMP441

    i2s_config_t i2s_spk_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
        .sample_rate = 24000,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 8,
        .dma_buf_len = 1024,
        .use_apll = false,
        .tx_desc_auto_clear = true,
        .fixed_mclk = 0
    };
    i2s_pin_config_t i2s_spk_pins = {
        .bck_io_num = I2S_SPK_BCLK,
        .ws_io_num = I2S_SPK_LRCLK,
        .data_out_num = I2S_SPK_DOUT,
        .data_in_num = I2S_PIN_NO_CHANGE
    };
    i2s_driver_install(I2S_NUM_1, &i2s_spk_config, 0, NULL);
    i2s_set_pin(I2S_NUM_1, &i2s_spk_pins);
}

void setup_frontend() {
    frontend_config.window.size_ms = FEATURE_DURATION_MS;
    frontend_config.window.step_size_ms = FEATURE_STEP_SIZE_MS;
    frontend_config.filterbank.num_channels = PREPROCESSOR_FEATURE_SIZE;
    frontend_config.filterbank.lower_band_limit = 125.0;
    frontend_config.filterbank.upper_band_limit = 7500.0;
    frontend_config.noise_reduction.smoothing_bits = 10;
    frontend_config.noise_reduction.even_smoothing = 0.025;
    frontend_config.noise_reduction.odd_smoothing = 0.06;
    frontend_config.noise_reduction.min_signal_remaining = 0.05;
    frontend_config.pcan_gain_control.enable_pcan = 1;
    frontend_config.pcan_gain_control.strength = 0.95;
    frontend_config.pcan_gain_control.offset = 80.0;
    frontend_config.pcan_gain_control.gain_bits = 21;
    frontend_config.log_scale.enable_log = 1;
    frontend_config.log_scale.scale_shift = 6;

    if (!FrontendPopulateState(&frontend_config, &frontend_state, SAMPLE_RATE)) {
        Serial.println("FrontendPopulateState failed!");
    }
}

void setup_tflite() {
    model = tflite::GetModel(porfiriy_tflite);
    if (model->version() != TFLITE_SCHEMA_VERSION) {
        Serial.println("Model schema version mismatch!");
        return;
    }

    static tflite::AllOpsResolver resolver;

    static tflite::MicroAllocator* allocator = tflite::MicroAllocator::Create(tensor_arena, kTensorArenaSize, error_reporter);
    resource_variables = tflite::MicroResourceVariables::Create(allocator, 10);
    
    static tflite::MicroInterpreter static_interpreter(model, resolver, allocator, error_reporter, resource_variables);
    interpreter = &static_interpreter;

    if (interpreter->AllocateTensors() != kTfLiteOk) {
        Serial.println("AllocateTensors failed.");
        return;
    }

    input_tensor = interpreter->input(0);
    output_tensor = interpreter->output(0);
    
    num_slices = input_tensor->dims->data[1];
    feature_ring_buffer = (int8_t*)malloc(num_slices * PREPROCESSOR_FEATURE_SIZE);
    memset(feature_ring_buffer, 0, num_slices * PREPROCESSOR_FEATURE_SIZE);

    Serial.println("TFLite initialized successfully.");
}

void onMessageCallback(WebsocketsMessage message) {
    if (message.isBinary()) {
        if (current_state == STATE_OTA) {
            size_t len = message.length();
            if (len > 0) {
                size_t written = Update.write((uint8_t*)message.c_str(), len);
                if (written != len) {
                    Serial.printf("[OTA] Write mismatch: expected %u, wrote %u\n", (unsigned int)len, (unsigned int)written);
                }
                ota_written_size += written;
                ota_last_packet_time = millis();
            }
            return;
        }
        if (current_state == STATE_LISTENING || current_state == STATE_IDLE) {
            return;
        }
        set_state(STATE_SPEAKING);
        
        const int16_t* pcm = (const int16_t*)message.c_str();
        int num_samples = message.length() / 2;
        int chunk_size = 512;
        
        for (int i = 0; i < num_samples; i += chunk_size) {
            int current_chunk = (num_samples - i < chunk_size) ? (num_samples - i) : chunk_size;
            size_t bytes_written;
            i2s_write(I2S_NUM_1, (const char*)&pcm[i], current_chunk * 2, &bytes_written, portMAX_DELAY);
        }
    } else if (message.isText()) {
        Serial.println("Server text: " + message.data());
        
        if (message.data().indexOf("\"type\":\"ota_start\"") >= 0 || message.data().indexOf("\"type\": \"ota_start\"") >= 0) {
            Serial.println("[OTA] Server commanded OTA_START.");
            int size_idx = message.data().indexOf("\"size\":");
            size_t total_size = 0;
            if (size_idx >= 0) {
                total_size = (size_t)message.data().substring(size_idx + 7).toInt();
            }
            if (total_size == 0) {
                total_size = UPDATE_SIZE_UNKNOWN;
            }
            
            // Глушим I2S DMA буферы перед стартом OTA
            i2s_zero_dma_buffer(I2S_NUM_1);
            i2s_zero_dma_buffer(I2S_NUM_0);
            
            if (Update.begin(total_size, U_FLASH)) {
                set_state(STATE_OTA);
                ota_total_size = total_size;
                ota_written_size = 0;
                ota_last_packet_time = millis();
                Serial.printf("[OTA] Update.begin success. Target size: %u bytes\n", (unsigned int)total_size);
                client.send("{\"type\":\"ota_ready\"}");
            } else {
                Serial.printf("[OTA] Update.begin failed! Error: %d\n", Update.getError());
                client.send("{\"type\":\"ota_error\",\"error\":\"Update.begin failed\"}");
            }
        }
        else if (message.data().indexOf("\"type\":\"ota_end\"") >= 0 || message.data().indexOf("\"type\": \"ota_end\"") >= 0) {
            Serial.println("[OTA] Server commanded OTA_END.");
            if (current_state == STATE_OTA) {
                if (Update.end(true)) {
                    Serial.println("[OTA] Firmware successfully written and verified! Sending success and rebooting...");
                    client.send("{\"type\":\"ota_success\"}");
                    delay(800);
                    ESP.restart();
                } else {
                    Serial.printf("[OTA] Update.end failed! Error code: %d\n", Update.getError());
                    client.send("{\"type\":\"ota_error\",\"error\":\"Update.end verification failed\"}");
                    set_state(STATE_IDLE);
                }
            }
        }
        else if (message.data().indexOf("\"type\":\"ota_abort\"") >= 0 || message.data().indexOf("\"type\": \"ota_abort\"") >= 0) {
            Serial.println("[OTA] Server commanded OTA_ABORT.");
            if (current_state == STATE_OTA) {
                Update.abort();
                set_state(STATE_IDLE);
            }
        }
        else if (message.data().indexOf("\"type\":\"sleep\"") >= 0 || message.data().indexOf("\"type\": \"sleep\"") >= 0) {
            Serial.println("Server commanded SLEEP.");
            set_state(STATE_IDLE);
            last_sleep_time = millis();
        }
        else if (message.data().indexOf("\"type\":\"thinking\"") >= 0 || message.data().indexOf("\"type\": \"thinking\"") >= 0) {
            Serial.println("Server commanded THINKING.");
            set_state(STATE_THINKING);
        }
        else if (message.data().indexOf("\"type\":\"listen\"") >= 0 || message.data().indexOf("\"type\": \"listen\"") >= 0) {
            Serial.println("Server commanded LISTEN (Server Wake Word triggered).");
            set_state(STATE_LISTENING);
            user_has_spoken = false;
            last_speech_time = millis();
            ws_tx_buffer_len = 0;
        }
        else if (message.data().indexOf("\"type\":\"speaking\"") >= 0 || message.data().indexOf("\"type\": \"speaking\"") >= 0) {
            Serial.println("Server commanded SPEAKING.");
            set_state(STATE_SPEAKING);
        }
        else if (message.data().indexOf("\"type\":\"done_speaking\"") >= 0 || message.data().indexOf("\"type\": \"done_speaking\"") >= 0) {
            set_state(STATE_IDLE);
            last_sleep_time = millis();
        }
        else if (message.data().indexOf("\"type\":\"beep\"") >= 0 || message.data().indexOf("\"type\": \"beep\"") >= 0) {
            Serial.println("Server commanded BEEP.");
            void play_beep();
            play_beep();
        }
        else if (message.data().indexOf("\"type\":\"reboot\"") >= 0 || message.data().indexOf("\"type\": \"reboot\"") >= 0) {
            Serial.println("Server commanded REBOOT.");
            delay(500);
            ESP.restart();
        }
        else if (message.data().indexOf("\"type\":\"start_mic_test\"") >= 0 || message.data().indexOf("\"type\": \"start_mic_test\"") >= 0) {
            Serial.println("[MIC-TEST] Server requested microphone test.");
            int dur_idx = message.data().indexOf("\"duration_ms\":");
            unsigned long duration = 5000;
            if (dur_idx >= 0) {
                duration = (unsigned long)message.data().substring(dur_idx + 14).toInt();
                if (duration < 1000) duration = 1000;
                if (duration > 15000) duration = 15000;
            }
            mic_test_end_time = millis() + duration;
            set_state(STATE_MIC_TEST);
            client.send("{\"type\":\"mic_test_started\"}");
        }
        else if (message.data().indexOf("\"type\":\"set_config\"") >= 0 || message.data().indexOf("\"type\": \"set_config\"") >= 0) {
            Serial.println("Server commanded SET_CONFIG.");
            void handle_remote_config(String json);
            handle_remote_config(message.data());
        }
    }
}

void play_beep() {
    const int sample_rate = 16000;
    int16_t beep_buf[256];
    
    // Тон 1: 700 Гц (80 мс)
    for (int i = 0; i < 1280; i += 256) {
        for (int j = 0; j < 256; j++) {
            float s = sin(2.0 * PI * 700.0 * (i + j) / sample_rate) * 0.4 * speaker_volume;
            beep_buf[j] = (int16_t)(s * 32767);
        }
        size_t bw;
        i2s_write(I2S_NUM_1, beep_buf, 256 * 2, &bw, portMAX_DELAY);
    }
    // Тон 2: 1000 Гц (100 мс)
    for (int i = 0; i < 1600; i += 256) {
        for (int j = 0; j < 256; j++) {
            float s = sin(2.0 * PI * 1000.0 * (i + j) / sample_rate) * 0.4 * speaker_volume;
            beep_buf[j] = (int16_t)(s * 32767);
        }
        size_t bw;
        i2s_write(I2S_NUM_1, beep_buf, 256 * 2, &bw, portMAX_DELAY);
    }
}

void handle_remote_config(String json) {
    preferences.begin("porfiriy", false);
    
    auto parse_float = [&](const String& key, float& target, const char* pref_key) {
        int idx = json.indexOf("\"" + key + "\"");
        if (idx >= 0) {
            int colon = json.indexOf(":", idx);
            if (colon > 0) {
                float v = json.substring(colon + 1).toFloat();
                target = v;
                preferences.putFloat(pref_key, v);
                Serial.printf("[CONFIG] Updated %s = %.2f\n", key.c_str(), v);
            }
        }
    };

    auto parse_int = [&](const String& key, int& target, const char* pref_key) {
        int idx = json.indexOf("\"" + key + "\"");
        if (idx >= 0) {
            int colon = json.indexOf(":", idx);
            if (colon > 0) {
                int v = json.substring(colon + 1).toInt();
                target = v;
                preferences.putInt(pref_key, v);
                Serial.printf("[CONFIG] Updated %s = %d\n", key.c_str(), v);
            }
        }
    };

    auto parse_bool = [&](const String& key, bool& target, const char* pref_key) {
        int idx = json.indexOf("\"" + key + "\"");
        if (idx >= 0) {
            int colon = json.indexOf(":", idx);
            if (colon > 0) {
                String sub = json.substring(colon + 1, colon + 8);
                bool v = (sub.indexOf("true") >= 0);
                target = v;
                preferences.putBool(pref_key, v);
                Serial.printf("[CONFIG] Updated %s = %s\n", key.c_str(), v ? "true" : "false");
            }
        }
    };

    auto parse_string = [&](const String& key, String& target, const char* pref_key) {
        int idx = json.indexOf("\"" + key + "\"");
        if (idx >= 0) {
            int q1 = json.indexOf("\"", idx + key.length() + 2);
            if (q1 > 0) {
                int q2 = json.indexOf("\"", q1 + 1);
                if (q2 > 0) {
                    String v = json.substring(q1 + 1, q2);
                    target = v;
                    preferences.putString(pref_key, v);
                    Serial.printf("[CONFIG] Updated %s = %s\n", key.c_str(), v.c_str());
                }
            }
        }
    };

    parse_float("mic_gain", mic_gain, "mic_gain_f");
    limiter.set_gain(mic_gain * 1.8f);
    parse_float("speaker_volume", speaker_volume, "spk_vol");
    parse_float("wake_word_threshold", wake_word_threshold, "ww_thres");
    parse_string("wake_word_mode", wake_word_mode, "ww_mode_str");
    parse_int("wake_word_window_size", wake_word_window_size, "ww_win");
    if (wake_word_window_size < 1) wake_word_window_size = 1;
    if (wake_word_window_size > MAX_WINDOW_SIZE) wake_word_window_size = MAX_WINDOW_SIZE;
    parse_int("wake_word_window_mode", wake_word_window_mode, "ww_mode");
    parse_int("silence_timeout_ms", silence_timeout_ms, "sil_ms");
    parse_int("listen_timeout_s", listen_timeout_s, "listen_to");
    parse_int("silence_threshold_energy", silence_threshold_energy, "sil_thres");
    // Локальный Barge-in на ESP32 принудительно отключен (прерывание обрабатывается на сервере)
    enable_barge_in = false;
    parse_int("led_brightness", led_brightness, "led_bright");
    parse_int("led_mode_idle", led_mode_idle, "led_m_idle");
    parse_string("led_color_idle", led_color_idle, "led_cidle");
    parse_int("led_mode_listen", led_mode_listen, "led_m_listen");
    parse_string("led_color_listen", led_color_listen, "led_clisten");
    parse_int("led_mode_think", led_mode_think, "led_m_think");
    parse_string("led_color_think", led_color_think, "led_cthink");
    parse_int("led_mode_speak", led_mode_speak, "led_m_speak");
    parse_string("led_color_speak", led_color_speak, "led_cspeak");
    
    preferences.end();
}

void send_registration() {
    String mac = WiFi.macAddress();
    String ip = WiFi.localIP().toString();
    int rssi = WiFi.RSSI();
    
    String json = "{";
    json += "\"type\":\"register\",";
    json += "\"mac\":\"" + mac + "\",";
    json += "\"ip\":\"" + ip + "\",";
    json += "\"rssi\":" + String(rssi) + ",";
    json += "\"uptime\":" + String(millis() / 1000) + ",";
    json += "\"device_type\":\"esp32\",";
    json += "\"firmware\":\"" FIRMWARE_VERSION "\",";
    json += "\"config\":{";
    json += "\"mic_gain\":" + String(mic_gain, 1) + ",";
    json += "\"speaker_volume\":" + String(speaker_volume, 2) + ",";
    json += "\"wake_word_threshold\":" + String(wake_word_threshold, 2) + ",";
    json += "\"wake_word_window_size\":" + String(wake_word_window_size) + ",";
    json += "\"wake_word_window_mode\":" + String(wake_word_window_mode) + ",";
    json += "\"silence_timeout_ms\":" + String(silence_timeout_ms) + ",";
    json += "\"listen_timeout_s\":" + String(listen_timeout_s) + ",";
    json += "\"silence_threshold_energy\":" + String(silence_threshold_energy) + ",";
    json += "\"led_brightness\":" + String(led_brightness) + ",";
    json += "\"led_mode_idle\":" + String(led_mode_idle) + ",";
    json += "\"led_color_idle\":\"" + led_color_idle + "\",";
    json += "\"led_mode_listen\":" + String(led_mode_listen) + ",";
    json += "\"led_color_listen\":\"" + led_color_listen + "\",";
    json += "\"led_mode_think\":" + String(led_mode_think) + ",";
    json += "\"led_color_think\":\"" + led_color_think + "\",";
    json += "\"led_mode_speak\":" + String(led_mode_speak) + ",";
    json += "\"led_color_speak\":\"" + led_color_speak + "\"";
    json += "}}";
    
    client.send(json);
    Serial.println("[REGISTER] Sent registration packet to server.");
}

void onEventsCallback(WebsocketsEvent event, String data) {
    if (event == WebsocketsEvent::ConnectionOpened) {
        Serial.println("WebSocket Connected!");
        is_connected = true;
        set_state(STATE_IDLE);
        send_registration();
    } else if (event == WebsocketsEvent::ConnectionClosed) {
        Serial.println("WebSocket Disconnected");
        is_connected = false;
        set_state(STATE_IDLE);
        last_sleep_time = millis();
    }
}

bool detect_wakeword(int16_t* audio_buffer, size_t num_samples, float custom_threshold = -1.0) {
    size_t samples_processed = 0;
    struct FrontendOutput frontend_output = FrontendProcessSamples(&frontend_state, audio_buffer, num_samples, &samples_processed);
    
    if (frontend_output.size > 0) {
        int slices_produced = frontend_output.size / PREPROCESSOR_FEATURE_SIZE;
        float active_threshold = (custom_threshold > 0.0) ? custom_threshold : wake_word_threshold;

        for (int s = 0; s < slices_produced; s++) {
            // Нормализация (ESPHome INCEPTION style)
            for (size_t i = 0; i < PREPROCESSOR_FEATURE_SIZE; ++i) {
                int32_t value_scale = 256;
                int32_t value_div = 666; 
                int32_t value = ((frontend_output.values[s * PREPROCESSOR_FEATURE_SIZE + i] * value_scale) + (value_div / 2)) / value_div;
                value += -128;
                
                if (value < -128) value = -128;
                if (value > 127) value = 127;
                feature_ring_buffer[(feature_buffer_index * PREPROCESSOR_FEATURE_SIZE) + i] = (int8_t)value;
            }
            feature_buffer_index = (feature_buffer_index + 1) % num_slices;

            // Копируем кольцевой буфер в тензор
            for (int i = 0; i < num_slices; i++) {
                int ring_idx = (feature_buffer_index + i) % num_slices;
                memcpy(
                    input_tensor->data.int8 + (i * PREPROCESSOR_FEATURE_SIZE),
                    feature_ring_buffer + (ring_idx * PREPROCESSOR_FEATURE_SIZE),
                    PREPROCESSOR_FEATURE_SIZE
                );
            }

            if (interpreter->Invoke() == kTfLiteOk) {
                float prob = 0.0;
                if (output_tensor->type == kTfLiteUInt8) {
                    prob = (output_tensor->data.uint8[0] - output_tensor->params.zero_point) * output_tensor->params.scale;
                } else if (output_tensor->type == kTfLiteInt8) {
                    prob = (output_tensor->data.int8[0] - output_tensor->params.zero_point) * output_tensor->params.scale;
                }
                
                // Трекаем недавний пик для вебморды ESP32
                if (prob > recent_peak_score) {
                    recent_peak_score = prob;
                }
                if (millis() - last_peak_reset_time > 400) {
                    recent_peak_score = prob;
                    last_peak_reset_time = millis();
                }

                // Умное логирование в Serial (только если prob >= 0.30 и не чаще раз в 250 мс)
                if (prob >= 0.30f && (millis() - last_score_log_time > 250)) {
                    last_score_log_time = millis();
                    Serial.printf("[WW-DEBUG] Score: %.2f | Win: %d | Mode: %d | Thres: %.2f\n", 
                        prob, wake_word_window_size, wake_word_window_mode, active_threshold);
                }

                // Помещаем срез в кольцевой буфер скользящего окна
                prob_window[prob_window_head] = prob;
                prob_window_head = (prob_window_head + 1) % wake_word_window_size;

                // Режимы детекции скользящего окна
                bool detected = false;
                if (wake_word_window_mode == 1) {
                    // Режим 1: Peak Hold (максимальный пик в окне - идеален для слитной речи)
                    float max_p = 0.0f;
                    for (int w = 0; w < wake_word_window_size; w++) {
                        if (prob_window[w] > max_p) max_p = prob_window[w];
                    }
                    if (max_p >= active_threshold) detected = true;
                } else if (wake_word_window_mode == 2) {
                    // Режим 2: Majority (большинство срезов выше порога)
                    int count = 0;
                    for (int w = 0; w < wake_word_window_size; w++) {
                        if (prob_window[w] >= active_threshold) count++;
                    }
                    if (count >= (wake_word_window_size / 2 + 1)) detected = true;
                } else {
                    // Режим 0: Average (скользящее среднее)
                    float sum_p = 0.0f;
                    for (int w = 0; w < wake_word_window_size; w++) {
                        sum_p += prob_window[w];
                    }
                    if ((sum_p / wake_word_window_size) >= active_threshold) detected = true;
                }

                if (detected) {
                    // Сбрасываем окно для предотвращения дребезга
                    for (int w = 0; w < MAX_WINDOW_SIZE; w++) prob_window[w] = 0.0f;
                    recent_peak_score = prob;
                    return true;
                }
            }
        }
    }
    return false;
}

void setup() {
    Serial.begin(115200);
    delay(1000);
    
    // NVS Fix for bootloops
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    load_preferences();
    limiter.init(SAMPLE_RATE, 2.0f, 200.0f, 26000.0f, mic_gain * 1.8f);

    // Настройка светодиода
    FastLED.addLeds<WS2812, LED_BOARD_PIN, GRB>(leds, NUM_LEDS);
    leds[0] = CRGB::Black;
    FastLED.show();

    pinMode(LED_ONBOARD_PIN, OUTPUT);
    digitalWrite(LED_ONBOARD_PIN, LOW); // Выключен onboard LED

    setup_wifi();
    
    if (!captive_portal) {
        setup_i2s();
        setup_tflite();
        setup_frontend();
        
        client.onMessage(onMessageCallback);
        client.onEvent(onEventsCallback);
        client.connect(ws_host.c_str(), ws_port, "/");
    }
}

void loop() {
    if (captive_portal) dnsServer.processNextRequest();
    server.handleClient();
    update_led();
    
    if (captive_portal) {
        delay(10);
        return;
    }

    // Если идет процесс OTA обновления прошивки - изолируем микрофон, TFLite и аудио
    if (current_state == STATE_OTA) {
        client.poll();
        // Сторожевой таймер: если пакеты не приходили более 20 секунд - прерываем
        if (millis() - ota_last_packet_time > 20000) {
            Serial.println("[OTA] Timeout waiting for firmware chunks! Aborting...");
            Update.abort();
            set_state(STATE_IDLE);
            client.send("{\"type\":\"ota_error\",\"error\":\"Packet timeout\"}");
        }
        delay(1);
        return;
    }

    if (!is_connected || !client.available()) {
        is_connected = false;
        if (millis() - last_reconnect_time > (unsigned long)(reconnect_interval * 1000)) {
            Serial.println("Attempting to reconnect WebSocket...");
            last_reconnect_time = millis();
            client.connect(ws_host.c_str(), ws_port, "/");
        }
        delay(10);
        return;
    }

    // --- СТОРОЖЕВЫЕ ТАЙМЕРЫ (WATCHDOG) ---
    // 1. В режиме LISTENING: если прошло listen_timeout_s (например 6 сек)
    if (current_state == STATE_LISTENING && (millis() - state_enter_time > (unsigned long)(listen_timeout_s * 1000))) {
        Serial.println("[WATCHDOG] Listening timeout! Returning to IDLE.");
        set_state(STATE_IDLE);
        last_sleep_time = millis();
        client.send("{\"type\":\"timeout\"}");
    }

    // 2. В режиме THINKING: если сервер не отвечает 10 секунд (защита от зависания!)
    if (current_state == STATE_THINKING && (millis() - state_enter_time > 10000)) {
        Serial.println("[WATCHDOG] Thinking timeout (10s)! Returning to IDLE.");
        set_state(STATE_IDLE);
        last_sleep_time = millis();
    }

    // 3. В режиме SPEAKING: если последний аудиопакет был более 3.5 секунд назад
    if (current_state == STATE_SPEAKING && (millis() - state_enter_time > 3500)) {
        Serial.println("[WATCHDOG] Speaking timeout. Returning to IDLE.");
        set_state(STATE_IDLE);
        last_sleep_time = millis();
    }

    client.poll();

    // Фоновый Heartbeat телеметрии (RSSI, Uptime) каждые 20 сек в состоянии IDLE
    static unsigned long last_heartbeat_time = 0;
    if (is_connected && current_state == STATE_IDLE && millis() - last_heartbeat_time > 20000) {
        last_heartbeat_time = millis();
        String hb = "{\"type\":\"heartbeat\",\"rssi\":" + String(WiFi.RSSI()) + 
                    ",\"uptime\":" + String(millis() / 1000) + 
                    ",\"state\":\"idle\"}";
        client.send(hb);
    }

    // Считываем микрофон
    size_t bytes_read = 0;
    i2s_read(I2S_NUM_0, mic_buffer_32, sizeof(mic_buffer_32), &bytes_read, portMAX_DELAY);
    int samples_read = bytes_read / 4;
    
    // Высококачественная обработка микрофона INMP441:
    // Сдвиг на 12 бит берет оптимальные 20 бит из 24-битного слова INMP441.
    // 1-й порядок DC-блокер (Leaky Integrator) чисто убирает постоянное смещение без звона.
    // WebRTC Peak Limiter мягко компрессирует громкие звуки (атака 2мс, порог 26000),
    // не допуская клиппинга и хрипа при крике, сохраняя чистый и разборчивый шепот.
    static float dc_x1 = 0.0f;
    static float dc_y1 = 0.0f;
    const float R = 0.985f;

    int32_t sum_amp = 0;
    for (int i = 0; i < samples_read; i++) {
        float x = (float)(mic_buffer_32[i] >> 12);
        float y = x - dc_x1 + R * dc_y1;
        dc_x1 = x;
        dc_y1 = y;

        mic_buffer_16[i] = limiter.process(y);
        sum_amp += abs(mic_buffer_16[i]);
        
        pre_roll_buffer[pre_roll_head] = mic_buffer_16[i];
        pre_roll_head = (pre_roll_head + 1) % PRE_ROLL_SAMPLES;
    }
    int avg_amp = samples_read > 0 ? (sum_amp / samples_read) : 0;

    // --- ЛОГИКА КОНЕЧНОГО АВТОМАТА (STATE MACHINE) ---
    if (current_state == STATE_IDLE) {
        if (wake_word_mode == "server") {
            // Серверный вейкворд: плата непрерывно транслирует аудио в WebSocket
            if (is_connected && (millis() - last_sleep_time > 500)) {
                send_mic_data_batched(mic_buffer_16, samples_read);
            }
        } else {
            // ТОЛЬКО В РЕЖИМЕ IDLE ДЕТЕКТИРУЕТСЯ ВЕЙКВОРД!
            bool detected = detect_wakeword(mic_buffer_16, samples_read);
            if (millis() - last_sleep_time > 1500) {
                if (detected) {
                    Serial.println("Wake word detected! Entering LISTENING state...");
                    set_state(STATE_LISTENING);
                    user_has_spoken = false;
                    last_speech_time = millis();
                    ws_tx_buffer_len = 0; // Сбрасываем батч-буфер при новом диалоге
                    client.send("{\"type\":\"wake_word_detected\"}");
                    
                    // Отправляем буфер предзаписи
                    if (is_connected) {
                        int oldest_idx = pre_roll_head;
                        int oldest_count = PRE_ROLL_SAMPLES - oldest_idx;
                        client.sendBinary((const char*)(pre_roll_buffer + oldest_idx), oldest_count * 2);
                        if (oldest_idx > 0) {
                            client.sendBinary((const char*)pre_roll_buffer, oldest_idx * 2);
                        }
                    }
                }
            }
        }
    }
    else if (current_state == STATE_LISTENING) {
        // Передаем звук микрофона в WebSocket батчами
        send_mic_data_batched(mic_buffer_16, samples_read);

        // Локальный детектор тишины
        if (avg_amp > silence_threshold_energy) {
            user_has_spoken = true;
            last_speech_time = millis();
        } else if (user_has_spoken && (millis() - last_speech_time > (unsigned long)silence_timeout_ms)) {
            Serial.println("Silence detected after speech! Switching to THINKING.");
            set_state(STATE_THINKING);
            client.send("{\"type\":\"end_of_speech\"}");
        }
    }
    else if (current_state == STATE_SPEAKING) {
        // Отправляем звук микрофона на бэкенд для серверной обработки прерываний (Barge-in)
        send_mic_data_batched(mic_buffer_16, samples_read);

        // Локальный детектор на ESP32 сохранен, но принудительно отключен (enable_barge_in = false)
        if (enable_barge_in) {
            float barge_thresh = (wake_word_threshold + 0.03f > 0.98f) ? 0.98f : (wake_word_threshold + 0.03f);
            bool detected = detect_wakeword(mic_buffer_16, samples_read, barge_thresh);
            if (detected) {
                Serial.println("[BARGE-IN] Wake word detected during speech! Interrupting speaker...");
                i2s_zero_dma_buffer(I2S_NUM_1);
                set_state(STATE_LISTENING);
                user_has_spoken = false;
                last_speech_time = millis();
                client.send("{\"type\":\"interrupted\"}");
                client.send("{\"type\":\"wake_word_detected\"}");
                
                if (is_connected) {
                    int oldest_idx = pre_roll_head;
                    int oldest_count = PRE_ROLL_SAMPLES - oldest_idx;
                    client.sendBinary((const char*)(pre_roll_buffer + oldest_idx), oldest_count * 2);
                    if (oldest_idx > 0) {
                        client.sendBinary((const char*)pre_roll_buffer, oldest_idx * 2);
                    }
                }
            }
        }
    }
    else if (current_state == STATE_MIC_TEST) {
        // Передаем сырой звук микрофона в WebSocket для тестовой записи
        send_mic_data_batched(mic_buffer_16, samples_read);
        
        if (millis() >= mic_test_end_time) {
            Serial.println("[MIC-TEST] Finished mic test duration. Returning to IDLE.");
            client.send("{\"type\":\"mic_test_complete\"}");
            set_state(STATE_IDLE);
            last_sleep_time = millis();
        }
    }
}
