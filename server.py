"""
server.py — Ten Forward web app + job worker.

Run:  venv\Scripts\python.exe server.py   (or start.bat)
Env: TF_PORT (8410), TF_HOST (0.0.0.0), TF_VOICES / TF_PERSONAL (feature flags), TF_NO_WORKER (1 = API only, no jobs).
Everything else is a setting: the Settings tab writes the DB, an environment variable is the fallback, then the
built-in default in config.py.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any

TF_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TF_ROOT))
LOG_DIR = TF_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_KEEP_DAYS = 2   # logs roll at midnight and only the last two days are kept, here and on the phones
_server_log = logging.handlers.TimedRotatingFileHandler(LOG_DIR / "server.log", when="midnight", backupCount=LOG_KEEP_DAYS, encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[logging.StreamHandler(sys.stdout), _server_log])
LOG = logging.getLogger("tenforward")

import engine as engine_mod  # noqa: E402  (chdirs into the Wan2GP root, must come before fastapi static paths are resolved)
import audio_utils  # noqa: E402
import llm  # noqa: E402
try:
    import voice_ft  # noqa: E402
except Exception as _voice_err:  # the release build ships without the voice code; every voice route then answers 404
    voice_ft = None
    LOG.info("voice features are not installed (%s)", _voice_err)
import loras as loras_mod  # noqa: E402
import listen  # noqa: E402
import radio  # noqa: E402
import importer  # noqa: E402
import themes  # noqa: E402
import wizard  # noqa: E402
import lyric_intel  # noqa: E402
import sleeptones  # noqa: E402
import sheet  # noqa: E402
import subprocess  # noqa: E402
from db import get_db, new_id  # noqa: E402
import config  # noqa: E402
import auth  # noqa: E402
from config import cfg  # noqa: E402

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

STATIC = TF_ROOT / "static"
LIBRARY = TF_ROOT / "library"
VOICES = TF_ROOT / "voices"
UPLOADS = TF_ROOT / "uploads"
for d in (LIBRARY, VOICES, UPLOADS, LIBRARY / "_raw", LIBRARY / "_tmp"):
    d.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("TF_PORT", "8410"))   # a port change needs a restart, so it stays an environment variable
HOST = os.environ.get("TF_HOST", "0.0.0.0")     # phones have to reach it: LAN and tailnet only, never a public address
NO_WORKER = os.environ.get("TF_NO_WORKER", "") not in ("", "0", "false")  # API only (used to check a second copy of the tree)
API_VERSION = 2                                 # bump when a client-visible response shape changes (2: users, features, settings)
VERSION = "1.6.1"


def songs_dir() -> Path:
    """Where hearted songs are copied as plain files (a setting; the folder is created when it is missing)."""
    return config.path_cfg("songs_dir")


def voices_on() -> bool:
    return bool(cfg("voices_enabled")) and voice_ft is not None


def personal_on() -> bool:
    return bool(cfg("personal_enabled"))


def need_voices():
    """Dependency on every voice route: switched off (or not installed) reads as 'not here'."""
    if not voices_on():
        raise HTTPException(404, "voice features are switched off")


def features() -> dict[str, Any]:
    return {"voices": voices_on(), "personal": personal_on(), "voices_available": voice_ft is not None,
            "login_required": auth.login_required(db)}


def urls() -> dict[str, str]:
    return {"lan": str(cfg("lan_url") or ""), "https": str(cfg("https_url") or "")}

db = get_db()
engine = engine_mod.get_engine()
app = FastAPI(title="Ten Forward", version=VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], expose_headers=["Content-Range", "Accept-Ranges"])  # LAN/tailnet only, no public exposure
# uvicorn runs with log_config=None (see __main__): uvicorn.error propagates to the root handlers above (console + server.log)
_access = logging.getLogger("uvicorn.access")
_access.addHandler(logging.handlers.TimedRotatingFileHandler(LOG_DIR / "access.log", when="midnight", backupCount=LOG_KEEP_DAYS, encoding="utf-8"))
_access.propagate = False


@app.middleware("http")
async def _who_is_listening(request, call_next):
    """Signs the request in from its cookie, its bearer token, or ?token= on /media (players cannot send headers),
    and turns anonymous calls away. With one password-less person and no 'always ask' setting, everybody is that
    person and nothing is ever asked."""
    try:
        request.state.user = auth.resolve(db, request)
    except Exception:
        LOG.exception("auth: could not work out who is asking")
        request.state.user = None
    if request.state.user is None and request.method != "OPTIONS" and not auth.is_public_path(request.url.path):
        return JSONResponse(status_code=401, content={"detail": "login"})
    return await call_next(request)


@app.middleware("http")
async def _no_stale_shell(request, call_next):
    """Phones must pick up UI updates on the next load: static shell files revalidate every time (ETag keeps it cheap)."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/") or path in ("/sw.js", "/manifest.webmanifest", "/listen/", "/listen/app.js", "/listen/app.css"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(Exception)
async def _unhandled(request, exc):
    LOG.error("unhandled error on %s %s: %s\n%s", request.method, request.url.path, exc, traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})

STYLE_PRESETS = [
    {"name": "Country road", "style": "English, country, warm male baritone, acoustic guitar, pedal steel, upright bass, brushed drums, steady, 92 BPM"},
    {"name": "Indie pop", "style": "English, indie pop, bright female vocal, jangly electric guitar, synth pad, bass, tight drums, 112 BPM"},
    {"name": "Arena rock", "style": "English, arena rock, powerful male tenor, distorted guitars, bass, big drums, anthemic chorus, 132 BPM"},
    {"name": "Late night soul", "style": "English, neo soul, smooth male vocal, Rhodes piano, electric bass, soft drums, warm horns, 84 BPM"},
    {"name": "Folk porch", "style": "English, folk, gentle female vocal, fingerpicked acoustic guitar, mandolin, upright bass, light percussion, 100 BPM"},
    {"name": "Synthwave", "style": "English, synthwave, airy female vocal, analog synths, gated drums, driving bass, 118 BPM"},
    {"name": "Hip hop", "style": "English, boom bap hip hop, confident male rap vocal, dusty drums, deep bass, piano sample, 92 BPM"},
    {"name": "Cyber metal", "style": "English, industrial metal, aggressive male vocal, down tuned guitars, synths, double kick drums, 150 BPM"},
    {"name": "Gospel", "style": "English, gospel, soaring female lead with choir, piano, organ, bass, drums, hand claps, 96 BPM"},
    {"name": "Instrumental cinematic", "style": "Instrumental, cinematic electronic, warm pads, analog bass, slow drums, piano motif, 85 BPM"},
]


# --------------------------------------------------------------------------- helpers
def now() -> float:
    return time.time()


def maybe_retry_clipped(params: dict[str, Any], song: dict[str, Any]) -> None:
    """A song that hit the duration cap mid-ending gets one more go with more room.

    YuE2 stops at `duration` rather than aiming for it, so a song can end on a fade instead of an
    ending. radio.ask_seconds already pads the ask from the channel's own history; this catches the
    ones that still ran out. Once only - the retry is marked so it cannot loop."""
    try:
        if not cfg("retry_clipped") or params.get("_clipped_retry"):
            return
        if "clipped" not in (song.get("tags") or []):
            return
        if (params.get("source") or "") != "radio":
            return
        room = int(min(float(params.get("duration") or 180) * 1.3, 600))
        if room <= int(params.get("duration") or 0):
            return
        again = {**params, "duration": room, "_clipped_retry": True, "seed": -1}
        enqueue("song", again, priority=2)
        LOG.info("radio: '%s' was clipped at %ss; trying once more with %ss of room",
                 params.get("title"), params.get("duration"), room)
    except Exception:
        LOG.exception("clipped retry check failed")


def _first_line(text: Any) -> str:
    """The first line with something on it, for naming a job. Lyrics that are only blank
    lines give nothing back rather than raising, which used to take the Queue tab down."""
    lines = str(text or "").strip().splitlines()
    return lines[0][:40] if lines else ""


def enqueue(job_type: str, params: dict[str, Any], priority: int = 10) -> dict[str, Any]:
    params = dict(params)
    params.setdefault("requested_by", "auto" if priority <= 3 else ("you" if priority == 5 else "studio"))
    job = {"id": new_id("job_"), "type": job_type, "status": "queued", "params": params, "progress": 0.0, "phase": "queued", "message": "Waiting for the engine", "created": now(), "priority": priority}
    return db.insert("jobs", job)


# A channel that stops producing used to do it in complete silence: both park rules were a bare
# `continue`. Now it is said once in the log, carried on /api/status so the deck and the app can
# show it, and raised on the fleet board if one is reachable - and it clears itself the moment the
# channel plans a song again.
def note_parked(station: dict[str, Any], why: str) -> None:
    sid = station.get("id") or "?"
    prev = db.setting(f"station_parked:{sid}")
    if prev and now() - float(prev.get("since") or 0) < 3600:
        return                                      # already said, and said recently
    db.set_setting(f"station_parked:{sid}", {"since": now(), "why": why, "name": station.get("name")})
    LOG.warning("radio: %s is not producing - %s", station.get("name") or sid, why)
    fleet_alert("raise", f"tenforward-{sid}", f"Ten Forward: {station.get('name') or sid} is not making songs - {why}")


def clear_parked(station: dict[str, Any]) -> None:
    sid = station.get("id") or "?"
    if db.setting(f"station_parked:{sid}"):
        LOG.info("radio: %s is producing again", station.get("name") or sid)
        db.drop_setting(f"station_parked:{sid}")
        fleet_alert("clear", f"tenforward-{sid}", "")
    db.drop_setting(f"station_plan_failed:{sid}")


def parked_stations() -> list[dict[str, Any]]:
    out = []
    for key, val in db.settings_prefix("station_parked:").items():
        if isinstance(val, dict):
            out.append({"id": key, "name": val.get("name") or key, "why": val.get("why"), "since": val.get("since")})
    return out


def fleet_alert(action: str, key: str, text: str) -> None:
    """Best effort: the fleet alert board, if one is set. Never a reason to fail a render."""
    url = str(cfg("fleet_alert_url") or "").strip()
    if not url:
        return
    try:
        body = json.dumps({"key": key, "text": text, "source": "ten-forward"}).encode()
        req = urllib.request.Request(f"{url.rstrip('/')}/{action}", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=4).read()
    except Exception:
        pass


def queue_paused() -> bool:
    return bool(db.setting("queue_paused", False))


CLIENT_LOG = LOG_DIR / "client.log"
CLIENT_LOG_MAX = 4000          # lines, so one shouting phone cannot fill the disk
_client_log_pruned = 0.0


def client_log(entry: dict[str, Any]) -> None:
    """One line of JSON per report from a phone or a browser. Nothing older than two days is kept."""
    entry = dict(entry)
    entry.setdefault("at", now())
    with CLIENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False)[:2000] + "\n")
    prune_client_log()


def client_log_entries(limit: int = 200) -> list[dict[str, Any]]:
    """Newest first, already inside the two days."""
    cutoff = now() - LOG_KEEP_DAYS * 86400
    out: list[dict[str, Any]] = []
    if not CLIENT_LOG.exists():
        return out
    try:
        lines = CLIENT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        e = _client_log_parse(line)
        if e is not None and e.get("at", 0) >= cutoff:
            out.append(e)
    out.reverse()
    return out[:limit]


def _client_log_parse(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        e = json.loads(line)
        if isinstance(e, dict):
            e.setdefault("at", 0.0)
            return e
    except ValueError:
        pass
    # the older format: "2026-09-17 12:00:00 {json}"
    try:
        stamp, rest = line[:19], line[20:]
        e = json.loads(rest)
        e["at"] = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
        return e
    except Exception:
        return None


def prune_client_log(force: bool = False) -> None:
    global _client_log_pruned
    if not force and now() - _client_log_pruned < 900:
        return
    _client_log_pruned = now()
    if not CLIENT_LOG.exists():
        return
    cutoff = now() - LOG_KEEP_DAYS * 86400
    try:
        lines = CLIENT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        keep = [e for e in (_client_log_parse(x) for x in lines) if e is not None and e.get("at", 0) >= cutoff]
        keep = keep[-CLIENT_LOG_MAX:]
        CLIENT_LOG.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep), encoding="utf-8")
    except OSError as e:
        LOG.debug("could not prune the client log: %s", e)


def job_update(job_id: str, **fields):
    db.update("jobs", job_id, **fields)


def ui_build() -> str:
    """Newest mtime of the app shell; the page reloads itself between songs when this changes, so a phone never runs stale JS for a day again."""
    try:
        return str(int(max((STATIC / f).stat().st_mtime for f in ("index.html", "app.js", "app.css"))))
    except Exception:
        return "0"


def power_on() -> bool:
    return bool(db.setting("power", True))


def gpu_used_mb() -> int | None:
    """Whole-GPU memory in use (nvidia-smi); per-process numbers are not available under WDDM."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return None


GPU_TOTAL_MB = 24576


MAX_YIELDS = 2          # a song that has stepped aside twice keeps the card the third time


def butler_wants_it(above: int) -> str | None:
    """The name of whoever is waiting for the card with a priority above `above`, if anybody is.

    Ten Forward holds the card at priority 0; an image or video you asked for queues at 5. The
    butler will hand it over the moment we let go - it just will not take it off us."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:9200/status", timeout=3) as r:
            q = json.loads(r.read() or b"{}").get("queue") or []
        for w in q:
            if int(w.get("priority") or 0) > above:
                return str(w.get("name") or "something")
    except Exception:
        return None            # no butler, or it is not answering: carry on rendering
    return None


def wait_for_llm_vram(max_wait_s: int = 240) -> bool:
    """The lyric model and YuE2 share the graphics card. If a render is in flight and VRAM is tight, free ComfyUI's idle
    models and wait for the render to finish rather than letting Ollama spill onto the CPU."""
    t0 = now()
    needs_mb = int(cfg("llm_vram_mb"))
    while True:
        used = gpu_used_mb()
        free = (GPU_TOTAL_MB - used) if used is not None else GPU_TOTAL_MB
        if free >= needs_mb or llm.loaded_models():
            return True
        if not engine.busy:
            engine.comfy_free_if_idle()
            used = gpu_used_mb()
            free = (GPU_TOTAL_MB - used) if used is not None else GPU_TOTAL_MB
            if free >= needs_mb:
                return True
            return True  # nothing else to free; let Ollama fit what it can
        if now() - t0 > max_wait_s:
            LOG.warning("planner: waited %ds for VRAM (free %d MB), going ahead anyway", max_wait_s, free)
            return False
        time.sleep(10)


def set_power(on: bool, force: bool = False) -> dict[str, Any]:
    """ON: reclaim the GPU (ComfyUI drops its models when idle) and warm YuE2 in the background.
    OFF: hold every GPU job, cancel a running radio song, and release YuE2/Seed-VC/Whisper from VRAM."""
    db.set_setting("power", bool(on))
    info: dict[str, Any] = {"power": bool(on)}
    if on:
        info["comfy_freed"] = engine.comfy_free_if_idle()
        info["comfy_busy"] = engine.comfy_busy()
        for j in db.query("jobs", "status='queued'"):
            job_update(j["id"], message="Waiting for the engine")
        if not engine.is_loaded() or db.setting("engine_unloaded_at", 0) >= engine.last_used:
            def warm():
                try:
                    engine.session()
                except Exception:
                    LOG.exception("power on: warm-up failed")
            threading.Thread(target=warm, daemon=True, name="tf-warm").start()
            info["warming"] = True
    else:
        for j in db.query("jobs", "status='queued'"):
            job_update(j["id"], message="Holding: power is off")
        cur = worker.current
        if cur and ((cur.get("params") or {}).get("source") == "radio" or force):
            worker.cancel_event.set()
            info["cancelled_job"] = cur["id"]

        def release():
            for _ in range(600):
                if not engine.busy:
                    break
                time.sleep(1)
            if not power_on():
                engine.unload()
                llm.unload_model()
                db.set_setting("engine_unloaded_at", now())
                LOG.info("power off: models released (YuE2, Seed-VC, Whisper, lyric model)")
        threading.Thread(target=release, daemon=True, name="tf-release").start()
        info["releasing"] = True
    info["gpu_used_mb"] = gpu_used_mb()
    return info


def first_lyric_line(lyrics: str) -> str:
    for line in (lyrics or "").splitlines():
        s = line.strip()
        if s and not s.startswith("["):
            return re.sub(r"[\\/:*?\"<>|]", "", s)[:48]
    return ""


def song_dir(song_id: str) -> Path:
    d = LIBRARY / song_id
    d.mkdir(parents=True, exist_ok=True)
    return d


_STATION_RET: dict[str, tuple[float, float | None]] = {}


def station_retention_days(station_id: str | None) -> float | None:
    """A station's own retention (0 = never delete, None = the radio default), cached for 30 s."""
    if not station_id:
        return None
    hit = _STATION_RET.get(station_id)
    if hit and now() - hit[0] < 30:
        return hit[1]
    st = db.get("stations", station_id)
    val = st.get("retention_days") if st else None
    val = None if val is None or val == "" else float(val)
    _STATION_RET[station_id] = (now(), val)
    return val


