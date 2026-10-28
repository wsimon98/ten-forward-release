"""trainer.py - the Train tab: teach YuE2 a style (or an artist) from a folder of songs.

What happens, in the order Will sees it:

  1. a *job* is a folder under data/train/<id>/ : the songs (dataset/song_001.mp3 + song_001.txt), the captions
     (captions.json), the run (config.yaml, train.log, status.json) and the checkpoints (out/<name>/*.safetensors);
  2. "Write the captions": lyrics come from Whisper on a vocal stem (engine.separate + engine.transcribe), or from a
     .txt that sits beside the song; the style line comes from the local lyric model (llm.ask_json) and the genre hint;
  3. "Train": the radio's line is paused, the song being made finishes, YuE2 is released from the card, and Ostris'
     AI Toolkit (trainer/ai-toolkit, its own venv in trainer/venv) runs as a child process whose output goes to
     train.log; this module tails that file, so a server restart can pick the run up again;
  4. checkpoints: "Try it" converts one to a throw-away LoRA and queues a short song with it, "Keep this one" converts
     it for good (lora_convert.convert_file) and loras.scan() lists it everywhere LoRAs are listed.

Wired by server.py with one line, like listen.py:  app.include_router(trainer.setup(...)).
This module never imports server.py. Only the processes this module started are ever stopped, by exact PID.
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

import auth

LOG = logging.getLogger("tenforward.train")

TF_ROOT = Path(__file__).resolve().parent
TRAINER = TF_ROOT / "trainer"
AITK = TRAINER / "ai-toolkit"
PY = TRAINER / "venv" / "Scripts" / "python.exe"
MODELS = TRAINER / "models"            # MODELS_PATH for the trainer: ComfyUI folder layout
HF_HOME = TRAINER / "hf"
DATA = TF_ROOT / "data" / "train"

CHECKPOINT = MODELS / "checkpoints" / "yue2_3b_int8_convrot.safetensors"
HEADS = {"v9": MODELS / "ai_toolkit" / "tokenizer_head_joint_v9.bf16.safetensors",
         "v4": MODELS / "ai_toolkit" / "tokenizer_head_joint_v4.bf16.safetensors"}
NEEDED_FILES = {
    "YuE2 base model": CHECKPOINT,
    "SheetSage2 (reads the music)": MODELS / "audio_encoders" / "sheetsage2_bf16.safetensors",
    "MERT (hears the songs)": HF_HOME / "hub" / "models--m-a-p--MERT-v2-FullSong",
    "SheetSage2 code": HF_HOME / "hub" / "models--m-a-p--SheetSage2",
    "Song head v9": HEADS["v9"],
    "FFmpeg for the trainer": TRAINER / "ffmpeg" / "bin" / "avcodec-62.dll",
}

AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aac", ".wma"}
KEEP_EXT = {".mp3", ".wav", ".flac"}   # copied as they are; the rest become wav
MIN_SONGS_OK = 12
MAX_FILES_LOOK = 300

RUNNING_STATES = {"waiting", "freeing", "starting", "caching", "training"}
NEED_FREE_MB = {"sound": 14000, "both": 17500}

# the two knobs
LENGTHS = {"short": 8, "medium": 14, "long": 20}          # passes per song
STRENGTHS = {"subtle": (16, 5e-5), "balanced": (32, 1e-4), "strong": (48, 1.5e-4)}
SOUND_ONLY_SKIP = ["transformer.ar.", "time_embedder", "vae2llm", "llm2vae"]
TRY_LYRICS = "[Verse 1]\nThe lights come up across the water\nI hear the engine humming low\nEvery road I ever wandered\nBrought me back to where I know\n\n[Chorus]\nSing it out, sing it out tonight\nHold the sound until the morning light\nSing it out, we are alright\n"

_ctx: dict[str, Any] = {}
_lock = threading.RLock()
_jobs: dict[str, dict[str, Any]] = {}     # status.json of every job, loaded lazily
_caps: dict[str, dict[str, Any]] = {}     # captions.json of every job
_threads: dict[str, threading.Thread] = {}
_cancel: dict[str, threading.Event] = {}
_live: dict[str, dict[str, Any]] = {}     # per running job: pid, started, last log offset ...
_caption_job: list[str] = []              # the one job being captioned at a time

router = APIRouter(prefix="/api/train", dependencies=[Depends(auth.admin_only)])


def setup(*, db, engine, engine_mod, worker, enqueue: Callable, llm, loras, library: Path, queue_paused: Callable[[], bool], now: Callable[[], float] = time.time):
    """Wired by server.py once the database, the engine and the job line exist. Returns the router to include."""
    _ctx.update(db=db, engine=engine, engine_mod=engine_mod, worker=worker, enqueue=enqueue, llm=llm, loras=loras,
                library=library, queue_paused=queue_paused, now=now)
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        _recover()
    except Exception:
        LOG.exception("train: could not look through the old runs")
    return router


# =========================================================================== small helpers
def _now() -> float:
    return float(_ctx.get("now", time.time)())


def _slug(text: str, fallback: str = "my-style") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or fallback


def _jdir(job_id: str) -> Path:
    if not re.fullmatch(r"tr_[0-9a-f]{8}", job_id or ""):
        raise HTTPException(404, "no such training job")
    return DATA / job_id


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _job(job_id: str) -> dict[str, Any]:
    d = _jdir(job_id)
    with _lock:
        if job_id not in _jobs:
            st = _read_json(d / "status.json", None)
            if not st:
                raise HTTPException(404, "no such training job")
            _jobs[job_id] = st
        return _jobs[job_id]


def _save(job_id: str) -> None:
    with _lock:
        st = _jobs.get(job_id)
        if st is not None:
            st["updated"] = _now()
            _write_json(_jdir(job_id) / "status.json", st)


def _songs(job_id: str) -> list[dict[str, Any]]:
    d = _jdir(job_id)
    with _lock:
        if job_id not in _caps:
            _caps[job_id] = _read_json(d / "captions.json", {"songs": []})
        return _caps[job_id]["songs"]


def _save_songs(job_id: str) -> None:
    with _lock:
        if job_id in _caps:
            _write_json(_jdir(job_id) / "captions.json", _caps[job_id])


def _set(job_id: str, **fields) -> None:
    with _lock:
        _job(job_id).update(fields)
        _save(job_id)


def _logline(job_id: str, text: str) -> None:
    try:
        with (_jdir(job_id) / "train.log").open("a", encoding="utf-8", errors="replace") as f:
            f.write(text.rstrip("\n") + "\n")
    except Exception:
        pass


def suggest_trigger(name: str = "") -> str:
    """A made-up one-word token the model has never seen, the same for the same name."""
    seed = hashlib.sha1((name or secrets.token_hex(4)).encode()).digest()
    syl = ["ka", "ro", "vi", "zu", "mel", "tan", "lo", "shi", "dra", "pex", "fen", "qua", "bri", "nox", "ul", "ver", "tia", "gor", "sym", "wex"]
    n = 3 if seed[0] % 2 else 2
    word = "".join(syl[seed[i + 1] % len(syl)] for i in range(n))
    return word


def _clean_trigger(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:24]


def _gpu_free_mb() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=6).stdout
        used, total = [int(x) for x in out.strip().splitlines()[0].split(",")]
        return total - used
    except Exception:
        return None


def _duration(path: Path) -> float:
    try:
        import audio_utils
        return float(audio_utils.duration_seconds(path))
    except Exception:
        return 0.0


# =========================================================================== readiness
def readiness() -> dict[str, Any]:
    missing = [label for label, p in NEEDED_FILES.items() if not p.exists()]
    problems = []
    if not PY.exists():
        problems.append("the trainer's Python is not installed (trainer/venv)")
    if not (AITK / "run.py").exists():
        problems.append("the trainer program is not installed (trainer/ai-toolkit)")
    if missing:
        problems.append("not downloaded yet: " + ", ".join(missing))
    return {"ready": not problems, "problems": problems, "heads": [k for k, p in HEADS.items() if p.exists()],
            "can_split_vocals": (MODELS / "checkpoints" / "melbandroformer_vocals_kj.safetensors").exists()}


# =========================================================================== jobs: create, list, read
def _public(job_id: str, full: bool = False) -> dict[str, Any]:
    st = dict(_job(job_id))
    songs = _songs(job_id)
    st["song_count"] = len(songs)
    st["minutes"] = round(sum(float(s.get("seconds") or 0) for s in songs) / 60.0, 1)
    st["captioned"] = sum(1 for s in songs if s.get("lyrics") is not None and s.get("style") is not None)
    if full:
        st["songs"] = songs
        st["warnings"] = _warnings(songs)
        st["checkpoints"] = _checkpoints(job_id)
        st["plan"] = plan_for(st.get("settings") or {}, len(songs))
        _cleanup_tries(job_id)
    return st


def _warnings(songs: list[dict[str, Any]]) -> list[str]:
    out = []
    n = len(songs)
    if n and n < MIN_SONGS_OK:
        out.append(f"Only {n} song{'s' if n != 1 else ''}. Under {MIN_SONGS_OK} it tends to copy those songs instead of learning the style. 15 to 50 is ideal.")
    short = [s for s in songs if 0 < float(s.get("seconds") or 0) < 30]
    if short:
        out.append(f"{len(short)} song{'s are' if len(short) != 1 else ' is'} under 30 seconds.")
    long_ = [s for s in songs if float(s.get("seconds") or 0) > 600]
    if long_:
        out.append(f"{len(long_)} song{'s are' if len(long_) != 1 else ' is'} over 10 minutes; those take a while to prepare.")
    return out


@router.get("/status")
def api_status():
    """Is the trainer installed, is something training, what does the card have free."""
    active = _active_job()
    r = readiness()
    r["active"] = active
    r["free_mb"] = _gpu_free_mb()
    r["captioning"] = _caption_job[0] if _caption_job else None
    return r


@router.get("/jobs")
def api_jobs():
    rows = []
    for d in sorted(DATA.glob("tr_*"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            p = _public(d.name)
            rows.append({k: p.get(k) for k in ("id", "name", "trigger", "state", "message", "created", "updated", "song_count", "minutes", "captioned", "kept", "progress")})
        except HTTPException:
            continue
    return rows


@router.get("/trigger")
def api_trigger(name: str = ""):
    return {"trigger": suggest_trigger(name)}


@router.post("/jobs")
def api_create(body: dict[str, Any]):
    name = (str(body.get("name") or "").strip() or "My style")[:60]
    job_id = "tr_" + secrets.token_hex(4)
    d = DATA / job_id
    (d / "dataset").mkdir(parents=True, exist_ok=True)
    trigger = _clean_trigger(body.get("trigger") or "") or suggest_trigger(name)
    st = {"id": job_id, "name": name, "slug": _slug(name), "trigger": trigger, "genre": str(body.get("genre") or "").strip()[:200],
          "state": "draft", "message": "Add some songs to begin.", "created": _now(), "updated": _now(), "settings": {}, "progress": {}, "kept": {}, "tries": []}
    with _lock:
        _jobs[job_id] = st
        _caps[job_id] = {"songs": []}
        _save(job_id)
        _save_songs(job_id)
    return _public(job_id, full=True)


@router.get("/jobs/{job_id}")
def api_job(job_id: str):
    return _public(job_id, full=True)


@router.post("/jobs/{job_id}/meta")
def api_meta(job_id: str, body: dict[str, Any]):
    st = _job(job_id)
    if st["state"] in RUNNING_STATES:
        raise HTTPException(409, "it is training right now")
    with _lock:
        if "name" in body and str(body["name"]).strip():
            st["name"] = str(body["name"]).strip()[:60]
            st["slug"] = _slug(st["name"])
        if "trigger" in body:
            st["trigger"] = _clean_trigger(body["trigger"]) or st["trigger"]
        if "genre" in body:
            st["genre"] = str(body["genre"]).strip()[:200]
        if isinstance(body.get("settings"), dict):
            st["settings"] = body["settings"]
        _save(job_id)
    return _public(job_id, full=True)


@router.delete("/jobs/{job_id}")
def api_delete(job_id: str):
    st = _job(job_id)
    if st["state"] in RUNNING_STATES or job_id in _caption_job:
        raise HTTPException(409, "stop it first")
    _cleanup_tries(job_id, force=True)
    shutil.rmtree(_jdir(job_id), ignore_errors=True)
    with _lock:
        _jobs.pop(job_id, None)
        _caps.pop(job_id, None)
    return {"ok": True}


# =========================================================================== songs: look, add, remove
def _sidecar(audio: Path) -> dict[str, str] | None:
    """The .txt beside a song: Ten Forward's own export (header, then lyrics) or plain lyrics."""
    txt = audio.with_suffix(".txt")
    if not txt.exists():
        return None
    try:
        raw = txt.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return None
    lines = raw.splitlines()
    style = ""
    body_from = 0
    if lines and (lines[0].startswith("#") or re.match(r"^(Station|Style|Made|Ten Forward id):", lines[0])):
        # header block ends at the first blank line
        for i, line in enumerate(lines):
            m = re.match(r"^Style:\s*(.+)$", line)
            if m:
                style = m.group(1).strip()
            if not line.strip():
                body_from = i + 1
                break
        else:
            body_from = len(lines)
    lyrics = "\n".join(lines[body_from:]).strip()
    if not lyrics and not style:
        return None
    return {"lyrics": lyrics, "style": style}


