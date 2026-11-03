"""Environment-driven configuration."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name) or default).expanduser()
    return p if p.is_absolute() else ROOT / p


DATABASE_PATH = _path("DATABASE_PATH", "data/fitness.db")
GARMIN_TOKEN_DIR = _path("GARMIN_TOKEN_DIR", "data/garmin_tokens")
GARMIN_EMAIL = os.getenv("GARMIN_EMAIL") or None
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD") or None

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID") or None
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET") or None
STRAVA_REDIRECT_URI = os.getenv("STRAVA_REDIRECT_URI") or "http://localhost:8000/auth/strava/callback"

INITIAL_SYNC_DAYS = int(os.getenv("INITIAL_SYNC_DAYS") or 120)
AUTO_SYNC_HOURS = float(os.getenv("AUTO_SYNC_HOURS") or 0)

# Athlete defaults; editable in the Settings tab (stored in the DB).
DEFAULT_SETTINGS = {
    "max_hr": 185,
    "resting_hr": 55,
    "sex": "male",            # used by the Banister TRIMP weighting
    "weight_kg": 75,
    "sleep_target_h": 8.0,
    "protein_target_g_per_kg": 1.6,
    "weekly_drink_limit": 7,
    # Macro calculator
    "height_cm": None,        # needed for resting metabolism (Mifflin-St Jeor)
    "birth_year": None,
    "activity_level": "auto",  # non-exercise activity: auto (from Garmin), sedentary, light, active, very_active
    "goal": "maintain",       # maintain, lose, gain
    "goal_rate_kg_week": 0.25,
    "fat_pct": 25,            # share of calories from fat (a floor of 0.6 g/kg always applies)
}