def expires_at(song: dict[str, Any]) -> float | None:
    """When an unhearted song will be deleted; None = kept forever (hearted, imported, made by hand in Create, or on a
    station whose retention is 0 such as My Songs).

    The clock starts when a song is first PLAYED, not when it was made. A song the radio built and
    nobody has heard yet is the whole point of the buffer - deleting it on age alone threw away
    audio the GPU had already paid for. Unplayed songs are still capped (keep_unplayed_days) so a
    channel nobody listens to cannot fill the disk on its own."""
    if song.get("liked"):
        return None
    if song.get("banned"):
        return float(song.get("created") or 0) + float(cfg("banned_retention_h")) * 3600
    if (song.get("source") or "") == "import":
        return None
    if (song.get("source") or "") == "radio":
        days = station_retention_days(song.get("station_id"))
        if days is None:
            days = float(cfg("retention_days"))
        if days <= 0:
            return None
        created = float(song.get("created") or 0)
        if not song.get("plays"):
            cap = float(cfg("keep_unplayed_days"))
            return None if cap <= 0 else created + cap * 86400
        # heard: the retention window runs from when it was heard, or from when it was made if an
        # older song has a play count but no timestamp for it
        heard = float(song.get("last_played") or 0) or created
        return heard + days * 86400
    return None


def public_song(song: dict[str, Any]) -> dict[str, Any]:
    s = dict(song)
    files = s.get("files") or {}
    s["urls"] = {k: f"/media/{s['id']}/{k}" for k in files}
    s["expires_at"] = expires_at(song)
    return s


def _safe_name(text: str, fallback: str = "song") -> str:
    out = re.sub(r"[\\/:*?\"<>|]+", "", text or "").strip().rstrip(".")
    return out[:80] or fallback


def export_song(song: dict[str, Any]) -> Path | None:
    """Copy the best mix + a notes file into <favourites folder>/<Channel>/<Title>.mp3 so hearted songs live outside
    the app. Switched off with the 'Copy hearted songs out as files' setting; the song is kept either way."""
    if not cfg("export_liked"):
        return None
    files = song.get("files") or {}
    src_dir = LIBRARY / song["id"]
    best = None
    for key in ("mp3", "voiced", "master"):
        if files.get(key) and (src_dir / files[key]).exists():
            best = src_dir / files[key]
            break
    if best is None:
        return None
    st = db.get("stations", song.get("station_id") or "") if song.get("station_id") else None
    folder = songs_dir() / _safe_name(st["name"] if st else ("Created" if (song.get("source") or "") != "radio" else "Radio"))
    folder.mkdir(parents=True, exist_ok=True)
    base = _safe_name(song.get("title"), song["id"])
    dst = folder / f"{base}{best.suffix.lower()}"
    if dst.exists() and dst.stat().st_size != best.stat().st_size:
        dst = folder / f"{base} ({song['id'][-4:]}){best.suffix.lower()}"
    shutil.copy2(best, dst)
    notes = dst.with_suffix(".txt")
    meta = [f"# {song.get('title')}", f"Station: {st['name'] if st else song.get('source') or ''}", f"Style: {song.get('style') or ''}"]
    if song.get("theme"):
        meta.append(f"Theme: {song['theme']}")
    if song.get("singer"):
        meta.append(f"Singer: {song['singer']}")
    meta.append(f"Made: {time.strftime('%Y-%m-%d %H:%M', time.localtime(float(song.get('created') or time.time())))}")
    meta.append(f"Ten Forward id: {song['id']}")
    notes.write_text("\n".join(meta) + "\n\n" + (song.get("lyrics") or "").strip() + "\n", encoding="utf-8")
    return dst


def unexport_song(song: dict[str, Any]) -> None:
    path = song.get("saved_path")
    if not path:
        return
    p = Path(path)
    try:
        if p.exists() and songs_dir() in p.parents:
            p.unlink()
            p.with_suffix(".txt").unlink(missing_ok=True)
    except Exception:
        LOG.exception("could not remove exported copy %s", path)


def cleanup_expired() -> int:
    """Delete unhearted songs past their expiry (the retention settings, or a channel's own). Never a hearted song."""
    removed = 0
    for song in db.query("songs", "status='ready' AND COALESCE(liked,0)=0 AND (source='radio' OR COALESCE(banned,0)=1)"):
        exp = expires_at(song)
        if exp is None or exp > now():
            continue
        try:
            db.delete("songs", song["id"])
            shutil.rmtree(LIBRARY / song["id"], ignore_errors=True)
            removed += 1
        except Exception:
            LOG.exception("cleanup: could not delete %s", song["id"])
    if removed:
        LOG.info("cleanup: deleted %d expired song(s) (radio > %.0f days, banned > %.0f h)", removed, float(cfg("retention_days")), float(cfg("banned_retention_h")))
    return removed


def _stage_fraction(phase: str, status: str, step, total, progress=None) -> float:
    text = f"{phase} {status}".lower()
    if progress is not None:
        try:
            pct = float(progress)
            if 0 <= pct <= 100:
                return max(0.01, min(0.92, pct / 100.0))
        except Exception:
            pass
    inner = 0.0
    if step is not None and total:
        try:
            inner = max(0.0, min(1.0, float(step) / float(total)))
        except Exception:
            inner = 0.0
    if "load" in text or "download" in text:
        return 0.02 + 0.03 * inner
    if "score" in text or "transcri" in text or "sheetsage" in text:
        return 0.06 + 0.08 * inner
    if "semantic" in text:
        return 0.15 + 0.45 * inner
    if "acoustic" in text or "synthesis" in text or "denois" in text:
        return 0.62 + 0.28 * inner
    if "decod" in text:
        return 0.91 + 0.07 * inner
    if "voice" in text or "seedvc" in text or "replace" in text:
        return 0.93 + 0.05 * inner
    return 0.05


def friendly_phase(phase: str, status: str) -> str:
    text = f"{phase} {status}".lower()
    if "download" in text:
        return "Downloading model files"
    if "load" in text:
        return "Loading YuE2"
    if "score" in text or "sheetsage" in text or "transcri" in text:
        return "Writing the score"
    if "semantic" in text:
        return "Composing the performance"
    if "acoustic" in text or "synthesis" in text:
        return "Rendering audio"
    if "decod" in text:
        return "Decoding to stereo"
    if "voice" in text or "seedvc" in text or "replace" in text:
        return "Re-voicing"
    return (status or phase or "Working").strip()[:80]


