"""
Badger Tape — Video Agent
Kling v2.1 Pro ile sahne görselinden loop video üretir.
"""

import threading
import urllib.request
from pathlib import Path

import fal_client

from agents.common import (
    COST_VIDEO_5S,
    CostTracker, log, save_state, save_error,
)


class VideoAgent:
    """
    Adim 3: image_agent çıktısından Kling v2.1 Pro ile 5 saniyelik loop video üretir.
    """

    def run(
        self,
        state: dict,
        state_path: Path,
        session_dir: dict,
        cost_tracker: CostTracker,
        lock: threading.Lock,
    ) -> tuple[bool, str | None]:

        if state["steps"]["video_generated"]:
            log("VideoAgent", "Adim 3 — atlanıyor (tamamlandi)")
            return True, None

        log("VideoAgent", "Adim 3 — loop video uretiliyor (Kling v2.1 Pro)")
        try:
            cost_tracker.check(COST_VIDEO_5S)

            scene_path = Path(state["artifacts"]["image_path"])
            if not scene_path.exists():
                raise FileNotFoundError(f"Sahne gorseli bulunamadi: {scene_path}")

            variations = state["artifacts"]["variations"]

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

            cost_tracker.record(COST_VIDEO_5S, "kling-video/v2.1/pro (5s)")

            video_url = result.get("video", {}).get("url") or result.get("url")
            if not video_url:
                raise ValueError(f"Video URL'si bulunamadi: {result}")

            out_path = session_dir["videos"] / "loop.mp4"
            urllib.request.urlretrieve(video_url, out_path)
            log("VideoAgent", f"  Kaydedildi: {out_path}")

            with lock:
                state["steps"]["video_generated"] = True
                state["artifacts"]["video_path"] = str(out_path)
                state["cost_tracker"]["video"] += COST_VIDEO_5S
                state["cost_tracker"]["total"] = cost_tracker.total
            save_state(state, state_path, lock)

        except Exception as e:
            msg = f"Video uretimi basarisiz: {e}"
            save_error(state, state_path, "video_generated", msg, lock)
            return False, msg

        return True, None
