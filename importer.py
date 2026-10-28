"""importer.py — bring your own material into Ten Forward.

Two kinds of import, both landing on the "My Songs" station:
  * lyric notes: a Google Keep export (one big text file, songs separated by blank lines) becomes one lyric file per
    song under lyrics/my-songs, in the section-tag style YuE2 sings from ([Intro] [Verse 1] [Chorus] ...);
  * finished songs: mp3/wav/m4a files from a folder (the NAS) or an upload become ready library songs (source
    "import", never auto-deleted), transcoded to mp3, with their lyrics attached when a lyric file with a matching
    title exists.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import audio_utils
import radio
from db import DB, new_id

LOG = logging.getLogger("tenforward.importer")

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".wma", ".aiff", ".aif", ".mp4"}
STATION = "my-songs"
FOLDER = "my-songs"

# --------------------------------------------------------------------------- lyric text normalising
_SECTION_WORDS = {
    "intro": "Intro", "verse": "Verse", "chorus": "Chorus", "pre-chorus": "Pre-Chorus", "post-chorus": "Post-Chorus",
    "bridge": "Bridge", "outro": "Outro", "hook": "Hook", "refrain": "Refrain", "interlude": "Interlude", "breakdown": "Breakdown",
    "final chorus": "Final Chorus", "instrumental": "Instrumental", "instrumental break": "Instrumental Break", "solo": "Solo",
    "tag": "Tag", "final tag": "Final Tag", "chorus tag": "Chorus Tag", "drop": "Drop", "chant": "Chant",
}
_SECTION_RE = re.compile(
    r"^\s*(?:\d+\.\s*)?[\[(]?\s*\**\s*(?P<name>intro|verse|chorus|pre[- ]?chorus|post[- ]?chorus|bridge|outro|hook|refrain|interlude|breakdown|final chorus|instrumental(?: break)?|solo|chorus tag|final tag|tag|drop|chant)"
    r"\s*(?P<num>\d+)?\s*(?:\((?:\d+:\d+)[^)]*\))?\s*\**\s*[\])]?\s*:?\s*$",
    re.I,
)
_TITLE_JUNK = re.compile(r"\s*(\((?:\d+|alt|redo|male version|female version|vocal swap|instrumental|dad version|second version|wilmashl)[^)]*\)|\balt\b|_)\s*", re.I)


def normalize_section_line(line: str) -> str | None:
    """'(Verse 1)', '2. CHORUS (0:50 – 1:30)', '[INTRO (0:00 – 0:15)]' -> '[Verse 1]', '[Chorus]', '[Intro]'. None if not a section line."""
    m = _SECTION_RE.match(line)
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group("name").lower())
    name = re.sub(r"pre[- ]?chorus", "pre-chorus", name)
    name = re.sub(r"post[- ]?chorus", "post-chorus", name)
    label = _SECTION_WORDS.get(name, name.title())
    if m.group("num"):
        label += " " + m.group("num")
    return f"[{label}]"


def tidy_lyrics(text: str) -> str:
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip().replace("**", "")
        s = line.strip()
        if not s:
            out.append("")
            continue
        if re.match(r"^(lyrics\s*&\s*arrangement|song details|sound design|arrangement\s*&\s*structure)\s*:?\s*$", s, re.I):
            continue
        sec = normalize_section_line(s)
        if sec:
            out.append(sec)
            continue
        out.append(s)
    text = "\n".join(out).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    # a section tag directly followed by a blank line reads better without the gap
    text = re.sub(r"(\[[^\]\n]+\])\n\n(?=\S)", r"\1\n", text)
    return text


def clean_title(title: str) -> str:
    t = title.strip().strip('"').strip("*").strip()
    t = re.sub(r"[\\/:*?\"<>|]+", "", t).strip().rstrip(".").strip()
    return t[:80]


def norm_key(title: str) -> str:
    """Loose key for matching an audio file name to a lyric title: lowercase, no punctuation, version junk removed."""
    t = _TITLE_JUNK.sub(" ", title.lower())
    t = t.replace("’", "'").replace("`", "'")
    t = re.sub(r"\b(the|a)\s+", "", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\b(\d+(\.\d+)?)\b$", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def first_lyric_line(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("[") and not s.startswith("("):
            return re.sub(r"[\\/:*?\"<>|]", "", s).strip(" ,.;:!?")[:60]
    return ""


# --------------------------------------------------------------------------- Google Keep export
_NEW_SONG_TAG = re.compile(r"^\s*(?:\d+\.\s*)?[\[(]\s*\**\s*(intro|verse\s*1\b|lyrics)", re.I)


def split_keep_export(text: str) -> list[dict[str, str]]:
    """Songs are separated by 4+ blank lines; the first line is the note title. A block that opens with a bracket
    tag is a fragment: a new untitled song when it starts with [Intro]/[Verse 1], else the tail of the previous song."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    parts = [p.strip("\n") for p in re.split(r"\n{4,}", text) if p.strip()]
    songs: list[dict[str, str]] = []
    for p in parts:
        lines = p.split("\n")
        first = lines[0].strip()
        if first.startswith("[") or first.startswith("("):
            if songs and not _NEW_SONG_TAG.match(first):
                songs[-1]["lyrics"] = (songs[-1]["lyrics"] + "\n\n" + tidy_lyrics(p)).strip()
                continue
            title = first_lyric_line(p) or f"Untitled {len(songs) + 1}"
            body = p
        else:
            title = first
            body = "\n".join(lines[1:])
            # Keep sometimes repeats the title as the first body line
            rest = [l for l in body.split("\n") if l.strip()]
            if rest and norm_key(rest[0]) == norm_key(title):
                body = body.replace(rest[0], "", 1)
        songs.append({"title": clean_title(title), "lyrics": tidy_lyrics(body)})
    return [s for s in songs if s["lyrics"]]


