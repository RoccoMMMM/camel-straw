"""Plain-text (Markdown) export of your data, sized to paste into a Claude chat.

Each section is a compact table with a short legend, so a chat assistant can answer questions
and plan macros without access to the app.
"""
from __future__ import annotations

from datetime import timedelta
from statistics import mean
from typing import Any, Iterable

from . import analytics, diagnostics

SECTIONS = ("summary", "daily", "activities", "weekly", "macros")


def _fmt(v: Any, digits: int = 0) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, float):
        return f"{v:.{digits}f}" if digits else str(round(v))
    return str(v).replace("|", "/").replace("\n", " ")


def _num(v: Any) -> str:
    """Whole numbers without a trailing .0, others to one decimal: 3 -> "3", 2.5 -> "2.5"."""
    return "" if v is None else f"{round(float(v), 1):g}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Markdown table, dropping columns that are empty in every row."""
    keep = [i for i in range(len(headers)) if any(r[i] for r in rows)] if rows else list(range(len(headers)))
    out = ["| " + " | ".join(headers[i] for i in keep) + " |",
           "|" + "|".join("---" for _ in keep) + "|"]
    out += ["| " + " | ".join(r[i] for i in keep) + " |" for r in rows]
    return "\n".join(out)


def _avg(xs: Iterable[Any]) -> float | None:
    vals = [x for x in xs if x is not None]
    return mean(vals) if vals else None


def _dur(s: float | None) -> str:
    if not s:
        return ""
    h, m = divmod(round(s / 60), 60)
    return f"{h}h{m:02d}" if h else f"{m}m"


def _pace_or_speed(a: dict) -> str:
    v = a.get("avg_speed")
    if not v:
        return ""
    if a.get("sport") in ("run", "walk", "hike", "ruck"):
        s = 1000 / v
        return f"{int(s // 60)}:{int(s % 60):02d}/km"
    if a.get("sport") == "swim":
        s = 100 / v
        return f"{int(s // 60)}:{int(s % 60):02d}/100m"
    return f"{v * 3.6:.1f} km/h"


