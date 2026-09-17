#include <Arduino.h>
#include <driver/i2s.h>
#include <FastLED.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_wifi.h>

#define I2S_MIC_BCLK 3
#define I2S_MIC_LRCLK 8
#define I2S_MIC_DIN 10

#define LED_BOARD_PIN 48
#define NUM_LEDS 1
CRGB leds[NUM_LEDS];
bool led_enabled = false;

// Wi-Fi радио-эмулятор нагрузки
bool wifi_tx_enabled = false;
WiFiUDP udp;
uint8_t dummy_tx_payload[512];

#define SAMPLE_RATE 16000
#define BUFFER_SAMPLES 256

// Настройки по умолчанию
int current_shift = 12;
float current_gain = 3.5f; // Базовое усиление (громкий шепот)
bool enable_dc_filter = true;
bool enable_limiter = true;
int current_pull = 1; // 0=Float, 1=PullDown, 2=PullUp
i2s_channel_fmt_t current_channel = I2S_CHANNEL_FMT_ONLY_LEFT;

bool is_streaming = false;
uint16_t packet_seq = 0;

int32_t raw_buffer_32[BUFFER_SAMPLES];
int16_t processed_buffer_16[BUFFER_SAMPLES];

// Фильтр DC
float dc_x1 = 0.0f;
float dc_y1 = 0.0f;
const float R = 0.985f;

// Классический DSP пиковый лимитер с быстрой атакой и плавным релизом (WebRTC/ITU-T style)
class AudioLimiter {
private:
    float envelope = 0.0f;
    float attack_coeff = 0.0f;
    float release_coeff = 0.0f;
    float target_threshold = 26000.0f; // Порог сжатия (-2 dBFS)
    float base_gain = 3.5f;

public:
    void init(float sample_rate, float attack_ms = 2.0f, float release_ms = 200.0f, float threshold = 26000.0f, float gain = 3.5f) {
        attack_coeff = expf(-1.0f / (sample_rate * (attack_ms / 1000.0f)));
        release_coeff = expf(-1.0f / (sample_rate * (release_ms / 1000.0f)));
        target_threshold = threshold;
        base_gain = gain;
        envelope = 0.0f;
    }

    void set_gain(float g) {
        base_gain = g;
    }

    float get_gain() {
        return base_gain;
    }

    inline int16_t process(float sample_after_dc) {
        float boosted = sample_after_dc * base_gain;
        float abs_s = fabsf(boosted);

        // Огибающая (Peak Detector)
        if (abs_s > envelope) {
            envelope = attack_coeff * envelope + (1.0f - attack_coeff) * abs_s;
        } else {
            envelope = release_coeff * envelope + (1.0f - release_coeff) * abs_s;
        }

        // Плавный коэффициент компрессии
        float gain_reduction = 1.0f;
        if (envelope > target_threshold) {
            gain_reduction = target_threshold / envelope;
        }

        float out = boosted * gain_reduction;

        // Защитный хард-клип (safety clamp)
        if (out > 32767.0f) out = 32767.0f;
        if (out < -32768.0f) out = -32768.0f;

        return (int16_t)roundf(out);
    }
};

AudioLimiter limiter;

void update_led() {
    static unsigned long last_led_update = 0;
    if (millis() - last_led_update < 30) return; // ~33 FPS
    last_led_update = millis();

    if (!led_enabled) {
        leds[0] = CRGB::Black;
        FastLED.show();
        return;
    }

    float breathe = (exp(sin(millis() / 1500.0 * PI)) - 0.36787944) * 108.0;
    int b = (int)((breathe / 255.0) * 120);
    if (b < 10) b = 10;
    if (b > 255) b = 255;
    leds[0] = CRGB::Blue;
    FastLED.setBrightness(b);
    FastLED.show();
}

void update_wifi_tx() {
    if (!wifi_tx_enabled) return;
    static unsigned long last_tx = 0;
    if (millis() - last_tx >= 20) {
        last_tx = millis();
        udp.beginPacket(IPAddress(192, 168, 4, 255), 8888);
        udp.write(dummy_tx_payload, sizeof(dummy_tx_payload));
        udp.endPacket();
    }
}

void apply_pin_pull() {
    if (current_pull == 1) {
        pinMode(I2S_MIC_DIN, INPUT_PULLDOWN);
    } else if (current_pull == 2) {
        pinMode(I2S_MIC_DIN, INPUT_PULLUP);
    } else {
        pinMode(I2S_MIC_DIN, INPUT);
    }
}

