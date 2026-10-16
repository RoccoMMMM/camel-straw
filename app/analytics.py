"""Training-load and wellness analytics. Pure functions over plain dicts so they're easy to test.

Load model
----------
Every activity gets one load number on a TRIMP-like scale (a 1 h steady endurance session ≈ 60–90):

1. Banister TRIMP from average heart rate, when HR is recorded (most consistent across sports).
2. Otherwise Garmin's own "training load" (EPOC-based, similar scale).
3. Otherwise Strava "relative effort".
4. Otherwise duration-based estimate (0.8 per minute, a moderate session).

From daily load:  Fitness (CTL) = 42-day EWMA, Fatigue (ATL) = 7-day EWMA, Form (TSB) = CTL − ATL
(both from the previous day, i.e. how fresh you are going *into* today).
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, pstdev
from typing import Any, Iterable

CTL_DAYS = 42
ATL_DAYS = 7


# ---- small helpers -----------------------------------------------------------

def d(s: str) -> date:
    return date.fromisoformat(s[:10])


def daterange(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def avg(xs: Iterable[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return mean(vals) if vals else None


def sd(xs: Iterable[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return pstdev(vals) if len(vals) >= 2 else None


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 5:
        return None
    mx, my = mean(xs), mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def clock_to_minutes(hhmm: str | None, bedtime: bool = False) -> float | None:
    """'23:30' -> 1410. For bedtimes, times after midnight continue past 1440 so 00:30 > 23:30."""
    if not hhmm:
        return None
    try:
        h, m = (int(x) for x in hhmm.split(":")[:2])
    except ValueError:
        return None
    mins = h * 60 + m
    if bedtime and mins < 12 * 60:
        mins += 24 * 60
    return float(mins)


def minutes_to_clock(mins: float | None) -> str | None:
    if mins is None:
        return None
    mins = round(mins) % (24 * 60)
    return f"{mins // 60:02d}:{mins % 60:02d}"


# ---- training load -----------------------------------------------------------

def trimp(duration_s: float, avg_hr: float, rest_hr: float, max_hr: float, sex: str = "male") -> float:
    if max_hr <= rest_hr:
        return 0.0
    hrr = max(0.0, min(1.0, (avg_hr - rest_hr) / (max_hr - rest_hr)))
    k, b = (0.86, 1.67) if str(sex).lower().startswith("f") else (0.64, 1.92)
    return (duration_s / 60) * hrr * k * math.exp(b * hrr)


def activity_load(a: dict, settings: dict) -> float:
    dur = a.get("moving_s") or a.get("duration_s") or 0
    if a.get("avg_hr") and dur:
        return trimp(dur, a["avg_hr"], settings["resting_hr"], settings["max_hr"], settings.get("sex", "male"))
    if a.get("garmin_load"):
        return float(a["garmin_load"])
    if a.get("suffer_score"):
        return float(a["suffer_score"])
    return dur / 60 * 0.8


def intensity_class(a: dict, settings: dict) -> str | None:
    """Rough zone from average HR as % of max: easy (<77%), moderate/'grey zone' (77–87%), hard (>87%)."""
    if not a.get("avg_hr"):
        return None
    pct = a["avg_hr"] / settings["max_hr"]
    return "easy" if pct < 0.77 else "moderate" if pct < 0.87 else "hard"


def load_series(activities: list[dict], settings: dict, start: date, end: date) -> list[dict]:
    """Daily load, CTL, ATL, TSB from `start` to `end` (EWMAs warm up from the first activity)."""
    daily: dict[date, float] = defaultdict(float)
    for a in activities:
        daily[d(a["date"])] += activity_load(a, settings)
    first = min([start, *daily.keys()]) if daily else start
    ctl = atl = 0.0
    ka, kc = 1 - math.exp(-1 / ATL_DAYS), 1 - math.exp(-1 / CTL_DAYS)
    out = []
    for day in daterange(first, end):
        tsb = ctl - atl
        load = daily.get(day, 0.0)
        ctl += (load - ctl) * kc
        atl += (load - atl) * ka
        if day >= start:
            out.append({"date": day.isoformat(), "load": round(load, 1), "ctl": round(ctl, 1),
                        "atl": round(atl, 1), "tsb": round(tsb, 1)})
    return out


def weekly_summary(activities: list[dict], settings: dict, weeks: int, today: date) -> list[dict]:
    monday = today - timedelta(days=today.weekday())
    starts = [monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    buckets = {s: {"week": s.isoformat(), "hours": 0.0, "distance_km": 0.0, "load": 0.0, "count": 0,
                   "by_sport": defaultdict(float)} for s in starts}
    for a in activities:
        ad = d(a["date"])
        wk = ad - timedelta(days=ad.weekday())
        b = buckets.get(wk)
        if b is None:
            continue
        hrs = (a.get("moving_s") or a.get("duration_s") or 0) / 3600
        b["hours"] += hrs
        b["distance_km"] += (a.get("distance_m") or 0) / 1000
        b["load"] += activity_load(a, settings)
        b["count"] += 1
        b["by_sport"][a.get("sport") or "other"] += hrs
    out = []
    for s in starts:
        b = buckets[s]
        out.append({**b, "hours": round(b["hours"], 2), "distance_km": round(b["distance_km"], 1),
                    "load": round(b["load"]), "by_sport": {k: round(v, 2) for k, v in b["by_sport"].items()}})
    return out


def efficiency_series(activities: list[dict], settings: dict) -> list[dict]:
    """Aerobic efficiency for easy/steady runs & rides: output per heartbeat.

    Runs: metres per minute ÷ HR.  Rides with power: watts ÷ HR.  Rising = fitter.
    """
    out = []
    for a in sorted(activities, key=lambda x: x["start_time"]):
        hr = a.get("avg_hr")
        dur = a.get("moving_s") or a.get("duration_s") or 0
        if not hr or dur < 20 * 60 or intensity_class(a, settings) == "hard":
            continue
        if a.get("sport") == "run" and a.get("avg_speed"):
            out.append({"date": a["date"], "sport": "run", "ef": round(a["avg_speed"] * 60 / hr, 3), "name": a.get("name")})
        elif a.get("sport") == "ride" and a.get("avg_power"):
            out.append({"date": a["date"], "sport": "ride", "ef": round(a["avg_power"] / hr, 3), "name": a.get("name")})
    return out


# ---- wellness / habits -------------------------------------------------------

MERGE_FIELDS = {
    # merged field: (habit column, garmin column)
    "bedtime": ("bedtime", "bedtime"),
    "wake_time": ("wake_time", "wake_time"),
    "drinks": ("drinks", "drinks"),
    "calories": ("calories", "kcal_in"),
    "protein_g": ("protein_g", "protein_g"),
    "carbs_g": ("carbs_g", "carbs_g"),
    "fat_g": ("fat_g", "fat_g"),
}


def merge_days(wellness: list[dict], habits: list[dict], start: date, end: date) -> list[dict]:
    """One row per day: Garmin wellness + manual habits (manual entries win)."""
    w = {r["date"]: r for r in wellness}
    h = {r["date"]: r for r in habits}
    out = []
    for day in daterange(start, end):
        k = day.isoformat()
        wr, hr = w.get(k, {}), h.get(k, {})
        row: dict[str, Any] = {"date": k}
        for f in ("resting_hr", "hrv", "hrv_baseline_low", "hrv_baseline_high", "hrv_status", "sleep_s",
                  "deep_s", "rem_s", "light_s", "awake_s", "sleep_score", "steps", "stress_avg",
                  "body_battery_high", "body_battery_low", "kcal_burned", "active_kcal",
                  "vo2max_run", "vo2max_ride", "readiness"):
            row[f] = wr.get(f)
        for f, (hc, gc) in MERGE_FIELDS.items():
            row[f] = hr.get(hc) if hr.get(hc) is not None else wr.get(gc)
        row["water_l"] = hr.get("water_l") if hr.get("water_l") is not None else (
            wr["water_ml"] / 1000 if wr.get("water_ml") else None)
        for f in ("sleep_quality", "caffeine_mg", "energy", "soreness", "notes"):
            row[f] = hr.get(f)
        # Sleep duration from logged bed/wake times when the watch didn't record the night.
        if row["sleep_s"] is None and row["bedtime"] and row["wake_time"]:
            b = clock_to_minutes(row["bedtime"], bedtime=True)
            wk = clock_to_minutes(row["wake_time"]) + 24 * 60
            if b is not None and 0 < wk - b < 16 * 60:
                row["sleep_s"] = (wk - b) * 60
                row["sleep_estimated"] = True
        row["bed_min"] = clock_to_minutes(row["bedtime"], bedtime=True)
        row["wake_min"] = clock_to_minutes(row["wake_time"])
        out.append(row)
    return out


def habit_correlations(days: list[dict]) -> list[dict]:
    """Compare recovery after nights with vs. without a habit.

    Logging convention (same as Garmin): a day's row holds *last night's* sleep (bed/wake time,
    sleep score, overnight HRV and resting HR are all keyed by the morning you woke up) plus
    *that day's* drinks and food. So drinks on day D affect the sleep/HRV recorded on day D+1,
    while a late bedtime on day D's row already describes the night measured on day D.
    """
    nxt = {r["date"]: days[i + 1] for i, r in enumerate(days[:-1])}
    results = []

    def split(flag_fn, metric: str, label: str, unit: str, better: str, next_day: bool):
        yes, no = [], []
        for r in days:
            target = nxt.get(r["date"]) if next_day else r
            flag = flag_fn(r)
            if target is None or flag is None or target.get(metric) is None:
                continue
            (yes if flag else no).append(target[metric])
        if len(yes) >= 3 and len(no) >= 3:
            diff = mean(yes) - mean(no)
            worse = diff < 0 if better == "higher" else diff > 0
            results.append({"habit": label, "metric": metric, "unit": unit, "with": round(mean(yes), 1),
                            "without": round(mean(no), 1), "diff": round(diff, 1), "n_with": len(yes),
                            "n_without": len(no), "worse": worse})

    drank = lambda r: None if r.get("drinks") is None else r["drinks"] >= 2
    late = lambda r: None if r.get("bed_min") is None else r["bed_min"] > 24 * 60   # after midnight
    short = lambda r: None if r.get("sleep_s") is None else r["sleep_s"] < 7 * 3600

    split(drank, "hrv", "2+ drinks the evening before", "ms", "higher", True)
    split(drank, "resting_hr", "2+ drinks the evening before", "bpm", "lower", True)
    split(drank, "sleep_score", "2+ drinks the evening before", "pts", "higher", True)
    split(late, "sleep_score", "Bed after midnight", "pts", "higher", False)
    split(late, "hrv", "Bed after midnight", "ms", "higher", False)
    split(short, "hrv", "Under 7 h sleep", "ms", "higher", False)
    split(short, "body_battery_high", "Under 7 h sleep", "pts", "higher", False)
    return results


# ---- loading from the DB -----------------------------------------------------

def load_context(conn: sqlite3.Connection, settings: dict, today: date | None = None, days: int = 180) -> dict:
    from .db import rows

    today = today or date.today()
    start = today - timedelta(days=days)
    acts = rows(conn, "SELECT * FROM activities WHERE date <= ? ORDER BY start_time", (today.isoformat(),))
    wellness = rows(conn, "SELECT * FROM daily_metrics WHERE date BETWEEN ? AND ?", (start.isoformat(), today.isoformat()))
    habits = rows(conn, "SELECT * FROM habits WHERE date BETWEEN ? AND ?", (start.isoformat(), today.isoformat()))
    for a in acts:
        a.pop("raw", None)
        a["load"] = round(activity_load(a, settings), 1)
        a["intensity"] = intensity_class(a, settings)
    return {
        "today": today,
        "settings": settings,
        "activities": acts,
        "recent_activities": [a for a in acts if d(a["date"]) >= start],
        "load": load_series(acts, settings, start, today),
        "days": merge_days(wellness, habits, start, today),
    }
