"""Generate ~6 months of realistic synthetic data so the dashboard can be explored without accounts.

The story baked in: steady base building, a too-aggressive block in the last ~3 weeks, more
weekend drinking and later bedtimes recently — so several diagnostics fire.
"""
from __future__ import annotations

import math
import random
import sqlite3
from datetime import date, datetime, timedelta

from . import db


def seed(conn: sqlite3.Connection, days: int = 180, today: date | None = None, rng_seed: int = 7) -> None:
    rnd = random.Random(rng_seed)
    today = today or date.today()
    start = today - timedelta(days=days)
    for t in ("activities", "daily_metrics", "habits"):
        conn.execute(f"DELETE FROM {t}")
    db.save_settings({"max_hr": 188, "resting_hr": 50, "sex": "male", "weight_kg": 74, "sleep_target_h": 8,
                      "protein_target_g_per_kg": 1.6, "weekly_drink_limit": 7}, conn)

    fatigue = 0.0
    fitness_gain = 0.0
    prev_drinks = 0.0
    aid = 1000
    for i in range(days + 1):
        day = start + timedelta(days=i)
        dow = day.weekday()
        days_left = (today - day).days
        block = 0.8 + 0.0015 * i            # gradual build
        if days_left <= 21:
            block *= 1.3                    # overreaching block
        recovery_week = (i // 7) % 4 == 3 and days_left > 21
        if recovery_week:
            block *= 0.6

        # ---- training ----
        plan = {0: None, 1: ("run", 50, 0.83), 2: ("ride", 75, 0.70), 3: ("run", 45, 0.74),
                4: ("strength", 40, 0.62), 5: ("ride", 150, 0.72), 6: ("run", 90, 0.73)}[dow]
        if days_left <= 21 and dow == 0:
            plan = ("run", 40, 0.80)        # no rest day in the hard block
        day_load = 0.0
        if plan and rnd.random() > 0.08:
            sport, mins, pct = plan
            mins = mins * block * rnd.uniform(0.85, 1.15)
            pct = pct + rnd.uniform(-0.03, 0.03) + (0.04 if days_left <= 21 else 0)
            hr = 188 * pct
            fit = 1 + fitness_gain
            speed = (2.9 + 1.4 * (pct - 0.65)) * fit * (0.97 if days_left <= 21 else 1)  # m/s, fatigue costs efficiency
            power = 140 + 260 * (pct - 0.55) * fit
            dur = mins * 60
            start_dt = datetime.combine(day, datetime.min.time()) + timedelta(hours=rnd.choice([6, 7, 17, 18]),
                                                                                 minutes=rnd.randint(0, 50))
            if sport == "ride":
                speed = 6.5 + (power - 140) / 60
            dist = speed * dur if sport in ("run", "ride") else None
            aid += 1
            db.upsert(conn, "activities", {
                "id": f"garmin:{aid}", "source": "garmin", "garmin_id": str(aid),
                "strava_id": str(aid + 900000) if sport != "strength" else None,
                "name": {"run": "Run", "ride": "Ride", "strength": "Strength"}[sport] +
                        (" — long" if mins > 80 else " — intervals" if pct > 0.82 else ""),
                "sport": sport, "start_time": start_dt.isoformat(), "date": day.isoformat(),
                "duration_s": dur * 1.04, "moving_s": dur, "distance_m": round(dist, 0) if dist else None,
                "elevation_m": rnd.randint(20, 400) if sport != "strength" else None,
                "avg_hr": round(hr), "max_hr": round(min(188, hr * 1.12)),
                "avg_speed": round(speed, 2) if dist else None,
                "avg_power": round(power) if sport == "ride" else None,
                "calories": round(mins * (11 if pct > 0.75 else 9)), "garmin_load": None,
                "aerobic_te": round(2 + 3 * (pct - 0.6), 1), "anaerobic_te": round(max(0, 4 * (pct - 0.75)), 1),
            }, key="id")
            day_load = mins * pct
            fitness_gain += 0.0006 if days_left > 21 else 0.0001
        fatigue = fatigue * 0.8 + day_load / 60

        # ---- habits ----
        weekend = dow in (4, 5)
        recent = days_left <= 28
        drinks = 0.0
        if weekend and rnd.random() < (0.85 if recent else 0.55):
            drinks = rnd.choice([1, 2, 2, 3, 4] + ([5, 6] if recent else []))
        elif rnd.random() < 0.1:
            drinks = 1
        bed = 22 * 60 + 45 + rnd.gauss(0, 20) + (70 if prev_drinks >= 2 else 0) + (40 if recent else 0) \
            + (60 if dow in (5, 6) else 0)
        wake = 6 * 60 + 30 + rnd.gauss(0, 15) + (90 if dow in (5, 6) else 0)
        sleep_s = max(4.5 * 3600, ((wake + 24 * 60 - bed) - rnd.uniform(15, 40)) * 60)
        sleep_h = sleep_s / 3600

        rhr = 50 - 4 * fitness_gain * 10 + 0.9 * fatigue + 2.5 * min(prev_drinks, 5) / 2 + rnd.gauss(0, 1.2)
        hrv = 66 + 30 * fitness_gain * 10 - 2.2 * fatigue - 4.5 * min(prev_drinks, 6) / 2 \
            - 2.5 * max(0, 7 - sleep_h) + rnd.gauss(0, 4)
        score = max(30, min(98, 82 + 6 * (sleep_h - 7.5) - 4 * prev_drinks + rnd.gauss(0, 5)))
        burned = 2350 + day_load * 9
        intake = burned - rnd.gauss(250, 200) - (450 if recent else 0)

        def clock(m):
            m = round(m) % 1440
            return f"{m // 60:02d}:{m % 60:02d}"

        db.upsert(conn, "daily_metrics", {
            "date": day.isoformat(), "resting_hr": round(rhr), "hrv": round(hrv),
            "hrv_baseline_low": 60, "hrv_baseline_high": 80, "hrv_status": "BALANCED",
            "sleep_s": round(sleep_s), "deep_s": round(sleep_s * 0.17), "rem_s": round(sleep_s * 0.22),
            "light_s": round(sleep_s * 0.55), "awake_s": round(sleep_s * 0.06), "sleep_score": round(score),
            "bedtime": clock(bed), "wake_time": clock(wake), "steps": round(rnd.gauss(9000, 2500)),
            "stress_avg": round(24 + 2.5 * fatigue + rnd.gauss(0, 4)),
            "body_battery_high": round(max(20, min(100, 95 - 4 * fatigue - 5 * prev_drinks + rnd.gauss(0, 5)))),
            "body_battery_low": round(rnd.uniform(5, 25)), "kcal_burned": round(burned),
            "active_kcal": round(day_load * 9), "vo2max_run": round(52 + 25 * fitness_gain, 1),
            "readiness": None, "updated_at": db.now(),
        })
        if rnd.random() < 0.85:   # realistic: not every day gets logged
            db.upsert(conn, "habits", {
                "date": day.isoformat(), "bedtime": None, "wake_time": None,
                "sleep_quality": max(1, min(5, round(score / 20))), "drinks": drinks,
                "calories": round(intake, -1), "protein_g": round(rnd.gauss(105 if recent else 125, 15)),
                "carbs_g": round(intake * 0.5 / 4), "fat_g": round(intake * 0.3 / 9),
                "water_l": round(rnd.uniform(1.5, 3.2), 1), "caffeine_mg": rnd.choice([0, 95, 190, 285]),
                "energy": max(1, min(5, round(4 - fatigue / 4 + rnd.gauss(0, 0.6)))),
                "soreness": max(1, min(5, round(1.5 + fatigue / 3 + rnd.gauss(0, 0.6)))),
                "notes": None, "updated_at": db.now(),
            })
        prev_drinks = drinks
    conn.commit()
