"""AI coach: a Claude conversation with tools over your training, recovery and habit data.

Claude looks up the numbers it needs (activities, sleep, HRV, load, habits) before answering, and
can log habits you mention in chat ("3 beers last night, in bed at 1am") into the habits table.
Conversations are stored in SQLite and replayed append-only.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, timedelta
from statistics import mean
from typing import Any, Callable

from . import analytics, db, diagnostics

MODEL = os.getenv("COACH_MODEL") or "claude-opus-5-5"
EFFORT = os.getenv("COACH_EFFORT") or "medium"
MAX_TOOL_ROUNDS = 12

SYSTEM_PROMPT = """You are an endurance and strength coach built into the athlete's personal training dashboard. \
You have tools that read their real data (synced from Garmin and Strava, plus their manual habit log) and a tool \
that logs habits.

How to work:
- Look up the data before answering. Don't guess numbers; call the tools, then reason from what they return.
- When asked why a session went badly (or well), call explain_activity for that session first. Then pull the \
  surrounding weeks of load, sleep, HRV, resting HR, alcohol and nutrition as needed. Look for the usual causes: \
  residual fatigue from a recent big effort or race, a load spike, short or late sleep, alcohol, under-fuelling, \
  heat, illness signs (resting HR up, HRV down), and pacing (started too hard). Rank the likely causes, cite the \
  specific numbers and dates, and say how confident you are. Then give concrete advice for the next few days.
- When the athlete tells you about habits (drinks, food, bed or wake times, sleep quality, energy, soreness, \
  water, caffeine), log them with log_habits. Then confirm in one line what you saved and for which date. \
  If the date is genuinely unclear, ask.
- Habit date convention: a day's entry holds last night's sleep (the night that ended that morning) plus that \
  day's food and drinks. "Last night I had 3 beers" is drinks on yesterday's date. "I went to bed at 1am" is \
  the bedtime on today's entry, if they woke up today.
- Be direct and specific, like a good coach texting an athlete. Use short paragraphs or brief bullet lists. \
  Use the athlete's units (km, bpm, ms, hours).
- Correlation in a single person's data is suggestive, not proof. Say so when it matters.
- You're not a doctor. If they mention chest pain, fainting, palpitations at rest, or an injury that is getting \
  worse, tell them to see a medical professional."""


# ---- tool implementations ----------------------------------------------------

def _ctx(conn: sqlite3.Connection, days: int = 400) -> dict:
    return analytics.load_context(conn, db.get_settings(conn), days=days)


def _parse_date(s: str | None, default: date) -> date:
    if not s:
        return default
    return date.fromisoformat(str(s)[:10])


def _clip(start: date, end: date, max_days: int = 180) -> tuple[date, date]:
    if end < start:
        start, end = end, start
    if (end - start).days > max_days:
        start = end - timedelta(days=max_days)
    return start, end


def _round(v: Any) -> Any:
    return round(v, 2) if isinstance(v, float) else v


def _pace(a: dict) -> str | None:
    if a.get("sport") in ("run", "walk", "hike", "ruck") and a.get("avg_speed"):
        s = 1000 / a["avg_speed"]
        return f"{int(s // 60)}:{int(s % 60):02d}/km"
    return None


def _activity_summary(a: dict) -> dict:
    dur = a.get("moving_s") or a.get("duration_s") or 0
    return {
        "id": a["id"], "date": a["date"], "start": a["start_time"][11:16], "name": a.get("name"),
        "sport": a.get("sport"), "duration_min": round(dur / 60), "distance_km": round((a.get("distance_m") or 0) / 1000, 2) or None,
        "pace": _pace(a), "avg_speed_kmh": round(a["avg_speed"] * 3.6, 1) if a.get("avg_speed") else None,
        "avg_hr": a.get("avg_hr"), "max_hr": a.get("max_hr"), "avg_power": a.get("avg_power"),
        "elevation_m": a.get("elevation_m"), "load": a.get("load"), "intensity": a.get("intensity"),
        "aerobic_te": a.get("aerobic_te"), "anaerobic_te": a.get("anaerobic_te"),
        "strava_relative_effort": a.get("suffer_score"),
    }


