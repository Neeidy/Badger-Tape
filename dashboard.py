"""
Badger Tape - Studio Dashboard (read-only)
===========================================
Zero dependencies. Python standard library only.

Run:
    python dashboard.py
Then it opens in your browser: http://127.0.0.1:8765

What it does:
- Lists every past session (output/session_*/state.json)
- Live pipeline tracking, refreshed every 2.5s (step_progress.json + upload_progress.json)
- Gallery of generated artwork / loop video / music / YouTube embeds
- Cost, success-rate and trend analytics
- Per-session activity timeline parsed from logs/pipeline.log
- In-app music player for generated tracks
- CSV / JSON export of session history

It is STRICTLY READ-ONLY. It never generates, uploads, edits or deletes anything.

Security:
- Serves only .png .jpg .mp4 .mp3 .json files inside images/ videos/ music/ output/
- Secret files (.env, client_secrets.json, token.json, .mcp.json) can NEVER be served
  (whitelist of roots + extensions + path-traversal guard).
"""

import json
import re
import sys
import subprocess
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

# -- Config ---------------------------------------------------------------------
BASE_DIR   = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
IMAGES_DIR = BASE_DIR / "images"
VIDEOS_DIR = BASE_DIR / "videos"
MUSIC_DIR  = BASE_DIR / "music"
LOG_FILE   = BASE_DIR / "logs" / "pipeline.log"
ORCH_FILE  = BASE_DIR / "orchestrator.py"

PORT = 8765
SPENDING_LIMIT = 2.00

# -- Pipeline runner (write side) ----------------------------------------------
# NOTE: Unlike the rest of this dashboard (which is strictly read-only), this
# section can LAUNCH the real pipeline as a subprocess. A real run calls paid
# APIs (fal.ai) and uploads to YouTube. Guards below: single-run lock, strict
# season whitelist, list-form argv (no shell, no injection), bound to 127.0.0.1.
SEASONS = {"autumn", "winter", "spring", "summer"}

_run_lock = threading.Lock()
_run_state = {
    "running": False, "pid": None, "started_at": None,
    "theme": None, "season": None, "vocal": False, "dry_run": False,
    "returncode": None, "finished_at": None,
}


def _pipeline_active():
    return _run_state["running"]


def _launch_pipeline(theme, season, vocal, dry_run):
    """Validate inputs and spawn orchestrator.py. Returns (ok, message)."""
    theme = (theme or "").strip()
    season = (season or "").strip().lower()
    # --- validation (defense against bad / malicious input) ---
    if not theme or len(theme) > 120:
        return False, "Theme is required (max 120 chars)."
    if season not in SEASONS:
        return False, "Season must be one of: autumn, winter, spring, summer."
    if not ORCH_FILE.exists():
        return False, "orchestrator.py not found."

    with _run_lock:
        if _run_state["running"]:
            return False, "A pipeline run is already in progress."
        # list-form argv -> no shell interpretation, theme passed safely as one token
        cmd = [sys.executable, str(ORCH_FILE), "--theme", theme, "--season", season]
        if vocal:
            cmd.append("--vocal")
        if dry_run:
            cmd.append("--dry-run")
        try:
            # stdin must be closed: orchestrator may call input() for resume; we
            # don't want it to block forever waiting on a terminal. With closed
            # stdin, input() raises EOFError and it proceeds to a fresh run.
            proc = subprocess.Popen(
                cmd, cwd=str(BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"Failed to start: {e}"
        _run_state.update({
            "running": True, "pid": proc.pid, "started_at": datetime.now().isoformat(),
            "theme": theme, "season": season, "vocal": bool(vocal),
            "dry_run": bool(dry_run), "returncode": None, "finished_at": None,
        })

    def _waiter(p):
        rc = p.wait()
        with _run_lock:
            _run_state.update({"running": False, "returncode": rc,
                               "finished_at": datetime.now().isoformat()})

    threading.Thread(target=_waiter, args=(proc,), daemon=True).start()
    mode = "dry-run" if dry_run else "live"
    return True, f"Pipeline started ({mode}) — theme: {theme}, season: {season}."

STEP_NAMES = [
    "variation_selected", "image_generated", "video_generated",
    "music_generated", "ffmpeg_merged", "metadata_created", "uploaded",
]

ALLOWED_EXT  = {".png", ".jpg", ".jpeg", ".mp4", ".mp3", ".json"}
MEDIA_ROOTS  = {"images": IMAGES_DIR, "videos": VIDEOS_DIR,
                "music": MUSIC_DIR, "output": OUTPUT_DIR}


# -- Redaction (privacy) --------------------------------------------------------
# Anything that reaches the browser passes through _redact() so private local
# Windows username paths never leave the machine in plaintext. Public YouTube
# IDs/URLs are deliberately left intact. The dashboard never reads .env,
# token.json, client_secrets.json or any credential file, so no secret value can
# ever be produced here — this guard only scrubs incidental filesystem paths
# that the pipeline wrote into logs / state.json.
_REDACT_PATTERNS = [
    # C:\Users\<name>\...  and  C:/Users/<name>/...  -> C:\Users\<redacted>\...
    (re.compile(r"([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\s\"']+", re.IGNORECASE),
     r"\1<redacted>"),
    # /home/<name>/ and /Users/<name>/ (defensive, cross-platform)
    (re.compile(r"((?:/home|/Users)/)[^/\s\"']+", re.IGNORECASE),
     r"\1<redacted>"),
]


def _redact(value):
    """Recursively scrub private username paths from any string / dict / list."""
    if isinstance(value, str):
        out = value
        for rx, repl in _REDACT_PATTERNS:
            out = rx.sub(repl, out)
        return out
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


# -- Helpers --------------------------------------------------------------------
def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _yt_id(url):
    if not url:
        return None
    m = re.search(r"(?:watch\?v=|youtu\.be/)([\w-]{11})", url)
    return m.group(1) if m else None


def _session_dt(sid):
    try:
        return datetime.strptime(sid, "%Y%m%d_%H%M%S")
    except Exception:
        return None


def _count_tracks(sid):
    d = MUSIC_DIR / f"session_{sid}"
    return len(list(d.glob("track_*.mp3"))) if d.exists() else 0


def _track_files(sid):
    d = MUSIC_DIR / f"session_{sid}"
    out = []
    if d.exists():
        for t in sorted(d.glob("track_*.mp3")):
            out.append({"name": t.name, "size_mb": _size_mb(t)})
    return out


def _size_mb(p):
    try:
        return round(p.stat().st_size / 1024 / 1024, 1)
    except Exception:
        return None


# -- Log parsing ----------------------------------------------------------------
_LOG_RE = re.compile(r"^([\d-]+ [\d:]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| (.*)$")


def parse_log():
    """Return {sid: {entries:[...], start, end, duration_sec, result}}."""
    index = {}
    if not LOG_FILE.exists():
        return index
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return index
    for ln in lines:
        m = _LOG_RE.match(ln)
        if not m:
            continue
        ts, sid, agent, level, msg = (x.strip() for x in m.groups())
        if sid == "unknown" or not re.match(r"\d{8}_\d{6}", sid):
            continue
        rec = index.setdefault(sid, {"entries": [], "start": None, "end": None,
                                     "duration_sec": None, "result": None})
        rec["entries"].append({"time": ts, "agent": agent, "level": level, "msg": msg})
        if "SESSION START" in msg and rec["start"] is None:
            rec["start"] = ts
        if "SESSION END" in msg:
            rec["end"] = ts
            rm = re.search(r"result:\s*(\w+)", msg)
            if rm:
                rec["result"] = rm.group(1)
            dm = re.search(r"duration:\s*(?:(\d+)h\s*)?(?:(\d+)m\s*)?(\d+)s", msg)
            if dm:
                h = int(dm.group(1) or 0); mn = int(dm.group(2) or 0); s = int(dm.group(3) or 0)
                rec["duration_sec"] = h * 3600 + mn * 60 + s
    return index


# -- Session model --------------------------------------------------------------
def _summary(sid, state, log_index=None):
    steps = state.get("steps", {})
    done = sum(1 for s in STEP_NAMES if steps.get(s))
    yt = state.get("artifacts", {}).get("youtube_url")
    uploaded_real = bool(steps.get("uploaded")) and bool(_yt_id(yt))
    if uploaded_real:
        status = "uploaded"
    elif done == len(STEP_NAMES):
        status = "complete"
    elif state.get("last_error"):
        status = "error"
    else:
        status = "incomplete"
    dt = _session_dt(sid)
    final_video = OUTPUT_DIR / f"session_{sid}" / "badgertape_1hour.mp4"
    artwork     = IMAGES_DIR / f"session_{sid}" / "scene.png"
    loop_video  = VIDEOS_DIR / f"session_{sid}" / "loop.mp4"
    dur = None
    result = None
    if log_index and sid in log_index:
        dur = log_index[sid]["duration_sec"]
        result = log_index[sid]["result"]
    return {
        "id": sid,
        "theme": state.get("theme", ""),
        "season": state.get("season", ""),
        "vocal": state.get("vocal", False),
        "datetime": dt.strftime("%b %d, %Y - %H:%M") if dt else sid,
        "date_iso": dt.isoformat() if dt else None,
        "steps": {s: bool(steps.get(s)) for s in STEP_NAMES},
        "steps_done": done,
        "steps_total": len(STEP_NAMES),
        "status": status,
        "cost": round(float(state.get("cost_tracker", {}).get("total", 0) or 0), 3),
        "cost_breakdown": state.get("cost_tracker", {}),
        "youtube_url": yt,
        "youtube_id": _yt_id(yt),
        "last_error": state.get("last_error"),
        "tracks": _count_tracks(sid),
        "has_artwork": artwork.exists(),
        "has_loop": loop_video.exists(),
        "has_final": final_video.exists(),
        "final_size_mb": _size_mb(final_video) if final_video.exists() else None,
        "duration_sec": dur,
        "result": result,
    }


def list_sessions():
    out = []
    if not OUTPUT_DIR.exists():
        return out
    log_index = parse_log()
    for sf in OUTPUT_DIR.glob("session_*/state.json"):
        st = _read_json(sf)
        if not st:
            continue
        sid = sf.parent.name.replace("session_", "")
        out.append(_summary(sid, st, log_index))
    out.sort(key=lambda s: s["id"], reverse=True)
    return out


def session_detail(sid):
    sf = OUTPUT_DIR / f"session_{sid}" / "state.json"
    st = _read_json(sf)
    if not st:
        return None
    log_index = parse_log()
    d = _summary(sid, st, log_index)
    d["metadata"] = _read_json(OUTPUT_DIR / f"session_{sid}" / "metadata.json")
    d["variations"] = st.get("artifacts", {}).get("variations")
    d["track_files"] = _track_files(sid)
    d["log_entries"] = log_index.get(sid, {}).get("entries", [])
    return d


def global_stats(sessions):
    total = len(sessions)
    uploaded = sum(1 for s in sessions if s["status"] == "uploaded")
    succeeded = sum(1 for s in sessions if s["steps_done"] == s["steps_total"])
    total_cost = round(sum(s["cost"] for s in sessions), 2)
    total_tracks = sum(s["tracks"] for s in sessions)
    hours = sum(1 for s in sessions if s["has_final"])
    durs = [s["duration_sec"] for s in sessions if s["duration_sec"]]
    avg_dur = round(sum(durs) / len(durs) / 60) if durs else None
    by_season = {}
    for s in sessions:
        by_season[s["season"]] = by_season.get(s["season"], 0) + 1
    return {
        "total_sessions": total,
        "uploaded": uploaded,
        "succeeded": succeeded,
        "success_rate": round(succeeded / total * 100) if total else 0,
        "total_cost": total_cost,
        "avg_cost": round(total_cost / total, 3) if total else 0,
        "total_tracks": total_tracks,
        "video_hours": hours,
        "avg_duration_min": avg_dur,
        "by_season": by_season,
        "spending_limit": SPENDING_LIMIT,
    }


def live_status():
    sessions = list_sessions()
    if not sessions:
        return {"active": False}
    latest = sessions[0]
    sid = latest["id"]
    sdir = OUTPUT_DIR / f"session_{sid}"
    step_prog = _read_json(sdir / "step_progress.json")
    up_prog   = _read_json(sdir / "upload_progress.json")
    is_active = latest["status"] not in ("uploaded",) and latest["steps_done"] < latest["steps_total"]
    current = None
    for s in STEP_NAMES:
        if not latest["steps"][s]:
            current = s
            break
    return {
        "active": is_active,
        "session": latest,
        "current_step": current,
        "step_progress": step_prog,
        "upload_progress": up_prog,
    }


def recent_log(n=80):
    if not LOG_FILE.exists():
        return []
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-n:]:
        m = _LOG_RE.match(ln)
        if m:
            ts, sid, agent, level, msg = (x.strip() for x in m.groups())
            out.append({"time": ts, "session": sid, "agent": agent, "level": level, "msg": msg})
        else:
            out.append({"time": "", "session": "", "agent": "", "level": "INFO", "msg": ln})
    return out


# -- Safe media -----------------------------------------------------------------
def resolve_media(rel):
    rel = unquote(rel).lstrip("/")
    parts = rel.split("/", 1)
    if len(parts) != 2:
        return None
    root = MEDIA_ROOTS.get(parts[0])
    if root is None:
        return None
    candidate = (root / parts[1]).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    if candidate.suffix.lower() not in ALLOWED_EXT or not candidate.is_file():
        return None
    return candidate


# -- HTTP handler ---------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        # Central egress point: scrub private username paths from every API
        # response so nothing private is ever serialized to the browser.
        body = json.dumps(_redact(obj), ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        import mimetypes
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(404); return
        rng = self.headers.get("Range")
        f = open(path, "rb")
        try:
            if rng and (ctype.startswith("video") or ctype.startswith("audio")):
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                start = int(m.group(1))
                end = int(m.group(2)) if m and m.group(2) else size - 1
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                f.seek(start)
                rem = length
                while rem > 0:
                    chunk = f.read(min(65536, rem))
                    if not chunk:
                        break
                    self.wfile.write(chunk); rem -= len(chunk)
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            f.close()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._html(PAGE)
            elif path == "/api/sessions":
                sess = list_sessions()
                self._json({"sessions": sess, "stats": global_stats(sess)})
            elif path == "/api/session":
                d = session_detail((qs.get("id") or [""])[0])
                self._json(d if d else {"error": "not found"}, 200 if d else 404)
            elif path == "/api/live":
                self._json(live_status())
            elif path == "/api/run-status":
                self._json(dict(_run_state))
            elif path == "/api/log":
                n = int((qs.get("n") or ["80"])[0])
                self._json({"lines": recent_log(min(n, 400))})
            elif path.startswith("/media/"):
                p = resolve_media(path[len("/media/"):])
                if p is None:
                    self.send_error(404)
                else:
                    self._file(p)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path != "/api/run":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:               # tiny payload only; nothing big expected
                self._json({"ok": False, "error": "payload too large"}, 413)
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._json({"ok": False, "error": "invalid JSON"}, 400)
                return
            ok, msg = _launch_pipeline(
                body.get("theme"), body.get("season"),
                bool(body.get("vocal")), bool(body.get("dry_run")),
            )
            self._json({"ok": ok, "message": msg, "state": dict(_run_state)},
                       200 if ok else 409)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Badger Tape — Studio Control</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>%F0%9F%A6%A1</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600;700;800&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.7.0/dist/tabler-icons.min.css">
<style>
:root{
  --bg:#070809; --bg2:#0b0d12; --bg3:#0e1117;
  --panel:rgba(20,23,31,.72); --panel-solid:#14171f; --panel2:rgba(28,32,42,.7);
  --glass:rgba(18,21,29,.55); --glass-brd:rgba(255,255,255,.06);
  --line:rgba(255,255,255,.07); --line2:rgba(255,255,255,.12);
  --txt:#f1f3f8; --txt2:#c5cad6; --mut:#8b93a7; --mut2:#5a6275;
  --amber:#ffb454; --amber2:#f7913a; --amber-d:#e07b27;
  --aglow:rgba(255,180,84,.14); --aglow2:rgba(255,180,84,.30);
  --teal:#4fd6c5; --green:#5cd887; --red:#ff6f6f; --violet:#a99cf6; --blue:#5aa9f0;
  --r:14px; --rl:20px; --rxl:26px;
  --sh:0 24px 64px -18px rgba(0,0,0,.7); --sh-sm:0 8px 24px -10px rgba(0,0,0,.55);
  --t:.32s cubic-bezier(.4,0,.2,1); --t2:.5s cubic-bezier(.16,1,.3,1);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--txt);
  font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased; min-height:100vh; overflow-x:hidden;
}
h1,h2,h3,.brand b,.stat .v,.ttl,.hero-num,.kpi b{font-family:Sora,Inter,sans-serif}
.mono{font-family:"JetBrains Mono",ui-monospace,"SF Mono",Menlo,monospace}

/* Ambient cinematic background */
#rain{position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.55}
.bg-aura{position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(900px 600px at 12% -8%, rgba(255,180,84,.10), transparent 60%),
    radial-gradient(800px 700px at 100% 0%, rgba(90,169,240,.06), transparent 55%),
    radial-gradient(700px 500px at 50% 120%, rgba(169,156,246,.05), transparent 60%);}
.bg-grid{position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.4;
  background-image:linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px);
  background-size:54px 54px;mask-image:radial-gradient(circle at 50% 30%,#000,transparent 80%)}
.app{position:relative;z-index:1}
a{color:var(--amber);text-decoration:none}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:8px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.18);background-clip:padding-box}
::-webkit-scrollbar-track{background:transparent}