void init_i2s() {
    i2s_driver_uninstall(I2S_NUM_0);

    i2s_config_t i2s_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate = SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
        .channel_format = current_channel,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 16,
        .dma_buf_len = BUFFER_SAMPLES,
        .use_apll = false,
        .tx_desc_auto_clear = false,
        .fixed_mclk = 0
    };

    i2s_pin_config_t pin_config = {
        .bck_io_num = I2S_MIC_BCLK,
        .ws_io_num = I2S_MIC_LRCLK,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num = I2S_MIC_DIN
    };

    i2s_driver_install(I2S_NUM_0, &i2s_config, 0, NULL);
    i2s_set_pin(I2S_NUM_0, &pin_config);
    apply_pin_pull();
}

void print_binary32(uint32_t val) {
    for (int b = 31; b >= 0; b--) {
        Serial.print((val >> b) & 1);
        if (b % 8 == 0 && b > 0) Serial.print(" ");
    }
}

void do_dump() {
    size_t bytes_read = 0;
    i2s_read(I2S_NUM_0, raw_buffer_32, sizeof(raw_buffer_32), &bytes_read, portMAX_DELAY);
    int n = bytes_read / 4;
    if (n > 20) n = 20;

    Serial.println("\n--- [I2S RAW 32-BIT DUMP] ---");
    Serial.printf("Channel: %s | Pull: %d | Shift: >> %d | Gain: %.1f | Limiter: %s | LED: %s | Wi-Fi: %s\n",
                  (current_channel == I2S_CHANNEL_FMT_ONLY_LEFT ? "LEFT" : "RIGHT"),
                  current_pull, current_shift, current_gain,
                  enable_limiter ? "ON (Auto-compress loud sounds)" : "OFF",
                  led_enabled ? "ON" : "OFF",
                  wifi_tx_enabled ? "ACTIVE" : "OFF");
    
    for (int i = 0; i < n; i++) {
        uint32_t uval = (uint32_t)raw_buffer_32[i];
        int32_t sval = raw_buffer_32[i];
        int16_t s16 = (int16_t)(sval >> 16);
        int16_t s14 = (int16_t)(sval >> 14);
        int16_t s12 = (int16_t)(sval >> 12);

        Serial.printf("[%02d] HEX: 0x%08X | BIN: ", i, uval);
        print_binary32(uval);
        Serial.printf(" | >>16: %6d | >>14: %6d | >>12: %6d\n", s16, s14, s12);
    }
    Serial.println("--- [END DUMP] ---\n");
}

void do_stats() {
    Serial.println("\nCollecting 1 second of audio for statistics...");
    const int total_samples = 16000;
    int collected = 0;
    
    int32_t min_v = 32767;
    int32_t max_v = -32768;
    int64_t sum = 0;
    int64_t sum_sq = 0;
    int click_count = 0;
    int16_t prev_sample = 0;

    while (collected < total_samples) {
        update_led();
        update_wifi_tx();
        size_t bytes_read = 0;
        i2s_read(I2S_NUM_0, raw_buffer_32, sizeof(raw_buffer_32), &bytes_read, portMAX_DELAY);
        int n = bytes_read / 4;

        for (int i = 0; i < n && collected < total_samples; i++) {
            float x = (float)(raw_buffer_32[i] >> current_shift);
            float y = x;
            if (enable_dc_filter) {
                y = x - dc_x1 + R * dc_y1;
                dc_x1 = x;
                dc_y1 = y;
            }

            int16_t s;
            if (enable_limiter) {
                s = limiter.process(y);
            } else {
                float v = y * current_gain;
                if (v > 32767.0f) v = 32767.0f;
                if (v < -32768.0f) v = -32768.0f;
                s = (int16_t)roundf(v);
            }

            if (s < min_v) min_v = s;
            if (s > max_v) max_v = s;
            sum += s;
            sum_sq += (int64_t)s * s;

            if (collected > 0) {
                int diff = abs((int)s - (int)prev_sample);
                if (diff > 4000) click_count++;
            }
            prev_sample = s;
            collected++;
        }
    }

    float mean = (float)sum / total_samples;
    float rms = sqrtf((float)sum_sq / total_samples);
    float pk_pk = (float)(max_v - min_v);

    Serial.println("--- [AUDIO METRICS REPORT] ---");
    Serial.printf("Samples Analyzed : %d (1.0 sec)\n", total_samples);
    Serial.printf("Limiter Status   : %s\n", enable_limiter ? "ENABLED (WebRTC Soft Limiter)" : "DISABLED");
    Serial.printf("Min / Max Sample : %d / %d\n", min_v, max_v);
    Serial.printf("Peak-to-Peak     : %.0f\n", pk_pk);
    Serial.printf("DC Offset (Mean) : %.2f\n", mean);
    Serial.printf("RMS Energy       : %.2f\n", rms);
    Serial.printf("Click/Spike Count: %d (jumps > 4000)\n", click_count);
    if (click_count > 10) {
        Serial.println(">> DIAGNOSTIC VERDICT: CRACKLE DETECTED (High jump rate!)");
    } else if (rms < 5.0f && pk_pk < 20) {
        Serial.println(">> DIAGNOSTIC VERDICT: SILENCE / DEAD MIC (Check VDD/GND/LR pin!)");
    } else {
        Serial.println(">> DIAGNOSTIC VERDICT: CLEAN AUDIO SIGNAL");
    }
    Serial.println("--- [END REPORT] ---\n");
}

