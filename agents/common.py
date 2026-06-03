"""
Badger Tape — Shared utilities for agent architecture
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path


def _resolve_bin(name: str, env_key: str) -> str:
    """ffmpeg/ffprobe yolunu cozumle: once .env'deki tam yol, yoksa PATH."""
    p = os.getenv(env_key) or shutil.which(name)
    if not p:
        raise RuntimeError(
            f"{name} bulunamadi. PATH'e ekleyin veya .env icinde {env_key} tanimlayin."
        )
    return p


FFMPEG_PATH = _resolve_bin("ffmpeg", "FFMPEG_PATH")
FFPROBE_PATH = _resolve_bin("ffprobe", "FFPROBE_PATH")

# ── Directories ──────────────────────────────────────────────────────────────

BASE_DIR       = Path(__file__).resolve().parent.parent
REFERENCES_DIR = BASE_DIR / "references"
IMAGES_DIR     = BASE_DIR / "images"
VIDEOS_DIR     = BASE_DIR / "videos"
MUSIC_DIR      = BASE_DIR / "music"
OUTPUT_DIR     = BASE_DIR / "output"
TEMP_DIR       = BASE_DIR / "temp"

# ── Pipeline Logger ───────────────────────────────────────────────────────────

_pipeline_session_id = "unknown"


def set_pipeline_session_id(session_id: str):
    global _pipeline_session_id
    _pipeline_session_id = session_id


class _PipelineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts    = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        agent = record.name.split(".")[-1]
        return f"{ts} | {_pipeline_session_id} | {agent} | {record.levelname} | {record.getMessage()}"


def _setup_pipeline_logger() -> logging.Logger:
    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("badger_tape.pipeline")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.FileHandler(logs_dir / "pipeline.log", encoding="utf-8")
    handler.setFormatter(_PipelineFormatter())
    logger.addHandler(handler)
    return logger


_pipeline_logger = _setup_pipeline_logger()


def _log_to_file(agent: str, msg: str, level: int = logging.INFO):
    _pipeline_logger.getChild(agent).log(level, msg)


# ── Cost Constants ────────────────────────────────────────────────────────────

SPENDING_LIMIT    = 2.00
COST_IMAGE        = 0.08
COST_VIDEO_5S     = 0.35
COST_MUSIC_TRACK  = 0.035

# ── Progress Step Definitions ─────────────────────────────────────────────────

STEP_NAMES = [
    "variation_selected",
    "image_generated",
    "video_generated",
    "music_generated",
    "ffmpeg_merged",
    "metadata_created",
    "uploaded",
]

STEP_LABELS = {
    "variation_selected": "Varyasyon secildi",
    "image_generated":    "Gorsel uretildi",
    "video_generated":    "Video uretildi",
    "music_generated":    "Muzik uretildi",
    "ffmpeg_merged":      "FFmpeg birlestirme",
    "metadata_created":   "Metadata olusturuldu",
    "uploaded":           "YouTube'a yuklendi",
}

STEP_ACTIVE_LABELS = {
    "variation_selected": "Varyasyon seciliyor... (~5 sn)",
    "image_generated":    "Gorsel uretiliyor... (~45 sn)",
    "video_generated":    "Video uretiliyor... (~3-5 dk)",
    "music_generated":    "Muzik uretiliyor... (~15-20 dk) [paralel]",
    "ffmpeg_merged":      "FFmpeg birlestiriyor... (~20-40 dk)",
    "metadata_created":   "Metadata olusturuluyor... (~5 sn)",
    "uploaded":           "YouTube'a yukleniyor... (~5-10 dk)",
}

STEP_ESTIMATES_SEC = {
    "variation_selected": 5,
    "image_generated":    45,
    "video_generated":    240,
    "music_generated":    1050,
    "ffmpeg_merged":      1800,
    "metadata_created":   5,
    "uploaded":           450,
}


# ── CostTracker ───────────────────────────────────────────────────────────────

class CostTracker:
    """Thread-safe cost tracker."""

    def __init__(self, initial=0.0):
        self._lock = threading.Lock()
        self._total = initial

    @property
    def total(self):
        with self._lock:
            return self._total

    def check(self, about_to_spend: float):
        """Raise RuntimeError if spending would exceed limit."""
        with self._lock:
            projected = self._total + about_to_spend
        if projected > SPENDING_LIMIT:
            raise RuntimeError(
                f"$2 harcama limiti asilacak! "
                f"Simdiye kadar: ${self._total:.3f}, "
                f"eklenecek: ${about_to_spend:.3f}, "
                f"limit: ${SPENDING_LIMIT:.2f}"
            )
        if projected > SPENDING_LIMIT * 0.8:
            log("MALIYET", f"WARNING: Limite yaklasildi! Tahmini toplam: ${projected:.3f} / ${SPENDING_LIMIT:.2f}")

    def record(self, amount: float, label: str):
        with self._lock:
            self._total += amount
            total = self._total
        log("harcama", f"+${amount:.3f} ({label}) | toplam: ${total:.3f}")

    def remaining(self):
        return SPENDING_LIMIT - self.total


# ── Logging ───────────────────────────────────────────────────────────────────

def log(step: str, msg: str):
    print(f"[{step}] {msg}")
    sys.stdout.flush()
    msg_lower = msg.lower()
    if any(kw in msg_lower for kw in ("hata", "error", "basarisiz", "failed", "exception")):
        level = logging.ERROR
    elif any(kw in msg_lower for kw in ("uyari", "warning", "kritik", "limit")):
        level = logging.WARNING
    else:
        level = logging.INFO
    _log_to_file(step, msg, level)


# ── Progress Bar ──────────────────────────────────────────────────────────────

