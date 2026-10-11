# Rotary Telephone AI Assistant - Audio Loopback Test

This project contains the ESP32-S3 firmware and a Python diagnostic script to test and verify the microphone, earpiece, and hook switch on your vintage Elektrisk Bureau rotary telephone.

---

## 1. Hardware Pinout Reference

| Component | Signal | ESP32-S3 Pin | Notes |
| :--- | :--- | :--- | :--- |
| **MAX98357A I2S DAC** | BCLK | **GPIO 5** | Bit Clock |
| | LRC / WS | **GPIO 6** | Left/Right Clock (8 kHz) |
| | DIN | **GPIO 7** | Serial Audio Data |
| | VIN / GND | **5V / GND** | Standard power connection |
| **Carbon Microphone** | Bias Output | **GPIO 4** | ADC1 Channel 3 (DC offset removed via EMA filter) |
| | Bias Resistor | 330Ω to 3.3V | Other lead of mic to GND |
| **Hook Switch** | Leaf Switch | **GPIO 15** | Connected between GPIO 15 and GND (`INPUT_PULLUP`) |

---

## 2. Firmware (ESP32-S3)

Located in `firmware/`:
- **ADC Sampling**: Samples GPIO 4 at 8000 Hz using high-resolution `esp_timer`.
- **DC Offset Removal**: Tracks dynamic bias voltage with an exponential running average filter ($raw - DC$) and scales with gain.
- **I2S Output**: Direct ESP-IDF `driver/i2s.h` stereo stream to the MAX98357A.
- **USB CDC Serial**: Communicates audio bytes and hook switch state changes at **921600 baud**.

### Flashing the Firmware

```bash
cd /home/pharzan/Projects/rotary-phone-assistant/firmware
pio run -t upload
```

*(Or open `/home/pharzan/Projects/rotary-phone-assistant/firmware` in VS Code / PlatformIO / Arduino IDE).*

---

## 3. Python Loopback Script

Located in `python/loopback_test.py`.

A virtual environment is set up at `/home/pharzan/Projects/rotary-phone-assistant/.venv` with `pyserial` and `numpy`.

### Running Modes

1. **Full USB Audio Loopback (Mic -> Host -> Earpiece)**:
   ```bash
   /home/pharzan/Projects/rotary-phone-assistant/.venv/bin/python /home/pharzan/Projects/rotary-phone-assistant/python/loopback_test.py --mode loopback
   ```
   *Speak into the transmitter: you will hear your voice in the earpiece.*

2. **Earpiece Test Tone (Dial Tone)**:
   ```bash
   /home/pharzan/Projects/rotary-phone-assistant/.venv/bin/python /home/pharzan/Projects/rotary-phone-assistant/python/loopback_test.py --mode tone
   ```
   *Generates a 425 Hz continuous telephone dial tone and plays it through the earpiece.*

3. **Microphone VU Meter / Level Monitor**:
   ```bash
   /home/pharzan/Projects/rotary-phone-assistant/.venv/bin/python /home/pharzan/Projects/rotary-phone-assistant/python/loopback_test.py --mode monitor
   ```
   *Visualizes live RMS audio level bars on your terminal as you speak or tap the microphone.*
