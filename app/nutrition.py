"""Activity-adjusted daily calorie and macro targets.

For each day:
  energy  = resting metabolism (Mifflin-St Jeor) x non-exercise activity factor
          + that day's training (what you did for past days; your planned sessions for upcoming days)
          ± your goal (deficit or surplus; the deficit is halved on big training days so you can fuel them)
  protein = your g/kg target, every day
  fat     = your % of calories, never below 0.6 g/kg
  carbs   = the rest, checked against sports-nutrition guidance for that day's training load
            (roughly 3-5 g/kg on easy days up to 8-12 g/kg on very long days). If carbs would fall short,
            fat is trimmed toward its floor first; the day before a long race gets a carb load.

Training calories use your own history where possible: kcal per hour by sport from your recorded activities.
Otherwise they come from standard MET values. These are planning estimates, not medical advice.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from statistics import median
from typing import Any

from . import analytics

# MET values (easy, moderate, hard) for estimating training calories without personal history.
SPORT_MET = {
    "run": (8.0, 10.0, 12.0), "ride": (6.0, 8.0, 10.5), "swim": (6.0, 8.0, 10.0), "ruck": (6.0, 7.5, 9.0),
    "walk": (3.3, 4.3, 5.5), "hike": (5.0, 6.0, 7.5), "strength": (3.5, 5.0, 6.0), "row": (6.0, 8.5, 11.0),
    "ski": (6.0, 8.0, 10.0), "yoga": (2.5, 3.0, 4.0), "multisport": (7.0, 9.0, 11.0), "other": (5.0, 7.0, 9.0),
}
INTENSITY_INDEX = {"easy": 0, "moderate": 1, "hard": 2, "race": 2}
# Scaling of a sport's personal average kcal/h by session intensity.
INTENSITY_SCALE = {"easy": 0.85, "moderate": 1.0, "hard": 1.2, "race": 1.3}
ACTIVITY_FACTORS = {"sedentary": 1.2, "light": 1.35, "active": 1.5, "very_active": 1.65}
KCAL_PER_KG_FAT = 7700
DEFAULT_HEIGHT_CM = {"male": 178, "female": 165}
DEFAULT_AGE = 30


# ---- body and baseline -------------------------------------------------------

def bmr(settings: dict, today: date) -> tuple[float, list[str]]:
    """Resting metabolism (Mifflin-St Jeor). Uses defaults, and says so, if height or age aren't set."""
    notes = []
    sex = settings.get("sex", "male")
    kg = float(settings["weight_kg"])
    height = settings.get("height_cm")
    if not height:
        height = DEFAULT_HEIGHT_CM.get(sex, 175)
        notes.append(f"Height isn't set, so {height} cm is assumed. Add it in Settings → Athlete profile.")
    age = today.year - int(settings["birth_year"]) if settings.get("birth_year") else None
    if age is None:
        age = DEFAULT_AGE
        notes.append(f"Birth year isn't set, so age {age} is assumed. Add it in Settings → Athlete profile.")
    value = 10 * kg + 6.25 * float(height) - 5 * age + (5 if sex == "male" else -161)
    return value, notes


def activity_factor(settings: dict, days: list[dict], activities: list[dict], base: float) -> tuple[float, str]:
    """Non-exercise activity multiplier. "auto" learns it from Garmin's daily burn on days without training."""
    level = settings.get("activity_level") or "auto"
    if level != "auto":
        return ACTIVITY_FACTORS[level], level.replace("_", " ")
    trained = {a["date"] for a in activities}
    rest_burns = [r["kcal_burned"] for r in days[-60:]
                  if r.get("kcal_burned") and r["date"] not in trained and r["kcal_burned"] > base * 0.9]
    if len(rest_burns) >= 5:
        f = max(1.15, min(1.8, median(rest_burns) / base))
        return round(f, 2), f"learned from your watch ({len(rest_burns)} rest days)"
    return ACTIVITY_FACTORS["light"], "light (default: not enough rest days with watch data yet)"