def _scan(folder: Path, recursive: bool = False) -> list[Path]:
    it = folder.rglob("*") if recursive else folder.glob("*")
    return sorted((p for p in it if p.is_file() and p.suffix.lower() in AUDIO_EXT and not p.name.startswith(".")), key=lambda p: p.name.lower())


@router.post("/look")
def api_look(body: dict[str, Any]):
    """List the songs in a folder this computer can see."""
    raw = str(body.get("path") or "").strip().strip('"')
    if not raw:
        raise HTTPException(400, "type a folder first")
    folder = Path(raw)
    if not folder.exists():
        raise HTTPException(400, f"this computer cannot see {folder}")
    if folder.is_file():
        folder = folder.parent
    files = _scan(folder, bool(body.get("recursive")))
    subs = []
    if not files:
        for sub in sorted(p for p in folder.iterdir() if p.is_dir())[:60]:
            try:
                n = len(_scan(sub))
            except Exception:
                n = 0
            if n:
                subs.append({"name": sub.name, "path": str(sub), "count": n})
    capped = len(files) > MAX_FILES_LOOK
    files = files[:MAX_FILES_LOOK]
    rows = []
    for p in files:
        side = _sidecar(p)
        rows.append({"name": p.name, "path": str(p), "seconds": round(_duration(p), 1), "has_words": bool(side and side.get("lyrics"))})
    total = sum(r["seconds"] for r in rows)
    warn = []
    if capped:
        warn.append(f"More than {MAX_FILES_LOOK} songs in there. Pick a folder with just the songs to learn from.")
    if rows and len(rows) < MIN_SONGS_OK:
        warn.append(f"Only {len(rows)} song{'s' if len(rows) != 1 else ''}. Under {MIN_SONGS_OK} it tends to copy them instead of learning the style.")
    return {"path": str(folder), "files": rows, "count": len(rows), "minutes": round(total / 60.0, 1), "subfolders": subs, "warnings": warn}


