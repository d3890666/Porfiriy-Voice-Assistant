import sys
import os
import time
import struct
import wave
import math
import argparse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    print("[ERROR] pyserial is not installed in this Python environment.")
    print("Run: pip install pyserial")
    sys.exit(1)

RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "debug_recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

def find_esp_port():
    ports = list(list_ports.comports())
    for p in ports:
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()
        if "303a" in hwid or "esp32" in desc or "ch340" in desc or "cp210" in desc or "usb jtag" in desc or "usb serial" in desc:
            return p.device
    for p in ports:
        if "com1" not in p.device.lower():
            return p.device
    return "COM3"

def send_and_wait(ser, cmd, wait_sec=0.5):
    ser.reset_input_buffer()
    ser.write((cmd + "\n").encode('utf-8'))
    ser.flush()
    time.sleep(wait_sec)
    lines = []
    while ser.in_waiting:
        line = ser.readline().decode('utf-8', errors='replace').strip()
        if line:
            lines.append(line)
    return lines

def analyze_audio_samples(samples, sample_rate=16000):
    if not samples:
        return {}
    
    n = len(samples)
    min_v = min(samples)
    max_v = max(samples)
    pk_pk = max_v - min_v
    mean_v = sum(samples) / n
    
    sum_sq = sum(s * s for s in samples)
    rms = math.sqrt(sum_sq / n)
    dbfs = 20 * math.log10(rms / 32767.0) if rms > 0 else -100.0
    
    # Детектор резких скачков (щелчков/треска)
    clicks = []
    jump_threshold = 3500
    for i in range(1, n):
        diff = abs(samples[i] - samples[i-1])
        if diff > jump_threshold:
            clicks.append((i, diff, samples[i-1], samples[i]))
            
    click_rate = len(clicks) / (n / float(sample_rate))
    
    return {
        "samples": n,
        "duration_s": n / float(sample_rate),
        "min": min_v,
        "max": max_v,
        "peak_to_peak": pk_pk,
        "mean_dc": mean_v,
        "rms": rms,
        "dbfs": dbfs,
        "clicks": len(clicks),
        "click_rate": click_rate,
        "first_clicks": clicks[:5]
    }

