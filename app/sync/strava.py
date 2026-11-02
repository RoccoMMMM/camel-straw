"""Strava OAuth + activity import (official API v3)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from .. import config, db
from .common import find_duplicate, normalise_sport, num, parse_local

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API = "https://www.strava.com/api/v3"


def configured() -> bool:
    return bool(config.STRAVA_CLIENT_ID and config.STRAVA_CLIENT_SECRET)


def authorize_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": config.STRAVA_CLIENT_ID,
        "redirect_uri": config.STRAVA_REDIRECT_URI,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "read,activity:read_all",
        "state": state,
    })


def exchange_code(code: str) -> dict:
    r = httpx.post(TOKEN_URL, data={
        "client_id": config.STRAVA_CLIENT_ID,
        "client_secret": config.STRAVA_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json()
    db.save_token("strava", {k: tok.get(k) for k in ("access_token", "refresh_token", "expires_at", "athlete")})
    return tok


def _access_token() -> str:
    tok = db.get_token("strava")
    if not tok:
        raise RuntimeError("Strava is not connected. Use Settings → Connect Strava.")
    if tok["expires_at"] < time.time() + 120:
        r = httpx.post(TOKEN_URL, data={
            "client_id": config.STRAVA_CLIENT_ID,
            "client_secret": config.STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
        }, timeout=30)
        r.raise_for_status()
        new = r.json()
        tok.update({k: new[k] for k in ("access_token", "refresh_token", "expires_at")})
        db.save_token("strava", tok)
    return tok["access_token"]


def to_row(a: dict) -> dict:
    start = parse_local(a["start_date_local"])
    return {
        "id": f"strava:{a['id']}",
        "source": "strava",
        "strava_id": str(a["id"]),
        "name": a.get("name"),
        "sport": normalise_sport(a.get("sport_type") or a.get("type"), a.get("name")),
        "start_time": start.isoformat(),
        "date": start.date().isoformat(),
        "duration_s": num(a.get("elapsed_time")),
        "moving_s": num(a.get("moving_time")),
        "distance_m": num(a.get("distance")),
        "elevation_m": num(a.get("total_elevation_gain")),
        "avg_hr": num(a.get("average_heartrate")),
        "max_hr": num(a.get("max_heartrate")),
        "avg_speed": num(a.get("average_speed")),
        "avg_power": num(a.get("average_watts")),
        "calories": num(a.get("calories")) or (num(a.get("kilojoules")) or None),
        "suffer_score": num(a.get("suffer_score")),
    }


def store_activity(conn, a: dict) -> str:
    """Insert a Strava activity, merging into an existing Garmin copy when it's the same workout."""
    row = to_row(a)
    existing = conn.execute("SELECT id FROM activities WHERE strava_id=?", (row["strava_id"],)).fetchone()
    if existing is None:
        dup = find_duplicate(conn, parse_local(row["start_time"]), row["duration_s"], "strava")
        if dup is not None:
            # Garmin is the richer source; just link the Strava id and fill any gaps.
            conn.execute(
                "UPDATE activities SET strava_id=?, suffer_score=COALESCE(suffer_score, ?), "
                "name=COALESCE(name, ?), avg_power=COALESCE(avg_power, ?) WHERE id=?",
                (row["strava_id"], row["suffer_score"], row["name"], row["avg_power"], dup["id"]),
            )
            return "merged"
    elif existing["id"] != row["id"]:
        conn.execute("UPDATE activities SET suffer_score=?, name=? WHERE id=?",
                     (row["suffer_score"], row["name"], existing["id"]))
        return "merged"
    db.upsert(conn, "activities", row, key="id", keep_existing=True)
    return "stored"


def sync(days: int | None = None) -> str:
    conn = db.get_conn()
    last = conn.execute("SELECT MAX(start_time) m FROM activities WHERE strava_id IS NOT NULL").fetchone()["m"]
    if days is None and last:
        after = parse_local(last) - timedelta(days=3)
    else:
        after = datetime.now() - timedelta(days=days or config.INITIAL_SYNC_DAYS)
    headers = {"Authorization": f"Bearer {_access_token()}"}
    counts = {"stored": 0, "merged": 0}
    page = 1
    with httpx.Client(timeout=30, headers=headers) as client:
        while True:
            r = client.get(f"{API}/athlete/activities",
                           params={"after": int(after.timestamp()), "per_page": 100, "page": page})
            if r.status_code == 429:
                raise RuntimeError("Strava rate limit reached; try again in 15 minutes.")
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            with db.tx(conn):
                for a in batch:
                    counts[store_activity(conn, a)] += 1
            page += 1
    return f"Strava: {counts['stored']} activities stored, {counts['merged']} matched to Garmin"
