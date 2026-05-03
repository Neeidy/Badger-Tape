"""
Badger Tape — Image Agent
Nano Banana 2 ile Porsuk karakteri sahne görseli üretir.
"""

import random
import threading
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
import os

load_dotenv()

import fal_client

from agents.common import (
    BASE_DIR, REFERENCES_DIR, COST_IMAGE,
    CostTracker, log, save_state, save_error,
)

# ── Variation Pool ────────────────────────────────────────────────────────────

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
    "cozy wooden desk facing rain-streaked window, coffee mug steaming, open notebook",
    "late night cafe corner, neon signs reflected in wet glass, ceramic cup on table",
    "rooftop at night, city lights below, small bluetooth speaker, open journal",
    "dimly lit home recording studio, mixing board visible, soundproof foam walls",
    "fireplace corner, warm orange glow, vinyl player spinning, snow outside window",
    "attic room, string fairy lights, stacked film reels, moonlight through skylight",
    "library alcove, floor lamp, leather armchair, rain on tall windows",
    "japanese tea room, paper screen glowing softly, zen garden visible outside",
]

SEASON_ATMOSPHERE = {
    "autumn": "autumn leaves stuck to wet window, orange and red hues outside",
    "winter": "frost on window edges, snowflakes drifting outside, cold blue light",
    "spring": "cherry blossom petals drifting gently in the breeze, soft warm sunlight through tree canopy",
    "summer": "heavy summer rain on window, warm humid glow outside",
}

# Outdoor scenes override the desk/indoor setup for certain seasons
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


def select_variations(season: str) -> dict:
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
        "activity_desc":     activity["desc"],
        "activity_motion":   activity["motion"],
        "lighting":          random.choice(LIGHTING),
        "background":        random.choice(BACKGROUNDS),
        "season_atmosphere": SEASON_ATMOSPHERE.get(season_lower, SEASON_ATMOSPHERE["autumn"]),
        "is_outdoor":        False,
    }


# ── Agent ─────────────────────────────────────────────────────────────────────

class ImageAgent:
    """
    Adim 1: Varyasyon sec
    Adim 2: fal.ai Nano Banana 2 ile gorsel uret
    """

    def run(
        self,
        state: dict,
        state_path: Path,
        session_dir: dict,
        cost_tracker: CostTracker,
        lock: threading.Lock,
    ) -> tuple[bool, str | None]:
        """
        Returns (success, error_message).
        Updates state in-place and persists to state_path.
        """

        # ── Adim 1: Varyasyon ────────────────────────────────────────────────
        if not state["steps"]["variation_selected"]:
            log("ImageAgent", "Adim 1 — varyasyon seciliyor")
            try:
                variations = select_variations(state["season"])
                log("ImageAgent", f"  Aktivite : {variations['activity_desc']}")
                log("ImageAgent", f"  Hareket  : {variations['activity_motion']}")
                log("ImageAgent", f"  Isik     : {variations['lighting']}")
                log("ImageAgent", f"  Arka plan: {variations['background']}")
                log("ImageAgent", f"  Mevsim   : {variations['season_atmosphere']}")
                with lock:
                    state["steps"]["variation_selected"] = True
                    state["artifacts"]["variations"] = variations
                save_state(state, state_path, lock)
            except Exception as e:
                msg = f"Varyasyon secimi basarisiz: {e}"
                save_error(state, state_path, "variation_selected", msg, lock)
                return False, msg
        else:
            variations = state["artifacts"]["variations"]
            log("ImageAgent", "Adim 1 — atlanıyor (tamamlandi)")

        # ── Adim 2: Gorsel ───────────────────────────────────────────────────
        if not state["steps"]["image_generated"]:
            log("ImageAgent", "Adim 2 — gorsel uretiliyor (Nano Banana 2)")
            try:
                cost_tracker.check(COST_IMAGE)

                avatar_path = REFERENCES_DIR / "avatar.png"
                if not avatar_path.exists():
                    raise FileNotFoundError("references/avatar.png bulunamadi")

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
                log("ImageAgent", f"  Prompt: {prompt[:120]}...")

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

                cost_tracker.record(COST_IMAGE, "nano-banana-2")

                img_url = (
                    result.get("images", [{}])[0].get("url")
                    or result.get("image", {}).get("url")
                    or result.get("url")
                )
                if not img_url:
                    raise ValueError(f"Gorsel URL'si bulunamadi: {result}")

                out_path = session_dir["images"] / "scene.png"
                urllib.request.urlretrieve(img_url, out_path)
                log("ImageAgent", f"  Kaydedildi: {out_path}")

                with lock:
                    state["steps"]["image_generated"] = True
                    state["artifacts"]["image_path"] = str(out_path)
                    state["cost_tracker"]["image"] += COST_IMAGE
                    state["cost_tracker"]["total"] = cost_tracker.total
                save_state(state, state_path, lock)

            except Exception as e:
                msg = f"Gorsel uretimi basarisiz: {e}"
                save_error(state, state_path, "image_generated", msg, lock)
                return False, msg
        else:
            log("ImageAgent", "Adim 2 — atlanıyor (tamamlandi)")

        return True, None