def record_wav(ser, duration_sec=5.0, filename=None):
    if not filename:
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"test_{ts}.wav"
    filepath = os.path.join(RECORDINGS_DIR, filename)

    print(f"\n[RECORD] Starting {duration_sec}s recording into: {filename}...")
    ser.reset_input_buffer()
    ser.write(b"STREAM\n")
    ser.flush()

    samples = []
    start_time = time.time()
    last_seq = None
    dropped_packets = 0
    total_packets = 0

    # Буфер сбора пакетов
    rx_buf = bytearray()
    
    try:
        while (time.time() - start_time) < duration_sec:
            in_w = ser.in_waiting
            if in_w > 0:
                rx_buf.extend(ser.read(in_w))
            else:
                time.sleep(0.005)
                continue

            # Поиск пакета 0xAA 0x55
            while len(rx_buf) >= 8:
                if rx_buf[0] == 0xAA and rx_buf[1] == 0x55:
                    seq, num_samples = struct.unpack("<HH", rx_buf[2:6])
                    pkt_len = 6 + num_samples * 2 + 2
                    if len(rx_buf) < pkt_len:
                        break # Ждем остаток пакета
                    
                    if rx_buf[pkt_len - 2] == 0x55 and rx_buf[pkt_len - 1] == 0xAA:
                        # Валидный пакет
                        audio_raw = rx_buf[6:6 + num_samples * 2]
                        s_data = struct.unpack(f"<{num_samples}h", audio_raw)
                        samples.extend(s_data)
                        
                        if last_seq is not None:
                            diff_seq = (seq - last_seq) & 0xFFFF
                            if diff_seq > 1:
                                dropped_packets += (diff_seq - 1)
                        last_seq = seq
                        total_packets += 1
                        
                        rx_buf = rx_buf[pkt_len:]
                    else:
                        # Поврежден футер, сдвигаем на 1 байт
                        rx_buf = rx_buf[1:]
                else:
                    rx_buf = rx_buf[1:]
    finally:
        ser.write(b"STOP\n")
        ser.flush()
        time.sleep(0.1)
        ser.reset_input_buffer()

    if not samples:
        print("[ERROR] No audio data received! Check connection.")
        return None

    # Сохранение WAV
    with wave.open(filepath, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    print(f"[OK] Saved WAV file: {filepath}")
    print(f"     Total samples: {len(samples)} ({len(samples)/16000:.2f}s)")
    print(f"     Packets: {total_packets} received, {dropped_packets} dropped")

    # Анализ
    res = analyze_audio_samples(samples)
    print("\n--- [АНАЛИЗ АУДИОЗАПИСИ] ---")
    print(f"Уровень сигнала (RMS) : {res['rms']:.1f} ({res['dbfs']:.1f} dBFS)")
    print(f"Диапазон Min..Max      : {res['min']} .. {res['max']} (Размах: {res['peak_to_peak']})")
    print(f"Постоянное смещение DC : {res['mean_dc']:.2f}")
    print(f"Щелчки/треск (jumps)  : {res['clicks']} обнаружено ({res['click_rate']:.1f} щелчков/сек)")
    
    if res['click_rate'] > 10.0:
        print(">> ВЕРДИКТ: [!] ВЫСОКИЙ УРОВЕНЬ ТРЕСКА!")
        print("   Причины: висящий L/R пин микрофона (не на GND), пульсации питания без конденсатора или наводки BCLK.")
    elif res['click_rate'] > 1.0:
        print(">> ВЕРДИКТ: [!] РЕДКИЕ ЩЕЛЧКИ (возможны единичные выпадения сэмплов или помехи).")
    elif res['rms'] < 5.0 and res['peak_to_peak'] < 30:
        print(">> ВЕРДИКТ: [?] ТИШИНА / МИКРОФОН НЕ ОТВЕЧАЕТ. Проверьте питание 3.3V и контакты.")
    else:
        print(">> ВЕРДИКТ: [OK] ЧИСТЫЙ ЗВУК! Треск и резкие скачки отсутствуют.")
    print("----------------------------\n")

    return filepath

def main():
    parser = argparse.ArgumentParser(description="ESP32-S3 Audio Diagnostic & WAV Recorder")
    parser.add_argument("--port", default=None, help="COM port (e.g. COM3)")
    parser.add_argument("--baud", type=int, default=115200, help="Baudrate (default 115200)")
    parser.add_argument("--cmd", choices=["record", "dump", "stats", "set"], default=None)
    parser.add_argument("--duration", type=float, default=5.0, help="Record duration in seconds")
    parser.add_argument("--file", default=None, help="Output WAV filename")
    parser.add_argument("--gain", type=float, default=None, help="Software gain multiplier (e.g. 8.0, 10.0)")
    parser.add_argument("--shift", type=int, default=None)
    parser.add_argument("--channel", choices=["LEFT", "RIGHT"], default=None)
    parser.add_argument("--pull", choices=["FLOAT", "DOWN", "UP"], default=None)
    parser.add_argument("--filter", type=int, choices=[0, 1], default=None)
    parser.add_argument("--led", type=int, choices=[0, 1], default=None, help="Enable WS2812B LED (1 or 0)")
    parser.add_argument("--wifi", type=int, choices=[0, 1], default=None, help="Enable Wi-Fi TX Load (1 or 0)")
    parser.add_argument("--txpower", type=int, default=None, help="Wi-Fi TX Power raw value (e.g. 78=19.5dBm, 44=11dBm, 20=5dBm, 8=2dBm)")
    parser.add_argument("--ps", type=int, choices=[0, 1], default=None, help="Wi-Fi Power Save (0=NONE, 1=MIN_MODEM)")
    parser.add_argument("--limiter", type=int, choices=[0, 1], default=None, help="WebRTC Peak Limiter (1=ON, 0=OFF)")
    args = parser.parse_args()

    port = args.port or find_esp_port()
    print(f"[INIT] Connecting to ESP32 on {port}...")

    try:
        ser = serial.Serial(port, args.baud, timeout=1.0)
    except Exception as e:
        print(f"[ERROR] Could not open port {port}: {e}")
        sys.exit(1)

    time.sleep(1.0) # Даем USB CDC стабилизироваться

    # Применение настроек если заданы
    if args.gain is not None:
        lines = send_and_wait(ser, f"SET GAIN {args.gain}")
        print("\n".join(lines))
    if args.shift is not None:
        lines = send_and_wait(ser, f"SET SHIFT {args.shift}")
        print("\n".join(lines))
    if args.channel is not None:
        lines = send_and_wait(ser, f"SET CHANNEL {args.channel}")
        print("\n".join(lines))
    if args.pull is not None:
        lines = send_and_wait(ser, f"SET PULL {args.pull}")
        print("\n".join(lines))
    if args.filter is not None:
        lines = send_and_wait(ser, f"SET FILTER {args.filter}")
        print("\n".join(lines))
    if args.led is not None:
        lines = send_and_wait(ser, f"SET LED {args.led}")
        print("\n".join(lines))
    if args.wifi is not None:
        lines = send_and_wait(ser, f"SET WIFI {args.wifi}", wait_sec=1.0)
        print("\n".join(lines))
    if args.txpower is not None:
        lines = send_and_wait(ser, f"SET TXPOWER {args.txpower}")
        print("\n".join(lines))
    if args.ps is not None:
        lines = send_and_wait(ser, f"SET PS {args.ps}")
        print("\n".join(lines))
    if args.limiter is not None:
        lines = send_and_wait(ser, f"SET LIMITER {args.limiter}")
        print("\n".join(lines))

    if args.cmd == "dump":
        lines = send_and_wait(ser, "DUMP", wait_sec=0.8)
        print("\n".join(lines))
    elif args.cmd == "stats":
        lines = send_and_wait(ser, "STATS", wait_sec=1.5)
        print("\n".join(lines))
    elif args.cmd == "record":
        record_wav(ser, args.duration, args.file)
    else:
        # Интерактивное меню
        while True:
            print("\n=== ESP32 AUDIO DIAGNOSTIC MENU ===")
            print("1. Записать 5 сек аудио в WAV (прослушать и проанализировать)")
            print("2. Запустить STATS (статистика 1 сек на плате)")
            print("3. Запустить DUMP (сырые 32-бит I2S регистры и HEX)")
            print("4. Переключить канал (LEFT / RIGHT)")
            print("5. Изменить сдвиг бит (>> 16 / >> 15 / >> 14)")
            print("6. Изменить подтяжку DIN (PULLDOWN / FLOAT / PULLUP)")
            print("7. Вкл/Выкл DC фильтр (FILTER 1/0)")
            print("0. Выход")
            try:
                choice = input("\nВыберите действие [0-7]: ").strip()
            except (KeyboardInterrupt, EOFError):
                break

            if choice == "1":
                record_wav(ser, 5.0)
            elif choice == "2":
                lines = send_and_wait(ser, "STATS", wait_sec=1.5)
                print("\n".join(lines))
            elif choice == "3":
                lines = send_and_wait(ser, "DUMP", wait_sec=0.8)
                print("\n".join(lines))
            elif choice == "4":
                ch = input("Введите канал (LEFT или RIGHT): ").strip().upper()
                lines = send_and_wait(ser, f"SET CHANNEL {ch}")
                print("\n".join(lines))
            elif choice == "5":
                sh = input("Введите сдвиг (16, 15 или 14): ").strip()
                lines = send_and_wait(ser, f"SET SHIFT {sh}")
                print("\n".join(lines))
            elif choice == "6":
                p = input("Введите подтяжку (DOWN, FLOAT, UP): ").strip().upper()
                lines = send_and_wait(ser, f"SET PULL {p}")
                print("\n".join(lines))
            elif choice == "7":
                f = input("Фильтр (1 - вкл, 0 - выкл): ").strip()
                lines = send_and_wait(ser, f"SET FILTER {f}")
                print("\n".join(lines))
            elif choice == "0":
                break

    ser.close()

if __name__ == "__main__":
    main()
