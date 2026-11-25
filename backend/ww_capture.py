from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ww_capture")

DEFAULT_CAPTURE_DIR = (
    "/data/ww_captures"
    if os.path.exists("/data")
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "ww_captures")
)
MAX_CAPTURES = 200
CSV_COLUMNS = [
    "filename",
    "timestamp",
    "device_name",
    "mac",
    "model",
    "kind",
    "mode",
    "score",
    "threshold",
    "capture_threshold",
    "rms_dbfs",
    "outcome",
    "transcript",
]


class WakeWordCaptureStore:
    """Хранилище 3-секундных WAV-клипов срабатываний и near-miss вейкворда на сервере HA."""

    def __init__(self, base_dir: str = DEFAULT_CAPTURE_DIR, max_files: int = MAX_CAPTURES):
        self.base_dir = base_dir
        self.max_files = max_files
        self.meta_path = os.path.join(self.base_dir, "metadata.jsonl")
        self._lock = threading.Lock()
        self._records: List[Dict[str, Any]] = []
        os.makedirs(self.base_dir, exist_ok=True)
        self._load()

    def _load(self) -> None:
        records: List[Dict[str, Any]] = []
        if os.path.exists(self.meta_path):
            try:
                with open(self.meta_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            fn = rec.get("filename", "")
                            if fn and os.path.exists(os.path.join(self.base_dir, fn)):
                                records.append(rec)
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"[WW-CAPTURE] Failed to load metadata.jsonl: {e}")
        self._records = records

    def _persist_locked(self) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
        tmp_path = self.meta_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for rec in self._records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp_path, self.meta_path)

    def _prune_locked(self) -> None:
        if len(self._records) <= self.max_files:
            return
        overflow = len(self._records) - self.max_files
        to_remove = self._records[:overflow]
        self._records = self._records[overflow:]
        for rec in to_remove:
            fn = rec.get("filename", "")
            if fn:
                p = os.path.join(self.base_dir, fn)
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

    def save_clip(
        self,
        mac: str,
        device_name: str,
        wav_bytes: bytes,
        score: float,
        threshold: float,
        capture_threshold: float,
        rms_dbfs: float,
        model: str = "porfiriy.onnx",
        kind: str = "trigger",
        mode: str = "idle",
    ) -> Dict[str, Any]:
        if not wav_bytes:
            raise ValueError("Empty WAV payload")

        now_dt = datetime.now()
        ts_file = now_dt.strftime("%Y%m%d_%H%M%S")
        ts_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        mac_clean = re.sub(r"[^a-zA-Z0-9]", "", (mac or "dev").lower())[-4:] or "dev"
        kind_clean = re.sub(r"[^a-z0-9_]", "", (kind or "trigger").lower()) or "trigger"
        score_f = round(float(score), 4)
        thr_f = round(float(threshold), 4)
        cap_thr_f = round(float(capture_threshold), 4)
        rms_f = round(float(rms_dbfs), 1)

        with self._lock:
            base_name = f"{ts_file}_{mac_clean}_{kind_clean}_s{score_f:.2f}"
            filename = f"{base_name}.wav"
            idx = 1
            while os.path.exists(os.path.join(self.base_dir, filename)):
                filename = f"{base_name}_{idx}.wav"
                idx += 1

            os.makedirs(self.base_dir, exist_ok=True)
            full_path = os.path.join(self.base_dir, filename)
            with open(full_path, "wb") as f:
                f.write(wav_bytes)

            rec = {
                "id": filename[:-4],
                "filename": filename,
                "timestamp": ts_iso,
                "ts_epoch": round(time.time(), 2),
                "device_name": device_name or mac,
                "mac": (mac or "").strip().lower(),
                "model": model or "porfiriy.onnx",
                "kind": kind_clean,
                "mode": mode or "idle",
                "score": score_f,
                "threshold": thr_f,
                "capture_threshold": cap_thr_f,
                "rms_dbfs": rms_f,
                "size_bytes": len(wav_bytes),
                "outcome": "near_miss" if kind_clean == "near_miss" else "pending",
                "transcript": "",
            }
            self._records.append(rec)
            self._prune_locked()
            self._persist_locked()
            logger.info(
                f"[WW-CAPTURE] Saved {filename} ({len(wav_bytes)} bytes, kind={kind_clean}, "
                f"score={score_f:.3f}, thr={thr_f:.2f}, cap_thr={cap_thr_f:.2f})"
            )
            return dict(rec)

    def update_outcome(self, clip_id: str, outcome: str, transcript: str = "") -> bool:
        if not clip_id:
            return False
        with self._lock:
            for rec in reversed(self._records):
                if rec.get("id") == clip_id:
                    changed = False
                    if outcome and rec.get("outcome") != outcome:
                        rec["outcome"] = outcome
                        changed = True
                    clean_tr = (transcript or "").strip()
                    if clean_tr and rec.get("transcript") != clean_tr:
                        rec["transcript"] = clean_tr[:300]
                        changed = True
                    if changed:
                        self._persist_locked()
                    return True
        return False

    def list_clips(self, mac: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        mac_norm = (mac or "").strip().lower()
        with self._lock:
            filtered = [
                rec for rec in self._records
                if (not mac_norm or rec.get("mac") == mac_norm)
            ]
            total_count = len(filtered)
            total_bytes = sum(int(r.get("size_bytes") or 0) for r in filtered)
            newest = list(reversed(filtered))[:max(1, min(limit, self.max_files))]
            return {
                "clips": newest,
                "total_count": total_count,
                "total_bytes": total_bytes,
                "max_files": self.max_files,
            }

    def get_clip_path(self, filename: str) -> Optional[str]:
        safe_name = os.path.basename(filename or "")
        if not safe_name or not safe_name.endswith(".wav"):
            return None
        full_path = os.path.join(self.base_dir, safe_name)
        if os.path.exists(full_path):
            return full_path
        return None

    def delete_clip(self, filename: str) -> bool:
        safe_name = os.path.basename(filename or "")
        if not safe_name:
            return False
        with self._lock:
            before = len(self._records)
            self._records = [r for r in self._records if r.get("filename") != safe_name]
            full_path = os.path.join(self.base_dir, safe_name)
            removed_file = False
            if os.path.exists(full_path):
                try:
                    os.remove(full_path)
                    removed_file = True
                except Exception:
                    pass
            if len(self._records) != before:
                self._persist_locked()
                return True
            return removed_file

    def clear_clips(self, mac: Optional[str] = None) -> int:
        mac_norm = (mac or "").strip().lower()
        with self._lock:
            kept: List[Dict[str, Any]] = []
            removed = 0
            for rec in self._records:
                if not mac_norm or rec.get("mac") == mac_norm:
                    fn = rec.get("filename", "")
                    if fn:
                        p = os.path.join(self.base_dir, fn)
                        try:
                            if os.path.exists(p):
                                os.remove(p)
                        except Exception:
                            pass
                    removed += 1
                else:
                    kept.append(rec)
            self._records = kept
            self._persist_locked()
            logger.info(f"[WW-CAPTURE] Cleared {removed} clips (mac={mac_norm or 'all'}).")
            return removed

    def build_zip_bytes(self, mac: Optional[str] = None) -> bytes:
        mac_norm = (mac or "").strip().lower()
        with self._lock:
            selected = [
                dict(rec) for rec in self._records
                if (not mac_norm or rec.get("mac") == mac_norm)
            ]

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            csv_buf = io.StringIO()
            writer = csv.DictWriter(csv_buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for rec in selected:
                writer.writerow({k: rec.get(k, "") for k in CSV_COLUMNS})
            zf.writestr("metadata.csv", csv_buf.getvalue().encode("utf-8-sig"))

            for rec in selected:
                fn = rec.get("filename", "")
                if not fn:
                    continue
                full_path = os.path.join(self.base_dir, fn)
                if os.path.exists(full_path):
                    zf.write(full_path, arcname=f"wav/{fn}")

        return buf.getvalue()


ww_capture_store = WakeWordCaptureStore()