void process_serial_command(String cmd) {
    cmd.trim();
    if (cmd.length() == 0) return;

    if (cmd == "STREAM") {
        is_streaming = true;
        packet_seq = 0;
        Serial.println("\n[OK] STREAM_STARTED");
    } else if (cmd == "STOP") {
        is_streaming = false;
        delay(50);
        Serial.println("\n[OK] STREAM_STOPPED");
    } else if (cmd == "DUMP") {
        is_streaming = false;
        do_dump();
    } else if (cmd == "STATS") {
        is_streaming = false;
        do_stats();
    } else if (cmd.startsWith("SET LED ")) {
        led_enabled = (cmd.substring(8).toInt() != 0);
        if (!led_enabled) {
            leds[0] = CRGB::Black;
            FastLED.show();
        }
        Serial.printf("[OK] LED WS2812B: %s\n", led_enabled ? "ENABLED" : "DISABLED");
    } else if (cmd.startsWith("SET WIFI ")) {
        int en = cmd.substring(9).toInt();
        if (en) {
            WiFi.mode(WIFI_AP);
            WiFi.softAP("Porfiriy_Diag_AP", "12345678");
            WiFi.setTxPower(WIFI_POWER_8_5dBm);
            esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
            udp.begin(8888);
            wifi_tx_enabled = true;
            Serial.println("[OK] Wi-Fi: ENABLED (AP Mode, 8.5 dBm, MIN_MODEM PS, UDP active)");
        } else {
            wifi_tx_enabled = false;
            udp.stop();
            WiFi.disconnect(true);
            WiFi.mode(WIFI_OFF);
            Serial.println("[OK] Wi-Fi: DISABLED");
        }
    } else if (cmd.startsWith("SET TXPOWER ")) {
        int p = cmd.substring(12).toInt();
        wifi_power_t wp = (wifi_power_t)p;
        WiFi.setTxPower(wp);
        Serial.printf("[OK] Wi-Fi TX Power set to raw: %d\n", p);
    } else if (cmd.startsWith("SET PS ")) {
        int ps = cmd.substring(7).toInt();
        if (ps == 0) esp_wifi_set_ps(WIFI_PS_NONE);
        else esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
        Serial.printf("[OK] Wi-Fi Power Save: %s\n", ps == 0 ? "NONE" : "MIN_MODEM");
    } else if (cmd.startsWith("SET LIMITER ")) {
        enable_limiter = (cmd.substring(12).toInt() != 0);
        Serial.printf("[OK] Limiter: %s\n", enable_limiter ? "ENABLED" : "DISABLED");
    } else if (cmd.startsWith("SET SHIFT ")) {
        current_shift = cmd.substring(10).toInt();
        if (current_shift < 8) current_shift = 8;
        if (current_shift > 24) current_shift = 24;
        Serial.printf("[OK] Shift set to >> %d\n", current_shift);
    } else if (cmd.startsWith("SET GAIN ")) {
        current_gain = cmd.substring(9).toFloat();
        limiter.set_gain(current_gain);
        Serial.printf("[OK] Gain set to %.2f\n", current_gain);
    } else if (cmd.startsWith("SET FILTER ")) {
        enable_dc_filter = (cmd.substring(11).toInt() != 0);
        Serial.printf("[OK] DC Filter: %s\n", enable_dc_filter ? "ENABLED" : "DISABLED");
    } else if (cmd.startsWith("SET PULL ")) {
        String p = cmd.substring(9);
        p.toUpperCase();
        if (p == "DOWN") current_pull = 1;
        else if (p == "UP") current_pull = 2;
        else current_pull = 0;
        apply_pin_pull();
        Serial.printf("[OK] Pull set to %s\n", current_pull == 1 ? "PULLDOWN" : (current_pull == 2 ? "PULLUP" : "FLOAT"));
    } else if (cmd.startsWith("SET CHANNEL ")) {
        String c = cmd.substring(12);
        c.toUpperCase();
        if (c == "RIGHT") current_channel = I2S_CHANNEL_FMT_ONLY_RIGHT;
        else current_channel = I2S_CHANNEL_FMT_ONLY_LEFT;
        init_i2s();
        Serial.printf("[OK] Channel set to %s\n", current_channel == I2S_CHANNEL_FMT_ONLY_LEFT ? "LEFT" : "RIGHT");
    } else if (cmd == "HELP") {
        Serial.println("\nCommands: STREAM, STOP, DUMP, STATS, SET LIMITER <0|1>, SET WIFI <0|1>, SET LED <0|1>, SET SHIFT <11|12|16>, SET GAIN <f>, SET FILTER <0|1>, SET PULL <FLOAT|DOWN|UP>, SET CHANNEL <LEFT|RIGHT>");
    } else {
        Serial.println("[ERR] Unknown command. Type HELP");
    }
}

