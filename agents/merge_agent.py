"""
Badger Tape — Merge Agent
FFmpeg ile loop video + müzik parçalarını birleştirir, kalite doğrulaması yapar.
"""

import json
import random
import threading
from datetime import datetime
from pathlib import Path

from agents.common import (
    BASE_DIR,
    CostTracker, log, save_state, save_error,
    run_ffmpeg, run_ffmpeg_with_progress, get_duration,
)

# ── Metadata Templates ────────────────────────────────────────────────────────

TITLE_TEMPLATES_BY_SEASON = {
    "spring": [
        "pov: you're lying in the park reading your favorite book 🌸",
        "spring mornings with badger 🌸 lofi beats to drift away to",
        "cherry blossom lofi 🌸 1 hour of cozy spring beats",
        "badger tape 🎧 spring lofi mix — beats to bloom to",
        "pov: it's finally warm outside and you don't want to move 🌿",
    ],
    "summer": [
        "pov: it's a lazy summer afternoon and nothing needs doing ☀️",
        "badger tape 🎧 summer lofi mix — beats to drift away to",
        "summer nights with badger 🌙 1 hour lofi beats",
        "pov: windows open, fan on, no plans ☀️ lofi beats",
    ],
    "autumn": [
        "pov: it's 3am and you can't sleep 🌙 lofi beats for the quiet hours",
        "autumn lofi mix 🍂 1 hour beats to drift away to",
        "midnight rainy night 🌧 lofi hip hop beats to relax to",
        "badger tape 🎧 beats to study to [1 hour lofi]",
        "pov: coffee in hand, rain on the window 🍂 lofi beats",
    ],
    "winter": [
        "winter nights with badger ❄️ 1 hour lofi beats to drift away to",
        "pov: it's snowing outside and you have nowhere to be ❄️",
        "badger tape 🎧 cozy winter lofi — beats to stay in to",
        "midnight winter 🌙 lofi hip hop beats to relax to",
    ],
}

# fallback for unknown seasons
TITLE_TEMPLATES_DEFAULT = [
    "badger tape 🎧 beats to study to [1 hour lofi]",
    "pov: it's 3am and you can't sleep 🌙 lofi beats for the quiet hours",
    "{season} lofi mix — 1 hour beats to drift away to",
]

TARGET_DURATION_SEC = 3600
DURATION_TOLERANCE  = 5  # ±5 saniye


def _load_channel_description() -> str:
    desc_path = BASE_DIR / "channel-description.md"
    if not desc_path.exists():
        log("MergeAgent", "UYARI: channel-description.md bulunamadi, varsayilan kullaniliyor")
        return (
            "welcome to badger tape. 🎧\n\n"
            "lofi beats to study, sleep & drift away to.\n"
            "press play. let it run. drift away, one beat at a time.\n\n"
            "— badger tape 🎧"
        )

    content = desc_path.read_text(encoding="utf-8")
    in_long = in_code = False
    lines = []
    for line in content.splitlines():
        if "Uzun Versiyon" in line:
            in_long = True
            continue
        if in_long and line.strip() == "```" and not in_code:
            in_code = True
            continue
        if in_long and in_code and line.strip() == "```":
            break
        if in_long and in_code:
            lines.append(line)

    return "\n".join(lines).strip() if lines else content.strip()


# ── Agent ─────────────────────────────────────────────────────────────────────

