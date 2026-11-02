"""SQLite storage. Plain sqlite3 keeps the app dependency-light and easy to back up (one file)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id            TEXT PRIMARY KEY,          -- "garmin:<id>" or "strava:<id>"
    source        TEXT NOT NULL,
    garmin_id     TEXT,
    strava_id     TEXT,
    name          TEXT,
    sport         TEXT,                      -- normalised: run, ride, swim, walk, hike, strength, other
    start_time    TEXT NOT NULL,             -- local wall time, ISO 8601
    date          TEXT NOT NULL,             -- YYYY-MM-DD (local)
    duration_s    REAL,
    moving_s      REAL,
    distance_m    REAL,
    elevation_m   REAL,
    avg_hr        REAL,
    max_hr        REAL,
    avg_speed     REAL,                      -- m/s
    avg_power     REAL,
    calories      REAL,
    garmin_load   REAL,                      -- Garmin "activityTrainingLoad"
    suffer_score  REAL,                      -- Strava relative effort
    aerobic_te    REAL,
    anaerobic_te  REAL,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);

CREATE TABLE IF NOT EXISTS daily_metrics (   -- from Garmin wellness endpoints
    date              TEXT PRIMARY KEY,
    resting_hr        REAL,
    hrv               REAL,                  -- last-night average rMSSD (ms)
    hrv_baseline_low  REAL,
    hrv_baseline_high REAL,
    hrv_status        TEXT,
    sleep_s           REAL,
    deep_s            REAL,
    rem_s             REAL,
    light_s           REAL,
    awake_s           REAL,
    sleep_score       REAL,
    bedtime           TEXT,                  -- HH:MM local
    wake_time         TEXT,                  -- HH:MM local
    steps             REAL,
    stress_avg        REAL,
    body_battery_high REAL,
    body_battery_low  REAL,
    kcal_burned       REAL,
    active_kcal       REAL,
    vo2max_run        REAL,
    vo2max_ride       REAL,
    readiness         REAL,
    kcal_in           REAL,                  -- Garmin nutrition log, if used
    protein_g         REAL,
    carbs_g           REAL,
    fat_g             REAL,
    water_ml          REAL,
    drinks            REAL,                  -- Garmin lifestyle logging "alcohol", if used
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS habits (          -- manual log; overrides Garmin values when present
    date          TEXT PRIMARY KEY,          -- the morning you woke up
    bedtime       TEXT,                      -- HH:MM
    wake_time     TEXT,                      -- HH:MM
    sleep_quality INTEGER,                   -- 1-5
    drinks        REAL,                      -- standard drinks
    calories      REAL,
    protein_g     REAL,
    carbs_g       REAL,
    fat_g         REAL,
    water_l       REAL,
    caffeine_mg   REAL,
    energy        INTEGER,                   -- 1-5 subjective
    soreness      INTEGER,                   -- 1-5 subjective
    notes         TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tokens   (provider TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT, started_at TEXT, finished_at TEXT, ok INTEGER, message TEXT
);
"""

_lock = threading.Lock()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
    return _conn


def set_conn(conn: sqlite3.Connection | None) -> None:
    """Swap the global connection (used by tests and the demo seeder)."""
    global _conn
    _conn = conn


@contextmanager
def tx(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    conn = conn or get_conn()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any], key: str = "date",
           keep_existing: bool = False) -> None:
    """Insert or update a row. With keep_existing, None values never overwrite stored data."""
    cols = list(row)
    placeholders = ",".join("?" for _ in cols)
    if keep_existing:
        updates = ",".join(f"{c}=COALESCE(excluded.{c}, {table}.{c})" for c in cols if c != key)
    else:
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != key)
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT({key}) DO UPDATE SET {updates}"
    conn.execute(sql, [row[c] for c in cols])


def rows(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---- settings / tokens -------------------------------------------------------

def get_settings(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    conn = conn or get_conn()
    out = dict(config.DEFAULT_SETTINGS)
    for r in conn.execute("SELECT key, value FROM settings"):
        out[r["key"]] = json.loads(r["value"])
    return out


def save_settings(values: dict[str, Any], conn: sqlite3.Connection | None = None) -> None:
    with tx(conn) as c:
        for k, v in values.items():
            if k in config.DEFAULT_SETTINGS:
                c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))


def get_token(provider: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    conn = conn or get_conn()
    r = conn.execute("SELECT data FROM tokens WHERE provider=?", (provider,)).fetchone()
    return json.loads(r["data"]) if r else None


def save_token(provider: str, data: dict[str, Any] | None, conn: sqlite3.Connection | None = None) -> None:
    with tx(conn) as c:
        if data is None:
            c.execute("DELETE FROM tokens WHERE provider=?", (provider,))
        else:
            c.execute("INSERT OR REPLACE INTO tokens (provider, data) VALUES (?, ?)", (provider, json.dumps(data)))


def log_sync(provider: str, started: str, ok: bool, message: str, conn: sqlite3.Connection | None = None) -> None:
    with tx(conn) as c:
        c.execute("INSERT INTO sync_log (provider, started_at, finished_at, ok, message) VALUES (?,?,?,?,?)",
                  (provider, started, now(), int(ok), message[:2000]))