def _add_file(job_id: str, src: Path, display: str) -> dict[str, Any]:
    d = _jdir(job_id) / "dataset"
    songs = _songs(job_id)
    used = {int(s["n"]) for s in songs}
    n = 1
    while n in used:
        n += 1
    ext = src.suffix.lower()
    if ext in KEEP_EXT:
        dst = d / f"song_{n:03d}{ext}"
        shutil.copy2(src, dst)
    else:
        import audio_utils
        dst = d / f"song_{n:03d}.wav"
        audio_utils.to_wav(src, dst)
    side = _sidecar(src)
    title = re.sub(r"[_]+", " ", Path(display).stem).strip()
    song = {"n": n, "file": dst.name, "title": title, "orig": display, "seconds": round(_duration(dst), 1),
            "lyrics": None, "style": None, "instrumental": False, "source": "", "state": "todo"}
    if side and side.get("lyrics"):
        song["sidecar"] = side
    elif side and side.get("style"):
        song["sidecar"] = side
    songs.append(song)
    return song


@router.post("/jobs/{job_id}/songs/folder")
def api_add_folder(job_id: str, body: dict[str, Any]):
    st = _job(job_id)
    if st["state"] in RUNNING_STATES or job_id in _caption_job:
        raise HTTPException(409, "wait until it has stopped")
    folder = Path(str(body.get("path") or "").strip().strip('"'))
    if not folder.exists():
        raise HTTPException(400, f"this computer cannot see {folder}")
    only = {str(x) for x in (body.get("files") or [])}
    files = [p for p in _scan(folder, bool(body.get("recursive")))[:MAX_FILES_LOOK] if not only or p.name in only]
    if not files:
        raise HTTPException(400, "no songs there")
    have = {s.get("orig") for s in _songs(job_id)}
    added = 0
    for p in files:
        if p.name in have:
            continue
        try:
            _add_file(job_id, p, p.name)
            added += 1
        except Exception as e:
            LOG.warning("train: could not take %s: %s", p, e)
    _save_songs(job_id)
    _set(job_id, state="draft" if st["state"] in ("done", "failed", "cancelled", "interrupted", "draft") else st["state"], message=f"{added} song{'s' if added != 1 else ''} added.")
    return _public(job_id, full=True)


@router.post("/jobs/{job_id}/songs/upload")
async def api_add_upload(job_id: str, files: list[UploadFile] = File(...)):
    st = _job(job_id)
    if st["state"] in RUNNING_STATES or job_id in _caption_job:
        raise HTTPException(409, "wait until it has stopped")
    tmpd = _jdir(job_id) / "_upload"
    tmpd.mkdir(exist_ok=True)
    added = 0
    try:
        for up in files:
            ext = Path(up.filename or "x").suffix.lower()
            if ext not in AUDIO_EXT:
                continue
            tmp = tmpd / f"{secrets.token_hex(4)}{ext}"
            with tmp.open("wb") as f:
                while chunk := await up.read(1 << 20):
                    f.write(chunk)
            try:
                _add_file(job_id, tmp, up.filename or tmp.name)
                added += 1
            except Exception as e:
                LOG.warning("train: could not take upload %s: %s", up.filename, e)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    _save_songs(job_id)
    _set(job_id, message=f"{added} song{'s' if added != 1 else ''} added.")
    return _public(job_id, full=True)


@router.delete("/jobs/{job_id}/songs/{n}")
def api_remove_song(job_id: str, n: int):
    st = _job(job_id)
    if st["state"] in RUNNING_STATES or job_id in _caption_job:
        raise HTTPException(409, "wait until it has stopped")
    songs = _songs(job_id)
    s = next((x for x in songs if int(x["n"]) == n), None)
    if not s:
        raise HTTPException(404, "no such song")
    d = _jdir(job_id) / "dataset"
    for p in d.glob(f"song_{n:03d}.*"):
        p.unlink(missing_ok=True)
    songs.remove(s)
    _save_songs(job_id)
    return _public(job_id, full=True)


# =========================================================================== captions
_FILLER = re.compile(r"^(thank you\.?|thanks for watching\.?|you|bye\.?|\.+|[♪♫\s]+|subtitles? by .*|transcribed by .*)$", re.I)