class MergeAgent:
    """
    Adim 5: FFmpeg ile video+müzik birleştirir ve kalite doğrulaması yapar.
    Adim 6: Metadata JSON oluşturur.
    """

    def run(
        self,
        state: dict,
        state_path: Path,
        session_dir: dict,
        cost_tracker: CostTracker,
        lock: threading.Lock,
    ) -> tuple[bool, str | None]:

        loop_path  = Path(state["artifacts"]["video_path"])
        tracks     = [Path(p) for p in state["artifacts"]["music_paths"]]
        temp_dir   = session_dir["temp"]
        out_dir    = session_dir["output"]

        # ── Adim 5: FFmpeg Birlestirme ────────────────────────────────────────
        if not state["steps"]["ffmpeg_merged"]:
            log("MergeAgent", "Adim 5 — FFmpeg birlestirme basliyor")
            try:
                progress_path = out_dir / "step_progress.json"
                final_path = self._ffmpeg_merge(loop_path, tracks, temp_dir, out_dir, progress_path)
                with lock:
                    state["steps"]["ffmpeg_merged"] = True
                    state["artifacts"]["final_video_path"] = str(final_path)
                save_state(state, state_path, lock)
            except Exception as e:
                msg = f"FFmpeg birlestirme basarisiz: {e}"
                save_error(state, state_path, "ffmpeg_merged", msg, lock)
                return False, msg
        else:
            log("MergeAgent", "Adim 5 — atlanıyor (tamamlandi)")

        # ── Adim 6: Metadata ──────────────────────────────────────────────────
        if not state["steps"]["metadata_created"]:
            log("MergeAgent", "Adim 6 — metadata olusturuluyor")
            try:
                metadata_path = self._generate_metadata(state, out_dir)
                with lock:
                    state["steps"]["metadata_created"] = True
                    state["artifacts"]["metadata_path"] = str(metadata_path)
                save_state(state, state_path, lock)
            except Exception as e:
                msg = f"Metadata olusturma basarisiz: {e}"
                save_error(state, state_path, "metadata_created", msg, lock)
                return False, msg
        else:
            log("MergeAgent", "Adim 6 — atlanıyor (tamamlandi)")

        return True, None

    # ── Private ───────────────────────────────────────────────────────────────

    def _ffmpeg_merge(
        self,
        loop_path: Path,
        tracks: list[Path],
        temp_dir: Path,
        out_dir: Path,
        progress_path: Path | None = None,
    ) -> Path:
        # 5a — Muzik concat listesi
        log("MergeAgent", "  5a — muzik concat listesi olusturuluyor")
        track_durations = {t: get_duration(t) for t in tracks}
        total_music_dur = sum(track_durations.values())
        log("MergeAgent", f"  Toplam parca suresi: {total_music_dur:.1f}s")

        concat_lines = []
        accumulated  = 0.0
        while accumulated < TARGET_DURATION_SEC:
            for t in tracks:
                concat_lines.append(f"file '{t.as_posix()}'")
                accumulated += track_durations[t]
                if accumulated >= TARGET_DURATION_SEC:
                    break

        music_list_path = temp_dir / "music_list.txt"
        music_list_path.write_text("\n".join(concat_lines), encoding="utf-8")
        log("MergeAgent", f"  Concat listesi: {len(concat_lines)} girdi ({accumulated:.1f}s)")

        # 5b — 10 saniyelik test
        log("MergeAgent", "  5b — 10 saniyelik test videosu")
        test_path = temp_dir / "test_10sec.mp4"
        run_ffmpeg(
            [
                "-stream_loop", "-1", "-i", str(loop_path),
                "-f", "concat", "-safe", "0", "-stream_loop", "-1",
                "-i", str(music_list_path),
                "-t", "10",
                "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                "-c:a", "aac", "-b:a", "192k",
                str(test_path),
            ],
            "10sn test video",
        )
        if not test_path.exists() or test_path.stat().st_size == 0:
            raise RuntimeError("Test videosu olusturulamadi")

        test_dur = get_duration(test_path)
        log("MergeAgent", f"  Test basarili: {test_dur:.1f}s, {test_path.stat().st_size / 1024:.0f}KB")

        # 5c — 1 saatlik final video
        log("MergeAgent", "  5c — 1 saatlik final video uretiliyor (uzun surebilir)")
        final_path = out_dir / "badgertape_1hour.mp4"
        ffmpeg_args = [
            "-stream_loop", "-1", "-i", str(loop_path),
            "-f", "concat", "-safe", "0", "-stream_loop", "-1",
            "-i", str(music_list_path),
            "-t", str(TARGET_DURATION_SEC),
            "-c:v", "libx264", "-crf", "23", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            str(final_path),
        ]
        if progress_path:
            run_ffmpeg_with_progress(ffmpeg_args, "1 saatlik final video", progress_path, TARGET_DURATION_SEC)
        else:
            run_ffmpeg(ffmpeg_args, "1 saatlik final video")

        # 5d — Sure dogrulama
        final_dur = get_duration(final_path)
        log("MergeAgent", f"  Final video suresi: {final_dur:.1f}s")
        if not (TARGET_DURATION_SEC - DURATION_TOLERANCE <= final_dur <= TARGET_DURATION_SEC + DURATION_TOLERANCE):
            raise RuntimeError(
                f"Sure dogrulamasi basarisiz. "
                f"Beklenen: {TARGET_DURATION_SEC-DURATION_TOLERANCE}-{TARGET_DURATION_SEC+DURATION_TOLERANCE}s, "
                f"alinan: {final_dur:.1f}s"
            )

        size_mb = final_path.stat().st_size / 1024 / 1024
        log("MergeAgent", f"  Cikti: {final_path} ({size_mb:.1f} MB)")
        return final_path

    def _generate_metadata(self, state: dict, out_dir: Path) -> Path:
        theme  = state["theme"]
        season = state["season"]

        analiz_path = BASE_DIR / "lofi-girl-analiz.md"
        if analiz_path.exists():
            log("MergeAgent", "  lofi-girl-analiz.md okundu (referans)")

        templates = TITLE_TEMPLATES_BY_SEASON.get(season.lower(), TITLE_TEMPLATES_DEFAULT)
        title     = random.choice(templates).format(season=season.lower(), theme=theme.lower())

        episode_counter_path = BASE_DIR / "episode_counter.txt"
        if episode_counter_path.exists():
            episode_number = int(episode_counter_path.read_text(encoding="utf-8").strip()) + 1
        else:
            episode_number = 1
        episode_counter_path.write_text(str(episode_number), encoding="utf-8")
        title = title + f" #{episode_number}"

        description = _load_channel_description()

        tags = [
            "lofi", "lofi hip hop", "study music", "chill beats",
            "sleep music", "lofi mix", "badger tape",
            theme.lower(), season.lower(),
            "1 hour lofi", "beats to study to", "relaxing music",
        ]

        metadata = {
            "title":          title,
            "description":    description,
            "tags":           tags,
            "category_id":    "10",
            "privacy_status": "private",
        }

        out_path = out_dir / "metadata.json"
        out_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        log("MergeAgent", f"  Baslik: {title}")
        log("MergeAgent", f"  Kaydedildi: {out_path}")
        return out_path
