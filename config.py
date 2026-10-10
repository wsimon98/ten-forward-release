"""
config.py — every knob in one place, with three layers:

    DB settings row (what the Settings tab writes)  >  environment variable  >  built-in default

Nothing here imports server.py, radio.py or llm.py at module level: those import this. `set_cfg` calls the
hooks it needs lazily (llm.configure, cache invalidation) so the import graph stays a tree.

A fresh install has no DB rows at all: everything falls back to the defaults below, which is why Ten Forward
starts and runs out of the box on a machine nobody has set up yet.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Callable

TF_ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("tenforward.config")

_TRUE = ("1", "true", "yes", "on", "y")
_FALSE = ("0", "false", "no", "off", "n")


def _lan_guess() -> str:
    """http://<this machine's LAN address>:<port> — what a phone on the same wifi types."""
    port = os.environ.get("TF_PORT", "8410")
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        try:
            s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the default route
            ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        ip = ""
    if not ip:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = "127.0.0.1"
    try:
        if ipaddress.ip_address(ip).is_loopback:
            ip = "127.0.0.1"
    except Exception:
        pass
    return f"http://{ip}:{port}"


# key: (env var, default, type, label, help, group)
# type: str | int | float | bool | path | text
DEFAULTS: dict[str, tuple[str, Any, str, str, str, str]] = {
    # ---- songs and keeping
    "songs_dir": ("TF_SONGS_DIR", str(TF_ROOT / "songs"), "path", "Favourites folder",
                  "Hearted songs are copied here as <Channel>/<Title>.mp3 next to a .txt with the lyrics.", "songs"),
    "export_liked": ("TF_EXPORT_LIKED", True, "bool", "Copy hearted songs out as files",
                     "Off keeps them in the app only (they are still never deleted).", "songs"),
    "retention_days": ("TF_RADIO_RETENTION_DAYS", 7.0, "float", "Keep unhearted radio songs for (days)",
                       "0 = never delete anything. A channel can override this in its own settings.", "songs"),
    "banned_retention_h": ("TF_BANNED_RETENTION_H", 24.0, "float", "Keep 'never again' songs for (hours)",
                           "A banned song is held this long so it is not planned again straight away.", "songs"),
    # ---- radio
    "yield_to_asks": ("TF_YIELD_TO_ASKS", True, "bool", "Let what you ask for interrupt a background song",
                      "When another app sharing the card (through the GPU butler) asks for it, the card is handed over straight away "
                      "instead of after the song being made finishes - which can be two or three minutes. The song goes "
                      "back in the line and is made as soon as the card is free. A song YOU asked for is never "
                      "interrupted, and no song is pushed aside more than twice in a row.", "radio"),
    "retry_clipped": ("TF_RETRY_CLIPPED", True, "bool", "Make a clipped song again with more room",
                      "When the engine runs out of room before a song has finished, its last seconds are faded rather "
                      "than ended. With this on, that song is made once more with more room. It costs one extra render.",
                      "radio"),
    "steps_default": ("TF_STEPS", 32, "int", "Sampling steps per song",
                      "How hard the engine works on the sound of each song. The last stage of a render is directly "
                      "proportional to this, so 24 takes about a quarter less time than 32. A channel can set its own. "
                      "Every song ever made here used 32, so there is nothing to compare against yet - change one channel "
                      "and listen before changing them all.", "radio"),
    "planners": ("TF_PLANNERS", 3, "int", "Channels planned at once",
                 "How many channels can be having their next song written at the same time. The words are written one "
                 "at a time whatever this says (the language model has its own lock), but an instrumental channel needs "
                 "no words at all, and one channel waiting no longer holds up the rest.", "radio"),
    "fleet_alert_url": ("TF_FLEET_ALERT_URL", "", "str", "Fleet alert board",
                        "Where to raise an alert when a channel stops making songs, e.g. http://<alert board>:9310. "
                        "Empty means only the log and this app's own screens are told.", "radio"),
    "buffer_minutes": ("TF_BUFFER_MINUTES", 45.0, "float", "Fresh audio kept ready per channel (minutes)",
                       "What the radio tries to have waiting on every channel, measured in minutes of music rather than "
                       "a number of songs - minutes are what runs out. The card renders about 1.6 times faster than a song "
                       "plays, so a deep buffer costs disk, not listening. Set a channel's Keep ahead to 0 to stop it entirely.",
                       "radio"),
    "keep_unplayed_days": ("TF_KEEP_UNPLAYED_DAYS", 90.0, "float", "Keep a song nobody has played for this long",
                           "A song the radio made and nobody has heard is not deleted on age - the clock starts when you "
                           "play it. This is only a backstop so a channel you never tune to cannot fill the disk. 0 = keep forever.",
                           "radio"),
    "keep_ahead_default": ("TF_KEEP_AHEAD", 5, "int", "Songs kept ready per channel",
                           "Used by new channels and by any channel that has no number of its own.", "radio"),
    "radio_queue_cap": ("TF_RADIO_QUEUE_CAP", 24, "int", "Planned songs waiting to be made",
                        "The radio stops planning ahead past this, so the queue stays steerable.", "radio"),
    "place_chance": ("TF_PLACE_CHANCE", 0.12, "float", "Local place names (share of sung songs)",
                     "0 = never name your town. A channel can override this.", "radio"),
    "default_duration_s": ("TF_DEFAULT_DURATION", 300, "int", "Song length cap for new channels (seconds)", "", "radio"),
    "skip_retire": ("TF_SKIP_RETIRE", 3, "int", "Early skips that retire a song",
                    "Skipping a song in its first seconds ON THE RADIO is a vote against it. This many and it is retired, "
                    "exactly as if you had pressed Never again. 0 = never retire anything. A song you have hearted is "
                    "never retired however often it is skipped.", "radio"),
    "dial_order": ("TF_DIAL_ORDER", "played", "choice", "Order of the channels on the dial",
                   "played = what you listen to most rises to the top, and slides back down over a week or so if you stop. "
                   "fixed = the order set on each channel. A channel made in the last two days sits near the top either way.",
                   "radio"),
    # ---- engine
    "idle_unload_min": ("TF_IDLE_UNLOAD_MIN", 20.0, "float", "Release the GPU after idle (minutes)",
                        "0 = keep the music model loaded for ever.", "engine"),
    # ---- lyric writer
    "llm_url": ("TF_LLM_URL", "http://127.0.0.1:11434/v1", "str", "Lyric writer URL",
                "An Ollama (or any OpenAI-compatible) server. Ollama's own address is http://127.0.0.1:11434/v1.", "llm"),
    "llm_models": ("TF_LLM_MODELS", "qwen2.5:14b", "str", "Lyric writer model(s)",
                   "Comma separated, first one wins. qwen2.5:14b on a 24 GB card, qwen2.5:7b on a smaller one.", "llm"),
    "llm_num_ctx": ("TF_LLM_NUM_CTX", 4096, "int", "Context size", "", "llm"),
    "llm_keep_alive": ("TF_LLM_KEEP_ALIVE", "10m", "str", "Keep the model loaded for",
                       "The writer shares the GPU with the music model, so it lets go after a while.", "llm"),
    "llm_timeout_s": ("TF_LLM_TIMEOUT", 180, "int", "Timeout (seconds)", "", "llm"),
    "llm_vram_mb": ("TF_LLM_VRAM_MB", 10500, "int", "VRAM the writer needs (MB)",
                    "The planner waits for this much free memory before asking for lyrics.", "llm"),
    "llm_fallback_url": ("TF_LLM_FALLBACK_URL", "", "str", "Fallback writer URL",
                         "A second machine's Ollama to write lyrics when this one does not answer, e.g. "
                         "http://<other computer>:11434. Without it, a hiccup on the local writer stops every channel that "
                         "sings - the fallback to a channel's own lyric files only works while it still has unused ones. It is "
                         "only used while its model is already loaded there, exactly as it is running, so it never makes that "
                         "machine reload anything. Empty (the default) turns it off.",
                         "llm"),
    "llm_fallback_model": ("TF_LLM_FALLBACK_MODEL", "", "str", "Fallback writer model",
                           "The model on that machine, as Ollama names it.", "llm"),
    # ---- lyric memory (0.9.3): the radio watching its own writing habits
    "lyric_memory": ("TF_LYRIC_MEMORY", "normal", "choice", "Lyric variety memory",
                     "How hard the radio watches what it has been writing. Off writes exactly the way it did before.", "lyrics"),
    "lyric_window": ("TF_LYRIC_WINDOW", 25, "int", "Songs it remembers per channel",
                     "Counted in songs, not days: a busy channel writes seventy in a day.", "lyrics"),
    "worn_out_words": ("TF_WORN_OUT_WORDS", "hoodie, fridge, blinds, receipt, echo*, whisper*, shadow*, flicker*, fade*, ghost, silence, memory, the air, empty, clock tick*, tapestry", "str", "Words the radio has worn out",
                       "Comma separated. End a word with * to catch every form of it (fade* = fade, fades, faded, fading); "
                       "a plain word catches its plurals. A word here is never given to the writer as a song's object, is always on its "
                       "list of things to avoid, and if one still turns up in a finished song those lines are sent back "
                       "to be rewritten. When you notice a channel leaning on a word, add it here - no prompt editing.",
                       "lyrics"),
    "mood_example_share": ("TF_MOOD_EXAMPLE_SHARE", 0.35, "float", "Songs that get one of their channel's example objects",
                           "A channel's mood can list example objects (a streetlight through the blinds, the fridge light). "
                           "Handing one to every song guarantees each a turn every few songs, which is how they become a "
                           "channel's signature. At 0.35 about one song in three gets a named object and the rest pick their "
                           "own. 1.0 is the old behaviour.", "lyrics"),
    "classic_writer_channels": ("TF_CLASSIC_WRITER_CHANNELS", "", "str", "Channels that keep the older idea writer",
                                "Channel ids, comma separated. Since 1.3 every song idea is a story where something happens, "
                                "with one suggested turn of events per song, and ideas that are only a picture of somebody "
                                "standing or watching are sent back. A channel listed here keeps writing exactly the way it "
                                "did before that, including its titles.", "lyrics"),
    "lyric_retry": ("TF_LYRIC_RETRY", True, "bool", "Rewrite the repeating lines once",
                    "One extra pass over the repeating lines only, and never when the channel is running out of songs.", "lyrics"),
    "lyric_station_memory": ("TF_LYRIC_STATION_MEMORY", True, "bool", "Remember each channel on its own",
                             "Off pools the whole dial, so a word worn out on one channel quiets it everywhere.", "lyrics"),
    "lrclib_enabled": ("TF_LRCLIB", False, "bool", "Learn from real songs (lrclib.net)",
                       "Measures real songs in the background and keeps the NUMBERS, not the words. Needs the internet.", "lyrics"),
    "lrclib_corpus": ("TF_LRCLIB_CORPUS", 500, "int", "Real songs measured per lane", "", "lyrics"),
    "lrclib_keep_text": ("TF_LRCLIB_KEEP_TEXT", False, "bool", "Keep the real words too",
                         "Off throws the lyric text away once it has been measured. On is what covers need.", "lyrics"),
    "cover_chance": ("TF_COVER_CHANCE", 0.0, "float", "Covers (share of songs)",
                     "0 = never, 1 = every song it can find one for. A channel can set its own. Needs the two settings above.", "lyrics"),
    # ---- this server
    "lan_url": ("TF_LAN_URL", "", "str", "Address on your own network",
                "What a phone on the same wifi types, and what you put in the Android app. Blank = worked out from this machine.", "server"),
    "https_url": ("TF_PUBLIC_HTTPS_URL", "", "str", "Address from anywhere (Tailscale)",
                  "Your tailnet https address, if you use one. The microphone only works on https.", "server"),
    "header_tagline": ("TF_HEADER_TAGLINE", "MUSIC LOUNGE · DECK 10", "str", "Line under the title", "", "server"),
    "links": ("TF_LINKS", "", "str", "Your own links",
              "Shown at the bottom of Settings. Name|address, separated by commas: Router|http://192.168.1.1, Files|http://nas", "server"),
    # ---- features
    "voices_enabled": ("TF_VOICES", True, "bool", "Voice features",
                       "Record or upload a voice, train it, and re-sing songs in it. Needs a restart.", "features"),
    "personal_enabled": ("TF_PERSONAL", False, "bool", "My Songs channel",
                         "The private channel for your own finished songs and your own lyric files. Needs a restart.", "features"),
    # ---- who is listening
    "require_login": ("TF_REQUIRE_LOGIN", False, "bool", "Always ask who is listening",
                      "Off: when there is one person with no password, they are signed in automatically.", "users"),
}

