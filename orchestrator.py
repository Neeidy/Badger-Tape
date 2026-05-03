"""
Badger Tape — Orchestrator
Agent mimarisini koordine eder.

Kullanim:
    python orchestrator.py --theme "rainy night" --season autumn

Paralel calisma:
    - ImageAgent + MusicAgent AYNI ANDA baslar
    - ImageAgent tamamlaninca VideoAgent baslar (sirayla)
    - Her iki paralel dal (image->video ve music) tamamlaninca MergeAgent baslar
    - MergeAgent -> UploadAgent sirayla calisir
"""

import argparse
import io
import json
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

# Windows terminal emoji support
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from dotenv import load_dotenv
import os

load_dotenv()

if not os.getenv("FAL_KEY"):
    print("ERROR: FAL_KEY not found in .env")
    sys.exit(1)

from agents.common import (
    BASE_DIR, OUTPUT_DIR, SPENDING_LIMIT, COST_IMAGE, COST_VIDEO_5S, COST_MUSIC_TRACK,
    CostTracker, log,
    make_session_dir, create_state, save_state, load_state, find_incomplete_sessions,
    print_progress, format_duration, set_pipeline_session_id,
)
from agents.image_agent  import ImageAgent
from agents.video_agent  import VideoAgent
from agents.music_agent  import MusicAgent
from agents.merge_agent  import MergeAgent
from agents.upload_agent import UploadAgent


# ── Orchestrator ──────────────────────────────────────────────────────────────