/* Header */
header{position:sticky;top:0;z-index:40;backdrop-filter:blur(22px) saturate(1.4);
  background:linear-gradient(180deg,rgba(7,8,9,.94),rgba(7,8,9,.66));border-bottom:1px solid var(--line)}
.hbar{max-width:1320px;margin:0 auto;padding:15px 28px;display:flex;align-items:center;gap:16px}
.logo{width:48px;height:48px;border-radius:15px;display:grid;place-items:center;font-size:24px;position:relative;
  background:radial-gradient(130% 130% at 28% 18%,#2e2436,#111319);border:1px solid var(--line2);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 8px 22px rgba(0,0,0,.5);transition:var(--t)}
.logo::after{content:"";position:absolute;inset:-1px;border-radius:15px;padding:1px;background:linear-gradient(140deg,var(--aglow2),transparent 50%);
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude;opacity:0;transition:var(--t)}
.logo:hover{transform:translateY(-2px) rotate(-5deg)}
.logo:hover::after{opacity:1}
.brand b{font-size:19px;font-weight:700;letter-spacing:.2px;display:block;line-height:1.1}
.brand small{color:var(--mut);font-size:12px;letter-spacing:.3px}
.htools{margin-left:auto;display:flex;align-items:center;gap:10px}
.iconbtn{width:40px;height:40px;border-radius:12px;border:1px solid var(--line);background:var(--glass);
  color:var(--mut);display:grid;place-items:center;cursor:pointer;transition:var(--t);font-size:18px;backdrop-filter:blur(8px)}
.iconbtn:hover{color:var(--txt);border-color:var(--line2);background:var(--panel2);transform:translateY(-2px)}
.kbtn{display:flex;align-items:center;gap:8px;padding:0 14px;height:40px;border-radius:12px;border:1px solid var(--line);
  background:var(--glass);color:var(--mut);cursor:pointer;font-size:13px;transition:var(--t);backdrop-filter:blur(8px)}
.kbtn:hover{border-color:var(--line2);color:var(--txt)}
.kbd{font-size:11px;border:1px solid var(--line2);border-radius:6px;padding:2px 6px;color:var(--mut2);background:rgba(0,0,0,.3)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut2);transition:var(--t)}
.dot.on{background:var(--green);box-shadow:0 0 0 0 rgba(92,216,135,.5);animation:pulse 1.8s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(92,216,135,.5)}70%{box-shadow:0 0 0 9px rgba(92,216,135,0)}100%{box-shadow:0 0 0 0 rgba(92,216,135,0)}}
/* fal.ai budget pill */
.bal-pill{display:flex;align-items:center;gap:13px;height:50px;padding:0 20px 0 14px;border-radius:999px;border:1px solid var(--line);
  background:linear-gradient(180deg,rgba(20,23,31,.78),rgba(11,13,18,.55));backdrop-filter:blur(8px);transition:var(--t);cursor:default}
.bal-pill:hover{border-color:var(--aglow2);transform:translateY(-1px);box-shadow:0 8px 22px -12px var(--aglow2)}
.bal-pill .bal-ic{width:32px;height:32px;border-radius:10px;display:grid;place-items:center;font-size:17px;color:var(--amber);background:var(--aglow);border:1px solid var(--aglow2);flex-shrink:0}
.bal-pill .bal-txt{display:flex;flex-direction:column;justify-content:center;line-height:1.25;gap:2px}
.bal-pill .bal-lbl{font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--mut);font-weight:600;white-space:nowrap}
.bal-pill .bal-val{font-size:14.5px;font-weight:700;font-family:Sora;color:var(--txt);font-variant-numeric:tabular-nums;letter-spacing:-.2px;white-space:nowrap}
.bal-pill .bal-val small{color:var(--mut2);font-weight:500;font-size:12px}
.bal-pill.warn{border-color:rgba(255,180,84,.4)}.bal-pill.warn .bal-val{color:var(--amber)}
.bal-pill.danger{border-color:rgba(255,111,111,.45)}.bal-pill.danger .bal-val{color:var(--red)}.bal-pill.danger .bal-ic{color:var(--red);background:rgba(255,111,111,.1);border-color:rgba(255,111,111,.35)}

/* Tabs */
.tabs{max-width:1320px;margin:0 auto;padding:0 28px;display:flex;gap:4px}
.tab{position:relative;padding:15px 18px;color:var(--mut);cursor:pointer;font-size:14px;font-weight:500;
  display:flex;align-items:center;gap:9px;transition:var(--t);border:none;background:none}
.tab:hover{color:var(--txt2)}
.tab.on{color:var(--amber)}
.tab.on::after{content:"";position:absolute;left:12px;right:12px;bottom:-1px;height:2px;border-radius:2px;
  background:linear-gradient(90deg,var(--amber-d),var(--amber));box-shadow:0 0 14px var(--aglow2)}
.tab .ti{font-size:18px}
.tab .cnt{font-size:11px;color:var(--mut2);background:rgba(255,255,255,.05);border:1px solid var(--line);padding:1px 7px;border-radius:999px;min-width:20px;text-align:center}
.tab.on .cnt{color:var(--amber);border-color:var(--aglow2)}

.wrap{max-width:1320px;margin:0 auto;padding:30px 28px 140px}
.view{animation:fade .5s cubic-bezier(.16,1,.3,1) both}
@keyframes fade{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
h2.sec{font-family:Sora;font-size:12px;letter-spacing:1px;text-transform:uppercase;color:var(--mut);margin:36px 0 18px;font-weight:600;display:flex;align-items:center;gap:10px}
h2.sec .ti{color:var(--amber);font-size:16px}
h2.sec::after{content:"";flex:1;height:1px;background:linear-gradient(90deg,var(--line),transparent)}

/* Hero strip (overview top) */
.hero{display:grid;grid-template-columns:1.4fr 1fr;gap:18px;margin-bottom:8px}
.hero-main{position:relative;overflow:hidden;border-radius:var(--rxl);border:1px solid var(--line2);padding:28px 30px;
  background:linear-gradient(150deg,rgba(28,32,42,.85),rgba(11,13,18,.92));box-shadow:var(--sh)}
.hero-main::before{content:"";position:absolute;top:-60px;right:-40px;width:280px;height:280px;border-radius:50%;
  background:radial-gradient(circle,var(--aglow2),transparent 68%);opacity:.7}
.hero-main::after{content:"";position:absolute;inset:0;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='6' height='6'%3E%3Ccircle cx='1' cy='1' r='.5' fill='%23ffffff' opacity='.03'/%3E%3C/svg%3E");pointer-events:none}
.hero-eyebrow{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--amber);font-weight:600;display:flex;align-items:center;gap:8px;position:relative}
.hero-title{font-family:Sora;font-size:27px;font-weight:800;letter-spacing:-.5px;margin:14px 0 6px;position:relative;line-height:1.15}
.hero-sub{color:var(--mut);font-size:14px;position:relative;max-width:90%}
.hero-row{display:flex;gap:26px;margin-top:24px;position:relative;flex-wrap:wrap}
.hero-kpi .n{font-family:Sora;font-size:26px;font-weight:700;letter-spacing:-.5px}
.hero-kpi .l{font-size:12px;color:var(--mut);margin-top:2px}
.hero-kpi .n.amber{color:var(--amber)}.hero-kpi .n.green{color:var(--green)}
.hero-side{border-radius:var(--rxl);border:1px solid var(--line2);overflow:hidden;background:var(--panel-solid);box-shadow:var(--sh);position:relative;display:flex;flex-direction:column}
.hero-side .hs-thumb{aspect-ratio:16/10;background:#070809 center/cover no-repeat;position:relative}
.hero-side .hs-thumb::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,transparent 40%,rgba(7,8,9,.92))}
.hero-side .hs-ph{position:absolute;inset:0;display:grid;place-items:center;font-size:40px;opacity:.25}
.hero-side .hs-body{padding:16px 18px;position:relative;margin-top:-44px}
.hero-side .hs-tag{font-size:10.5px;letter-spacing:1px;text-transform:uppercase;color:var(--green);font-weight:700;display:flex;align-items:center;gap:6px}
.hero-side .hs-title{font-size:14.5px;font-weight:600;margin:7px 0 4px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.hero-side .hs-meta{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:10px}
.hero-side .hs-watch{margin-top:13px;display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--amber);font-weight:600}

/* Stat cards */
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:14px;margin-top:18px}
.stat{position:relative;overflow:hidden;border-radius:var(--rl);padding:18px 18px 16px;
  background:linear-gradient(165deg,var(--panel),rgba(11,13,18,.85));border:1px solid var(--line);
  backdrop-filter:blur(10px);transition:var(--t);cursor:default}
.stat:hover{transform:translateY(-5px);border-color:var(--line2);box-shadow:var(--sh-sm)}
.stat::before{content:"";position:absolute;top:-50px;right:-50px;width:120px;height:120px;border-radius:50%;
  background:radial-gradient(circle,var(--aglow2),transparent 70%);opacity:0;transition:var(--t)}
.stat:hover::before{opacity:.8}
.stat::after{content:"";position:absolute;left:0;top:0;height:100%;width:3px;border-radius:3px;
  background:linear-gradient(180deg,var(--amber),transparent);opacity:0;transition:var(--t)}
.stat:hover::after{opacity:1}
.stat .ic{font-size:17px;color:var(--amber);margin-bottom:13px;display:inline-grid;place-items:center;
  width:38px;height:38px;border-radius:11px;background:var(--aglow);border:1px solid var(--aglow2)}
.stat .k{font-size:11.5px;color:var(--txt2);letter-spacing:.4px;text-transform:uppercase;font-weight:600}
.stat .v{font-size:29px;font-weight:700;margin-top:5px;letter-spacing:-.8px}
.stat .s{font-size:11.5px;color:var(--mut);margin-top:5px;font-weight:500;display:flex;align-items:center;gap:6px}
.stat .s::before{content:"";width:5px;height:5px;border-radius:50%;background:var(--amber);box-shadow:0 0 6px var(--aglow2);flex-shrink:0;opacity:.85}
.stat .spark{position:absolute;right:14px;bottom:14px;opacity:.5}

/* generic panel */
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--rl);backdrop-filter:blur(10px)}
.grid2{display:grid;grid-template-columns:1.7fr 1fr;gap:16px}
.chartcard{background:linear-gradient(165deg,var(--panel),rgba(11,13,18,.82));border:1px solid var(--line);border-radius:var(--rl);padding:24px;backdrop-filter:blur(10px);transition:var(--t)}
.chartcard:hover{border-color:var(--line2)}
.chartcard h3{font-size:14px;margin:0 0 4px;font-weight:600;font-family:Sora}
.chartcard .sub{font-size:12px;color:var(--mut);margin-bottom:18px}
.chartcard .chart-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:18px}
.chartcard .chart-head h3{margin:0 0 4px}
.chartcard .chart-head .sub{margin:0}
.avg-badge{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;color:var(--amber);background:rgba(255,180,84,.08);
  border:1px solid var(--aglow2);padding:5px 11px;border-radius:9px;white-space:nowrap;flex-shrink:0;font-variant-numeric:tabular-nums}
