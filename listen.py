"""listen.py — the public radio: a dial anybody can listen to, and nothing else.

Everything under /listen/ is open (no sign-in) and locked down: it lists the channels, hands out the next song,
streams or downloads a finished song, and counts a play. It cannot make, change, heart, skip, ban or delete
anything, cannot see the owner's play queue, settings, jobs, voices or lyric files, and never serves the private
channels (Favorites, My Songs). A channel somebody is listening to here counts as tuned in, exactly as it does
on the owner's own dial, so the radio keeps that channel stocked; when a channel has nothing at all to play,
one song is asked for, at most once every ten minutes.

To put it on the internet, a web server proxies ONE path to it and nothing else, for example nginx:

    location /10fwd/ { proxy_pass http://<ten forward>:8410/listen/; proxy_buffering off; }

Every link the page makes is relative, so it works at any prefix.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

import radio

LOG = logging.getLogger("tenforward.listen")
router = APIRouter(prefix="/listen")

ASK_EVERY_S = 10 * 60          # how often an empty channel may be asked for a song from here
HIDDEN = {"favorites", radio.MY_SONGS_STATION}
MEDIA_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac"}

_ctx: dict[str, Any] = {}


def setup(*, db, library: Path, static: Path, enqueue: Callable, power_on: Callable, cfg: Callable, version: str) -> None:
    """Wired by server.py once the database and the job line exist (this module never imports server)."""
    _ctx.update(db=db, library=library, static=static / "listen", enqueue=enqueue, power_on=power_on, cfg=cfg, version=version)


def _db():
    return _ctx["db"]


# --------------------------------------------------------------------------- what a listener may see
def public_station(st: dict[str, Any]) -> dict[str, Any]:
    nu = st.get("next_up") or None
    # the name only: a channel's description is its sound prompt, the owner's own writing, not a listener's business
    return {"id": st["id"], "name": st.get("name"), "color": st.get("color"),
            "instrumental": bool(st.get("instrumental")), "ready": int(st.get("ready") or 0),
            "cooking": {"title": nu.get("title"), "status": nu.get("status"), "progress": nu.get("progress") or 0} if nu else None}


def listenable(st: dict[str, Any] | None) -> bool:
    return bool(st) and bool(st.get("enabled", 1)) and not radio.is_favorites(st) and st["id"] not in HIDDEN


def best_file(song: dict[str, Any]) -> Path | None:
    files = song.get("files") or {}
    for key in ("mp3", "voiced", "master"):
        if files.get(key):
            p = _ctx["library"] / song["id"] / files[key]
            if p.exists():
                return p
    return None


def public_song(song: dict[str, Any]) -> dict[str, Any]:
    c = song.get("cover_of") if isinstance(song.get("cover_of"), dict) else None
    # the title and who it covers, nothing more: not the sound prompt, not the words
    return {"id": song["id"], "title": song.get("title"), "duration_s": song.get("duration_s"), "station_id": song.get("station_id"),
            "cover_of": {"track": c.get("track"), "artist": c.get("artist"), "kind": c.get("kind")} if c else None,
            "url": f"media/{song['id']}", "download": f"download/{song['id']}"}


def _song(song_id: str) -> dict[str, Any]:
    s = _db().get("songs", song_id)
    if not s or s.get("status") != "ready" or s.get("banned"):
        raise HTTPException(404, "no such song")
    st = _db().get("stations", s.get("station_id") or "")
    if not listenable(st):
        raise HTTPException(404, "no such song")
    return s


# --------------------------------------------------------------------------- the page
@router.get("")
def index_redirect():
    return RedirectResponse("listen/", status_code=301)


@router.get("/")
def index():
    return FileResponse(str(_ctx["static"] / "index.html"), media_type="text/html")


@router.get("/{name}.{ext}")
def asset(name: str, ext: str):
    if ext not in ("js", "css", "png") or not re.fullmatch(r"[a-z0-9_-]+", name):
        raise HTTPException(404)
    p = _ctx["static"] / f"{name}.{ext}"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), media_type={"js": "application/javascript", "css": "text/css", "png": "image/png"}[ext])


# --------------------------------------------------------------------------- the api
@router.get("/api/hello")
def hello():
    return {"app": "Ten Forward", "version": _ctx["version"], "tagline": str(_ctx["cfg"]("header_tagline") or ""),
            "power": bool(_ctx["power_on"]()), "time": time.time()}


@router.get("/api/stations")
def stations():
    return [public_station(st) for st in radio.stations_status(_db()) if listenable(st)]


@router.get("/api/next")
def next_song(station: str, exclude: str = ""):
    """The next song on a channel. Tunes the channel in (so the radio keeps it stocked) and never touches the
    owner's own play queue. With nothing to play, one song is asked for, at most once every ten minutes."""
    db = _db()
    st = db.get("stations", station)
    if not listenable(st):
        raise HTTPException(404, "no such channel")
    db.set_setting(f"station_last_tuned:{station}", time.time())
    skip = [e for e in exclude.split(",") if e][:20]
    song = radio.next_song(db, station, skip) or (radio.next_song(db, station, []) if skip else None)
    pending = radio.queued_count(db, station)
    asked = False
    if song is None and not pending and _ctx["power_on"]() and db.setting("radio_autofill", True) and st.get("auto_generate", 1):
        last = float(db.setting(f"listen_asked:{station}", 0) or 0)
        if time.time() - last > ASK_EVERY_S:
            db.set_setting(f"listen_asked:{station}", time.time())
            try:
                _ctx["enqueue"]("song", radio.plan_song_for_station(db, st), priority=3)
                asked, pending = True, 1
                LOG.info("listen: %s had nothing to play; asked for one", station)
            except Exception:
                LOG.warning("listen: could not ask for a song on %s", station, exc_info=True)
    return {"song": public_song(song) if song else None, "station": public_station({**st, "next_up": radio.station_next_up(db, station)}),
            "pending_jobs": pending, "asked": asked, "power": bool(_ctx["power_on"]())}


@router.post("/api/played")
def played(body: dict[str, Any], request: Request):
    s = _song(str((body or {}).get("id") or ""))
    _db().mark_played(s["id"])
    _db().note_play(s.get("station_id"))
    return {"ok": True}


@router.api_route("/media/{song_id}", methods=["GET", "HEAD"])
def media(song_id: str):
    s = _song(song_id)
    p = best_file(s)
    if not p:
        raise HTTPException(404, "file missing")
    return FileResponse(str(p), media_type=MEDIA_TYPES.get(p.suffix.lower(), "application/octet-stream"),
                        headers={"Cache-Control": "private, max-age=3600"})


@router.api_route("/download/{song_id}", methods=["GET", "HEAD"])
def download(song_id: str):
    s = _song(song_id)
    p = best_file(s)
    if not p:
        raise HTTPException(404, "file missing")
    name = re.sub(r"[^A-Za-z0-9 _()'-]+", "", s.get("title") or song_id).strip() or song_id
    return FileResponse(str(p), media_type=MEDIA_TYPES.get(p.suffix.lower(), "application/octet-stream"),
                        filename=f"{name}{p.suffix}")