def build(ctx: dict, days: int = 30, sections: Iterable[str] = SECTIONS, conn: Any = None) -> str:
    sections = set(sections)
    today = ctx["today"]
    start = today - timedelta(days=days - 1)
    s = ctx["settings"]
    rows_days = [r for r in ctx["days"] if r["date"] >= start.isoformat()]
    acts = [a for a in ctx["activities"] if a["date"] >= start.isoformat()]
    load = {r["date"]: r for r in ctx["load"]}
    out: list[str] = [
        f"# My training data: {start.isoformat()} to {today.isoformat()} ({days} days)",
        "",
        "Exported from my personal training dashboard (Garmin + Strava + my own habit log). "
        "Use it to answer my questions about training, recovery and nutrition. Units: km, bpm, ms (HRV), kcal, grams.",
    ]

    if "summary" in sections:
        findings = diagnostics.diagnose(ctx)
        ready = diagnostics.readiness_score(ctx, findings)
        t = ctx["load"][-1] if ctx["load"] else {}
        last7 = ctx["days"][-7:]
        target_protein = round(float(s["protein_target_g_per_kg"]) * float(s["weight_kg"]))
        out += [
            "", "## Profile and targets",
            f"- Sex: {s['sex']} · weight {s['weight_kg']} kg · max HR {s['max_hr']} · resting HR setting {s['resting_hr']}",
            f"- Targets: sleep {s['sleep_target_h']} h/night · protein {s['protein_target_g_per_kg']} g/kg "
            f"(~{target_protein} g/day) · max {s['weekly_drink_limit']:g} drinks/week",
            "", f"## Status today ({today.isoformat()})",
            f"- Readiness: {ready['score']}/100 ({ready['label']})",
            f"- Fitness (CTL, 42-day load) {_fmt(t.get('ctl'))} · fatigue (ATL, 7-day load) {_fmt(t.get('atl'))} · "
            f"form (TSB = fitness − fatigue) {_fmt(t.get('tsb'))}. Below −30 is deep fatigue; above +20 is very fresh.",
            f"- Last 7 days averages: sleep {_fmt(_avg((r.get('sleep_s') or None) and r['sleep_s'] / 3600 for r in last7), 1)} h · "
            f"HRV {_fmt(_avg(r.get('hrv') for r in last7))} ms · resting HR {_fmt(_avg(r.get('resting_hr') for r in last7))} bpm · "
            f"calories eaten {_fmt(_avg(r.get('calories') for r in last7))} / burned {_fmt(_avg(r.get('kcal_burned') for r in last7))} kcal · "
            f"protein {_fmt(_avg(r.get('protein_g') for r in last7))} g · "
            f"drinks {_num(sum(r.get('drinks') or 0 for r in last7))} total",
        ]
        serious = [f for f in findings if f["severity"] in ("critical", "serious", "warning", "good")]
        if serious:
            out += ["", "## Issues the dashboard flagged"]
            out += [f"- [{f['severity']}] {f['title']}: {f['detail']}" for f in serious]
        corr = analytics.habit_correlations(ctx["days"])
        if corr:
            out += ["", "## Habit effects in my own data (nights with vs without)"]
            names = {"hrv": "HRV", "resting_hr": "resting HR", "sleep_score": "sleep score",
                     "body_battery_high": "Body Battery"}
            out += [f"- {c['habit']}: {names.get(c['metric'], c['metric'])} {c['with']} vs {c['without']} {c['unit']} "
                    f"({c['diff']:+g}; {c['n_with']}/{c['n_without']} nights)" for c in corr]

    if "daily" in sections:
        out += ["", "## Daily log",
                "Sleep, bed/wake, HRV and resting HR are for the night that ended that morning; food, drinks and burned "
                "calories are for that calendar day. Src: W = watch recorded the night, M = worked out from my logged "
                "bed/wake times (no HRV or sleep score on M nights). Energy and soreness are 1–5.", ""]
        rows = []
        for r in rows_days:
            src = {"watch": "W", "manual log": "M"}.get(r.get("sleep_source") or "", "")
            rows.append([
                r["date"][5:], _fmt(r["sleep_s"] / 3600 if r.get("sleep_s") else None, 1), src,
                _fmt(r.get("sleep_score")), _fmt(r.get("bedtime")), _fmt(r.get("wake_time")),
                _fmt(r.get("hrv")), _fmt(r.get("resting_hr")), _fmt(r.get("stress_avg")), _fmt(r.get("body_battery_high")),
                _num(r.get("drinks")),
                _fmt(r.get("calories")), _fmt(r.get("kcal_burned")), _fmt(r.get("protein_g")), _fmt(r.get("carbs_g")),
                _fmt(r.get("fat_g")), _fmt(r.get("water_l"), 1), _fmt(r.get("caffeine_mg")),
                _fmt(r.get("energy")), _fmt(r.get("soreness")), _fmt((r.get("notes") or "")[:100]),
            ])
        out.append(_table(["Date", "Sleep h", "Src", "Sleep score", "Bed", "Wake", "HRV", "RHR", "Stress", "Body Battery",
                           "Drinks", "kcal eaten", "kcal burned", "Protein g", "Carbs g", "Fat g", "Water L",
                           "Caffeine mg", "Energy", "Soreness", "Notes"], rows))

    if "activities" in sections:
        out += ["", "## Activities",
                "Load = training load (TRIMP-style; a steady 1-hour endurance session is roughly 60–90). Intensity is from "
                "average HR as % of max: easy <77%, moderate 77–87%, hard >87%. Event = legs of one triathlon/brick.", ""]
        rows = []
        for a in acts:
            rows.append([
                a["date"][5:], a["start_time"][11:16], _fmt(a.get("name")), a.get("sport") or "", _fmt(a.get("event")),
                _dur(a.get("moving_s") or a.get("duration_s")),
                _fmt((a.get("distance_m") or 0) / 1000, 1) if a.get("distance_m") else "",
                _pace_or_speed(a), _fmt(a.get("avg_hr")), _fmt(a.get("max_hr")), _fmt(a.get("avg_power")),
                _fmt(a.get("elevation_m")), _fmt(a.get("load")), a.get("intensity") or "", _fmt(a.get("calories")),
            ])
        out.append(_table(["Date", "Start", "Name", "Sport", "Event", "Time", "km", "Pace/speed", "Avg HR", "Max HR",
                           "Watts", "Elev m", "Load", "Intensity", "kcal"], rows) if rows else "No activities in this period.")

    if "weekly" in sections:
        weeks = max(1, (days + 6) // 7)
        out += ["", "## Weekly totals", "Week starts Monday. Form is at the end of the week.", ""]
        rows = []
        for w in analytics.weekly_summary(ctx["activities"], s, weeks, today):
            end = min(analytics.d(w["week"]) + timedelta(days=6), today).isoformat()
            ld = load.get(end, {})
            sports = ", ".join(f"{k} {v:.1f}h" for k, v in sorted(w["by_sport"].items(), key=lambda kv: -kv[1]))
            rows.append([w["week"], _fmt(w["hours"], 1), _fmt(w["distance_km"]), str(w["count"]), _fmt(w["load"]),
                         _fmt(ld.get("ctl")), _fmt(ld.get("tsb")), sports])
        out.append(_table(["Week of", "Hours", "km", "Sessions", "Load", "Fitness", "Form", "By sport"], rows))

    if "macros" in sections and conn is not None:
        from . import nutrition
        plan = nutrition.week_plan(conn, s, nutrition.monday(today), today)
        a_ = plan["assumptions"]
        out += ["", f"## Macro targets for this week (from {plan['week_start']})",
                f"Calculated by the dashboard from resting metabolism ({a_['bmr_kcal']} kcal) × daily activity "
                f"{a_['activity_factor']} + training (done for past days, planned for upcoming days), goal: "
                f"{a_['goal']}. Carb guide is g per kg body weight for that day's training.", ""]
        rows = []
        for d_ in plan["days"]:
            training = "; ".join(f"{(x.get('title') or x.get('sport'))} {_dur((x.get('duration_min') or 0) * 60)} "
                                 f"{x.get('intensity') or ''}".strip() for x in d_["sessions"]) or "rest"
            rows.append([f"{d_['weekday']} {d_['date'][5:]}", d_["source"], training, str(d_["kcal"]), str(d_["protein_g"]),
                         f"{d_['carbs_g']} ({d_['carbs_g_per_kg']} g/kg; guide {d_['carb_band'][0]:g}–{d_['carb_band'][1]:g})",
                         str(d_["fat_g"])])
        out.append(_table(["Day", "Status", "Training", "kcal", "Protein g", "Carbs g", "Fat g"], rows))

    return "\n".join(out) + "\n"