def _norm_line(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()


def structure_lyrics(segments: list[dict[str, Any]]) -> str:
    """Whisper segments (start, end, text) -> lyrics with section tags: gaps and repeats make the sections."""
    lines: list[tuple[float, float, str]] = []
    for sg in segments:
        t = str(sg.get("text") or "").strip()
        if not t or _FILLER.match(t):
            continue
        if lines and _norm_line(t) == _norm_line(lines[-1][2]) and float(sg.get("start") or 0) - lines[-1][1] < 1.0 and len(lines) > 2:
            # the same line twice in a row with no breath between is usually Whisper stuck, not a song
            if sum(1 for x in lines[-3:] if _norm_line(x[2]) == _norm_line(t)) >= 2:
                continue
        lines.append((float(sg.get("start") or 0), float(sg.get("end") or 0), t))
    if not lines:
        return ""
    chunks: list[list[str]] = [[]]
    prev_end = lines[0][0]
    for start, end, text in lines:
        if chunks[-1] and (start - prev_end >= 4.0 or len(chunks[-1]) >= 8):
            chunks.append([])
        chunks[-1].append(text)
        prev_end = end
    count = collections.Counter(_norm_line(t) for ch in chunks for t in ch)
    labels: list[str] = []
    verses = 0
    chorus_seen = 0
    for i, ch in enumerate(chunks):
        rep = sum(1 for t in ch if count[_norm_line(t)] >= 2) / max(1, len(ch))
        if rep >= 0.5:
            labels.append("[Chorus]")
            chorus_seen += 1
        elif i == len(chunks) - 1 and len(ch) <= 4 and i > 1:
            labels.append("[Outro]")
        elif chorus_seen >= 2 and verses >= 2 and not any(l == "[Bridge]" for l in labels):
            labels.append("[Bridge]")
        elif i == 0 and len(ch) <= 3 and len(chunks) > 3:
            labels.append("[Intro]")
        else:
            verses += 1
            labels.append(f"[Verse {verses}]")
    return "\n\n".join(f"{lab}\n" + "\n".join(ch) for lab, ch in zip(labels, chunks))


def _is_instrumental(segments: list[dict[str, Any]], seconds: float) -> bool:
    words = sum(len(str(s.get("text") or "").split()) for s in segments if not _FILLER.match(str(s.get("text") or "").strip()))
    return words < 10 or (seconds > 0 and words / (seconds / 60.0) < 8)


def _tags_from_llm(song: dict[str, Any], job: dict[str, Any]) -> str:
    """One line of lowercase style tags for a song, from the genre hint, the title, the length and a little of the words."""
    llm = _ctx["llm"]
    system = ("You write the style line of a training caption for a song-making AI. Reply with ONE line only: "
              "comma-separated lowercase tags describing the music, in this order: genre and sub-genre, mood, vocal type "
              "(male vocal, female vocal, duet, choir, rap, or instrumental), vocal delivery, lead instruments and production, "
              "tempo feel (slow ballad, mid-tempo, upbeat, fast), era or scene. Concrete tags only, 8 to 14 of them, "
              "no sentences, no artist names, no song names, no quotation marks, and spaces rather than hyphens inside a tag (male vocal, not male-vocal).")
    words = (song.get("lyrics") or "")
    excerpt = re.sub(r"\[[^\]]*\]\n?", "", words)[:500].strip()
    user = f"Genre hint from the person who owns these songs: {job.get('genre') or 'none given'}\nSong title: {song.get('title')}\nLength: {int(float(song.get('seconds') or 0))} seconds\n"
    if song.get("instrumental"):
        user += "This song has no singing: include the tag instrumental.\n"
    if (song.get("sidecar") or {}).get("style"):
        user += f"Its original generation prompt was: {song['sidecar']['style'][:300]}\n"
    if excerpt:
        user += f"Start of its lyrics:\n{excerpt}\n"
    text = llm.ask_json(system, user, max_tokens=160, temperature=0.7, timeout=120)
    return _clean_tags(text, job, instrumental=bool(song.get("instrumental")))


KEEP_HYPHEN = {"lo-fi", "hi-fi", "hip-hop", "k-pop", "j-pop", "synth-pop", "post-punk", "post-rock", "trip-hop", "neo-soul", "power-pop", "sing-along", "r&b", "drum-and-bass", "post-hardcore"}


def _clean_tags(text: str, job: dict[str, Any], instrumental: bool = False) -> str:
    line = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    line = re.sub(r"^(style( line)?|tags|caption)\s*:\s*", "", line, flags=re.I).strip().strip("\"'`")
    tags = [re.sub(r"\s+", " ", t.strip().strip(".").lower()) for t in line.split(",")]
    tags = [t if t in KEEP_HYPHEN else re.sub(r"(?<=\w)-(?=\w)", " ", t) for t in tags]
    trig = job.get("trigger") or ""
    tags = [t for t in tags if t and t != trig and len(t) <= 40]
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    if instrumental and "instrumental" not in out:
        out.insert(0, "instrumental")
    return ", ".join(out[:16])


def _fallback_tags(song: dict[str, Any], job: dict[str, Any]) -> str:
    base = (job.get("genre") or "").strip().lower()
    side = ((song.get("sidecar") or {}).get("style") or "").lower()
    pieces = [p.strip() for p in (base or side).split(",") if p.strip()]
    if song.get("instrumental"):
        pieces.insert(0, "instrumental")
    return ", ".join(pieces[:14])


def _caption_one(job_id: str, song: dict[str, Any], redo_lyrics: bool = True, cancel: threading.Event | None = None) -> None:
    job = _job(job_id)
    engine = _ctx["engine"]
    audio = _jdir(job_id) / "dataset" / song["file"]
    side = song.get("sidecar") or {}
    cache = _jdir(job_id) / "stems" / f"song_{song['n']:03d}.segments.json"
    if redo_lyrics or song.get("lyrics") is None:
        segments: list[dict[str, Any]] | None = None
        if side.get("lyrics"):
            song["lyrics"] = side["lyrics"].strip()
            said = " ".join(l for l in song["lyrics"].splitlines() if l.strip() and not re.fullmatch(r"\[[^\]]*\]", l.strip()))
            song["instrumental"] = len(said.split()) < 5       # only section tags, no words: nobody sings on it
            if song["instrumental"]:
                song["lyrics"] = "[instrumental]"
            song["source"] = "the .txt beside the song"
        else:
            if cache.exists():
                segments = _read_json(cache, None)
            if segments is None:
                if cancel and cancel.is_set():
                    return
                try:
                    tmp = _jdir(job_id) / "stems"
                    tmp.mkdir(exist_ok=True)
                    vocals, _inst = engine.separate(audio, tmp, prefix=f"song_{song['n']:03d}")
                    src = vocals
                except Exception as e:
                    LOG.warning("train: could not pull the vocals out of %s (%s); listening to the whole mix", audio.name, e)
                    src = str(audio)
                tr = engine.transcribe(src)
                segments = tr.get("segments") or []
                _write_json(cache, segments)
                for p in (_jdir(job_id) / "stems").glob(f"song_{song['n']:03d}_*.wav"):
                    p.unlink(missing_ok=True)
            song["instrumental"] = _is_instrumental(segments, float(song.get("seconds") or 0))
            song["lyrics"] = "[instrumental]" if song["instrumental"] else (structure_lyrics(segments) or "[instrumental]")
            if song["lyrics"] == "[instrumental]":
                song["instrumental"] = True
            song["source"] = "Whisper on the vocals"
    if cancel and cancel.is_set():
        return
    try:
        tags = _tags_from_llm(song, job)
    except Exception as e:
        LOG.warning("train: the lyric model could not write a style line for %s: %s", song.get("title"), e)
        tags = ""
    song["style"] = tags or _fallback_tags(song, job)
    song["state"] = "done"


def _caption_worker(job_id: str, only: list[int] | None, redo_lyrics: bool) -> None:
    cancel = _cancel.setdefault(job_id, threading.Event())
    cancel.clear()
    songs = _songs(job_id)
    todo = [s for s in songs if (int(s["n"]) in only if only else (s.get("style") is None or s.get("lyrics") is None))]
    # Whisper shares the engine with the radio's renders: with the line running, every song waited for a radio song
    # to finish (measured 4.5 min a song instead of under one). A real batch holds the line; a redo of a few does not.
    hold = len(todo) > 3
    if hold:
        _pause_queue(job_id)
    try:
        for i, s in enumerate(todo):
            if cancel.is_set():
                break
            _set(job_id, state="captioning", message=f"Listening to song {i + 1} of {len(todo)}: {s.get('title')}",
                 progress={"stage": "captioning", "step": i, "steps": len(todo)})
            s["state"] = "doing"
            try:
                _caption_one(job_id, s, redo_lyrics=redo_lyrics, cancel=cancel)
            except Exception as e:
                LOG.exception("train: captioning %s failed", s.get("title"))
                s["state"] = "error"
                s["error"] = str(e)[:200]
                if s.get("lyrics") is None:
                    s["lyrics"] = ""
                if s.get("style") is None:
                    s["style"] = ""
            _save_songs(job_id)
        done = sum(1 for s in songs if s.get("state") == "done")
        _set(job_id, state="draft", message=("Stopped." if cancel.is_set() else f"Captions written for {done} song{'s' if done != 1 else ''}. Read them, fix anything, then train."), progress={})
    finally:
        if hold:
            _resume_queue(job_id)
        with _lock:
            if job_id in _caption_job:
                _caption_job.remove(job_id)
            _threads.pop(job_id + ":cap", None)


@router.post("/jobs/{job_id}/captions/write")
def api_captions_write(job_id: str, body: dict[str, Any] | None = None):
    st = _job(job_id)
    body = body or {}
    if st["state"] in RUNNING_STATES:
        raise HTTPException(409, "it is training right now")
    if not _songs(job_id):
        raise HTTPException(400, "add some songs first")
    with _lock:
        if _caption_job:
            raise HTTPException(409, "captions are already being written" + ("" if _caption_job[0] == job_id else " for another job"))
        _caption_job.append(job_id)
    only = [int(x) for x in (body.get("only") or [])] or None
    redo = bool(body.get("again", False)) or bool(only)
    if body.get("all"):
        for s in _songs(job_id):
            s["style"] = None
            s["lyrics"] = None
        only, redo = None, True
        _save_songs(job_id)
    t = threading.Thread(target=_caption_worker, args=(job_id, only, redo), daemon=True, name=f"tf-caption-{job_id}")
    _threads[job_id + ":cap"] = t
    t.start()
    return {"ok": True}


@router.post("/jobs/{job_id}/captions/stop")
def api_captions_stop(job_id: str):
    ev = _cancel.get(job_id)
    if ev:
        ev.set()
    return {"ok": True}


@router.put("/jobs/{job_id}/songs/{n}")
def api_edit_song(job_id: str, n: int, body: dict[str, Any]):
    st = _job(job_id)
    if st["state"] in RUNNING_STATES:
        raise HTTPException(409, "it is training right now")
    s = next((x for x in _songs(job_id) if int(x["n"]) == n), None)
    if not s:
        raise HTTPException(404, "no such song")
    if "lyrics" in body:
        s["lyrics"] = str(body["lyrics"] or "").strip()
        s["instrumental"] = s["lyrics"].lower().replace(" ", "") == "[instrumental]"
    if "style" in body:
        s["style"] = _clean_tags(str(body["style"] or ""), st, instrumental=bool(s.get("instrumental")))
    if "title" in body:
        s["title"] = str(body["title"] or "")[:120]
    if s.get("lyrics") is not None and s.get("style") is not None:
        s["state"] = "done"
    s["edited"] = True
    _save_songs(job_id)
    return s


@router.post("/jobs/{job_id}/songs/{n}/again")
def api_song_again(job_id: str, n: int):
    """Write one song's captions again (listen again, ask the lyric model again)."""
    st = _job(job_id)
    if st["state"] in RUNNING_STATES:
        raise HTTPException(409, "it is training right now")
    s = next((x for x in _songs(job_id) if int(x["n"]) == n), None)
    if not s:
        raise HTTPException(404, "no such song")
    with _lock:
        if _caption_job:
            raise HTTPException(409, "captions are already being written")
        _caption_job.append(job_id)
    try:
        _caption_one(job_id, s, redo_lyrics=bool(s.get("lyrics") is None or not (s.get("sidecar") or {}).get("lyrics")))
    except Exception as e:
        raise HTTPException(500, f"that did not work: {e}")
    finally:
        with _lock:
            if job_id in _caption_job:
                _caption_job.remove(job_id)
    _save_songs(job_id)
    return s


def write_caption_files(job_id: str) -> int:
    """song_NNN.txt beside each song_NNN.mp3: line 1 is the trigger and the style, then [Lyrics], then the lyrics."""
    st = _job(job_id)
    d = _jdir(job_id) / "dataset"
    n = 0
    for s in _songs(job_id):
        lyr = (s.get("lyrics") or "").strip() or "[instrumental]"
        style = (s.get("style") or "").strip()
        head = ", ".join(x for x in (st["trigger"], style) if x)
        (d / f"song_{int(s['n']):03d}.txt").write_text(f"{head}\n[Lyrics]\n{lyr}\n", encoding="utf-8")
        n += 1
    return n


# =========================================================================== the plan and the config
def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def plan_for(settings: dict[str, Any], n_songs: int) -> dict[str, Any]:
    """Everything the run will use, from the two knobs or the Advanced fields."""
    s = settings or {}
    adv = bool(s.get("advanced"))
    length = s.get("length") if s.get("length") in LENGTHS else "medium"
    strength = s.get("strength") if s.get("strength") in STRENGTHS else "balanced"
    passes = LENGTHS[length]
    rank, lr = STRENGTHS[strength]
    steps = int(max(200, min(1200, passes * max(1, n_songs))))
    plan = {"advanced": adv, "length": length, "strength": strength, "passes": passes, "halves": "sound", "rank": rank, "lr": lr, "steps": steps,
            "save_every": 100, "train_window_frames": 1500, "cot": "full", "abc_dropout": 0.5, "do_separation": False, "head": "v9", "grad_accum": 1, "ema": False}
    if adv:
        plan["halves"] = "both" if s.get("halves") == "both" else "sound"
        plan["rank"] = int(_clamp(s.get("rank"), 8, 64, rank))
        plan["lr"] = _clamp(s.get("lr"), 2e-5, 2e-4, lr)
        plan["steps"] = int(_clamp(s.get("steps"), 100, 2000, steps))
        plan["save_every"] = int(_clamp(s.get("save_every"), 50, 250, 100))
        # 60 s per step fits when only the sound is trained; with the planner too it spills out of a 24 GB card
        # (about 80 s a step instead of 3), so that half defaults to 30 s
        plan["train_window_frames"] = int(_clamp(s.get("train_window_frames"), 750, 1500, 750 if plan["halves"] == "both" else 1500))
        plan["cot"] = s.get("cot") if s.get("cot") in ("off", "melody", "full") else "full"
        plan["abc_dropout"] = round(_clamp(s.get("abc_dropout"), 0.0, 1.0, 0.5), 2)
        plan["do_separation"] = bool(s.get("do_separation"))
        plan["head"] = s.get("head") if s.get("head") in HEADS else "v9"
        plan["grad_accum"] = int(_clamp(s.get("grad_accum"), 1, 4, 1))
        plan["ema"] = bool(s.get("ema"))
    if s.get("_smoke_steps"):                 # a short test run (never set by the page)
        plan["steps"] = int(s["_smoke_steps"])
        plan["save_every"] = int(s.get("_smoke_save") or 10)
    plan["save_every"] = min(plan["save_every"], plan["steps"])
    return plan


def _yaml(obj: Any, indent: int = 0) -> str:
    """A tiny YAML writer: decimals never in exponent form (PyYAML reads 5e-05 as a string), strings always quoted."""
    pad = "  " * indent
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{k}:")
                out.append(_yaml(v, indent + 1))
            else:
                out.append(f"{pad}{k}: {_scalar(v)}")
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, dict) and v:
                first = True
                for k, vv in v.items():
                    prefix = f"{pad}- " if first else f"{pad}  "
                    first = False
                    if isinstance(vv, (dict, list)) and vv:
                        out.append(f"{prefix}{k}:")
                        out.append(_yaml(vv, indent + 2))
                    else:
                        out.append(f"{prefix}{k}: {_scalar(vv)}")
            elif isinstance(v, (dict, list)) and v:
                out.append(f"{pad}-")
                out.append(_yaml(v, indent + 1))
            else:
                out.append(f"{pad}- {_scalar(v)}")
    return "\n".join(out)


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = f"{v:.10f}".rstrip("0")
        return s + "0" if s.endswith(".") else s
    if isinstance(v, dict):
        return "{}"
    if isinstance(v, list):
        return "[]"
    return json.dumps(str(v), ensure_ascii=False)


