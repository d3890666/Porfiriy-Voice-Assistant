#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <ArduinoWebsockets.h>
#include <driver/i2s.h>
#include <FastLED.h>
#include <nvs_flash.h>

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
int mic_gain = 2;
float speaker_volume = 1.0;
float wake_word_threshold = 0.93;
int led_brightness = 50;
String led_color_idle = "#000000";
String led_color_listen = "#0000ff";
String led_color_speak = "#00ff00";
int led_mode = 1; // 0=Off, 1=Solid, 2=Breathing
int reconnect_interval = 5; // в секундах

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

// --- Состояния ---
bool is_listening = false;
bool is_connected = false;
bool is_speaking = false;
bool captive_portal = false;
unsigned long last_reconnect_time = 0;

// --- Буферы для микрофона ---
#define SAMPLE_RATE 16000
#define BUFFER_SAMPLES 512
int32_t mic_buffer_32[BUFFER_SAMPLES];
int16_t mic_buffer_16[BUFFER_SAMPLES];

// Pre-roll буфер (последняя 1 секунда звука = 16000 сэмплов)
#define PRE_ROLL_SAMPLES 16000
int16_t pre_roll_buffer[PRE_ROLL_SAMPLES];
int pre_roll_head = 0;

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
    if (led_mode == 0) {
        leds[0] = CRGB::Black;
        FastLED.show();
        return;
    }
    
    CRGB target_color;
    if (is_speaking) target_color = hexToCRGB(led_color_speak);
    else if (is_listening) target_color = hexToCRGB(led_color_listen);
    else target_color = hexToCRGB(led_color_idle);
    
    if (led_mode == 1) { // Solid
        leds[0] = target_color;
        FastLED.setBrightness(led_brightness);
    } else if (led_mode == 2) { // Breathing
        // Простое синусоидальное "дыхание" раз в 2 секунды
        float breathe = (exp(sin(millis()/2000.0*PI)) - 0.36787944)*108.0; 
        // 0..255 примерно
        int b = (int)((breathe / 255.0) * led_brightness);
        leds[0] = target_color;
        FastLED.setBrightness(b);
    }
    FastLED.show();
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
  <p>Состояние: <span id="status-text" class="status-badge">Загрузка...</span></p>
  <p>Связь с сервером: <span id="conn-text" class="status-badge">Загрузка...</span></p>
  <button type="button" onclick="forceReconnect()" style="margin-top:10px; padding:5px 10px; background:#ffc107; color:#000; border:none; border-radius:5px; cursor:pointer; font-weight:bold;">Переподключить сейчас</button>
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
  <label>Усиление микрофона (x1-x10)</label>
  <input type="number" name="mic_gain" value="%MIC_GAIN%" min="1" max="10">
  <label>Громкость динамика (0.1 - 2.0)</label>
  <input type="number" step="0.1" name="speaker_volume" value="%SPK_VOL%">
  <label>Порог вейкворда (0.0 - 1.0)</label>
  <input type="number" step="0.01" name="wake_word_threshold" value="%WW_THRES%">
  
  <h2 style="margin-top:30px;">Настройки Подсветки (На лету)</h2>
  <label>Режим диода</label>
  <select name="led_mode">
    <option value="0" %LED_M0%>Выключен</option>
    <option value="1" %LED_M1%>Светится</option>
    <option value="2" %LED_M2%>Дышит</option>
  </select>
  <label>Яркость (0 - 255)</label>
  <input type="range" name="led_brightness" value="%LED_BRIGHT%" min="0" max="255" style="padding:0">
  <label>Цвет (Ожидание)</label>
  <input type="color" name="led_color_idle" value="%LED_CIDLE%" style="height:40px; padding:0;">
  <label>Цвет (Слушает)</label>
  <input type="color" name="led_color_listen" value="%LED_CLISTEN%" style="height:40px; padding:0;">
  <label>Цвет (Говорит)</label>
  <input type="color" name="led_color_speak" value="%LED_CSPEAK%" style="height:40px; padding:0;">

  <input type="submit" value="Сохранить настройки">
</form>

<script>
setInterval(() => {
  fetch('/status').then(r => r.json()).then(data => {
    let st = document.getElementById('status-text');
    let ct = document.getElementById('conn-text');
    if(data.is_speaking) { st.innerText = "Отвечаю..."; st.style.background = "#28a745"; }
    else if(data.is_listening) { st.innerText = "Слушаю вас..."; st.style.background = "#007bff"; }
    else { st.innerText = "Ожидание слова"; st.style.background = "#6c757d"; }
    
    if(data.is_connected) { ct.innerText = "Подключено"; ct.style.background = "#28a745"; }
    else { ct.innerText = "Отключено"; ct.style.background = "#dc3545"; }
  }).catch(() => {
    document.getElementById('status-text').innerText = "Плата недоступна";
    document.getElementById('status-text').style.background = "#dc3545";
  });
}, 1000);

