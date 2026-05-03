"""
Badger Tape — Full Content Pipeline
Usage: python scripts/pipeline.py --theme "rainy night" --season "autumn"
"""

import argparse
import io
import json
import os
import random
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

# Windows terminal emoji desteği
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from dotenv import load_dotenv

load_dotenv()

FAL_KEY = os.getenv("FAL_KEY")
if not FAL_KEY:
    print("ERROR: FAL_KEY not found in .env")
    sys.exit(1)

import fal_client

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BASE_DIR = Path(__file__).resolve().parent.parent
REFERENCES_DIR = BASE_DIR / "references"
IMAGES_DIR = BASE_DIR / "images"
VIDEOS_DIR = BASE_DIR / "videos"
MUSIC_DIR = BASE_DIR / "music"
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR = BASE_DIR / "temp"

SPENDING_LIMIT = 2.00
total_spent = 0.0

# Cost estimates
COST_IMAGE = 0.08
COST_VIDEO_5S = 0.35
COST_VIDEO_10S = 0.49
COST_MUSIC_TRACK = 0.035

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Progress & State Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
    "music_generated":    "Muzik uretiliyor... (~15-20 dk)",
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def log(step, msg):
    print(f"[{step}] {msg}")
    sys.stdout.flush()


def check_spending(about_to_spend):
    global total_spent
    projected = total_spent + about_to_spend
    if projected > SPENDING_LIMIT:
        print(
            f"\nERROR: $2 harcama limiti asilacak! "
            f"Simdiye kadar: ${total_spent:.3f}, "
            f"eklenecek: ${about_to_spend:.3f}, "
            f"limit: ${SPENDING_LIMIT:.2f}"
        )
        print("Pipeline durduruluyor.")
        sys.exit(1)
    if projected > SPENDING_LIMIT * 0.8:
        print(
            f"  WARNING: Limite yaklasildi! "
            f"Tahmini toplam: ${projected:.3f} / ${SPENDING_LIMIT:.2f}"
        )


def record_spending(amount, label):
    global total_spent
    total_spent += amount
    print(f"  [harcama] +${amount:.3f} ({label}) | toplam: ${total_spent:.3f}")
    sys.stdout.flush()


def run_ffmpeg(args_list, label):
    cmd = ["ffmpeg", "-y"] + args_list
    log("FFmpeg", label)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: FFmpeg basarisiz — {label}")
        print(result.stderr[-2000:] if result.stderr else "No stderr output")
        sys.exit(1)


def get_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        print(f"ERROR: ffprobe sure okunamadi: {path}")
        sys.exit(1)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session State Yönetimi
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def make_session_dir(session_id):
    """session_id'den session_dir dict'i oluştur ve klasörleri yarat."""
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


def create_state(session_id, theme, season, vocal=False):
    return {
        "session_id": session_id,
        "theme": theme,
        "season": season,
        "vocal": vocal,
        "steps": {
            "variation_selected": False,
            "image_generated":    False,
            "video_generated":    False,
            "music_generated":    False,
            "ffmpeg_merged":      False,
            "metadata_created":   False,
            "uploaded":           False,
        },
        "artifacts": {
            "variations":      None,
            "image_path":      None,
            "video_path":      None,
            "music_paths":     [],
            "final_video_path": None,
            "metadata_path":   None,
            "youtube_url":     None,
        },
        "cost_tracker": {
            "image": 0.0,
            "video": 0.0,
            "music": 0.0,
            "total": 0.0,
        },
        "last_error": None,
    }


def save_state(state, state_path):
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.flush()


def load_state(state_path):
    return json.loads(state_path.read_text(encoding="utf-8"))


def find_incomplete_sessions():
    """OUTPUT_DIR içinde uploaded=False olan session'ları bul."""
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


