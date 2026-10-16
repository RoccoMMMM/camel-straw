"""Helpers shared by the Garmin and Strava importers."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

SPORT_MAP = {
    "run": ["run", "treadmill", "trail", "track"],
    "ride": ["ride", "cycling", "bike", "biking", "velomobile", "handcycle"],
    "swim": ["swim"],
    "walk": ["walk"],
    "hike": ["hike", "hiking", "mountaineering"],
    "strength": ["strength", "weight", "crossfit", "hiit", "workout", "training"],
    "row": ["row"],
    "ski": ["ski", "snowboard"],
    "yoga": ["yoga", "pilates", "mobility"],
}


def normalise_sport(raw: str | None) -> str:
    s = (raw or "").lower()
    # Order matters: "virtualride" -> ride, "trail_running" -> run, etc.
    for sport, needles in SPORT_MAP.items():
        if any(n in s for n in needles):
            return sport
    return "other"


def num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def find_number(obj: Any, *keys: str) -> float | None:
    """Depth-first search of nested dicts/lists for the first numeric value under any of `keys`.

    Garmin's undocumented endpoints change shape between firmware/app versions, so the importer
    looks values up by name rather than by fixed path.
    """
    wanted = {k.lower() for k in keys}
    stack = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k.lower() in wanted and num(v) is not None and not isinstance(v, bool):
                    return num(v)
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


def parse_local(ts: str) -> datetime:
    ts = ts.replace("Z", "").replace("T", " ")
    return datetime.fromisoformat(ts[:19])


def find_duplicate(conn: sqlite3.Connection, start: datetime, duration_s: float | None,
                   exclude_source: str) -> sqlite3.Row | None:
    """Same workout recorded on Garmin and synced to Strava: start within 3 min, duration within 10%."""
    lo = start.timestamp() - 180
    hi = start.timestamp() + 180
    for r in conn.execute(
        "SELECT * FROM activities WHERE source != ? AND date BETWEEN ? AND ?",
        (exclude_source, (start.date()).isoformat(), (start.date()).isoformat()),
    ):
        t = parse_local(r["start_time"]).timestamp()
        if lo <= t <= hi:
            d = r["duration_s"] or 0
            if not duration_s or not d or abs(d - duration_s) <= 0.1 * max(d, duration_s):
                return r
    return None