def build_config(job_id: str) -> dict[str, Any]:
    st = _job(job_id)
    n = len(_songs(job_id))
    p = plan_for(st.get("settings") or {}, n)
    d = _jdir(job_id)
    both = p["halves"] == "both"
    rank = p["rank"]
    lr = p["lr"]
    model_kwargs: dict[str, Any] = {
        "ar_cuda_graphs": False,
        "semantic_head_path": str(HEADS[p["head"]]).replace("\\", "/"),
        "cot": p["cot"], "abc_dropout": p["abc_dropout"], "train_window_frames": p["train_window_frames"],
        "do_separation": p["do_separation"], "empty_cache_every_step": True,
    }
    network: dict[str, Any] = {"type": "lora", "linear": rank, "linear_alpha": rank}
    if both:
        model_kwargs.update({"ar_loss_weight": 1.0, "ar_lr_multiplier": 0.4, "ar_kl_weight": 0.2, "ar_max_tokens": 4000})
    else:
        network["network_kwargs"] = {"ignore_if_contains": SOUND_ONLY_SKIP}
        model_kwargs.update({"ar_loss_weight": 0.0, "ar_kl_weight": 0.0})
    train: dict[str, Any] = {
        "batch_size": 1, "steps": p["steps"], "gradient_accumulation": p["grad_accum"], "train_unet": True, "train_text_encoder": False,
        "gradient_checkpointing": True, "noise_scheduler": "flowmatch", "timestep_type": "sigmoid", "optimizer": "adamw8bit", "lr": lr,
        "dtype": "bf16", "disable_sampling": True, "skip_first_sample": True, "unload_text_encoder": False,
        "ema_config": {"use_ema": bool(p["ema"]), "ema_decay": 0.99},
    }
    name = st.get("slug") or "my-style"
    cfg = {
        "job": "extension",
        "config": {
            "name": name,
            "process": [{
                "type": "sd_trainer",
                "training_folder": str(d / "out").replace("\\", "/"),
                "device": "cuda:0",
                "network": network,
                "save": {"dtype": "float16", "save_every": p["save_every"], "max_step_saves_to_keep": 20},
                "datasets": [{"folder_path": str(d / "dataset").replace("\\", "/"), "caption_ext": "txt", "caption_dropout_rate": 0,
                              "cache_latents_to_disk": True, "resolution": [512]}],
                "train": train,
                "model": {"name_or_path": str(CHECKPOINT).replace("\\", "/"), "arch": "yue2", "quantize": True, "qtype": "convrot8",
                          "model_kwargs": model_kwargs},
                "sample": {"sampler": "flowmatch", "sample_every": 1000000, "width": 512, "height": 512, "prompts": [], "neg": "",
                           "seed": 42, "walk_seed": False, "guidance_scale": 1.0, "sample_steps": 32},
            }],
        },
        "meta": {"name": "[name]", "version": "1.0"},
    }
    return cfg


