"""
Badger Tape — Music Agent
MiniMax Music v2 ile 10 lofi parçasını ThreadPoolExecutor(max_workers=3) ile paralel üretir.
"""

import random
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fal_client

from agents.common import (
    COST_MUSIC_TRACK,
    CostTracker, log, save_state, save_error, get_duration,
)

# ── Music Variation Pool ──────────────────────────────────────────────────────

BPM_OPTIONS = [65, 68, 70, 72, 75, 78, 80, 82, 85]

INSTRUMENTS = [
    "muted jazz piano",
    "Rhodes electric piano",
    "soft acoustic guitar and piano",
    "vibraphone and muted piano",
    "upright piano with felt damper",
]

MOODS = [
    "melancholic", "nostalgic", "introspective",
    "peaceful", "bittersweet", "contemplative",
]

REFERENCES = [
    "Nujabes inspired",
    "J Dilla influenced",
    "late night Tokyo atmosphere",
    "Thelonious Monk chord voicings",
    "Satie-like minimalism",
]

# Season-specific overrides for atmosphere, mood and instruments
SEASON_MUSIC = {
    "spring": {
        "atmosphere": "sunny spring morning in a park, cherry blossom breeze, warm fresh air",
        "extra_instruments": ["soft acoustic guitar and gentle flute", "acoustic guitar and mellow piano", "kalimba and soft piano"],
        "moods": ["peaceful", "hopeful", "warm", "light", "breezy", "nostalgic"],
        "bpm_options": [70, 72, 75, 78, 80],
    },
    "summer": {
        "atmosphere": "warm summer evening, city lights, humid night air",
        "extra_instruments": None,
        "moods": None,
        "bpm_options": None,
    },
    "autumn": {
        "atmosphere": "late night rainy window atmosphere, bedroom producer aesthetic",
        "extra_instruments": None,
        "moods": None,
        "bpm_options": None,
    },
    "winter": {
        "atmosphere": "cold winter night, snowflakes on window, warm indoor glow",
        "extra_instruments": ["soft piano with gentle bells", "Rhodes electric piano and soft strings"],
        "moods": ["melancholic", "cozy", "introspective", "peaceful", "nostalgic"],
        "bpm_options": [65, 68, 70, 72],
    },
}

LYRICS_PROMPT_INSTRUMENTAL = "[Intro]\n[Inst]\n[Verse]\n[Inst]\n[Bridge]\n[Inst]\n[Outro]\n[Inst]"

# Vokal lofi için: gerçek söz yapısı, yumuşak kadın veya erkek vokal
LYRICS_PROMPT_VOCAL = "[Intro]\n[Verse]\n[Chorus]\n[Verse]\n[Chorus]\n[Bridge]\n[Chorus]\n[Outro]"

# Vokal lofi için özel prompt şablonları
VOCAL_PROMPTS = [
    "Lofi hip hop, soft female vocals, {bpm} BPM, {instrument}, warm upright bass, soft brushed snare, hi-hat with subtle swing, {mood} mood, {atmosphere}, dreamy reverb on vocals, intimate whispery delivery, lyrics about drifting and letting go, minimal vinyl crackle, subtle tape hiss, clean balanced mix",
    "Lofi hip hop, gentle male vocals, {bpm} BPM, {instrument}, warm upright bass, lo-fi drums, {mood} mood, {atmosphere}, soft falsetto, introspective lyrics about late nights and quiet mornings, vinyl crackle, tape hiss, bedroom recording aesthetic",
    "Lofi hip hop, breathy female vocals, {bpm} BPM, {instrument}, soft bass, brushed snare, {mood} mood, {atmosphere}, jazzy vocal phrasing, lyrics about seasons changing and peaceful moments, warm low-pass filter, subtle reverb, cozy intimate feel",
    "Lofi hip hop, soft androgynous vocals, {bpm} BPM, {instrument}, warm bass, swung hi-hat, {mood} mood, {atmosphere}, hazy dream-like vocal texture, lyrics about stillness and nostalgia, minimal vinyl crackle, tape saturation, clean mix",
]

TOTAL_TRACKS = 10
MIN_TRACKS   = 7


# ── Agent ─────────────────────────────────────────────────────────────────────