DAY_FIELDS = ["date", "sleep_h", "sleep_score", "bedtime", "wake_time", "hrv", "hrv_baseline_low", "hrv_baseline_high",
              "resting_hr", "stress_avg", "body_battery_high", "readiness", "steps", "drinks", "calories", "kcal_burned",
              "protein_g", "carbs_g", "fat_g", "water_l", "caffeine_mg", "sleep_quality", "energy", "soreness", "notes"]


def _day_summary(r: dict) -> dict:
    out = {k: _round(r.get(k)) for k in DAY_FIELDS if k != "sleep_h"}
    out["sleep_h"] = round(r["sleep_s"] / 3600, 2) if r.get("sleep_s") else None
    return {k: v for k, v in out.items() if v is not None}


def tool_get_overview(conn, _args) -> dict:
    ctx = _ctx(conn, 180)
    findings = diagnostics.diagnose(ctx)
    load = ctx["load"][-1] if ctx["load"] else {}
    return {
        "today": ctx["today"].isoformat(),
        "athlete_profile": ctx["settings"],
        "readiness": diagnostics.readiness_score(ctx, findings),
        "fitness_ctl": load.get("ctl"), "fatigue_atl": load.get("atl"), "form_tsb": load.get("tsb"),
        "open_findings": [{k: f[k] for k in ("severity", "category", "title", "detail")} for f in findings],
        "last_7_days": [_day_summary(r) for r in ctx["days"][-7:]],
        "recent_activities": [_activity_summary(a) for a in ctx["activities"][-10:]],
    }


def tool_get_activities(conn, args) -> dict:
    today = date.today()
    start, end = _clip(_parse_date(args.get("start_date"), today - timedelta(days=30)), _parse_date(args.get("end_date"), today))
    ctx = _ctx(conn, (today - start).days + 1)
    sport = (args.get("sport") or "").lower() or None
    acts = [a for a in ctx["activities"] if start.isoformat() <= a["date"] <= end.isoformat()
            and (sport is None or a.get("sport") == sport)]
    return {"start_date": start.isoformat(), "end_date": end.isoformat(), "count": len(acts),
            "activities": [_activity_summary(a) for a in acts]}


def tool_get_daily_metrics(conn, args) -> dict:
    today = date.today()
    start, end = _clip(_parse_date(args.get("start_date"), today - timedelta(days=14)), _parse_date(args.get("end_date"), today))
    ctx = _ctx(conn, (today - start).days + 1)
    days = [r for r in ctx["days"] if start.isoformat() <= r["date"] <= end.isoformat()]
    return {"note": "Sleep, HRV and resting HR are keyed by the morning you woke up. Drinks and food are for that calendar day.",
            "days": [_day_summary(r) for r in days]}


