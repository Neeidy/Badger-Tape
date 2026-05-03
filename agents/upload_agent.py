"""
Badger Tape — Upload Agent
YouTube'a video yükler. Duplicate koruması ile.
"""

import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from agents.common import (
    BASE_DIR,
    CostTracker, log, save_state, save_error,
)


class UploadAgent:
    """
    Adim 7: YouTube'a video yükler.
    - Duplicate koruması: state["steps"]["uploaded"] == True ise atlar.
    - youtube_upload.py script'ini subprocess ile çağırır.
    """

    def run(
        self,
        state: dict,
        state_path: Path,
        session_dir: dict,
        cost_tracker: CostTracker,
        lock: threading.Lock,
    ) -> tuple[bool, str | None]:

        # ── Duplicate koruması ────────────────────────────────────────────────
        _existing_url = state["artifacts"].get("youtube_url", "")
        if state["steps"]["uploaded"] and "watch?v=" in _existing_url:
            log("UploadAgent", "Adim 7 — ATLANIYIOR: Bu session zaten yuklendi!")
            log("UploadAgent", f"  YouTube URL: {_existing_url}")
            return True, None

        log("UploadAgent", "Adim 7 — YouTube'a yukleniyor")
        try:
            final_video   = Path(state["artifacts"]["final_video_path"])
            metadata_path = Path(state["artifacts"]["metadata_path"])

            if not final_video.exists():
                raise FileNotFoundError(f"Final video bulunamadi: {final_video}")
            if not metadata_path.exists():
                raise FileNotFoundError(f"Metadata bulunamadi: {metadata_path}")

            upload_script = BASE_DIR / "scripts" / "youtube_upload.py"
            if not upload_script.exists():
                raise FileNotFoundError("scripts/youtube_upload.py bulunamadi")

            cmd = [
                sys.executable, str(upload_script),
                "--file",     str(final_video),
                "--metadata", str(metadata_path),
            ]

            result = subprocess.run(cmd, capture_output=True, timeout=7200)
            if result.returncode != 0:
                error_detail = result.stderr.decode('utf-8', errors='replace') if result.stderr else "no stderr"
                stdout_detail = result.stdout.decode('utf-8', errors='replace') if result.stdout else "no stdout"
                msg = f"youtube_upload.py cikis kodu: {result.returncode} | stderr: {error_detail} | stdout: {stdout_detail}"
                raise RuntimeError(msg)

            stdout_text = result.stdout.decode('utf-8', errors='replace') if result.stdout else ""
            match = re.search(r'https://(?:www\.youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})', stdout_text)
            if not match:
                raise RuntimeError(f"YouTube video ID bulunamadi. stdout: {stdout_text[:500]}")
            youtube_url = f"https://www.youtube.com/watch?v={match.group(1)}"
            with lock:
                state["steps"]["uploaded"]         = True
                state["artifacts"]["youtube_url"]  = youtube_url
            save_state(state, state_path, lock)
            log("UploadAgent", f"  Yukleme tamamlandi: {youtube_url}")

        except Exception as e:
            msg = f"YouTube upload basarisiz: {e}"
            save_error(state, state_path, "uploaded", msg, lock)
            return False, msg

        return True, None
