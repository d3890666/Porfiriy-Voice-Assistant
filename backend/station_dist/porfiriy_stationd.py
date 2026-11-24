#!/usr/bin/env python3
"""
porfiriy_stationd.py -- Нативный демон локального вейкворда и аудио-моста для Яндекс Станции 2.

Функции:
  1. Захват 16-канального PDM потока hw:0,2 (48 кГц S16_LE):
     - ch 0, 2, 4, 7: 4 микрофона
     - ch 8, 9: аппаратно синхронизированный feedback динамика
  2. DSP-цепочка: DC-блокер (IIR HPF) -> децимация 48->16 кГц -> программный NLMS AEC по ch 8/9 -> усиление.
  3. Локальный openWakeWord ONNX (1 поток, ~42 мс на 80 мс блок, ~14% CPU):
     melspectrogram.onnx -> embedding_model.onnx -> porfiriy.onnx.
  4. WebSocket клиент к Porfiriy Add-on (:8765):
     - Отправка wake_word_detected + pre-roll (0.8с) + голос в Gemini Live API.
     - Приём 24 кГц PCM ответов и earcons -> воспроизведение через aplay -D default (Master).
     - Локальный авто-дакинг музыки (amixer set 'Music' 25% / 100%).
     - Телеметрия ww_score / peak / rms_dbfs / infer_ms и 3-секундный WAV дамп после AEC.
  5. Управление LED-панелью Станции 2 (/dev/fb0 89x1 32bpp + /sys/class/backlight/fb0/brightness).
"""

import asyncio
import base64
import collections
import ctypes
import io
import json
import math
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import wave

import numpy as np
import onnxruntime as ort
import websockets

CHUNK_SAMPLES_16K = 1280  # 80 ms @ 16 kHz
CHUNK_FRAMES_48K = CHUNK_SAMPLES_16K * 3  # 3840 frames @ 48 kHz
NUM_CHANNELS_HW = 16
BYTES_PER_BLOCK_48K = CHUNK_FRAMES_48K * NUM_CHANNELS_HW * 2  # 122880 bytes

STATIOND_BASE = "/data/opt/porfiriy-stationd"
LOCAL_CONFIG_PATH = os.path.join(STATIOND_BASE, "config.json")
WIFI_SERVERS_PATH = os.path.join(STATIOND_BASE, "wifi_servers.json")

PERSISTED_KEYS = (
    "ww_threshold",
    "pdm_gain",
    "mic_gain",
    "dc_blocker_enabled",
    "aec_enabled",
    "led_brightness",
    "led_mode_idle",
    "led_color_idle",
    "led_mode_listen",
    "led_color_listen",
    "led_mode_think",
    "led_color_think",
    "led_mode_speak",
    "led_color_speak",
)


def load_local_config() -> dict:
    try:
        if os.path.exists(LOCAL_CONFIG_PATH):
            with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        print(f"[STATIOND] Failed to read local config.json: {e}", flush=True)
    return {}


def save_local_config(cfg: dict) -> None:
    try:
        clean = {k: cfg[k] for k in PERSISTED_KEYS if k in cfg and cfg[k] is not None}
        os.makedirs(STATIOND_BASE, exist_ok=True)
        tmp_path = LOCAL_CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, LOCAL_CONFIG_PATH)
    except Exception as e:
        print(f"[STATIOND] Failed to save local config.json: {e}", flush=True)


