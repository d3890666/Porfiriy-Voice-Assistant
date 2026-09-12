#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <ArduinoWebsockets.h>
#include <driver/i2s.h>
#include <FastLED.h>
#include "model.h"

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
bool captive_portal = false;

// Буферы для микрофона
#define SAMPLE_RATE 16000
#define BUFFER_SAMPLES 1024
int32_t mic_buffer_32[BUFFER_SAMPLES];
int16_t mic_buffer_16[BUFFER_SAMPLES];

// HTML страница настроек
const char index_html[] PROGMEM = R"rawliteral(
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Настройка Porfiriy</title>
<style>
  body { font-family: sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; background:#222; color:#fff; margin:0;}
  form { background:#333; padding:20px; border-radius:10px; box-shadow:0 0 15px rgba(0,0,0,0.5); width:90%; max-width:400px;}
  label { display:block; margin-bottom:5px; color:#aaa; font-size:14px;}
  input { display:block; width:100%; margin-bottom:20px; padding:10px; box-sizing:border-box; border-radius:5px; border:none; font-size:16px;}
  input[type=submit] { background:#007bff; color:white; font-weight:bold; cursor:pointer; margin-top:10px;}
  input[type=submit]:hover { background:#0056b3; }
  h2 { text-align: center; margin-top:0; color:#4dabf7; }
</style>
</head><body>
<form action="/save" method="POST">
  <h2>Настройка Сети</h2>
  <label>Название Wi-Fi (SSID)</label>
  <input type="text" name="ssid" placeholder="MyWiFi" required>
  <label>Пароль Wi-Fi</label>
  <input type="password" name="password" placeholder="***">
  <label>IP-адрес Сервера (Home Assistant)</label>
  <input type="text" name="host" value="192.168.1.50" required>
  <label>Порт WebSocket</label>
  <input type="number" name="port" value="8765" required>
  <input type="submit" value="Сохранить и перезагрузить">
</form>
</body></html>
)rawliteral";

void handleRoot() {
  server.send(200, "text/html", index_html);
}

void handleSave() {
  if (server.hasArg("ssid") && server.hasArg("host") && server.hasArg("port")) {
    preferences.begin("porfiriy", false);
    preferences.putString("ssid", server.arg("ssid"));
    preferences.putString("password", server.arg("password"));
    preferences.putString("host", server.arg("host"));
    preferences.putUInt("port", server.arg("port").toInt());
    preferences.end();
    
    server.send(200, "text/html", "<!DOCTYPE html><html><body style='background:#222;color:#fff;text-align:center;font-family:sans-serif;margin-top:20vh;'><h2>Настройки сохранены!</h2><p>Устройство перезагружается...</p></body></html>");
    delay(2000);
    ESP.restart();
  } else {
    server.send(400, "text/html", "Ошибка: заполните все поля.");
  }
}

void setup_wifi() {
    preferences.begin("porfiriy", true);
    ssid = preferences.getString("ssid", "");
    password = preferences.getString("password", "");
    ws_host = preferences.getString("host", "192.168.1.50");
    ws_port = preferences.getUInt("port", 8765);
    preferences.end();

    if (ssid != "") {
        Serial.print("Connecting to WiFi: ");
        Serial.println(ssid);
        WiFi.begin(ssid.c_str(), password.c_str());
        
        int attempts = 0;
        // Ждем подключения до 10 секунд
        while (WiFi.status() != WL_CONNECTED && attempts < 20) {
            delay(500);
            Serial.print(".");
            attempts++;
        }
    }

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("\nWiFi connection failed or empty settings. Starting Captive Portal...");
        captive_portal = true;
        
        // Индикация режима настройки (Желтый цвет)
        leds[0] = CRGB::Yellow;
        FastLED.show();

        WiFi.mode(WIFI_AP);
        WiFi.softAP("Porfiriy_Setup");
        
        dnsServer.start(53, "*", WiFi.softAPIP());
        
        server.on("/", handleRoot);
        server.on("/save", handleSave);
        server.onNotFound([](){
          server.sendHeader("Location", "http://192.168.4.1/", true);
          server.send(302, "text/plain", "");
        });
        server.begin();
        Serial.println("AP started. Connect to 'Porfiriy_Setup' and open http://192.168.4.1");
    } else {
        Serial.println("\nConnected to WiFi!");
    }
}

void setup_i2s() {
    // Настройка I2S для микрофона
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

    // Настройка I2S для динамика
    i2s_config_t i2s_spk_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
        .sample_rate = 22050,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
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

void onMessageCallback(WebsocketsMessage message) {
    if (message.isBinary()) {
        size_t bytes_written;
        i2s_write(I2S_NUM_1, message.c_str(), message.length(), &bytes_written, portMAX_DELAY);
    } else if (message.isText()) {
        Serial.println("Server text: " + message.data());
    }
}

void onEventsCallback(WebsocketsEvent event, String data) {
    if (event == WebsocketsEvent::ConnectionOpened) {
        Serial.println("WebSocket Connected!");
        is_connected = true;
        leds[0] = CRGB::Green;
        FastLED.show();
    } else if (event == WebsocketsEvent::ConnectionClosed) {
        Serial.println("WebSocket Disconnected");
        is_connected = false;
        leds[0] = CRGB::Red;
        FastLED.show();
    }
}

// Заглушка для функции извлечения признаков и вызова TFLite Micro
bool detect_wakeword(int16_t* audio_buffer, size_t num_samples) {
    // В реальной интеграции microWakeWord здесь необходимо вычислить STFT / Mel Filterbanks
    // и передать их на вход интерпретатора: interpreter->Invoke();
    // Возвращаем true, если обнаружено слово "Порфирий".
    // Для отладки можно временно эмулировать активацию по уровню звука:
    long long sum = 0;
    for(size_t i=0; i<num_samples; i++) {
        sum += abs(audio_buffer[i]);
    }
    int avg = sum / num_samples;
    // Если громко, эмулируем активацию
    // if(avg > 5000) return true; 
    
    return false; // Замените на логику TFLite
}

void setup() {
    Serial.begin(115200);
    
    // Настройка светодиодов
    FastLED.addLeds<WS2812, LED_BOARD_PIN, GRB>(leds, NUM_LEDS);
    leds[0] = CRGB::Red;
    FastLED.show();

    pinMode(LED_ONBOARD_PIN, OUTPUT);
    digitalWrite(LED_ONBOARD_PIN, LOW); // Выключен

    setup_wifi();
    
    if (!captive_portal) {
        setup_i2s();
        client.onMessage(onMessageCallback);
        client.onEvent(onEventsCallback);
        client.connect(ws_host.c_str(), ws_port, "/");
    }
}

void loop() {
    if (captive_portal) {
        dnsServer.processNextRequest();
        server.handleClient();
        return; // Блокируем остальной код, пока мы в режиме настройки
    }

    client.poll();

    size_t bytes_read = 0;
    i2s_read(I2S_NUM_0, mic_buffer_32, sizeof(mic_buffer_32), &bytes_read, portMAX_DELAY);
    
    int samples_read = bytes_read / 4;
    
    // Конвертация 32-bit (микрофон) в 16-bit
    for (int i = 0; i < samples_read; i++) {
        mic_buffer_16[i] = mic_buffer_32[i] >> 14; 
    }

    if (!is_listening) {
        // Проверяем вейкворд локально
        if (detect_wakeword(mic_buffer_16, samples_read)) {
            Serial.println("Wake word detected! Starting stream...");
            is_listening = true;
            leds[0] = CRGB::Blue; // Синий при прослушивании
            FastLED.show();
            client.send("{\"type\":\"wake_word_detected\"}");
        }
    } else {
        // Стримим аудио на сервер, если подключены
        if (is_connected) {
            client.sendBinary((const char*)mic_buffer_16, samples_read * 2);
        }
    }
}
