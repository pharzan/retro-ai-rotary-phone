#!/usr/bin/env python3
"""
Rotary Phone AI Assistant - Ring & Answer Pipeline

Interaction Flow:
1. User enters question via CLI.
2. `agy` generates answer and synthesizes audio via neural TTS.
3. Python tells ESP32 to ring the buzzer (`RING:START` over UDP port 5006).
4. ESP32 rings in vintage dual-ring cadence until user lifts handset off cradle (GPIO 15 LOW).
5. As soon as user picks up:
   - Buzzer immediately silences.
   - ESP32 signals pickup (`HOOK:OFF`).
   - Python streams the speech audio to the earpiece via UDP port 5005.
"""

import sys
import os
import time
import argparse
import asyncio
import subprocess
import socket
import edge_tts
import numpy as np

SAMPLE_RATE = 8000
CHUNK_SAMPLES = 256  # 512 bytes per packet
BYTES_PER_SAMPLE = 2
CHUNK_BYTES = CHUNK_SAMPLES * BYTES_PER_SAMPLE


def ask_ai(prompt: str) -> str:
    """Query high-speed Gemini API directly (~1.5-2.5s) using local gcloud auth, with fallback to agy."""
    print(f"\n[AI] Thinking: \"{prompt}\"...")
    system_instruction = (
        "You are a friendly, conversational vintage telephone operator. "
        "Speak naturally as if talking to a friend on a real phone call. "
        "Use natural contractions (like I'm, that's, you'll) and conversational phrasing. "
        "Keep your response concise, spoken-style, and strictly under 2 sentences. "
        "Never output markdown, bullet points, asterisks, or code."
    )

    # 1. High-speed direct API path (1.5 - 2.5 seconds total latency)
    try:
        token_proc = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=4
        )
        token = token_proc.stdout.strip()
        if token.startswith("ya29."):
            import urllib.request
            import json

            url = "https://us-central1-aiplatform.googleapis.com/v1/projects/pharzan/locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent"
            payload = {
                "contents": [
                    {"role": "user", "parts": [{"text": f"{system_instruction}\nCaller asks: {prompt}"}]}
                ]
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                text = text.replace("*", "").replace("#", "").strip()
                print(f"[AI Response]:\n{text}\n")
                return text
    except Exception as e:
        print(f"[Info] Direct API call bypassed ({e}), falling back to agy CLI...")

    # 2. Fallback to local agy CLI
    try:
        full_prompt = f"{system_instruction}\nCaller asks: {prompt}"
        cmd = [
            "agy",
            "--disable-slash-commands",
            "--model", "gemini-3.8-flash-low",
            "--effort", "low",
            f"-p={full_prompt}"
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        text = res.stdout.strip()
        text = text.replace("*", "").replace("#", "").strip()
        print(f"[AI Response]:\n{text}\n")
        return text
    except Exception as e:
        print(f"[WARNING] AI query failed ({e}).")
        return "Number please. I am sorry, the operator line is currently busy. Please try your question again."


async def synthesize_speech(text: str, output_wav: str):
    """Use Edge-TTS with conversational neural voice and warm telephone acoustic filter."""
    print("[TTS] Generating neural conversational speech...")
    voice = "en-US-AndrewNeural"
    mp3_tmp = "/tmp/phone_tts_temp.mp3"

    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(mp3_tmp)

    # Use soxr high-quality sinc resampler with gentle telephone bandpass (300Hz-3400Hz)
    # to eliminate harsh digital aliasing and give warm natural analog vocal presence
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", mp3_tmp,
        "-af", "aresample=resampler=soxr,highpass=f=200,lowpass=f=3600",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-f", "s16le",
        output_wav
    ]
    subprocess.run(cmd, check=True)
    if os.path.exists(mp3_tmp):
        os.remove(mp3_tmp)
    print(f"[TTS] Synthesized speech ready at {output_wav}")


def ring_and_wait_for_pickup(sock: socket.socket, esp_ip: str, ctrl_port: int = 5006, timeout: float = 60.0) -> bool:
    """Send ring command to ESP32 buzzer and wait for user to lift the handset."""
    print("\n" + "=" * 65)
    print(" >>> INCOMING CALL: Ringing phone buzzer! <<<")
    print(" >>> Pick up the handset to hear your answer... <<<")
    print("=" * 65)

    # Start ringer on ESP32
    sock.sendto(b"RING:START\n", (esp_ip, ctrl_port))

    t_start = time.time()
    picked_up = False

    try:
        while time.time() - t_start < timeout:
            try:
                sock.settimeout(0.5)
                data, addr = sock.recvfrom(128)
                msg = data.decode("utf-8", errors="ignore").strip()

                if "HOOK:OFF" in msg or "STATE:OFF_HOOK" in msg:
                    picked_up = True
                    break
            except socket.timeout:
                # Periodic keepalive ring signal in case first packet was dropped
                sock.sendto(b"STATUS?\n", (esp_ip, ctrl_port))

        if picked_up:
            print("\n[CALL CONNECTED] Handset lifted off cradle! Starting playback in 1 second...\n")
            # Ensure ringer is stopped
            sock.sendto(b"RING:STOP\n", (esp_ip, ctrl_port))
            time.sleep(1.0)  # 1.0s natural pause after pickup before speech begins
            return True
        else:
            print("\n[TIMEOUT] Nobody answered the telephone.")
            sock.sendto(b"RING:STOP\n", (esp_ip, ctrl_port))
            return False

    except KeyboardInterrupt:
        print("\nCall cancelled by user.")
        sock.sendto(b"RING:STOP\n", (esp_ip, ctrl_port))
        return False



def send_audio_network(esp_ip: str, audio_port: int, ctrl_sock: socket.socket, pcm_file: str, volume: float = 1.0):
    """Stream PCM audio over Wi-Fi UDP to MAX98357A on the ESP32 and stop immediately on hangup."""
    with open(pcm_file, "rb") as f:
        raw_pcm = f.read()

    if volume != 1.0:
        samples = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32)
        samples = np.clip(samples * volume, -32768, 32767).astype(np.int16)
        raw_pcm = samples.tobytes()

    total_samples = len(raw_pcm) // BYTES_PER_SAMPLE
    duration_s = total_samples / SAMPLE_RATE
    print(f"[Audio] Speaking through earpiece ({duration_s:.1f}s)...")
    print("        (Placing handset back on cradle will cut off call immediately)")

    audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    chunk_delay = (CHUNK_SAMPLES / SAMPLE_RATE) * 0.95

    offset = 0
    hung_up = False

    while offset < len(raw_pcm):
        # Check if user hung up (HOOK:ON received on ctrl socket)
        try:
            ctrl_sock.settimeout(0.001)
            msg, _ = ctrl_sock.recvfrom(128)
            if b"HOOK:ON" in msg or b"STATE:ON_HOOK" in msg:
                print("\n[CALL TERMINATED] Handset hung up on cradle! Cutting off audio.")
                hung_up = True
                break
        except (socket.timeout, BlockingIOError):
            pass

        packet = raw_pcm[offset:offset + CHUNK_BYTES]
        audio_sock.sendto(packet, (esp_ip, audio_port))
        offset += len(packet)
        time.sleep(chunk_delay)

    audio_sock.close()
    if not hung_up:
        print("\n[Audio] Playback complete! Playing telephone disconnect beeps until you hang up...")
        ctrl_sock.sendto(b"PLAY:DONE\n", (esp_ip, 5006))

        # Generate 425 Hz disconnect tone cadence: 0.5s tone, 0.5s pause
        t_tone = np.linspace(0, 0.5, int(SAMPLE_RATE * 0.5), endpoint=False)
        tone_samples = (np.sin(2 * np.pi * 425 * t_tone) * 12000).astype(np.int16)
        silence_samples = np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.int16)
        cycle_pcm = np.concatenate([tone_samples, silence_samples]).tobytes()

        tone_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            while True:
                # Stream one 1.0-second cycle of beep + silence in small chunks
                cycle_offset = 0
                cycle_hung_up = False
                while cycle_offset < len(cycle_pcm):
                    # Check if user placed handset on cradle
                    try:
                        ctrl_sock.settimeout(0.001)
                        msg, _ = ctrl_sock.recvfrom(128)
                        if b"HOOK:ON" in msg or b"STATE:ON_HOOK" in msg:
                            cycle_hung_up = True
                            break
                    except (socket.timeout, BlockingIOError):
                        pass

                    chunk = cycle_pcm[cycle_offset:cycle_offset + CHUNK_BYTES]
                    tone_sock.sendto(chunk, (esp_ip, audio_port))
                    cycle_offset += len(chunk)
                    time.sleep(chunk_delay)

                if cycle_hung_up:
                    print("[CALL ENDED] Handset placed back on cradle. Good bye!\n")
                    break

                # Poll hook status periodically in case event packet was lost
                try:
                    ctrl_sock.sendto(b"STATUS?\n", (esp_ip, 5006))
                    ctrl_sock.settimeout(0.05)
                    msg, _ = ctrl_sock.recvfrom(128)
                    if b"STATE:ON_HOOK" in msg:
                        print("[CALL ENDED] Handset placed back on cradle. Good bye!\n")
                        break
                except (socket.timeout, BlockingIOError):
                    pass
        except KeyboardInterrupt:
            print("\nExited.")
        finally:
            tone_sock.close()