def personal_kcal_per_hour(activities: list[dict], base: float, today: date) -> dict[str, float]:
    """Net (above resting) training kcal per hour by sport, from the last 120 days of your recorded sessions."""
    by_sport: dict[str, list[float]] = {}
    for a in activities:
        hours = (a.get("moving_s") or a.get("duration_s") or 0) / 3600
        if not a.get("calories") or hours < 0.25 or analytics.d(a["date"]) < today - timedelta(days=120):
            continue
        net = a["calories"] - base / 24 * hours
        if net > 0:
            by_sport.setdefault(a.get("sport") or "other", []).append(net / hours)
    return {sport: median(v) for sport, v in by_sport.items() if len(v) >= 3}


def session_kcal(sport: str, minutes: float, intensity: str | None, kg: float, calib: dict[str, float]) -> float:
    """Net training calories for one session."""
    if not minutes or sport == "rest":
        return 0.0
    intensity = intensity if intensity in INTENSITY_INDEX else "moderate"
    hours = minutes / 60
    if sport in calib:
        return calib[sport] * INTENSITY_SCALE[intensity] * hours
    met = SPORT_MET.get(sport, SPORT_MET["other"])[INTENSITY_INDEX[intensity]]
    if intensity == "race":
        met *= 1.08
    return max(0.0, (met - 1) * kg * hours)


# ---- per-day targets -----------------------------------------------------------

def carb_band(minutes: float, hard_minutes: float) -> tuple[float, float, str]:
    """Daily carbohydrate guidance in g/kg for the training load (ACSM / IOC consensus ranges)."""
    if minutes >= 240 or (minutes >= 180 and hard_minutes >= 60):
        return 8, 12, "very high training load"
    if minutes >= 90 or hard_minutes >= 45:
        return 6, 10, "high training load"
    if minutes >= 45:
        return 5, 7, "moderate training"
    if minutes > 0:
        return 3, 5, "light training"
    return 3, 5, "rest day"


def fuelling_notes(sessions: list[dict]) -> list[str]:
    notes = []
    for s in sessions:
        m = s.get("duration_min") or 0
        label = s.get("title") or s.get("sport")
        if m >= 150:
            notes.append(f"{label}: take 60–90 g carbs per hour during it, plus fluids and electrolytes.")
        elif m >= 75:
            notes.append(f"{label}: take 30–60 g carbs per hour during it.")
        if s.get("intensity") in ("hard", "race") and m >= 30:
            notes.append(f"{label}: have a carb-based meal (1–2 g/kg) 2–4 h before.")
    if len([s for s in sessions if (s.get("duration_min") or 0) >= 30]) >= 2:
        notes.append("Two sessions: have 20–40 g protein plus carbs within an hour after the first one.")
    return notes


def day_targets(day: date, sessions: list[dict], source: str, settings: dict, base: float, factor: float,
                calib: dict[str, float], race_soon: dict | None = None) -> dict[str, Any]:
    kg = float(settings["weight_kg"])
    minutes = sum(s.get("duration_min") or 0 for s in sessions if s.get("sport") != "rest")
    hard = sum(s.get("duration_min") or 0 for s in sessions if s.get("intensity") in ("hard", "race"))
    training = sum(session_kcal(s.get("sport") or "other", s.get("duration_min") or 0, s.get("intensity"), kg, calib)
                   for s in sessions)
    maintenance = base * factor + training

    goal, rate = settings.get("goal") or "maintain", float(settings.get("goal_rate_kg_week") or 0)
    adjust = 0.0
    if goal == "lose" and rate:
        adjust = -rate * KCAL_PER_KG_FAT / 7
        if minutes >= 120 or hard >= 45:
            adjust /= 2             # fuel big days; take the deficit on lighter ones
    elif goal == "gain" and rate:
        adjust = rate * KCAL_PER_KG_FAT / 7
    floor = 1500 if settings.get("sex") == "male" else 1200
    kcal = max(floor, base * 1.1, maintenance + adjust)

    protein = float(settings["protein_target_g_per_kg"]) * kg
    fat_min = 0.6 * kg
    fat = max(fat_min, kcal * float(settings.get("fat_pct") or 25) / 100 / 9)
    carbs = (kcal - protein * 4 - fat * 9) / 4
    low, high, band_label = carb_band(minutes, hard)
    notes: list[str] = []

    if race_soon:
        low, high, band_label = 8, 10, f"carb load for {race_soon.get('title') or 'race'} tomorrow"
    if carbs < low * kg and fat > fat_min:
        # Make room for carbs by trimming fat toward its floor.
        shift = min(fat - fat_min, (low * kg - carbs) * 4 / 9)
        fat -= shift
        carbs += shift * 9 / 4
    if race_soon and carbs < low * kg:
        carbs = low * kg
        kcal = protein * 4 + fat * 9 + carbs * 4
        notes.append("Calories raised for the carb load. Keep fat and fibre low today.")
    elif carbs < low * kg:
        notes.append(f"Carbs are below the {low:g}–{high:g} g/kg guide for a {band_label}. "
                     + ("Consider a smaller deficit today." if adjust < 0 else "Consider eating a bit more."))
    notes += fuelling_notes(sessions)

    return {
        "date": day.isoformat(), "weekday": day.strftime("%a"), "source": source,
        "sessions": sessions, "training_min": round(minutes), "training_kcal": round(training),
        "kcal": round(kcal), "protein_g": round(protein), "carbs_g": round(carbs), "fat_g": round(fat),
        "carbs_g_per_kg": round(carbs / kg, 1), "carb_band": [low, high], "carb_band_label": band_label,
        "maintenance_kcal": round(maintenance), "goal_adjust_kcal": round(adjust), "notes": notes,
    }