def write_config(job_id: str) -> Path:
    path = _jdir(job_id) / "config.yaml"
    path.write_text(_yaml(build_config(job_id)) + "\n", encoding="utf-8")
    return path


# =========================================================================== running it
def _active_job() -> str | None:
    with _lock:
        for jid, st in _jobs.items():
            if st.get("state") in RUNNING_STATES:
                return jid
    return None


def _pause_queue(job_id: str) -> None:
    db = _ctx["db"]
    st = _job(job_id)
    if not _ctx["queue_paused"]():
        db.set_setting("queue_paused", True)
        st["paused_by_us"] = True
    else:
        st["paused_by_us"] = False
    _save(job_id)


def _resume_queue(job_id: str) -> None:
    st = _job(job_id)
    if st.get("paused_by_us"):
        _ctx["db"].set_setting("queue_paused", False)
        st["paused_by_us"] = False
        _save(job_id)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HOME": str(HF_HOME),
                "MODELS_PATH": str(MODELS), "TF_FFMPEG_BIN": str(TRAINER / "ffmpeg" / "bin"), "HF_HUB_DISABLE_TELEMETRY": "1", "NO_ALBUMENTATIONS_UPDATE": "1", "DISABLE_TELEMETRY": "YES"})
    for k in ("TF_PORT", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME"):
        env.pop(k, None)
    return env


def _kill_pid(pid: int) -> None:
    """The exact process tree this module started, never anything found by name."""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True, timeout=30)
    except Exception:
        LOG.exception("train: taskkill %s failed", pid)


def _pid_alive(pid: int, created: float | None) -> bool:
    try:
        import psutil
        p = psutil.Process(int(pid))
        if created and abs(p.create_time() - float(created)) > 5:
            return False                     # the PID was reused by something else
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


@router.post("/jobs/{job_id}/start")
def api_start(job_id: str, body: dict[str, Any] | None = None):
    st = _job(job_id)
    body = body or {}
    r = readiness()
    if not r["ready"]:
        raise HTTPException(409, "the trainer is not installed: " + "; ".join(r["problems"]))
    if st["state"] in RUNNING_STATES or job_id in _caption_job:
        raise HTTPException(409, "it is already running")
    other = _active_job()
    if other and other != job_id:
        raise HTTPException(409, "another training run is going; one at a time")
    if _caption_job:
        raise HTTPException(409, "captions are being written; wait for that to finish")
    songs = _songs(job_id)
    if not songs:
        raise HTTPException(400, "add some songs first")
    missing = [s for s in songs if s.get("lyrics") is None or s.get("style") is None]
    if missing:
        raise HTTPException(400, f"{len(missing)} song{'s have' if len(missing) != 1 else ' has'} no captions yet: press Write the captions")
    if isinstance(body.get("settings"), dict):
        st["settings"] = body["settings"]
    if body.get("smoke_steps"):                 # a short test run of the trainer itself
        st["settings"] = {**(st.get("settings") or {}), "_smoke_steps": int(body["smoke_steps"]), "_smoke_save": int(body.get("smoke_save") or 10)}
    write_caption_files(job_id)
    write_config(job_id)
    with _lock:
        st.update(state="waiting", message="Starting...", progress={"stage": "waiting", "step": 0, "steps": plan_for(st.get("settings") or {}, len(songs))["steps"]},
                  started=_now(), finished=None, error=None, pid=None, exit_code=None)
        # a fresh run starts its log and its output folder from nothing
        shutil.rmtree(_jdir(job_id) / "out", ignore_errors=True)
        shutil.rmtree(_jdir(job_id) / "dataset" / "_latent_cache", ignore_errors=True)
        (_jdir(job_id) / "train.log").write_text("", encoding="utf-8")
        _save(job_id)
    ev = _cancel.setdefault(job_id, threading.Event())
    ev.clear()
    t = threading.Thread(target=_run, args=(job_id,), daemon=True, name=f"tf-train-{job_id}")
    _threads[job_id] = t
    t.start()
    return _public(job_id, full=True)


@router.post("/jobs/{job_id}/cancel")
def api_cancel(job_id: str):
    st = _job(job_id)
    ev = _cancel.setdefault(job_id, threading.Event())
    ev.set()
    live = _live.get(job_id) or {}
    pid = live.get("pid") or st.get("pid")
    if st["state"] in RUNNING_STATES and pid and _pid_alive(pid, st.get("pid_created")):
        _kill_pid(pid)
    return {"ok": True}


def _wait_for_card(job_id: str, cancel: threading.Event) -> bool:
    """Pause the line, let the song being made finish, release YuE2. False when cancelled."""
    ctx = _ctx
    worker, engine, llm = ctx["worker"], ctx["engine"], ctx["llm"]
    _pause_queue(job_id)
    shown = False
    while True:
        if cancel.is_set():
            return False
        cur = worker.current
        if cur is None and not engine.busy:
            break
        if not shown or int(time.time()) % 15 == 0:
            _set(job_id, state="waiting", message="Waiting for the song being made to finish", progress={**(_job(job_id).get("progress") or {}), "stage": "waiting"})
            shown = True
        time.sleep(2)
    _set(job_id, state="freeing", message="Clearing the graphics card")
    try:
        engine.unload()
    except Exception:
        LOG.exception("train: engine.unload failed")
    try:
        llm.unload_model()
    except Exception:
        pass
    plan = plan_for(_job(job_id).get("settings") or {}, len(_songs(job_id)))
    need = NEED_FREE_MB[plan["halves"]]
    try:
        engine.comfy_free_if_idle(need_mb=need)      # ComfyUI, if it is idle, lets go of its models (the radio does the same)
    except Exception:
        pass
    waited = 0
    while True:
        free = _gpu_free_mb()
        if free is None or free >= need:
            return True
        if cancel.is_set():
            return False
        if waited >= 90:
            raise RuntimeError(f"Something else is using the graphics card: only {free / 1024:.1f} GB is free and training needs about {need / 1024:.0f} GB. Close the other program and try again.")
        _set(job_id, message=f"Waiting for the graphics card ({free / 1024:.1f} GB free, need {need / 1024:.0f} GB)")
        time.sleep(3)
        waited += 3


def _run(job_id: str) -> None:
    cancel = _cancel[job_id]
    engine, engine_mod = _ctx["engine"], _ctx["engine_mod"]
    try:
        if not _wait_for_card(job_id, cancel):
            _finish(job_id, "cancelled", "Cancelled before it started.")
            return
        with engine.hold("training a LoRA"):
            with engine_mod.gpu_lease("tenforward"):
                _spawn_and_watch(job_id, cancel)
    except Exception as e:
        LOG.exception("train: %s failed", job_id)
        _finish(job_id, "failed", str(e)[:400], error=str(e))


def _spawn_and_watch(job_id: str, cancel: threading.Event) -> None:
    cfg = write_config(job_id)
    d = _jdir(job_id)
    _set(job_id, state="starting", message="Starting the trainer")
    logf = (d / "train.log").open("ab")
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    proc = subprocess.Popen([str(PY), "run.py", str(cfg)], cwd=str(AITK), env=_env(), stdout=logf, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=flags)
    created = None
    try:
        import psutil
        created = psutil.Process(proc.pid).create_time()
    except Exception:
        pass
    _live[job_id] = {"pid": proc.pid, "proc": proc}
    _set(job_id, pid=proc.pid, pid_created=created)
    LOG.info("train: %s started, pid %s", job_id, proc.pid)
    try:
        _tail(job_id, proc.pid, cancel, proc)
    finally:
        try:
            logf.close()
        except Exception:
            pass
    code = proc.poll()
    _after_exit(job_id, code, cancel)


# ---- reading the log
_BAR = re.compile(r"(\d+)/(\d+) \[[^<\]]*<([^,\]]+),")
_LOSS = re.compile(r"loss:\s*([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)")
_DESC = re.compile(r"^\s*([^|]*?)\s*:?\s*\d+%\|")