class Orchestrator:
    def __init__(self, state: dict, state_path: Path, session_dir: dict, dry_run: bool = False):
        self.state       = state
        self.state_path  = state_path
        self.session_dir = session_dir
        self.dry_run     = dry_run
        self.lock        = threading.Lock()
        self.cost        = CostTracker(initial=state["cost_tracker"]["total"])
        self.start_time  = datetime.now()

        # Errors collected from threads
        self._errors: list[str] = []

    def _progress(self, active_step: str):
        print_progress(self.state, active_step, self.start_time, self.cost)

    # ── Dry-run step simulators ───────────────────────────────────────────────

    def _dry_image(self) -> tuple[bool, None]:
        log("Orchestrator", "DRY-RUN: variation_selected atlanidi")
        log("Orchestrator", "DRY-RUN: image_generated atlanidi")
        dummy_image = self.session_dir["images"] / "scene_dry_run.png"
        with self.lock:
            self.state["steps"]["variation_selected"] = True
            self.state["steps"]["image_generated"]    = True
            self.state["artifacts"]["variations"] = {
                "activity":        "dry_run_activity",
                "activity_motion": "dry_run_motion",
                "lighting":        "dry_run_lighting",
                "background":      "dry_run_background",
                "atmosphere":      "dry_run_atmosphere",
            }
            self.state["artifacts"]["image_path"] = str(dummy_image)
        save_state(self.state, self.state_path, self.lock)
        return True, None

    def _dry_video(self) -> tuple[bool, None]:
        log("Orchestrator", "DRY-RUN: video_generated atlanidi")
        dummy_video = self.session_dir["videos"] / "loop_dry_run.mp4"
        with self.lock:
            self.state["steps"]["video_generated"]      = True
            self.state["artifacts"]["video_path"]       = str(dummy_video)
            self.state["cost_tracker"]["video"]        += 0.0
            self.state["cost_tracker"]["total"]         = self.cost.total
        save_state(self.state, self.state_path, self.lock)
        return True, None

    def _dry_music(self) -> tuple[bool, None]:
        log("Orchestrator", "DRY-RUN: music_generated atlanidi")
        dummy_tracks = [str(self.session_dir["music"] / f"track_{i:02d}_dry_run.mp3") for i in range(1, 11)]
        with self.lock:
            self.state["steps"]["music_generated"]      = True
            self.state["artifacts"]["music_paths"]      = dummy_tracks
            self.state["cost_tracker"]["music"]        += 0.0
            self.state["cost_tracker"]["total"]         = self.cost.total
        save_state(self.state, self.state_path, self.lock)
        return True, None

    def _dry_merge(self) -> tuple[bool, None]:
        log("Orchestrator", "DRY-RUN: ffmpeg_merged atlanidi")
        log("Orchestrator", "DRY-RUN: metadata_created atlanidi")
        dummy_video    = self.session_dir["output"] / "badgertape_1hour_dry_run.mp4"
        dummy_metadata = self.session_dir["output"] / "metadata_dry_run.json"
        with self.lock:
            self.state["steps"]["ffmpeg_merged"]            = True
            self.state["steps"]["metadata_created"]         = True
            self.state["artifacts"]["final_video_path"]     = str(dummy_video)
            self.state["artifacts"]["metadata_path"]        = str(dummy_metadata)
        save_state(self.state, self.state_path, self.lock)
        return True, None

    def _dry_upload(self) -> tuple[bool, None]:
        log("Orchestrator", "DRY-RUN: uploaded atlanidi")
        dummy_url = f"dry_run_youtube_url_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        with self.lock:
            self.state["steps"]["uploaded"]            = True
            self.state["artifacts"]["youtube_url"]     = dummy_url
        save_state(self.state, self.state_path, self.lock)
        log("Orchestrator", f"  DRY-RUN URL: {dummy_url}")
        return True, None

    # ── Image + Video pipeline (runs in its own thread) ───────────────────────

    def _run_image_video(self):
        self._progress("image_generated")
        if self.dry_run:
            ok, err = self._dry_image()
        else:
            ok, err = ImageAgent().run(self.state, self.state_path, self.session_dir, self.cost, self.lock)
        if not ok:
            self._errors.append(f"[ImageAgent] {err}")
            return

        self._progress("video_generated")
        if self.dry_run:
            ok, err = self._dry_video()
        else:
            ok, err = VideoAgent().run(self.state, self.state_path, self.session_dir, self.cost, self.lock)
        if not ok:
            self._errors.append(f"[VideoAgent] {err}")

    # ── Music pipeline (runs in its own thread) ───────────────────────────────

    def _run_music(self):
        self._progress("music_generated")
        if self.dry_run:
            ok, err = self._dry_music()
        else:
            ok, err = MusicAgent().run(self.state, self.state_path, self.session_dir, self.cost, self.lock)
        if not ok:
            self._errors.append(f"[MusicAgent] {err}")

    # ── Main orchestration ────────────────────────────────────────────────────

    def run(self) -> bool:
        log("Orchestrator", f"Pipeline basliyor — session {self.state['session_id']}")
        log("Orchestrator", f"Tema: {self.state['theme']} | Mevsim: {self.state['season']}")
        log("Orchestrator", f"Harcama limiti: ${SPENDING_LIMIT:.2f}")
        if self.dry_run:
            log("Orchestrator", "*** DRY-RUN MODU — API cagrisi yapilmayacak ***")

        # Already done? Show summary and exit early.
        _yt_url = self.state["artifacts"].get("youtube_url", "")
        if self.state["steps"]["uploaded"] and "watch?v=" in _yt_url:
            log("Orchestrator", "Bu session zaten tamamlandi ve yuklendi.")
            log("Orchestrator", f"YouTube URL: {_yt_url}")
            return True

        # ── Phase 1: Image+Video  ||  Music  (parallel) ───────────────────────
        image_video_done = (
            self.state["steps"]["image_generated"] and
            self.state["steps"]["video_generated"]
        )
        music_done = self.state["steps"]["music_generated"]

        if not image_video_done or not music_done:
            log("Orchestrator", "Faz 1 — Image+Video ve Music paralel basliyor")

            threads = []
            if not image_video_done:
                t_iv = threading.Thread(target=self._run_image_video, name="image-video", daemon=True)
                threads.append(t_iv)
            if not music_done:
                t_mu = threading.Thread(target=self._run_music, name="music", daemon=True)
                threads.append(t_mu)

            for t in threads:
                t.start()
            for t in threads:
                t.join()

            if self._errors:
                log("Orchestrator", "HATA: Faz 1 tamamlanamadi:")
                for e in self._errors:
                    log("Orchestrator", f"  {e}")
                return False
        else:
            log("Orchestrator", "Faz 1 — atlanıyor (zaten tamamlandi)")

        # ── Phase 2: Merge ────────────────────────────────────────────────────
        if not (self.state["steps"]["ffmpeg_merged"] and self.state["steps"]["metadata_created"]):
            log("Orchestrator", "Faz 2 — MergeAgent basliyor")
            self._progress("ffmpeg_merged")
            if self.dry_run:
                ok, err = self._dry_merge()
            else:
                ok, err = MergeAgent().run(self.state, self.state_path, self.session_dir, self.cost, self.lock)
            if not ok:
                log("Orchestrator", f"HATA [MergeAgent]: {err}")
                return False
        else:
            log("Orchestrator", "Faz 2 — atlanıyor (zaten tamamlandi)")

        # ── Phase 3: Upload ───────────────────────────────────────────────────
        self._progress("uploaded")
        if self.dry_run:
            ok, err = self._dry_upload()
        else:
            ok, err = UploadAgent().run(self.state, self.state_path, self.session_dir, self.cost, self.lock)
        if not ok:
            log("Orchestrator", f"HATA [UploadAgent]: {err}")
            return False

        return True