GROUPS = [
    ("songs", "Songs and keeping"),
    ("radio", "Radio"),
    ("llm", "Lyric writer"),
    ("lyrics", "Lyric memory"),
    ("engine", "Engine"),
    ("users", "Who is listening"),
    ("server", "Addresses"),
    ("features", "Features"),
]

RESTART_KEYS = {"voices_enabled", "personal_enabled"}

# settings that are one of a short list of words
CHOICES: dict[str, list[str]] = {"lyric_memory": ["off", "low", "normal", "high"], "dial_order": ["played", "fixed"]}

_cache: dict[str, Any] = {}
_cache_at = 0.0
_CACHE_S = 5.0
_hooks: dict[str, Callable[[Any], None]] = {}


def _db():
    from db import get_db
    return get_db()


def _coerce(kind: str, value: Any, default: Any) -> Any:
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            if s in _TRUE:
                return True
            if s in _FALSE:
                return False
            return bool(default)
        if kind == "int":
            return int(float(value))
        if kind == "float":
            return float(value)
        return str(value)
    except Exception:
        return default


def _raw_db_settings() -> dict[str, Any]:
    global _cache, _cache_at
    if time.time() - _cache_at < _CACHE_S and _cache:
        return _cache
    out: dict[str, Any] = {}
    try:
        db = _db()
        with db._lock:  # noqa: SLF001 — one cheap read of the whole table, the DB class has no "all settings" helper
            rows = db._conn.execute("SELECT key, value FROM settings").fetchall()
        import json as _json
        for r in rows:
            if r[0] in DEFAULTS:
                try:
                    out[r[0]] = _json.loads(r[1])
                except Exception:
                    out[r[0]] = r[1]
    except Exception:
        out = {}
    _cache, _cache_at = out, time.time()
    return out