def _eta_s(text: str) -> float | None:
    text = (text or "").strip()
    if not re.fullmatch(r"\d+(?::\d+){1,2}", text):
        return None
    parts = [int(x) for x in text.split(":")]
    s = 0
    for x in parts:
        s = s * 60 + x
    return float(s)


class _Parser:
    def __init__(self, job_id: str):
        self.job_id = job_id
        st = _job(job_id)
        self.steps = int((st.get("progress") or {}).get("steps") or 0)
        self.name = st.get("slug") or ""
        self.stage = "starting"
        self.losses: collections.deque = collections.deque(maxlen=20)
        self.points: list[list[float]] = []
        self.last_tail: collections.deque = collections.deque(maxlen=40)
        self.cache_done = 0
        self.cache_total = 0
        self.step = 0
        self.eta: float | None = None
        self.started_training: float | None = None
        self.last_save = 0.0
        self.error_line = ""

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if not _BAR.search(line):
            self.last_tail.append(line[:300])
            low = line.lower()
            if "traceback" in low or re.search(r"(error|exception)\b", low) and "warn" not in low:
                self.error_line = line[:300]
            if "num_samples=0" in low or "failed to load or were unusable" in low:
                self.error_line = "None of the songs could be read. Open Show details to see why."
            if "out of memory" in low:
                self.error_line = "The graphics card ran out of memory. Pick a shorter training window or fewer, shorter songs, then try again."
            if self.stage in ("starting",) and ("loading yue2" in low or "loading" in low and "model" in low):
                self.stage = "loading"
            return
        m = _BAR.search(line)
        n, total, eta = int(m.group(1)), int(m.group(2)), m.group(3)
        desc = (_DESC.match(line).group(1) if _DESC.match(line) else "").lower()
        if "load" in desc:                      # "Loading weights" bars: the models are being read in, not the songs
            if self.stage != "training":
                self.stage = "loading"
            return
        if "cach" in desc or "latent" in desc or "encod" in desc or "embed" in desc:
            self.stage = "caching"
            self.cache_done, self.cache_total = n, total
            self.eta = _eta_s(eta)
            return
        if "separat" in desc or "sheet" in desc or "token" in desc:
            self.stage = "caching"
            self.cache_done, self.cache_total = n, total
            self.eta = _eta_s(eta)
            return
        # anything else with a bar the size of the run is the training bar
        if total == self.steps or (self.name and self.name.lower() in desc) or "train" in desc:
            self.stage = "training"
            self.step = n
            if self.steps and total != self.steps:
                self.steps = total
            self.eta = _eta_s(eta)
            lm = _LOSS.search(line)
            if lm:
                try:
                    v = float(lm.group(1))
                    self.losses.append(v)
                    self.points.append([n, v])
                    if len(self.points) > 400:
                        self.points = self.points[::2]
                except ValueError:
                    pass
        else:
            # an unlabelled bar: treat as caching until training has begun
            if self.stage != "training":
                self.stage = "caching"
                self.cache_done, self.cache_total = n, total
                self.eta = _eta_s(eta)

    def progress(self) -> dict[str, Any]:
        avg = sum(self.losses) / len(self.losses) if self.losses else None
        return {"stage": self.stage, "step": self.step, "steps": self.steps, "eta_s": self.eta, "loss": avg,
                "loss_points": self.points[-400:], "cache_done": self.cache_done, "cache_total": self.cache_total}

    def message(self) -> str:
        if self.stage == "caching":
            return f"Caching: getting your songs ready ({self.cache_done}/{self.cache_total})" if self.cache_total else "Caching: getting your songs ready"
        if self.stage == "training":
            return f"Training: step {self.step} of {self.steps}"
        if self.stage == "loading":
            return "Loading the model (this takes a few minutes every time)"
        return "Starting the trainer"


def _tail(job_id: str, pid: int, cancel: threading.Event, proc: subprocess.Popen | None = None) -> None:
    """Follow train.log until the process is gone, turning tqdm lines into progress."""
    path = _jdir(job_id) / "train.log"
    parser = _Parser(job_id)
    pos = 0
    buf = b""
    last_write = 0.0
    last_pause = 0.0
    created = _job(job_id).get("pid_created")
    while True:
        alive = (proc.poll() is None) if proc is not None else _pid_alive(pid, created)
        try:
            with path.open("rb") as f:
                f.seek(pos)
                chunk = f.read()
                pos += len(chunk)
        except FileNotFoundError:
            chunk = b""
        if chunk:
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                parser.feed(raw.decode("utf-8", "replace"))
        if cancel.is_set() and alive:
            _kill_pid(pid)
        now = time.time()
        if alive and now - last_pause > 5:
            last_pause = now
            try:
                if not _ctx["queue_paused"]():       # somebody pressed Resume on the Queue tab: the card is not free, so hold the line again
                    _ctx["db"].set_setting("queue_paused", True)
                    _job(job_id)["paused_by_us"] = True     # ours to let go of when the run ends
            except Exception:
                pass
        if chunk or now - last_write > 5:
            if now - last_write >= 1.5 or not alive:
                with _lock:
                    st = _job(job_id)
                    st["state"] = {"caching": "caching", "training": "training"}.get(parser.stage, "starting")
                    st["message"] = parser.message()
                    st["progress"] = parser.progress()
                    st["tail"] = list(parser.last_tail)[-8:]
                    st["error_line"] = parser.error_line
                    _save(job_id)
                last_write = now
        if not alive:
            if buf:
                parser.feed(buf.decode("utf-8", "replace"))
            break
        time.sleep(0.8)
    with _lock:
        st = _job(job_id)
        st["progress"] = parser.progress()
        st["error_line"] = parser.error_line
        st["tail"] = list(parser.last_tail)[-8:]
        _save(job_id)


def _after_exit(job_id: str, code: int | None, cancel: threading.Event) -> None:
    st = _job(job_id)
    cps = _checkpoints(job_id)
    if cancel.is_set():
        _finish(job_id, "cancelled", f"Stopped. {len(cps)} checkpoint{'s' if len(cps) != 1 else ''} saved so far." if cps else "Stopped.", code=code)
    elif code == 0:
        _finish(job_id, "done", f"Finished: {len(cps)} checkpoints. Try them and keep the best one.", code=code)
    else:
        reason = st.get("error_line") or (st.get("tail") or [""])[-1] or f"the trainer stopped with code {code}"
        _finish(job_id, "failed", f"The trainer stopped: {reason}"[:400], code=code, error="\n".join(st.get("tail") or []))


def _finish(job_id: str, state: str, message: str, code: int | None = None, error: str | None = None) -> None:
    with _lock:
        st = _job(job_id)
        st.update(state=state, message=message, finished=_now(), exit_code=code, pid=None)
        if error:
            st["error"] = error[-2000:]
        _save(job_id)
    try:
        _resume_queue(job_id)
    except Exception:
        LOG.exception("train: could not start the line again")
    _live.pop(job_id, None)
    LOG.info("train: %s %s: %s", job_id, state, message)


def _recover() -> None:
    """After a server restart: a run whose trainer is still going is picked up again from its log; the rest are marked."""
    for d in DATA.glob("tr_*"):
        st = _read_json(d / "status.json", None)
        if not st:
            continue
        jid = d.name
        _jobs[jid] = st
        state = st.get("state")
        if state == "captioning":
            st["state"], st["message"] = "draft", "Captioning was interrupted by a restart. Press Write the captions to carry on."
            _save(jid)
        elif state in RUNNING_STATES:
            pid, created = st.get("pid"), st.get("pid_created")
            if pid and _pid_alive(pid, created):
                ev = _cancel.setdefault(jid, threading.Event())
                _live[jid] = {"pid": pid}
                LOG.info("train: %s is still training (pid %s); watching it again", jid, pid)

                def watch(jid=jid, pid=pid, ev=ev):
                    try:
                        _tail(jid, pid, ev, None)
                        _after_exit(jid, 0 if _checkpoints(jid) and not _job(jid).get("error_line") else 1, ev)
                    except Exception:
                        LOG.exception("train: lost track of %s", jid)
                t = threading.Thread(target=watch, daemon=True, name=f"tf-train-{jid}")
                _threads[jid] = t
                t.start()
            else:
                st["state"], st["message"] = "interrupted", "It was stopped by a restart before it finished. Checkpoints saved so far are below."
                _save(jid)
                try:
                    _resume_queue(jid)
                except Exception:
                    pass