// Перехват отправки формы для сохранения "на лету" без ребута (если wifi/сервер не менялись)
document.getElementById('settingsForm').addEventListener('submit', function(e) {
  e.preventDefault();
  let fd = new FormData(this);
  fetch('/save', {
    method: 'POST',
    body: new URLSearchParams(fd)
  }).then(r => r.text()).then(t => {
    if(t.includes("перезагружается")) {
      document.body.innerHTML = "<h2 style='text-align:center;margin-top:20vh;'>Настройки сети изменены. Перезагрузка...</h2>";
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
    html.replace("%MIC_GAIN%", String(mic_gain));
    html.replace("%SPK_VOL%", String(speaker_volume, 1));
    html.replace("%WW_THRES%", String(wake_word_threshold, 2));
    
    html.replace("%LED_M0%", led_mode == 0 ? "selected" : "");
    html.replace("%LED_M1%", led_mode == 1 ? "selected" : "");
    html.replace("%LED_M2%", led_mode == 2 ? "selected" : "");
    html.replace("%LED_BRIGHT%", String(led_brightness));
    html.replace("%LED_CIDLE%", led_color_idle);
    html.replace("%LED_CLISTEN%", led_color_listen);
    html.replace("%LED_CSPEAK%", led_color_speak);
    
    server.send(200, "text/html", html);
}

void handleStatus() {
    String json = "{\"is_connected\": " + String(is_connected ? "true" : "false") + 
                  ", \"is_listening\": " + String(is_listening ? "true" : "false") + 
                  ", \"is_speaking\": " + String(is_speaking ? "true" : "false") + "}";
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
    if (server.hasArg("mic_gain")) { mic_gain = server.arg("mic_gain").toInt(); preferences.putInt("mic_gain", mic_gain); }
    if (server.hasArg("speaker_volume")) { speaker_volume = server.arg("speaker_volume").toFloat(); preferences.putFloat("spk_vol", speaker_volume); }
    if (server.hasArg("wake_word_threshold")) { wake_word_threshold = server.arg("wake_word_threshold").toFloat(); preferences.putFloat("ww_thres", wake_word_threshold); }
    if (server.hasArg("reconnect_interval")) { reconnect_interval = server.arg("reconnect_interval").toInt(); preferences.putInt("reconn_int", reconnect_interval); }
    
    // Настройки LED
    if (server.hasArg("led_mode")) { led_mode = server.arg("led_mode").toInt(); preferences.putInt("led_mode", led_mode); }
    if (server.hasArg("led_brightness")) { led_brightness = server.arg("led_brightness").toInt(); preferences.putInt("led_bright", led_brightness); }
    if (server.hasArg("led_color_idle")) { led_color_idle = server.arg("led_color_idle"); preferences.putString("led_cidle", led_color_idle); }
    if (server.hasArg("led_color_listen")) { led_color_listen = server.arg("led_color_listen"); preferences.putString("led_clisten", led_color_listen); }
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
    mic_gain = preferences.getInt("mic_gain", 2);
    speaker_volume = preferences.getFloat("spk_vol", 1.0);
    wake_word_threshold = preferences.getFloat("ww_thres", 0.93);
    reconnect_interval = preferences.getInt("reconn_int", 5);
    
    led_mode = preferences.getInt("led_mode", 1);
    led_brightness = preferences.getInt("led_bright", 50);
    led_color_idle = preferences.getString("led_cidle", "#000000");
    led_color_listen = preferences.getString("led_clisten", "#0000ff");
    led_color_speak = preferences.getString("led_cspeak", "#00ff00");
    preferences.end();
}

void setup_wifi() {
    if (ssid != "") {
        Serial.print("Connecting to WiFi: ");
        Serial.println(ssid);
        WiFi.begin(ssid.c_str(), password.c_str());
        
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
        .dma_buf_count = 8,
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
        is_speaking = true; // При получении аудио переходим в статус "Отвечаю"
        
        // Применяем speaker_volume и пишем в I2S ЧАНКАМИ, чтобы избежать переполнения стека!
        const int16_t* pcm = (const int16_t*)message.c_str();
        int num_samples = message.length() / 2;
        int chunk_size = 512;
        
        for (int i = 0; i < num_samples; i += chunk_size) {
            int current_chunk = (num_samples - i < chunk_size) ? (num_samples - i) : chunk_size;
            int16_t vol_buffer[512]; // Фиксированный безопасный размер буфера на стеке (1 КБ)
            
            for(int j = 0; j < current_chunk; j++) {
                float val = pcm[i + j] * speaker_volume;
                if (val > 32767) val = 32767;
                if (val < -32768) val = -32768;
                vol_buffer[j] = (int16_t)val;
            }
            size_t bytes_written;
            i2s_write(I2S_NUM_1, vol_buffer, current_chunk * 2, &bytes_written, portMAX_DELAY);
        }
    } else if (message.isText()) {
        Serial.println("Server text: " + message.data());
        
        // Обработка команд от бэкенда
        if (message.data().indexOf("\"type\":\"sleep\"") >= 0 || message.data().indexOf("\"type\": \"sleep\"") >= 0) {
            Serial.println("Server commanded SLEEP. Returning to wake word mode.");
            is_listening = false;
            is_speaking = false;
        }
        else if (message.data().indexOf("\"type\":\"speaking\"") >= 0) {
            is_speaking = true;
        }
        else if (message.data().indexOf("\"type\":\"done_speaking\"") >= 0) {
            is_speaking = false;
        }
    }
}

void onEventsCallback(WebsocketsEvent event, String data) {
    if (event == WebsocketsEvent::ConnectionOpened) {
        Serial.println("WebSocket Connected!");
        is_connected = true;
    } else if (event == WebsocketsEvent::ConnectionClosed) {
        Serial.println("WebSocket Disconnected");
        is_connected = false;
        is_listening = false;
        is_speaking = false;
    }
}

bool detect_wakeword(int16_t* audio_buffer, size_t num_samples) {
    size_t samples_processed = 0;
    struct FrontendOutput frontend_output = FrontendProcessSamples(&frontend_state, audio_buffer, num_samples, &samples_processed);
    
    if (frontend_output.size > 0) {
        int slices_produced = frontend_output.size / PREPROCESSOR_FEATURE_SIZE;
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
        }

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
            
            if (prob >= wake_word_threshold) {
                return true;
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

    if (!is_connected) {
        if (millis() - last_reconnect_time > (reconnect_interval * 1000)) {
            Serial.println("Attempting to reconnect WebSocket...");
            last_reconnect_time = millis();
            client.connect(ws_host.c_str(), ws_port, "/");
        }
        delay(100);
        return;
    }

    client.poll();

    size_t bytes_read = 0;
    i2s_read(I2S_NUM_0, mic_buffer_32, sizeof(mic_buffer_32), &bytes_read, portMAX_DELAY);
    
    int samples_read = bytes_read / 4;
    
    // Конвертация 32-bit в 16-bit + Применение усиления микрофона
    for (int i = 0; i < samples_read; i++) {
        int32_t val = (mic_buffer_32[i] >> 16) * mic_gain;
        if (val > 32767) val = 32767;
        if (val < -32768) val = -32768;
        mic_buffer_16[i] = (int16_t)val;
        
        // Пишем в pre-roll буфер
        pre_roll_buffer[pre_roll_head] = mic_buffer_16[i];
        pre_roll_head = (pre_roll_head + 1) % PRE_ROLL_SAMPLES;
    }

    if (!is_listening) {
        if (detect_wakeword(mic_buffer_16, samples_read)) {
            Serial.println("Wake word detected! Sending pre-roll buffer...");
            is_listening = true;
            client.send("{\"type\":\"wake_word_detected\"}");
            
            // Отправляем предзаписанный буфер по частям (чтобы не превысить размер пакета WebSocket)
            if (is_connected) {
                // Данные в буфере идут от pre_roll_head (самые старые) до pre_roll_head - 1 (самые свежие)
                int oldest_idx = pre_roll_head;
                int oldest_count = PRE_ROLL_SAMPLES - oldest_idx;
                
                // Отправляем первую часть (до конца массива)
                client.sendBinary((const char*)(pre_roll_buffer + oldest_idx), oldest_count * 2);
                // Отправляем вторую часть (от начала массива до pre_roll_head)
                if (oldest_idx > 0) {
                    client.sendBinary((const char*)pre_roll_buffer, oldest_idx * 2);
                }
            }
        }
    } else {
        if (is_connected) {
            client.sendBinary((const char*)mic_buffer_16, samples_read * 2);
        }
    }
}
