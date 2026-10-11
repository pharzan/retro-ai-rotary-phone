#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include "driver/i2s.h"

// ==========================================
// CONFIGURATION: Connected Wi-Fi
// ==========================================
const char* WIFI_SSID     = "Zyxel_1286";
const char* WIFI_PASSWORD = "MMMKLUNL7J";

// Network Ports
#define AUDIO_UDP_PORT    5005
#define CTRL_UDP_PORT     5006

// Pins
#define HOOK_PIN          1     // Analog hook on GPIO 1 (ADC1 Ch0)
#define BUZZER_PIN        16    // Buzzer on GPIO 16

// MAX98357A I2S DAC pins
#define I2S_BCLK_PIN      5
#define I2S_LRC_PIN       6
#define I2S_DOUT_PIN      7
#define I2S_NUM           I2S_NUM_0

#define SAMPLE_RATE       8000
#define PACKET_BUF_SIZE   1024

// Hook switch calibration:
// UP (Picked up)   = ADC ~204 (Min 186, Max 211)
// DOWN (On cradle) = ADC ~646 (Min 636, Max 652)
// Midpoint threshold is 425 (Halfway between 204 and 646)
// With hysteresis:
// Lifted (UP) when ADC < 350
// Hung up (DOWN) when ADC > 500
#define HOOK_THRESHOLD_UP   350
#define HOOK_THRESHOLD_DOWN 500

WiFiUDP audio_udp;
WiFiUDP ctrl_udp;

static uint8_t udp_packet_buf[PACKET_BUF_SIZE];
static int16_t stereo_buf[PACKET_BUF_SIZE];

static IPAddress last_controller_ip;
static uint16_t last_controller_port = 0;

static volatile bool is_ringing = false;
static uint32_t ring_cycle_timer = 0;
static bool buzzer_sound_active = false;
static bool stable_picked_up = false;

static void set_buzzer_state(bool on) {
    if (on) {
        // Generate audible pulse on GPIO 16 (works for passive tone or active buzzers)
        for (int i = 0; i < 40; i++) {
            digitalWrite(BUZZER_PIN, HIGH);
            delayMicroseconds(500);
            digitalWrite(BUZZER_PIN, LOW);
            delayMicroseconds(500);
        }
        digitalWrite(BUZZER_PIN, HIGH);
    } else {
        digitalWrite(BUZZER_PIN, LOW);
    }
    buzzer_sound_active = on;
}

static void init_i2s_output() {
    i2s_config_t i2s_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
        .sample_rate = SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 8,
        .dma_buf_len = 128,
        .use_apll = false,
        .tx_desc_auto_clear = true,
        .fixed_mclk = 0
    };

    i2s_pin_config_t pin_config = {
        .bck_io_num = I2S_BCLK_PIN,
        .ws_io_num = I2S_LRC_PIN,
        .data_out_num = I2S_DOUT_PIN,
        .data_in_num = I2S_PIN_NO_CHANGE
    };

    ESP_ERROR_CHECK(i2s_driver_install(I2S_NUM, &i2s_config, 0, NULL));
    ESP_ERROR_CHECK(i2s_set_pin(I2S_NUM, &pin_config));
    ESP_ERROR_CHECK(i2s_zero_dma_buffer(I2S_NUM));
}

// Read smoothed ADC value
static int read_smoothed_adc() {
    long sum = 0;
    for (int i = 0; i < 16; i++) {
        sum += analogRead(HOOK_PIN);
        delayMicroseconds(100);
    }
    return sum / 16;
}

void setup() {
    Serial.begin(115200);
    delay(500);

    pinMode(HOOK_PIN, INPUT_PULLUP);
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);

    init_i2s_output();

    Serial.println("\n==============================================");
    Serial.println("  ESP32-S3 Rotary Phone AI Smart Assistant");
    Serial.println("==============================================");
    Serial.printf("Connecting to Wi-Fi \"%s\"...\n", WIFI_SSID);

    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    int retries = 0;
    while (WiFi.status() != WL_CONNECTED && retries < 30) {
        delay(500);
        Serial.print(".");
        retries++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\n[WiFi] Connected!");
        Serial.printf("[WiFi] IP Address: %s\n", WiFi.localIP().toString().c_str());
        Serial.printf("[WiFi] Audio: UDP %d | Control: UDP %d\n", AUDIO_UDP_PORT, CTRL_UDP_PORT);
        audio_udp.begin(AUDIO_UDP_PORT);
        ctrl_udp.begin(CTRL_UDP_PORT);
    } else {
        Serial.println("\n[WiFi] Failed to connect.");
    }

    // Initialize hook state
    int initial_adc = read_smoothed_adc();
    stable_picked_up = (initial_adc < HOOK_THRESHOLD_UP);
    Serial.printf("[INIT] Handset initial state: %s (ADC=%d)\n",
                  stable_picked_up ? "OFF-HOOK (PICKED UP)" : "ON-HOOK (ON CRADLE)",
                  initial_adc);
}

