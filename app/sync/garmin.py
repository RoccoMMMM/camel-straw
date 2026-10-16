"""Garmin Connect import (activities + daily wellness: sleep, HRV, resting HR, stress, body battery,
VO2max, readiness, and — if you log them in Garmin Connect — food, hydration and alcohol).

Garmin's official Health API is only open to approved businesses, so this uses the widely used
`garminconnect` library, which talks to the same endpoints as the Garmin Connect web app.
Run `python -m app.cli garmin-login` once to store tokens (handles MFA); the server reuses them.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from .. import config, db
from .common import find_duplicate, find_number, normalise_sport, num, parse_local

log = logging.getLogger(__name__)


def token_dir() -> str:
    config.GARMIN_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    return str(config.GARMIN_TOKEN_DIR)


def has_tokens() -> bool:
    d = config.GARMIN_TOKEN_DIR
    return d.exists() and any(d.iterdir())


def configured() -> bool:
    return has_tokens() or bool(config.GARMIN_EMAIL and config.GARMIN_PASSWORD)


def client(prompt_mfa: Callable[[], str] | None = None):
    from garminconnect import Garmin

    if has_tokens():
        g = Garmin()
        g.login(token_dir())
        return g
    if not (config.GARMIN_EMAIL and config.GARMIN_PASSWORD):
        raise RuntimeError("Garmin is not connected. Run `python -m app.cli garmin-login`.")
    g = Garmin(config.GARMIN_EMAIL, config.GARMIN_PASSWORD, prompt_mfa=prompt_mfa)
    g.login()
    g.client.dump(token_dir())
    return g


# ---- activities --------------------------------------------------------------

def activity_row(a: dict) -> dict:
    start = parse_local(a["startTimeLocal"])
    return {
        "id": f"garmin:{a['activityId']}",
        "source": "garmin",
        "garmin_id": str(a["activityId"]),
        "name": a.get("activityName"),
        "sport": normalise_sport((a.get("activityType") or {}).get("typeKey"), a.get("activityName")),
        "start_time": start.isoformat(),
        "date": start.date().isoformat(),
        "duration_s": num(a.get("duration")),
        "moving_s": num(a.get("movingDuration")),
        "distance_m": num(a.get("distance")),
        "elevation_m": num(a.get("elevationGain")),
        "avg_hr": num(a.get("averageHR")),
        "max_hr": num(a.get("maxHR")),
        "avg_speed": num(a.get("averageSpeed")),
        "avg_power": num(a.get("avgPower")),
        "calories": num(a.get("calories")),
        "garmin_load": num(a.get("activityTrainingLoad")),
        "aerobic_te": num(a.get("aerobicTrainingEffect")),
        "anaerobic_te": num(a.get("anaerobicTrainingEffect")),
    }


def drop_multisport_parents(acts: list[dict]) -> list[dict]:
    """A Garmin triathlon is a multisport parent plus one child per leg. When the legs are present,
    keep them (per-sport detail) and drop the parent so the race isn't counted twice."""
    ids = {a.get("activityId") for a in acts}
    return [a for a in acts if not (a.get("childIds") and any(c in ids for c in a["childIds"]))]


def store_activity(conn, a: dict) -> str:
    row = activity_row(a)
    if conn.execute("SELECT 1 FROM activities WHERE id=?", (row["id"],)).fetchone() is None:
        if row["sport"] == "multisport" and row["duration_s"]:
            # Strava may hold the race as separate swim/bike/run legs: fold them all into this record.
            t0 = parse_local(row["start_time"]).timestamp()
            legs = [r for r in conn.execute("SELECT * FROM activities WHERE source='strava' AND date=?", (row["date"],))
                    if t0 - 180 <= parse_local(r["start_time"]).timestamp() <= t0 + row["duration_s"] + 180]
            if legs:
                row["strava_id"] = legs[0]["strava_id"]
                row["suffer_score"] = sum(r["suffer_score"] or 0 for r in legs) or None
                conn.executemany("DELETE FROM activities WHERE id=?", [(r["id"],) for r in legs])
                db.upsert(conn, "activities", row, key="id", keep_existing=True)
                return "merged"
        dup = find_duplicate(conn, parse_local(row["start_time"]), row["duration_s"], "garmin")
        if dup is not None and dup["source"] == "strava":
            # Replace the Strava copy with the richer Garmin record but keep the Strava link/effort.
            row["strava_id"] = dup["strava_id"]
            row["suffer_score"] = dup["suffer_score"]
            conn.execute("DELETE FROM activities WHERE id=?", (dup["id"],))
            db.upsert(conn, "activities", row, key="id", keep_existing=True)
            return "merged"
    db.upsert(conn, "activities", row, key="id", keep_existing=True)
    return "stored"


# ---- daily wellness ----------------------------------------------------------

def _hhmm_from_local_ms(ms: Any) -> str | None:
    if not ms:
        return None
    # Garmin "...TimestampLocal" values are local wall-clock time encoded as if it were UTC.
    return datetime.fromtimestamp(float(ms) / 1000, timezone.utc).strftime("%H:%M")


def _safe(fn: Callable, *args) -> Any:
    try:
        return fn(*args)
    except Exception as e:  # one missing endpoint (e.g. no HRV on older watches) shouldn't stop the sync
        log.debug("garmin %s%s failed: %s", getattr(fn, "__name__", fn), args, e)
        return None