def invalidate() -> None:
    global _cache_at
    _cache_at = 0.0


def default_for(key: str) -> Any:
    env, dflt, kind, *_ = DEFAULTS[key]
    if key == "lan_url" and not dflt:
        return _lan_guess()
    return dflt


def cfg(key: str) -> Any:
    """The live value of a setting: DB row, else environment, else default."""
    if key not in DEFAULTS:
        raise KeyError(f"unknown setting {key!r}")
    env, dflt, kind, *_ = DEFAULTS[key]
    dflt = default_for(key)
    row = _raw_db_settings().get(key)
    if row is not None and row != "":
        return _coerce(kind, row, dflt)
    if env and os.environ.get(env) not in (None, ""):
        return _coerce(kind, os.environ[env], dflt)
    return _coerce(kind, dflt, dflt)


def source_of(key: str) -> str:
    env = DEFAULTS[key][0]
    if _raw_db_settings().get(key) not in (None, ""):
        return "db"
    if env and os.environ.get(env) not in (None, ""):
        return "env"
    return "default"


def path_cfg(key: str) -> Path:
    p = Path(str(cfg(key)))
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        LOG.warning("settings: cannot create %s (%s)", key, p)
    return p


# --------------------------------------------------------------------------- writing
CLAMPS: dict[str, tuple[float, float]] = {
    "retention_days": (0, 3650), "banned_retention_h": (0, 8760), "keep_ahead_default": (1, 30), "keep_unplayed_days": (0, 3650), "buffer_minutes": (0, 1440), "planners": (1, 8), "steps_default": (4, 64), "mood_example_share": (0, 1),
    "radio_queue_cap": (1, 20), "place_chance": (0, 1), "default_duration_s": (30, 600), "skip_retire": (0, 20),
    "idle_unload_min": (0, 600), "llm_num_ctx": (1024, 32768), "llm_timeout_s": (10, 1800), "llm_vram_mb": (0, 80000),
    "lyric_window": (5, 200), "lrclib_corpus": (50, 5000), "cover_chance": (0, 1),
}


