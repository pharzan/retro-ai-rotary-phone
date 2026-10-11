#!/usr/bin/env python3
"""
Interactive Hardware Debugger & State Tester for ESP32-S3 Rotary Phone

All-in-one menu tool:
  [1] Live Cradle Monitor (GPIO 1 ADC & Hook Transitions)
  [2] Test Buzzer (Short 300ms beep over UDP)
  [3] Start Ringing Cadence (Buzzer dual-ring pattern)
  [4] Stop Ringing Cadence
  [5] Play Test Audio Tone (425Hz dial tone in earpiece)
  [6] Play Custom Audio File through earpiece
  [7] Test Full Flow with MOCK AI (Instant question -> Ring -> Pickup -> Speak -> Hangup cutoff)
  [q] Quit
"""

import sys
import os
import time
import socket
import select
import subprocess
import threading
import numpy as np

# Network & Audio Config
ESP_IP = "192.168.1.32"
AUDIO_PORT = 5005
CTRL_PORT = 5006
SAMPLE_RATE = 8000
CHUNK_SAMPLES = 256
BYTES_PER_SAMPLE = 2
CHUNK_BYTES = CHUNK_SAMPLES * BYTES_PER_SAMPLE
DEFAULT_TEST_AUDIO = "/home/pharzan/Projects/rotary-phone-assistant/audio/test_speech.pcm"


def generate_dial_tone(duration_s: float = 3.0, freq: float = 425.0) -> bytes:
    """Generate 8000Hz 16-bit mono sine wave."""
    total_samples = int(SAMPLE_RATE * duration_s)
    t = np.arange(total_samples) / SAMPLE_RATE
    signal = 0.4 * np.sin(2 * np.pi * freq * t)
    pcm = (signal * 32767).astype(np.int16)
    return pcm.tobytes()


def convert_audio_to_pcm(input_path: str) -> bytes:
    """Convert any audio file to 8000 Hz 16-bit Mono raw PCM."""
    if input_path.endswith(".pcm"):
        with open(input_path, "rb") as f:
            return f.read()

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", input_path,
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-f", "s16le",
        "-"
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return proc.stdout


def send_audio_stream(raw_pcm: bytes, ctrl_sock: socket.socket = None, esp_ip: str = ESP_IP, audio_port: int = AUDIO_PORT) -> bool:
    """Streams PCM chunks over UDP to the earpiece. Checks for hangup if ctrl_sock provided."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    chunk_delay = (CHUNK_SAMPLES / SAMPLE_RATE) * 0.95
    total_samples = len(raw_pcm) // BYTES_PER_SAMPLE
    duration_s = total_samples / SAMPLE_RATE

    print(f"\n[Audio] Streaming {duration_s:.1f}s audio to earpiece...")
    offset = 0
    hung_up = False

    try:
        while offset < len(raw_pcm):
            # Check hangup on ctrl_sock
            if ctrl_sock:
                try:
                    ctrl_sock.settimeout(0.001)
                    msg, _ = ctrl_sock.recvfrom(128)
                    if b"HOOK:ON" in msg or b"STATE:ON_HOOK" in msg:
                        print("\n[CALL TERMINATED] Handset hung up on cradle! Audio cut off immediately.")
                        hung_up = True
                        break
                except (socket.timeout, BlockingIOError):
                    pass

            chunk = raw_pcm[offset:offset + CHUNK_BYTES]
            sock.sendto(chunk, (esp_ip, audio_port))
            offset += len(chunk)
            time.sleep(chunk_delay)

        if not hung_up:
            print("\n[Audio] Playback finished.")
    except KeyboardInterrupt:
        print("\n[Audio] Playback stopped by user.")
    finally:
        sock.close()

    return not hung_up


def test_hook_monitor(esp_ip: str = ESP_IP, ctrl_port: int = CTRL_PORT):
    """Monitors live hook status from the ESP32."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    sock.settimeout(1.0)

    print("\n" + "=" * 60)
    print(" LIVE HOOK MONITOR (GPIO 1)")
    print("=" * 60)
    print("Lift handset up and place down on cradle to observe events.")
    print("Press Enter or Ctrl+C to return to main menu.\n")

    # Request initial status
    sock.sendto(b"STATUS?\n", (esp_ip, ctrl_port))

    stop_event = threading.Event()

    def wait_for_enter():
        sys.stdin.readline()
        stop_event.set()

    t = threading.Thread(target=wait_for_enter, daemon=True)
    t.start()

    try:
        last_poll = time.time()
        while not stop_event.is_set():
            if time.time() - last_poll > 2.0:
                sock.sendto(b"STATUS?\n", (esp_ip, ctrl_port))
                last_poll = time.time()

            try:
                data, _ = sock.recvfrom(128)
                msg = data.decode("utf-8", errors="ignore").strip()
                if "HOOK:OFF" in msg:
                    print("  --> [EVENT] Handset LIFTED (Off-Hook / User Answered)")
                elif "HOOK:ON" in msg:
                    print("  --> [EVENT] Handset PLACED ON CRADLE (On-Hook / Hung Up)")
                elif "STATE:" in msg:
                    state_str = "OFF-HOOK (Picked Up)" if "OFF_HOOK" in msg else "ON-HOOK (On Cradle)"
                    print(f"  [Status Report] Phone is currently: {state_str}")
                else:
                    print(f"  [ESP32] {msg}")
            except socket.timeout:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        print("\nStopped hook monitor.")