def get_current_ssid() -> str:
    try:
        out = subprocess.check_output(
            ["wpa_cli", "-i", "wlan0", "status"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
        for line in out.splitlines():
            if line.startswith("ssid="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def resolve_server_for_ssid(ssid: str, fallback_host: str, fallback_port: int = 8765):
    """Определяет IP сервера Порфирия для текущего Wi-Fi SSID из wifi_servers.json."""
    try:
        if ssid and os.path.exists(WIFI_SERVERS_PATH):
            with open(WIFI_SERVERS_PATH, "r", encoding="utf-8") as f:
                mapping = json.load(f)
            if isinstance(mapping, dict) and ssid in mapping:
                entry = mapping[ssid]
                if isinstance(entry, dict) and entry.get("server_ip"):
                    host = str(entry["server_ip"]).strip()
                    port = int(entry.get("ws_port", fallback_port))
                    return host, port
    except Exception as e:
        print(f"[STATIOND] wifi_servers.json parse note: {e}", flush=True)
    return fallback_host, fallback_port


def sync_squeezelite_server(server_ip: str) -> None:
    """Если сервер изменился (например, при переезде на дачу), обновляет SQUEEZELITE_SERVER и перезапускает squeezelite."""
    env_path = "/data/opt/squeezelite/squeezelite.env"
    if not server_ip or not os.path.exists(env_path):
        return
    try:
        content = open(env_path, "r", encoding="utf-8").read()
        target_line = f'SQUEEZELITE_SERVER="{server_ip}"'
        if f"SQUEEZELITE_SERVER={server_ip}" in content or target_line in content:
            return
        new_lines = []
        replaced = False
        for line in content.splitlines():
            if line.startswith("SQUEEZELITE_SERVER="):
                new_lines.append(target_line)
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(target_line)
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
        subprocess.Popen(["sh", "/data/opt/squeezelite/service", "restart"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[STATIOND] Updated Squeezelite server to {server_ip} for current Wi-Fi", flush=True)
    except Exception as e:
        print(f"[STATIOND] Squeezelite sync note: {e}", flush=True)


class StationLEDController:
    """Управление LED-панелью Яндекс Станции 2 через /dev/fb0 (89x1, 32bpp) и backlight."""

    FB_PATH = "/dev/fb0"
    BL_PATH = "/sys/class/backlight/fb0/brightness"
    PIXELS = 89

    def __init__(self):
        self.state = "idle"
        self.running = False
        self.cfg = {
            "led_brightness": 80,
            "led_mode_idle": 0,
            "led_color_idle": "#000000",
            "led_mode_listen": 1,
            "led_color_listen": "#0088ff",
            "led_mode_think": 2,
            "led_color_think": "#ffaa00",
            "led_mode_speak": 1,
            "led_color_speak": "#00ff88",
        }
        self._lock = threading.Lock()
        self._thread = None

    def update_config(self, new_cfg: dict):
        with self._lock:
            for k in list(self.cfg.keys()):
                if k in new_cfg and new_cfg[k] is not None:
                    self.cfg[k] = new_cfg[k]

    def set_state(self, state: str):
        with self._lock:
            self.state = state

    @staticmethod
    def _parse_hex(hex_str: str):
        s = str(hex_str or "#000000").lstrip("#")
        if len(s) != 6:
            return 0, 0, 0
        try:
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        except Exception:
            return 0, 0, 0

    def _write_frame(self, r: int, g: int, b: int, brightness: int):
        if not os.path.exists(self.FB_PATH):
            return
        try:
            if os.path.exists(self.BL_PATH):
                with open(self.BL_PATH, "w") as bf:
                    bf.write(str(max(0, min(255, int(brightness)))))
            # BGRA 32-bit pixel format for Linux fb0
            px = struct.pack("<BBBB", b, g, r, 0xFF)
            frame = px * self.PIXELS
            with open(self.FB_PATH, "wb") as fb:
                fb.write(frame)
        except Exception:
            pass

    def _loop(self):
        while self.running:
            with self._lock:
                st = self.state
                c = dict(self.cfg)
            mode_key = {
                "idle": "led_mode_idle",
                "listening": "led_mode_listen",
                "thinking": "led_mode_think",
                "speaking": "led_mode_speak",
            }.get(st, "led_mode_idle")
            col_key = {
                "idle": "led_color_idle",
                "listening": "led_color_listen",
                "thinking": "led_color_think",
                "speaking": "led_color_speak",
            }.get(st, "led_color_idle")

            mode = int(c.get(mode_key, 0))
            bright = int(c.get("led_brightness", 80))
            if mode == 0 or bright <= 0:
                self._write_frame(0, 0, 0, 0)
            else:
                r, g, b = self._parse_hex(c.get(col_key, "#0088ff"))
                if mode == 2:  # Breathing
                    t = time.monotonic()
                    factor = 0.20 + 0.80 * (0.5 + 0.5 * math.sin(t * math.pi * 1.4))
                    self._write_frame(int(r * factor), int(g * factor), int(b * factor), bright)
                else:  # Solid
                    self._write_frame(r, g, b, bright)
            time.sleep(0.06)

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self._write_frame(0, 0, 0, 0)


class Speex2ChAEC:
    """Нативный 2-канальный C-эхоподавитель (libspeexdsp) + Residual Echo Suppressor + Noise Suppressor."""

    FRAME = 160       # 10 ms @ 16 kHz
    FILTER_LEN = 2048 # 128 ms эхо-хвост комнаты
    SR = 16000

    def __init__(self, lib_path: str = "/data/opt/porfiriy-stationd/lib64/libspeexdsp.so.1"):
        self.ready = False
        self._lib = None
        self._echo = None
        self._pp = None
        try:
            lib = ctypes.CDLL(lib_path)
            lib.speex_echo_state_init_mc.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
            lib.speex_echo_state_init_mc.restype = ctypes.c_void_p
            lib.speex_echo_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            lib.speex_echo_ctl.restype = ctypes.c_int
            lib.speex_echo_cancellation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            lib.speex_echo_cancellation.restype = None
            lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
            lib.speex_preprocess_state_init.restype = ctypes.c_void_p
            lib.speex_preprocess_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            lib.speex_preprocess_ctl.restype = ctypes.c_int
            lib.speex_preprocess_run.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            lib.speex_preprocess_run.restype = ctypes.c_int

            echo_st = lib.speex_echo_state_init_mc(self.FRAME, self.FILTER_LEN, 1, 2)
            sr_val = ctypes.c_int(self.SR)
            lib.speex_echo_ctl(echo_st, 24, ctypes.byref(sr_val))  # SPEEX_ECHO_SET_SAMPLING_RATE

            pp_st = lib.speex_preprocess_state_init(self.FRAME, self.SR)
            denoise = ctypes.c_int(1)
            lib.speex_preprocess_ctl(pp_st, 0, ctypes.byref(denoise))  # SPEEX_PREPROCESS_SET_DENOISE
            lib.speex_preprocess_ctl(pp_st, 24, ctypes.c_void_p(echo_st))  # SPEEX_PREPROCESS_SET_ECHO_STATE
            sup = ctypes.c_int(-40)
            sup_act = ctypes.c_int(-25)
            lib.speex_preprocess_ctl(pp_st, 20, ctypes.byref(sup))  # SPEEX_PREPROCESS_SET_ECHO_SUPPRESS
            lib.speex_preprocess_ctl(pp_st, 22, ctypes.byref(sup_act))  # SPEEX_PREPROCESS_SET_ECHO_SUPPRESS_ACTIVE

            self._lib = lib
            self._echo = echo_st
            self._pp = pp_st
            self.ready = True
            print("[STATIOND] Loaded native 2-ch SpeexDSP AEC + Residual Echo Suppressor (128ms tail)", flush=True)
        except Exception as e:
            print(f"[STATIOND] SpeexDSP not available ({e}), AEC will be bypassed", flush=True)

    def process(self, mic_i16: np.ndarray, fb_l_i16: np.ndarray, fb_r_i16: np.ndarray) -> np.ndarray:
        if not self.ready:
            return mic_i16
        n = len(mic_i16)
        out = np.empty(n, dtype=np.int16)
        # Масштабируем аппаратный feedback (ch 8/9) в 4x под динамический диапазон микрофона
        fb_l = np.clip(fb_l_i16.astype(np.int32) * 4, -32767, 32767).astype(np.int16)
        fb_r = np.clip(fb_r_i16.astype(np.int32) * 4, -32767, 32767).astype(np.int16)
        ref_blk = np.empty(self.FRAME * 2, dtype=np.int16)
        e_blk = np.empty(self.FRAME, dtype=np.int16)
        for off in range(0, n - self.FRAME + 1, self.FRAME):
            m_blk = np.ascontiguousarray(mic_i16[off : off + self.FRAME], dtype=np.int16)
            ref_blk[0::2] = fb_l[off : off + self.FRAME]
            ref_blk[1::2] = fb_r[off : off + self.FRAME]
            self._lib.speex_echo_cancellation(self._echo, m_blk.ctypes.data, ref_blk.ctypes.data, e_blk.ctypes.data)
            self._lib.speex_preprocess_run(self._pp, e_blk.ctypes.data)
            out[off : off + self.FRAME] = e_blk
        return out


class DSPProcessor:
    """DC-блокер + децимация 48->16 кГц + 2-канальный SpeexDSP AEC по ch 8/9 + мягкий лимитер."""

    def __init__(self):
        self.dc_blocker_enabled = True
        self.aec_enabled = True
        self.mic_gain = 4.0

        # Состояние IIR DC-блокера (R = 0.985 @ 16kHz -> cutoff ~38 Hz) для mic, fbL, fbR
        self._dc_state = {"mic": [0.0, 0.0], "fbl": [0.0, 0.0], "fbr": [0.0, 0.0]}
        self.aec = Speex2ChAEC()

    def _dc_block(self, x: np.ndarray, key: str) -> np.ndarray:
        if len(x) == 0:
            return x
        x = x - np.mean(x)
        r = 0.985
        x1, y1 = self._dc_state[key]
        out = np.empty_like(x)
        for i in range(len(x)):
            xi = float(x[i])
            yi = xi - x1 + r * y1
            x1 = xi
            y1 = yi
            out[i] = yi
        self._dc_state[key][0] = x1
        self._dc_state[key][1] = y1
        return out

    def process_48k_16ch(self, raw_bytes: bytes) -> np.ndarray:
        arr = np.frombuffer(raw_bytes, dtype=np.int16).reshape(-1, NUM_CHANNELS_HW)
        # Усредняем 4 PDM-микрофона (ch 0, 2, 4, 7) для лучшего SNR
        mic_48k = (
            arr[:, 0].astype(np.float32)
            + arr[:, 2].astype(np.float32)
            + arr[:, 4].astype(np.float32)
            + arr[:, 7].astype(np.float32)
        ) * 0.25
        fb_l_48k = arr[:, 8].astype(np.float32)
        fb_r_48k = arr[:, 9].astype(np.float32)

        # Децимация 48 кГц -> 16 кГц
        n16 = len(mic_48k) // 3
        mic_16k = mic_48k[: n16 * 3].reshape(n16, 3).mean(axis=1)
        fbl_16k = fb_l_48k[: n16 * 3].reshape(n16, 3).mean(axis=1)
        fbr_16k = fb_r_48k[: n16 * 3].reshape(n16, 3).mean(axis=1)

        if self.dc_blocker_enabled:
            mic_16k = self._dc_block(mic_16k, "mic")
            fbl_16k = self._dc_block(fbl_16k, "fbl")
            fbr_16k = self._dc_block(fbr_16k, "fbr")

        # 2-канальный нативный SpeexDSP AEC + Residual Echo Suppressor (вычитает музыку до -29 дБ!)
        if self.aec_enabled and self.aec.ready and n16 == CHUNK_SAMPLES_16K:
            mic_i16 = np.clip(mic_16k, -32767.0, 32767.0).astype(np.int16)
            fbl_i16 = np.clip(fbl_16k, -32767.0, 32767.0).astype(np.int16)
            fbr_i16 = np.clip(fbr_16k, -32767.0, 32767.0).astype(np.int16)
            mic_16k = self.aec.process(mic_i16, fbl_i16, fbr_i16).astype(np.float32)

        if self.mic_gain != 1.0:
            mic_16k = mic_16k * float(self.mic_gain)

        # Мягкий лимитер (soft-knee tanh выше 24000) для защиты от клиппинга при высоком усилении
        peak = float(np.max(np.abs(mic_16k))) if len(mic_16k) else 0.0
        if peak > 24000.0:
            mic_16k = np.tanh(mic_16k / 28000.0) * 30000.0

        np.clip(mic_16k, -32767.0, 32767.0, out=mic_16k)
        return mic_16k.astype(np.int16)


class LocalOWW:
    """Полноценный openWakeWord ONNX (melspectrogram + embedding + porfiriy) на 1 потоке."""

    def __init__(self, models_dir: str, ww_model: str = "porfiriy.onnx", threads: int = 1):
        self.models_dir = models_dir
        self.ww_model = ww_model
        self.threads = threads
        self._load()

    def _load(self):
        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads
        so.inter_op_num_threads = 1
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        mel_path = os.path.join(self.models_dir, "melspectrogram.onnx")
        emb_path = os.path.join(self.models_dir, "embedding_model.onnx")
        ww_path = self.ww_model if os.path.isabs(self.ww_model) else os.path.join(self.models_dir, self.ww_model)

        self.mel_sess = ort.InferenceSession(mel_path, sess_options=so, providers=["CPUExecutionProvider"])
        self.emb_sess = ort.InferenceSession(emb_path, sess_options=so, providers=["CPUExecutionProvider"])
        self.ww_sess = ort.InferenceSession(ww_path, sess_options=so, providers=["CPUExecutionProvider"])

        self.mel_in = self.mel_sess.get_inputs()[0].name
        self.emb_in = self.emb_sess.get_inputs()[0].name
        self.ww_in = self.ww_sess.get_inputs()[0].name
        ww_shape = self.ww_sess.get_inputs()[0].shape
        self.emb_window = int(ww_shape[1]) if len(ww_shape) >= 2 and isinstance(ww_shape[1], int) else 16
        self.reset()

    def reset(self):
        self.raw_tail = np.zeros(480, dtype=np.int16)
        self.mel_buffer = np.ones((76, 32), dtype=np.float32)
        self.emb_buffer = np.zeros((self.emb_window, 96), dtype=np.float32)
        self.frames_seen = 0

    def predict(self, samples_i16: np.ndarray) -> float:
        audio_in = np.concatenate([self.raw_tail, samples_i16]).astype(np.float32)[None, :]
        self.raw_tail = samples_i16[-480:].copy()

        mel = np.squeeze(self.mel_sess.run(None, {self.mel_in: audio_in})[0])
        if mel.ndim == 1:
            mel = mel[None, :]
        mel = mel / 10.0 + 2.0

        n_new = mel.shape[0]
        if n_new >= 76:
            self.mel_buffer = mel[-76:].astype(np.float32)
        else:
            self.mel_buffer = np.roll(self.mel_buffer, -n_new, axis=0)
            self.mel_buffer[-n_new:] = mel.astype(np.float32)

        self.frames_seen += n_new
        if self.frames_seen < 76:
            return 0.0

        emb = self.emb_sess.run(None, {self.emb_in: self.mel_buffer[None, :, :, None]})[0].reshape(96)
        self.emb_buffer = np.roll(self.emb_buffer, -1, axis=0)
        self.emb_buffer[-1] = emb

        ww_out = self.ww_sess.run(None, {self.ww_in: self.emb_buffer[None, :, :]})[0]
        return float(np.squeeze(ww_out))


def duck_music(enable: bool):
    if enable and (os.path.exists("/data/opt/disable_ducking") or os.path.exists("/tmp/disable_ducking")):
        return
    pct = "25%" if enable else "100%"
    subprocess.Popen(["amixer", "set", "Music", pct], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pcm16_to_wav_bytes(pcm: bytes, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return buf.getvalue()


def get_station_mac() -> str:
    env_mac = os.environ.get("PORFIRIY_MAC", "").strip().lower()
    if env_mac:
        return env_mac
    for iface in ("wlan0", "eth0"):
        p = f"/sys/class/net/{iface}/address"
        if os.path.exists(p):
            try:
                return open(p).read().strip().lower()
            except Exception:
                pass
    return "yandex_station_native"


def get_station_ip() -> str:
    try:
        out = subprocess.check_output("ip route get 1 | awk '{print $NF;exit}'", shell=True, text=True).strip()
        if out:
            return out
    except Exception:
        pass
    return "127.0.0.1"


async def run_daemon():
    fallback_host = os.environ.get("PORFIRIY_HOST", "").strip()
    if not fallback_host and os.path.exists("/data/opt/porfiriy_host"):
        fallback_host = open("/data/opt/porfiriy_host").read().strip()
    fallback_port = int(os.environ.get("PORFIRIY_WS_PORT", "8765"))
    models_dir = os.environ.get("PORFIRIY_MODELS_DIR", "/data/opt/porfiriy-stationd/models")
    ww_model = os.environ.get("PORFIRIY_WW_MODEL", "porfiriy.onnx")

    mac = get_station_mac()
    led = StationLEDController()
    led.start()
    dsp = DSPProcessor()
    oww = LocalOWW(models_dir, ww_model=ww_model, threads=1)

    state = {
        "mode": "idle",  # idle | listening | thinking | speaking
        "ww_threshold": 0.90,
        "pdm_gain": 40,
        "cooldown_until": 0.0,
        "listen_start": 0.0,
        "last_speech": 0.0,
        "user_spoke": False,
        "speech_frames": 0,
        "noise_floor_rms": 120.0,
        "peak_score": 0.0,
        "peak_time": 0.0,
        "last_tel_time": 0.0,
    }

    local_cfg = {
        "ww_threshold": 0.90,
        "pdm_gain": 40,
        "mic_gain": 4.0,
        "dc_blocker_enabled": True,
        "aec_enabled": True,
        "led_brightness": 80,
        "led_mode_idle": 0,
        "led_color_idle": "#000000",
        "led_mode_listen": 1,
        "led_color_listen": "#0088ff",
        "led_mode_think": 2,
        "led_color_think": "#ffaa00",
        "led_mode_speak": 1,
        "led_color_speak": "#00ff88",
    }

    def apply_config(cfg: dict, persist: bool = True):
        if "ww_threshold" in cfg and cfg["ww_threshold"] is not None:
            state["ww_threshold"] = float(cfg["ww_threshold"])
            local_cfg["ww_threshold"] = state["ww_threshold"]
        if "dc_blocker_enabled" in cfg and cfg["dc_blocker_enabled"] is not None:
            dsp.dc_blocker_enabled = bool(cfg["dc_blocker_enabled"])
            local_cfg["dc_blocker_enabled"] = dsp.dc_blocker_enabled
        if "aec_enabled" in cfg and cfg["aec_enabled"] is not None:
            dsp.aec_enabled = bool(cfg["aec_enabled"])
            local_cfg["aec_enabled"] = dsp.aec_enabled
        if "mic_gain" in cfg and cfg["mic_gain"] is not None:
            dsp.mic_gain = max(0.2, min(20.0, float(cfg["mic_gain"])))
            local_cfg["mic_gain"] = dsp.mic_gain
        if "pdm_gain" in cfg and cfg["pdm_gain"] is not None:
            pdm_val = max(0, min(48, int(cfg["pdm_gain"])))
            state["pdm_gain"] = pdm_val
            local_cfg["pdm_gain"] = pdm_val
            subprocess.Popen(
                ["amixer", "cset", "numid=42", str(pdm_val)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        led.update_config(cfg)
        for k in PERSISTED_KEYS:
            if k.startswith("led_") and k in cfg and cfg[k] is not None:
                local_cfg[k] = cfg[k]
        if persist:
            save_local_config(local_cfg)

    # Применяем сохранённый локальный конфиг сразу при старте (ещё до подключения к серверу!)
    saved = load_local_config()
    if saved:
        apply_config(saved, persist=False)
        print(f"[STATIOND] Loaded local config.json: thr={state['ww_threshold']:.2f} pdm={state['pdm_gain']} gain={dsp.mic_gain:.1f}x", flush=True)
    else:
        apply_config(local_cfg, persist=True)

    duck_music(False)

    preroll = collections.deque(maxlen=6)    # ~0.48s @ 80ms
    debug_ring = collections.deque(maxlen=38)  # ~3.0s @ 80ms

    aplay_proc = None

    def ensure_aplay():
        nonlocal aplay_proc
        if aplay_proc is None or aplay_proc.poll() is not None:
            aplay_proc = subprocess.Popen(
                ["aplay", "-D", "default", "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", "24000", "-q"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return aplay_proc

    def close_aplay():
        nonlocal aplay_proc
        if aplay_proc is not None:
            try:
                if aplay_proc.stdin:
                    aplay_proc.stdin.close()
                aplay_proc.wait(timeout=3.0)
            except Exception:
                try:
                    aplay_proc.kill()
                except Exception:
                    pass
            aplay_proc = None

    while True:
        arecord_proc = None
        try:
            cur_ssid = get_current_ssid()
            server_host, ws_port = resolve_server_for_ssid(cur_ssid, fallback_host, fallback_port)
            if not server_host:
                print("[STATIOND] Server host is empty, waiting 5s...", flush=True)
                await asyncio.sleep(5)
                continue
            sync_squeezelite_server(server_host)
            ip = get_station_ip()
            ws_url = f"ws://{server_host}:{ws_port}"
            print(f"[STATIOND] Connecting to {ws_url} (mac={mac}, ip={ip}, ssid={cur_ssid or '?'})...", flush=True)
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10, max_size=4 * 1024 * 1024) as ws:
                await ws.send(json.dumps({
                    "type": "register",
                    "mac": mac,
                    "ip": ip,
                    "ssid": cur_ssid,
                    "device_type": "yandex_station",
                    "name": "Яндекс Станция (Нативный Порфирий)",
                    "firmware": "stationd-1.2",
                    "config": local_cfg,
                }))
                print("[STATIOND] Registered on Porfiriy server. Starting ALSA hw:0,2 capture...", flush=True)

                arecord_proc = await asyncio.create_subprocess_exec(
                    "arecord", "-D", "hw:0,2", "-r", "48000", "-f", "S16_LE", "-c", "16", "-t", "raw", "-q",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                async def ws_receiver():
                    async for msg in ws:
                        if isinstance(msg, bytes):
                            p = ensure_aplay()
                            try:
                                p.stdin.write(msg)
                                p.stdin.flush()
                            except Exception:
                                close_aplay()
                        elif isinstance(msg, str):
                            try:
                                data = json.loads(msg)
                                mtype = data.get("type")
                                if mtype == "state":
                                    st = data.get("state", "idle")
                                    led.set_state(st)
                                    if st in ("thinking", "speaking"):
                                        state["mode"] = st
                                    elif st == "idle":
                                        state["mode"] = "idle"
                                elif mtype == "turn_complete":
                                    await asyncio.to_thread(close_aplay)
                                    duck_music(False)
                                    oww.reset()
                                    state["mode"] = "idle"
                                    state["cooldown_until"] = time.monotonic() + 1.2
                                    led.set_state("idle")
                                elif mtype == "set_config":
                                    cfg = data.get("config", data)
                                    apply_config(cfg, persist=True)
                                    print(
                                        f"[STATIOND] Config applied & saved: thr={state['ww_threshold']:.2f} "
                                        f"gain={dsp.mic_gain:.1f}x pdm={state['pdm_gain']} "
                                        f"dc={dsp.dc_blocker_enabled} aec={dsp.aec_enabled}",
                                        flush=True,
                                    )
                                elif mtype == "dump_mic" or mtype == "start_mic_test":
                                    pcm_all = b"".join(debug_ring)
                                    if pcm_all:
                                        wav_b = pcm16_to_wav_bytes(pcm_all, sr=16000)
                                        await ws.send(json.dumps({
                                            "type": "mic_dump_wav",
                                            "wav_b64": base64.b64encode(wav_b).decode("ascii"),
                                            "duration_s": len(pcm_all) / 32000.0,
                                        }))
                                elif mtype == "wake":
                                    if state["mode"] == "idle":
                                        duck_music(True)
                                        state["mode"] = "listening"
                                        state["listen_start"] = time.monotonic()
                                        state["last_speech"] = time.monotonic()
                                        state["user_spoke"] = False
                                        state["speech_frames"] = 0
                                        led.set_state("listening")
                                        await ws.send(json.dumps({"type": "wake_word_detected", "score": 1.0, "infer_ms": 0.0}))
                            except Exception as e_rx:
                                print(f"[STATIOND] RX parse note: {e_rx}", flush=True)

                rx_task = asyncio.create_task(ws_receiver())
                try:
                    while True:
                        raw = await arecord_proc.stdout.readexactly(BYTES_PER_BLOCK_48K)
                        pcm_i16 = await asyncio.to_thread(dsp.process_48k_16ch, raw)
                        pcm_bytes = pcm_i16.tobytes()
                        preroll.append(pcm_bytes)
                        debug_ring.append(pcm_bytes)

                        rms = float(np.sqrt(np.mean(pcm_i16.astype(np.float32) ** 2)))
                        dbfs = round(20.0 * math.log10(max(1.0, rms) / 32768.0), 1)
                        now = time.monotonic()

                        if state["mode"] == "idle":
                            if now < state["cooldown_until"]:
                                continue
                            t0 = time.perf_counter()
                            score = await asyncio.to_thread(oww.predict, pcm_i16)
                            infer_ms = (time.perf_counter() - t0) * 1000.0

                            # Адаптивно отслеживаем фоновый шум комнаты, когда вейкворд молчит
                            if score < 0.15:
                                nf = state["noise_floor_rms"]
                                if rms < nf:
                                    state["noise_floor_rms"] = nf * 0.90 + rms * 0.10
                                elif rms < nf * 3.0:
                                    state["noise_floor_rms"] = nf * 0.98 + rms * 0.02

                            if score > state["peak_score"] or (now - state["peak_time"] > 3.0):
                                state["peak_score"] = score
                                state["peak_time"] = now

                            if (score >= 0.15 and now - state["last_tel_time"] >= 0.12) or (now - state["last_tel_time"] >= 0.35):
                                state["last_tel_time"] = now
                                await ws.send(json.dumps({
                                    "type": "ww_telemetry",
                                    "score": round(score, 4),
                                    "peak": round(state["peak_score"], 4),
                                    "rms_dbfs": dbfs,
                                    "infer_ms": round(infer_ms, 1),
                                }))

                            if score >= state["ww_threshold"]:
                                print(
                                    f"[STATIOND] WAKE DETECTED! score={score:.3f} infer={infer_ms:.1f}ms "
                                    f"rms={dbfs}dBFS noise_floor={state['noise_floor_rms']:.0f}",
                                    flush=True,
                                )
                                duck_music(True)
                                state["mode"] = "listening"
                                state["listen_start"] = now
                                state["last_speech"] = now
                                state["user_spoke"] = False
                                state["speech_frames"] = 0
                                led.set_state("listening")
                                await ws.send(json.dumps({
                                    "type": "wake_word_detected",
                                    "score": round(score, 4),
                                    "infer_ms": round(infer_ms, 1),
                                }))
                                # Отправляем pre-roll буфер (чтобы слитная речь сразу после «Порфирий» не обрезалась)
                                for pr in list(preroll):
                                    await ws.send(pr)
                                preroll.clear()

                        elif state["mode"] == "listening":
                            await ws.send(pcm_bytes)
                            listen_elapsed = now - state["listen_start"]
                            speech_thresh = min(2500.0, max(350.0, state["noise_floor_rms"] * 2.2))

                            # Первые 0.40с после вейкворда (хвост слова «Порфирий» и earcon) звук стримим,
                            # но не засчитываем за user_spoke, чтобы пауза после вейкворда не обрывала запись.
                            if listen_elapsed >= 0.40:
                                if rms >= speech_thresh:
                                    state["speech_frames"] += 1
                                    if state["speech_frames"] >= 2:
                                        state["user_spoke"] = True
                                        state["last_speech"] = now
                                else:
                                    if not state["user_spoke"]:
                                        state["speech_frames"] = 0

                            if state["user_spoke"] and ((now - state["last_speech"] > 1.0) or (listen_elapsed > 12.0)):
                                print(
                                    f"[STATIOND] End of speech (elapsed={listen_elapsed:.2f}s, "
                                    f"thresh={speech_thresh:.0f}). Switching to thinking.",
                                    flush=True,
                                )
                                state["mode"] = "thinking"
                                led.set_state("thinking")
                                await ws.send(json.dumps({"type": "end_of_speech"}))
                            elif not state["user_spoke"] and listen_elapsed > 5.5:
                                print(
                                    f"[STATIOND] No speech detected for {listen_elapsed:.1f}s "
                                    f"(thresh={speech_thresh:.0f}). Returning to idle.",
                                    flush=True,
                                )
                                duck_music(False)
                                oww.reset()
                                state["mode"] = "idle"
                                state["cooldown_until"] = now + 0.8
                                led.set_state("idle")
                                await ws.send(json.dumps({"type": "listen_timeout"}))
                finally:
                    rx_task.cancel()
                    close_aplay()
                    duck_music(False)
                    if arecord_proc and arecord_proc.returncode is None:
                        arecord_proc.terminate()
        except Exception as e:
            print(f"[STATIOND] Connection/loop error: {e}. Reconnecting in 3s...", flush=True)
            close_aplay()
            duck_music(False)
            led.set_state("idle")
            await asyncio.sleep(3)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    asyncio.run(run_daemon())
