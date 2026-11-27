"""SQLite, one file. Small on purpose: a box with one camera makes a few hundred rows a day."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_V1 = """
CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- One row per camera that has ever uploaded. The serial is the first folder name the
-- camera writes into, which is how a file is traced to the camera that sent it.
CREATE TABLE cameras (
  id          INTEGER PRIMARY KEY,
  serial      TEXT UNIQUE,
  ip          TEXT,
  label       TEXT,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  last_upload TEXT,
  uploads     INTEGER NOT NULL DEFAULT 0
);

-- One row per time a plate was seen (after merging the several pictures one pass makes).
CREATE TABLE sightings (
  id             INTEGER PRIMARY KEY,
  plate          TEXT NOT NULL,
  camera_id      INTEGER REFERENCES cameras(id) ON DELETE SET NULL,
  seen_at        TEXT NOT NULL,      -- box clock, UTC
  cam_time       TEXT,               -- the camera's own wall clock, as it prints it on the picture
  confidence     REAL,
  det_confidence REAL,
  box            TEXT,               -- JSON {x,y,w,h} in the full picture
  plate_px       INTEGER,            -- plate width in pixels: how legible the read is
  valid_format   INTEGER NOT NULL DEFAULT 0,
  image          TEXT,               -- relative to media_dir
  crop           TEXT
);
CREATE INDEX sightings_plate_idx ON sightings (plate, seen_at);
CREATE INDEX sightings_time_idx  ON sightings (seen_at);

-- A short diary: camera connected, picture received, network changed.
CREATE TABLE events (
  id     INTEGER PRIMARY KEY,
  ts     TEXT NOT NULL,
  kind   TEXT NOT NULL,
  detail TEXT
);
CREATE INDEX events_ts_idx ON events (ts);
"""

# The motion log: one row per picture kept as a record of movement in front of the camera, whether or not
# a plate was in it. Capped by TOTAL SIZE of the pictures (see motionlog.py), oldest removed first.
SCHEMA_V2 = """
CREATE TABLE motion_events (
  id        INTEGER PRIMARY KEY,
  camera_id INTEGER REFERENCES cameras(id) ON DELETE SET NULL,
  seen_at   TEXT NOT NULL,        -- box clock, UTC
  cam_time  TEXT,                 -- the camera's own wall clock, from the file name
  image     TEXT NOT NULL,        -- relative to media_dir
  bytes     INTEGER NOT NULL,
  width     INTEGER,
  height    INTEGER,
  plates    TEXT NOT NULL DEFAULT ''   -- plates read in this picture, space separated
);
CREATE INDEX motion_events_time_idx ON motion_events (seen_at, id);
"""

# Pictures the intake has already dealt with. The camera asks the receiver whether a picture is there before it
# sends it, and sends it again if not, so a processed picture is KEPT (for a while, see intake.py) and listed here
# so it is never processed twice.
SCHEMA_V3 = """
CREATE TABLE delivered (
  path  TEXT PRIMARY KEY,         -- relative to the intake folder, with / separators
  size  INTEGER NOT NULL,
  mtime REAL NOT NULL,            -- the file's own modification time when it was dealt with
  t     REAL NOT NULL             -- when PlateBox dealt with it (epoch seconds)
);
CREATE INDEX delivered_t_idx ON delivered (t);
"""

# A fingerprint of every picture read. The camera re-sends stored pictures under new file names and with the
# picture's own old time printed on it, so neither the name nor the time shows it is a repeat; its bytes do.
SCHEMA_V4 = """
CREATE TABLE seen_pictures (
  hash TEXT PRIMARY KEY,          -- SHA-256 of the picture exactly as the camera sent it
  t    REAL NOT NULL              -- when PlateBox first saw it (epoch seconds)
);
CREATE INDEX seen_pictures_t_idx ON seen_pictures (t);
"""

MIGRATIONS = [SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Db:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            have = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for i, sql in enumerate(MIGRATIONS[have:], start=have + 1):
                self._conn.execute("BEGIN")
                try:
                    for stmt in [s for s in sql.split(";\n") if s.strip()]:
                        self._conn.execute(stmt)
                    self._conn.execute(f"PRAGMA user_version = {i}")
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    def run(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return cur.lastrowid or 0

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    # -- settings ---------------------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def put(self, key: str, value: str) -> None:
        self.run(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete_setting(self, key: str) -> None:
        self.run("DELETE FROM settings WHERE key = ?", (key,))

    # -- diary ------------------------------------------------------------------------
    def event(self, kind: str, detail: str = "") -> None:
        self.run("INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)", (now_iso(), kind, detail))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