def check_writable(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    probe = folder / ".tenforward_write_test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)


def set_cfg(key: str, value: Any) -> Any:
    """Validate, store in the DB and tell whoever caches it. Raises ValueError with a readable message."""
    if key not in DEFAULTS:
        raise ValueError(f"there is no setting called {key}")
    env, dflt, kind, label, _help, _group = DEFAULTS[key]
    dflt = default_for(key)
    if kind == "path":
        folder = Path(str(value).strip().strip('"'))
        if not str(folder):
            raise ValueError(f"{label}: needs a folder")
        try:
            check_writable(folder)
        except Exception as e:
            raise ValueError(f"{label}: cannot write to {folder} ({e})")
        value = str(folder)
    elif kind == "bool":
        value = _coerce("bool", value, dflt)
    elif kind in ("int", "float"):
        value = _coerce(kind, value, dflt)
        lo, hi = CLAMPS.get(key, (None, None))
        if lo is not None:
            value = max(lo, min(hi, value))
            value = int(value) if kind == "int" else float(value)
    elif kind == "choice":
        value = str(value).strip().lower()
        if value not in CHOICES.get(key, []):
            raise ValueError(f"{label}: pick one of {', '.join(CHOICES.get(key, []))}")
    else:
        value = str(value).strip()
        if key in ("llm_url", "llm_fallback_url", "lan_url", "https_url") and value and not value.startswith(("http://", "https://")):
            raise ValueError(f"{label}: an address starts with http:// or https://")
        value = value.rstrip("/") if key in ("llm_url", "llm_fallback_url", "lan_url", "https_url") else value
    _db().set_setting(key, value)
    invalidate()
    _after_change(key)
    return value


def reset_cfg(key: str) -> Any:
    if key not in DEFAULTS:
        raise ValueError(f"there is no setting called {key}")
    _db().delete("settings", key)
    invalidate()
    _after_change(key)
    return cfg(key)


def _after_change(key: str) -> None:
    try:
        if key.startswith("llm_"):
            import llm
            llm.configure(url=cfg("llm_url"), models=cfg("llm_models"), num_ctx=cfg("llm_num_ctx"),
                          keep_alive=cfg("llm_keep_alive"), timeout=cfg("llm_timeout_s"),
                          fallback_url=cfg("llm_fallback_url"), fallback_model=cfg("llm_fallback_model"))
        if key == "songs_dir":
            path_cfg("songs_dir")
    except Exception:
        LOG.exception("settings: hook for %s failed", key)


def apply_llm() -> None:
    """Push the stored lyric-writer settings into llm.py (called at startup)."""
    _after_change("llm_")


def describe(include_secrets: bool = True) -> list[dict[str, Any]]:
    """Every setting with its value, default, where it came from and how to show it."""
    out = []
    for key, (env, dflt, kind, label, help_, group) in DEFAULTS.items():
        out.append({"key": key, "label": label, "help": help_, "group": group, "type": kind,
                    "value": cfg(key), "default": default_for(key), "env": env, "source": source_of(key),
                    "restart": key in RESTART_KEYS, "choices": CHOICES.get(key)})
    return out


def as_dict() -> dict[str, Any]:
    return {k: cfg(k) for k in DEFAULTS}