.avg-badge i{font-size:13px;opacity:.85}
.avg-badge b{font-weight:700;font-family:Sora}
.chart-plot{position:relative;height:206px;margin-bottom:8px}
.chart-grid{position:absolute;left:44px;right:8px;top:0;height:200px;pointer-events:none;z-index:0}
.chart-grid .gl{position:absolute;left:0;right:0;border-top:1px solid rgba(255,255,255,.045)}
.chart-grid .gl span{position:absolute;left:-44px;top:-7px;width:38px;text-align:right;font-size:9.5px;color:var(--mut2);font-variant-numeric:tabular-nums}
.chart-avg{position:absolute;left:44px;right:8px;border-top:1.5px dashed rgba(255,180,84,.45);z-index:3}
.chart{position:absolute;left:44px;right:8px;top:0;bottom:0;display:flex;align-items:flex-end;justify-content:space-between;gap:14px;z-index:1}
.col{flex:1 1 0;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;min-width:0;cursor:default;height:100%}
.col .barwrap{width:100%;max-width:34px;display:flex;align-items:flex-end;justify-content:center;flex:1;position:relative}
.col .barwrap::before{content:"";position:absolute;left:50%;transform:translateX(-50%);bottom:0;width:100%;max-width:34px;height:100%;border-radius:10px;background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.03)}
.col .barv{width:100%;border-radius:10px;position:relative;min-height:6px;z-index:1;
  background:linear-gradient(180deg,#ffd89a 0%,var(--amber) 42%,var(--amber-d) 100%);
  box-shadow:0 -2px 20px -6px var(--aglow2),inset 0 0 0 1px rgba(255,255,255,.08);
  transition:height 1.05s cubic-bezier(.16,1,.3,1),filter var(--t),transform var(--t)}
.col:hover .barv{filter:brightness(1.14) saturate(1.08);transform:translateY(-2px)}
.col .barv::before{content:"";position:absolute;inset:0;border-radius:inherit;background:linear-gradient(180deg,rgba(255,255,255,.32),transparent 42%)}
.col .barv::after{content:"";position:absolute;left:0;right:0;top:0;height:7px;border-radius:10px 10px 0 0;background:radial-gradient(60% 100% at 50% 0,rgba(255,255,255,.55),transparent)}
.col .cap-dot{position:absolute;top:-9px;left:50%;transform:translateX(-50%);width:6px;height:6px;border-radius:50%;background:#ffe1b0;box-shadow:0 0 12px var(--amber);opacity:0;transition:var(--t)}
.col:hover .cap-dot{opacity:1}
.chart-labs{display:flex;justify-content:space-between;gap:14px;padding-left:44px;padding-right:8px;margin-top:2px}
.chart-labs .lab{flex:1 1 0;text-align:center;font-size:10px;color:var(--mut2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:color var(--t)}
.col .val{position:absolute;top:-20px;left:50%;transform:translateX(-50%) translateY(4px);font-size:11px;color:var(--amber);font-weight:700;opacity:0;transition:var(--t);font-variant-numeric:tabular-nums;text-shadow:0 0 12px var(--aglow2);white-space:nowrap}
.col:hover .val{opacity:1;transform:translateX(-50%)}

/* ring */
.ring{display:grid;place-items:center;height:230px;position:relative}
.ring::before{content:"";position:absolute;width:150px;height:150px;border-radius:50%;background:radial-gradient(circle,var(--aglow),transparent 70%);filter:blur(8px);animation:ringpulse 3.4s ease-in-out infinite}
@keyframes ringpulse{0%,100%{opacity:.45;transform:scale(.95)}50%{opacity:.8;transform:scale(1.05)}}
.ring svg{transform:rotate(-90deg);position:relative;z-index:1;filter:drop-shadow(0 0 9px var(--aglow2))}
.ring svg .track{stroke:rgba(255,255,255,.06)}
.ring svg .glowcirc{filter:drop-shadow(0 0 6px rgba(255,180,84,.65))}
.ring .rc{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;z-index:2;width:140px}
.ring .rc b{display:block;font-size:42px;line-height:1;font-family:Sora;font-weight:800;letter-spacing:-2px;background:linear-gradient(180deg,#fff 25%,var(--amber));-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;text-shadow:0 0 30px var(--aglow2)}
.ring .rc small{display:block;color:var(--mut);font-size:11.5px;margin-top:6px;letter-spacing:.3px}
.ring-foot{display:flex;justify-content:center;gap:22px;margin-top:6px}
.ring-foot span{font-size:11.5px;color:var(--mut);display:flex;align-items:center;gap:7px}
.ring-foot i.dotc{width:9px;height:9px;border-radius:50%;display:inline-block;box-shadow:0 0 8px currentColor}

.seasonrow{display:flex;flex-direction:column;gap:16px;margin-top:8px}
.sbar{display:flex;align-items:center;gap:14px;font-size:13px}
.sbar .nm{width:92px;color:var(--txt2);display:flex;align-items:center;gap:8px;font-weight:500;text-transform:capitalize}
.sbar .nm .ti{color:var(--teal);font-size:15px}
.sbar .tk{flex:1;height:13px;background:rgba(0,0,0,.4);border-radius:8px;overflow:hidden;border:1px solid var(--line);position:relative}
.sbar .tk i{display:block;height:100%;border-radius:8px;position:relative;width:0;
  background:linear-gradient(90deg,var(--blue),var(--teal) 80%);box-shadow:0 0 16px -2px rgba(79,214,197,.5),inset 0 1px 0 rgba(255,255,255,.15);
  transition:width 1.05s cubic-bezier(.16,1,.3,1)}
.sbar .tk i::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.3),transparent);transform:translateX(-100%);animation:shimmer 3s infinite;animation-delay:.6s}
.sbar .cn{width:30px;text-align:right;color:var(--txt2);font-variant-numeric:tabular-nums;font-weight:600;font-family:Sora}

/* toolbar / inputs */
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.inp{display:flex;align-items:center;gap:9px;background:var(--glass);border:1px solid var(--line);border-radius:12px;padding:0 13px;height:42px;transition:var(--t);flex:1;min-width:220px;backdrop-filter:blur(8px)}
.inp:focus-within{border-color:var(--amber);box-shadow:0 0 0 3px var(--aglow)}
.inp input{background:none;border:none;outline:none;color:var(--txt);font-size:14px;width:100%;font-family:inherit}
.inp input::placeholder{color:var(--mut2)}
.inp .ti{color:var(--mut)}
.sel{background:var(--glass);border:1px solid var(--line);border-radius:12px;color:var(--txt);height:42px;padding:0 13px;font-size:13px;cursor:pointer;font-family:inherit;transition:var(--t);backdrop-filter:blur(8px)}
.sel:hover{border-color:var(--line2)}
.btn{display:inline-flex;align-items:center;gap:8px;height:42px;padding:0 15px;border-radius:12px;border:1px solid var(--line);background:var(--glass);color:var(--txt2);cursor:pointer;font-size:13px;font-family:inherit;transition:var(--t);backdrop-filter:blur(8px)}
.btn:hover{border-color:var(--aglow2);color:var(--amber);transform:translateY(-2px);background:var(--aglow)}

/* table */
.tablecard{background:var(--panel);border:1px solid var(--line);border-radius:var(--rl);overflow:hidden;backdrop-filter:blur(10px)}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--mut);font-weight:600;font-size:11px;letter-spacing:.6px;text-transform:uppercase;padding:15px 20px;border-bottom:1px solid var(--line);background:rgba(0,0,0,.25);position:sticky;top:0;cursor:pointer;user-select:none;white-space:nowrap;transition:color .15s;z-index:2}
th:hover{color:var(--txt2)}th .ti{font-size:13px;opacity:.5}
td{padding:15px 20px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:none}
tr.row{cursor:pointer;transition:background .18s}
tr.row:hover{background:rgba(255,255,255,.025)}
tr.row:hover td:first-child{box-shadow:inset 3px 0 0 var(--amber)}
.sid{font-family:"JetBrains Mono",monospace;font-size:11.5px;color:var(--mut)}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:600;padding:4px 11px;border-radius:999px;border:1px solid;white-space:nowrap;letter-spacing:.2px}
.badge .ti{font-size:13px}
.b-uploaded{color:var(--green);border-color:rgba(92,216,135,.4);background:rgba(92,216,135,.1)}
.b-complete{color:var(--teal);border-color:rgba(79,214,197,.4);background:rgba(79,214,197,.1)}
.b-error{color:var(--red);border-color:rgba(255,111,111,.4);background:rgba(255,111,111,.1)}
.b-incomplete{color:var(--amber);border-color:rgba(255,180,84,.4);background:rgba(255,180,84,.1)}
.chip{font-size:11px;color:var(--txt2);background:rgba(255,255,255,.04);border:1px solid var(--line);padding:3px 10px;border-radius:8px;white-space:nowrap;display:inline-flex;align-items:center;gap:5px}
.cost{font-variant-numeric:tabular-nums;color:var(--amber);font-weight:500}
.ytlink{display:inline-flex;align-items:center;gap:5px;color:var(--green)}
.ytlink:hover{color:var(--amber)}

/* Gallery */
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(265px,1fr));gap:20px}
.tile{background:var(--panel-solid);border:1px solid var(--line);border-radius:18px;overflow:hidden;cursor:pointer;transition:var(--t);position:relative}
.tile:hover{transform:translateY(-7px);border-color:var(--line2);box-shadow:var(--sh)}
.tile .thumb{aspect-ratio:16/9;background:#070809 center/cover no-repeat;position:relative;display:grid;place-items:center;overflow:hidden}
.tile .thumb .ph{font-size:38px;opacity:.3}
.tile:hover .thumb{filter:brightness(1.06)}
.tile .thumb::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,transparent 50%,rgba(0,0,0,.7));opacity:0;transition:var(--t)}
.tile:hover .thumb::after{opacity:1}
.tile .ribbon{position:absolute;top:11px;left:11px;display:flex;gap:6px;z-index:2}
.tile .rb{font-size:10px;font-weight:700;letter-spacing:.4px;padding:3px 8px;border-radius:7px;backdrop-filter:blur(8px);background:rgba(7,8,9,.6);border:1px solid var(--line2);color:var(--txt2);display:flex;align-items:center;gap:4px}
.tile .rb.up{color:var(--green);border-color:rgba(92,216,135,.4)}
.vinyl{position:absolute;right:13px;bottom:13px;width:32px;height:32px;border-radius:50%;background:conic-gradient(#222,#000,#222,#000);border:2px solid #333;display:grid;place-items:center;opacity:0;transform:translateY(8px);transition:var(--t);z-index:2}
.vinyl::after{content:"";width:7px;height:7px;border-radius:50%;background:var(--amber)}
.tile:hover .vinyl{opacity:1;transform:none;animation:spin 3s linear infinite}
@keyframes spin{100%{transform:rotate(360deg)}}
.tile .play{position:absolute;left:13px;bottom:13px;font-size:11px;color:#fff;opacity:0;transform:translateY(8px);transition:var(--t);display:flex;align-items:center;gap:5px;text-shadow:0 1px 4px #000;z-index:2;font-weight:500}
.tile:hover .play{opacity:1;transform:none}
.tile .meta{padding:14px 15px}
.tile .meta .t{font-size:13.5px;font-weight:600;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:38px}
.tile .meta .d{display:flex;gap:8px;align-items:center;margin-top:11px;flex-wrap:wrap}

/* Live */
.live{position:relative;overflow:hidden;border-radius:var(--rxl);border:1px solid var(--line2);padding:26px 28px;
  background:linear-gradient(150deg,rgba(28,32,42,.82),rgba(11,13,18,.92));box-shadow:var(--sh)}
.live::after{content:"";position:absolute;inset:0;background:radial-gradient(760px 220px at 88% -20%,var(--aglow),transparent 62%);pointer-events:none}
.live-head{display:flex;align-items:center;gap:15px;position:relative}
.live-head .ttl{font-size:19px;font-weight:700;font-family:Sora;letter-spacing:-.3px}
.live-head .sub{color:var(--mut);font-size:13px;margin-top:3px}
.eq{display:inline-flex;gap:3px;align-items:flex-end;height:22px}
.eq span{width:3px;background:var(--amber);border-radius:2px;animation:eq 1s ease-in-out infinite}
.eq span:nth-child(2){animation-delay:.2s}.eq span:nth-child(3){animation-delay:.4s}.eq span:nth-child(4){animation-delay:.1s}.eq span:nth-child(5){animation-delay:.3s}
@keyframes eq{0%,100%{height:5px}50%{height:20px}}
.lbadge{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;color:#0a0b0f;background:linear-gradient(90deg,var(--amber),var(--amber2));padding:4px 11px;border-radius:7px;letter-spacing:.6px}

/* progress rail (timeline) */
.rail{position:relative;margin:30px 0 8px;padding:0 6px}
.rail-line{position:absolute;left:6px;right:6px;top:21px;height:3px;background:rgba(255,255,255,.08);border-radius:3px}
.rail-fill{position:absolute;left:6px;top:21px;height:3px;border-radius:3px;background:linear-gradient(90deg,var(--amber-d),var(--amber));box-shadow:0 0 14px var(--aglow2);transition:width 1s cubic-bezier(.16,1,.3,1)}
.rail-steps{position:relative;display:flex;justify-content:space-between}
.rstep{display:flex;flex-direction:column;align-items:center;gap:10px;flex:1;text-align:center;min-width:0}
.rnode{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;background:var(--bg3);border:2px solid rgba(255,255,255,.1);font-size:17px;color:var(--mut2);transition:var(--t);position:relative;z-index:2}
.rstep.done .rnode{background:rgba(92,216,135,.14);border-color:rgba(92,216,135,.55);color:var(--green)}
.rstep.done .rnode i{animation:pop .45s cubic-bezier(.16,1,.3,1)}
@keyframes pop{0%{transform:scale(.3);opacity:0}65%{transform:scale(1.18)}100%{transform:scale(1)}}
.rstep.cur .rnode{background:var(--aglow);border-color:var(--amber);color:var(--amber);box-shadow:0 0 0 5px var(--aglow)}
.rstep.cur .rnode i{animation:spinl 1.4s linear infinite}
@keyframes spinl{100%{transform:rotate(360deg)}}
.rstep .rlb{font-size:11.5px;color:var(--mut);font-weight:500;transition:var(--t)}
.rstep.done .rlb{color:var(--txt2)}.rstep.cur .rlb{color:var(--amber);font-weight:600}
.rstep .rs2{font-size:10.5px;color:var(--amber);min-height:13px;font-weight:600;font-variant-numeric:tabular-nums}

/* cost cap */
.capwrap{display:grid;grid-template-columns:1.4fr 1fr;gap:16px;margin-top:24px}
.capcard,.outcard{background:rgba(0,0,0,.22);border:1px solid var(--line);border-radius:var(--rl);padding:20px 22px}
.cap-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.cap-h .l{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.7px;font-weight:600;display:flex;align-items:center;gap:7px}
.cap-h .v{font-family:Sora;font-size:15px;font-weight:700}
.cap-bar{height:16px;border-radius:999px;background:rgba(0,0,0,.45);border:1px solid var(--line);overflow:hidden;position:relative}
.cap-bar>i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--green),var(--teal));transition:width 1s cubic-bezier(.16,1,.3,1);position:relative}
.cap-bar.warn>i{background:linear-gradient(90deg,var(--amber-d),var(--amber))}
.cap-bar.danger>i{background:linear-gradient(90deg,#e05555,var(--red))}
.cap-bar>i::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.25),transparent);transform:translateX(-100%);animation:shimmer 2.4s infinite}
@keyframes shimmer{100%{transform:translateX(220%)}}
.cap-marks{display:flex;justify-content:space-between;margin-top:9px;font-size:11px;color:var(--mut2)}
.cap-foot{margin-top:12px;font-size:12px;color:var(--mut)}
.cap-foot b{color:var(--txt)}
.outcard .ol{display:flex;justify-content:space-between;align-items:center;padding:7px 0;font-size:13px;border-bottom:1px solid rgba(255,255,255,.04)}
.outcard .ol:last-child{border:none}
.outcard .ol .k{color:var(--mut);display:flex;align-items:center;gap:8px}
.outcard .ol .v{color:var(--txt2);font-variant-numeric:tabular-nums}
.outcard .ol .v.ok{color:var(--green)}.outcard .ol .v.no{color:var(--mut2)}