void loop() {
    uint32_t now = millis();

    // 1. Debounced Hook Switch Check with Hysteresis (every 50ms)
    static uint32_t last_hook_check = 0;
    if (now - last_hook_check >= 50) {
        last_hook_check = now;
        int adc = read_smoothed_adc();

        bool new_state = stable_picked_up;
        if (adc < HOOK_THRESHOLD_UP) {
            new_state = true;  // Picked up (UP ~204)
        } else if (adc > HOOK_THRESHOLD_DOWN) {
            new_state = false; // On cradle (DOWN ~646)
        }

        if (new_state != stable_picked_up) {
            stable_picked_up = new_state;

            Serial.printf("[HOOK] Handset %s (ADC=%d)\n",
                          stable_picked_up ? "OFF-HOOK (PICKED UP!)" : "ON-HOOK (ON CRADLE)",
                          adc);

            // If user picks up while ringing, silence ringer immediately
            if (stable_picked_up && is_ringing) {
                is_ringing = false;
                set_buzzer_state(false);
                Serial.println("[RINGER] User answered call! Buzzer silenced.");
            }

            // Notify PC controller over UDP
            if (last_controller_port != 0) {
                ctrl_udp.beginPacket(last_controller_ip, last_controller_port);
                if (stable_picked_up) {
                    ctrl_udp.write((const uint8_t*)"HOOK:OFF\n", 9);
                } else {
                    ctrl_udp.write((const uint8_t*)"HOOK:ON\n", 8);
                }
                ctrl_udp.endPacket();
            }
        }
    }

    // 2. Telephone Ring Cadence (Classic: 0.4s ON, 0.2s OFF, 0.4s ON, 2.0s OFF)
    if (is_ringing) {
        if (stable_picked_up) {
            is_ringing = false;
            set_buzzer_state(false);
        } else {
            uint32_t elapsed = (now - ring_cycle_timer) % 3000;
            if (elapsed < 400) {
                set_buzzer_state(true);
            } else if (elapsed < 600) {
                set_buzzer_state(false);
            } else if (elapsed < 1000) {
                set_buzzer_state(true);
            } else {
                set_buzzer_state(false);
            }
        }
    } else if (buzzer_sound_active) {
        set_buzzer_state(false);
    }

    // 3. Process Control Packets from PC
    int ctrl_len = ctrl_udp.parsePacket();
    if (ctrl_len > 0) {
        last_controller_ip = ctrl_udp.remoteIP();
        last_controller_port = ctrl_udp.remotePort();

        char cmd_buf[64];
        int bytes_read = ctrl_udp.read(cmd_buf, sizeof(cmd_buf) - 1);
        if (bytes_read > 0) {
            cmd_buf[bytes_read] = '\0';
            String cmd = String(cmd_buf);
            cmd.trim();

            if (cmd == "RING:START") {
                if (!stable_picked_up) {
                    is_ringing = true;
                    ring_cycle_timer = now;
                    Serial.println("[RINGER] Ring signal received! Buzzer started.");
                }
                ctrl_udp.beginPacket(last_controller_ip, last_controller_port);
                ctrl_udp.printf("STATE:%s\n", (stable_picked_up ? "OFF_HOOK" : "ON_HOOK"));
                ctrl_udp.endPacket();
            } else if (cmd == "RING:STOP") {
                is_ringing = false;
                set_buzzer_state(false);
                Serial.println("[RINGER] Ring stopped.");
            } else if (cmd == "BUZZ:TEST") {
                set_buzzer_state(true);
                delay(300);
                set_buzzer_state(false);
                ctrl_udp.beginPacket(last_controller_ip, last_controller_port);
                ctrl_udp.write((const uint8_t*)"BUZZ:OK\n", 8);
                ctrl_udp.endPacket();
            } else if (cmd == "STATUS?") {
                ctrl_udp.beginPacket(last_controller_ip, last_controller_port);
                ctrl_udp.printf("STATE:%s\n", (stable_picked_up ? "OFF_HOOK" : "ON_HOOK"));
                ctrl_udp.endPacket();
            }
        }
    }

    // 4. Play audio packets from PC to MAX98357A I2S (only if handset is lifted!)
    int packet_size = audio_udp.parsePacket();
    if (packet_size > 0) {
        int len = audio_udp.read((char*)udp_packet_buf, sizeof(udp_packet_buf));
        
        // If user hung up, discard audio packets and clear I2S DMA buffer immediately
        if (!stable_picked_up) {
            i2s_zero_dma_buffer(I2S_NUM);
            return;
        }

        int num_samples = len / sizeof(int16_t);
        int16_t* mono_samples = (int16_t*)udp_packet_buf;

        for (int i = 0; i < num_samples; i++) {
            stereo_buf[i * 2]     = mono_samples[i];
            stereo_buf[i * 2 + 1] = mono_samples[i];
        }

        size_t bytes_written = 0;
        i2s_write(I2S_NUM, stereo_buf, num_samples * sizeof(int16_t) * 2, &bytes_written, portMAX_DELAY);
    }
}