void setup() {
    Serial.begin(115200);
    delay(1500);

    FastLED.addLeds<WS2812, LED_BOARD_PIN, GRB>(leds, NUM_LEDS);
    leds[0] = CRGB::Black;
    FastLED.show();

    limiter.init(SAMPLE_RATE, 2.0f, 200.0f, 26000.0f, current_gain);

    for (size_t i = 0; i < sizeof(dummy_tx_payload); i++) {
        dummy_tx_payload[i] = (uint8_t)(i & 0xFF);
    }

    WiFi.mode(WIFI_OFF);

    init_i2s();

    Serial.println("\n==========================================");
    Serial.println("  ESP32-S3 INMP441 AUDIO DIAGNOSTIC TOOL  ");
    Serial.println("==========================================");
    Serial.println("Pins: BCLK=3, WS=8, DIN=10, LED=48");
    Serial.println("Limiter: WebRTC Peak Limiter (Attack 2ms, Release 200ms, Thres 26000)");
    Serial.println("Ready. Type HELP for commands.");
}

uint8_t packet_buffer[520];

void loop() {
    if (Serial.available()) {
        String cmd = Serial.readStringUntil('\n');
        process_serial_command(cmd);
    }

    update_led();
    update_wifi_tx();

    if (is_streaming) {
        size_t bytes_read = 0;
        i2s_read(I2S_NUM_0, raw_buffer_32, sizeof(raw_buffer_32), &bytes_read, portMAX_DELAY);
        int n = bytes_read / 4;

        for (int i = 0; i < n; i++) {
            float x = (float)(raw_buffer_32[i] >> current_shift);
            float y = x;
            if (enable_dc_filter) {
                y = x - dc_x1 + R * dc_y1;
                dc_x1 = x;
                dc_y1 = y;
            }

            if (enable_limiter) {
                processed_buffer_16[i] = limiter.process(y);
            } else {
                float v = y * current_gain;
                if (v > 32767.0f) v = 32767.0f;
                if (v < -32768.0f) v = -32768.0f;
                processed_buffer_16[i] = (int16_t)roundf(v);
            }
        }

        packet_buffer[0] = 0xAA;
        packet_buffer[1] = 0x55;
        packet_buffer[2] = (uint8_t)(packet_seq & 0xFF);
        packet_buffer[3] = (uint8_t)((packet_seq >> 8) & 0xFF);
        uint16_t samples_count = (uint16_t)n;
        packet_buffer[4] = (uint8_t)(samples_count & 0xFF);
        packet_buffer[5] = (uint8_t)((samples_count >> 8) & 0xFF);
        memcpy(&packet_buffer[6], processed_buffer_16, n * 2);
        packet_buffer[6 + n * 2] = 0x55;
        packet_buffer[7 + n * 2] = 0xAA;

        Serial.write(packet_buffer, 8 + n * 2);
        packet_seq++;
    } else {
        delay(10);
    }
}