def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def print_progress(state: dict, active_step: str, start_time: datetime, cost_tracker: CostTracker):
    steps = state["steps"]
    cost = cost_tracker.total

    completed = sum(1 for s in STEP_NAMES if steps.get(s, False))
    total_steps = len(STEP_NAMES)
    pct = int(completed / total_steps * 100)

    bar_width = 20
    filled = int(pct / 100 * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)

    active_idx = STEP_NAMES.index(active_step) if active_step in STEP_NAMES else -1
    elapsed = (datetime.now() - start_time).total_seconds()
    remaining_sec = sum(
        STEP_ESTIMATES_SEC[s]
        for i, s in enumerate(STEP_NAMES)
        if i >= active_idx and not steps.get(s, False)
    )

    print("\n" + "━" * 44)
    print(" BADGER TAPE PIPELINE  [agent mimarisi]")
    print("━" * 44)
    print(f" [{bar}] %{pct:3d} | Adim {completed}/{total_steps}")

    for step in STEP_NAMES:
        label = STEP_LABELS[step]
        if steps.get(step, False):
            print(f" OK {label}")
        elif step == active_step:
            print(f" >> {STEP_ACTIVE_LABELS[step]}")
        else:
            print(f" -- {label}")

    print("━" * 44)
    print(f" Gecen: {format_duration(elapsed)} | Kalan: ~{format_duration(remaining_sec)}")
    print(f" Harcama: ${cost:.3f} / ${SPENDING_LIMIT:.2f}")
    print("━" * 44 + "\n")
    sys.stdout.flush()


# ── Session State Helpers ─────────────────────────────────────────────────────

def make_session_dir(session_id: str) -> dict:
    sd = {
        "images": IMAGES_DIR / f"session_{session_id}",
        "videos": VIDEOS_DIR / f"session_{session_id}",
        "music":  MUSIC_DIR  / f"session_{session_id}",
        "output": OUTPUT_DIR / f"session_{session_id}",
        "temp":   TEMP_DIR   / f"session_{session_id}",
    }
    for d in sd.values():
        d.mkdir(parents=True, exist_ok=True)
    return sd


def create_state(session_id: str, theme: str, season: str, vocal: bool = False) -> dict:
    return {
        "session_id": session_id,
        "theme": theme,
        "season": season,
        "vocal": vocal,
        "steps": {s: False for s in STEP_NAMES},
        "artifacts": {
            "variations":       None,
            "image_path":       None,
            "video_path":       None,
            "music_paths":      [],
            "final_video_path": None,
            "metadata_path":    None,
            "youtube_url":      None,
        },
        "cost_tracker": {
            "image": 0.0,
            "video": 0.0,
            "music": 0.0,
            "total": 0.0,
        },
        "last_error": None,
    }


def save_state(state: dict, state_path: Path, lock: threading.Lock = None):
    def _write():
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        sys.stdout.flush()

    if lock:
        with lock:
            _write()
    else:
        _write()


def load_state(state_path: Path) -> dict:
    return json.loads(state_path.read_text(encoding="utf-8"))


def find_incomplete_sessions() -> list:
    incomplete = []
    if not OUTPUT_DIR.exists():
        return incomplete
    for state_file in OUTPUT_DIR.glob("session_*/state.json"):
        try:
            state = load_state(state_file)
            if not state.get("steps", {}).get("uploaded", True):
                incomplete.append((state_file, state))
        except Exception:
            pass
    return incomplete


def save_error(state: dict, state_path: Path, step: str, message: str, lock: threading.Lock = None):
    state["last_error"] = {
        "step": step,
        "message": message,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_state(state, state_path, lock)
    _log_to_file(step, f"SAVED ERROR: {message}", logging.ERROR)


# ── FFmpeg Helpers ────────────────────────────────────────────────────────────

def run_ffmpeg(args_list: list, label: str):
    cmd = [FFMPEG_PATH, "-y"] + args_list
    log("FFmpeg", label)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg basarisiz — {label}\n"
            + (result.stderr[-2000:] if result.stderr else "No stderr")
        )


def run_ffmpeg_with_progress(
    args_list: list,
    label: str,
    progress_path: Path,
    target_seconds: float = 3600.0,
):
    """
    FFmpeg'i çalıştırır ve stderr'den time= okuyarak progress_path'e yazar.
    Monitor bu dosyayı okuyarak canlı ilerlemeyi gösterir.
    """
    import re

    cmd = [FFMPEG_PATH, "-y"] + args_list
    log("FFmpeg", label)

    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    time_re = re.compile(r"time=(\d+):(\d+):([\d.]+)")
    stderr_tail = []

    def _write_prog(pct: int, current_s: float):
        try:
            progress_path.write_text(
                json.dumps({
                    "step": "ffmpeg",
                    "percent": pct,
                    "current_seconds": round(current_s, 1),
                    "target_seconds": target_seconds,
                }),
                encoding="utf-8",
            )
        except Exception:
            pass

    _write_prog(0, 0)

    for line in proc.stderr:
        stderr_tail.append(line)
        if len(stderr_tail) > 50:
            stderr_tail.pop(0)
        m = time_re.search(line)
        if m:
            h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            current_s = h * 3600 + mn * 60 + s
            pct = min(99, int(current_s / target_seconds * 100))
            _write_prog(pct, current_s)

    proc.wait()

    if proc.returncode != 0:
        stderr_out = "".join(stderr_tail)
        raise RuntimeError(f"FFmpeg basarisiz — {label}\n{stderr_out[-2000:]}")

    _write_prog(100, target_seconds)
    log("FFmpeg", f"{label} — tamamlandi")


def get_duration(path: Path) -> float:
    result = subprocess.run(
        [FFPROBE_PATH, "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe sure okunamadi: {path}")