# --------------------------------------------------------------------------- worker
class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="tf-worker")
        self.current: dict[str, Any] | None = None
        self.cancel_event = threading.Event()
        self.yielded_to: str | None = None      # set when a render stood aside for a higher-priority ask
        self._last_radio_check = 0.0
        self._last_cleanup = 0.0

    def run(self):
        LOG.info("worker started")
        # jobs left 'running' by a crash go back to the queue
        for j in db.query("jobs", "status='running'"):
            job_update(j["id"], status="queued", message="Restarted after server restart")
        while True:
            try:
                job = db.next_job() if (power_on() and not queue_paused()) else None
                if job is None:
                    self.idle_tick()
                    time.sleep(1.0)
                    continue
                self.run_job(job)
            except Exception:
                LOG.exception("worker loop error")
                time.sleep(2)

    # ---- idle behaviour: model unload only (planning runs in the Scheduler thread so it continues during renders)
    def idle_tick(self):
        t = now()
        idle_min = float(cfg("idle_unload_min"))
        if engine.is_loaded() and not engine.busy and idle_min > 0 and t - engine.last_used > idle_min * 60 and db.count("jobs", "status='queued'") == 0:
            if db.setting("engine_unloaded_at", 0) < engine.last_used:
                LOG.info("idle for %.0f min: releasing models", idle_min)
                engine.unload()
                db.set_setting("engine_unloaded_at", now())

    def radio_keep_ahead(self):
        """Keeps every channel stocked, planning in side threads so LLM latency never blocks the GPU.

        Up to TF_PLANNERS channels are planned at once. The LLM itself is serialised by its own
        process-wide lock, so the writing still happens one at a time - but an instrumental channel
        needs no LLM at all, and a planner waiting on VRAM no longer holds up every other channel."""
        if not db.setting("radio_autofill", True) or not power_on():
            return
        self._planners = [t for t in getattr(self, "_planners", []) if t.is_alive()]
        if len(self._planners) >= max(1, int(cfg("planners"))):
            return
        cap = int(cfg("radio_queue_cap"))
        # A channel made a minute ago owns the planner until it has its first songs: nothing else is queued,
        # so somebody can listen to another channel meanwhile without the two of them taking turns on the GPU.
        # Only the new channel's own songs count against the cap, or a full line for whatever is playing would
        # stop the new channel being planned at all.
        new_ids = [b["id"] for b in radio.priming(db)["stations"]]
        if new_ids:
            if sum(radio.queued_count(db, sid) for sid in new_ids) < cap and self.plan_one(new_ids, new=True):
                return
            # every new channel is either stocked or parked after a failure: fall through rather than go silent
        # planned-but-unrendered radio songs are capped so the Queue tab stays short and steerable
        if db.count("jobs", "type='song' AND status IN ('queued','running') AND priority <= 3") >= cap:
            return
        self.plan_one(None, new=False)

    def plan_one(self, only: list[str] | None, new: bool) -> bool:
        """Starts the planner on the first station that wants a song. True when one was started."""
        stations = db.query("stations", "enabled=1 AND auto_generate=1", order="sort ASC, created ASC")
        if only is not None:
            stations = [st for st in stations if st["id"] in only]
        for st in stations:
            st["_tuned_ago"] = now() - float(db.setting(f"station_last_tuned:{st['id']}", 0) or 0)
        # the station somebody is listening to comes first, then the emptiest station
        stations.sort(key=lambda x: (0 if x["_tuned_ago"] < 30 * 60 else 1, radio.unplayed_count(db, x["id"], rendered_only=True) + radio.queued_count(db, x["id"]), x.get("sort") or 0))
        for st in stations:
            active = st["_tuned_ago"] < 30 * 60
            # 0 is a real answer here ("stop stocking this channel"), so it cannot be treated as absent
            keep = int(st["keep_ahead"] if st.get("keep_ahead") is not None else cfg("keep_ahead_default"))
            have_n = radio.unplayed_count(db, st["id"], rendered_only=True) + radio.queued_count(db, st["id"])
            have_s, coming_s = radio.buffer_seconds(db, st)
            want_s = radio.target_seconds(st)
            if keep <= 0:
                continue                     # this channel has been told to stop
            if new:
                if have_n >= radio.prime_target(st):
                    continue
            # Minutes of audio is the unit that matters - a count of songs says five 2-minute songs
            # and five 5-minute songs are the same cushion, and they are not. keep_ahead stays as
            # the floor in songs so nobody's existing setting quietly changes meaning.
            elif have_n >= keep and (have_s + coming_s) >= want_s:
                continue
            # skip a station that failed in the last 10 minutes so one broken station cannot starve the others,
            # and park a station whose songs keep failing (2+ failed generations in the last hour)
            if now() - float(db.setting(f"station_plan_failed:{st['id']}", 0) or 0) < 600:
                continue
            if db.count("jobs", "type='song' AND status='failed' AND finished > ? AND params LIKE ?", (now() - 3600, f'%"station_id": "{st["id"]}"%')) >= 2:
                note_parked(st, "two songs in a row failed to render in the last hour")
                continue
            LOG.info("radio: planning a song for %s (%d songs, %.0f of %.0f min ready, active=%s, new=%s)",
                     st["id"], have_n, (have_s + coming_s) / 60, want_s / 60, active, new)
            # a brand new channel goes in front of the ordinary radio line (3 and 0) but behind anything a
            # person actually asked for (5 and 10), so its first songs are what the GPU does next
            priority = 4 if new else (3 if active else 0)

            def plan(station=st, prio=priority):
                t0 = now()
                try:
                    if not station.get("instrumental"):
                        wait_for_llm_vram()
                    params = radio.plan_song_for_station(db, station)
                    enqueue("song", params, priority=prio)
                    LOG.info("radio: queued '%s' for station %s (planned in %.0fs, priority %d)", params.get("title"), station["id"], now() - t0, prio)
                    clear_parked(station)
                except Exception as e:
                    LOG.exception("radio: planning failed for %s", station["id"])
                    db.set_setting(f"station_plan_failed:{station['id']}", now())
                    note_parked(station, str(e)[:160])

            t = threading.Thread(target=plan, daemon=True, name=f"tf-planner-{st['id'][:12]}")
            self._planners = [x for x in getattr(self, "_planners", []) if x.is_alive()]
            self._planners.append(t)
            self._planner = t          # kept for anything still reading the old single-planner field
            t.start()
            return True
        return False

    def watch_for_asks(self, job: dict[str, Any]) -> threading.Event | None:
        """While a background song renders, keep an eye out for something you asked for.

        Returns the event that stops the watcher, or None when this job is not one that should
        ever give way (you asked for it yourself, or it has already stood aside twice)."""
        if not cfg("yield_to_asks"):
            return None
        if int(job.get("priority") or 0) > 3:
            return None                                   # you asked for this song; it keeps the card
        if int((job.get("params") or {}).get("_yields") or 0) >= MAX_YIELDS:
            return None                                   # already stood aside twice; finish it
        stop = threading.Event()

        def watch():
            while not stop.wait(2.0):
                who = butler_wants_it(above=0)
                if who:
                    self.yielded_to = who
                    LOG.info("radio: standing aside for %s - '%s' goes back in the queue",
                             who, (job.get("params") or {}).get("title") or job["id"])
                    self.cancel_event.set()
                    return

        threading.Thread(target=watch, daemon=True, name="tf-yield-watch").start()
        return stop

    def requeue_after_yield(self, job: dict[str, Any]) -> None:
        """Put a song that gave way back in the line, one step up so it goes first when the card
        is free again, and count the yield so it cannot be pushed aside for ever."""
        p = dict(job.get("params") or {})
        p["_yields"] = int(p.get("_yields") or 0) + 1
        db.update("jobs", job["id"], status="queued", started=None, finished=None, progress=0.0,
                  phase="queued", message=f"Stood aside for {self.yielded_to or 'something you asked for'}",
                  params=p, priority=int(job.get("priority") or 0) + 1)

    # ---- dispatch
    def run_job(self, job: dict[str, Any]):
        self.current = job
        self.cancel_event.clear()
        self.yielded_to = None
        job_update(job["id"], status="running", started=now(), phase="starting", message="Starting", progress=0.01)
        t0 = now()
        stop_watch = self.watch_for_asks(job) if job["type"] == "song" else None
        try:
            handler = {"song": self.job_song, "revoice": self.job_revoice, "voice_prep": self.job_voice_prep, "transcribe": self.job_transcribe, "stems": self.job_stems,
                       "voice_train": self.job_voice_train, "arrange": self.job_arrange}[job["type"]]
            result = handler(job) or {}
            job_update(job["id"], status="done", finished=now(), progress=1.0, phase="done", message=result.get("message", f"Done in {now() - t0:.0f}s"), song_id=result.get("song_id"))
        except engine_mod.Cancelled:
            if self.yielded_to:
                # not a cancellation: this song stepped aside for something you asked for, and goes
                # back in the line rather than being thrown away
                self.requeue_after_yield(job)
            else:
                job_update(job["id"], status="cancelled", finished=now(), phase="cancelled", message="Cancelled")
        except Exception as e:
            LOG.error("job %s failed: %s\n%s", job["id"], e, traceback.format_exc())
            job_update(job["id"], status="failed", finished=now(), phase="failed", message=str(e)[:400], error=traceback.format_exc()[-4000:])
        finally:
            if stop_watch is not None:
                stop_watch.set()
            self.yielded_to = None
            self.current = None

    def progress(self, job_id: str):
        def cb(**k):
            phase = k.get("phase", "") or ""
            status = k.get("status", "") or k.get("info", "") or ""
            if k.get("frac") is not None:
                # our own stages (a re-voice: split, sing, mix): a fraction of the whole job and the words to show. The
                # YuE2 path below speaks in percents, which is why these used to land at 1% and read "Re-voicing" for
                # the whole job.
                job_update(job_id, progress=round(max(0.01, min(0.95, float(k["frac"]))), 3), phase=phase[:60], message=(status or friendly_phase(phase, status))[:120])
                return
            frac = _stage_fraction(phase, status, k.get("step"), k.get("total"), k.get("progress"))
            job_update(job_id, progress=round(frac, 3), phase=phase[:60], message=friendly_phase(phase, status)[:120])
        return cb

    # ---- song (create / cover / radio)
    def job_song(self, job: dict[str, Any]) -> dict[str, Any]:
        p = job["params"]
        cb = self.progress(job["id"])
        lyrics = (p.get("lyrics") or "").strip()
        style = (p.get("style") or "").strip()
        source_audio = p.get("source_audio")
        if p.get("instrumental") and not lyrics:
            lyrics = "[Intro]\n[Verse]\n[Chorus]\n[Verse]\n[Chorus]\n[Bridge]\n[Chorus]\n[Outro]"
        if source_audio and p.get("transcribe_lyrics") and not lyrics:
            job_update(job["id"], message="Listening to the source lyrics (Whisper)", progress=0.03)
            tr = engine.transcribe(source_audio)
            lyrics = "[Verse]\n" + "\n".join(tr["lines"]) if tr["lines"] else tr["text"]
        if not lyrics:
            lyrics = "[Verse]\nLa la la"
        if not style:
            style = STYLE_PRESETS[1]["style"]
        abc_path = None
        if p.get("abc_text") and not source_audio:
            abc_path = LIBRARY / "_tmp" / f"{job['id']}.abc"
            abc_path.write_text(p["abc_text"], encoding="utf-8")
        mode = int(p.get("mode", 0))
        if (abc_path or source_audio) and mode == 2:
            mode = 0                      # direct planning cannot follow a sheet
        loras_plan = {k: float(v) for k, v in (p.get("loras") or {}).items() if float(v) != 0}
        res = engine.generate_song(
            lyrics=lyrics, style=style, mode=mode, duration=int(p.get("duration", 120)), steps=int(p.get("steps", 32)), seed=int(p.get("seed", -1)),
            cfg=float(p.get("cfg", 1.0)), temperature=float(p.get("temperature", 1.0)), top_k=int(p.get("top_k", 100)), top_p=float(p.get("top_p", 0.95)),
            abc_path=str(abc_path) if abc_path else None, source_audio=source_audio, save_score=True, loras=loras_plan, on_progress=cb, cancel=self.cancel_event,
        )
        song_id = new_id("song_")
        d = song_dir(song_id)
        master = d / ("master" + Path(res["audio"]).suffix.lower())
        shutil.move(res["audio"], master)
        files = {"master": master.name}
        for key, src in (("abc", res.get("abc")), ("mid", res.get("mid"))):
            if src and Path(src).exists():
                dst = d / ("score" + Path(src).suffix.lower())
                shutil.move(src, dst)
                files[key] = dst.name
        abc_text = None
        try:
            abc_text = (res.get("plan") or {}).get("abc")
            if abc_text and "abc" not in files:
                (d / "score.abc").write_text(abc_text, encoding="utf-8")
                files["abc"] = "score.abc"
        except Exception:
            pass
        final = master
        voice_id = p.get("voice_id") if voices_on() else None
        if p.get("voice_id") and not voices_on():
            LOG.warning("job %s asked for a voice but voice features are off: singing it plain", job["id"])
        if voice_id:
            voice = db.get("voices", voice_id)
            if voice and (VOICES / voice["file"]).exists():
                job_update(job["id"], message=f"Re-voicing as {voice['name']}", progress=0.93, phase="voice")
                voiced_raw = d / "voiced_raw.wav"
                engine.voice_convert(master, VOICES / voice["file"], voiced_raw, on_progress=cb)
                voiced = d / "voiced.wav"
                audio_utils.normalize_file(voiced_raw, voiced)
                voiced_raw.unlink(missing_ok=True)
                files["voiced"] = voiced.name
                final = voiced
        fx_info = None
        st_fx = ((db.get("stations", p.get("station_id")) or {}).get("post_fx") or {}) if p.get("station_id") else {}
        if isinstance(st_fx, dict) and st_fx.get("binaural"):
            job_update(job["id"], message="Laying the sleep frequencies under it", progress=0.975, phase="mix")
            layered = d / "layered.wav"
            fx_info = sleeptones.layer_under(final, layered, st_fx)
            files["layered"] = layered.name
            final = layered
            LOG.info("sleep frequencies under %s: %s", song_id, fx_info)
        truncated = res.get("truncated") or {}
        clipped = bool(truncated.get("semantic"))  # YuE2 hit the duration cap before its own end-of-song token
        if clipped:
            LOG.warning("song hit the %ss cap before its ending (lyrics %d lines): fading the last 3 s", p.get("duration"), len(lyrics.splitlines()))
        job_update(job["id"], message="Encoding mp3" + (" (ending clipped by the cap, fading)" if clipped else ""), progress=0.985)
        mp3 = d / "song.mp3"
        audio_utils.to_mp3(final, mp3, fade_out=3.0 if clipped else 0.0)
        files["mp3"] = mp3.name
        duration = audio_utils.duration_seconds(final)
        title = (p.get("title") or "").strip() or first_lyric_line(lyrics) or f"Song {song_id[-4:]}"
        song = {
            "id": song_id, "title": title, "style": style, "lyrics": lyrics, "mode": int(p.get("mode", 0)), "duration_s": round(duration, 1), "seed": res.get("seed") if res.get("seed") is not None else p.get("seed"),
            "steps": int(p.get("steps", 32)), "cfg": float(p.get("cfg", 1.0)), "voice_id": voice_id, "loras": loras_plan, "station_id": p.get("station_id"), "source": p.get("source") or "studio",
            "parent_id": p.get("parent_id"), "files": files, "abc": abc_text, "tags": list(p.get("tags") or []) + (["clipped"] if clipped else []) + ([f"{fx_info['beat_hz']:g} Hz {fx_info['band']} on {fx_info['carrier_hz']} Hz"] if fx_info else []), "created": now(), "status": "ready",
            "topic": p.get("topic"), "theme": p.get("theme"), "singer": p.get("singer"), "lyrics_hash": p.get("lyrics_hash"),
            "cover_of": p.get("cover_of"),
        }
        db.insert("songs", song)
        maybe_retry_clipped(p, song)
        lyric_intel.remember(db, {**song, "_novelty": p.get("novelty")})
        (d / "meta.json").write_text(json.dumps({**song, "params": p, "lora_report": res.get("lora_report"), "truncated": truncated}, indent=2, default=str), encoding="utf-8")
        if p.get("lyrics_hash"):
            db.insert("lyrics_used", {"hash": p["lyrics_hash"], "station_id": p.get("station_id"), "path": p.get("lyrics_path"), "song_id": song_id, "used_at": now()})
        return {"song_id": song_id, "message": f"Ready: {title} ({duration:.0f}s)"}

    # ---- re-voice an existing song
    def job_revoice(self, job: dict[str, Any]) -> dict[str, Any]:
        if not voices_on():
            raise RuntimeError("voice features are switched off")
        p = job["params"]
        cb = self.progress(job["id"])
        parent = db.get("songs", p["song_id"])
        voice = db.get("voices", p["voice_id"])
        if not parent or not voice:
            raise RuntimeError("song or voice not found")
        pd = LIBRARY / parent["id"]
        files = parent.get("files") or {}
        src = pd / files.get("master", files.get("mp3"))
        song_id = new_id("song_")
        d = song_dir(song_id)
        trained = bool(voice.get("model")) and Path(voice["model"]).exists()
        job_update(job["id"], message=f"Re-voicing as {voice['name']}" + (" (trained voice)" if trained else ""), progress=0.2, phase="voice")
        voiced_raw = d / "voiced_raw.wav"
        if voice_ft is not None:
            voice_ft.LAST_FIT.clear()   # so a Wan2GP-path conversion never reads the previous song's numbers
        try:
            blend = float(p.get("blend") if p.get("blend") is not None else 1.0)
            engine.voice_convert(src, VOICES / voice["file"], voiced_raw, on_progress=cb, voice=voice, semi_tone_shift=int(p.get("semi_tone_shift") or 0), blend=blend,
                                 key_safe=bool(p.get("key_safe", True)), ai_samples=ai_samples())
        except Exception:
            shutil.rmtree(d, ignore_errors=True)  # no half-made song folder left in the library
            raise
        fit = dict(voice_ft.LAST_FIT) if voice_ft is not None else {}
        voiced = d / "voiced.wav"
        audio_utils.normalize_file(voiced_raw, voiced)
        voiced_raw.unlink(missing_ok=True)
        mp3 = d / "song.mp3"
        audio_utils.to_mp3(voiced, mp3)
        new_files = {"voiced": voiced.name, "mp3": mp3.name}
        if (d / "vocals.wav").exists():
            new_files["vocals"] = "vocals.wav"   # the new voice alone (download it from the Library to hear it dry)
        for k in ("abc", "mid"):
            if files.get(k) and (pd / files[k]).exists():
                shutil.copy2(pd / files[k], d / files[k])
                new_files[k] = files[k]
        song = {**{k: parent.get(k) for k in ("style", "lyrics", "mode", "seed", "steps", "cfg", "loras", "station_id", "abc")}, "tags": list(parent.get("tags") or []) + _voice_tags(trained, blend, fit) + _fit_tags(fit),
                "id": song_id, "title": p.get("title") or revoiced_title(parent.get("title")), "duration_s": round(audio_utils.duration_seconds(voiced), 1), "voice_id": voice["id"], "source": "revoice", "parent_id": parent["id"], "files": new_files, "created": now(), "status": "ready"}
        db.insert("songs", song)
        (d / "meta.json").write_text(json.dumps(song, indent=2, default=str), encoding="utf-8")
        return {"song_id": song_id, "message": f"Ready: {song['title']} ({_sang_as(voice, blend, fit)})" + _fit_note(fit)}

    # ---- voice sample preparation
    def job_voice_prep(self, job: dict[str, Any]) -> dict[str, Any]:
        if not voices_on():
            raise RuntimeError("voice features are switched off")
        p = job["params"]
        voice = db.get("voices", p["voice_id"])
        if not voice:
            raise RuntimeError("voice not found")
        src = Path(p["src"])
        work = src
        if p.get("remove_music"):
            job_update(job["id"], message="Removing the music from the sample", progress=0.2, phase="separate")
            vocals, _inst = engine.separate(src, LIBRARY / "_tmp", prefix=f"voice_{voice['id']}")
            work = Path(vocals)
        job_update(job["id"], message="Trimming the sample", progress=0.8, phase="trim")
        out = VOICES / f"{voice['id']}.wav"
        info = audio_utils.trim_voice_sample(work, out, max_seconds=float(p.get("max_seconds", 30)))
        db.update("voices", voice["id"], file=out.name, duration_s=info["seconds"], notes=json.dumps(info))
        for tmp in (LIBRARY / "_tmp").glob(f"voice_{voice['id']}*"):
            tmp.unlink(missing_ok=True)
        return {"message": f"Voice '{voice['name']}' ready ({info['seconds']}s)"}

    def job_voice_train(self, job: dict[str, Any]) -> dict[str, Any]:
        """Fine-tune Seed-VC's singing model on a voice (its sample + voices/train/<id>/ recordings). Runs in a subprocess
        with the card to itself: YuE2 and the lyric model are unloaded first and the engine stays 'busy' throughout."""
        if not voices_on():
            raise RuntimeError("voice features are switched off")
        p = job["params"]
        voice = db.get("voices", p["voice_id"])
        if not voice or not voice.get("file") or not (VOICES / voice["file"]).exists():
            raise RuntimeError("voice not found or its sample is not ready")
        todo = voice_ft.unseparated_files(voice["id"]) if p.get("remove_music", True) else []
        if todo:
            tmp = LIBRARY / "_tmp"
            for i, f in enumerate(todo):
                if self.cancel_event.is_set():
                    raise engine_mod.Cancelled()
                job_update(job["id"], message=f"Pulling the vocal out of {f['name'][14:]} ({i + 1}/{len(todo)})", progress=0.02 + 0.06 * i / len(todo), phase="separate")
                src = voice_ft.TRAIN_DIR / voice["id"] / f["name"]
                try:
                    vocals, _inst = engine.separate(src, tmp, prefix=f"vt_{i}")
                    clean = src.with_suffix(".clean.wav")
                    audio_utils.to_wav(vocals, clean, sr=voice_ft.SR, mono=True)
                    clean.replace(src)
                    voice_ft.mark_separated(voice["id"], f["name"], audio_utils.duration_seconds(src))
                except Exception as e:
                    LOG.warning("voice train: could not separate %s (%s); using it as recorded", f["name"], e)
                    voice_ft.mark_separated(voice["id"], f["name"])
                for t in tmp.glob(f"vt_{i}*"):
                    t.unlink(missing_ok=True)
        job_update(job["id"], message="Clearing the GPU for training", progress=0.08, phase="prepare")
        engine.unload()
        try:
            llm.unload_model()
        except Exception:
            pass

        def prog(frac, msg):
            fields = {"message": str(msg)[:120], "phase": "train"}
            if frac is not None:
                fields["progress"] = round(float(frac), 3)
            job_update(job["id"], **fields)
            if self.cancel_event.is_set():
                raise engine_mod.Cancelled()

        with engine.hold("voice training"):
            with engine_mod.gpu_lease("tenforward"):
                meta = voice_ft.train(voice["id"], VOICES / voice["file"], steps=p.get("steps"), progress=prog)
        db.update("voices", voice["id"], model=str(voice_ft.model_path(voice["id"])), trained=now(), train_meta=json.dumps(meta))
        pitch = meta.get("pitch") or {}
        rng = f", range {pitch['low_note']}–{pitch['high_note']} ({pitch['gender']})" if pitch.get("low_note") else ""
        return {"message": f"{voice['name']} trained on {meta['seconds']}s of audio ({meta['clips']} clips, {meta['steps']} steps) in {meta['minutes']} min{rng}"}

    def job_transcribe(self, job: dict[str, Any]) -> dict[str, Any]:
        p = job["params"]
        job_update(job["id"], message="Listening (Whisper)", progress=0.3, phase="transcribe")
        tr = engine.transcribe(p["src"], language=p.get("language"))
        lyrics = "[Verse]\n" + "\n".join(tr["lines"]) if tr["lines"] else tr["text"]
        db.update("jobs", job["id"], params={**p, "result": {"lyrics": lyrics, "language": tr.get("language"), "segments": tr.get("segments")}})
        return {"message": "Lyrics transcribed"}

    def job_arrange(self, job: dict[str, Any]) -> dict[str, Any]:
        """Read a recording into a lead sheet once (SheetSage2), hear its words (Whisper) and put them under the
        sheet's sections; saved as an arrangement so every later song on that tune skips all of this. A sheet file
        (MIDI, MusicXML, a printed page) takes the converter path instead."""
        p = job["params"]
        cb = self.progress(job["id"])
        src = p["src"]
        name = (p.get("name") or Path(src).stem)[:120]
        if p.get("kind") == "sheet":
            job_update(job["id"], message="Reading the sheet file", progress=0.2, phase="score")
            conv = sheet.from_file(src)
            arr = save_arrangement(name, p.get("source") or "file", conv["abc"], words=conv.get("words") or "", source_hash=p.get("source_hash"),
                                   extra={k: conv.get(k) for k in ("part", "parts", "melody_track", "tracks", "omr") if conv.get(k) is not None})
        else:
            job_update(job["id"], message="Reading the sheet (SheetSage2)", progress=0.05, phase="score")
            res = engine.score(src, melody_only=False, on_progress=cb, cancel=self.cancel_event)
            sh = sheet.parse(res["abc"])
            words = ""
            if not p.get("instrumental"):
                job_update(job["id"], message="Listening to the words (Whisper)", progress=0.7, phase="transcribe")
                tr = engine.transcribe(src, language=p.get("language"))
                if tr.get("segments"):
                    words = sheet.align_words(tr["segments"], sh)
            arr = save_arrangement(name, p.get("source") or "recording", res["abc"], words=words, source_hash=p.get("source_hash"),
                                   song_id=p.get("song_id"), extra={"structure": res.get("structure"), "warnings": res.get("warnings")})
        db.update("jobs", job["id"], params={**p, "result": {"arrangement": arr["id"]}})
        info = arr.get("info") or {}
        return {"message": f"Sheet read: {arr['name']} ({float(arr.get('duration_s') or 0):.0f}s, {len(info.get('sections') or [])} sections)"}

    def job_stems(self, job: dict[str, Any]) -> dict[str, Any]:
        p = job["params"]
        song = db.get("songs", p["song_id"])
        if not song:
            raise RuntimeError("song not found")
        d = LIBRARY / song["id"]
        files = song.get("files") or {}
        src = d / files.get("voiced", files.get("master", files.get("mp3")))
        job_update(job["id"], message="Splitting stems", progress=0.3, phase="separate")
        vocals, inst = engine.separate(src, d, prefix="stem")
        files.update({"vocals": Path(vocals).name, "instrumental": Path(inst).name})
        db.update("songs", song["id"], files=files)
        return {"song_id": song["id"], "message": "Stems ready"}


class Scheduler(threading.Thread):
    """Ticks every 20 s whatever the worker is doing: keeps every station stocked (radio_keep_ahead plans in its own
    side thread, LLM only), and runs the hourly retention cleanup. Before this, planning only happened while the
    worker sat idle, so the line could never hold more than one song."""

    def __init__(self, worker: "Worker"):
        super().__init__(daemon=True, name="tf-scheduler")
        self.worker = worker
        self._last_cleanup = 0.0

    def run(self):
        time.sleep(5)
        while True:
            try:
                if now() - self._last_cleanup > 3600:
                    self._last_cleanup = now()
                    cleanup_expired()
            except Exception:
                LOG.exception("cleanup failed")
            try:
                self.worker.radio_keep_ahead()
            except Exception:
                LOG.exception("radio keep-ahead failed")
            time.sleep(20)


worker = Worker()
scheduler = Scheduler(worker)


# --------------------------------------------------------------------------- API: status + presets
@app.get("/api/status")
def api_status(request: Request):
    user = auth.current(request)
    if user is None:  # signed out: just enough for the login sheet to draw itself
        return {"version": VERSION, "api_version": API_VERSION, "power": power_on(), "features": features(),
                "ui_build": ui_build(), "needs_login": True, "urls": urls(), "time": now()}
    cur = worker.current
    return {
        "version": VERSION,
        "engine": {"loaded": engine.is_loaded(), "busy": engine.busy, "vram": engine.vram(), "mode": engine.engine_mode, "comfy_busy": engine.comfy_busy()},
        "worker": {"current": db.get("jobs", cur["id"]) if cur else None, "queued": db.count("jobs", "status='queued'")},
        "counts": {"songs": db.count("songs", "status='ready'"), "liked": db.count("songs", "status='ready' AND liked=1"), "voices": db.count("voices") if voices_on() else 0, "stations": db.count("stations", "enabled=1")},
        "songs_dir": str(songs_dir()), "retention_days": float(cfg("retention_days")), "banned_retention_h": float(cfg("banned_retention_h")),
        "radio_autofill": db.setting("radio_autofill", True),
        "buffer": buffer_brief(),
        "power": power_on(),
        "features": features(), "user": auth.public_user(user), "needs_login": False, "header_tagline": str(cfg("header_tagline")),
        "api_version": API_VERSION, "urls": urls(),
        "building": radio.priming(db),
        "import": importer.progress(),
        "ui_build": ui_build(),
        "queue_paused": queue_paused(),
        "gpu_used_mb": gpu_used_mb(),
        "llm_loaded": llm.loaded_models(),
        "time": now(),
    }