def import_keep_export(db: DB, path: Path, folder: str = FOLDER) -> dict[str, Any]:
    """Write every song of a Keep export into lyrics/<folder>/<Title>.txt. Skips songs whose text is already there."""
    songs = split_keep_export(path.read_text(encoding="utf-8-sig", errors="replace"))
    target = radio.LYRICS_ROOT / folder
    target.mkdir(parents=True, exist_ok=True)
    have = {radio.lyrics_text_hash(radio.read_lyrics_file(p)[1]) for p in target.iterdir() if p.suffix.lower() in (".txt", ".md")}
    written, skipped, titles = 0, 0, []
    for s in songs:
        h = radio.lyrics_text_hash(s["lyrics"])
        if h in have:
            skipped += 1
            continue
        have.add(h)
        radio.save_lyrics_file(folder, s["title"], s["lyrics"], {"source": "google keep export", "imported": time.strftime("%Y-%m-%d")})
        written += 1
        titles.append(s["title"])
    LOG.info("keep import: %d songs in the export, %d written to lyrics/%s, %d already there", len(songs), written, folder, skipped)
    return {"found": len(songs), "written": written, "skipped": skipped, "titles": titles}


# --------------------------------------------------------------------------- lyric matching for audio files
def lyric_index(folder: str = FOLDER) -> list[tuple[str, Path, str]]:
    """[(norm_key(title), path, title)] for every lyric file in a folder."""
    d = radio.LYRICS_ROOT / folder
    out = []
    if d.exists():
        for p in sorted(d.iterdir()):
            if p.suffix.lower() in (".txt", ".md") and p.is_file():
                title, _ = radio.read_lyrics_file(p)
                out.append((norm_key(title), p, title))
    return out


def match_lyrics(title: str, index: list[tuple[str, Path, str]]) -> tuple[Path, str, float] | None:
    """Best lyric file for an audio title: exact key, then prefix, then fuzzy (ratio >= 0.82)."""
    key = norm_key(title)
    if not key:
        return None
    for k, p, t in index:
        if k == key:
            return p, t, 1.0
    for k, p, t in index:
        if (k.startswith(key) or key.startswith(k)) and min(len(k), len(key)) >= 6:
            return p, t, 0.9
    best = None
    for k, p, t in index:
        r = difflib.SequenceMatcher(None, k, key).ratio()
        if r >= 0.82 and (best is None or r > best[2]):
            best = (p, t, r)
    return best


# --------------------------------------------------------------------------- audio import
def file_sha1(path: Path, limit: int = 1 << 24) -> str:
    """sha1 of the first 16 MB + size: enough to recognise the same file again, cheap on a NAS."""
    h = hashlib.sha1()
    with path.open("rb") as f:
        h.update(f.read(limit))
    h.update(str(path.stat().st_size).encode())
    return h.hexdigest()


def already_imported(db: DB, src: Path) -> dict[str, Any] | None:
    rows = db.query("songs", "source='import' AND origin_path=?", (str(src),), limit=1)
    if rows:
        return rows[0]
    rows = db.query("songs", "source='import' AND origin_hash=?", (file_sha1(src),), limit=1)
    return rows[0] if rows else None


def title_from_filename(path: Path) -> str:
    t = path.stem
    t = re.sub(r"\s*\((\d+)\)\s*$", r" (take \1)", t)  # 'Back Road (2)' -> 'Back Road (take 2)'
    t = t.replace("_", " ").strip()
    return clean_title(t) or path.stem