def tool_get_training_load(conn, args) -> dict:
    today = date.today()
    start, end = _clip(_parse_date(args.get("start_date"), today - timedelta(days=42)), _parse_date(args.get("end_date"), today), 365)
    ctx = _ctx(conn, (today - start).days + 1)
    rows = [r for r in ctx["load"] if start.isoformat() <= r["date"] <= end.isoformat()]
    weekly = analytics.weekly_summary(ctx["activities"], ctx["settings"], max(1, (end - start).days // 7 + 1), end)
    return {"explanation": "load=daily training load (TRIMP-like). ctl=fitness (42-day average), atl=fatigue (7-day), "
                           "tsb=form going into the day (ctl-atl; below -30 is deep fatigue).",
            "daily": rows, "weekly": [{k: w[k] for k in ("week", "hours", "distance_km", "load", "count", "by_sport")} for w in weekly]}


def tool_explain_activity(conn, args) -> dict:
    """Everything needed to judge one session: the session, comparable past sessions, and what preceded it."""
    ctx = _ctx(conn, 400)
    acts = ctx["activities"]
    target = None
    if args.get("activity_id"):
        target = next((a for a in acts if a["id"] == args["activity_id"]), None)
    else:
        day = _parse_date(args.get("date"), date.today()).isoformat()
        sport = (args.get("sport") or "").lower() or None
        cands = [a for a in acts if a["date"] == day and (sport is None or a.get("sport") == sport)]
        target = cands[-1] if cands else None
        if target is None:
            return {"error": f"No {sport or ''} activity found on {day}. Data may not be synced yet; try sync_data, "
                             f"or call get_activities to find it.",
                    "activities_that_day": [_activity_summary(a) for a in acts if a["date"] == day]}
    if target is None:
        return {"error": "Activity not found."}

    tday = analytics.d(target["date"])
    settings = ctx["settings"]
    # Comparable sessions: same sport, previous 90 days, similar duration (±40%).
    dur = target.get("moving_s") or target.get("duration_s") or 0
    comps = [a for a in acts if a["id"] != target["id"] and a.get("sport") == target.get("sport")
             and tday - timedelta(days=90) <= analytics.d(a["date"]) < tday
             and dur and 0.6 * dur <= (a.get("moving_s") or a.get("duration_s") or 0) <= 1.4 * dur]

    def eff(a):
        if a.get("avg_hr") and a.get("avg_speed"):
            return a["avg_speed"] * 60 / a["avg_hr"]
        if a.get("avg_hr") and a.get("avg_power"):
            return a["avg_power"] / a["avg_hr"]
        return None

    comp_stats = None
    if comps:
        def m(field_fn):
            vals = [v for v in (field_fn(a) for a in comps) if v is not None]
            return round(mean(vals), 3) if vals else None
        comp_stats = {
            "n_sessions": len(comps),
            "avg_hr": m(lambda a: a.get("avg_hr")),
            "avg_speed_kmh": (lambda v: round(v * 3.6, 2) if v else None)(m(lambda a: a.get("avg_speed"))),
            "avg_power": m(lambda a: a.get("avg_power")),
            "efficiency_output_per_heartbeat": m(eff),
            "this_session_efficiency": round(eff(target), 3) if eff(target) else None,
        }
        if comp_stats["efficiency_output_per_heartbeat"] and comp_stats["this_session_efficiency"]:
            comp_stats["efficiency_vs_usual_pct"] = round(
                (comp_stats["this_session_efficiency"] / comp_stats["efficiency_output_per_heartbeat"] - 1) * 100, 1)

    load_by_day = {r["date"]: r for r in ctx["load"]}
    before = load_by_day.get(target["date"], {})
    days = {r["date"]: r for r in ctx["days"]}
    prior_14 = [a for a in acts if tday - timedelta(days=14) <= analytics.d(a["date"]) < tday]
    biggest = sorted(prior_14, key=lambda a: a.get("load") or 0, reverse=True)[:3]
    base = [r for r in ctx["days"] if tday - timedelta(days=60) <= analytics.d(r["date"]) < tday - timedelta(days=3)]

    def baseline(field):
        vals = [r[field] for r in base if r.get(field) is not None]
        return round(mean(vals), 1) if vals else None

    return {
        "session": _activity_summary(target),
        "athlete_max_hr": settings["max_hr"],
        "pct_max_hr": round(target["avg_hr"] / settings["max_hr"] * 100) if target.get("avg_hr") else None,
        "comparable_sessions_last_90d": comp_stats,
        "going_in": {"fitness_ctl": before.get("ctl"), "fatigue_atl": before.get("atl"), "form_tsb": before.get("tsb")},
        "load_last_7_days": round(sum(r["load"] for r in ctx["load"] if tday - timedelta(days=7) <= analytics.d(r["date"]) < tday)),
        "avg_weekly_load_prior_4_weeks": round(sum(r["load"] for r in ctx["load"]
                                                    if tday - timedelta(days=35) <= analytics.d(r["date"]) < tday - timedelta(days=7)) / 4),
        "biggest_sessions_prior_14_days": [_activity_summary(a) for a in biggest],
        "recovery_last_4_mornings": [_day_summary(days[(tday - timedelta(days=i)).isoformat()])
                                     for i in range(3, -1, -1) if (tday - timedelta(days=i)).isoformat() in days],
        "personal_baselines_60d": {"hrv": baseline("hrv"), "resting_hr": baseline("resting_hr"),
                                   "sleep_h": (lambda v: round(v / 3600, 2) if v else None)(baseline("sleep_s")),
                                   "sleep_score": baseline("sleep_score")},
    }


def tool_get_habit_correlations(conn, _args) -> dict:
    ctx = _ctx(conn, 180)
    return {"correlations": analytics.habit_correlations(ctx["days"])}


HABIT_COLS = ["bedtime", "wake_time", "sleep_quality", "drinks", "calories", "protein_g", "carbs_g", "fat_g",
              "water_l", "caffeine_mg", "energy", "soreness", "notes"]


def tool_log_habits(conn, args) -> dict:
    from pydantic import ValidationError

    from .main import Habit  # same validation as the Habits form

    day = _parse_date(args.get("date"), date.today())
    if day > date.today() + timedelta(days=1):
        return {"error": "Date is in the future."}
    fields = {k: args[k] for k in HABIT_COLS if args.get(k) is not None}
    if not fields:
        return {"error": "No habit fields given."}
    existing = conn.execute("SELECT * FROM habits WHERE date=?", (day.isoformat(),)).fetchone()
    merged = {k: (existing[k] if existing else None) for k in HABIT_COLS}
    if "notes" in fields and merged.get("notes"):
        fields["notes"] = f"{merged['notes']}\n{fields['notes']}"
    merged.update(fields)
    try:
        Habit(**merged)
    except ValidationError as e:
        return {"error": f"Invalid value: {e.errors()[0]['loc'][0]}: {e.errors()[0]['msg']}"}
    with db.tx(conn):
        db.upsert(conn, "habits", {"date": day.isoformat(), **merged, "updated_at": db.now()})
    return {"saved": True, "date": day.isoformat(), "fields": fields}


def tool_sync_data(conn, _args) -> dict:
    from .sync import runner
    return {"results": runner.run("all")}


TOOLS: dict[str, tuple[Callable, dict]] = {
    "get_overview": (tool_get_overview, {
        "description": "Current snapshot: today's date, athlete profile, readiness score, fitness/fatigue/form, "
                       "open diagnostic findings, the last 7 days of sleep/HRV/habits and the 10 most recent activities. "
                       "Good first call for general questions.",
        "input_schema": {"type": "object", "properties": {}},
    }),
    "explain_activity": (tool_explain_activity, {
        "description": "Diagnose one training session. Returns the session, how it compares with similar sessions "
                       "of the same sport in the previous 90 days (HR, speed, efficiency), training load and form "
                       "going into it, the biggest sessions in the prior 14 days, the 4 mornings of recovery data "
                       "before it, and 60-day personal baselines. Use this first for 'why was my run/ride/ruck bad'. "
                       "Give activity_id, or a date plus an optional sport.",
        "input_schema": {"type": "object", "properties": {
            "activity_id": {"type": "string", "description": "Activity id from another tool, e.g. 'garmin:123'."},
            "date": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
            "sport": {"type": "string", "description": "run, ride, swim, walk, hike, ruck, strength, multisport, row, ski, yoga or other."},
        }},
    }),
    "get_activities": (tool_get_activities, {
        "description": "List activities between two dates (up to 180 days), optionally filtered by sport. "
                       "Each has duration, distance, pace, HR, power, training load and intensity.",
        "input_schema": {"type": "object", "properties": {
            "start_date": {"type": "string", "description": "YYYY-MM-DD, defaults to 30 days ago."},
            "end_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today."},
            "sport": {"type": "string", "description": "Optional sport filter."},
        }},
    }),
    "get_daily_metrics": (tool_get_daily_metrics, {
        "description": "Day-by-day recovery and habits between two dates (up to 180 days): sleep hours/score, bed and "
                       "wake times, HRV with baseline band, resting HR, stress, Body Battery, steps, drinks, calories "
                       "eaten and burned, macros, water, caffeine, subjective energy/soreness and notes.",
        "input_schema": {"type": "object", "properties": {
            "start_date": {"type": "string", "description": "YYYY-MM-DD, defaults to 14 days ago."},
            "end_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today."},
        }},
    }),
    "get_training_load": (tool_get_training_load, {
        "description": "Daily training load with fitness (CTL), fatigue (ATL) and form (TSB), plus weekly totals by sport.",
        "input_schema": {"type": "object", "properties": {
            "start_date": {"type": "string", "description": "YYYY-MM-DD, defaults to 42 days ago."},
            "end_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today."},
        }},
    }),
    "get_habit_correlations": (tool_get_habit_correlations, {
        "description": "From the athlete's own last 6 months: average HRV, resting HR, sleep score and Body Battery "
                       "after nights with vs without 2+ drinks, a bedtime after midnight, or under 7 h sleep.",
        "input_schema": {"type": "object", "properties": {}},
    }),
    "log_habits": (tool_log_habits, {
        "description": "Save habits to the daily log for one date. Only fields you pass are changed; anything else already logged "
                       "for that date is kept, and notes are appended. Day convention: bedtime/wake_time/sleep_quality "
                       "describe the night that ended on the morning of `date`; drinks and food are for `date` itself.",
        "input_schema": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "bedtime": {"type": "string", "description": "HH:MM, 24-hour"},
            "wake_time": {"type": "string", "description": "HH:MM, 24-hour"},
            "sleep_quality": {"type": "integer", "description": "1 (awful) to 5 (great)"},
            "drinks": {"type": "number", "description": "Standard alcoholic drinks"},
            "calories": {"type": "number"}, "protein_g": {"type": "number"}, "carbs_g": {"type": "number"},
            "fat_g": {"type": "number"}, "water_l": {"type": "number"}, "caffeine_mg": {"type": "number"},
            "energy": {"type": "integer", "description": "1-5"}, "soreness": {"type": "integer", "description": "1-5"},
            "notes": {"type": "string", "description": "Free text, e.g. what they ate, illness, travel, heat, how they felt."},
        }, "required": ["date"]},
    }),
    "sync_data": (tool_sync_data, {
        "description": "Pull the latest data from Garmin and Strava now. Use this when the athlete asks about a session "
                       "that isn't in the data yet. It can take up to a minute.",
        "input_schema": {"type": "object", "properties": {}},
    }),
}
TOOL_DEFS = [{"name": n, **spec} for n, (_, spec) in TOOLS.items()]
TOOL_LABELS = {"get_overview": "overview", "explain_activity": "session analysis", "get_activities": "activities",
               "get_daily_metrics": "sleep & habits", "get_training_load": "training load",
               "get_habit_correlations": "habit correlations", "log_habits": "habit log", "sync_data": "sync"}