_BUFFER_CACHE: dict[str, Any] = {"t": 0.0, "v": None}


def buffer_brief() -> dict[str, Any]:
    """The short version for /api/status: hours on hand, what is coming, and the thinnest channel.
    Cached for 20 s because status is polled by three different screens."""
    if now() - float(_BUFFER_CACHE["t"]) < 20 and _BUFFER_CACHE["v"] is not None:
        return _BUFFER_CACHE["v"]
    try:
        rep = radio.buffer_report(db)
        brief = {"hours": rep["hours"], "minutes": rep["minutes"], "coming_minutes": rep["coming_minutes"],
                 "thinnest": rep.get("thinnest"), "parked": len(parked_stations())}
    except Exception:
        LOG.exception("buffer report failed")
        brief = {}
    _BUFFER_CACHE.update(t=now(), v=brief)
    return brief


@app.get("/api/buffer")
def api_buffer():
    """How much fresh audio the radio is holding, per channel and in total.

    The one number worth watching: hours on hand. The card renders about 1.6x faster than a song
    plays, so a healthy radio banks audio while nobody is listening and spends it while somebody
    is. Nothing recorded this before, which is why "the radio ran dry" left no trace."""
    rep = radio.buffer_report(db)
    rep["parked"] = parked_stations()
    return rep


@app.get("/api/hello")
def api_hello():
    """Cheap discovery endpoint for the Android app: who am I, which API, where else am I reachable, do I want a login.
    Public on purpose: it is how the phone decides between its LAN address and its off-network address."""
    feats = ["radio", "queue", "import", "history", "power", "auth"]
    if voices_on():
        feats.append("voices")
    return {"app": "Ten Forward", "version": VERSION, "api_version": API_VERSION, "urls": urls(), "time": now(),
            "features": feats, "login_required": auth.login_required(db)}


def import_station(station_id: str | None) -> str:
    """Songs are imported onto a real channel. Favorites is a view of hearted songs, so nothing can be imported
    into it -- it would land there and never be seen."""
    sid = (station_id or "my-songs").strip() or "my-songs"
    if radio.is_favorites(db.get("stations", sid)):
        raise HTTPException(400, "Favorites is not a place songs live. Import onto a channel and heart it.")
    return sid


# --------------------------------------------------------------------------- API: imports (your own recordings + lyric notes)
async def _import_upload(file: UploadFile, station_id: str, title: str = "", album: str = "") -> dict[str, Any]:
    """An uploaded recording -> a ready library song (never auto-deleted); the same file twice is the song already there."""
    ext = Path(file.filename or "song").suffix.lower()
    if ext not in importer.AUDIO_EXT:
        raise HTTPException(400, f"not an audio file: {ext or file.filename}")
    tmp = UPLOADS / f"{new_id('imp_')}{ext}"
    with tmp.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    try:
        song = importer.import_audio(db, LIBRARY, tmp, station_id=import_station(station_id), title=(title or Path(file.filename or "").stem or None), album=album or None)
    finally:
        tmp.unlink(missing_ok=True)
    if song.get("_duplicate"):
        # already in the library: say so, and leave the original's own origin_path alone
        return {**public_song(song), "duplicate": True}
    song["origin_path"] = file.filename  # the temp path means nothing to anyone
    db.update("songs", song["id"], origin_path=file.filename or "upload")
    return public_song(song)


@app.post("/api/import/upload")
async def api_import_upload(file: UploadFile = File(...), station_id: str = Form("my-songs"), title: str = Form(""), album: str = Form("")):
    """One finished song from the phone or desktop -> a ready library song (never auto-deleted)."""
    return await _import_upload(file, station_id, title, album)


@app.post("/api/swap", dependencies=[Depends(need_voices)])
async def api_swap(file: UploadFile = File(...), voice_id: str = Form(...), blend: str = Form("1"), semi_tone_shift: str = Form("0"), station_id: str = Form("my-songs"),
                   pitch: str = Form("auto")):
    """Swap the singer, the easy way: a real recording in (kept as a library song on `station_id`, My Songs unless said)
    and a re-voice queued at once. The music stays; the vocal is lifted out, sung by the voice (moved by whole octaves
    into its range, so the key stays with the music) and mixed back in. The new take lands next to the song."""
    if not db.get("voices", voice_id or ""):
        raise HTTPException(404, "voice not found")
    try:
        shift = max(-12, min(12, int(float(semi_tone_shift or 0))))
        bl = max(0.0, min(1.0, float(blend if blend not in (None, "") else 1)))   # 0 = the AI singer only, from the sample
    except ValueError:
        raise HTTPException(400, "blend and semi_tone_shift are numbers")
    # pitch: "auto" = whole octaves into the voice's range (the key stays); "song" = as sung, no move; "up" / "down" = one octave
    pitch = (pitch or "auto").strip().lower()
    key_safe = pitch == "auto"
    if pitch == "up":
        shift = 12
    elif pitch == "down":
        shift = -12
    song = await _import_upload(file, station_id)
    job = enqueue("revoice", {"song_id": song["id"], "voice_id": voice_id, "semi_tone_shift": shift, "blend": bl, "key_safe": key_safe, "easy": True,
                              "title": revoiced_title(song.get("title"))}, priority=8)
    return {"song": song, "job": job}


@app.post("/api/import/folder", dependencies=[Depends(auth.admin_only)])
def api_import_folder(body: dict[str, Any]):
    """Import every audio file under a folder this computer can see (a drive or a network share). Runs in the background; poll /api/import/status."""
    root = Path(str(body.get("path") or "").strip().strip('"'))
    if not root.exists():
        raise HTTPException(400, f"this computer cannot see {root}")
    try:
        return importer.import_folder_async(db, LIBRARY, root, station_id=import_station(body.get("station_id")), recursive=bool(body.get("recursive", True)))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.get("/api/import/status")
def api_import_status():
    return importer.progress()


@app.post("/api/import/keep", dependencies=[Depends(auth.admin_only)])
async def api_import_keep(file: UploadFile = File(...), folder: str = Form("my-songs")):
    """A Google Keep export (one text file, songs separated by blank lines) -> one lyric file per song."""
    tmp = UPLOADS / f"{new_id('keep_')}.txt"
    tmp.write_bytes(await file.read())
    try:
        return importer.import_keep_export(db, tmp, folder=re.sub(r"[^A-Za-z0-9 _-]+", "", folder).strip() or "my-songs")
    finally:
        tmp.unlink(missing_ok=True)


@app.get("/api/history")
def api_history(limit: int = 30, station: str | None = None):
    """What played most recently (the player marks a song played after 10 s)."""
    where, params = "status='ready' AND plays>0", []
    if station and station != "all":
        where += " AND station_id=?"
        params.append(station)
    return [public_song(x) for x in db.query("songs", where, params, order="last_played DESC", limit=max(1, min(int(limit), 100)))]


@app.get("/api/defaults")
def api_defaults():
    return {"style_presets": STYLE_PRESETS, "modes": [{"value": 0, "label": "Melody and chords"}, {"value": 1, "label": "Melody only"}, {"value": 2, "label": "Direct"}], "duration_default": 150, "steps_default": 32}


@app.get("/api/llm/health")
def api_llm_health():
    out = _llm_health_inner()
    out["craft"] = llm.craft_summary()
    return out


def _llm_health_inner():
    return llm.health()


# --------------------------------------------------------------------------- API: uploads
@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "upload").suffix.lower() or ".bin"
    if ext not in (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".webm", ".aac", ".opus", ".mp4", ".mov", ".abc", ".txt", ".md") + sheet.SHEET_EXTS:
        raise HTTPException(400, f"unsupported file type {ext}")
    dest = UPLOADS / f"{new_id('up_')}{ext}"
    with dest.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    info = {"path": str(dest), "name": file.filename, "bytes": dest.stat().st_size}
    if ext in sheet.SHEET_EXTS and ext != ".abc":
        info["kind"] = "sheet"      # a MIDI, MusicXML or printed page: read by /api/arrangements, never by ffmpeg
    elif ext not in (".abc", ".txt", ".md"):
        # normalise anything the browser recorded (webm/m4a) to wav so every downstream tool is happy
        wav = dest.with_name(dest.stem + "_48k.wav")
        try:
            audio_utils.to_wav(dest, wav, sr=48000)
            dest.unlink(missing_ok=True)
            info["path"] = str(wav)
        except Exception as e:
            LOG.warning("ffmpeg convert failed for %s: %s", dest, e)
        info["duration"] = audio_utils.duration_seconds(info["path"])
    else:
        info["text"] = dest.read_text(encoding="utf-8-sig", errors="replace")
    return info


# --------------------------------------------------------------------------- API: songs + jobs
@app.post("/api/songs")
def api_create_song(body: dict[str, Any]):
    lyrics = (body.get("lyrics") or "").strip()
    style = (body.get("style") or "").strip()
    if not style and not lyrics and not body.get("source_audio"):
        raise HTTPException(400, "give me at least a style or some lyrics")
    count = max(1, min(int(body.get("count", 1)), 8))
    if body.get("write_lyrics") and not lyrics and not body.get("instrumental"):
        lyrics = llm.write_lyrics(body.get("topic") or style or "a song", mood=body.get("mood", ""), style=style)
        body["lyrics"] = lyrics
    if not style:
        style = llm.suggest_style(body.get("topic") or "", lyrics) if lyrics else STYLE_PRESETS[1]["style"]
        body["style"] = style
    if body.get("source_song_id") and not body.get("source_audio"):
        src_song = db.get("songs", body["source_song_id"])
        if src_song and (src_song.get("abc") or "").strip() and not body.get("abc_text"):
            # the sheet this song was sung from is the sheet; no need to read the audio again (1.6)
            body["abc_text"] = src_song["abc"]
            body["parent_id"] = src_song["id"]
            if int(body.get("mode") or 0) == 2:
                body["mode"] = 0
        elif src_song:
            sf = src_song.get("files") or {}
            src_path = LIBRARY / src_song["id"] / sf.get("voiced", sf.get("master", sf.get("mp3", "")))
            if src_path.exists():
                body["source_audio"] = str(src_path)
                body["parent_id"] = src_song["id"]
    # A cover is as long as the original: the cap follows the source (the library song, its sheet, or the audio) with
    # the radio's headroom, never below what was asked. A 150 s cap had been chopping 3 minute covers (1.6.1).
    src_len = 0.0
    if body.get("source_song_id"):
        src_len = float((db.get("songs", body["source_song_id"]) or {}).get("duration_s") or 0)
    if not src_len and body.get("abc_text"):
        try:
            src_len = float(sheet.parse(body["abc_text"]).get("duration_s") or 0)   # Sheet is a dict
        except Exception:
            src_len = 0.0
    if not src_len and body.get("source_audio") and Path(str(body["source_audio"])).exists():
        try:
            src_len = float(audio_utils.duration_seconds(body["source_audio"]))
        except Exception:
            src_len = 0.0
    if src_len:
        body["duration"] = int(max(int(body.get("duration") or 0), min(radio.ENGINE_MAX_S, src_len * radio.HEADROOM + 10)))
        body["target_duration"] = int(round(src_len))
    params = {k: body.get(k) for k in ("title", "lyrics", "style", "mode", "duration", "target_duration", "steps", "seed", "cfg", "temperature", "top_k", "top_p", "abc_text", "source_audio", "transcribe_lyrics", "voice_id", "loras", "instrumental", "tags", "parent_id") if body.get(k) is not None}
    params.setdefault("mode", 0)
    params.setdefault("duration", 240)   # a cap, not a target; 150 chopped 3 minute songs
    params.setdefault("steps", 32)
    params["source"] = body.get("source") or "studio"
    if params.get("instrumental") and "loras" not in body:
        # the instrumental planner LoRA only when the caller said nothing about LoRAs: it forces the slow legacy engine
        params["loras"] = {"ar_lora_inst_v3abc.bf16.safetensors": 1.0} if (loras_mod.LORA_DIR / "ar_lora_inst_v3abc.bf16.safetensors").exists() else {}
    jobs = []
    for i in range(count):
        p = dict(params)
        if count > 1:
            p["seed"] = -1 if int(p.get("seed", -1)) < 0 else int(p["seed"]) + i
            p["title"] = (p.get("title") or "") and f"{p['title']} {i + 1}"
        jobs.append(enqueue("song", p, priority=10))
    return {"jobs": jobs, "lyrics": lyrics, "style": style}


@app.get("/api/jobs")
def api_jobs(status: str | None = None, limit: int = 40):
    where, params = "1=1", []
    if status:
        where = "status IN (%s)" % ",".join("?" for _ in status.split(","))
        params = status.split(",")
    return db.query("jobs", where, params, order="CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, created DESC", limit=limit)


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    j = db.get("jobs", job_id)
    if not j:
        raise HTTPException(404)
    return j


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    j = db.get("jobs", job_id)
    if not j:
        raise HTTPException(404)
    if j["status"] == "queued":
        db.update("jobs", job_id, status="cancelled", finished=now(), message="Cancelled")
    elif j["status"] == "running" and worker.current and worker.current["id"] == job_id:
        worker.cancel_event.set()
        try:
            if engine.current_job is not None:
                engine.current_job.cancel()
        except Exception:
            pass
    return db.get("jobs", job_id)


@app.delete("/api/jobs/{job_id}")
def api_job_delete(job_id: str):
    j = db.get("jobs", job_id)
    if j and j["status"] in ("done", "failed", "cancelled"):
        db.delete("jobs", job_id)
    return {"ok": True}


@app.post("/api/jobs/clear")
def api_jobs_clear():
    for j in db.query("jobs", "status IN ('done','failed','cancelled')"):
        db.delete("jobs", j["id"])
    return {"ok": True}


def _job_view(j: dict[str, Any]) -> dict[str, Any]:
    p = j.get("params") or {}
    st = db.get("stations", p["station_id"]) if p.get("station_id") else None
    return {"id": j["id"], "type": j["type"], "status": j["status"], "progress": j.get("progress") or 0, "phase": j.get("phase"), "message": j.get("message"),
            "created": j.get("created"), "started": j.get("started"), "finished": j.get("finished"), "priority": j.get("priority") or 0, "song_id": j.get("song_id"),
            "title": p.get("title") or _first_line(p.get("lyrics")) or (j["type"] == "revoice" and p.get("song_id") and revoiced_title((db.get("songs", p["song_id"]) or {}).get("title"))) or j["type"],
            "station_id": p.get("station_id"), "station": st["name"] if st else None, "color": st.get("color") if st else None,
            "requested_by": p.get("requested_by") or ("auto" if p.get("source") == "radio" else "studio"), "instrumental": bool(p.get("instrumental")),
            "style": (p.get("style") or "")[:120], "voice_id": p.get("voice_id"), "duration": p.get("duration"), "error": (j.get("error") or "")[-300:] if j.get("status") == "failed" else None}


@app.get("/api/queue")
def api_queue():
    cur = worker.current
    running = db.get("jobs", cur["id"]) if cur else None
    queued = db.query("jobs", "status='queued'", order="priority DESC, created ASC")
    recent = db.query("jobs", "status IN ('done','failed','cancelled')", order="COALESCE(finished, created) DESC", limit=12)
    return {"running": _job_view(running) if running else None, "queued": [_job_view(j) for j in queued], "recent": [_job_view(j) for j in recent],
            "queue_paused": queue_paused(), "radio_autofill": db.setting("radio_autofill", True), "power": power_on(),
            "engine": {"loaded": engine.is_loaded(), "busy": engine.busy}, "time": now()}


@app.post("/api/jobs/{job_id}/bump")
def api_job_bump(job_id: str):
    """Move a queued job to the front of the line."""
    j = db.get("jobs", job_id)
    if not j or j["status"] != "queued":
        raise HTTPException(409, "only a queued job can be moved")
    top = db.query("jobs", "status='queued'", order="priority DESC", limit=1)
    return _job_view(db.update("jobs", job_id, priority=int((top[0].get("priority") or 0) if top else 0) + 1, created=now() - 1))


@app.post("/api/jobs/{job_id}/later")
def api_job_later(job_id: str):
    """Send a queued job to the back of the line."""
    j = db.get("jobs", job_id)
    if not j or j["status"] != "queued":
        raise HTTPException(409, "only a queued job can be moved")
    return _job_view(db.update("jobs", job_id, priority=-1, created=now()))


CLIENT_LOG_FIELDS = ("event", "song_id", "title", "t", "duration", "ua", "detail", "mode", "station",
                     "level", "tag", "message", "source", "device", "app_version")


@app.post("/api/client/log")
def api_client_log(body: dict[str, Any], request: Request):
    """What the phones and browsers report: playback anomalies (ended early, stalled, skips) and, from the
    app's Problems screen, whatever went wrong there. Kept for two days, shown in Settings."""
    entry = {k: body.get(k) for k in CLIENT_LOG_FIELDS if k in body}
    who = getattr(request.state, "user", None)
    if who:
        entry["who"] = who.get("name")
    client_log(entry)
    return {"ok": True}


@app.get("/api/client/log", dependencies=[Depends(auth.admin_only)])
def api_client_log_read(limit: int = 200):
    """The Problems block in Settings: what the phones and browsers have reported lately."""
    return {"entries": client_log_entries(max(1, min(int(limit or 200), 1000))), "keep_days": LOG_KEEP_DAYS}