def send_ctrl_cmd(cmd: str, esp_ip: str = ESP_IP, ctrl_port: int = CTRL_PORT) -> str:
    """Send single control command and return reply if any."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    sock.settimeout(0.5)
    try:
        sock.sendto(cmd.encode("utf-8") + b"\n", (esp_ip, ctrl_port))
        data, _ = sock.recvfrom(128)
        return data.decode("utf-8", errors="ignore").strip()
    except socket.timeout:
        return "(no reply)"
    finally:
        sock.close()


def test_mock_flow(esp_ip: str = ESP_IP, ctrl_port: int = CTRL_PORT, audio_port: int = AUDIO_PORT):
    """Test full pipeline using pre-generated test audio without waiting for LLM or TTS."""
    print("\n" + "=" * 60)
    print(" FULL FLOW TEST WITH DEFAULT TEST AUDIO (Instant)")
    print("=" * 60)
    print("Flow:")
    print(" 1. Loads pre-generated operator test audio.")
    print(" 2. Ringer starts (buzzer on GPIO 16).")
    print(" 3. When you PICK UP handset -> Buzzer stops and audio speaks.")
    print(" 4. When you HANG UP -> Audio cuts off immediately.")
    print("=" * 60)

    # Load default audio directly
    if os.path.exists(DEFAULT_TEST_AUDIO):
        print(f"[Audio] Using default test audio: {DEFAULT_TEST_AUDIO}")
        with open(DEFAULT_TEST_AUDIO, "rb") as f:
            pcm_bytes = f.read()
    else:
        print("[Audio] Pre-generating default test audio...")
        mock_text = (
            "Number please. This is an automated test from your vintage telephone operator. "
            "All circuits, buzzer ringer, and earpiece audio are operating properly. "
            "When you hang up the handset, this playback will terminate immediately."
        )
        mp3_tmp = "/tmp/mock_speech.mp3"
        subprocess.run(["edge-tts", "--text", mock_text, "--voice", "en-US-JennyNeural", "--write-media", mp3_tmp], check=True)
        pcm_bytes = convert_audio_to_pcm(mp3_tmp)
        with open(DEFAULT_TEST_AUDIO, "wb") as f:
            f.write(pcm_bytes)
        if os.path.exists(mp3_tmp):
            os.remove(mp3_tmp)

    ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctrl_sock.bind(("", 0))

    try:
        print("\n[Ringer] Ringing phone! Pick up handset to answer...")
        ctrl_sock.sendto(b"RING:START\n", (esp_ip, ctrl_port))

        answered = False
        t_start = time.time()
        while time.time() - t_start < 45.0:
            try:
                ctrl_sock.settimeout(0.5)
                data, _ = ctrl_sock.recvfrom(128)
                msg = data.decode("utf-8", errors="ignore").strip()
                if "HOOK:OFF" in msg or "STATE:OFF_HOOK" in msg:
                    answered = True
                    break
            except socket.timeout:
                ctrl_sock.sendto(b"STATUS?\n", (esp_ip, ctrl_port))

        if answered:
            print("\n[CALL CONNECTED] Handset lifted! Stopping buzzer and playing mock answer...")
            ctrl_sock.sendto(b"RING:STOP\n", (esp_ip, ctrl_port))
            time.sleep(0.3)
            send_audio_stream(pcm_bytes, ctrl_sock=ctrl_sock, esp_ip=esp_ip, audio_port=audio_port)
        else:
            print("\n[TIMEOUT] Call was not answered.")
            ctrl_sock.sendto(b"RING:STOP\n", (esp_ip, ctrl_port))

    except KeyboardInterrupt:
        print("\nTest cancelled by user.")
        ctrl_sock.sendto(b"RING:STOP\n", (esp_ip, ctrl_port))
    finally:
        ctrl_sock.close()


def print_menu():
    print("\n" + "=" * 55)
    print("       ROTARY PHONE HARDWARE & STATE DEBUGGER")
    print("=" * 55)
    print("  [1] Live Cradle Monitor (GPIO 1 Hook & Transitions)")
    print("  [2] Test Buzzer (Short 300ms beep)")
    print("  [3] Start Ringing Cadence (Buzzer continuous)")
    print("  [4] Stop Ringing Cadence")
    print("  [5] Test Earpiece Tone (425 Hz Dial Tone, 3s)")
    print("  [6] Stream Custom Audio File to Earpiece")
    print("  [7] TEST FULL FLOW (Mock AI -> Ring -> Pickup -> Cutoff)")
    print("  [q] Quit")
    print("=" * 55)


def main():
    while True:
        print_menu()
        choice = input("Select an option [1-7, q]: ").strip().lower()

        if choice == "1":
            test_hook_monitor()
        elif choice == "2":
            print("Testing buzzer pulse...")
            reply = send_ctrl_cmd("BUZZ:TEST")
            print(f"Buzzer test sent. Response: {reply}")
        elif choice == "3":
            print("Starting ringing cadence...")
            reply = send_ctrl_cmd("RING:START")
            print(f"Ring started. Current phone state: {reply}")
        elif choice == "4":
            print("Stopping ringing cadence...")
            reply = send_ctrl_cmd("RING:STOP")
            print("Ring stopped.")
        elif choice == "5":
            print("Generating 425Hz dial tone...")
            tone_pcm = generate_dial_tone(duration_s=3.5, freq=425.0)
            print("Make sure handset is off cradle to hear the tone!")
            send_audio_stream(tone_pcm)
        elif choice == "6":
            filepath = input(f"Enter path to audio file [press Enter for default '{os.path.basename(DEFAULT_TEST_AUDIO)}']: ").strip()
            if not filepath:
                filepath = DEFAULT_TEST_AUDIO

            if os.path.exists(filepath):
                try:
                    pcm = convert_audio_to_pcm(filepath)
                    print("Make sure handset is off cradle to hear audio!")
                    send_audio_stream(pcm)
                except Exception as e:
                    print(f"Error converting audio: {e}")
            else:
                print(f"File '{filepath}' not found.")
        elif choice == "7":
            test_mock_flow()
        elif choice == "q":
            print("Exiting debugger. Goodbye!")
            break
        else:
            print("Invalid choice, please select 1-7 or q.")


if __name__ == "__main__":
    main()