def run_tool(conn, name: str, args: Any) -> tuple[str, bool]:
    if name not in TOOLS:
        return f"Unknown tool {name}", True
    if not isinstance(args, dict):
        return "Tool input must be an object.", True
    try:
        out = TOOLS[name][0](conn, args)
        return json.dumps(out, default=str), "error" in out
    except ValueError as e:
        return f"Invalid input: {e}", True
    except Exception as e:  # report to the model rather than failing the whole turn
        return f"Tool failed: {type(e).__name__}: {e}", True


# ---- conversation storage ----------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL,          -- JSON: API content (string or list of blocks), replayed verbatim
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_conv ON chat_messages(conversation_id, id);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def _history(conn, conv_id: int) -> list[dict]:
    return [{"role": r["role"], "content": json.loads(r["content"])}
            for r in conn.execute("SELECT role, content FROM chat_messages WHERE conversation_id=? ORDER BY id", (conv_id,))]


def _append(conn, conv_id: int, role: str, content: Any) -> None:
    with db.tx(conn):
        conn.execute("INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                     (conv_id, role, json.dumps(content), db.now()))
        conn.execute("UPDATE chat_conversations SET updated_at=? WHERE id=?", (db.now(), conv_id))


def transcript(conn, conv_id: int) -> list[dict]:
    """User-visible messages only: user text and assistant text, plus which data each answer used."""
    out: list[dict] = []
    pending_tools: list[str] = []
    for m in _history(conn, conv_id):
        c = m["content"]
        if m["role"] == "user":
            if isinstance(c, str):
                out.append({"role": "user", "text": c})
            continue
        blocks = c if isinstance(c, list) else [{"type": "text", "text": c}]
        pending_tools += [TOOL_LABELS.get(b.get("name"), b.get("name")) for b in blocks if b.get("type") == "tool_use"]
        text = "\n\n".join(b["text"] for b in blocks if b.get("type") == "text" and b.get("text"))
        if text and not any(b.get("type") == "tool_use" for b in blocks):
            out.append({"role": "assistant", "text": text, "tools": sorted(set(pending_tools))})
            pending_tools = []
        elif text:
            out.append({"role": "assistant", "text": text, "tools": [], "interim": True})
    return out


def list_conversations(conn) -> list[dict]:
    return db.rows(conn, "SELECT id, title, updated_at FROM chat_conversations ORDER BY updated_at DESC LIMIT 30")


# ---- the agent loop ------------------------------------------------------------

def _system() -> list[dict]:
    today = date.today()
    return [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"Today is {today:%A} {today.isoformat()}."},
    ]


