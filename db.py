"""db.py — SQLite store for Ten Forward (songs, jobs, voices, stations, lyrics usage, settings)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

TF_ROOT = Path(__file__).resolve().parent
DB_PATH = TF_ROOT / "data" / "tenforward.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
  id TEXT PRIMARY KEY, title TEXT, style TEXT, lyrics TEXT, mode INTEGER DEFAULT 0,
  duration_s REAL DEFAULT 0, seed INTEGER, steps INTEGER, cfg REAL, voice_id TEXT, loras TEXT,
  station_id TEXT, source TEXT, parent_id TEXT, files TEXT, abc TEXT, tags TEXT,
  liked INTEGER DEFAULT 0, plays INTEGER DEFAULT 0, last_played REAL, created REAL, status TEXT DEFAULT 'ready'
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, type TEXT, status TEXT, params TEXT, progress REAL DEFAULT 0, phase TEXT, message TEXT,
  song_id TEXT, created REAL, started REAL, finished REAL, error TEXT, priority INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS voices (
  id TEXT PRIMARY KEY, name TEXT, file TEXT, source TEXT, duration_s REAL, created REAL, notes TEXT
);
CREATE TABLE IF NOT EXISTS stations (
  id TEXT PRIMARY KEY, name TEXT, description TEXT, style_prompts TEXT, lyrics_folder TEXT, voice_id TEXT,
  loras TEXT, mode INTEGER DEFAULT 0, duration_s INTEGER DEFAULT 180, auto_generate INTEGER DEFAULT 1,
  keep_ahead INTEGER DEFAULT 2, color TEXT, created REAL, enabled INTEGER DEFAULT 1, lyric_policy TEXT DEFAULT 'mixed',
  instrumental INTEGER DEFAULT 0, sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lyrics_used (
  hash TEXT PRIMARY KEY, station_id TEXT, path TEXT, song_id TEXT, used_at REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, name TEXT UNIQUE, password_hash TEXT, salt TEXT, role TEXT DEFAULT 'user', created REAL, last_seen REAL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, user_id TEXT, device TEXT, created REAL, last_seen REAL
);
CREATE TABLE IF NOT EXISTS lyric_features (
  song_id TEXT PRIMARY KEY, station_id TEXT, created REAL, features TEXT, novelty REAL, guidance TEXT, version INTEGER
);
CREATE TABLE IF NOT EXISTS lrclib_cache (
  id INTEGER PRIMARY KEY, track TEXT, artist TEXT, album TEXT, duration REAL, instrumental INTEGER DEFAULT 0,
  lang TEXT, text_hash TEXT UNIQUE, plain TEXT, lane TEXT, fetched REAL, features TEXT
);
CREATE TABLE IF NOT EXISTS corpus_profile (lane TEXT PRIMARY KEY, n_songs INTEGER, profile TEXT, built REAL);
CREATE TABLE IF NOT EXISTS station_plays (station_id TEXT PRIMARY KEY, score REAL DEFAULT 0, plays INTEGER DEFAULT 0, last REAL);
CREATE TABLE IF NOT EXISTS arrangements (
  id TEXT PRIMARY KEY, name TEXT, source TEXT, source_hash TEXT, song_id TEXT, abc TEXT, abc_melody TEXT, info TEXT, words TEXT,
  duration_s REAL DEFAULT 0, on_radio INTEGER DEFAULT 0, used INTEGER DEFAULT 0, last_used REAL, created REAL
);
CREATE TABLE IF NOT EXISTS play_queue (
  id TEXT PRIMARY KEY, song_id TEXT, station_id TEXT, added_by TEXT, sort REAL DEFAULT 0, created REAL
);
CREATE INDEX IF NOT EXISTS idx_songs_station ON songs(station_id, created);
CREATE INDEX IF NOT EXISTS idx_lyric_features_station ON lyric_features(station_id, created);
CREATE INDEX IF NOT EXISTS idx_lrclib_lane ON lrclib_cache(lane, fetched);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_play_queue_sort ON play_queue(sort, created);
"""

JSON_FIELDS = {"songs": ("loras", "files", "tags", "cover_of"), "stations": ("style_prompts", "loras", "themes", "banned_topics", "fusions", "post_fx"), "jobs": ("params",),
               "lyric_features": ("features", "guidance"), "lrclib_cache": ("features",), "corpus_profile": ("profile",), "arrangements": ("info",)}
KEY_COLUMNS = {"lyrics_used": "hash", "settings": "key", "sessions": "token", "lyric_features": "song_id", "corpus_profile": "lane",
               "station_plays": "station_id"}

# How fast a channel's listening score fades: a week. What you played last night outranks what you played last
# month, and a channel you stop playing slides back down on its own without anything having to delete anything.
PLAY_HALF_LIFE_S = 7 * 86400


def _key(table: str) -> str:
    return KEY_COLUMNS.get(table, "id")


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


