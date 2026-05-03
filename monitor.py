"""
Badger Tape — Pipeline Monitor
Canlı progress takibi. Yeni bir terminal açıp çalıştır:
    python monitor.py
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"

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
    "variation_selected": "Varyasyon",
    "image_generated":    "Gorsel  ",
    "video_generated":    "Video   ",
    "music_generated":    "Muzik   ",
    "ffmpeg_merged":      "FFmpeg  ",
    "metadata_created":   "Metadata",
    "uploaded":           "YouTube ",
}

STEP_ESTIMATES = {
    "variation_selected": 5,
    "image_generated":    45,
    "video_generated":    240,
    "music_generated":    1050,
    "ffmpeg_merged":      1800,
    "metadata_created":   5,
    "uploaded":           450,
}


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def fmt_dur(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def bar(pct: int, width: int = 28) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def find_latest_session() -> tuple[Path | None, dict | None]:
    if not OUTPUT_DIR.exists():
        return None, None
    sessions = sorted(OUTPUT_DIR.glob("session_*/state.json"), reverse=True)
    for state_file in sessions:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            return state_file, state
        except Exception:
            continue
    return None, None


def get_upload_progress(state_file: Path) -> dict | None:
    progress_file = state_file.parent / "upload_progress.json"
    if not progress_file.exists():
        return None
    try:
        return json.loads(progress_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_step_progress(state_file: Path) -> dict | None:
    """FFmpeg veya müzik adımı için granüler ilerleme."""
    p = state_file.parent / "step_progress.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def render(state_file: Path, state: dict, start_times: dict):
    steps    = state["steps"]
    cost     = state["cost_tracker"]["total"]
    session  = state["session_id"]
    theme    = state["theme"]
    season   = state["season"]

    completed = sum(1 for s in STEP_NAMES if steps.get(s, False))
    pct       = int(completed / len(STEP_NAMES) * 100)

    # Detect active step
    active_step = None
    for s in STEP_NAMES:
        if not steps.get(s, False):
            active_step = s
            break

    # Elapsed since session started
    now = datetime.now()
    try:
        session_dt = datetime.strptime(session, "%Y%m%d_%H%M%S")
        elapsed    = (now - session_dt).total_seconds()
    except Exception:
        elapsed = 0

    # Estimated remaining
    if active_step:
        active_idx = STEP_NAMES.index(active_step)
        remaining = sum(
            STEP_ESTIMATES[s]
            for i, s in enumerate(STEP_NAMES)
            if i >= active_idx and not steps.get(s, False)
        )
    else:
        remaining = 0

    # Upload progress
    upload_info   = get_upload_progress(state_file)
    step_progress = get_step_progress(state_file)

    # ── Render ────────────────────────────────────────────────────────────────
    W = 52
    print("┌" + "─" * W + "┐")
    print(f"│  BADGER TAPE MONITOR  [{now.strftime('%H:%M:%S')}]" + " " * (W - 42) + "│")
    print(f"│  {theme} · {season}" + " " * (W - len(theme) - len(season) - 5) + "│")
    print("├" + "─" * W + "┤")

    # Overall progress bar
    b = bar(pct)
    pct_str = f"{pct:3d}%"
    line = f"  [{b}] {pct_str}  {completed}/{len(STEP_NAMES)} adim"
    print(f"│{line}" + " " * (W - len(line)) + "│")
    print("├" + "─" * W + "┤")

    # Steps
    for step in STEP_NAMES:
        label = STEP_LABELS[step]
        done  = steps.get(step, False)

        if done:
            icon   = " OK"
            detail = ""
        elif step == active_step:
            icon   = " >>"
            if step not in start_times:
                start_times[step] = time.time()
            step_elapsed = time.time() - start_times[step]
            detail = f"  {fmt_dur(step_elapsed)} gecti"
        else:
            icon   = "   "
            detail = ""

        # FFmpeg step: show granular progress from step_progress.json
        if step == "ffmpeg_merged" and step == active_step and step_progress and step_progress.get("step") == "ffmpeg":
            fp_pct = step_progress.get("percent", 0)
            cur_s  = step_progress.get("current_seconds", 0)
            fp_bar = bar(fp_pct, width=12)
            line   = f"{icon} {label}  [{fp_bar}] {fp_pct:3d}%  {fmt_dur(cur_s)}/1h"

        # Music step: show track-by-track progress
        elif step == "music_generated" and step == active_step and step_progress and step_progress.get("step") == "music":
            done_t = step_progress.get("done_tracks", 0)
            tot_t  = step_progress.get("total_tracks", 10)
            mp_pct = step_progress.get("percent", 0)
            mp_bar = bar(mp_pct, width=12)
            line   = f"{icon} {label}  [{mp_bar}] {done_t}/{tot_t} track"

        # Upload step: show upload progress
        elif step == "uploaded":
            if done:
                if upload_info and upload_info.get("url"):
                    url_short = upload_info["url"][-28:]
                    line = f"{icon} {label}  {url_short}"
                else:
                    line = f"{icon} {label}  tamamlandi"
            elif step == active_step and upload_info:
                up_pct  = upload_info.get("percent", 0)
                up_stat = upload_info.get("status", "uploading")
                up_bar  = bar(up_pct, width=12)
                line    = f"{icon} {label}  [{up_bar}] {up_pct:3d}%  {up_stat}"
            else:
                line = f"{icon} {label}{detail}"

        else:
            line = f"{icon} {label}{detail}"

        pad = W - len(line)
        if pad < 0:
            line = line[:W]
            pad  = 0
        print(f"│{line}" + " " * pad + "│")

    print("├" + "─" * W + "┤")

    # Timing + cost
    t_line = f"  Gecen: {fmt_dur(elapsed)}  Kalan: ~{fmt_dur(remaining)}"
    print(f"│{t_line}" + " " * (W - len(t_line)) + "│")
    c_line = f"  Harcama: ${cost:.3f} / $2.00"
    print(f"│{c_line}" + " " * (W - len(c_line)) + "│")

    # Error
    err = state.get("last_error")
    if err:
        print("├" + "─" * W + "┤")
        e_line = f"  HATA [{err['step']}]: {err['message'][:W - 14]}"
        print(f"│{e_line}" + " " * max(0, W - len(e_line)) + "│")

    print("└" + "─" * W + "┘")
    print("\n  Ctrl+C ile çık  |  2sn'de bir yenilenir")


def main():
    print("Badger Tape Monitor başlatılıyor...")
    start_times: dict[str, float] = {}

    try:
        while True:
            state_file, state = find_latest_session()
            clear()

            if state is None:
                print("┌─────────────────────────────────────┐")
                print("│  Henüz başlamış bir session yok...  │")
                print("│  Pipeline başlatılınca görünür.     │")
                print("└─────────────────────────────────────┘")
            else:
                render(state_file, state, start_times)

                # Auto-stop when fully done
                if state["steps"].get("uploaded") and state["artifacts"].get("youtube_url", "").startswith("https://"):
                    print("\n  Pipeline tamamlandı!")
                    break

            time.sleep(2)

    except KeyboardInterrupt:
        print("\n  Monitor kapatıldı.")


if __name__ == "__main__":
    main()