@app.get("/api/songs")
def api_songs(station: str | None = None, q: str | None = None, liked: int | None = None, limit: int = 200,
              brief: int = 0, order: str = "new"):
    """The songs you can pick from. brief=1 leaves the lyrics out - a phone asking for a whole
    channel does not want 300 sets of lyrics down a mobile connection to draw a list."""
    where, params = "status='ready' AND COALESCE(banned,0)=0", []
    if station and station != "all":
        extra, ps = radio.station_where(db, station)
        if extra:
            where += " AND " + extra
            params.extend(ps)
    if liked:
        where += " AND liked=1"
    if q:
        where += " AND (title LIKE ? OR lyrics LIKE ? OR style LIKE ?)"
        params += [f"%{q}%"] * 3
    by = {"new": "created DESC", "old": "created ASC", "title": "title COLLATE NOCASE ASC",
          "played": "COALESCE(last_played,0) DESC", "unplayed": "plays ASC, created DESC"}.get(order, "created DESC")
    out = [public_song(s) for s in db.query("songs", where, params, order=by, limit=limit)]
    if brief:
        keep = {"id", "title", "style", "station_id", "liked", "duration_s", "plays", "last_played",
                "created", "urls", "cover_of", "tags", "source", "voice_id", "expires_at", "album"}
        out = [{k: v for k, v in s.items() if k in keep} for s in out]
    return out


@app.get("/api/songs/{song_id}")
def api_song(song_id: str):
    s = db.get("songs", song_id)
    if not s:
        raise HTTPException(404)
    return public_song(s)


@app.delete("/api/songs/{song_id}", dependencies=[Depends(auth.admin_only)])
def api_song_delete(song_id: str):
    s = db.get("songs", song_id)
    if s:
        unexport_song(s)
        db.delete("songs", song_id)
        shutil.rmtree(LIBRARY / song_id, ignore_errors=True)
    return {"ok": True}


@app.post("/api/songs/{song_id}/like")
def api_song_like(song_id: str):
    s = db.get("songs", song_id)
    if not s:
        raise HTTPException(404)
    if s.get("liked"):
        unexport_song(s)
        return public_song(db.update("songs", song_id, liked=0, saved_path=None, liked_at=None))
    saved = export_song(s)
    return public_song(db.update("songs", song_id, liked=1, liked_at=now(), saved_path=str(saved) if saved else None))


EARLY_SKIP_S = 20.0    # past this (or a fifth of the song, whichever is longer) a skip is not a verdict


@app.post("/api/songs/{song_id}/skip")
def api_song_skip(song_id: str, body: dict[str, Any] | None = None):
    """A skip on the radio is a vote. Enough early ones and the song retires itself.

    Only the radio counts: clicking through the library is browsing, not an opinion. A hearted song is never
    retired however often it is skipped, because keeping it was deliberate and this is not."""
    song = db.get("songs", song_id)
    if not song:
        raise HTTPException(404)
    b = body or {}
    at = float(b.get("at") or 0)
    dur = float(b.get("duration") or song.get("duration_s") or 0)
    limit = int(cfg("skip_retire"))
    if str(b.get("mode") or "") != "radio" or at > max(EARLY_SKIP_S, dur * 0.2):
        return {"skips": int(song.get("skips") or 0), "early": False, "retired": False, "limit": limit}
    n = int(song.get("skips") or 0) + 1
    retire = bool(limit) and n >= limit and not song.get("liked") and not song.get("banned")
    db.update("songs", song_id, skips=n, last_skip=now(), **({"banned": 1} if retire else {}))
    if retire:
        LOG.info("skips: retiring '%s' after %d early skips on the radio", song.get("title"), n)
    return {"skips": n, "early": True, "retired": retire, "limit": limit}


@app.post("/api/songs/{song_id}/ban")
def api_song_ban(song_id: str):
    song = db.get("songs", song_id)
    if not song:
        raise HTTPException(404)
    return public_song(db.update("songs", song_id, banned=0 if song.get("banned") else 1))


@app.post("/api/songs/{song_id}/more")
def api_song_more(song_id: str, body: dict[str, Any] | None = None):
    """More like this: another song on the same station with the same theme, singer and style, ahead of the auto stock."""
    s = db.get("songs", song_id)
    if not s:
        raise HTTPException(404)
    st = db.get("stations", s.get("station_id") or "")
    if not st:
        raise HTTPException(409, "this song is not on a station; use Cover this in the Library instead")
    if not power_on():
        raise HTTPException(409, "power is off")
    if not st.get("instrumental") and not llm.quick_ok():
        raise HTTPException(503, "the lyric writer is not answering")
    params = radio.plan_song_for_station(db, st, force_theme=s.get("theme"), force_singer=s.get("singer"), force_style=(body or {}).get("style") or s.get("style"))
    params["requested_by"] = "you"
    params["parent_id"] = s["id"]
    params["topic"] = (params.get("topic") or "") + f" (more like '{s.get('title')}')"
    job = enqueue("song", params, priority=5)
    return {"job": _job_view(job), "title": params.get("title")}


@app.post("/api/songs/{song_id}/played")
def api_song_played(song_id: str, body: dict[str, Any] | None = None):
    """A song counted as heard. `station` is the channel it was heard ON, which is not always the channel the
    song lives on: a song played from Favorites should count for Favorites. Older clients send no body."""
    db.mark_played(song_id)
    tuned = (body or {}).get("station")
    if not tuned or tuned == "all":
        tuned = (db.get("songs", song_id) or {}).get("station_id")
    db.note_play(tuned)
    return {"ok": True}


@app.post("/api/songs/{song_id}/title")
def api_song_title(song_id: str, body: dict[str, Any]):
    _s = db.get("songs", song_id)
    if not _s:
        raise HTTPException(404)
    if _s and _s.get("liked"):
        unexport_song(_s)
        _s["title"] = (body.get("title") or _s.get("title") or "").strip()
        saved = export_song(_s)
        db.update("songs", song_id, saved_path=str(saved) if saved else None)
    return public_song(db.update("songs", song_id, title=(body.get("title") or "Untitled")[:80]))


def revoiced_title(title: str | None) -> str:
    """'<the song> - revoiced': the sound file's own name with the word on the end (Will), never twice over."""
    base = re.sub(r"\s*[-·]\s*revoiced\s*$", "", (title or "Untitled").strip(), flags=re.I).strip() or "Untitled"
    return f"{base} - revoiced"


AI_SINGER_GENDERS = ("male", "female")


def ai_samples() -> dict[str, str]:
    """The stock AI singers on file, by gender: YuE2's own voice lifted from its songs (POST /api/voices/stock). The
    reference a 0 % swap sings from: nothing of the recorded voice reaches the model."""
    out: dict[str, str] = {}
    for v in db.query("voices", "source='stock'", order="created DESC"):
        try:
            notes = json.loads(v.get("notes") or "{}") if isinstance(v.get("notes"), str) else (v.get("notes") or {})
        except Exception:
            notes = {}
        g = (notes or {}).get("gender") or ""
        if g in AI_SINGER_GENDERS and v.get("file") and (VOICES / v["file"]).exists() and g not in out:
            out[g] = str(VOICES / v["file"])
    return out


def _stock_song_for(gender: str) -> dict[str, Any] | None:
    """A YuE2 song of the radio's own whose singer is of this gender, by its style line: the most loved first."""
    rows = db.query("songs", "status='ready' AND COALESCE(source,'')='radio'", order="liked DESC, plays DESC, created DESC", limit=400)
    for s in rows:
        if s.get("instrumental") or not (s.get("lyrics") or "").strip():
            continue   # an instrumental has no singer to lift
        style = (s.get("style") or "").lower()
        ok = ("female" in style) if gender == "female" else ("male" in style and "female" not in style)
        files = s.get("files") or {}
        if ok and (LIBRARY / s["id"] / (files.get("master") or files.get("mp3") or "x")).exists():
            return s
    return None


@app.post("/api/voices/stock", dependencies=[Depends(need_voices), Depends(auth.admin_only)])
def api_voices_stock(body: dict[str, Any] | None = None):
    """Make the AI singers: one male, one female, each lifted from one of the radio's own YuE2 songs (the vocal split
    out, the loudest 30 s kept). Already there and ready: left alone, unless `again` is set."""
    again = bool((body or {}).get("again"))
    have = ai_samples()
    pending = set()   # a stock voice still being made counts as there too, so two clicks make one pair
    for v in db.query("voices", "source='stock'"):
        try:
            pending.add((json.loads(v.get("notes") or "{}") or {}).get("gender") or "")
        except Exception:
            pass
    made, jobs = [], []
    for g in AI_SINGER_GENDERS:
        if (g in have or g in pending) and not again:
            continue
        s = _stock_song_for(g)
        if not s:
            continue
        files = s.get("files") or {}
        src = LIBRARY / s["id"] / (files.get("vocals") or files.get("master") or files.get("mp3"))
        voice = db.insert("voices", {"id": new_id("v_"), "name": f"AI singer · {g}", "file": None, "source": "stock", "duration_s": float(s.get("duration_s") or 0), "created": now(),
                                     "notes": json.dumps({"gender": g, "song_id": s["id"], "title": s.get("title")})})
        job = enqueue("voice_prep", {"voice_id": voice["id"], "src": str(src), "remove_music": 0 if files.get("vocals") else 1, "requested_by": "you"}, priority=20)
        made.append(voice)
        jobs.append(job)
    return {"voices": made, "jobs": jobs, "have": sorted(have)}


def _voice_tags(trained: bool, blend: float, fit: dict[str, Any] | None) -> list[str]:
    """What sang: the AI singer alone, a share of each, or the trained voice (with its weight blend when under 100 %)."""
    who = (fit or {}).get("reference") or "voice"
    if who == "ai":
        return ["AI singer"]
    if who == "both":
        return ["trained voice", f"{int(round(blend * 100))}% mine"]
    tags = ["trained voice"] if trained and blend > 0.005 else []
    if trained and 0.005 < blend < 0.995:
        tags.append(f"{int(round(blend * 100))}% voice")
    if trained and blend <= 0.005:
        tags.append("AI singer only")   # no stock AI singer on file yet: the base model on the voice's own sample
    return tags


def _sang_as(voice: dict[str, Any], blend: float, fit: dict[str, Any] | None) -> str:
    who = (fit or {}).get("reference") or "voice"
    ai = (fit or {}).get("ai_sample") or "the AI singer"
    if who == "ai":
        return f"as the AI singer ({ai}), nothing of {voice.get('name')}"
    if who == "both":
        return f"as {voice.get('name')} {int(round(blend * 100))}% with the AI singer {int(round((1 - blend) * 100))}%"
    return f"as {voice.get('name')}"


def _octaves_word(n: int | None) -> str:
    """'down an octave', 'up 2 octaves', '' for none: the whole octaves a re-voice moved the melody to sit in the voice."""
    n = int(n or 0)
    if not n:
        return ""
    k = abs(n) // 12
    return ("down" if n < 0 else "up") + (" an octave" if k == 1 else f" {k} octaves")


def _fit_tags(fit: dict[str, Any] | None) -> list[str]:
    fit = fit or {}
    w = _octaves_word(fit.get("octaves"))
    shift = int(fit.get("shift") or 0)
    tags = ([w] if w else []) + ([f"{shift:+d} st"] if shift else [])
    if not tags and fit.get("key_safe") is False:
        tags = ["as sung"]
    return tags


def _fit_note(fit: dict[str, Any] | None) -> str:
    """For the job message: ' (the song sat at A3, the voice at C#3: moved down an octave)'."""
    if not fit or not fit.get("source_note") or not fit.get("voice_note"):
        return ""
    w = _octaves_word(fit.get("octaves"))
    shift = int(fit.get("shift") or 0)
    if w and shift:
        how = f"moved {w} and {shift:+d} semitones"
    elif w:
        how = f"moved {w}"
    elif shift:
        how = f"moved {shift:+d} semitones"
    elif fit.get("key_safe") is False:
        how = "kept as sung"
    else:
        how = "no move needed"
    return f" (the song sat at {fit['source_note']}, the voice at {fit['voice_note']}: {how})"


@app.post("/api/songs/{song_id}/revoice", dependencies=[Depends(need_voices)])
def api_song_revoice(song_id: str, body: dict[str, Any]):
    parent = db.get("songs", song_id)
    if not parent or not db.get("voices", body.get("voice_id", "")):
        raise HTTPException(404, "song or voice not found")
    shift = max(-12, min(12, int(body.get("semi_tone_shift") or 0)))
    blend = max(0.0, min(1.0, float(body.get("blend") if body.get("blend") is not None else 1.0)))  # trained voices: 1 = all you, 0.5 = half AI singer, 0 = the AI singer only
    return enqueue("revoice", {"song_id": song_id, "voice_id": body["voice_id"], "semi_tone_shift": shift, "blend": blend, "key_safe": bool(body.get("key_safe", True)),
                               "title": revoiced_title(parent.get("title"))}, priority=8)


@app.post("/api/songs/{song_id}/stems")
def api_song_stems(song_id: str):
    if not db.get("songs", song_id):
        raise HTTPException(404)
    return enqueue("stems", {"song_id": song_id}, priority=8)


@app.api_route("/media/{song_id}/{kind}", methods=["GET", "HEAD"])
def media(song_id: str, kind: str, dl: int = 0):
    s = db.get("songs", song_id)
    if not s:
        raise HTTPException(404)
    files = s.get("files") or {}
    if kind not in files:
        raise HTTPException(404, "no such file")
    path = LIBRARY / song_id / files[kind]
    if not path.exists():
        raise HTTPException(404, "file missing")
    media_types = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac", ".abc": "text/plain", ".mid": "audio/midi"}
    if dl:
        return FileResponse(str(path), media_type=media_types.get(path.suffix.lower(), "application/octet-stream"), filename=f"{re.sub(r'[^A-Za-z0-9 _-]+', '', s.get('title') or song_id)}{path.suffix}")
    return FileResponse(str(path), media_type=media_types.get(path.suffix.lower(), "application/octet-stream"))


# --------------------------------------------------------------------------- API: voices
@app.get("/api/voices", dependencies=[Depends(need_voices)])
def api_voices():
    out = []
    training = {j["params"].get("voice_id"): j for j in db.query("jobs", "type='voice_train' AND status IN ('queued','running')") if isinstance(j.get("params"), dict)}
    for v in db.query("voices", order="created DESC"):
        v["ready"] = bool(v.get("file")) and (VOICES / v["file"]).exists()
        v["url"] = f"/api/voices/{v['id']}/file" if v["ready"] else None
        v["model"] = v.get("model") if v.get("model") and Path(v["model"]).exists() else None
        try:
            v["train_meta"] = json.loads(v["train_meta"]) if isinstance(v.get("train_meta"), str) and v["train_meta"] else None
        except Exception:
            v["train_meta"] = None
        idx = voice_ft.train_index(v["id"])
        v["pitch"] = (v.get("train_meta") or {}).get("pitch") if isinstance(v.get("train_meta"), dict) else None
        v["train_files"] = idx.get("files", [])
        v["train_seconds"] = round(float(idx.get("seconds") or 0) + float(v.get("duration_s") or 0), 1)
        j = training.get(v["id"])
        v["training"] = {"job_id": j["id"], "status": j["status"], "progress": j.get("progress") or 0, "message": j.get("message") or ""} if j else None
        out.append(v)
    return out


@app.post("/api/voices", dependencies=[Depends(need_voices)])
async def api_voice_create(file: UploadFile = File(...), name: str = Form("New voice"), remove_music: int = Form(0), source: str = Form("upload")):
    ext = Path(file.filename or "voice").suffix.lower() or ".webm"
    tmp = UPLOADS / f"{new_id('voice_')}{ext}"
    with tmp.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    wav = tmp.with_name(tmp.stem + "_44k.wav")  # never the same path as the upload (a .wav upload would overwrite itself)
    audio_utils.to_wav(tmp, wav, sr=44100, mono=True)
    tmp.unlink(missing_ok=True)
    voice = db.insert("voices", {"id": new_id("v_"), "name": name.strip()[:40] or "New voice", "file": None, "source": source, "duration_s": audio_utils.duration_seconds(wav), "created": now(), "notes": ""})
    job = enqueue("voice_prep", {"voice_id": voice["id"], "src": str(wav), "remove_music": int(remove_music)}, priority=20)
    return {"voice": voice, "job": job}


@app.post("/api/voices/from_song", dependencies=[Depends(need_voices)])
def api_voice_from_song(body: dict[str, Any]):
    """A voice out of a song's singer: the vocal is lifted from the song, a YuE2 one or an
    import, and the loudest 30 s kept as the sample. Every singer the radio ever had, and every record in the Library,
    is a voice for Swap the singer from then on. Seed-VC has no singer of its own: a voice is always a sample of someone."""
    s = db.get("songs", (body or {}).get("song_id") or "")
    if not s:
        raise HTTPException(404, "song not found")
    files = s.get("files") or {}
    dry = bool(files.get("vocals"))   # a stem split, or a re-voice: the voice alone already
    src = LIBRARY / s["id"] / (files.get("vocals") or files.get("voiced") or files.get("master") or files.get("mp3") or "")
    if not src.is_file():
        raise HTTPException(404, "no audio for that song")
    name = ((body or {}).get("name") or f"Singer of {s['title']}").strip()[:40] or "Singer"
    voice = db.insert("voices", {"id": new_id("v_"), "name": name, "file": None, "source": "song", "duration_s": float(s.get("duration_s") or 0), "created": now(),
                                 "notes": json.dumps({"song_id": s["id"], "title": s.get("title")})})
    job = enqueue("voice_prep", {"voice_id": voice["id"], "src": str(src), "remove_music": 0 if dry else 1, "requested_by": "you"}, priority=20)
    return {"voice": voice, "job": job}