def _alcohol(lifestyle: Any) -> float | None:
    """Garmin 'Lifestyle Logging' lets you tag alcohol; pull the amount if it was logged."""
    stack = [lifestyle]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            label = " ".join(str(cur.get(k, "")) for k in ("name", "behaviorName", "key", "behavior", "category")).lower()
            if "alcohol" in label:
                v = find_number(cur, "amount", "quantity", "value", "count", "totalAmount")
                if v is not None:
                    return v
                status = str(cur.get("status") or cur.get("logStatus") or "").lower()
                if status in ("no", "false", "none"):
                    return 0.0
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def daily_row(g, day: str) -> dict:
    sleep = _safe(g.get_sleep_data, day) or {}
    dto = sleep.get("dailySleepDTO") or {}
    scores = dto.get("sleepScores") or {}
    hrv = (_safe(g.get_hrv_data, day) or {}).get("hrvSummary") or {}
    baseline = hrv.get("baseline") or {}
    stats = _safe(g.get_stats, day) or {}
    maxm = _safe(g.get_max_metrics, day)
    readiness = _safe(g.get_training_readiness, day)
    hydration = _safe(g.get_hydration_data, day) or {}
    food = _safe(g.get_nutrition_daily_food_log, day, False, True)
    lifestyle = _safe(g.get_lifestyle_logging_data, day)

    vo2_run = vo2_ride = None
    if isinstance(maxm, list) and maxm:
        vo2_run = find_number(maxm[0].get("generic") or {}, "vo2MaxPreciseValue", "vo2MaxValue")
        vo2_ride = find_number(maxm[0].get("cycling") or {}, "vo2MaxPreciseValue", "vo2MaxValue")

    kcal_in = find_number(food, "consumedCalories", "caloriesConsumed", "totalCalories") if food else None
    # Nights you didn't wear the watch come back with an empty or zero-length sleep record: store nothing
    # for them, so they don't read as "0 h sleep" and your manual log fills the gap.
    if not (num(dto.get("sleepTimeSeconds")) or 0) > 0:
        dto, scores = {}, {}
    return {
        "date": day,
        "resting_hr": num(stats.get("restingHeartRate")) or num(sleep.get("restingHeartRate")),
        "hrv": num(hrv.get("lastNightAvg")),
        "hrv_baseline_low": num(baseline.get("balancedLow")),
        "hrv_baseline_high": num(baseline.get("balancedUpper")),
        "hrv_status": hrv.get("status"),
        "sleep_s": num(dto.get("sleepTimeSeconds")),
        "deep_s": num(dto.get("deepSleepSeconds")),
        "rem_s": num(dto.get("remSleepSeconds")),
        "light_s": num(dto.get("lightSleepSeconds")),
        "awake_s": num(dto.get("awakeSleepSeconds")),
        "sleep_score": num((scores.get("overall") or {}).get("value")),
        "bedtime": _hhmm_from_local_ms(dto.get("sleepStartTimestampLocal")),
        "wake_time": _hhmm_from_local_ms(dto.get("sleepEndTimestampLocal")),
        "steps": num(stats.get("totalSteps")),
        "stress_avg": num(stats.get("averageStressLevel")),
        "body_battery_high": num(stats.get("bodyBatteryHighestValue")),
        "body_battery_low": num(stats.get("bodyBatteryLowestValue")),
        "kcal_burned": num(stats.get("totalKilocalories")),
        "active_kcal": num(stats.get("activeKilocalories")),
        "vo2max_run": vo2_run,
        "vo2max_ride": vo2_ride,
        "readiness": find_number(readiness, "score") if readiness else None,
        "kcal_in": kcal_in or None,
        "protein_g": (find_number(food, "protein", "proteinGrams") or None) if food else None,
        "carbs_g": (find_number(food, "carbohydrates", "carbs", "carbohydrateGrams") or None) if food else None,
        "fat_g": (find_number(food, "fat", "fatGrams") or None) if food else None,
        "water_ml": num(hydration.get("valueInML")) or None,
        "drinks": _alcohol(lifestyle),
        "updated_at": db.now(),
    }


def sync(days: int | None = None) -> str:
    conn = db.get_conn()
    today = date.today()
    last = conn.execute("SELECT MAX(date) m FROM daily_metrics").fetchone()["m"]
    if days is None and last:
        start = min(date.fromisoformat(last), today) - timedelta(days=2)
    else:
        start = today - timedelta(days=days or config.INITIAL_SYNC_DAYS)

    g = client()
    acts = g.get_activities_by_date(start.isoformat(), today.isoformat()) or []
    counts = {"stored": 0, "merged": 0}
    with db.tx(conn):
        for a in drop_multisport_parents(acts):
            counts[store_activity(conn, a)] += 1

    n_days = 0
    d = start
    while d <= today:
        row = daily_row(g, d.isoformat())
        with db.tx(conn):
            db.upsert(conn, "daily_metrics", row, keep_existing=True)
        n_days += 1
        d += timedelta(days=1)
    return f"Garmin: {counts['stored']} activities stored, {counts['merged']} merged with Strava, {n_days} days of wellness data"


# ---- interactive login from the web UI (supports MFA) ------------------------

_pending: dict[str, Any] = {}


def web_login(email: str, password: str) -> str:
    """Returns "ok" or "needs_mfa". Tokens are saved so the password is never stored."""
    from garminconnect import Garmin

    g = Garmin(email, password, return_on_mfa=True)
    status, state = g.login()
    if status == "needs_mfa":
        _pending.clear()
        _pending.update({"g": g, "state": state})
        return "needs_mfa"
    g._load_profile_and_settings()
    g.client.dump(token_dir())
    return "ok"


def web_mfa(code: str) -> str:
    if not _pending:
        raise RuntimeError("No Garmin login is waiting for an MFA code. Start again.")
    g, state = _pending["g"], _pending["state"]
    g.resume_login(state, code)
    g.client.dump(token_dir())
    _pending.clear()
    return "ok"


def disconnect() -> None:
    import shutil

    shutil.rmtree(config.GARMIN_TOKEN_DIR, ignore_errors=True)