def main():
    parser = argparse.ArgumentParser(description="Ask agy, ring rotary phone, and play response upon handset pickup")
    parser.add_argument("prompt", nargs="?", default="Tell me an interesting vintage telephone fact.", help="Question for agy")
    parser.add_argument("--ip", default="192.168.1.32", help="ESP32 IP address (default: 192.168.1.32)")
    parser.add_argument("--audio-port", type=int, default=5005, help="Audio UDP port (default: 5005)")
    parser.add_argument("--ctrl-port", type=int, default=5006, help="Control UDP port (default: 5006)")
    parser.add_argument("--volume", "-v", type=float, default=1.0, help="Volume multiplier (e.g. 1.0, 1.5)")
    parser.add_argument("--mock", action="store_true", help="Skip AI LLM query and use instant mock answer for testing")

    args = parser.parse_args()

    default_test_pcm = "/home/pharzan/Projects/rotary-phone-assistant/audio/test_speech.pcm"

    # 1. Ask agy (or mock)
    if args.mock:
        print("[MOCK] Using instant mock operator response (zero latency)...")
        if os.path.exists(default_test_pcm):
            pcm_path = default_test_pcm
        else:
            answer = (
                "Number please! This is a test call from the simulated switchboard operator. "
                "Your telephone audio and cradle switch are working normally. "
                "Hang up at any time to disconnect."
            )
            pcm_path = "/tmp/phone_speech.pcm"
            asyncio.run(synthesize_speech(answer, pcm_path))
    else:
        answer = ask_ai(args.prompt)
        pcm_path = "/tmp/phone_speech.pcm"
        asyncio.run(synthesize_speech(answer, pcm_path))

    # Create UDP control socket for bidirectional hook/ringer communication
    ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctrl_sock.bind(("", 0))

    try:
        # 3. Ring the phone buzzer and wait for user to lift handset
        answered = ring_and_wait_for_pickup(ctrl_sock, args.ip, ctrl_port=args.ctrl_port)

        # 4. If handset was picked up, play answer into earpiece
        if answered:
            send_audio_network(args.ip, args.audio_port, ctrl_sock, pcm_path, volume=args.volume)
    finally:
        ctrl_sock.close()


if __name__ == "__main__":
    main()