def import_audio(db: DB, library: Path, src: Path, station_id: str = STATION, title: str | None = None, lyrics: str | None = None,
                 album: str | None = None, lyric_folder: str = FOLDER, index: list | None = None) -> dict[str, Any]:
    """Copy (mp3) or transcode (anything else) one audio file into the library as a ready song on `station_id`."""
    src = Path(src)
    if src.suffix.lower() not in AUDIO_EXT:
        raise ValueError(f"not an audio file: {src.name}")
    dup = already_imported(db, src)
    if dup:
        return {**dup, "_duplicate": True}
    title = clean_title(title or title_from_filename(src))
    song_id = new_id("song_")
    d = library / song_id
    d.mkdir(parents=True, exist_ok=True)
    dst = d / "song.mp3"
    if src.suffix.lower() == ".mp3":
        shutil.copy2(src, dst)
    else:
        audio_utils.to_mp3(src, dst, bitrate="192k")
    duration = audio_utils.duration_seconds(dst)
    lyrics_hash = None
    lyric_path = None
    matched_title = None
    if not lyrics:
        index = index if index is not None else lyric_index(lyric_folder)
        m = match_lyrics(title, index)
        if m:
            lyric_path, matched_title, _score = m
            _t, lyrics = radio.read_lyrics_file(lyric_path)
            lyrics = radio._strip_meta(lyrics)
            lyrics_hash = radio.file_hash(lyric_path)
    st = db.get("stations", station_id)
    style = f"imported · {album}" if album else "imported song"
    song = {
        "id": song_id, "title": title, "style": style, "lyrics": lyrics or "", "mode": 0, "duration_s": round(duration, 1), "seed": None, "steps": None, "cfg": None,
        "voice_id": None, "loras": {}, "station_id": station_id if st else None, "source": "import", "parent_id": None, "files": {"mp3": dst.name}, "abc": None,
        "tags": ["import"] + ([album] if album else []), "created": time.time(), "status": "ready", "topic": "your own recording", "theme": None, "singer": None,
        "lyrics_hash": lyrics_hash, "album": album, "origin_path": str(src), "origin_hash": file_sha1(src),
    }
    db.insert("songs", song)
    (d / "meta.json").write_text(json.dumps({**song, "lyrics_file": str(lyric_path) if lyric_path else None, "matched_lyric_title": matched_title}, indent=2, default=str), encoding="utf-8")
    if lyrics_hash and db.get("lyrics_used", lyrics_hash) is None:
        # the lyric file now has a finished recording: the radio will not render it again on its own ("Render this" in the lyric manager still can)
        db.insert("lyrics_used", {"hash": lyrics_hash, "station_id": station_id, "path": str(lyric_path), "song_id": song_id, "used_at": time.time()})
    LOG.info("imported '%s' (%.0fs) from %s%s", title, duration, src, f" · lyrics: {matched_title}" if matched_title else "")
    return song


def scan_audio(root: Path, recursive: bool = True) -> list[Path]:
    root = Path(root)
    if root.is_file():
        return [root] if root.suffix.lower() in AUDIO_EXT else []
    it = root.rglob("*") if recursive else root.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in AUDIO_EXT and not p.name.startswith("."))


# --------------------------------------------------------------------------- background folder import with progress
_PROGRESS: dict[str, Any] = {"running": False}
_LOCK = threading.Lock()


def progress() -> dict[str, Any]:
    return dict(_PROGRESS)


def import_folder_async(db: DB, library: Path, root: Path, station_id: str = STATION, recursive: bool = True, album_from_subfolder: bool = True) -> dict[str, Any]:
    """Import every audio file under `root` in a background thread; poll progress() for status."""
    with _LOCK:
        if _PROGRESS.get("running"):
            raise RuntimeError("an import is already running")
        files = scan_audio(root, recursive)
        _PROGRESS.clear()
        _PROGRESS.update({"running": True, "root": str(root), "total": len(files), "done": 0, "imported": 0, "duplicates": 0, "failed": 0,
                          "matched_lyrics": 0, "current": "", "started": time.time(), "errors": [], "titles": []})

    def work():
        index = lyric_index(FOLDER)
        for f in files:
            _PROGRESS["current"] = f.name
            try:
                album = f.parent.name if (album_from_subfolder and f.parent != Path(root)) else None
                song = import_audio(db, library, f, station_id=station_id, album=album, index=index)
                if song.get("_duplicate"):
                    _PROGRESS["duplicates"] += 1
                else:
                    _PROGRESS["imported"] += 1
                    _PROGRESS["titles"].append(song["title"])
                    if song.get("lyrics_hash"):
                        _PROGRESS["matched_lyrics"] += 1
            except Exception as e:
                LOG.exception("import failed for %s", f)
                _PROGRESS["failed"] += 1
                _PROGRESS["errors"].append(f"{f.name}: {e}"[:200])
            _PROGRESS["done"] += 1
        _PROGRESS["running"] = False
        _PROGRESS["current"] = ""
        _PROGRESS["finished"] = time.time()
        LOG.info("folder import done: %s -> %d imported, %d duplicates, %d failed", root, _PROGRESS["imported"], _PROGRESS["duplicates"], _PROGRESS["failed"])

    threading.Thread(target=work, daemon=True, name="tf-import").start()
    return progress()