# ── Monitor Auto-Launch ───────────────────────────────────────────────────────

def launch_monitor():
    """Yeni bir cmd penceresinde monitor.py'yi başlat."""
    monitor_script = BASE_DIR / "monitor.py"
    if not monitor_script.exists():
        log("Orchestrator", "UYARI: monitor.py bulunamadi, monitor baslatilmiyor")
        return
    try:
        subprocess.Popen(
            f'start "Badger Tape Monitor" cmd /k "python \\"{monitor_script}\\""',
            shell=True,
            cwd=str(BASE_DIR),
        )
        log("Orchestrator", "Monitor penceresi acildi")
    except Exception as e:
        log("Orchestrator", f"UYARI: Monitor baslatma hatasi: {e}")


# ── Stale Session Cleanup ─────────────────────────────────────────────────────

def cleanup_stale_sessions(current_session_id: str):
    """
    Gerçek YouTube URL'si olmayan (yüklenememiş) eski session klasörlerini sil.
    Mevcut session ve gerçekten yüklenenler korunur.
    """
    if not OUTPUT_DIR.exists():
        return
    deleted = 0
    for session_dir in OUTPUT_DIR.glob("session_*"):
        if not session_dir.is_dir():
            continue
        sid = session_dir.name.replace("session_", "")
        if sid == current_session_id:
            continue
        state_file = session_dir / "state.json"
        if not state_file.exists():
            shutil.rmtree(session_dir)
            log("Orchestrator", f"Eski session silindi (state yok): {session_dir.name}")
            deleted += 1
            continue
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            yt_url = state.get("artifacts", {}).get("youtube_url", "")
            real_uploaded = state["steps"].get("uploaded", False) and "watch?v=" in (yt_url or "")
            if not real_uploaded:
                shutil.rmtree(session_dir)
                log("Orchestrator", f"Tamamlanmamis session silindi: {session_dir.name}")
                deleted += 1
        except Exception:
            pass
    if deleted:
        log("Orchestrator", f"Toplam {deleted} eski/eksik session temizlendi")


# ── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup_temp(session_dir: dict):
    temp = session_dir["temp"]
    if temp.exists():
        shutil.rmtree(temp)
        log("Orchestrator", f"Temp temizlendi: {temp}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Badger Tape — Agent Orchestrator")
    parser.add_argument("--theme",  required=True,
                        help='Ornek: "rainy night"')
    parser.add_argument("--season", required=True,
                        choices=["autumn", "winter", "spring", "summer"])
    parser.add_argument("--vocal", action="store_true",
                        help="Vokal lofi modu — instrumental yerine soft vocals uret")
    parser.add_argument("--dry-run", action="store_true",
                        help="API cagrisi yapmadan akisi ve state yonetimini test et")
    args = parser.parse_args()

    # ── Resume kontrolu ───────────────────────────────────────────────────────
    incomplete = find_incomplete_sessions()
    state       = None
    session_dir = None
    state_path  = None

    if incomplete:
        print(f"\nTamamlanmamis {len(incomplete)} session bulundu:")
        for i, (sf, st) in enumerate(incomplete):
            err           = st.get("last_error")
            err_info      = f" | Son hata: [{err['step']}] {err['message'][:60]}" if err else ""
            completed_steps = sum(1 for v in st["steps"].values() if v)
            print(
                f"  [{i+1}] {st['session_id']} — "
                f"tema: {st['theme']}, mevsim: {st['season']}, "
                f"{completed_steps}/7 adim tamamlandi{err_info}"
            )
        print()
        choice = input("Kaldigi yerden devam edilsin mi? (y/n veya numara): ").strip().lower()

        resume_idx = None
        if choice == "y" and len(incomplete) == 1:
            resume_idx = 0
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(incomplete):
                resume_idx = idx

        if resume_idx is not None:
            state_path, state = incomplete[resume_idx]
            session_id  = state["session_id"]
            session_dir = make_session_dir(session_id)
            print(f"\nSession yuklendi: {session_id} | Onceki harcama: ${state['cost_tracker']['total']:.3f}")
            set_pipeline_session_id(session_id)
            skipped = [s for s, done in state["steps"].items() if done]
            if skipped:
                log("Orchestrator", f"RESUME MODE: skipping completed steps: {', '.join(skipped)}")

    if state is None:
        session_id  = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = make_session_dir(session_id)
        state       = create_state(session_id, args.theme, args.season, vocal=args.vocal)
        state_path  = OUTPUT_DIR / f"session_{session_id}" / "state.json"
        save_state(state, state_path)
        set_pipeline_session_id(session_id)
        # Yeni session başlarken: eski eksikleri temizle + monitor aç
        if not args.dry_run:
            cleanup_stale_sessions(session_id)
            launch_monitor()

    print(f"\n{'='*60}")
    print(f"  BADGER TAPE ORCHESTRATOR — session {state['session_id']}")
    print(f"  tema: {state['theme']} | mevsim: {state['season']}")
    print(f"  harcama limiti: ${SPENDING_LIMIT:.2f}")
    if args.dry_run:
        print(f"  MOD: DRY-RUN (API cagrisi yok)")
    print(f"{'='*60}\n")
    sys.stdout.flush()

    # ── Run ───────────────────────────────────────────────────────────────────
    orchestrator = Orchestrator(state, state_path, session_dir, dry_run=args.dry_run)
    log("Orchestrator", f"SESSION START | session_id: {state['session_id']} | theme: {state['theme']} | season: {state['season']} | dry_run: {args.dry_run}")
    success = orchestrator.run()
    total_duration = (datetime.now() - orchestrator.start_time).total_seconds()
    if success:
        log("Orchestrator", f"SESSION END | result: success | duration: {format_duration(total_duration)} | cost: ${orchestrator.cost.total:.3f}")
    else:
        log("Orchestrator", f"SESSION END | result: failed | duration: {format_duration(total_duration)} | cost: ${orchestrator.cost.total:.3f}")

    # ── Temp temizlik ─────────────────────────────────────────────────────────
    if success:
        cleanup_temp(session_dir)

    # ── Ozet ─────────────────────────────────────────────────────────────────
    cost = orchestrator.cost
    print(f"\n{'='*60}")
    if success:
        print(f"  PIPELINE TAMAMLANDI")
    else:
        print(f"  PIPELINE BASARISIZ — state kaydedildi, tekrar calistirabilirsiniz")
    print(f"{'='*60}")
    print(f"  Gorsel       : ~${COST_IMAGE:.2f}")
    print(f"  Video (5sn)  : ~${COST_VIDEO_5S:.2f}")
    print(f"  Muzik (10x)  : ~${COST_MUSIC_TRACK * 10:.2f}")
    print(f"  TOPLAM       : ${cost.total:.3f}")
    print(f"  Kalan limit  : ${cost.remaining():.3f}")
    if cost.remaining() < 0.50:
        print(f"  UYARI: $2 limitine yaklasiliyor!")
    if success:
        print(f"  Cikti : {state['artifacts']['final_video_path']}")
        print(f"  State : {state_path}")
    print(f"{'='*60}\n")
    sys.stdout.flush()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
