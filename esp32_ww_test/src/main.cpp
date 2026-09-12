#include <Arduino.h>
#include <driver/i2s.h>
#include <FastLED.h>
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

// --- Настройки ---
#define I2S_MIC_BCLK 3
#define I2S_MIC_LRCLK 8
#define I2S_MIC_DIN 10

#define MIC_GAIN 2 // Усиление микрофона

#define LED_BOARD_PIN 48
#define NUM_LEDS 1
CRGB leds[NUM_LEDS];

// --- TFLite переменные ---
const tflite::Model* model = nullptr;
tflite::MicroInterpreter* interpreter = nullptr;
TfLiteTensor* input_tensor = nullptr;
TfLiteTensor* output_tensor = nullptr;

// Арена (32 КБ как указано в porfiriy.json)
constexpr int kTensorArenaSize = 32768;
uint8_t tensor_arena[kTensorArenaSize];
tflite::MicroErrorReporter micro_error_reporter;
tflite::ErrorReporter* error_reporter = &micro_error_reporter;

// --- Frontend (Извлечение признаков) ---
struct FrontendConfig frontend_config;
struct FrontendState frontend_state;

// Параметры из ESPHome (micro_wake_word)
#define SAMPLE_RATE 16000
#define PREPROCESSOR_FEATURE_SIZE 40
#define FEATURE_DURATION_MS 30
#define FEATURE_STEP_SIZE_MS 10 // According to porfiriy.json

// Кольцевой буфер признаков для TFLite
int num_slices = 0; 
int8_t* feature_ring_buffer = nullptr;
int feature_buffer_index = 0;

void setup_tflite() {
    Serial.println("Initializing TFLite...");
    model = tflite::GetModel(porfiriy_tflite);
    if (model->version() != TFLITE_SCHEMA_VERSION) {
        Serial.println("Model schema version mismatch!");
        return;
    }

    static tflite::AllOpsResolver resolver;
    
    tflite::MicroAllocator* allocator = tflite::MicroAllocator::Create(tensor_arena, kTensorArenaSize, error_reporter);
    tflite::MicroResourceVariables* resource_variables = tflite::MicroResourceVariables::Create(allocator, 10);
    
    static tflite::MicroInterpreter static_interpreter(
        model, resolver, allocator, error_reporter, resource_variables);
    interpreter = &static_interpreter;

    if (interpreter->AllocateTensors() != kTfLiteOk) {
        Serial.println("AllocateTensors() failed!");
        return;
    }

    input_tensor = interpreter->input(0);
    output_tensor = interpreter->output(0);

    Serial.println("TFLite initialized successfully.");
    
    // ВЫВОД ФОРМЫ ТЕНЗОРА ДЛЯ ОТЛАДКИ!
    Serial.print(">>> Input tensor dims: [");
    int num_dims = input_tensor->dims->size;
    for (int i = 0; i < num_dims; i++) {
        Serial.print(input_tensor->dims->data[i]);
        if (i < num_dims - 1) Serial.print(", ");
    }
    Serial.println("]");
    
    Serial.print(">>> Input tensor type: ");
    Serial.println(input_tensor->type);
    Serial.print(">>> Output tensor type: ");
    Serial.println(output_tensor->type);
    
    // Сохраняем размер окна (num_slices)
    if (num_dims >= 3) {
        // Формат обычно [1, windows, 40, 1]
        num_slices = input_tensor->dims->data[1];
    } else {
        num_slices = 99; // Fallback
    }
    
    feature_ring_buffer = new int8_t[num_slices * PREPROCESSOR_FEATURE_SIZE];
    memset(feature_ring_buffer, 0, num_slices * PREPROCESSOR_FEATURE_SIZE);
}

void setup_frontend() {
    FrontendFillConfigWithDefaults(&frontend_config);
    // Базовые настройки из ESPHome
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
    
    FrontendPopulateState(&frontend_config, &frontend_state, SAMPLE_RATE);
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
        .dma_buf_len = 512,
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
}

void setup() {
    Serial.begin(115200);
    delay(3000);
    Serial.println("--- Starting Wake Word Test Firmware ---");
    
    // FastLED
    FastLED.addLeds<WS2812B, 48, GRB>(leds, 1);
    leds[0] = CRGB::Black;
    FastLED.show();

    setup_tflite();
    setup_frontend();
    setup_i2s();
    
    Serial.println("Wake Word Debugger started. Say 'Porfiriy'!");
}

int32_t mic_buffer_32[320];
int16_t mic_buffer_16[320];

void loop() {
    size_t bytes_read = 0;
    // Читаем 20ms аудио (320 сэмплов)
    i2s_read(I2S_NUM_0, mic_buffer_32, sizeof(mic_buffer_32), &bytes_read, portMAX_DELAY);
    int samples_read = bytes_read / 4;
    
    // Перевод в 16 бит с усилением
    for (int i = 0; i < samples_read; i++) {
        int32_t sample = (mic_buffer_32[i] >> 16) * MIC_GAIN;
        if (sample > 32767) sample = 32767;
        if (sample < -32768) sample = -32768;
        mic_buffer_16[i] = sample;
    }
    
    static int print_counter = 0;
    if (print_counter++ % 50 == 0) {
        Serial.printf("Raw I2S sample[0]: %d (16-bit)\n", mic_buffer_16[0]);
    }

    size_t num_samples_read = 0;
    struct FrontendOutput frontend_output = FrontendProcessSamples(
        &frontend_state, mic_buffer_16, samples_read, &num_samples_read);

    if (frontend_output.size > 0) {
        int slices_produced = frontend_output.size / PREPROCESSOR_FEATURE_SIZE;
        for (int s = 0; s < slices_produced; s++) {
            // Нормализация (ESPHome microWakeWord style)
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
        
        // Копируем кольцевой буфер в тензор (линейно)
        for (int i = 0; i < num_slices; i++) {
            int ring_idx = (feature_buffer_index + i) % num_slices;
            memcpy(
                input_tensor->data.int8 + (i * PREPROCESSOR_FEATURE_SIZE),
                feature_ring_buffer + (ring_idx * PREPROCESSOR_FEATURE_SIZE),
                PREPROCESSOR_FEATURE_SIZE
            );
        }
        
        // Запуск нейросети
        if (interpreter->Invoke() == kTfLiteOk) {
            float prob = 0.0;
            if (output_tensor->type == kTfLiteFloat32) {
                prob = output_tensor->data.f[0];
            } else if (output_tensor->type == kTfLiteInt8) {
                prob = (output_tensor->data.int8[0] - output_tensor->params.zero_point) * output_tensor->params.scale;
            } else if (output_tensor->type == kTfLiteUInt8) {
                prob = (output_tensor->data.uint8[0] - output_tensor->params.zero_point) * output_tensor->params.scale;
            }
            
            // Вывод раз в ~100 мс для отладки
            if (print_counter % 5 == 0) {
                Serial.printf("Prob: %.3f\n", prob);
            }
            
            if (prob > 0.94) {
                Serial.println(">>> WAKE WORD DETECTED! <<<");
                leds[0] = CRGB::Blue;
                FastLED.show();
                delay(1000); // Держим диод
                leds[0] = CRGB::Black;
                FastLED.show();
            }
        }
    }
}
