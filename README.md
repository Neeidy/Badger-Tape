# Badger Tape

> A one-command, human-supervised AI pipeline that produces lofi YouTube videos end to end — paired with a zero-dependency, read-only Studio dashboard for observability.

Badger Tape is an automated lofi music channel system. A single command generates the artwork, motion loop, music, final render, metadata, and (optionally) the YouTube upload — all governed by a hard spending cap and human-in-the-loop safety defaults. Its mascot is **Porsuk**, a headphone-wearing, night-owl cat.

*drift away, one beat at a time*

---

## Architecture

```
orchestrator.py  ── 3-phase, parallel pipeline coordinator
   │
   ├─ Phase 1 (parallel):  image  +  video(loop)  +  music
   ├─ Phase 2:             ffmpeg merge & quality check
   └─ Phase 3:             metadata  →  YouTube upload
   │
   ├─ agents/        image_agent · video_agent · music_agent · merge_agent · upload_agent
   │                 common.py (shared utils: ffmpeg, cost tracking, state, logging)
   │
   ├─ dashboard.py   read-only Studio dashboard  (http://127.0.0.1:8765, stdlib only)
   └─ monitor.py     terminal live monitor
```

State is checkpointed per session under `output/session_<timestamp>/state.json`, so interrupted runs can resume.

---

## Requirements

- **Python 3.10+** (the code uses 3.10+ typing syntax, e.g. `str | None`, `list[Path]`)
- **ffmpeg / ffprobe** available on your `PATH` (or set explicit paths in `.env`)
- Python packages (see `requirements.txt`):
  - `google-auth`, `google-auth-oauthlib`, `google-api-python-client`
  - `fal-client`
  - `python-dotenv`

> The dashboard (`dashboard.py`) needs **no extra dependencies** — it uses only the Python standard library.

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your environment file
cp .env.example .env
#    then edit .env and set FAL_KEY (and optionally FFMPEG_PATH / FFPROBE_PATH)

# 3. Install ffmpeg (if not already on PATH)
#    Windows: winget install Gyan.FFmpeg   |   macOS: brew install ffmpeg
#    Linux:   sudo apt install ffmpeg

# 4. YouTube OAuth (only needed for real uploads)
#    - Download client_secrets.json from Google Cloud Console (OAuth desktop app)
#    - Place it in the project root
#    - On the first real upload, a browser opens for consent and token.json is created
```

`.env`, `client_secrets.json`, and `token.json` are git-ignored and never committed.

---

## Usage

```bash
# Run the full pipeline
python orchestrator.py --theme "rainy night" --season autumn

# Optional flags
python orchestrator.py --theme "rooftop summer night" --season summer --vocal
python orchestrator.py --theme "test run" --season winter --dry-run   # no API calls, no spend, no upload

# Read-only Studio dashboard
python dashboard.py        # http://127.0.0.1:8765

# Terminal live monitor
python monitor.py
```

`--season` must be one of: `autumn`, `winter`, `spring`, `summer`.

---

## Safety

- **$2.00 per-run spending cap** — the pipeline halts before exceeding it.
- **Uploads default to `private`** — nothing goes public without your action.
- **Resume / checkpoint** — each step is saved; interrupted runs continue where they left off.
- **Duplicate protection** — avoids re-running completed steps.
- **Secrets are git-ignored** — `.env`, `client_secrets.json`, `token.json` never enter version control.
- **Dashboard is strictly read-only for observability**, and it redacts private local paths before display.

---

## AI disclosure

All music and visuals are produced with AI tools and curated by a human. The brand identity, the **Porsuk** mascot, titles, and channel voice are original. Badger Tape is an independent project; the content strategy is informed by genre benchmark analysis, not by copying any specific channel.

---

## Project layout

| Path | Purpose |
|---|---|
| `orchestrator.py` | Main pipeline coordinator (3-phase, parallel) |
| `agents/` | Per-step agents + shared `common.py` utilities |
| `dashboard.py` | Read-only Studio dashboard (stdlib only) |
| `monitor.py` | Terminal live monitor |
| `scripts/pipeline.py` | **Legacy** monolithic version kept for reference (pre-refactor; not used by the current pipeline) |
| `scripts/youtube_upload.py` | YouTube upload helper |
| `references/` | Brand reference assets (avatar, banner) |
| `docs/NOTES.md` | Channel description draft / notes |

---

## License

Released under the [MIT License](LICENSE).