@app.post("/api/voices/{voice_id}/train", dependencies=[Depends(need_voices)])
def api_voice_train(voice_id: str, body: dict[str, Any] | None = None):
    """Queue a fine-tune of the singing model on this voice (sample + extra recordings). ~10 min on a 24 GB card."""
    v = db.get("voices", voice_id)
    if not v or not v.get("file"):
        raise HTTPException(404, "voice not ready")
    if db.count("jobs", "type='voice_train' AND status IN ('queued','running')") > 0:
        raise HTTPException(409, "a voice is already training; one at a time")
    steps = (body or {}).get("steps")
    job = enqueue("voice_train", {"voice_id": voice_id, "steps": int(steps) if steps else None, "remove_music": bool((body or {}).get("remove_music", True)), "requested_by": "you"}, priority=4)
    return {"job": _job_view(job)}


@app.post("/api/voices/{voice_id}/train/files", dependencies=[Depends(need_voices)])
async def api_voice_train_files(voice_id: str, files: list[UploadFile] = File(...)):
    """Add recordings of the same person for training (any audio; converted to 44.1 kHz mono wav)."""
    v = db.get("voices", voice_id)
    if not v:
        raise HTTPException(404)
    idx = voice_ft.train_index(voice_id)
    added = 0
    for f in files:
        ext = Path(f.filename or "rec").suffix.lower() or ".wav"
        tmp = UPLOADS / f"{new_id('train_')}{ext}"
        with tmp.open("wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
        try:
            idx = voice_ft.add_training_file(voice_id, tmp, f.filename or "")
            added += 1
        except Exception as e:
            LOG.warning("training file %s rejected: %s", f.filename, e)
        finally:
            tmp.unlink(missing_ok=True)
    return {"added": added, **idx}


@app.post("/api/voices/{voice_id}/train/folder", dependencies=[Depends(need_voices)])
def api_voice_train_folder(voice_id: str, body: dict[str, Any]):
    """Add every audio file in a folder as training material: {path, exclude?: [substrings], max_minutes?}. Files may have
    music behind them; the training job pulls the vocal out first."""
    if not db.get("voices", voice_id):
        raise HTTPException(404)
    path = str(body.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "path required")
    try:
        return voice_ft.import_folder(voice_id, path, exclude=body.get("exclude") or [], max_minutes=float(body.get("max_minutes") or 45))
    except FileNotFoundError:
        raise HTTPException(404, f"folder not found: {path}")


@app.delete("/api/voices/{voice_id}/train/files/{name}", dependencies=[Depends(need_voices)])
def api_voice_train_file_delete(voice_id: str, name: str):
    if not db.get("voices", voice_id):
        raise HTTPException(404)
    return voice_ft.delete_training_file(voice_id, name)


@app.delete("/api/voices/{voice_id}/model", dependencies=[Depends(need_voices)])
def api_voice_model_delete(voice_id: str):
    """Forget the trained model; the voice goes back to zero-shot conversion from its sample."""
    if not db.get("voices", voice_id):
        raise HTTPException(404)
    voice_ft.delete_model(voice_id)
    db.update("voices", voice_id, model=None, trained=None, train_meta=None)
    return {"ok": True}


@app.post("/api/voices/{voice_id}/rename", dependencies=[Depends(need_voices)])
def api_voice_rename(voice_id: str, body: dict[str, Any]):
    if not db.get("voices", voice_id):
        raise HTTPException(404)
    return db.update("voices", voice_id, name=(body.get("name") or "Voice")[:40])


@app.delete("/api/voices/{voice_id}", dependencies=[Depends(need_voices)])
def api_voice_delete(voice_id: str):
    v = db.get("voices", voice_id)
    if v:
        if v.get("file"):
            (VOICES / v["file"]).unlink(missing_ok=True)
        voice_ft.delete_voice_files(voice_id)
        db.delete("voices", voice_id)
    return {"ok": True}


@app.get("/api/voices/{voice_id}/file", dependencies=[Depends(need_voices)])
def api_voice_file(voice_id: str):
    v = db.get("voices", voice_id)
    if not v or not v.get("file") or not (VOICES / v["file"]).exists():
        raise HTTPException(404)
    return FileResponse(str(VOICES / v["file"]), media_type="audio/wav")


# --------------------------------------------------------------------------- API: loras, lyrics, LLM
@app.get("/api/loras")
def api_loras():
    return loras_mod.scan()


@app.post("/api/lyrics/write")
def api_lyrics_write(body: dict[str, Any]):
    try:
        text = llm.write_lyrics(body.get("topic", ""), mood=body.get("mood", ""), style=body.get("style", ""), length=body.get("length", "standard"), extra_rules=body.get("extra", ""), instrumental=bool(body.get("instrumental")))
    except Exception as e:
        raise HTTPException(502, f"lyric writer unavailable: {e}")
    return {"lyrics": text}


@app.post("/api/lyrics/improve")
def api_lyrics_improve(body: dict[str, Any]):
    try:
        return {"lyrics": llm.improve_lyrics(body.get("lyrics", ""), body.get("instruction", "make it tighter"))}
    except Exception as e:
        raise HTTPException(502, f"lyric writer unavailable: {e}")


@app.post("/api/style/suggest")
def api_style_suggest(body: dict[str, Any]):
    try:
        return {"style": llm.suggest_style(body.get("description", ""), body.get("lyrics", ""))}
    except Exception as e:
        raise HTTPException(502, f"style writer unavailable: {e}")


@app.post("/api/transcribe")
def api_transcribe(body: dict[str, Any]):
    if not body.get("path") or not Path(body["path"]).exists():
        raise HTTPException(400, "upload first")
    return enqueue("transcribe", {"src": body["path"], "language": body.get("language")}, priority=20)


# --------------------------------------------------------------------------- API: arrangements (sheets, 1.6)
def save_arrangement(name: str, source: str, abc: str, words: str = "", source_hash: str | None = None, song_id: str | None = None,
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """A lead sheet kept for later: the sheet, a melody-only copy, the words it came with, and the budget new words
    must fit (the original words' syllables per line when there are words, the melody's note count otherwise)."""
    sh = sheet.parse(abc)
    words = (words or "").strip()
    from_words = sheet.budget_from_words(words) if words else []
    use_words = any(b.get("lines") for b in from_words)
    info = {"key": sh["key"], "meter": sh["meter"], "bpm": sh["bpm"], "bars": sh["bars"], "duration_s": sh["duration_s"],
            "has_chords": sh["has_chords"], "melody_voice": sh["melody_voice"],
            "sections": [{k: s[k] for k in ("label", "tag", "bars", "start_s", "end_s", "lines", "syllables")} for s in sh["sections"]],
            "budget": from_words if use_words else sheet.budget(sh), "budget_source": "words" if use_words else "sheet", **(extra or {})}
    row = {"id": new_id("arr_"), "name": (name or "Untitled").strip()[:120], "source": source, "source_hash": source_hash, "song_id": song_id,
           "abc": abc, "abc_melody": sheet.strip_chords(abc) if sh["has_chords"] else abc, "info": info, "words": words + ("\n" if words else ""),
           "duration_s": sh["duration_s"], "on_radio": 0, "used": 0, "created": now()}
    return db.insert("arrangements", row)


def public_arrangement(a: dict[str, Any], full: bool = False) -> dict[str, Any]:
    info = a.get("info") or {}
    out = {"id": a["id"], "name": a.get("name"), "source": a.get("source"), "song_id": a.get("song_id"), "duration_s": a.get("duration_s"),
           "on_radio": bool(a.get("on_radio")), "used": a.get("used") or 0, "last_used": a.get("last_used"), "created": a.get("created"),
           "has_words": bool((a.get("words") or "").strip()),
           "info": {k: info.get(k) for k in ("key", "meter", "bpm", "bars", "has_chords", "melody_voice", "budget_source", "budget", "sections", "warnings")}}
    if full:
        out["abc"] = a.get("abc")
        out["words"] = a.get("words") or ""
    return out


def _file_hash(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _arrangement_or_404(aid: str) -> dict[str, Any]:
    a = db.get("arrangements", aid)
    if not a:
        raise HTTPException(404, "no sheet with that id")
    return a


@app.get("/api/arrangements")
def api_arrangements():
    return [public_arrangement(a) for a in db.query("arrangements", order="created DESC", limit=300)]


@app.get("/api/arrangements/{aid}")
def api_arrangement(aid: str):
    return public_arrangement(_arrangement_or_404(aid), full=True)


@app.post("/api/arrangements")
def api_arrangement_make(body: dict[str, Any]):
    """A sheet from a recording (a job: SheetSage2 reads it, Whisper hears the words), from a library song (its own
    sheet, at once), from pasted ABC, or from an uploaded .abc/.mid/.musicxml/.mxl (at once; .pdf and images go
    through Audiveris in a job). Answers {arrangement} or {job}."""
    name = str(body.get("name") or "").strip()
    if body.get("source_song_id"):
        song = db.get("songs", str(body["source_song_id"]))
        if not song:
            raise HTTPException(404, "no song with that id")
        abc = (song.get("abc") or "").strip()
        if abc:
            words = "" if song.get("instrumental") else (song.get("lyrics") or "")
            arr = save_arrangement(name or song.get("title") or "Untitled", "library", abc, words=words, song_id=song["id"])
            return {"arrangement": public_arrangement(arr, full=True)}
        sf = song.get("files") or {}
        src = LIBRARY / song["id"] / sf.get("master", sf.get("mp3", ""))
        if not src.exists():
            raise HTTPException(404, "that song has no sheet and no audio to read one from")
        job = enqueue("arrange", {"src": str(src), "name": name or song.get("title") or "Untitled", "title": name or song.get("title"), "source": "library",
                                  "song_id": song["id"], "instrumental": bool(song.get("instrumental"))}, priority=20)
        return {"job": job}
    if (body.get("abc_text") or "").strip():
        abc = str(body["abc_text"]).strip() + "\n"
        v = sheet.validate(abc)
        if not v["ok"]:
            raise HTTPException(400, "that sheet will not do: " + "; ".join(v["problems"]))
        arr = save_arrangement(name or "Pasted sheet", "abc", abc, words=str(body.get("words") or ""))
        return {"arrangement": public_arrangement(arr, full=True)}
    if body.get("file"):
        p = Path(str(body["file"]))
        try:
            p.resolve().relative_to(UPLOADS.resolve())
        except ValueError:
            raise HTTPException(400, "upload the file first")
        if not p.exists():
            raise HTTPException(404, "that upload is gone")
        h = _file_hash(p)
        for a in db.query("arrangements", "source_hash=?", (h,), limit=1):
            return {"arrangement": public_arrangement(a, full=True), "existing": True}
        ext = p.suffix.lower()
        if ext in (".abc", ".mid", ".midi", ".musicxml", ".xml", ".mxl"):
            try:
                conv = sheet.from_file(p)
            except Exception as e:
                raise HTTPException(400, f"could not read that sheet: {e}")
            arr = save_arrangement(name or p.stem, "file", conv["abc"], words=conv.get("words") or "", source_hash=h,
                                   extra={k: conv.get(k) for k in ("part", "parts", "melody_track", "tracks") if conv.get(k) is not None})
            return {"arrangement": public_arrangement(arr, full=True)}
        if ext in sheet.SHEET_EXTS:
            if not sheet.audiveris_path():
                raise HTTPException(409, "Reading printed sheet music needs Audiveris (free, github.com/Audiveris/audiveris) installed on this computer.")
            job = enqueue("arrange", {"src": str(p), "name": name or p.stem, "title": name or p.stem, "source": "page", "kind": "sheet", "source_hash": h}, priority=20)
            return {"job": job}
        job = enqueue("arrange", {"src": str(p), "name": name or p.stem, "title": name or p.stem, "source": "recording", "source_hash": h,
                                  "instrumental": bool(body.get("instrumental")), "language": body.get("language")}, priority=20)
        return {"job": job}
    raise HTTPException(400, "give me a recording, a library song, a sheet file or pasted ABC")


@app.post("/api/arrangements/{aid}")
def api_arrangement_update(aid: str, body: dict[str, Any]):
    a = _arrangement_or_404(aid)
    fields: dict[str, Any] = {}
    if "name" in body:
        fields["name"] = str(body["name"] or "").strip()[:120] or a.get("name")
    if "on_radio" in body:
        fields["on_radio"] = 1 if body["on_radio"] in (1, True, "1", "true", "on") else 0
    if "words" in body:
        words = llm.clean_lyrics(str(body["words"] or "")[:12000]).strip()
        fields["words"] = words + ("\n" if words else "")
        info = dict(a.get("info") or {})
        from_words = sheet.budget_from_words(words) if words else []
        if any(b.get("lines") for b in from_words):
            info["budget"], info["budget_source"] = from_words, "words"
        else:
            info["budget"], info["budget_source"] = sheet.budget(sheet.parse(a["abc"])), "sheet"
        fields["info"] = info
    return public_arrangement(db.update("arrangements", aid, **fields) if fields else a, full=True)


@app.delete("/api/arrangements/{aid}", dependencies=[Depends(auth.admin_only)])
def api_arrangement_delete(aid: str):
    _arrangement_or_404(aid)
    db.delete("arrangements", aid)
    return {"ok": True}


@app.post("/api/arrangements/{aid}/fit")
def api_arrangement_fit(aid: str, body: dict[str, Any]):
    """Do these words sit on this tune? Line by line, syllables it has against what the melody wants."""
    a = _arrangement_or_404(aid)
    return sheet.check_fit(str(body.get("lyrics") or ""), (a.get("info") or {}).get("budget") or [])


@app.post("/api/arrangements/{aid}/words")
def api_arrangement_words(aid: str, body: dict[str, Any], request: Request):
    """New words to this tune: under `station`'s rules when one is named (the owner only), the writer's own otherwise,
    in `style` (one of the channel's lines on a channel; the Create box's sound without one). Nothing is queued."""
    a = _arrangement_or_404(aid)
    sid = str(body.get("station") or "").strip()
    st = db.get("stations", sid) if sid else None
    if sid and not st:
        raise HTTPException(404, "there is no channel with that name")
    if st and radio.is_favorites(st):
        raise HTTPException(409, "Favorites holds the songs you hearted; it does not write any.")
    if st and not auth.is_admin(request):
        raise HTTPException(403, "only the owner can have words written for a channel")
    _writer_awake()
    style = _channel_sound(st, str(body.get("style") or "")) if st else (str(body.get("style") or "").strip() or STYLE_PRESETS[1]["style"])
    lyrics, fit = radio.write_words(db, st, style, a, topic=str(body.get("topic") or "").strip()[:300])
    if not lyrics:
        raise HTTPException(502, "The writer could not get words onto that tune this time. Try again.")
    return {"lyrics": lyrics, "fit": fit, "style": style, "station": st["id"] if st else None, "budget": (a.get("info") or {}).get("budget")}


@app.post("/api/arrangements/{aid}/sing")
def api_arrangement_sing(aid: str, body: dict[str, Any], request: Request):
    """Sing these words to this tune, now: the sheet goes with the job, nothing is transcribed again. On a channel
    (the owner only) the song joins that channel in one of its own sounds; otherwise it is a Studio song in `style`."""
    a = _arrangement_or_404(aid)
    sid = str(body.get("station") or "").strip()
    st = db.get("stations", sid) if sid else None
    if sid and not st:
        raise HTTPException(404, "there is no channel with that name")
    if st and radio.is_favorites(st):
        raise HTTPException(409, "Favorites holds the songs you hearted; it does not write any.")
    if st and not auth.is_admin(request):
        raise HTTPException(403, "only the owner can put a song on a channel")
    instrumental = bool(body.get("instrumental"))
    mode = int(body.get("mode", (st or {}).get("mode", 0)) or 0)
    if mode not in (0, 1):
        mode = 0
    if instrumental:
        lyrics = sheet.skeleton(sheet.parse(a["abc"]))
        fit = None
    else:
        lyrics = llm.clean_lyrics(str(body.get("lyrics") or "")[:12000])
        if len(lyric_intel.features.lyric_lines(lyrics)) < 4:
            raise HTTPException(400, "those lyrics are too short to make a song from; write some first")
        fit = sheet.check_fit(lyrics, (a.get("info") or {}).get("budget") or [])
    style = _channel_sound(st, str(body.get("style") or "")) if st else (str(body.get("style") or "").strip() or STYLE_PRESETS[1]["style"])
    title = str(body.get("title") or "").strip()[:80]
    if not title:
        title = (first_lyric_line(lyrics) if not instrumental else "") or f"{a.get('name')} (new words)"
    shell = st or {"id": None, "mode": mode, "duration_s": a.get("duration_s") or 180, "voice_id": body.get("voice_id"),
                   "loras": body.get("loras") or {}, "steps": body.get("steps")}
    params = radio.rewrite_params(shell, a, title, lyrics, style, mode=mode, fit=fit)
    if not st:
        params.update({"source": "studio", "voice_id": body.get("voice_id") if voices_on() else None, "loras": body.get("loras") or {},
                       "station_id": None, "requested_by": "studio"})
        if body.get("seed") is not None:
            params["seed"] = int(body["seed"])
        if body.get("duration"):
            params["duration"] = int(max(params["duration"], int(body["duration"])))
    if instrumental:
        params.update({"instrumental": True, "lyrics_hash": None, "tags": ["rewrite", "instrumental"]})
    count = max(1, min(int(body.get("count", 1) or 1), 4))
    jobs = []
    for i in range(count):
        p = dict(params)
        if count > 1:
            p["seed"] = -1 if int(p.get("seed", -1)) < 0 else int(p["seed"]) + i
            p["title"] = f"{title} {i + 1}"
        jobs.append(enqueue("song", p, priority=5 if st else 10))
    db.update("arrangements", aid, used=int(a.get("used") or 0) + 1, last_used=now())
    return {"jobs": jobs, "job": jobs[0]["id"], "title": title, "style": style, "fit": fit, "station": st["id"] if st else None}


@app.get("/api/lyrics/habits")
def api_lyric_habits(station: str = ""):
    """What a channel (or the whole dial) has been reaching for lately: words, phrases, rhymes, image drawers."""
    return {"habits": lyric_intel.habits(db, station or None), "memory": lyric_intel.status(db)}


@app.get("/api/lyrics/memory")
def api_lyric_memory():
    return lyric_intel.status(db)


@app.post("/api/lyrics/corpus", dependencies=[Depends(auth.admin_only)])
def api_lyric_corpus(body: dict[str, Any] | None = None):
    """Measure some real songs for a channel's lane, in the background. Never touches song planning."""
    if not lyric_intel.outside_on():
        raise HTTPException(409, "Learning from real songs is switched off. Turn it on in Settings first.")
    stations = db.query("stations", "enabled=1", order="sort ASC, created ASC")
    sid = (body or {}).get("station")
    if sid:
        stations = [s for s in stations if s["id"] == sid]
    lyric_intel.corpus_async(db, stations)
    return {"started": [s["id"] for s in stations], "note": "this runs in the background, one request at a time"}


@app.get("/api/lyrics/covers")
def api_covers_list():
    return {"sung": lyric_intel.covers.recent(db), "enabled": lyric_intel.outside_on(),
            "default_chance": cfg("cover_chance")}


@app.get("/api/lyrics/covers/search")
def api_covers_search(q: str):
    """Look real songs up by artist, title or album, or an artist and words from a title in either order
    ("2pac girlfriend"). Returns what is singable, never the words."""
    if not lyric_intel.outside_on():
        raise HTTPException(409, "Learning from real songs is switched off. Turn it on in Settings first.")
    out = lyric_intel.finder.find(q)
    if out is None:
        raise HTTPException(503, "lrclib.net is not answering right now. Give it a minute and try again.")
    return out


def _lrclib_song(track_id: int) -> dict[str, Any] | None:
    try:
        return lyric_intel.lrclib.get_by_id(track_id, asked=True)
    except lyric_intel.lrclib.Unreachable:
        raise HTTPException(503, "lrclib.net is not answering right now. Give it a minute and try again.")


NO_NEW_VERSES = ("The writer could not get new verses around that chorus this time. Try again, or untick "
                 "keep only the chorus to sing the whole song.")


def _writer_awake() -> None:
    if not llm.quick_ok():
        raise HTTPException(503, "the lyric writer is not answering")


def _channel_sound(st: dict[str, Any], asked: str = "") -> str:
    """The style line a cover on this channel is sung in: the one asked for when it is one of the channel's own,
    otherwise one the channel has not used lately."""
    lines = radio.style_lines(st.get("style_prompts")) or ["English, pop, clear vocal, guitar, bass, drums, 100 BPM"]
    asked = (asked or "").strip()
    return asked if asked in lines else radio.pick_style(db, st, lines, [])


@app.get("/api/lyrics/covers/{track_id}")
def api_cover_preview(track_id: int, request: Request, station: str = "", interpolate: bool = False, style: str = ""):
    """The words of a real song, cut into a shape this radio can sing. With `interpolate`, only its chorus is kept and
    new verses are written around it: under `station`'s rules when one is given, the writer's own otherwise, in
    `style` (the Create box's sound) when there is one. Nothing is queued and nothing is rendered: on a channel the
    owner reads the words first and Begin (POST /api/radio/{id}/cover with `lyrics`) queues exactly those.
    The answer carries the `style` the words were written for, to be passed back with them."""
    if not lyric_intel.outside_on():
        raise HTTPException(409, "Learning from real songs is switched off. Turn it on in Settings first.")
    if station and interpolate and not auth.is_admin(request):
        raise HTTPException(403, "only the owner can have verses written for a channel")
    rec = _lrclib_song(track_id)
    if not rec or not (rec.get("plainLyrics") or "").strip():
        raise HTTPException(404, "lrclib does not have the words for that one")
    lyric_intel.corpus.store(db, rec, "search", keep_text=True)
    rows = db.query("lrclib_cache", "text_hash=?", (lyric_intel.corpus.text_hash((rec.get("plainLyrics") or "").strip()),),
                    order="fetched DESC", limit=1)
    real = db.get("stations", station) if station else None
    real = None if (real and radio.is_favorites(real)) else real
    st = real or {"id": "preview", "name": "Preview", "description": "", "style_prompts": [],
                  "duration_s": int(cfg("default_duration_s"))}
    sound = _channel_sound(real, style) if real else (style or "").strip()
    writer = None
    if interpolate:
        _writer_awake()
        writer = radio.interpolation_writer(db, real, sound) if real else lyric_intel.covers.plain_writer(sound)
    plan = lyric_intel.cover_plan(db, st, sound, float(st.get("duration_s") or 180),
                                  row=rows[0] if rows else None, interpolate=bool(interpolate), writer=writer,
                                  fallback=not interpolate)
    if not plan:
        if interpolate:
            raise HTTPException(502, NO_NEW_VERSES)
        raise HTTPException(400, "those words did not come through in a shape this radio can sing")
    return {**plan, "style": sound, "station": real["id"] if real else None}


@app.post("/api/radio/{station_id}/cover", dependencies=[Depends(auth.admin_only)])
def api_station_cover(station_id: str, body: dict[str, Any]):
    """Sing this real song on this channel, now. `interpolate` keeps only its chorus and writes new verses; with
    `lyrics` (the words the preview wrote, read and maybe changed by the owner) those words are sung as they are,
    in `style` when it is one of the channel's own lines."""
    st = db.get("stations", station_id)
    if not st:
        raise HTTPException(404, "there is no channel with that name")
    if radio.is_favorites(st):
        raise HTTPException(409, "Favorites holds the songs you hearted; it does not write any.")
    if not lyric_intel.outside_on():
        raise HTTPException(409, "Learning from real songs is switched off. Turn it on in Settings first.")
    try:
        track_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "which song? send the id from the search")
    interpolate = bool(body.get("interpolate"))
    style = _channel_sound(st, body.get("style") or "")
    written = llm.clean_lyrics(str(body.get("lyrics") or "")[:8000]) if interpolate else ""
    if written:
        # Begin: the owner has read these words; sing them as they are
        if len(lyric_intel.features.lyric_lines(written)) < lyric_intel.covers.MIN_LINES:
            raise HTTPException(400, "those lyrics are too short to make a song from")
        cached = db.query("lrclib_cache", "id=?", (track_id,), order="fetched DESC", limit=1)
        if cached:
            row = cached[0]
        else:
            rec = _lrclib_song(track_id) or {}
            row = {"id": track_id, "track": rec.get("trackName") or rec.get("name"), "artist": rec.get("artistName")}
        plan = lyric_intel.covers.interpolation_plan(row, written)
        job = enqueue("song", radio.cover_params(st, plan, style), priority=5)
        return {"job": job["id"], "title": plan["title"], "cover_of": plan.get("cover_of")}
    rec = _lrclib_song(track_id)
    if not rec or not (rec.get("plainLyrics") or "").strip():
        raise HTTPException(404, "lrclib does not have the words for that one")
    lyric_intel.corpus.store(db, rec, "search", keep_text=True)
    rows = db.query("lrclib_cache", "text_hash=?", (lyric_intel.corpus.text_hash((rec.get("plainLyrics") or "").strip()),),
                    order="fetched DESC", limit=1)
    writer = None
    if interpolate:
        _writer_awake()
        writer = radio.interpolation_writer(db, st, style)
    plan = lyric_intel.cover_plan(db, st, style, float(st.get("duration_s") or 180),
                                  row=rows[0] if rows else None, interpolate=interpolate, writer=writer,
                                  fallback=not interpolate)
    if not plan:
        if interpolate:
            raise HTTPException(502, NO_NEW_VERSES)
        raise HTTPException(400, "those words did not come through in a shape this radio can sing")
    job = enqueue("song", radio.cover_params(st, plan, style), priority=5)
    return {"job": job["id"], "title": plan["title"], "cover_of": plan.get("cover_of")}


@app.get("/api/lyrics/folders")
def api_lyrics_folders():
    radio.ensure_folders()
    out = []
    for d in sorted(p for p in radio.LYRICS_ROOT.iterdir() if p.is_dir()):
        items = radio.list_lyrics(d.name)
        used = sum(1 for i in items if db.get("lyrics_used", i["hash"]))
        out.append({"folder": d.name, "files": len(items), "used": used})
    return out


@app.get("/api/lyrics/folders/{folder}")
def api_lyrics_folder(folder: str):
    items = radio.list_lyrics(folder)
    for i in items:
        i["used"] = db.get("lyrics_used", i["hash"]) is not None
    return items


@app.post("/api/lyrics/folders/{folder}", dependencies=[Depends(auth.admin_only)])
def api_lyrics_folder_add(folder: str, body: dict[str, Any]):
    folder = re.sub(r"[^A-Za-z0-9 _-]+", "", folder).strip() or "misc"
    path = radio.save_lyrics_file(folder, body.get("title") or first_lyric_line(body.get("lyrics", "")) or "song", body.get("lyrics", ""))
    return {"saved": path.name, "folder": folder}


def _lyric_path(folder: str, filename: str) -> Path:
    path = (radio.LYRICS_ROOT / Path(folder).name / Path(filename).name)
    if not path.exists() or path.suffix.lower() not in (".txt", ".md"):
        raise HTTPException(404, "no such lyric file")
    return path


@app.get("/api/lyrics/folders/{folder}/{filename}")
def api_lyrics_file(folder: str, filename: str):
    path = _lyric_path(folder, filename)
    title, lyrics = radio.read_lyrics_file(path)
    h = radio.file_hash(path)
    songs = [public_song(x) for x in db.query("songs", "lyrics_hash=? AND status='ready'", (h,), limit=10)]
    return {"title": title, "lyrics": lyrics, "file": path.name, "folder": path.parent.name, "hash": h, "used": db.get("lyrics_used", h) is not None, "songs": songs}


@app.delete("/api/lyrics/folders/{folder}/{filename}", dependencies=[Depends(auth.admin_only)])
def api_lyrics_file_delete(folder: str, filename: str):
    path = _lyric_path(folder, filename)
    path.unlink()
    return {"ok": True}


@app.post("/api/lyrics/folders/{folder}/{filename}/render")
def api_lyrics_file_render(folder: str, filename: str, body: dict[str, Any] | None = None):
    """Sing this lyric file next: queued ahead of the auto stock on the station that owns the folder."""
    path = _lyric_path(folder, filename)
    body = body or {}
    st = db.get("stations", body.get("station_id") or "")
    if not st:
        hits = db.query("stations", "lyrics_folder=? AND enabled=1", (path.parent.name,), limit=1)
        st = hits[0] if hits else None
    if not st:
        raise HTTPException(409, "no station uses this folder; pick one")
    if not power_on():
        raise HTTPException(409, "power is off")
    params = radio.plan_song_for_station(db, st, lyrics_file=path.name, force_style=body.get("style") or None)
    params["requested_by"] = "you"
    job = enqueue("song", params, priority=5)
    return {"job": _job_view(job), "title": params.get("title")}


# --------------------------------------------------------------------------- API: stations + radio
@app.get("/api/stations")
def api_stations():
    return radio.stations_status(db)


@app.post("/api/stations", dependencies=[Depends(auth.admin_only)])
def api_station_save(body: dict[str, Any]):
    sid = body.get("id") or re.sub(r"[^a-z0-9]+", "-", (body.get("name") or "station").lower()).strip("-") or new_id("st_")
    if not body.get("id") and db.get("stations", sid):
        # the id comes from the name, so a second "Late Night" would have quietly overwritten the first
        raise HTTPException(409, f"There is already a channel called {body.get('name') or sid}. Give this one another name.")
    existing = db.get("stations", sid) or {"id": sid, "created": now(), "enabled": 1, "sort": 100}
    is_new = db.get("stations", sid) is None
    for k in ("name", "description", "style_prompts", "lyrics_folder", "voice_id", "loras", "mode", "duration_s", "auto_generate", "keep_ahead", "color", "lyric_policy", "instrumental", "enabled", "sort",
              "themes", "banned_topics", "variation", "fusions", "fusion_set", "replay_policy", "retention_days", "instrumental_chance", "mood", "explicit",
              "male_ratio", "place_chance", "steps"):
        if k in body:
            existing[k] = body[k]
    for k in ("male_ratio", "place_chance"):  # fractions 0..1; blank = station default (follow the style lines / 12 %)
        if k in body:
            existing[k] = None if body[k] in (None, "") else max(0.0, min(1.0, float(body[k])))
    if "cover_chance" in body:  # how often this channel sings a real song instead of writing one; blank = the dial's own setting
        existing["cover_chance"] = None if body["cover_chance"] in (None, "") else max(0.0, min(1.0, float(body["cover_chance"])))
    if "rewrite_chance" in body:  # how often it sings its own words to a saved tune (1.6); blank = never
        existing["rewrite_chance"] = None if body["rewrite_chance"] in (None, "") else max(0.0, min(1.0, float(body["rewrite_chance"])))
    if not is_new and body.get("color") in (None, "", "teal") and (db.get("stations", sid) or {}).get("color"):
        existing["color"] = (db.get("stations", sid) or {}).get("color")  # the editor used to send its default colour for every save
    if not is_new and "fusion_set" in body and not body.get("fusion_set") and (db.get("stations", sid) or {}).get("fusion_set") and not body.get("fusion_set_cleared"):
        existing["fusion_set"] = (db.get("stations", sid) or {}).get("fusion_set")  # a sound set the dropdown did not list must not be wiped by saving
    if "mood" in body:
        existing["mood"] = (str(body["mood"] or "").strip()[:300]) or None
    if "explicit" in body:
        existing["explicit"] = 1 if body["explicit"] in (1, True, "1", "true", "on") else 0
    if "retention_days" in body:
        existing["retention_days"] = None if body["retention_days"] in (None, "") else float(body["retention_days"])
        _STATION_RET.pop(sid, None)
    if "style_prompts" in body:
        existing["style_prompts"] = radio.style_lines(existing.get("style_prompts"))   # also splits lines glued together
    for k in ("themes", "banned_topics", "fusions"):
        if isinstance(existing.get(k), str):
            existing[k] = [x.strip() for x in re.split(r"[,\n]", existing[k]) if x.strip()]
    if not voices_on():
        existing["voice_id"] = None
    if is_new:
        existing.setdefault("duration_s", int(cfg("default_duration_s")))
        existing.setdefault("keep_ahead", int(cfg("keep_ahead_default")))
    existing.setdefault("lyrics_folder", sid)
    if radio.is_favorites(existing):
        existing.update(radio.FAVORITES_LOCK)  # whatever the screen sent, a Favorites channel makes nothing
        return db.insert("stations", existing)
    (radio.LYRICS_ROOT / existing["lyrics_folder"]).mkdir(parents=True, exist_ok=True)
    saved = db.insert("stations", existing)
    if is_new:
        radio.start_priming(db, saved)   # it owns the planner until it has its first songs
    return saved


@app.post("/api/stations/quick", dependencies=[Depends(auth.admin_only)])
def api_station_quick(body: dict[str, Any]):
    """Plain English in, a whole channel back. Nothing is saved: the screen shows what it made and the person
    changes anything they like before pressing save, which is the ordinary POST /api/stations."""
    text = str((body or {}).get("text") or "").strip()
    if len(text) < 8:
        raise HTTPException(400, "Say a sentence or two about the channel first.")
    body = body or {}

    def num(key):
        v = body.get(key)
        return None if v in (None, "") else float(v)

    out = wizard.draft(
        db, text,
        instrumental=bool(body.get("instrumental")),
        cover_chance=num("cover_chance"),
        explicit=None if body.get("explicit") is None else bool(body.get("explicit")),
        minutes=num("minutes"),
        keep_ahead=None if num("keep_ahead") is None else int(num("keep_ahead")),
        enabled=body.get("enabled", True) is not False,
        auto_generate=body.get("auto_generate", True) is not False,
        duration_default=int(cfg("default_duration_s")),
        keep_default=int(cfg("keep_ahead_default")),
    )
    if out["station"].get("cover_chance") and not cfg("lrclib_enabled"):
        out["notes"].append("Covers need 'Learn from real songs' switched on in Settings, and it is off right now. "
                            "Until then this channel writes its own songs and the covers share waits.")
        out["needs"] = "lrclib_enabled"
    LOG.info("wizard: drafted %s (%s) from %d characters, %d style lines",
             out["station"]["id"], out["source"], len(text), len(out["station"]["style_prompts"]))
    return out


@app.delete("/api/stations/{station_id}", dependencies=[Depends(auth.admin_only)])
def api_station_delete(station_id: str):
    if radio.is_favorites(db.get("stations", station_id)):
        raise HTTPException(409, "Favorites is built in and comes back on the next start. Turn off 'Show on the dial' to hide it instead.")
    db.delete("stations", station_id)
    return {"ok": True}


@app.get("/api/radio/next")
def api_radio_next(station: str = "all", exclude: str = "", queue: int = 1):
    """The next song. Anything you lined up yourself comes first, whatever channel it is from -
    that is what makes a song you picked on the phone actually play next. queue=0 asks the
    radio for its own pick and leaves the line alone."""
    if station and station != "all":
        db.set_setting(f"station_last_tuned:{station}", now())
    picked = queue_take() if queue else None
    song = picked or radio.next_song(db, station, exclude.split(",") if exclude else [])
    if song is None and exclude:
        song = radio.next_song(db, station, [])  # a one-song station loops rather than going silent
    st = db.get("stations", station) if station != "all" else None
    pending = radio.queued_count(db, station) if station != "all" else db.count("jobs", "type='song' AND status IN ('queued','running')")
    return {"song": public_song(song) if song else None, "station": st, "pending_jobs": pending, "unplayed": radio.unplayed_count(db, station) if station != "all" else db.count("songs", "status='ready' AND plays=0"),
            "next_up": radio.station_next_up(db, station) if station != "all" else None, "power": power_on(),
            "from_queue": bool(picked), "queued_songs": db.count("play_queue")}


@app.post("/api/radio/{station_id}/generate")
def api_radio_generate(station_id: str, body: dict[str, Any] | None = None):
    st = db.get("stations", station_id)
    if not st:
        raise HTTPException(404)
    if radio.is_favorites(st):
        raise HTTPException(409, "Favorites holds the songs you hearted; it does not write any. Heart a song on any channel and it shows up here.")
    n = max(1, min(int((body or {}).get("count", 1)), 5))
    singer = (body or {}).get("singer")
    singer = singer if singer in ("male", "female") else None
    jobs = []
    for _ in range(n):
        params = radio.plan_song_for_station(db, st, force_singer=singer)
        params["requested_by"] = "you"
        jobs.append(enqueue("song", params, priority=5))
    return {"jobs": jobs}


# ───────────────────────────────────────────────────────────────── the play queue
# Songs you picked yourself. They play before the radio picks anything of its own.
#
# It lives here rather than in each browser's localStorage (where the web's old queue lived)
# so that every screen sees the same line: the phone, the tablet and the desk. Line three
# songs up at the desk, walk out to the car, they are what plays.
#
# /api/radio/next serves from here first, which is the whole trick: the Android app gets the
# queue without containing a single line of queue code, because asking for the next song is
# already the only thing it does.
QUEUE_LOCK = threading.Lock()


def queue_rows() -> list[dict[str, Any]]:
    return db.query("play_queue", order="sort ASC, created ASC")


def queue_public() -> list[dict[str, Any]]:
    """The line, as songs. A song that has been deleted since it was lined up drops out here
    rather than sitting in the list as a title that cannot play."""
    out = []
    for q in queue_rows():
        s = db.get("songs", q.get("song_id") or "")
        if not s or s.get("status") != "ready" or s.get("banned"):
            db.delete("play_queue", q["id"])
            continue
        item = public_song(s)
        item["queue_id"] = q["id"]
        out.append(item)
    return out


def queue_take() -> dict[str, Any] | None:
    """Take the first playable song out of the line, or None if there is nothing in it."""
    with QUEUE_LOCK:
        for q in queue_rows():
            db.delete("play_queue", q["id"])
            s = db.get("songs", q.get("song_id") or "")
            if s and s.get("status") == "ready" and not s.get("banned"):
                return s
    return None


@app.get("/api/playqueue")
def api_playqueue():
    q = queue_public()
    return {"queue": q, "count": len(q)}


@app.post("/api/playqueue")
def api_playqueue_add(body: dict[str, Any] | None = None):
    """Line a song up. next=true puts it at the front instead of the back."""
    song_id = str((body or {}).get("song_id") or "")
    s = db.get("songs", song_id)
    if not s:
        raise HTTPException(404, "that song is not here any more")
    if s.get("status") != "ready":
        raise HTTPException(409, "that one is still being made")
    with QUEUE_LOCK:
        rows = queue_rows()
        if any(r.get("song_id") == song_id for r in rows):
            return {"ok": True, "already": True, "queue": queue_public(), "count": len(rows)}
        sorts = [float(r.get("sort") or 0) for r in rows]
        sort = (min(sorts) - 1 if (body or {}).get("next") else max(sorts) + 1) if sorts else 0.0
        db.insert("play_queue", {"id": new_id("q_"), "song_id": song_id, "station_id": s.get("station_id"),
                                 "added_by": "you", "sort": sort, "created": now()})
    out = queue_public()
    return {"ok": True, "queue": out, "count": len(out)}


@app.post("/api/playqueue/clear")
def api_playqueue_clear():
    with QUEUE_LOCK:
        for q in queue_rows():
            db.delete("play_queue", q["id"])
    return {"ok": True, "queue": [], "count": 0}


@app.post("/api/playqueue/take")
def api_playqueue_take():
    """Hand out the next lined-up song and remove it. For a player that wants to ask
    explicitly; /api/radio/next already does this on its own."""
    s = queue_take()
    out = queue_public()
    return {"song": public_song(s) if s else None, "queue": out, "count": len(out)}


@app.delete("/api/playqueue/{qid}")
def api_playqueue_drop(qid: str):
    db.delete("play_queue", qid)
    out = queue_public()
    return {"ok": True, "queue": out, "count": len(out)}


@app.get("/api/settings")
def api_settings_get(request: Request):
    """Everything the Settings tab shows: each knob with its value, default and where that value came from."""
    return {"settings": config.describe(), "groups": [{"key": k, "label": lbl} for k, lbl in config.GROUPS],
            "features": features(), "urls": urls(), "admin": auth.is_admin(request),
            "radio_autofill": db.setting("radio_autofill", True), "queue_paused": queue_paused(),
            "apk": (TF_ROOT / "dist" / "TenForward.apk").exists(), "version": VERSION}


@app.post("/api/settings")
def api_settings(body: dict[str, Any], request: Request):
    """The two plain switches are for everybody; anything in config.DEFAULTS is an admin's to change."""
    out: dict[str, Any] = {"ok": True}
    for k, v in (body or {}).items():
        if k in ("radio_autofill", "queue_paused"):
            db.set_setting(k, bool(v))
        elif k in config.DEFAULTS:
            if not auth.is_admin(request):
                raise HTTPException(403, "only an admin can change the settings")
            try:
                out[k] = config.set_cfg(k, v)
            except ValueError as e:
                raise HTTPException(400, str(e))
            LOG.info("settings: %s = %r", k, out[k])
        else:
            raise HTTPException(400, f"there is no setting called {k}")
    out["radio_autofill"] = db.setting("radio_autofill", True)
    out["queue_paused"] = queue_paused()
    return out


@app.post("/api/settings/reset", dependencies=[Depends(auth.admin_only)])
def api_settings_reset(body: dict[str, Any]):
    """Forget the stored value and fall back to the environment variable or the built-in default."""
    try:
        key = str((body or {}).get("key") or "")
        return {"ok": True, "key": key, "value": config.reset_cfg(key)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/settings/reexport", dependencies=[Depends(auth.admin_only)])
def api_settings_reexport():
    """Copy every hearted song into the favourites folder as it is set now (after the folder changed)."""
    n = 0
    for song in db.query("songs", "status='ready' AND liked=1"):
        saved = export_song(song)
        if saved:
            db.update("songs", song["id"], saved_path=str(saved))
            n += 1
    LOG.info("settings: re-exported %d hearted song(s) to %s", n, songs_dir())
    return {"ok": True, "exported": n, "folder": str(songs_dir())}


# --------------------------------------------------------------------------- API: who is listening
@app.post("/api/auth/login")
def api_auth_login(body: dict[str, Any], request: Request):
    device = str((body or {}).get("device") or request.headers.get("user-agent") or "")[:120]
    try:
        user, token = auth.login(db, str((body or {}).get("name") or ""), (body or {}).get("password") or None, device)
    except ValueError as e:
        raise HTTPException(401, str(e))
    LOG.info("auth: %s signed in (%s)", user["name"], device[:60])
    resp = JSONResponse({"token": token, "user": auth.public_user(user), "login_required": auth.login_required(db)})
    resp.set_cookie(value=token, **auth.cookie_kwargs(request))
    return resp


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    auth.end_session(db, auth.token_from_request(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def api_auth_me(request: Request):
    """Public: the page asks this first. user is null when somebody has to sign in."""
    return {"user": auth.public_user(auth.current(request)), "login_required": auth.login_required(db),
            "users": auth.public_users(db)}


@app.get("/api/auth/users-public")
def api_auth_users_public():
    """Names for the 'who is listening' sheet, and whether each one needs a password. Empty when nobody has to sign in."""
    return {"users": auth.public_users(db), "login_required": auth.login_required(db)}


@app.get("/api/users", dependencies=[Depends(auth.admin_only)])
def api_users():
    out = []
    for u in auth.all_users(db):
        row = auth.public_user(u)
        row["created"] = u.get("created")
        row["last_seen"] = u.get("last_seen")
        row["sessions"] = db.count("sessions", "user_id=?", (u["id"],))
        out.append(row)
    return out


@app.post("/api/users", dependencies=[Depends(auth.admin_only)])
def api_user_create(body: dict[str, Any]):
    try:
        user = auth.create_user(db, str((body or {}).get("name") or ""), (body or {}).get("password") or None,
                                str((body or {}).get("role") or "user"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return auth.public_user(user)


@app.post("/api/users/{user_id}/password")
def api_user_password(user_id: str, body: dict[str, Any], request: Request):
    """An admin sets or removes anybody's password; everybody else may change their own. null removes it."""
    me = auth.require_user(request)
    if me["id"] != user_id and not auth.is_admin(request):
        raise HTTPException(403, "only an admin can change somebody else's password")
    pw = (body or {}).get("password")
    pw = None if pw in (None, "") else str(pw)
    if pw is not None and len(pw) < 3:
        raise HTTPException(400, "a password needs at least 3 characters")
    try:
        return auth.public_user(auth.set_password(db, user_id, pw))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/users/{user_id}/role", dependencies=[Depends(auth.admin_only)])
def api_user_role(user_id: str, body: dict[str, Any]):
    try:
        return auth.public_user(auth.set_role(db, user_id, str((body or {}).get("role") or "user")))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/users/{user_id}", dependencies=[Depends(auth.admin_only)])
def api_user_delete(user_id: str, request: Request):
    me = auth.require_user(request)
    if me["id"] == user_id:
        raise HTTPException(400, "you cannot remove yourself")
    try:
        auth.delete_user(db, user_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# --------------------------------------------------------------------------- the Android app
@app.get("/app/TenForward.apk")
def apk():
    """The phone app, served from this machine. Public: a phone has to be able to fetch it before it signs in."""
    path = TF_ROOT / "dist" / "TenForward.apk"
    if not path.exists():
        raise HTTPException(404, "the Android app has not been built on this machine yet")
    return FileResponse(str(path), media_type="application/vnd.android.package-archive", filename="TenForward.apk")


@app.get("/app")
def app_download():
    return RedirectResponse("/app/TenForward.apk")


@app.get("/api/app/version")
def api_app_version():
    """What the phone app should be. The app asks this when it opens and offers to update itself.
    `android\\build.ps1` writes dist/app.json next to the APK; without it only the size is known.
    Public on purpose: the phone may not be signed in yet."""
    apk_path = TF_ROOT / "dist" / "TenForward.apk"
    if not apk_path.exists():
        return {"available": False, "version": "", "build": 0, "url": "/app/TenForward.apk"}
    meta: dict[str, Any] = {}
    info = TF_ROOT / "dist" / "app.json"
    if info.exists():
        try:
            meta = json.loads(info.read_text(encoding="utf-8-sig"))   # PowerShell writes a BOM
        except (ValueError, OSError):
            meta = {}
    size = apk_path.stat().st_size
    return {"available": True, "version": str(meta.get("version") or ""), "build": int(meta.get("build") or 0),
            "size_mb": round(size / 1048576, 2), "bytes": size,
            "built": float(meta.get("built") or apk_path.stat().st_mtime), "notes": str(meta.get("notes") or ""),
            "url": "/app/TenForward.apk"}


@app.get("/api/power")
def api_power_get():
    return {"power": power_on(), "engine_loaded": engine.is_loaded(), "engine_busy": engine.busy, "comfy_busy": engine.comfy_busy(), "gpu_used_mb": gpu_used_mb(), "vram": engine.vram()}


@app.post("/api/power", dependencies=[Depends(auth.admin_only)])
def api_power_set(body: dict[str, Any]):
    on = bool(body.get("on", True))
    LOG.info("power switched %s", "ON" if on else "OFF")
    return set_power(on, force=bool(body.get("force")))


@app.get("/api/themes")
def api_themes(full: int = 0):
    """The theme catalog. full=1 adds brief, weight and the edited/custom flags (desktop theme editor)."""
    if full:
        rows = themes.catalog()
    else:
        rows = [{"id": t["id"], "name": t["name"], "kind": t["kind"]} for t in themes.THEMES]
    return {"themes": rows, "kinds": list(themes.THEME_KINDS), "banned_default": themes.DEFAULT_BANNED,
            "ban_options": list(themes.BANNED_TEXT.keys()), "fusion_sets": list(themes.FUSION_SETS.keys())}


@app.post("/api/themes", dependencies=[Depends(auth.admin_only)])
def api_theme_save(body: dict[str, Any]):
    """Create a custom theme or edit any theme (id, name, brief, kind, weight). Saved to data/themes.json."""
    try:
        return themes.save_theme(body or {})
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/themes/{theme_id}", dependencies=[Depends(auth.admin_only)])
def api_theme_delete(theme_id: str):
    """Custom themes are removed; built-ins are hidden until reset."""
    if theme_id not in themes.THEME_BY_ID:
        raise HTTPException(404, "no such theme")
    return {"ok": themes.delete_theme(theme_id)}


@app.post("/api/themes/{theme_id}/reset", dependencies=[Depends(auth.admin_only)])
def api_theme_reset(theme_id: str):
    """Drop the edit on a built-in theme (or bring a hidden one back)."""
    row = themes.reset_theme(theme_id)
    if row is None:
        raise HTTPException(404, "not a built-in theme")
    return row


@app.post("/api/engine/unload", dependencies=[Depends(auth.admin_only)])
def api_engine_unload():
    if engine.busy:
        raise HTTPException(409, "engine is busy")
    engine.unload()
    db.set_setting("engine_unloaded_at", now())
    return {"ok": True, "vram": engine.vram()}


# --------------------------------------------------------------------------- static
@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"), media_type="text/html")


@app.get("/sw.js")
def sw():
    return FileResponse(str(STATIC / "sw.js"), media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(str(STATIC / "manifest.webmanifest"), media_type="application/manifest+json")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(str(STATIC / "favicon.ico"), media_type="image/x-icon")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
# the public dial (listen.py): open, read-only, and the only thing a web server should ever proxy to
listen.setup(db=db, library=LIBRARY, static=STATIC, enqueue=enqueue, power_on=power_on, cfg=cfg, version=VERSION)
app.include_router(listen.router)


def _migrate_settings() -> None:
    """One-time, for a machine that ran an older version. `dev_settings.json` next to server.py names the
    values that machine had hard-coded (its favourites folder, its two addresses) so nothing moves under a
    running install. A fresh install has no such file and starts from the defaults in config.py."""
    if db.setting("settings_migrated_v9b"):
        return
    path = TF_ROOT / "dev_settings.json"
    if path.exists():
        try:
            for key, value in json.loads(path.read_text(encoding="utf-8")).items():
                if key in config.DEFAULTS and db.setting(key) is None:
                    db.set_setting(key, value)
                    LOG.info("settings: kept %s = %s (dev_settings.json)", key, value)
        except Exception:
            LOG.exception("settings: could not read dev_settings.json")
    db.set_setting("settings_migrated_v9b", True)
    config.invalidate()


@app.on_event("startup")
def startup():
    _migrate_settings()
    config.apply_llm()
    auth.bootstrap(db)
    songs_dir().mkdir(parents=True, exist_ok=True)
    if cfg("export_liked"):
        for song in db.query("songs", "status='ready' AND liked=1 AND saved_path IS NULL"):
            saved = export_song(song)
            if saved:
                db.update("songs", song["id"], saved_path=str(saved))
                LOG.info("exported hearted song '%s' -> %s", song.get("title"), saved)
    if personal_on():
        radio.migrate_my_songs_folder()
    radio.seed_stations(db)
    if personal_on():
        radio.relocate_vault_songs(db)
        copied = radio.import_vault(db)
        if copied:
            LOG.info("imported %d lyric notes from the notes folder", copied)
    if NO_WORKER:
        LOG.warning("TF_NO_WORKER is set: the API is up but nothing will be planned or rendered")
    else:
        worker.start()
        scheduler.start()
    seeded = db.seed_play_scores()  # first run after 0.9.6: the dial starts from the plays already on the songs
    if seeded:
        LOG.info("dial: seeded the listening score of %d channel%s from songs already played", seeded, "" if seeded == 1 else "s")
    lyric_intel.backfill_async(db)  # songs written before the memory existed still count as recent writing
    lyric_intel.corpus_async(db, db.query("stations", "enabled=1", order="sort ASC, created ASC"))
    people = db.count("users")
    LOG.info("Ten Forward %s ready on port %d (voices %s, my songs %s, %d listener%s, %s)", VERSION, PORT,
             "on" if voices_on() else "off", "on" if personal_on() else "off", people, "" if people == 1 else "s",
             "sign-in required" if auth.login_required(db) else "no sign-in")
    # the first http:// line in the log on purpose: Pinokio and other launchers open whatever they see first
    LOG.info("Open it here: http://127.0.0.1:%d", PORT)
    lan = str(cfg("lan_url") or "")
    if lan and "127.0.0.1" not in lan:
        LOG.info("From a phone on the same network: %s", lan)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=True, log_config=None)  # keep our own handlers (access.log)