def _block_to_dict(b: Any) -> dict:
    return b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else dict(b)


def chat(user_text: str, conv_id: int | None = None, client: Any = None) -> dict:
    """Run one user turn to completion. Returns {conversation_id, reply, tools, logged}."""
    import anthropic

    conn = db.get_conn()
    ensure_schema(conn)
    if conv_id is None or conn.execute("SELECT 1 FROM chat_conversations WHERE id=?", (conv_id,)).fetchone() is None:
        with db.tx(conn):
            cur = conn.execute("INSERT INTO chat_conversations (title, created_at, updated_at) VALUES (?,?,?)",
                               (user_text[:80], db.now(), db.now()))
            conv_id = cur.lastrowid
    _append(conn, conv_id, "user", user_text)

    client = client or anthropic.Anthropic()
    tools_used: list[str] = []
    logged: list[dict] = []
    reply = ""
    for _ in range(MAX_TOOL_ROUNDS):
        messages = _history(conn, conv_id)
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=_system(),
            tools=TOOL_DEFS,
            messages=messages,
            output_config={"effort": EFFORT},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            reply = "Sorry, I can't help with that one. Try rephrasing the question."
            break
        content = [_block_to_dict(b) for b in response.content]
        # Store the assistant turn verbatim (thinking blocks included) so the history stays append-only.
        _append(conn, conv_id, "assistant", content)
        reply = "\n\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text"))

        calls = [b for b in content if b.get("type") == "tool_use"]
        if response.stop_reason == "max_tokens" and not calls:
            reply += "\n\n(Answer was cut off; ask me to continue.)"
            break
        if not calls:
            break
        results = []
        for call in calls:
            out, is_error = run_tool(conn, call["name"], call.get("input"))
            tools_used.append(TOOL_LABELS.get(call["name"], call["name"]))
            if call["name"] == "log_habits" and not is_error:
                logged.append(json.loads(out))
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": out,
                            **({"is_error": True} if is_error else {})})
        _append(conn, conv_id, "user", results)
    else:
        reply = reply or "I ran out of steps looking into that. Try a narrower question."

    return {"conversation_id": conv_id, "reply": reply, "tools": sorted(set(tools_used)), "logged": logged}