def _actual_sessions(activities: list[dict], day: str) -> list[dict]:
    return [{"sport": a.get("sport") or "other", "title": a.get("name"),
             "duration_min": round((a.get("moving_s") or a.get("duration_s") or 0) / 60),
             "distance_km": round(a["distance_m"] / 1000, 1) if a.get("distance_m") else None,
             "intensity": a.get("intensity") or "moderate", "actual_kcal": a.get("calories")}
            for a in activities if a["date"] == day]


def week_plan(conn: sqlite3.Connection, settings: dict, week_start: date, today: date | None = None) -> dict:
    """Targets for the 7 days from `week_start`: actual training for past days, your plan from today on."""
    from .db import rows

    today = today or date.today()
    ctx = analytics.load_context(conn, settings, today=today, days=120)
    base, warnings = bmr(settings, today)
    factor, factor_source = activity_factor(settings, ctx["days"], ctx["activities"], base)
    calib = personal_kcal_per_hour(ctx["activities"], base, today)
    end = week_start + timedelta(days=7)
    planned = rows(conn, "SELECT * FROM planned_sessions WHERE date >= ? AND date <= ? ORDER BY date, id",
                   (week_start.isoformat(), end.isoformat()))
    # Completed activities only exist up to today, which is what load_context returns.
    acts = [a for a in ctx["activities"] if week_start.isoformat() <= a["date"] < end.isoformat()]

    days = []
    for i in range(7):
        day = week_start + timedelta(days=i)
        key = day.isoformat()
        plan_today = [p for p in planned if p["date"] == key]
        done = _actual_sessions(acts, key)
        if day < today or (day == today and done):
            sessions, source = done, "done"
        else:
            sessions, source = plan_today, "planned"
        tomorrow = (day + timedelta(days=1)).isoformat()
        race = next((p for p in planned if p["date"] == tomorrow and p.get("intensity") == "race"
                     and (p.get("duration_min") or 0) >= 90), None) if day >= today else None
        days.append(day_targets(day, sessions, source, settings, base, factor, calib, race))

    total = {k: sum(d[k] for d in days) for k in ("kcal", "protein_g", "carbs_g", "fat_g", "training_min")}
    return {
        "week_start": week_start.isoformat(), "days": days,
        "average": {k: round(v / 7) for k, v in total.items()}, "total": total,
        "assumptions": {
            "bmr_kcal": round(base), "activity_factor": factor, "activity_factor_source": factor_source,
            "goal": settings.get("goal"), "goal_rate_kg_week": settings.get("goal_rate_kg_week"),
            "personal_kcal_per_hour": {k: round(v) for k, v in calib.items()},
        },
        "warnings": warnings,
        # The query also reads the next Monday (to spot a race right after the week); don't list it here.
        "planned_sessions": [p for p in planned if p["date"] < end.isoformat()],
    }


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())