@router.get("/jobs/{job_id}/log")
def api_log(job_id: str, tail: int = 80):
    _job(job_id)
    p = _jdir(job_id) / "train.log"
    if not p.exists():
        return {"lines": []}
    data = p.read_bytes()[-60000:].decode("utf-8", "replace")
    lines = [l for l in re.split(r"[\r\n]+", data) if l.strip()]
    return {"lines": lines[-max(1, min(tail, 400)):]}


# =========================================================================== checkpoints
def _checkpoints(job_id: str) -> list[dict[str, Any]]:
    st = _job(job_id)
    out_dir = _jdir(job_id) / "out" / (st.get("slug") or "")
    rows = []
    if not out_dir.exists():
        return rows
    final = None
    for p in out_dir.glob("*.safetensors"):
        m = re.search(r"_(\d{6,9})\.safetensors$", p.name)
        if m:
            rows.append({"step": int(m.group(1)), "file": p.name, "mb": round(p.stat().st_size / 1e6, 1), "saved": p.stat().st_mtime})
        else:
            final = p
    steps_done = {r["step"] for r in rows}
    if final is not None:
        # the end-of-run save: same weights as the last numbered one when the step count lines up
        total = int((st.get("progress") or {}).get("steps") or 0)
        if total and total not in steps_done:
            rows.append({"step": total, "file": final.name, "mb": round(final.stat().st_size / 1e6, 1), "saved": final.stat().st_mtime, "final": True})
    rows.sort(key=lambda r: r["step"])
    kept = st.get("kept") or {}
    tries = {t["step"]: t for t in st.get("tries") or []}
    for r in rows:
        r["kept_as"] = kept.get(str(r["step"]))
        t = tries.get(r["step"])
        r["try"] = {"job_id": t.get("job_id")} if t else None
        r["tested_song"] = (st.get("tested") or {}).get(str(r["step"]))
    return rows


def _checkpoint_path(job_id: str, step: int) -> Path:
    for r in _checkpoints(job_id):
        if r["step"] == step:
            return _jdir(job_id) / "out" / (_job(job_id).get("slug") or "") / r["file"]
    raise HTTPException(404, "no such checkpoint")


def _converter():
    try:
        import lora_convert  # type: ignore
        return lora_convert
    except ImportError:
        raise HTTPException(501, "The converter that turns a checkpoint into a Ten Forward LoRA (lora_convert.py) is not installed yet.")


def _lora_dir() -> Path:
    return Path(_ctx["loras"].LORA_DIR)


def _unregister_lora(files: list[str]) -> None:
    """Remove throw-away try-it LoRAs: the files and their rows in loras.json."""
    meta_path = _lora_dir() / "loras.json"
    for f in files:
        (_lora_dir() / f).unlink(missing_ok=True)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return
    changed = False
    for f in files:
        if f in meta:
            meta.pop(f)
            changed = True
    if changed:
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def _cleanup_tries(job_id: str, force: bool = False) -> None:
    """A try-it LoRA is deleted once the test song has been made (or has failed), or when the job goes."""
    try:
        st = _job(job_id)
    except HTTPException:
        return
    db = _ctx["db"]
    keep, drop = [], []
    for t in st.get("tries") or []:
        j = db.get("jobs", t.get("job_id") or "") if t.get("job_id") else None
        finished = j is None or j.get("status") in ("done", "failed", "cancelled")
        if finished or force:
            drop.extend(t.get("files") or [])
            if j and j.get("song_id"):
                st.setdefault("tested", {})[str(t.get("step"))] = j["song_id"]
        else:
            keep.append(t)
    if drop:
        _unregister_lora(drop)
    if drop or len(keep) != len(st.get("tries") or []):
        with _lock:
            st["tries"] = keep
            _save(job_id)


@router.get("/jobs/{job_id}/checkpoints")
def api_checkpoints(job_id: str):
    _cleanup_tries(job_id)
    return _checkpoints(job_id)


@router.post("/jobs/{job_id}/checkpoints/{step}/try")
def api_try(job_id: str, step: int, body: dict[str, Any] | None = None):
    """Make a short test song with this checkpoint. The LoRA is a throw-away copy, removed once the song is made."""
    body = body or {}
    st = _job(job_id)
    src = _checkpoint_path(job_id, step)
    conv = _converter()
    _cleanup_tries(job_id)
    songs = _songs(job_id)
    tag = f"try-{job_id[-6:]}-{step}"
    scale = float(body.get("scale") or 1.0)
    res = conv.convert_file(src, tag, trigger=st["trigger"], default_scale=scale, source=f"test copy of {job_id} step {step}", blurb="Throw-away test copy; removed after the test song is made.")
    files = list(res.get("files") or [f"{tag}.safetensors"])
    main = files[0]
    plan = plan_for(st.get("settings") or {}, len(songs))
    # the style of the first captioned song, with the trigger first: the same words the model was taught with
    first = next((s for s in songs if s.get("style")), None)
    style = body.get("style") or ", ".join(x for x in (st["trigger"], (first or {}).get("style") or st.get("genre") or "") if x)
    vocal = not all(s.get("instrumental") for s in songs)
    lyrics = body.get("lyrics") or (TRY_LYRICS if vocal else "[Intro]\n[Verse]\n[Chorus]\n[Outro]")
    params = {"title": f"Try {st['name']} step {step}", "lyrics": lyrics, "style": style, "mode": 0, "duration": int(body.get("duration") or 45),
              "steps": 32, "loras": {main: scale}, "source": "studio", "tags": ["lora test"], "instrumental": not vocal}
    job = _ctx["enqueue"]("song", params, priority=5)
    with _lock:
        st.setdefault("tries", [])
        st["tries"] = [t for t in st["tries"] if t.get("step") != step] + [{"step": step, "job_id": job["id"], "files": files, "lora": main, "at": _now()}]
        _save(job_id)
    waiting = "It will start when the training stops and the line is running again." if st["state"] in RUNNING_STATES else "It is in the line now."
    return {"job_id": job["id"], "lora": main, "message": f"A short test song is queued. {waiting}"}


@router.post("/jobs/{job_id}/checkpoints/{step}/keep")
def api_keep(job_id: str, step: int, body: dict[str, Any] | None = None):
    body = body or {}
    st = _job(job_id)
    src = _checkpoint_path(job_id, step)
    conv = _converter()
    name = _slug(str(body.get("name") or st["name"]), "my-style")
    taken = (_lora_dir() / f"{name}.safetensors").exists()
    if taken and not body.get("replace") and str((st.get("kept") or {}).get(str(step)) or "") != name:
        raise HTTPException(409, f"There is already a LoRA called {name}. Pick another name, or tick replace.")
    blurb = (f"Trained here on {len(_songs(job_id))} songs. Its word \"{st['trigger']}\" goes in front of the style by itself whenever it is on."
             + (" Sound only: it changes how songs sound, not how they are written." if plan_for(st.get("settings") or {}, 0)["halves"] == "sound" else " Planner and sound."))
    res = conv.convert_file(src, name, trigger=st["trigger"], default_scale=float(body.get("scale") or 1.0), source=f"trained here ({job_id}, step {step})", blurb=blurb)
    with _lock:
        st.setdefault("kept", {})[str(step)] = name
        _save(job_id)
    _ctx["loras"].scan()
    return {"ok": True, "name": name, "files": res.get("files"), "kind": res.get("kind"), "trigger": st["trigger"]}