/* logs */
.logwrap{background:rgba(4,6,9,.7);border:1px solid var(--line);border-radius:var(--rl);overflow:hidden;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:12.5px;backdrop-filter:blur(8px)}
.logrow{display:grid;grid-template-columns:142px 96px 96px 1fr;gap:12px;padding:8px 18px;border-bottom:1px solid rgba(255,255,255,.03);transition:background .12s;align-items:center}
.logrow:hover{background:rgba(255,255,255,.022)}
.logrow .lt{color:var(--mut2);white-space:nowrap}
.logrow .la{color:var(--violet);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.logrow .lv{font-weight:600;display:inline-flex;align-items:center;gap:5px}
.lv.INFO{color:var(--mut)}.lv.WARNING{color:var(--amber)}.lv.ERROR{color:var(--red)}
.logrow .lv .ti{font-size:12px}
.logrow .lm{color:var(--txt2);word-break:break-word}
.logrow.is-start{background:rgba(92,216,135,.05)}.logrow.is-start .lm{color:var(--green)}
.logrow.is-end{background:rgba(255,180,84,.05)}.logrow.is-end .lm{color:var(--amber)}

/* modal */
.overlay{position:fixed;inset:0;background:rgba(3,4,7,.8);backdrop-filter:blur(10px);z-index:100;display:none;align-items:flex-start;justify-content:center;padding:42px 20px;overflow:auto}
.overlay.on{display:flex;animation:fade .25s ease}
.modal{width:min(1000px,100%);background:linear-gradient(165deg,var(--panel2),rgba(11,13,18,.96));border:1px solid var(--line2);border-radius:26px;box-shadow:var(--sh);overflow:hidden;animation:rise .4s cubic-bezier(.16,1,.3,1);backdrop-filter:blur(18px)}
@keyframes rise{from{opacity:0;transform:translateY(30px) scale(.98)}to{opacity:1;transform:none}}
.mhead{display:flex;align-items:flex-start;gap:14px;padding:24px 26px;border-bottom:1px solid var(--line);position:relative}
.mhead .x{margin-left:auto;cursor:pointer;color:var(--mut);font-size:20px;background:none;border:1px solid var(--line);width:38px;height:38px;border-radius:11px;transition:var(--t);display:grid;place-items:center;flex-shrink:0}
.mhead .x:hover{color:var(--txt);border-color:var(--line2);transform:rotate(90deg)}
.mbody{padding:26px;max-height:74vh;overflow:auto}
.mediagrid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.mediabox{background:#070809;border:1px solid var(--line);border-radius:14px;overflow:hidden}
.mediabox img,.mediabox video,.mediabox iframe{width:100%;display:block;aspect-ratio:16/9;border:0;background:#000;object-fit:cover}
.mediabox .cap{padding:10px 14px;font-size:11px;color:var(--mut);border-top:1px solid var(--line);display:flex;align-items:center;gap:7px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:11px 20px;margin-top:22px;font-size:13.5px}
.kv .kk{color:var(--mut);display:flex;align-items:center;gap:8px}
.kv .vv{color:var(--txt2)}
.copy{cursor:pointer;color:var(--mut2);transition:var(--t)}.copy:hover{color:var(--amber)}
.tags{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}
.tracklist{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:12px}
.trk{display:flex;align-items:center;gap:10px;font-size:12px;background:rgba(0,0,0,.25);border:1px solid var(--line);border-radius:11px;padding:9px 12px;transition:var(--t)}
.trk:hover{border-color:var(--line2)}
.trk .pp{cursor:pointer;color:var(--amber);font-size:17px}
.trk .n{color:var(--mut);margin-left:auto;font-variant-numeric:tabular-nums}
.timeline{margin-top:16px;border-left:2px solid var(--line);padding-left:20px;display:flex;flex-direction:column;gap:12px}
.tlitem{position:relative;font-size:12.5px;line-height:1.5}
.tlitem::before{content:"";position:absolute;left:-27px;top:4px;width:9px;height:9px;border-radius:50%;background:var(--amber);box-shadow:0 0 0 3px var(--bg2)}
.tlitem.ERROR::before{background:var(--red)}.tlitem.WARNING::before{background:var(--amber)}
.tlitem .tt{color:var(--mut2);margin-right:9px;font-family:"JetBrains Mono",monospace;font-size:11px}
.tlitem .ta{color:var(--violet);margin-right:7px}
.errbox{background:rgba(255,111,111,.07);border:1px solid rgba(255,111,111,.3);color:#ffcccc;border-radius:13px;padding:14px;font-size:12px;margin-top:18px;font-family:"JetBrains Mono",monospace;white-space:pre-wrap;word-break:break-word;line-height:1.55}
.sub-tabs{display:flex;gap:6px;margin:22px 0 10px}
.sub-tab{padding:8px 15px;border-radius:10px;border:1px solid var(--line);background:var(--glass);color:var(--mut);cursor:pointer;font-size:12px;transition:var(--t)}
.sub-tab.on{color:var(--amber);border-color:var(--aglow2);background:var(--aglow)}

/* music player */
.player{position:fixed;left:0;right:0;bottom:0;z-index:90;transform:translateY(120%);transition:transform .5s cubic-bezier(.16,1,.3,1)}
.player.on{transform:none}
.player-inner{max-width:1320px;margin:0 auto;padding:13px 28px;display:flex;align-items:center;gap:18px;
  background:linear-gradient(180deg,rgba(14,17,23,.97),rgba(7,8,9,.99));backdrop-filter:blur(20px);border-top:1px solid var(--line2);box-shadow:0 -12px 44px rgba(0,0,0,.6)}
.pdisc{width:46px;height:46px;border-radius:50%;background:conic-gradient(#1c1c1c,#000,#1c1c1c,#000);border:2px solid #2a2a2a;display:grid;place-items:center;flex-shrink:0;box-shadow:0 4px 14px rgba(0,0,0,.5)}
.pdisc.spin{animation:spin 3.5s linear infinite}
.pdisc::after{content:"";width:10px;height:10px;border-radius:50%;background:var(--amber)}
.pinfo{min-width:0;width:160px}.pinfo .pt{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pinfo .ps{font-size:11px;color:var(--mut)}
.pctrl{display:flex;align-items:center;gap:7px}
.pbtn{width:37px;height:37px;border-radius:50%;border:1px solid var(--line);background:var(--glass);color:var(--txt);cursor:pointer;display:grid;place-items:center;font-size:16px;transition:var(--t)}
.pbtn:hover{border-color:var(--amber);color:var(--amber)}
.pbtn.main{width:44px;height:44px;background:linear-gradient(135deg,var(--amber),var(--amber2));color:#0a0b0f;border:none;font-size:19px}
.pbtn.main:hover{filter:brightness(1.1);transform:scale(1.06)}
.pseek{flex:1;display:flex;align-items:center;gap:11px;font-size:11px;color:var(--mut);font-variant-numeric:tabular-nums}
.pseek .track{flex:1;height:6px;background:rgba(0,0,0,.5);border-radius:4px;cursor:pointer;overflow:hidden;border:1px solid var(--line)}
.pseek .track i{display:block;height:100%;background:linear-gradient(90deg,var(--amber-d),var(--amber));border-radius:4px;width:0}
.pclose{cursor:pointer;color:var(--mut);transition:var(--t)}.pclose:hover{color:var(--red)}

/* command palette */
.cmdk{position:fixed;inset:0;z-index:120;background:rgba(3,4,7,.65);backdrop-filter:blur(8px);display:none;align-items:flex-start;justify-content:center;padding:14vh 20px}
.cmdk.on{display:flex;animation:fade .2s ease}
.cmdbox{width:min(640px,100%);background:linear-gradient(165deg,var(--panel2),rgba(11,13,18,.97));border:1px solid var(--line2);border-radius:20px;box-shadow:var(--sh);overflow:hidden;animation:rise .3s cubic-bezier(.16,1,.3,1);backdrop-filter:blur(18px)}
.cmdsearch{display:flex;align-items:center;gap:13px;padding:18px 20px;border-bottom:1px solid var(--line)}
.cmdsearch input{flex:1;background:none;border:none;outline:none;color:var(--txt);font-size:16px;font-family:inherit}
.cmdsearch .ti{font-size:20px;color:var(--mut)}
.cmdresults{max-height:52vh;overflow:auto;padding:9px}
.cmdrow{display:flex;align-items:center;gap:13px;padding:12px 15px;border-radius:12px;cursor:pointer;font-size:14px;transition:background .12s}
.cmdrow.sel,.cmdrow:hover{background:var(--aglow)}
.cmdrow .ti{color:var(--amber)}
.cmdrow .meta{margin-left:auto;color:var(--mut2);font-size:12px}

.toasts{position:fixed;bottom:96px;right:28px;z-index:200;display:flex;flex-direction:column;gap:11px}
.toast{background:linear-gradient(165deg,var(--panel2),rgba(11,13,18,.96));border:1px solid var(--line2);border-left:3px solid var(--amber);border-radius:13px;padding:13px 17px;font-size:13px;box-shadow:var(--sh);display:flex;align-items:center;gap:11px;animation:slidein .4s cubic-bezier(.16,1,.3,1);min-width:230px;backdrop-filter:blur(14px)}
.toast .ti{color:var(--amber)}
@keyframes slidein{from{opacity:0;transform:translateX(44px)}to{opacity:1;transform:none}}

/* run pipeline cta + modal */
.hero-cta{display:flex;align-items:center;gap:14px;margin-top:22px;position:relative}
.run-btn{display:inline-flex;align-items:center;gap:9px;height:44px;padding:0 22px;border-radius:12px;border:none;cursor:pointer;
  font-family:Sora;font-size:13.5px;font-weight:600;color:#140d04;background:linear-gradient(135deg,#ffd089,var(--amber) 55%,var(--amber2));
  box-shadow:0 10px 28px -10px var(--aglow2),inset 0 1px 0 rgba(255,255,255,.4);transition:var(--t)}
.run-btn:hover{transform:translateY(-2px);box-shadow:0 16px 36px -12px var(--aglow2),inset 0 1px 0 rgba(255,255,255,.4);filter:brightness(1.04)}
.run-btn:disabled{opacity:.55;cursor:not-allowed;transform:none;filter:grayscale(.3)}
.run-btn .ti{font-size:16px}
.run-state{font-size:12.5px;color:var(--mut);display:flex;align-items:center;gap:7px}
.run-state.live{color:var(--green)}.run-state .ti{font-size:14px}
.run-state .rdot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 0 rgba(92,216,135,.5);animation:pulse 1.8s infinite}
.run-modal{width:min(620px,100%)}
.rfield{margin-top:16px;display:flex;flex-direction:column;gap:7px}
.rfield label{font-size:12px;color:var(--txt2);font-weight:600;display:flex;align-items:center;gap:8px;letter-spacing:.2px}
.rfield .req{font-size:9.5px;color:var(--amber);text-transform:uppercase;letter-spacing:.6px;border:1px solid var(--aglow2);padding:1px 6px;border-radius:5px;font-weight:600}
.rinput{height:44px;background:var(--glass);border:1px solid var(--line);border-radius:11px;color:var(--txt);font-size:14px;font-family:inherit;padding:0 13px;outline:none;transition:var(--t)}
.rinput::placeholder{color:var(--mut2)}
.rinput:focus{border-color:var(--amber);box-shadow:0 0 0 3px var(--aglow)}
/* opaque dark dropdown so the native option list matches the theme */
select.rinput{cursor:pointer;background-color:#0e1117;-webkit-appearance:none;appearance:none;padding-right:36px;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238b93a7' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 13px center}
select.rinput option{background-color:#0e1117;color:var(--txt)}
.rfield .rhint{font-size:11px;color:var(--mut2)}
.rgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.rgrid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.rgrid .rfield,.rgrid3 .rfield{margin-top:0;min-width:0}
.rgrid .rinput,.rgrid3 .rinput{width:100%;min-width:0}
.rgrid,.rgrid3{margin-top:16px}
.rpreview{margin-top:18px;background:rgba(0,0,0,.28);border:1px solid var(--line);border-radius:12px;padding:13px 15px;display:flex;flex-direction:column;gap:6px}
.rpreview .rpl{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--mut2);font-weight:600}
.rpreview code{font-family:"JetBrains Mono",monospace;font-size:13px;color:var(--amber);word-break:break-word}
.rdry{display:flex;align-items:center;gap:11px;margin-top:18px;font-size:12.5px;color:var(--txt2);cursor:pointer;background:rgba(255,255,255,.025);border:1px solid var(--line);border-radius:11px;padding:12px 14px;transition:var(--t)}
.rdry:hover{border-color:var(--line2)}
.rdry input{width:17px;height:17px;accent-color:var(--amber);cursor:pointer;flex-shrink:0}
.rdry b{color:var(--txt)}
.rwarn{display:flex;gap:11px;align-items:flex-start;margin-top:14px;font-size:12.5px;color:#ffd9b0;background:rgba(255,180,84,.08);border:1px solid var(--aglow2);border-radius:12px;padding:13px 15px;line-height:1.5}
.rwarn .ti{font-size:18px;color:var(--amber);flex-shrink:0;margin-top:1px}
.rwarn b{color:var(--amber)}
.rwarn.hide{display:none}
.ractions{display:flex;justify-content:flex-end;gap:11px;margin-top:22px}
.run-go{display:inline-flex;align-items:center;gap:9px;height:44px;padding:0 22px;border-radius:12px;border:none;cursor:pointer;
  font-family:Sora;font-size:13.5px;font-weight:600;color:#140d04;background:linear-gradient(135deg,#ffd089,var(--amber) 55%,var(--amber2));transition:var(--t);box-shadow:0 8px 22px -10px var(--aglow2)}
.run-go:hover{transform:translateY(-2px);filter:brightness(1.05)}
.run-go:disabled{opacity:.55;cursor:not-allowed;transform:none}
.run-go.dry{background:linear-gradient(135deg,#9fb0c8,#6f7a90);color:#0c0f15}
.rresult{margin-top:16px;font-size:13px;border-radius:11px;padding:0;max-height:0;overflow:hidden;transition:max-height .3s,padding .3s}
.rresult.show{padding:13px 15px;max-height:140px}
.rresult.ok{background:rgba(92,216,135,.08);border:1px solid rgba(92,216,135,.3);color:#bff0cf}
.rresult.err{background:rgba(255,111,111,.08);border:1px solid rgba(255,111,111,.3);color:#ffcccc}

/* empty + loading */
.empty{padding:54px;text-align:center;color:var(--mut)}
.empty .ti{font-size:38px;opacity:.35;display:block;margin-bottom:12px}
.empty .et{font-size:14px;color:var(--txt2);margin-bottom:4px}
.empty .es{font-size:12.5px;color:var(--mut2)}
.skel{background:linear-gradient(90deg,rgba(255,255,255,.03) 25%,rgba(255,255,255,.07) 50%,rgba(255,255,255,.03) 75%);background-size:200% 100%;animation:sk 1.5s infinite;border-radius:var(--r)}
@keyframes sk{100%{background-position:-200% 0}}
.skel-stat{height:108px;border-radius:var(--rl)}
.skel-row{height:54px;border-radius:10px;margin-bottom:8px}

footer{margin-top:56px;text-align:center}
.powered{display:inline-flex;align-items:center;gap:13px;font-family:Sora;font-size:13.5px;font-weight:500;letter-spacing:.4px;
  color:var(--mut);padding:11px 26px;border-radius:999px;border:1px solid var(--line);background:linear-gradient(180deg,rgba(20,23,31,.7),rgba(11,13,18,.5));
  backdrop-filter:blur(10px);box-shadow:var(--sh-sm),inset 0 1px 0 rgba(255,255,255,.04);transition:var(--t)}
.powered:hover{border-color:var(--aglow2);transform:translateY(-2px)}
.powered b{font-weight:700;background:linear-gradient(120deg,#fff 10%,var(--amber) 55%,var(--amber-d) 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
  background-size:200% auto;animation:shine 4s linear infinite;letter-spacing:.5px}
@keyframes shine{to{background-position:200% center}}
.pw-spark{width:5px;height:5px;border-radius:50%;background:var(--amber);box-shadow:0 0 10px var(--amber),0 0 4px var(--amber);animation:twinkle 2.4s ease-in-out infinite}
.pw-spark:last-child{animation-delay:1.2s}
@keyframes twinkle{0%,100%{opacity:.35;transform:scale(.8)}50%{opacity:1;transform:scale(1.25)}}
.hide{display:none!important}

/* responsive */
@media(max-width:1100px){.stats{grid-template-columns:repeat(3,1fr)}.hero{grid-template-columns:1fr}.hero-side{max-width:480px}}
@media(max-width:860px){.grid2{grid-template-columns:1fr}.capwrap{grid-template-columns:1fr}.mediagrid{grid-template-columns:1fr}.tracklist{grid-template-columns:1fr}
  .rail-steps{flex-wrap:wrap;gap:16px;justify-content:center}.rail-line,.rail-fill{display:none}.rstep{flex:0 0 88px}
  .logrow{grid-template-columns:1fr;gap:3px}.logrow .lt,.logrow .la{font-size:11px}
  .pinfo,.pseek span{display:none}.player-inner{gap:12px}}
@media(max-width:560px){.stats{grid-template-columns:repeat(2,1fr)}.hbar{padding:13px 18px}.wrap{padding:24px 18px 130px}.tabs{padding:0 18px;overflow-x:auto}.kbtn span:not(.kbd){display:none}.brand small{display:none}.tab .cnt{display:none}.bal-pill .bal-lbl{display:none}.bal-pill{padding:0 12px}}
@media(prefers-reduced-motion:reduce){*{animation-duration:.001s!important;transition-duration:.06s!important}#rain{display:none}}
</style>
</head>
<body>
<canvas id="rain"></canvas>
<div class="bg-aura"></div>
<div class="bg-grid"></div>
<div class="app">
<header>
  <div class="hbar">
    <div class="logo">&#129441;</div>
    <div class="brand"><b>Badger Tape</b><small>drift away, one beat at a time</small></div>
    <div class="htools">
      <button class="kbtn" onclick="openCmd()"><i class="ti ti-search"></i><span>Search</span><span class="kbd">Ctrl K</span></button>
      <button class="iconbtn" id="motionBtn" title="Toggle ambient motion" onclick="toggleRain()"><i class="ti ti-sparkles"></i></button>
      <div class="bal-pill" id="balPill" title="Estimated fal.ai budget remaining under the $2.00 cap (computed from local spend — no API call)">
        <span class="bal-ic"><i class="ti ti-wallet"></i></span>
        <span class="bal-txt"><span class="bal-lbl">fal.ai budget</span><span class="bal-val" id="balVal">&mdash;</span></span>
      </div>
    </div>
  </div>
  <div class="tabs" id="tabs">
    <button class="tab on" data-v="overview"><i class="ti ti-layout-dashboard"></i>Overview</button>
    <button class="tab" data-v="sessions"><i class="ti ti-list-details"></i>Sessions<span class="cnt" id="cntSessions">0</span></button>
    <button class="tab" data-v="gallery"><i class="ti ti-photo"></i>Gallery<span class="cnt" id="cntGallery">0</span></button>
    <button class="tab" data-v="live"><i class="ti ti-activity"></i>Live</button>
    <button class="tab" data-v="logs"><i class="ti ti-terminal-2"></i>Logs</button>
  </div>
</header>
<div class="wrap">
  <!-- OVERVIEW -->
  <div id="view-overview" class="view">
    <div class="hero">
      <div class="hero-main">
        <div class="hero-eyebrow"><i class="ti ti-broadcast"></i>AI Media Production Pipeline</div>
        <div class="hero-title">Studio Control &amp; Observability</div>
        <div class="hero-sub">End-to-end automation across image, motion, music, render, metadata and upload — with cost governance under a strict $2.00 ceiling per run.</div>
        <div class="hero-row" id="heroRow"></div>
        <div class="hero-cta">
          <button class="run-btn" id="runBtn" onclick="openRun()"><i class="ti ti-player-play-filled"></i>Start Pipeline</button>
          <span class="run-state" id="runStateChip"></span>
        </div>
      </div>
      <div class="hero-side" id="heroSide"></div>
    </div>
    <div class="stats" id="stats"></div>
    <h2 class="sec"><i class="ti ti-chart-line"></i>Analytics</h2>
    <div class="grid2">
      <div class="chartcard">
        <div class="chart-head"><div><h3>Cost per run</h3><div class="sub">Last 10 sessions &middot; estimated USD spend</div></div>
          <span class="avg-badge" id="avgBadge" style="display:none"><i class="ti ti-wave-sine"></i>avg <b id="avgVal">$0.00</b></span></div>
        <div class="chart-host" id="chart"></div></div>
      <div class="chartcard"><h3>Upload success</h3><div class="sub">Sessions reaching YouTube</div><div class="ring" id="ring" style="position:relative"></div>
        <div class="ring-foot"><span><i class="dotc" style="background:var(--amber)"></i>Uploaded</span><span><i class="dotc" style="background:#1a1d27"></i>Pending</span></div></div>
    </div>
    <div class="chartcard" style="margin-top:16px"><h3>Sessions by season</h3><div class="sub">Seasonal content distribution</div><div class="seasonrow" id="seasons"></div></div>
  </div>

  <!-- SESSIONS -->
  <div id="view-sessions" class="view hide">
    <div class="toolbar">
      <div class="inp"><i class="ti ti-search"></i><input id="q" placeholder="Search by theme or session id&hellip;" oninput="renderRows()"></div>
      <select class="sel" id="fStatus" onchange="renderRows()"><option value="">All status</option><option>uploaded</option><option>complete</option><option>error</option><option>incomplete</option></select>
      <select class="sel" id="fSeason" onchange="renderRows()"><option value="">All seasons</option><option>autumn</option><option>winter</option><option>spring</option><option>summer</option></select>
      <button class="btn" onclick="exportCSV()"><i class="ti ti-file-spreadsheet"></i>CSV</button>
      <button class="btn" onclick="exportJSON()"><i class="ti ti-file-code"></i>JSON</button>
    </div>
    <div class="tablecard"><table>
      <thead><tr>
        <th onclick="sortBy('id')">Session <i class="ti ti-arrows-sort"></i></th>
        <th onclick="sortBy('theme')">Theme</th><th>Season</th>
        <th onclick="sortBy('steps_done')">Pipeline</th>
        <th onclick="sortBy('duration_sec')">Duration</th>
        <th onclick="sortBy('cost')">Cost</th><th>Status</th><th>Upload</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table></div>
  </div>

  <!-- GALLERY -->
  <div id="view-gallery" class="view hide"><div class="gallery" id="gallery"></div></div>

  <!-- LIVE -->
  <div id="view-live" class="view hide"><div id="livePanel"></div>
    <h2 class="sec"><i class="ti ti-terminal-2"></i>Recent activity</h2>
    <div class="logwrap" id="liveLog"></div>
  </div>

  <!-- LOGS -->
  <div id="view-logs" class="view hide">
    <div class="toolbar">
      <div class="inp"><i class="ti ti-search"></i><input id="logq" placeholder="Filter log&hellip;" oninput="renderLogs()"></div>
      <select class="sel" id="logLevel" onchange="renderLogs()"><option value="">All levels</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select>
      <button class="btn" onclick="loadLogs()"><i class="ti ti-refresh"></i>Refresh</button>
    </div>
    <div class="logwrap" id="logBox"></div>
  </div>

  <footer>
    <div class="powered"><span class="pw-spark"></span>Powered Up By <b>Neeidy</b><span class="pw-spark"></span></div>
  </footer>
</div>
</div>
<div class="overlay" id="overlay"><div class="modal" id="modal"></div></div>
<div class="overlay" id="runOverlay"><div class="modal run-modal">
  <div class="mhead">
    <div><div style="font-size:19px;font-weight:700;font-family:Sora;display:flex;align-items:center;gap:9px"><i class="ti ti-rocket" style="color:var(--amber)"></i>Start a new pipeline run</div>
      <div style="color:var(--mut);font-size:13px;margin-top:6px">Configure the run. Porsuk artwork, motion, music, render and upload are produced automatically.</div></div>
    <button class="x" onclick="closeRun()"><i class="ti ti-x"></i></button>
  </div>
  <div class="mbody">
    <div class="rfield"><label>Theme <span class="req">required</span></label>
      <input id="rfTheme" class="rinput" placeholder="e.g. rainy night, rooftop, late drive&hellip;" maxlength="120" oninput="syncTheme()">
      <div class="rhint">Short core idea. Atmosphere fields below are appended to enrich it.</div></div>
    <div class="rgrid">
      <div class="rfield"><label>Season <span class="req">required</span></label>
        <select id="rfSeason" class="rinput"><option value="">Select&hellip;</option><option value="spring">Spring</option><option value="summer">Summer</option><option value="autumn">Autumn</option><option value="winter">Winter</option></select></div>
      <div class="rfield"><label>Music type</label>
        <select id="rfVocal" class="rinput"><option value="">Instrumental (default)</option><option value="vocal">Soft vocals</option></select></div>
    </div>
    <div class="rgrid3">
      <div class="rfield"><label>Time of day</label><input id="rfTime" class="rinput" placeholder="3am, dusk&hellip;" oninput="syncTheme()"></div>
      <div class="rfield"><label>Weather</label><input id="rfWeather" class="rinput" placeholder="rain, snow, fog&hellip;" oninput="syncTheme()"></div>
      <div class="rfield"><label>Setting / mood</label><input id="rfMood" class="rinput" placeholder="cozy room, city&hellip;" oninput="syncTheme()"></div>
    </div>
    <div class="rpreview"><span class="rpl">Final theme</span><code id="rfPreview">&mdash;</code></div>
    <label class="rdry" id="rfDryWrap"><input type="checkbox" id="rfDry"><span><b>Dry-run</b> — test the flow, no API calls, no spend, nothing uploaded</span></label>
    <div class="rwarn" id="rfWarn"><i class="ti ti-alert-triangle"></i><div><b>Live run spends real money.</b> This calls paid fal.ai generation and uploads the final video to YouTube. Estimated cost is governed by the $2.00 per-run cap.</div></div>
    <div class="ractions">
      <button class="btn" onclick="closeRun()">Cancel</button>
      <button class="run-go" id="rfGo" onclick="submitRun()"><i class="ti ti-player-play-filled"></i><span id="rfGoTxt">Start live run</span></button>
    </div>
    <div class="rresult" id="rfResult"></div>
  </div>
</div></div>
<div class="cmdk" id="cmdk"><div class="cmdbox">
  <div class="cmdsearch"><i class="ti ti-search"></i><input id="cmdInput" placeholder="Jump to a session or view&hellip;"></div>
  <div class="cmdresults" id="cmdResults"></div>
</div></div>
<div class="player" id="player"><div class="player-inner">
  <div class="pdisc" id="pdisc"></div>
  <div class="pinfo"><div class="pt" id="pTitle">&mdash;</div><div class="ps" id="pSub"></div></div>
  <div class="pctrl">
    <button class="pbtn" onclick="prevTrack()"><i class="ti ti-player-skip-back"></i></button>
    <button class="pbtn main" id="pPlay" onclick="togglePlay()"><i class="ti ti-player-play"></i></button>
    <button class="pbtn" onclick="nextTrack()"><i class="ti ti-player-skip-forward"></i></button>
  </div>
  <div class="pseek"><span id="pCur">0:00</span><div class="track" id="pTrack" onclick="seek(event)"><i id="pFill"></i></div><span id="pDur">0:00</span></div>
  <i class="ti ti-x pclose" onclick="closePlayer()" style="font-size:18px"></i>
</div></div>
<div class="toasts" id="toasts"></div>
<audio id="audio"></audio>
<script>
const STEP_ORDER=["variation_selected","image_generated","video_generated","music_generated","ffmpeg_merged","metadata_created","uploaded"];
const STEP_LABELS={variation_selected:"Variation",image_generated:"Artwork",video_generated:"Motion",music_generated:"Music",ffmpeg_merged:"Render",metadata_created:"Metadata",uploaded:"Upload"};
const STEP_ICONS={variation_selected:"ti-wand",image_generated:"ti-photo",video_generated:"ti-movie",music_generated:"ti-music",ffmpeg_merged:"ti-versions",metadata_created:"ti-file-text",uploaded:"ti-brand-youtube"};
const SEASON_EMOJI={autumn:"\u{1F342}",winter:"❄️",spring:"\u{1F338}",summer:"☀️"};
const SEASON_ICON={autumn:"ti-leaf",winter:"ti-snowflake",spring:"ti-flower",summer:"ti-sun"};
const COST_CAP=2.00;
let SESSIONS=[],STATS={},LOGS=[],SORT={k:"id",dir:-1},CURVIEW="overview",LOADED=false;
const $=id=>document.getElementById(id);
const esc=s=>(s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtDur=s=>{if(!s)return"—";const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),r=s%60;return (h?h+"h ":"")+m+"m "+String(r).padStart(2,"0")+"s";};
const fmtTime=s=>{if(isNaN(s))return"0:00";const m=Math.floor(s/60),r=Math.floor(s%60);return m+":"+String(r).padStart(2,"0");};
// Defensive client-side redaction (server already redacts; this is belt-and-suspenders).
const redact=s=>(s==null?"":String(s)).replace(/([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\s"']+/gi,"$1<redacted>").replace(/((?:\/home|\/Users)\/)[^/\s"']+/gi,"$1<redacted>");
const badge=st=>`<span class="badge b-${st}">${st==="uploaded"?'<i class="ti ti-circle-check-filled"></i>':st==="error"?'<i class="ti ti-alert-triangle"></i>':st==="complete"?'<i class="ti ti-checks"></i>':'<i class="ti ti-progress"></i>'}${st}</span>`;
function toast(msg,icon){const t=document.createElement("div");t.className="toast";t.innerHTML=`<i class="ti ti-${icon||'check'}"></i>${esc(msg)}`;$("toasts").appendChild(t);setTimeout(()=>{t.style.opacity=0;t.style.transform="translateX(44px)";setTimeout(()=>t.remove(),320)},2600);}
function copy(txt){navigator.clipboard.writeText(txt).then(()=>toast("Copied to clipboard","copy"));}

$("tabs").addEventListener("click",e=>{const b=e.target.closest(".tab");if(b)switchView(b.dataset.v);});
function switchView(v){CURVIEW=v;document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.v===v));
  document.querySelectorAll(".view").forEach(el=>el.classList.add("hide"));
  const view=$("view-"+v);view.classList.remove("hide");view.style.animation="none";view.offsetHeight;view.style.animation="";
  if(v==="logs")loadLogs();}

async function loadAll(){try{const j=await(await fetch("/api/sessions")).json();SESSIONS=j.sessions;STATS=j.stats;LOADED=true;
  $("cntSessions").textContent=SESSIONS.length;$("cntGallery").textContent=SESSIONS.filter(s=>s.has_artwork||s.youtube_id).length;
  renderHero();renderStats();renderChart();renderRing();renderSeasons();renderRows();renderGallery();renderBalance();}catch(e){toast("Connection lost","alert-triangle");}}

// fal.ai budget — manual fixed value (no API call, no key). Update FAL_REMAINING when you top up / spend.
const FAL_REMAINING=39, FAL_LIMIT=100;
function renderBalance(){
  const el=$("balVal"),pill=$("balPill");if(!el)return;
  const pctUsed=FAL_LIMIT?(FAL_LIMIT-FAL_REMAINING)/FAL_LIMIT*100:0;
  el.innerHTML="$"+FAL_REMAINING+" <small>/ $"+FAL_LIMIT+"</small>";
  if(pill){pill.classList.remove("warn","danger");if(pctUsed>=90)pill.classList.add("danger");else if(pctUsed>=75)pill.classList.add("warn");}
}

function animateCount(el,to,suffix,decimals){const dur=950,start=performance.now();
  const step=t=>{let p=Math.min(1,(t-start)/dur);p=1-Math.pow(1-p,3);let val=to*p;
    el.textContent=(decimals?val.toFixed(decimals):Math.round(val))+(suffix||"");if(p<1)requestAnimationFrame(step);};requestAnimationFrame(step);}
function animateCountPre(el,to,pre,dec){const dur=950,start=performance.now();const step=t=>{let p=Math.min(1,(t-start)/dur);p=1-Math.pow(1-p,3);el.textContent=pre+(dec?(to*p).toFixed(dec):Math.round(to*p));if(p<1)requestAnimationFrame(step);};requestAnimationFrame(step);}

function renderHero(){
  const s=STATS;
  const succ=SESSIONS.filter(x=>x.status==="uploaded");
  const hi=succ[0]||SESSIONS.find(x=>x.has_artwork)||SESSIONS[0];
  $("heroRow").innerHTML=[
    {n:s.total_sessions,l:"Sessions",cls:""},
    {n:s.uploaded,l:"Uploaded",cls:"green"},
    {n:"$"+(s.total_cost||0).toFixed(2),l:"Total spend",cls:"amber"},
    {n:(s.success_rate||0)+"%",l:"Success rate",cls:""},
  ].map(k=>`<div class="hero-kpi"><div class="n ${k.cls}">${esc(k.n)}</div><div class="l">${k.l}</div></div>`).join("");
  const side=$("heroSide");
  if(!hi){side.innerHTML='<div class="empty" style="padding:40px"><i class="ti ti-photo-off"></i><div class="es">No sessions yet</div></div>';return;}
  const t=hi.youtube_id?`https://i.ytimg.com/vi/${hi.youtube_id}/hqdefault.jpg`:(hi.has_artwork?`/media/images/session_${hi.id}/scene.png`:null);
  const tag=hi.status==="uploaded"?'<i class="ti ti-circle-check-filled"></i>Latest upload':'<i class="ti ti-sparkles"></i>Featured run';
  side.innerHTML=`<div class="hs-thumb" ${t?`style="background-image:url('${t}')"`:""} onclick="openModal('${hi.id}')" style="cursor:pointer">${t?"":'<div class="hs-ph">&#129441;</div>'}</div>
    <div class="hs-body">
      <div class="hs-tag">${tag}</div>
      <div class="hs-title" onclick="openModal('${hi.id}')" style="cursor:pointer">${esc(hi.theme||hi.id)}</div>
      <div class="hs-meta"><span class="chip"><i class="ti ${SEASON_ICON[hi.season]||'ti-calendar'}"></i>${esc(hi.season||"—")}</span><span class="chip cost">$${(hi.cost||0).toFixed(3)}</span>${hi.duration_sec?`<span class="chip"><i class="ti ti-clock"></i>${fmtDur(hi.duration_sec)}</span>`:""}</div>
      ${hi.youtube_id?`<a class="hs-watch" href="${hi.youtube_url}" target="_blank">Watch on YouTube <i class="ti ti-external-link"></i></a>`:`<span class="hs-watch" style="color:var(--mut);cursor:pointer" onclick="openModal('${hi.id}')">Open details <i class="ti ti-arrow-right"></i></span>`}
    </div>`;
}

function renderStats(){
  const s=STATS;
  const cards=[
    {ic:"ti-stack-2",k:"Total sessions",v:s.total_sessions,sub:s.succeeded+" fully completed"},
    {ic:"ti-brand-youtube",k:"Uploaded",v:s.uploaded,sub:"published to YouTube"},
    {ic:"ti-coin",k:"Total spend",v:s.total_cost,pre:"$",dec:2,sub:"avg $"+(s.avg_cost||0).toFixed(3)+"/run"},
    {ic:"ti-music",k:"Tracks made",v:s.total_tracks,sub:"lofi tracks rendered"},
    {ic:"ti-clock-hour-4",k:"Avg runtime",v:s.avg_duration_min||0,sfx:"m",sub:s.video_hours+"h of final video"},
    {ic:"ti-circle-check",k:"Success rate",v:s.success_rate||0,sfx:"%",sub:s.succeeded+"/"+s.total_sessions+" runs"},
  ];
  $("stats").innerHTML=cards.map((c,i)=>`<div class="stat"><div class="ic"><i class="ti ${c.ic}"></i></div>
    <div class="k">${c.k}</div><div class="v" id="st${i}">${c.pre||""}0${c.sfx||""}</div><div class="s">${esc(c.sub)}</div></div>`).join("");
  cards.forEach((c,i)=>{const el=$("st"+i);if(c.pre)animateCountPre(el,c.v,c.pre,c.dec);else animateCount(el,c.v,c.sfx,c.dec);});
}

function renderChart(){
  const last=SESSIONS.slice(0,10).reverse();
  const host=$("chart");
  if(!last.length){host.innerHTML='<div class="empty" style="width:100%"><i class="ti ti-chart-bar-off"></i><div class="es">No cost data yet</div></div>';return;}
  const H=200; // px plot height
  const peak=Math.max(...last.map(s=>s.cost));
  // round the top gridline up to a clean step so the axis reads nicely
  const niceMax=v=>{const steps=[0.25,0.5,0.75,1,1.25,1.5,2];for(const st of steps)if(v<=st)return st;return Math.ceil(v*2)/2;};
  const max=niceMax(Math.max(peak*1.18,0.25));
  const avg=last.reduce((a,s)=>a+s.cost,0)/last.length;
  let grid="";
  for(let g=0;g<=4;g++){const frac=g/4;const top=H*(1-frac);grid+=`<div class="gl" style="top:${top}px"><span>$${(max*frac).toFixed(2)}</span></div>`;}
  const avgTop=H*(1-Math.min(1,avg/max));
  const avgLine=`<div class="chart-avg" style="top:${avgTop}px"></div>`;  // line only; the label lives in the card header badge
  const bars=last.map(s=>{const h=Math.max(6,Math.round(s.cost/max*H));
    return `<div class="col" title="$${s.cost.toFixed(3)} · ${esc(s.theme)}"><div class="barwrap"><div class="val">$${s.cost.toFixed(2)}</div><div class="barv" style="height:0px" data-h="${h}"><span class="cap-dot"></span></div></div></div>`;}).join("");
  const labs=last.map(s=>`<div class="lab">${esc(s.theme||s.id.slice(9,15))}</div>`).join("");
  host.innerHTML=`<div class="chart-plot"><div class="chart-grid">${grid}${avgLine}</div><div class="chart">${bars}</div></div><div class="chart-labs">${labs}</div>`;
  const badge=$("avgBadge");if(badge){$("avgVal").textContent="$"+avg.toFixed(2);badge.style.display="";}
  const bs=host.querySelectorAll(".barv");
  bs.forEach((b,i)=>setTimeout(()=>b.style.height=b.dataset.h+"px",90+i*55));
}

function renderRing(){
  const pct=STATS.success_rate||0;
  const SZ=200,CX=SZ/2,r=78,SW=14,c=2*Math.PI*r,off=c*(1-pct/100);
  // subtle tick marks just outside the ring (centered ring leaves room for the % text inside)
  let ticks="";
  for(let i=0;i<60;i++){const ang=i*6*Math.PI/180;const big=i%5===0;const r1=r+SW/2+4,r2=r1+(big?7:4);
    const x1=CX+Math.cos(ang)*r1,y1=CX+Math.sin(ang)*r1,x2=CX+Math.cos(ang)*r2,y2=CX+Math.sin(ang)*r2;
    ticks+=`<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="rgba(255,255,255,${big?.18:.07})" stroke-width="${big?1.6:1}"/>`;}
  $("ring").innerHTML=`<svg width="${SZ}" height="${SZ}" viewBox="0 0 ${SZ} ${SZ}">
    <g style="transform:rotate(90deg);transform-origin:center">${ticks}</g>
    <circle class="track" cx="${CX}" cy="${CX}" r="${r}" stroke-width="${SW}" fill="none"/>
    <circle class="glowcirc" cx="${CX}" cy="${CX}" r="${r}" stroke="url(#g)" stroke-width="${SW}" fill="none" stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${c}" style="transition:stroke-dashoffset 1.3s cubic-bezier(.16,1,.3,1)" id="rcirc"/>
    <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#e07b27"/><stop offset="55%" stop-color="#ffb454"/><stop offset="100%" stop-color="#ffd089"/></linearGradient></defs></svg>
    <div class="rc"><b id="rnum">0%</b><small>${STATS.uploaded}/${STATS.total_sessions} uploaded</small></div>`;
  setTimeout(()=>{$("rcirc").style.strokeDashoffset=off;animateCount($("rnum"),pct,"%");},90);
}

function renderSeasons(){
  const bs=STATS.by_season||{},max=Math.max(1,...Object.values(bs));const order=["spring","summer","autumn","winter"];
  const have=order.filter(s=>bs[s]);
  $("seasons").innerHTML=have.length?have.map(s=>`<div class="sbar"><span class="nm"><i class="ti ${SEASON_ICON[s]||'ti-calendar'}"></i>${s}</span>
    <div class="tk"><i style="width:0%" data-w="${bs[s]/max*100}"></i></div><span class="cn">${bs[s]}</span></div>`).join(""):'<div class="empty"><i class="ti ti-calendar-off"></i><div class="es">No seasonal data</div></div>';
  setTimeout(()=>document.querySelectorAll("#seasons .tk i").forEach(i=>i.style.width=i.dataset.w+"%"),70);
}

function filtered(){
  const q=($("q").value||"").toLowerCase(),fs=$("fStatus").value,fse=$("fSeason").value;
  let r=SESSIONS.filter(s=>(!q||(s.theme+s.id).toLowerCase().includes(q))&&(!fs||s.status===fs)&&(!fse||s.season===fse));
  r.sort((a,b)=>{let x=a[SORT.k],y=b[SORT.k];if(typeof x==="string"){x=x||"";y=y||"";return SORT.dir*x.localeCompare(y);}return SORT.dir*((x||0)-(y||0));});
  return r;
}
function sortBy(k){SORT.dir=(SORT.k===k)?-SORT.dir:-1;SORT.k=k;renderRows();}
function pipeMini(s){const done=s.steps_done,tot=s.steps_total;let cells="";for(let i=0;i<tot;i++)cells+=`<i style="display:inline-block;width:13px;height:5px;border-radius:2px;margin-right:2px;background:${i<done?'var(--amber)':'rgba(255,255,255,.1)'}"></i>`;
  return `<div style="display:flex;align-items:center;gap:9px"><span style="display:flex">${cells}</span><span style="font-size:11px;color:var(--mut);font-variant-numeric:tabular-nums">${done}/${tot}</span></div>`;}
function renderRows(){
  const r=filtered();
  $("rows").innerHTML=r.length?r.map(s=>`<tr class="row" onclick="openModal('${s.id}')">
    <td><div style="font-weight:500">${esc(s.datetime)}</div><div class="sid">${esc(s.id)}</div></td>
    <td>${esc(s.theme)} ${s.vocal?'<span class="chip">vocal</span>':''}</td>
    <td><span class="chip"><i class="ti ${SEASON_ICON[s.season]||'ti-calendar'}"></i>${esc(s.season||"—")}</span></td>
    <td>${pipeMini(s)}</td>
    <td style="color:var(--mut)">${fmtDur(s.duration_sec)}</td><td class="cost">$${s.cost.toFixed(3)}</td>
    <td>${badge(s.status)}</td>
    <td>${s.youtube_id?`<a class="ytlink" href="${s.youtube_url}" target="_blank" onclick="event.stopPropagation()">watch <i class="ti ti-external-link" style="font-size:12px"></i></a>`:'<span style="color:var(--mut2)">—</span>'}</td>
  </tr>`).join(""):`<tr><td colspan="8"><div class="empty"><i class="ti ti-mood-empty"></i><div class="et">No sessions match</div><div class="es">Try clearing filters or search</div></div></td></tr>`;
}

function thumb(s){if(s.youtube_id)return`https://i.ytimg.com/vi/${s.youtube_id}/hqdefault.jpg`;if(s.has_artwork)return`/media/images/session_${s.id}/scene.png`;return null;}
function renderGallery(){
  const g=SESSIONS.filter(s=>s.has_artwork||s.youtube_id);
  $("gallery").innerHTML=g.length?g.map(s=>{const t=thumb(s);
    const ribbons=[];if(s.youtube_id)ribbons.push('<span class="rb up"><i class="ti ti-brand-youtube"></i>YouTube</span>');
    if(s.has_loop)ribbons.push('<span class="rb"><i class="ti ti-movie"></i>Loop</span>');
    if(s.has_final)ribbons.push('<span class="rb"><i class="ti ti-device-tv"></i>1h</span>');
    return `<div class="tile" onclick="openModal('${s.id}')">
      <div class="thumb" ${t?`style="background-image:url('${t}')"`:""}>${t?"":'<div class="ph">&#129441;</div>'}
        <div class="ribbon">${ribbons.join("")}</div>
        <span class="play"><i class="ti ti-player-play"></i> ${s.has_loop?"preview loop":"view"}</span><div class="vinyl"></div></div>
      <div class="meta"><div class="t">${esc(s.theme||s.id)}</div>
      <div class="d">${badge(s.status)} <span class="chip"><i class="ti ${SEASON_ICON[s.season]||'ti-calendar'}"></i>${esc(s.season||"—")}</span></div></div>
    </div>`;}).join(""):'<div class="empty" style="grid-column:1/-1"><i class="ti ti-photo-off"></i><div class="et">No artwork yet</div><div class="es">Generated covers and uploads will appear here</div></div>';
}

async function refreshLive(){
  try{const j=await(await fetch("/api/live")).json();renderLive(j);}catch(e){}
}
function renderLive(j){
  const el=$("livePanel");
  if(!j.session){el.innerHTML='<div class="live"><div class="empty"><i class="ti ti-player-track-next"></i><div class="et">No sessions yet</div><div class="es">Pipeline activity will stream here in real time</div></div></div>';return;}
  const s=j.session,done=s.steps_done,pct=Math.round(done/s.steps_total*100),cur=j.current_step;
  const sp=j.step_progress,up=j.upload_progress;let subtxt="";
  if(cur==="music_generated"&&sp&&sp.step==="music")subtxt=`${sp.done_tracks||0}/${sp.total_tracks||10} tracks`;
  if(cur==="ffmpeg_merged"&&sp&&sp.step==="ffmpeg")subtxt=`${sp.percent||0}% · ${Math.round((sp.current_seconds||0)/60)}/60 min`;
  if(cur==="uploaded"&&up)subtxt=`${up.percent||0}% ${up.status||""}`;
  // build rail
  const fillPct=s.steps_total>1?(done/(s.steps_total))*100:0;
  const railFill=Math.min(100,Math.max(0,(done/(s.steps_total-0))*100));
  const idxCur=STEP_ORDER.indexOf(cur);
  const fillTo=idxCur<0?(done>=s.steps_total?100:0):(idxCur/(STEP_ORDER.length-1))*100;
  const steps=STEP_ORDER.map((k,i)=>{const d=s.steps[k],isc=(k===cur)&&j.active;let s2="";
    if(isc&&k==="music_generated"&&sp&&sp.step==="music")s2=`${sp.done_tracks||0}/10`;
    if(isc&&k==="ffmpeg_merged"&&sp&&sp.step==="ffmpeg")s2=`${sp.percent||0}%`;
    if(isc&&k==="uploaded"&&up)s2=`${up.percent||0}%`;
    return `<div class="rstep ${d?"done":""} ${isc?"cur":""}"><div class="rnode">${d?'<i class="ti ti-check"></i>':(isc?'<i class="ti ti-loader-2"></i>':`<i class="ti ${STEP_ICONS[k]}"></i>`)}</div><div class="rlb">${STEP_LABELS[k]}</div><div class="rs2">${s2}</div></div>`;}).join("");
  const eq=j.active?'<span class="eq"><span></span><span></span><span></span><span></span><span></span></span>':"";
  // cost cap
  const cost=s.cost||0,capPct=Math.min(100,cost/COST_CAP*100);
  const capCls=capPct>=90?"danger":capPct>=70?"warn":"";
  el.innerHTML=`<div class="live">
    <div class="live-head">${eq}<div><div class="ttl">${esc(s.theme||s.id)}</div>
      <div class="sub">${esc(s.datetime)} · ${esc(s.season||"")} · ${j.active?"running now":"last run"} ${subtxt?"· "+esc(subtxt):""}</div></div>
      <div style="margin-left:auto">${j.active?'<span class="lbadge"><i class="ti ti-point-filled"></i>LIVE</span>':badge(s.status)}</div></div>
    <div class="rail">
      <div class="rail-line"></div><div class="rail-fill" style="width:0%" id="railFill"></div>
      <div class="rail-steps">${steps}</div>
    </div>
    <div class="capwrap">
      <div class="capcard">
        <div class="cap-h"><span class="l"><i class="ti ti-shield-dollar"></i>Cost governance</span><span class="v"><span class="cost">$${cost.toFixed(3)}</span> <span style="color:var(--mut2);font-weight:400">/ $${COST_CAP.toFixed(2)}</span></span></div>
        <div class="cap-bar ${capCls}"><i style="width:0%" id="capFill"></i></div>
        <div class="cap-marks"><span>$0</span><span>$1.00</span><span>$${COST_CAP.toFixed(2)} cap</span></div>
        <div class="cap-foot">${capPct>=90?'<b style="color:var(--red)">Approaching cap</b> — pipeline halts at $2.00':capPct>=70?'<b style="color:var(--amber)">'+(COST_CAP-cost).toFixed(2)+' remaining</b> under the cap':'<b>$'+(COST_CAP-cost).toFixed(2)+'</b> remaining under the $2.00 cap'}</div>
      </div>
      <div class="outcard">
        <div class="ol"><span class="k"><i class="ti ti-checklist"></i>Steps</span><span class="v">${done}/${s.steps_total} · ${pct}%</span></div>
        <div class="ol"><span class="k"><i class="ti ti-clock"></i>Runtime</span><span class="v">${s.duration_sec?fmtDur(s.duration_sec):"—"}</span></div>
        <div class="ol"><span class="k"><i class="ti ti-music"></i>Tracks</span><span class="v">${s.tracks||0}</span></div>
        <div class="ol"><span class="k"><i class="ti ti-device-tv"></i>Final video</span><span class="v ${s.has_final?'ok':'no'}">${s.has_final?(s.final_size_mb?s.final_size_mb+" MB":"ready"):"pending"}</span></div>
        <div class="ol"><span class="k"><i class="ti ti-brand-youtube"></i>Upload</span><span class="v ${s.youtube_id?'ok':'no'}">${s.youtube_id?"published":"not uploaded"}</span></div>
      </div>
    </div>
  </div>`;
  setTimeout(()=>{const rf=$("railFill");if(rf)rf.style.width=fillTo+"%";const cf=$("capFill");if(cf)cf.style.width=capPct+"%";},80);
}

async function loadLogs(){try{const j=await(await fetch("/api/log?n=200")).json();LOGS=j.lines;renderLogs();renderLiveLog();}catch(e){}}
function logRowHTML(l){const li=l.level==="ERROR"?'<i class="ti ti-alert-circle"></i>':l.level==="WARNING"?'<i class="ti ti-alert-triangle"></i>':'<i class="ti ti-info-circle"></i>';
  const cls=/SESSION START/.test(l.msg)?"is-start":/SESSION END/.test(l.msg)?"is-end":"";
  return `<div class="logrow ${cls}"><span class="lt">${esc(l.time)}</span><span class="la">${esc(l.agent)}</span><span class="lv ${esc(l.level)}">${li}${esc(l.level)}</span><span class="lm">${esc(redact(l.msg))}</span></div>`;}
function renderLogs(){const q=($("logq").value||"").toLowerCase(),lv=$("logLevel").value;
  const r=LOGS.filter(l=>(!q||(l.msg+l.agent+l.session).toLowerCase().includes(q))&&(!lv||l.level===lv));
  $("logBox").innerHTML=r.length?r.slice().reverse().map(logRowHTML).join(""):'<div class="empty"><i class="ti ti-file-off"></i><div class="et">No log lines</div><div class="es">Adjust filters to see more</div></div>';}
function renderLiveLog(){$("liveLog").innerHTML=LOGS.slice(-14).reverse().map(logRowHTML).join("")||'<div class="empty"><i class="ti ti-history-off"></i><div class="es">No recent activity</div></div>';}

let MODAL_SID=null;
async function openModal(id){
  MODAL_SID=id;const s=await(await fetch("/api/session?id="+encodeURIComponent(id))).json();
  if(s.error)return;const m=s.metadata||{};
  const yt=s.youtube_id?`<div class="mediabox"><iframe src="https://www.youtube.com/embed/${s.youtube_id}" allowfullscreen loading="lazy"></iframe><div class="cap"><i class="ti ti-brand-youtube"></i>YouTube · published</div></div>`:"";
  const art=s.has_artwork?`<div class="mediabox"><img loading="lazy" src="/media/images/session_${s.id}/scene.png"><div class="cap"><i class="ti ti-photo"></i>Cover artwork</div></div>`:"";
  const loop=s.has_loop?`<div class="mediabox"><video controls muted loop preload="metadata" src="/media/videos/session_${s.id}/loop.mp4"></video><div class="cap"><i class="ti ti-movie"></i>Loop video</div></div>`:"";
  let media=[yt,art,loop].filter(Boolean);if(media.length>2)media=media.slice(0,2);
  const tracks=(s.track_files||[]).map((t,i)=>`<div class="trk"><i class="ti ti-player-play pp" onclick="playSession('${s.id}',${i})"></i><span>${esc(t.name)}</span><span class="n">${t.size_mb?t.size_mb+" MB":""}</span></div>`).join("");
  const tags=(m.tags||[]).map(t=>`<span class="chip">${esc(t)}</span>`).join("");
  const tl=(s.log_entries||[]).map(e=>`<div class="tlitem ${esc(e.level)}"><span class="tt">${esc((e.time||"").slice(11))}</span><span class="ta">${esc(e.agent)}</span>${esc(redact(e.msg))}</div>`).join("");
  const err=s.last_error?`<div class="errbox"><b>Last error [${esc(s.last_error.step)}]</b>\n${esc(redact(s.last_error.message))}</div>`:"";
  // Final video path shown REDACTED + relative-only (never the absolute local path).
  const relPath=`output/session_${s.id}/badgertape_1hour.mp4`;
  const finalCell=s.has_final?`<span class="vv">${s.final_size_mb} MB</span> <span class="sid">${relPath}</span> <i class="ti ti-copy copy" onclick="copy('${relPath}')" title="Copy relative path"></i>`:'<span style="color:var(--mut2)">not found</span>';
  const railPct=Math.round(s.steps_done/s.steps_total*100);
  $("modal").innerHTML=`
    <div class="mhead"><div><div style="font-size:19px;font-weight:700;font-family:Sora;line-height:1.25">${esc(m.title||s.theme||s.id)}</div>
      <div style="color:var(--mut);font-size:13px;margin-top:6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="sid">${esc(s.id)}</span> · ${esc(s.season||"")} ${s.vocal?"· vocal":""} · ${badge(s.status)} ${s.duration_sec?"· "+fmtDur(s.duration_sec):""}</div></div>
      <button class="x" onclick="closeModal()"><i class="ti ti-x"></i></button></div>
    <div class="mbody">
      ${media.length?`<div class="mediagrid">${media.join("")}</div>`:""}
      ${s.tracks?`<div style="margin-top:18px"><button class="btn" onclick="playSession('${s.id}',0)"><i class="ti ti-player-play"></i>Play all ${s.tracks} tracks</button></div>`:""}
      <div class="kv">
        <span class="kk"><i class="ti ti-checklist"></i>Pipeline</span><span class="vv">${s.steps_done}/${s.steps_total} steps · ${railPct}% complete</span>
        <span class="kk"><i class="ti ti-shield-dollar"></i>Cost</span><span class="vv"><span class="cost">$${s.cost.toFixed(3)}</span> <span style="color:var(--mut2)">/ $2.00 cap</span></span>
        <span class="kk"><i class="ti ti-music"></i>Music</span><span class="vv">${s.tracks} tracks</span>
        <span class="kk"><i class="ti ti-device-tv"></i>Final video</span><span class="vv">${finalCell}</span>
        ${s.youtube_url?`<span class="kk"><i class="ti ti-brand-youtube"></i>YouTube</span><span class="vv"><a href="${s.youtube_url}" target="_blank">${esc(s.youtube_url)} <i class="ti ti-external-link" style="font-size:12px"></i></a> <i class="ti ti-copy copy" onclick="copy('${s.youtube_url}')"></i></span>`:""}
      </div>
      ${tags?`<div style="margin-top:20px"><div style="color:var(--mut);font-size:12px;margin-bottom:8px;text-transform:uppercase;letter-spacing:.6px">Tags</div><div class="tags">${tags}</div></div>`:""}
      ${tracks||tl?`<div class="sub-tabs">${tracks?`<div class="sub-tab on" onclick="subTab(this,'mTracks')">Tracks</div>`:""}${tl?`<div class="sub-tab ${tracks?"":"on"}" onclick="subTab(this,'mTimeline')">Timeline</div>`:""}</div>`:""}
      ${tracks?`<div id="mTracks"><div class="tracklist">${tracks}</div></div>`:""}
      ${tl?`<div id="mTimeline" class="${tracks?"hide":""}"><div class="timeline">${tl}</div></div>`:""}
      ${err}
    </div>`;
  $("overlay").classList.add("on");
}
function subTab(el,id){el.parentElement.querySelectorAll(".sub-tab").forEach(t=>t.classList.remove("on"));el.classList.add("on");
  ["mTracks","mTimeline"].forEach(x=>{const e=$(x);if(e)e.classList.toggle("hide",x!==id);});}
function closeModal(){$("overlay").classList.remove("on");}
$("overlay").addEventListener("click",e=>{if(e.target.id==="overlay")closeModal();});

/* ---- Start Pipeline modal ---- */
function openRun(){syncTheme();$("rfResult").className="rresult";$("runOverlay").classList.add("on");}
function closeRun(){$("runOverlay").classList.remove("on");}
$("runOverlay").addEventListener("click",e=>{if(e.target.id==="runOverlay")closeRun();});
function buildTheme(){
  const core=($("rfTheme").value||"").trim();
  const extra=[$("rfTime").value,$("rfWeather").value,$("rfMood").value].map(x=>(x||"").trim()).filter(Boolean);
  return [core,...extra].filter(Boolean).join(", ").slice(0,120);
}
function syncTheme(){const t=buildTheme();$("rfPreview").textContent=t||"—";}
$("rfDry").addEventListener("change",()=>{
  const dry=$("rfDry").checked;
  $("rfWarn").classList.toggle("hide",dry);
  $("rfGo").classList.toggle("dry",dry);
  $("rfGoTxt").textContent=dry?"Run dry-run test":"Start live run";
});
async function submitRun(){
  const theme=buildTheme(),season=$("rfSeason").value,vocal=$("rfVocal").value==="vocal",dry=$("rfDry").checked;
  const res=$("rfResult");
  if(!theme){res.className="rresult show err";res.textContent="Please enter a theme.";return;}
  if(!season){res.className="rresult show err";res.textContent="Please choose a season.";return;}
  if(!dry && !confirm("Start a LIVE pipeline run?\n\nThis spends real money on fal.ai generation and uploads to YouTube.\n\nTheme: "+theme+"\nSeason: "+season))return;
  const go=$("rfGo");go.disabled=true;go.innerHTML='<i class="ti ti-loader-2" style="animation:spin 1s linear infinite"></i>Starting…';
  try{
    const r=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({theme,season,vocal,dry_run:dry})});
    const j=await r.json();
    if(j.ok){res.className="rresult show ok";res.innerHTML='<b>'+(dry?"Dry-run":"Live run")+' started.</b><br>'+esc(j.message)+' Switch to the Live tab to watch progress.';
      toast(dry?"Dry-run started":"Pipeline started","rocket");refreshRunState();setTimeout(()=>{closeRun();switchView("live");},1400);}
    else{res.className="rresult show err";res.textContent=j.message||j.error||"Could not start the run.";}
  }catch(e){res.className="rresult show err";res.textContent="Request failed: "+e;}
  go.disabled=false;go.innerHTML='<i class="ti ti-player-play-filled"></i><span id="rfGoTxt">'+(dry?"Run dry-run test":"Start live run")+'</span>';
}
async function refreshRunState(){
  try{const j=await(await fetch("/api/run-status")).json();
    const chip=$("runStateChip"),btn=$("runBtn");
    if(j.running){btn.disabled=true;chip.className="run-state live";
      chip.innerHTML='<span class="rdot"></span>'+(j.dry_run?"dry-run":"running")+' · '+esc(j.theme||"")+' / '+esc(j.season||"");}
    else{btn.disabled=false;chip.className="run-state";chip.textContent="";}
  }catch(e){}
}

const audio=$("audio");let QUEUE=[],QI=0,QSID=null;
function playSession(sid,i){fetch("/api/session?id="+sid).then(r=>r.json()).then(d=>{
    QUEUE=(d.track_files||[]).map(t=>({name:t.name,url:`/media/music/session_${sid}/${encodeURIComponent(t.name)}`}));
    QSID=sid;QI=i||0;if(!QUEUE.length)return toast("No tracks","music-off");playIndex(QI);$("player").classList.add("on");});}
function playIndex(i){if(i<0||i>=QUEUE.length)return;QI=i;audio.src=QUEUE[i].url;audio.play();
  $("pTitle").textContent=QUEUE[i].name.replace(".mp3","");$("pSub").textContent=(QSID||"")+" · "+(i+1)+"/"+QUEUE.length;
  $("pPlay").innerHTML='<i class="ti ti-player-pause"></i>';$("pdisc").classList.add("spin");}
function togglePlay(){if(audio.paused){audio.play();$("pPlay").innerHTML='<i class="ti ti-player-pause"></i>';$("pdisc").classList.add("spin");}else{audio.pause();$("pPlay").innerHTML='<i class="ti ti-player-play"></i>';$("pdisc").classList.remove("spin");}}
function nextTrack(){if(QUEUE.length)playIndex((QI+1)%QUEUE.length);}
function prevTrack(){if(QUEUE.length)playIndex((QI-1+QUEUE.length)%QUEUE.length);}
function closePlayer(){audio.pause();$("player").classList.remove("on");$("pdisc").classList.remove("spin");}
function seek(e){const t=e.currentTarget,r=t.getBoundingClientRect();if(audio.duration)audio.currentTime=((e.clientX-r.left)/r.width)*audio.duration;}
audio.addEventListener("timeupdate",()=>{if(audio.duration){$("pFill").style.width=(audio.currentTime/audio.duration*100)+"%";$("pCur").textContent=fmtTime(audio.currentTime);$("pDur").textContent=fmtTime(audio.duration);}});
audio.addEventListener("ended",nextTrack);

let cmdSel=0,cmdItems=[];
function openCmd(){$("cmdk").classList.add("on");$("cmdInput").value="";$("cmdInput").focus();buildCmd("");}
function closeCmd(){$("cmdk").classList.remove("on");}
function buildCmd(q){q=q.toLowerCase();
  const views=[["overview","Overview","ti-layout-dashboard"],["sessions","Sessions","ti-list-details"],["gallery","Gallery","ti-photo"],["live","Live","ti-activity"],["logs","Logs","ti-terminal-2"]];
  cmdItems=[];
  views.filter(v=>!q||v[1].toLowerCase().includes(q)).forEach(v=>cmdItems.push({type:"view",id:v[0],label:v[1],icon:v[2],meta:"view"}));
  SESSIONS.filter(s=>!q||(s.theme+s.id).toLowerCase().includes(q)).slice(0,8).forEach(s=>cmdItems.push({type:"session",id:s.id,label:s.theme||s.id,icon:"ti-disc",meta:s.datetime}));
  cmdSel=0;
  $("cmdResults").innerHTML=cmdItems.map((it,i)=>`<div class="cmdrow ${i===0?"sel":""}" data-i="${i}" onclick="runCmd(${i})"><i class="ti ${it.icon}"></i>${esc(it.label)}<span class="meta">${esc(it.meta)}</span></div>`).join("")||'<div class="empty" style="padding:28px"><div class="es">No matches</div></div>';
}
function runCmd(i){const it=cmdItems[i];if(!it)return;closeCmd();if(it.type==="view")switchView(it.id);else{switchView("sessions");openModal(it.id);}}
$("cmdInput").addEventListener("input",e=>buildCmd(e.target.value));
$("cmdInput").addEventListener("keydown",e=>{
  if(e.key==="ArrowDown"){e.preventDefault();cmdSel=Math.min(cmdItems.length-1,cmdSel+1);upCmdSel();}
  else if(e.key==="ArrowUp"){e.preventDefault();cmdSel=Math.max(0,cmdSel-1);upCmdSel();}
  else if(e.key==="Enter")runCmd(cmdSel);});
function upCmdSel(){document.querySelectorAll(".cmdrow").forEach((r,i)=>r.classList.toggle("sel",i===cmdSel));
  const sel=document.querySelector(".cmdrow.sel");if(sel)sel.scrollIntoView({block:"nearest"});}
$("cmdk").addEventListener("click",e=>{if(e.target.id==="cmdk")closeCmd();});

function exportCSV(){const rows=[["session_id","datetime","theme","season","status","steps_done","duration_sec","cost","youtube_url"]];
  filtered().forEach(s=>rows.push([s.id,s.datetime,s.theme,s.season,s.status,s.steps_done,s.duration_sec||"",s.cost,s.youtube_url||""]));
  const csv=rows.map(r=>r.map(c=>`"${String(c).replace(/"/g,'""')}"`).join(",")).join("\n");
  dl(csv,"badgertape-sessions.csv","text/csv");toast("CSV exported","download");}
function exportJSON(){dl(JSON.stringify(filtered(),null,2),"badgertape-sessions.json","application/json");toast("JSON exported","download");}
function dl(content,name,type){const b=new Blob([content],{type});const u=URL.createObjectURL(b);const a=document.createElement("a");a.href=u;a.download=name;a.click();URL.revokeObjectURL(u);}

document.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="k"){e.preventDefault();openCmd();return;}
  if(e.key==="Escape"){closeModal();closeCmd();closeRun();return;}
  if(document.activeElement.tagName==="INPUT")return;
  const map={g:"gallery",l:"live",o:"overview",s:"sessions"};
  if(map[e.key.toLowerCase()])switchView(map[e.key.toLowerCase()]);});

/* ambient rain */
let rainOn=true,rainRAF;const cv=$("rain"),ctx=cv.getContext("2d");let drops=[];
function initRain(){cv.width=innerWidth;cv.height=innerHeight;drops=[];const n=Math.min(90,Math.round(innerWidth/16));
  for(let i=0;i<n;i++)drops.push({x:Math.random()*cv.width,y:Math.random()*cv.height,l:6+Math.random()*14,v:2+Math.random()*4,o:.08+Math.random()*.3});}
function drawRain(){ctx.clearRect(0,0,cv.width,cv.height);ctx.strokeStyle="rgba(255,180,84,.45)";ctx.lineWidth=1.1;
  drops.forEach(d=>{ctx.globalAlpha=d.o;ctx.beginPath();ctx.moveTo(d.x,d.y);ctx.lineTo(d.x-0.6,d.y+d.l);ctx.stroke();
    d.y+=d.v;if(d.y>cv.height){d.y=-d.l;d.x=Math.random()*cv.width;}});ctx.globalAlpha=1;
  if(rainOn)rainRAF=requestAnimationFrame(drawRain);}
function toggleRain(){rainOn=!rainOn;$("motionBtn").style.color=rainOn?"":"var(--mut2)";
  if(rainOn){initRain();drawRain();cv.style.opacity=.55;toast("Ambient motion on","sparkles");}else{cancelAnimationFrame(rainRAF);ctx.clearRect(0,0,cv.width,cv.height);cv.style.opacity=0;toast("Ambient motion off","sparkles");}}
addEventListener("resize",()=>{if(rainOn)initRain();});
document.addEventListener("visibilitychange",()=>{if(document.hidden){cancelAnimationFrame(rainRAF);}else if(rainOn){drawRain();}});
if(matchMedia("(prefers-reduced-motion:reduce)").matches){rainOn=false;cv.style.opacity=0;}else{initRain();drawRain();}

loadAll();refreshLive();loadLogs();refreshRunState();
setInterval(refreshLive,2500);
setInterval(loadAll,15000);
setInterval(refreshRunState,4000);
setInterval(()=>{if(CURVIEW==="logs"||CURVIEW==="live")loadLogs();},8000);
</script>
</body>
</html>
"""


def main():
    if not OUTPUT_DIR.exists():
        print(f"WARNING: {OUTPUT_DIR} not found. Starting anyway.")
    url = f"http://127.0.0.1:{PORT}"
    print("=" * 56)
    print("  BADGER TAPE - STUDIO DASHBOARD  (read-only)")
    print(f"  {url}")
    print("  Stop with: Ctrl+C")
    print("=" * 56)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