def save_error_to_state(state, state_path, step, message):
    state["last_error"] = {
        "step": step,
        "message": message,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_state(state, state_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Progress Bar
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def print_progress(state, active_step, start_time):
    steps = state["steps"]
    cost = state["cost_tracker"]["total"]

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

    print("\n" + "━" * 42)
    print(" BADGER TAPE PIPELINE")
    print("━" * 42)
    print(f" [{bar}] %{pct:3d} | Adim {completed}/{total_steps}")

    for i, step in enumerate(STEP_NAMES):
        label = STEP_LABELS[step]
        if steps.get(step, False):
            icon = "✅"
            print(f" {icon} {label}")
        elif step == active_step:
            icon = "🔄"
            print(f" {icon} {STEP_ACTIVE_LABELS[step]}")
        else:
            icon = "⏳"
            print(f" {icon} {label}")

    print("━" * 42)
    print(f" Gecen sure: {format_duration(elapsed)} | Tahmini kalan: ~{format_duration(remaining_sec)}")
    print(f" Harcama: ${cost:.2f} / ${SPENDING_LIMIT:.2f} limit")
    print("━" * 42 + "\n")
    sys.stdout.flush()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADIM 1 — VARYASYON SEC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ACTIVITIES = [
    {
        "desc": "reading an open book, left paw resting on pages, eyes focused downward",
        "motion": "book pages slightly rustling",
    },
    {
        "desc": "writing in a notebook with right paw holding a pen, focused expression",
        "motion": "paw gently moving across paper",
    },
    {
        "desc": "looking out the rainy window with a distant melancholic gaze",
        "motion": "cat's gaze drifting slowly",
    },
    {
        "desc": "holding a steaming coffee mug with both paws, eyes half-closed",
        "motion": "paws warming around mug",
    },
    {
        "desc": "listening to music with eyes closed, head slightly tilted, peaceful expression",
        "motion": "subtle head sway with music",
    },
    {
        "desc": "working on a vintage typewriter, paws on keys",
        "motion": "paws moving on typewriter keys",
    },
    {
        "desc": "sketching in a sketchbook with a pencil",
        "motion": "paw making slow sketch movements",
    },
]

LIGHTING = [
    "warm orange banker lamp on the left, cool blue moonlight from window on the right",
    "single candle flame on desk, soft amber glow filling the room",
    "warm yellow fairy lights strung above, gentle lamp in corner",
    "neon purple and blue city lights bleeding through rainy window",
]

BACKGROUNDS = [
    "bookshelves packed with books and plants behind",
    "vintage posters and vinyl records mounted on wall behind",
    "minimalist Japanese-style room with wooden elements",
    "cozy attic room with slanted ceiling",
]

SEASON_ATMOSPHERE = {
    "autumn": "autumn leaves stuck to wet window, orange and red hues outside",
    "winter": "frost on window edges, snowflakes drifting outside, cold blue light",
    "spring": "cherry blossom petals drifting gently in the breeze, soft warm sunlight through tree canopy",
    "summer": "heavy summer rain on window, warm humid glow outside",
}

OUTDOOR_SCENES = {
    "spring": {
        "core": (
            "lying on a soft picnic blanket spread on lush green grass in a park, "
            "reading a book with left paw resting on the open pages, eyes focused downward"
        ),
        "props": (
            "small wicker basket nearby, a thermos of tea beside the blanket, "
            "wildflowers dotting the grass, cherry blossom tree overhead"
        ),
        "lighting": "warm golden afternoon sunlight filtering through cherry blossom branches, soft dappled light",
        "motion": "cherry blossom petals drifting slowly across the frame",
    }
}

SEASON_MUSIC_ATMOSPHERE = {
    "autumn": "late night rainy window atmosphere, bedroom producer aesthetic",
    "winter": "cold winter night, snowflakes on window, warm indoor glow",
    "spring": "sunny spring morning in a park, cherry blossom breeze, warm fresh air",
    "summer": "warm summer evening, city lights, humid night air",
}


def select_variations(season):
    season_lower = season.lower()
    activity = random.choice(ACTIVITIES)

    if season_lower in OUTDOOR_SCENES:
        outdoor = OUTDOOR_SCENES[season_lower]
        return {
            "activity_desc":     outdoor["core"],
            "activity_motion":   outdoor["motion"],
            "lighting":          outdoor["lighting"],
            "background":        outdoor["props"],
            "season_atmosphere": SEASON_ATMOSPHERE[season_lower],
            "is_outdoor":        True,
        }

    return {
        "activity_desc":  activity["desc"],
        "activity_motion": activity["motion"],
        "lighting":        random.choice(LIGHTING),
        "background":      random.choice(BACKGROUNDS),
        "season_atmosphere": SEASON_ATMOSPHERE.get(season_lower, SEASON_ATMOSPHERE["autumn"]),
        "is_outdoor":      False,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADIM 2 — GORSEL URETIMI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def generate_image(variations, session_dir):
    log("ADIM 2", "Gorsel uretiliyor — fal.ai Nano Banana 2")

    check_spending(COST_IMAGE)

    avatar_path = REFERENCES_DIR / "avatar.png"
    if not avatar_path.exists():
        print("ERROR: references/avatar.png bulunamadi")
        sys.exit(1)

    if variations.get("is_outdoor"):
        prompt = (
            f"The exact same striped tabby cat from the reference image, "
            f"{variations['activity_desc']}, "
            f"wearing large vintage over-ear headphones on head, "
            f"gold chain necklace around neck, orange bracelet on right front paw, "
            f"absolutely NO earring, NO hoop earring on ears, "
            f"{variations['lighting']}, "
            f"{variations['season_atmosphere']}, "
            f"{variations['background']}, "
            f"lofi aesthetic, anime style, studio ghibli inspired, "
            f"cinematic composition, soft depth of field, warm spring color grading, "
            f"highly detailed, 16:9 widescreen"
        )
    else:
        prompt = (
            f"The exact same striped tabby cat from the reference image, "
            f"{variations['activity_desc']}, sitting at wooden desk in 3/4 angle view, "
            f"wearing large vintage over-ear headphones on head, "
            f"gold chain necklace around neck, orange bracelet on right front paw, "
            f"absolutely NO earring, NO hoop earring on ears, "
            f"{variations['lighting']}, "
            f"{variations['season_atmosphere']}, rain drops on window glass, "
            f"cassette tapes and vinyl records scattered on desk, steaming coffee mug on desk corner, "
            f"{variations['background']}, "
            f"lofi aesthetic, anime style, studio ghibli inspired, "
            f"cinematic composition, soft depth of field, warm color grading, "
            f"highly detailed, 16:9 widescreen"
        )

    log("ADIM 2", f"Prompt: {prompt[:120]}...")

    # Reference image upload
    with open(avatar_path, "rb") as f:
        image_url = fal_client.upload(f.read(), "image/png")

    result = fal_client.subscribe(
        "fal-ai/nano-banana-2",
        arguments={
            "prompt": prompt,
            "image_url": image_url,
            "image_size": "landscape_16_9",
            "num_inference_steps": 28,
            "guidance_scale": 7.5,
            "num_images": 1,
        },
        with_logs=False,
    )

    record_spending(COST_IMAGE, "nano-banana-2")
    log("ADIM 2", f"Tahmini harcama: ~${COST_IMAGE}")

    # Extract image URL from response
    img_url = (
        result.get("images", [{}])[0].get("url")
        or result.get("image", {}).get("url")
        or result.get("url")
    )
    if not img_url:
        print(f"ERROR: Gorsel URL'si bulunamadi: {result}")
        sys.exit(1)

    out_path = session_dir["images"] / "scene.png"
    urllib.request.urlretrieve(img_url, out_path)
    log("ADIM 2", f"Kaydedildi: {out_path}")
    return out_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADIM 3 — LOOP VIDEO
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def generate_video(scene_path, variations, session_dir):
    log("ADIM 3", "Loop video uretiliyor — fal.ai Kling v2.1 Pro")

    check_spending(COST_VIDEO_5S)

    if variations.get("is_outdoor"):
        motion_prompt = (
            f"Cherry blossom petals drifting slowly from right to left across frame, "
            f"gentle breeze rippling the edges of the picnic blanket, "
            f"dappled sunlight flickering softly through tree canopy overhead, "
            f"cat breathing slowly with subtle chest movement, "
            f"{variations['activity_motion']}, "
            f"grass blades swaying lightly in breeze, "
            f"seamless natural loop — last frame matches first frame perfectly, "
            f"no camera movement, locked static wide shot, "
            f"cinematic quality, smooth motion"
        )
    else:
        motion_prompt = (
            f"Rain drops slowly trickling down window glass, "
            f"steam gently curling and rising from coffee mug, "
            f"cat breathing slowly with subtle chest movement, "
            f"{variations['activity_motion']}, "
            f"{variations['lighting']} softly flickering with warm glow, "
            f"plant leaves gently swaying if visible, "
            f"seamless natural loop — last frame matches first frame perfectly, "
            f"no camera movement, locked static wide shot, "
            f"cinematic quality, smooth motion"
        )

    # Upload scene image
    with open(scene_path, "rb") as f:
        image_url = fal_client.upload(f.read(), "image/png")

    result = fal_client.subscribe(
        "fal-ai/kling-video/v2.1/pro/image-to-video",
        arguments={
            "image_url": image_url,
            "prompt": motion_prompt,
            "duration": "5",
            "aspect_ratio": "16:9",
            "negative_prompt": (
                "blur, distort, low quality, artifacts, jitter, warping, "
                "morphing, text, watermark, extra limbs, "
                "steam from book, steam from notebook, steam from typewriter, "
                "smoke from paper, vapor from objects other than coffee mug"
            ),
            "cfg_scale": 0.5,
            "generate_audio": False,
        },
        with_logs=False,
    )

    record_spending(COST_VIDEO_5S, "kling-video/v2.1/pro (5s)")
    log("ADIM 3", f"Tahmini harcama: ~${COST_VIDEO_5S}")

    video_url = result.get("video", {}).get("url") or result.get("url")
    if not video_url:
        print(f"ERROR: Video URL'si bulunamadi: {result}")
        sys.exit(1)

    out_path = session_dir["videos"] / "loop.mp4"
    urllib.request.urlretrieve(video_url, out_path)
    log("ADIM 3", f"Kaydedildi: {out_path}")
    return out_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADIM 4 — MUZIK URETIMI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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

LYRICS_PROMPT = "[Intro]\n[Inst]\n[Verse]\n[Inst]\n[Bridge]\n[Inst]\n[Outro]\n[Inst]"


def generate_music(session_dir, season="autumn"):
    log("ADIM 4", "10 muzik parcasi uretiliyor — MiniMax Music v2")

    season_lower = season.lower()
    atmosphere = SEASON_MUSIC_ATMOSPHERE.get(season_lower, SEASON_MUSIC_ATMOSPHERE["autumn"])
    log("ADIM 4", f"Mevsim: {season_lower} — atmosfer: {atmosphere[:60]}...")

    tracks = []
    failed = 0

    for i in range(1, 11):
        bpm = random.choice(BPM_OPTIONS)
        instrument = random.choice(INSTRUMENTS)
        mood = random.choice(MOODS)
        reference = random.choice(REFERENCES)

        prompt = (
            f"Lofi hip hop, instrumental, no vocals, {bpm} BPM, "
            f"{instrument}, warm upright bass, soft brushed snare, "
            f"hi-hat with subtle swing, {mood} mood, "
            f"{atmosphere}, "
            f"{reference}, minimal vinyl crackle, subtle tape hiss, "
            f"clean balanced mix, warm low-pass filter, no distortion"
        )

        check_spending(COST_MUSIC_TRACK)

        log("ADIM 4", f"Track {i:02d}/10 — {bpm} BPM, {instrument}, {mood}")

        try:
            result = fal_client.subscribe(
                "fal-ai/minimax-music/v2",
                arguments={
                    "prompt": prompt,
                    "lyrics_prompt": LYRICS_PROMPT,
                },
                with_logs=False,
            )

            record_spending(COST_MUSIC_TRACK, f"minimax-music track {i:02d}")

            audio_url = result.get("audio", {}).get("url") or result.get("url")
            if not audio_url:
                raise ValueError(f"Audio URL bulunamadi: {result}")

            out_path = session_dir["music"] / f"track_{i:02d}.mp3"
            urllib.request.urlretrieve(audio_url, out_path)

            duration = get_duration(out_path)
            size_mb = out_path.stat().st_size / 1024 / 1024
            log("ADIM 4", f"  Kaydedildi: track_{i:02d}.mp3 ({duration:.1f}s, {size_mb:.2f}MB)")
            tracks.append(out_path)

        except Exception as e:
            failed += 1
            log("ADIM 4", f"  UYARI: Track {i:02d} basarisiz — {e}")

    if len(tracks) < 7:
        print(f"ERROR: En az 7 parca gerekli, sadece {len(tracks)} basarili. Pipeline durduruluyor.")
        sys.exit(1)

    log("ADIM 4", f"Uretilen: {len(tracks)}/10 parca ({failed} basarisiz)")
    log("ADIM 4", f"Tahmini harcama: 10 parca x ${COST_MUSIC_TRACK} = ${COST_MUSIC_TRACK * 10:.2f}")
    return tracks


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADIM 5 — FFMPEG BIRLESTIRME
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def combine_video_music(loop_path, tracks, session_dir):
    log("ADIM 5", "Video ve muzik birlestiriliyor — FFmpeg")

    temp_dir = session_dir["temp"]
    out_dir = session_dir["output"]

    # 5a — Muzik listesi dosyasi olustur
    log("ADIM 5a", "Muzik concat listesi olusturuluyor")
    track_durations = {t: get_duration(t) for t in tracks}
    total_track_duration = sum(track_durations.values())
    log("ADIM 5a", f"Toplam parca suresi: {total_track_duration:.1f}s")

    concat_lines = []
    accumulated = 0.0
    while accumulated < 3600:
        for t in tracks:
            concat_lines.append(f"file '{t.as_posix()}'")
            accumulated += track_durations[t]
            if accumulated >= 3600:
                break

    music_list_path = temp_dir / "music_list.txt"
    music_list_path.write_text("\n".join(concat_lines), encoding="utf-8")
    log("ADIM 5a", f"Concat listesi: {len(concat_lines)} girdi ({accumulated:.1f}s)")

    # 5b — 10 saniyelik test
    log("ADIM 5b", "10 saniyelik test videosu uretiliyor")
    test_path = temp_dir / "test_10sec.mp4"
    run_ffmpeg(
        [
            "-stream_loop", "-1", "-i", str(loop_path),
            "-f", "concat", "-safe", "0", "-stream_loop", "-1",
            "-i", str(music_list_path),
            "-t", "10",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "320k",
            str(test_path),
        ],
        "10sn test video",
    )

    if not test_path.exists() or test_path.stat().st_size == 0:
        print("ERROR: Test videosu olusturulamadi. Pipeline durduruluyor.")
        sys.exit(1)

    test_dur = get_duration(test_path)
    log("ADIM 5b", f"Test basarili: {test_dur:.1f}s, {test_path.stat().st_size / 1024:.0f}KB")

    # 5c — 1 saatlik video
    log("ADIM 5c", "1 saatlik video uretiliyor (bu uzun surebilir)")
    final_path = out_dir / "badgertape_1hour.mp4"
    run_ffmpeg(
        [
            "-stream_loop", "-1", "-i", str(loop_path),
            "-f", "concat", "-safe", "0", "-stream_loop", "-1",
            "-i", str(music_list_path),
            "-t", "3600",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "320k",
            str(final_path),
        ],
        "1 saatlik final video",
    )

    # 5d — Dogrulama
    final_duration = get_duration(final_path)
    log("ADIM 5d", f"Final video suresi: {final_duration:.1f}s")

    if not (3595 <= final_duration <= 3605):
        print(
            f"ERROR: Sure dogrulamasi basarisiz. "
            f"Beklenen: 3595-3605s, alinan: {final_duration:.1f}s"
        )
        sys.exit(1)

    size_mb = final_path.stat().st_size / 1024 / 1024
    log("ADIM 5", f"Cikti: {final_path} ({size_mb:.1f} MB)")
    return final_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADIM 6 — METADATA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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

TITLE_TEMPLATES_DEFAULT = [
    "badger tape 🎧 beats to study to [1 hour lofi]",
    "pov: it's 3am and you can't sleep 🌙 lofi beats for the quiet hours",
    "{season} lofi mix — 1 hour beats to drift away to",
]


def load_channel_description():
    """channel-description.md dosyasindan uzun versiyonu oku."""
    desc_path = BASE_DIR / "channel-description.md"
    if not desc_path.exists():
        log("ADIM 6", "UYARI: channel-description.md bulunamadi, varsayilan aciklama kullaniliyor")
        return (
            "welcome to badger tape. 🎧\n\n"
            "lofi beats to study, sleep & drift away to.\n"
            "press play. let it run. drift away, one beat at a time.\n\n"
            "— badger tape 🎧"
        )

    content = desc_path.read_text(encoding="utf-8")

    # "Uzun Versiyon" basligi altindaki ``` blogu icerigini cikart
    in_long = False
    in_code = False
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

    if lines:
        return "\n".join(lines).strip()

    # Fallback: dosyanin tamami
    return content.strip()


def generate_metadata(theme, season, session_dir):
    log("ADIM 6", "Metadata olusturuluyor")

    # lofi-girl-analiz.md kontrol
    analiz_path = BASE_DIR / "lofi-girl-analiz.md"
    if analiz_path.exists():
        log("ADIM 6", "lofi-girl-analiz.md okundu (referans)")
    else:
        log("ADIM 6", "UYARI: lofi-girl-analiz.md bulunamadi")

    # Channel description
    description = load_channel_description()

    # Baslik sec
    templates = TITLE_TEMPLATES_BY_SEASON.get(season.lower(), TITLE_TEMPLATES_DEFAULT)
    title = random.choice(templates).format(
        season=season.lower(),
        theme=theme.lower(),
    )

    tags = [
        "lofi", "lofi hip hop", "study music", "chill beats",
        "sleep music", "lofi mix", "badger tape",
        theme.lower(), season.lower(),
        "1 hour lofi", "beats to study to", "relaxing music",
    ]

    metadata = {
        "title": title,
        "description": description,
        "tags": tags,
        "category_id": "10",
        "privacy_status": "private",
    }

    out_path = session_dir["output"] / "metadata.json"
    out_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    log("ADIM 6", f"Baslik: {title}")
    log("ADIM 6", f"Kaydedildi: {out_path}")
    return metadata, out_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADIM 7 — YOUTUBE UPLOAD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def upload_to_youtube(video_path, metadata_path):
    import re
    log("ADIM 7", "YouTube'a yukleniyor")

    upload_script = BASE_DIR / "scripts" / "youtube_upload.py"
    if not upload_script.exists():
        print("ERROR: scripts/youtube_upload.py bulunamadi")
        sys.exit(1)

    cmd = [
        sys.executable, str(upload_script),
        "--file", str(video_path),
        "--metadata", str(metadata_path),
    ]

    result = subprocess.run(cmd, capture_output=True, timeout=7200)
    stdout_text = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    print(stdout_text, end="")  # kullanıcıya göster

    if result.returncode != 0:
        stderr_text = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
        print(f"ERROR: YouTube upload basarisiz\n{stderr_text[-1000:]}")
        sys.exit(1)

    match = re.search(r"https://(?:www\.youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})", stdout_text)
    if match:
        url = f"https://www.youtube.com/watch?v={match.group(1)}"
        log("ADIM 7", f"YouTube URL: {url}")
        return url

    log("ADIM 7", "UYARI: YouTube URL stdout'tan alinamadi")
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEMIZLIK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def launch_monitor():
    monitor_script = BASE_DIR / "monitor.py"
    if not monitor_script.exists():
        return
    try:
        subprocess.Popen(
            f'start "Badger Tape Monitor" cmd /k "python \\"{monitor_script}\\""',
            shell=True,
            cwd=str(BASE_DIR),
        )
        log("MONITOR", "Monitor penceresi acildi")
    except Exception as e:
        log("MONITOR", f"UYARI: Monitor baslatma hatasi: {e}")


def cleanup_stale_sessions(current_session_id):
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
            deleted += 1
            continue
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            yt_url = state.get("artifacts", {}).get("youtube_url", "")
            real_uploaded = state["steps"].get("uploaded", False) and "watch?v=" in (yt_url or "")
            if not real_uploaded:
                shutil.rmtree(session_dir)
                deleted += 1
        except Exception:
            pass
    if deleted:
        log("TEMIZLIK", f"{deleted} eski/eksik session silindi")


def cleanup_temp(session_dir):
    temp = session_dir["temp"]
    if temp.exists():
        shutil.rmtree(temp)
        log("TEMIZLIK", f"Silindi: {temp}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def main():
    global total_spent

    parser = argparse.ArgumentParser(description="Badger Tape Content Pipeline")
    parser.add_argument("--theme", required=True, help='Ornek: "rainy night"')
    parser.add_argument(
        "--season", required=True,
        choices=["autumn", "winter", "spring", "summer"],
        help="autumn / winter / spring / summer",
    )
    parser.add_argument("--vocal", action="store_true",
                        help="Vokal lofi modu")
    args = parser.parse_args()

    # ── Resume Kontrolü ──
    incomplete = find_incomplete_sessions()
    state = None
    session_dir = None
    state_path = None

    if incomplete:
        print(f"\nTamamlanmamis {len(incomplete)} session bulundu:")
        for i, (sf, st) in enumerate(incomplete):
            err = st.get("last_error")
            err_info = f" | Son hata: [{err['step']}] {err['message'][:60]}" if err else ""
            completed_steps = sum(1 for v in st["steps"].values() if v)
            print(f"  [{i+1}] {st['session_id']} — tema: {st['theme']}, mevsim: {st['season']}, "
                  f"{completed_steps}/7 adim tamamlandi{err_info}")

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
            session_id = state["session_id"]
            session_dir = make_session_dir(session_id)
            # Harcama miktarını state'den geri yükle
            total_spent = state["cost_tracker"]["total"]
            print(f"\nSession yuklendi: {session_id} | Onceki harcama: ${total_spent:.3f}")

    if state is None:
        # Yeni session başlat
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = make_session_dir(session_id)
        state = create_state(session_id, args.theme, args.season, vocal=args.vocal)
        state_path = OUTPUT_DIR / f"session_{session_id}" / "state.json"
        save_state(state, state_path)
        cleanup_stale_sessions(session_id)
        launch_monitor()

    start_time = datetime.now()

    print(f"\n{'='*60}")
    print(f"  BADGER TAPE PIPELINE — session {state['session_id']}")
    print(f"  tema: {state['theme']} | mevsim: {state['season']}")
    print(f"  harcama limiti: ${SPENDING_LIMIT:.2f}")
    print(f"{'='*60}\n")
    sys.stdout.flush()

    # ── ADIM 1: Varyasyon Sec ──
    if not state["steps"]["variation_selected"]:
        print_progress(state, "variation_selected", start_time)
        try:
            variations = select_variations(state["season"])
            log("ADIM 1", f"Aktivite  : {variations['activity_desc']}")
            log("ADIM 1", f"Hareket   : {variations['activity_motion']}")
            log("ADIM 1", f"Isiklandirma: {variations['lighting']}")
            log("ADIM 1", f"Arka plan : {variations['background']}")
            log("ADIM 1", f"Mevsim    : {variations['season_atmosphere']}")
            state["steps"]["variation_selected"] = True
            state["artifacts"]["variations"] = variations
            save_state(state, state_path)
        except Exception as e:
            save_error_to_state(state, state_path, "variation_selected", str(e))
            print(f"HATA [ADIM 1]: {e}")
            print("State kaydedildi. Pipeline tekrar calistirildikinda bu adimdan devam edilebilir.")
            sys.exit(1)
    else:
        variations = state["artifacts"]["variations"]
        log("ADIM 1", "Atlanıyor (zaten tamamlandi)")

    # ── ADIM 2: Gorsel Uretimi ──
    if not state["steps"]["image_generated"]:
        print_progress(state, "image_generated", start_time)
        try:
            scene_path = generate_image(variations, session_dir)
            state["steps"]["image_generated"] = True
            state["artifacts"]["image_path"] = str(scene_path)
            state["cost_tracker"]["image"] += COST_IMAGE
            state["cost_tracker"]["total"] = total_spent
            save_state(state, state_path)
        except Exception as e:
            save_error_to_state(state, state_path, "image_generated", str(e))
            print(f"HATA [ADIM 2]: {e}")
            print("State kaydedildi. Pipeline tekrar calistirildikinda bu adimdan devam edilebilir.")
            sys.exit(1)
    else:
        scene_path = Path(state["artifacts"]["image_path"])
        log("ADIM 2", "Atlanıyor (zaten tamamlandi)")

    # ── ADIM 3: Loop Video ──
    if not state["steps"]["video_generated"]:
        print_progress(state, "video_generated", start_time)
        try:
            loop_path = generate_video(scene_path, variations, session_dir)
            state["steps"]["video_generated"] = True
            state["artifacts"]["video_path"] = str(loop_path)
            state["cost_tracker"]["video"] += COST_VIDEO_5S
            state["cost_tracker"]["total"] = total_spent
            save_state(state, state_path)
        except Exception as e:
            save_error_to_state(state, state_path, "video_generated", str(e))
            print(f"HATA [ADIM 3]: {e}")
            print("State kaydedildi. Pipeline tekrar calistirildikinda bu adimdan devam edilebilir.")
            sys.exit(1)
    else:
        loop_path = Path(state["artifacts"]["video_path"])
        log("ADIM 3", "Atlanıyor (zaten tamamlandi)")

    # ── ADIM 4: Muzik Uretimi ──
    if not state["steps"]["music_generated"]:
        print_progress(state, "music_generated", start_time)
        try:
            tracks = generate_music(session_dir, season=state["season"])
            state["steps"]["music_generated"] = True
            state["artifacts"]["music_paths"] = [str(t) for t in tracks]
            state["cost_tracker"]["music"] += COST_MUSIC_TRACK * len(tracks)
            state["cost_tracker"]["total"] = total_spent
            save_state(state, state_path)
        except Exception as e:
            save_error_to_state(state, state_path, "music_generated", str(e))
            print(f"HATA [ADIM 4]: {e}")
            print("State kaydedildi. Pipeline tekrar calistirildikinda bu adimdan devam edilebilir.")
            sys.exit(1)
    else:
        tracks = [Path(p) for p in state["artifacts"]["music_paths"]]
        log("ADIM 4", "Atlanıyor (zaten tamamlandi)")

    # ── ADIM 5: FFmpeg Birlestirme ──
    if not state["steps"]["ffmpeg_merged"]:
        print_progress(state, "ffmpeg_merged", start_time)
        try:
            final_video = combine_video_music(loop_path, tracks, session_dir)
            state["steps"]["ffmpeg_merged"] = True
            state["artifacts"]["final_video_path"] = str(final_video)
            save_state(state, state_path)
        except Exception as e:
            save_error_to_state(state, state_path, "ffmpeg_merged", str(e))
            print(f"HATA [ADIM 5]: {e}")
            print("State kaydedildi. Pipeline tekrar calistirildikinda bu adimdan devam edilebilir.")
            sys.exit(1)
    else:
        final_video = Path(state["artifacts"]["final_video_path"])
        log("ADIM 5", "Atlanıyor (zaten tamamlandi)")

    # ── ADIM 6: Metadata ──
    if not state["steps"]["metadata_created"]:
        print_progress(state, "metadata_created", start_time)
        try:
            metadata, metadata_path = generate_metadata(state["theme"], state["season"], session_dir)
            state["steps"]["metadata_created"] = True
            state["artifacts"]["metadata_path"] = str(metadata_path)
            save_state(state, state_path)
        except Exception as e:
            save_error_to_state(state, state_path, "metadata_created", str(e))
            print(f"HATA [ADIM 6]: {e}")
            print("State kaydedildi. Pipeline tekrar calistirildikinda bu adimdan devam edilebilir.")
            sys.exit(1)
    else:
        metadata_path = Path(state["artifacts"]["metadata_path"])
        log("ADIM 6", "Atlanıyor (zaten tamamlandi)")

    # ── ADIM 7: YouTube Upload (Koruma) ──
    if state["steps"]["uploaded"]:
        print_progress(state, "uploaded", start_time)
        print(f"\nUYARI: Bu session zaten YouTube'a yuklendi!")
        print(f"  YouTube URL: {state['artifacts'].get('youtube_url', 'bilgi yok')}")
        print(f"  Tekrar yukleme atlanıyor.")
    else:
        print_progress(state, "uploaded", start_time)
        try:
            youtube_url = upload_to_youtube(final_video, metadata_path)
            state["steps"]["uploaded"] = True
            state["artifacts"]["youtube_url"] = youtube_url or f"uploaded_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            save_state(state, state_path)
        except Exception as e:
            save_error_to_state(state, state_path, "uploaded", str(e))
            print(f"HATA [ADIM 7]: {e}")
            print("State kaydedildi. Pipeline tekrar calistirildikinda upload yeniden denenecek.")
            sys.exit(1)

    # ── Temizlik ──
    cleanup_temp(session_dir)

    # ── Son Progress ──
    print_progress(state, "uploaded", start_time)

    # ── Harcama Ozeti ──
    remaining = SPENDING_LIMIT - total_spent
    print(f"\n{'='*60}")
    print(f"  PIPELINE TAMAMLANDI")
    print(f"{'='*60}")
    print(f"  Gorsel       : ~${COST_IMAGE:.2f}")
    print(f"  Video (5sn)  : ~${COST_VIDEO_5S:.2f}")
    print(f"  Muzik (10x)  : ~${COST_MUSIC_TRACK * 10:.2f}")
    print(f"  TOPLAM       : ${total_spent:.3f}")
    print(f"  Kalan limit  : ${remaining:.3f}")
    if remaining < 0.50:
        print(f"  UYARI: $2 limitine yaklasiliyor!")
    print(f"  Cikti        : {final_video}")
    print(f"  State        : {state_path}")
    print(f"{'='*60}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
