"""
wakeword_engine.py -- Серверный детектор вейкворда на базе openWakeWord (ONNX).
"""

import os
import time
import logging
import collections
from typing import Dict, Optional

logger = logging.getLogger("wakeword_engine")

CHUNK_SAMPLES = 1280
SAMPLE_RATE = 16000
DEFAULT_COOLDOWN_S = 3.0
PREROLL_CHUNKS = 8


class WakeWordEngine:
    def __init__(self, model_path: str, threshold: float = 0.94, cooldown_s: float = DEFAULT_COOLDOWN_S):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.model_path = model_path
        self._model = None
        self._model_name: Optional[str] = None
        self._sources: Dict[str, dict] = {}
        self._load_model()

    def _load_model(self):
        try:
            import numpy as np
            from openwakeword.model import Model
            candidates = [
                self.model_path,
                os.path.join(os.path.dirname(__file__), self.model_path),
                os.path.join(os.path.dirname(__file__), "..", self.model_path),
            ]
            resolved = None
            for c in candidates:
                if os.path.exists(c):
                    resolved = os.path.abspath(c)
                    break
            if not resolved:
                logger.error(f"[WW] Model not found: '{self.model_path}'. Server-side wake word DISABLED.")
                return
            self._model = Model(wakeword_models=[resolved], inference_framework="onnx")
            self._model_name = os.path.splitext(os.path.basename(resolved))[0]
            logger.info(f"[WW] Loaded: '{resolved}' (key='{self._model_name}', threshold={self.threshold})")
        except ImportError as e:
            logger.error(f"[WW] openwakeword not installed: {e}. DISABLED.")
        except Exception as e:
            logger.error(f"[WW] Load error: {e}")

    @property
    def is_ready(self) -> bool:
        return self._model is not None and self._model_name is not None

    def _get_source(self, device_id: str) -> dict:
        if device_id not in self._sources:
            self._sources[device_id] = {
                "last_trigger_time": 0.0,
                "triggered": False,
                "last_score": 0.0,
                "preroll": collections.deque(maxlen=PREROLL_CHUNKS),
            }
        return self._sources[device_id]

    def process_chunk(self, pcm_bytes: bytes, device_id: str) -> float:
        if not self.is_ready:
            return 0.0
        src = self._get_source(device_id)
        src["preroll"].append(pcm_bytes)
        now = time.time()
        if src["triggered"] or (now - src["last_trigger_time"]) < self.cooldown_s:
            return src["last_score"]
        try:
            import numpy as np
            chunk = np.frombuffer(pcm_bytes, dtype=np.int16)
            if len(chunk) < CHUNK_SAMPLES:
                chunk = np.pad(chunk, (0, CHUNK_SAMPLES - len(chunk)))
            elif len(chunk) > CHUNK_SAMPLES:
                chunk = chunk[:CHUNK_SAMPLES]
            prediction = self._model.predict(chunk)
            score = float(prediction.get(self._model_name, 0.0))
            src["last_score"] = score
            if score >= self.threshold:
                logger.info(f"[WW] Detected! device={device_id} score={score:.4f}")
                src["triggered"] = True
                src["last_trigger_time"] = now
            return score
        except Exception as e:
            logger.error(f"[WW] Inference error: {e}")
            return 0.0

    def is_triggered(self, device_id: str) -> bool:
        src = self._sources.get(device_id)
        return bool(src and src.get("triggered"))

    def consume_preroll(self, device_id: str) -> bytes:
        src = self._sources.get(device_id)
        if not src:
            return b""
        preroll_data = b"".join(src["preroll"])
        src["preroll"].clear()
        src["triggered"] = False
        return preroll_data

    def reset(self, device_id: str):
        if device_id in self._sources:
            del self._sources[device_id]
        if self.is_ready:
            try:
                if self._model_name in self._model.prediction_buffer:
                    self._model.prediction_buffer[self._model_name].clear()
            except Exception:
                pass

    def get_last_score(self, device_id: str) -> float:
        src = self._sources.get(device_id)
        return src["last_score"] if src else 0.0

    def set_threshold(self, new_threshold: float):
        self.threshold = max(0.5, min(1.0, float(new_threshold)))
        logger.info(f"[WW] Threshold updated to {self.threshold}")
