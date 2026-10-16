"""FastAPI app: JSON API + the static dashboard."""
from __future__ import annotations

import asyncio
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, config, db, diagnostics
from .sync import garmin, runner, strava

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if config.AUTO_SYNC_HOURS > 0:
        async def loop():
            while True:
                await asyncio.sleep(config.AUTO_SYNC_HOURS * 3600)
                await asyncio.to_thread(runner.run, "all")
        task = asyncio.create_task(loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Fitness Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


# ---- dashboard ---------------------------------------------------------------

@app.get("/api/dashboard")
def dashboard(days: int = Query(90, ge=14, le=730)) -> dict[str, Any]:
    conn = db.get_conn()
    settings = db.get_settings(conn)
    # Always analyse at least 120 days so baselines (HRV 60d, CTL 42d) are meaningful.
    ctx = analytics.load_context(conn, settings, days=max(days, 120))
    findings = diagnostics.diagnose(ctx)
    cutoff = (ctx["today"] - timedelta(days=days - 1)).isoformat()
    load = [r for r in ctx["load"] if r["date"] >= cutoff]
    days_rows = [r for r in ctx["days"] if r["date"] >= cutoff]
    today_load = ctx["load"][-1] if ctx["load"] else {"ctl": 0, "atl": 0, "tsb": 0}
    week_ago = ctx["load"][-8] if len(ctx["load"]) >= 8 else today_load
    last7 = ctx["days"][-7:]

    def latest(field):
        for r in reversed(ctx["days"]):
            if r.get(field) is not None:
                return r[field]
        return None

    tiles = {
        "fitness": today_load["ctl"], "fitness_change": round(today_load["ctl"] - week_ago["ctl"], 1),
        "fatigue": today_load["atl"], "form": today_load["tsb"],
        "resting_hr": latest("resting_hr"), "hrv": latest("hrv"),
        "sleep_avg_h": round(analytics.avg(r.get("sleep_s") for r in last7) / 3600, 1)
        if analytics.avg(r.get("sleep_s") for r in last7) else None,
        "drinks_7d": sum(r["drinks"] or 0 for r in last7 if r.get("drinks") is not None)
        if any(r.get("drinks") is not None for r in last7) else None,
        "vo2max": latest("vo2max_run") or latest("vo2max_ride"),
        "week_hours": round(sum((a.get("moving_s") or a.get("duration_s") or 0) for a in ctx["activities"]
                                if analytics.d(a["date"]) > ctx["today"] - timedelta(days=7)) / 3600, 1),
    }
    return {
        "today": ctx["today"].isoformat(),
        "settings": settings,
        "readiness": diagnostics.readiness_score(ctx, findings),
        "findings": findings,
        "tiles": tiles,
        "load": load,
        "weekly": analytics.weekly_summary(ctx["activities"], settings, max(4, days // 7), ctx["today"]),
        "efficiency": [p for p in analytics.efficiency_series(ctx["activities"], settings) if p["date"] >= cutoff],
        "days": days_rows,
        "correlations": analytics.habit_correlations(ctx["days"]),
        "activities": sorted([a for a in ctx["activities"] if a["date"] >= cutoff],
                             key=lambda a: a["start_time"], reverse=True),
    }


# ---- habits ------------------------------------------------------------------

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class Habit(BaseModel):
    bedtime: str | None = Field(None, pattern=HHMM)
    wake_time: str | None = Field(None, pattern=HHMM)
    sleep_quality: int | None = Field(None, ge=1, le=5)
    drinks: float | None = Field(None, ge=0, le=50)
    calories: float | None = Field(None, ge=0, le=15000)
    protein_g: float | None = Field(None, ge=0, le=1000)
    carbs_g: float | None = Field(None, ge=0, le=2000)
    fat_g: float | None = Field(None, ge=0, le=1000)
    water_l: float | None = Field(None, ge=0, le=20)
    caffeine_mg: float | None = Field(None, ge=0, le=3000)
    energy: int | None = Field(None, ge=1, le=5)
    soreness: int | None = Field(None, ge=1, le=5)
    notes: str | None = Field(None, max_length=2000)


def _check_date(day: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "invalid date")
    return day


@app.get("/api/habits/{day}")
def get_habit(day: str) -> dict[str, Any]:
    _check_date(day)
    conn = db.get_conn()
    h = conn.execute("SELECT * FROM habits WHERE date=?", (day,)).fetchone()
    g = conn.execute("SELECT bedtime, wake_time, kcal_in, protein_g, carbs_g, fat_g, water_ml, drinks "
                     "FROM daily_metrics WHERE date=?", (day,)).fetchone()
    return {"date": day, "manual": dict(h) if h else None, "garmin": dict(g) if g else None}


@app.put("/api/habits/{day}")
def put_habit(day: str, habit: Habit) -> dict[str, Any]:
    _check_date(day)
    row = {"date": day, **habit.model_dump(), "updated_at": db.now()}
    with db.tx() as conn:
        db.upsert(conn, "habits", row)
    return {"ok": True}


@app.delete("/api/habits/{day}")
def delete_habit(day: str) -> dict[str, Any]:
    _check_date(day)
    with db.tx() as conn:
        conn.execute("DELETE FROM habits WHERE date=?", (day,))
    return {"ok": True}


# ---- settings ----------------------------------------------------------------

class Settings(BaseModel):
    max_hr: int = Field(ge=120, le=230)
    resting_hr: int = Field(ge=25, le=110)
    sex: str = Field(pattern="^(male|female)$")
    weight_kg: float = Field(ge=30, le=250)
    sleep_target_h: float = Field(ge=5, le=11)
    protein_target_g_per_kg: float = Field(ge=0.5, le=3.5)
    weekly_drink_limit: float = Field(ge=0, le=50)


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return db.get_settings()


@app.put("/api/settings")
def put_settings(s: Settings) -> dict[str, Any]:
    db.save_settings(s.model_dump())
    return db.get_settings()


# ---- connections & sync ------------------------------------------------------

@app.get("/api/status")
def status() -> dict[str, Any]:
    conn = db.get_conn()
    tok = db.get_token("strava")
    athlete = (tok or {}).get("athlete") or {}
    return {
        "garmin": {"connected": garmin.has_tokens(), "env_credentials": bool(config.GARMIN_EMAIL)},
        "strava": {"configured": strava.configured(), "connected": bool(tok),
                   "athlete": " ".join(filter(None, [athlete.get("firstname"), athlete.get("lastname")])) or None},
        "auto_sync_hours": config.AUTO_SYNC_HOURS,
        "counts": {
            "activities": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
            "wellness_days": conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0],
            "habit_days": conn.execute("SELECT COUNT(*) FROM habits").fetchone()[0],
        },
        "log": db.rows(conn, "SELECT * FROM sync_log ORDER BY id DESC LIMIT 10"),
    }


@app.post("/api/sync")
def sync(provider: str = Query("all", pattern="^(all|garmin|strava)$"),
         days: int | None = Query(None, ge=1, le=3650)) -> dict[str, Any]:
    return {"results": runner.run(provider, days)}


class GarminLogin(BaseModel):
    email: str
    password: str


class GarminMFA(BaseModel):
    code: str = Field(pattern=r"^\d{4,8}$")


@app.post("/api/garmin/login")
def garmin_login(body: GarminLogin) -> dict[str, Any]:
    try:
        return {"status": garmin.web_login(body.email, body.password)}
    except Exception as e:
        raise HTTPException(400, f"Garmin login failed: {e}")


@app.post("/api/garmin/mfa")
def garmin_mfa(body: GarminMFA) -> dict[str, Any]:
    try:
        return {"status": garmin.web_mfa(body.code)}
    except Exception as e:
        raise HTTPException(400, f"Garmin MFA failed: {e}")


@app.post("/api/garmin/disconnect")
def garmin_disconnect() -> dict[str, Any]:
    garmin.disconnect()
    return {"ok": True}


_oauth_states: set[str] = set()


@app.get("/auth/strava/login", include_in_schema=False)
def strava_login():
    if not strava.configured():
        return HTMLResponse("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env first (see README).", 400)
    state = secrets.token_urlsafe(16)
    _oauth_states.add(state)
    return RedirectResponse(strava.authorize_url(state))


@app.get("/auth/strava/callback", include_in_schema=False)
def strava_callback(code: str | None = None, state: str | None = None, error: str | None = None,
                    scope: str | None = None):
    if error or not code:
        return RedirectResponse("/#settings?strava=denied")
    if state not in _oauth_states:
        return HTMLResponse("Invalid OAuth state — start the Strava connection again.", 400)
    _oauth_states.discard(state)
    if scope is not None and "activity:read" not in scope:
        return RedirectResponse("/#settings?strava=scope")
    strava.exchange_code(code)
    return RedirectResponse("/#settings?strava=connected")


@app.post("/api/strava/disconnect")
def strava_disconnect() -> dict[str, Any]:
    db.save_token("strava", None)
    return {"ok": True}