class MusicAgent:
    """
    Adim 4: 10 lofi parçasını ThreadPoolExecutor(max_workers=3) ile paralel üretir.
    Image agent ile AYNI ANDA başlar (orchestrator tarafından thread'de çalıştırılır).
    """

    def run(
        self,
        state: dict,
        state_path: Path,
        session_dir: dict,
        cost_tracker: CostTracker,
        lock: threading.Lock,
    ) -> tuple[bool, str | None]:

        if state["steps"]["music_generated"]:
            log("MusicAgent", "Adim 4 — atlanıyor (tamamlandi)")
            return True, None

        log("MusicAgent", f"Adim 4 — {TOTAL_TRACKS} parca uretiliyor (ThreadPoolExecutor max_workers=3)")

        season = state.get("season", "autumn").lower()
        season_cfg = SEASON_MUSIC.get(season, SEASON_MUSIC["autumn"])
        season_atmosphere  = season_cfg["atmosphere"]
        season_moods       = season_cfg["moods"] or MOODS
        season_bpms        = season_cfg["bpm_options"] or BPM_OPTIONS
        season_instruments = (INSTRUMENTS + season_cfg["extra_instruments"]) if season_cfg.get("extra_instruments") else INSTRUMENTS

        vocal_mode = state.get("vocal", False)
        log("MusicAgent", f"  Mevsim: {season} — atmosfer: {season_atmosphere[:60]}...")
        log("MusicAgent", f"  Mod: {'VOKAL' if vocal_mode else 'instrumental'}")

        # Pre-generate track parameters so each worker gets stable values
        track_params = []
        for i in range(1, TOTAL_TRACKS + 1):
            bpm        = random.choice(season_bpms)
            instrument = random.choice(season_instruments)
            mood       = random.choice(season_moods)
            reference  = random.choice(REFERENCES)

            if vocal_mode:
                template = random.choice(VOCAL_PROMPTS)
                prompt   = template.format(
                    bpm=bpm,
                    instrument=instrument,
                    mood=mood,
                    atmosphere=season_atmosphere,
                )
                lyrics = LYRICS_PROMPT_VOCAL
            else:
                prompt = (
                    f"Lofi hip hop, instrumental, no vocals, {bpm} BPM, "
                    f"{instrument}, warm upright bass, soft brushed snare, "
                    f"hi-hat with subtle swing, {mood} mood, "
                    f"{season_atmosphere}, "
                    f"{reference}, minimal vinyl crackle, subtle tape hiss, "
                    f"clean balanced mix, warm low-pass filter, no distortion"
                )
                lyrics = LYRICS_PROMPT_INSTRUMENTAL

            track_params.append((i, bpm, instrument, mood, prompt, lyrics))

        tracks: list[Path] = []
        failed = 0
        results_lock = threading.Lock()

        def produce_track(params):
            nonlocal failed
            idx, bpm, instrument, mood, prompt, lyrics = params
            log("MusicAgent", f"  Track {idx:02d}/10 — {bpm} BPM, {instrument}, {mood}")
            try:
                cost_tracker.check(COST_MUSIC_TRACK)

                result = fal_client.subscribe(
                    "fal-ai/minimax-music/v2",
                    arguments={
                        "prompt": prompt,
                        "lyrics_prompt": lyrics,
                    },
                    with_logs=False,
                    timeout=180,
                )

                cost_tracker.record(COST_MUSIC_TRACK, f"minimax-music track {idx:02d}")

                audio_url = result.get("audio", {}).get("url") or result.get("url")
                if not audio_url:
                    raise ValueError(f"Audio URL bulunamadi: {result}")

                out_path = session_dir["music"] / f"track_{idx:02d}.mp3"
                urllib.request.urlretrieve(audio_url, out_path)

                duration = get_duration(out_path)
                size_mb  = out_path.stat().st_size / 1024 / 1024
                log("MusicAgent", f"  track_{idx:02d}.mp3 — {duration:.1f}s, {size_mb:.2f}MB")

                return out_path

            except Exception as e:
                log("MusicAgent", f"  UYARI: Track {idx:02d} basarisiz — {e}")
                return None

        progress_path = session_dir["output"] / "step_progress.json"

        def _write_music_progress():
            with results_lock:
                done = len(tracks)
            pct = int(done / TOTAL_TRACKS * 100)
            try:
                import json as _json
                progress_path.write_text(
                    _json.dumps({"step": "music", "done_tracks": done, "total_tracks": TOTAL_TRACKS, "percent": pct}),
                    encoding="utf-8",
                )
            except Exception:
                pass

        _write_music_progress()

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(produce_track, p): p[0] for p in track_params}
            for future in as_completed(futures):
                result_path = future.result()
                if result_path is not None:
                    with results_lock:
                        tracks.append(result_path)
                else:
                    with results_lock:
                        failed += 1
                _write_music_progress()

        tracks.sort(key=lambda p: p.name)
        log("MusicAgent", f"  Uretilen: {len(tracks)}/{TOTAL_TRACKS} parca ({failed} basarisiz)")

        if len(tracks) < MIN_TRACKS:
            msg = f"En az {MIN_TRACKS} parca gerekli, sadece {len(tracks)} basarili"
            save_error(state, state_path, "music_generated", msg, lock)
            return False, msg

        total_music_cost = COST_MUSIC_TRACK * len(tracks)
        with lock:
            state["steps"]["music_generated"] = True
            state["artifacts"]["music_paths"] = [str(t) for t in tracks]
            state["cost_tracker"]["music"] += total_music_cost
            state["cost_tracker"]["total"] = cost_tracker.total
        save_state(state, state_path, lock)

        log("MusicAgent", f"  Tahmini toplam muzik maliyeti: ~${total_music_cost:.3f}")
        return True, None