class DB:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self):
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(stations)")}
        for col, ddl in (("lyric_policy", "TEXT DEFAULT 'mixed'"), ("instrumental", "INTEGER DEFAULT 0"), ("sort", "INTEGER DEFAULT 0"),
                         ("themes", "TEXT"), ("banned_topics", "TEXT"), ("variation", "INTEGER DEFAULT 1"), ("fusions", "TEXT"), ("fusion_set", "TEXT"),
                         ("replay_policy", "TEXT DEFAULT 'fresh'"), ("retention_days", "REAL"), ("instrumental_chance", "REAL DEFAULT 0"),
                         ("mood", "TEXT"), ("explicit", "INTEGER DEFAULT 0"), ("post_fx", "TEXT"), ("male_ratio", "REAL"), ("place_chance", "REAL"), ("kind", "TEXT"),
                         ("cover_chance", "REAL"), ("steps", "INTEGER"), ("rewrite_chance", "REAL")):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE stations ADD COLUMN {col} {ddl}")
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(voices)")}
        for col, ddl in (("model", "TEXT"), ("trained", "REAL"), ("train_meta", "TEXT")):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE voices ADD COLUMN {col} {ddl}")
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        if "priority" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER DEFAULT 0")
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(songs)")}
        for col, ddl in (("topic", "TEXT"), ("theme", "TEXT"), ("singer", "TEXT"), ("banned", "INTEGER DEFAULT 0"), ("lyrics_hash", "TEXT"), ("saved_path", "TEXT"), ("liked_at", "REAL"),
                         ("album", "TEXT"), ("origin_path", "TEXT"), ("origin_hash", "TEXT"), ("cover_of", "TEXT"),
                         ("skips", "INTEGER DEFAULT 0"), ("last_skip", "REAL")):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE songs ADD COLUMN {col} {ddl}")
        self._conn.commit()

    # ------------------------------------------------------------------ generic
    def _row(self, table: str, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for f in JSON_FIELDS.get(table, ()):
            if d.get(f) is not None and isinstance(d[f], str):
                try:
                    d[f] = json.loads(d[f])
                except Exception:
                    pass
        return d

    def _prep(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        out = dict(data)
        for f in JSON_FIELDS.get(table, ()):
            if f in out and not isinstance(out[f], (str, type(None))):
                out[f] = json.dumps(out[f])
        return out

    def insert(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        data = self._prep(table, data)
        cols = ", ".join(data.keys())
        marks = ", ".join("?" for _ in data)
        with self._lock:
            self._conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", list(data.values()))
            self._conn.commit()
        return self.get(table, data[_key(table)])

    def update(self, table: str, id_: str, **fields) -> dict[str, Any] | None:
        if not fields:
            return self.get(table, id_)
        fields = self._prep(table, fields)
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE {table} SET {sets} WHERE {_key(table)}=?", [*fields.values(), id_])
            self._conn.commit()
        return self.get(table, id_)

    def get(self, table: str, id_: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(f"SELECT * FROM {table} WHERE {_key(table)}=?", (id_,)).fetchone()
        return self._row(table, row)

    def delete(self, table: str, id_: str) -> None:
        with self._lock:
            self._conn.execute(f"DELETE FROM {table} WHERE {_key(table)}=?", (id_,))
            self._conn.commit()

    def query(self, table: str, where: str = "1=1", params: tuple | list = (), order: str = "created DESC", limit: int | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {table} WHERE {where} ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(table, r) for r in rows]

    def count(self, table: str, where: str = "1=1", params: tuple | list = ()) -> int:
        with self._lock:
            return int(self._conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0])

    def total(self, table: str, column: str, where: str = "1=1", params: tuple | list = ()) -> float:
        """SUM of one column. The radio adds up minutes of unplayed audio for every channel every
        20 seconds; doing that by loading the rows would drag every set of lyrics along with it."""
        with self._lock:
            return float(self._conn.execute(f"SELECT COALESCE(SUM({column}),0) FROM {table} WHERE {where}", params).fetchone()[0])

    # ------------------------------------------------------------------ settings
    def setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            self._conn.commit()

    def settings_prefix(self, prefix: str) -> dict[str, Any]:
        """Every setting whose key starts with prefix, keyed by what follows it (station_priming:blue-hour -> blue-hour)."""
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings WHERE key LIKE ?", (prefix.replace("%", "") + "%",)).fetchall()
        out: dict[str, Any] = {}
        for k, v in rows:
            try:
                out[k[len(prefix):]] = json.loads(v)
            except Exception:
                out[k[len(prefix):]] = v
        return out

    def drop_setting(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settings WHERE key=?", (key,))
            self._conn.commit()

    # ------------------------------------------------------------------ jobs
    # An hour of waiting is worth this many priority points, and no more. Straight priority order
    # was fine while the line was almost always empty; with a deep buffer it would mean a priority-0
    # song (a channel nobody is listening to - exactly the one that needs stock BEFORE it is tuned
    # to) could never run at all. The cap keeps something you just asked for in front.
    AGE_BONUS_PER_HOUR = 4.0
    AGE_BONUS_MAX = 4.0

    def next_job(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE status='queued' "
                "ORDER BY (priority + MIN(?, (? - COALESCE(created, ?)) / 3600.0 * ?)) DESC, created ASC LIMIT 1",
                (self.AGE_BONUS_MAX, time.time(), time.time(), self.AGE_BONUS_PER_HOUR)).fetchone()
        return self._row("jobs", row)

    def mark_played(self, song_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE songs SET plays=plays+1, last_played=? WHERE id=?", (time.time(), song_id))
            self._conn.commit()

    # ------------------------------------------------------------------ how much each channel is listened to
    def note_play(self, station_id: str | None) -> None:
        """One play on one channel. The score is halved for every week since the last play before this one is
        added, so it is a running picture of what is actually being listened to rather than a lifetime total."""
        if not station_id or station_id == "all":
            return
        t = time.time()
        with self._lock:
            row = self._conn.execute("SELECT score, plays, last FROM station_plays WHERE station_id=?", (station_id,)).fetchone()
            score, plays, last = (row[0] or 0.0, row[1] or 0, row[2]) if row else (0.0, 0, None)
            if last:
                score *= 0.5 ** ((t - last) / PLAY_HALF_LIFE_S)
            self._conn.execute(
                "INSERT INTO station_plays (station_id, score, plays, last) VALUES (?,?,?,?) "
                "ON CONFLICT(station_id) DO UPDATE SET score=excluded.score, plays=excluded.plays, last=excluded.last",
                (station_id, score + 1.0, plays + 1, t))
            self._conn.commit()

    def play_scores(self) -> dict[str, dict[str, Any]]:
        """Every channel's score as of now (the stored one, faded to this moment), its lifetime plays and when it
        was last heard."""
        t = time.time()
        out: dict[str, dict[str, Any]] = {}
        for sid, score, plays, last in self._conn.execute("SELECT station_id, score, plays, last FROM station_plays"):
            out[sid] = {"score": (score or 0.0) * 0.5 ** ((t - (last or t)) / PLAY_HALF_LIFE_S),
                        "plays": plays or 0, "last": last}
        return out

    def seed_play_scores(self) -> int:
        """First run only: where the dial starts. Every song already carries how many times it was played and
        when it was played last, so each channel is given the sum of its songs' plays faded by that date. It is
        an approximation — only the LAST play of each song is on record — and a day of real listening replaces
        it. Does nothing once the table has anything in it."""
        with self._lock:
            if self._conn.execute("SELECT COUNT(*) FROM station_plays").fetchone()[0]:
                return 0
            t = time.time()
            tally: dict[str, list[float]] = {}
            for sid, plays, last in self._conn.execute(
                    "SELECT station_id, plays, last_played FROM songs WHERE plays>0 AND station_id IS NOT NULL"):
                if not sid:
                    continue
                faded = (plays or 0) * 0.5 ** ((t - (last or t)) / PLAY_HALF_LIFE_S)
                row = tally.setdefault(sid, [0.0, 0.0, 0.0])
                row[0] += faded
                row[1] += plays or 0
                row[2] = max(row[2], last or 0.0)
            for sid, (score, plays, last) in tally.items():
                self._conn.execute("INSERT OR REPLACE INTO station_plays (station_id, score, plays, last) VALUES (?,?,?,?)",
                                   (sid, score, int(plays), last or None))
            self._conn.commit()
            return len(tally)

    def recent_played_ids(self, station_id: str | None, n: int = 6, liked_only: bool = False) -> list[str]:
        """The last n songs played. liked_only asks the Favorites channel's question instead: the last hearted
        songs played, whatever channel they live on."""
        where = "status='ready' AND last_played IS NOT NULL"
        params: list[Any] = []
        if liked_only:
            where += " AND liked=1"
        elif station_id and station_id != "all":
            where += " AND station_id=?"
            params.append(station_id)
        with self._lock:
            rows = self._conn.execute(f"SELECT id FROM songs WHERE {where} ORDER BY last_played DESC LIMIT ?", [*params, int(n)]).fetchall()
        return [r[0] for r in rows]

    def recent_values(self, column: str, station_id: str, n: int = 6) -> list[str]:
        """Last n non-null values of a songs column for a station, newest first (used to avoid repeating themes/styles)."""
        with self._lock:
            rows = self._conn.execute(f"SELECT {column} FROM songs WHERE station_id=? AND {column} IS NOT NULL ORDER BY created DESC LIMIT ?", (station_id, int(n))).fetchall()
        return [r[0] for r in rows if r[0]]


DBI: DB | None = None


def get_db() -> DB:
    global DBI
    if DBI is None:
        DBI = DB()
    return DBI
