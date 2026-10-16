"""Rule-based diagnosis of training, recovery and lifestyle issues.

Each rule looks at the analytics context and returns zero or more findings:
  {id, category, severity, title, detail, action}
severity: "critical" > "serious" > "warning" > "good" (positive signal) > "info".

Thresholds are deliberately conservative and drawn from widely used sports-science heuristics
(acute:chronic workload ratio, Foster monotony, HRV/RHR deviation from personal baseline, sleep
regularity). They flag things worth a look — not medical diagnoses.
"""
from __future__ import annotations

from datetime import timedelta
from statistics import mean
from typing import Callable

from .analytics import d, intensity_class, minutes_to_clock, sd

SEVERITY_ORDER = {"critical": 0, "serious": 1, "warning": 2, "good": 3, "info": 4}
RULES: list[Callable[[dict], list[dict]]] = []


def rule(fn):
    RULES.append(fn)
    return fn


def finding(id, category, severity, title, detail, action=None):
    return {"id": id, "category": category, "severity": severity, "title": title, "detail": detail, "action": action}


def _last(days: list[dict], n: int, field: str) -> list:
    return [r[field] for r in days[-n:] if r.get(field) is not None]


# ---- training load -----------------------------------------------------------

@rule
def form(ctx):
    load = ctx["load"]
    if len(load) < 14:
        return []
    t = load[-1]
    ctl, tsb = t["ctl"], t["tsb"]
    if ctl < 10:
        return []
    if tsb < -30:
        return [finding("form-deep-fatigue", "training", "serious", "Deep fatigue",
                        f"Form (TSB) is {tsb:.0f}. Below −30 the risk of illness, injury and stalled progress rises sharply.",
                        "Take 2–3 easy or rest days until form climbs back above −20.")]
    if tsb < -20:
        return [finding("form-fatigued", "training", "warning", "Carrying significant fatigue",
                        f"Form (TSB) is {tsb:.0f}. Productive for a block, but don't stay here for more than ~2 weeks.",
                        "Plan a recovery week soon.")]
    ctl_28 = load[-29]["ctl"] if len(load) > 28 else load[0]["ctl"]
    if tsb > 20 and ctl < ctl_28 * 0.9:
        return [finding("form-detraining", "training", "warning", "Losing fitness",
                        f"Fitness (CTL) fell from {ctl_28:.0f} to {ctl:.0f} over 4 weeks while form is very fresh (+{tsb:.0f}).",
                        "Fine for a taper or an off-season break; otherwise rebuild volume gradually.")]
    return []


@rule
def ramp_rate(ctx):
    load = ctx["load"]
    if len(load) < 8:
        return []
    ramp = load[-1]["ctl"] - load[-8]["ctl"]
    if ramp > 8:
        return [finding("ramp-high", "training", "serious", "Fitness is ramping too fast",
                        f"Fitness (CTL) rose {ramp:.1f} points in 7 days. Sustained ramps above ~5–8/week are a classic injury and overtraining pattern.",
                        "Hold this week's volume flat or cut it 20–30%.")]
    if ramp > 5:
        return [finding("ramp-elevated", "training", "warning", "Aggressive build",
                        f"Fitness (CTL) rose {ramp:.1f} points in 7 days.",
                        "OK for a week or two; follow it with a lighter week.")]
    if ramp > 1.5 and load[-1]["tsb"] > -25:
        return [finding("ramp-good", "training", "good", "Building fitness steadily",
                        f"Fitness (CTL) is up {ramp:.1f} points this week at a sustainable rate.")]
    return []


@rule
def acwr(ctx):
    load = ctx["load"]
    if len(load) < 28:
        return []
    acute = sum(r["load"] for r in load[-7:])
    chronic = sum(r["load"] for r in load[-28:]) / 4
    if chronic < 100:
        return []
    ratio = acute / chronic
    if ratio > 1.5:
        return [finding("acwr-spike", "training", "serious", "Training spike",
                        f"This week's load is {ratio:.2f}× your 4-week average (acute:chronic ratio). Above 1.5 is strongly associated with injury.",
                        "Back off for the next few days; increase load no more than ~10–20% week over week.")]
    if ratio > 1.3:
        return [finding("acwr-high", "training", "warning", "Load jumped this week",
                        f"Acute:chronic workload ratio is {ratio:.2f} (sweet spot 0.8–1.3).")]
    if ratio < 0.6:
        return [finding("acwr-low", "training", "info", "Much lighter week",
                        f"This week's load is {ratio:.2f}× your 4-week average — fine if planned recovery.")]
    return []


@rule
def monotony(ctx):
    loads = [r["load"] for r in ctx["load"][-7:]]
    if len(loads) < 7 or sum(loads) < 150:
        return []
    s = sd(loads) or 0
    m = mean(loads)
    mono = m / s if s else 99
    if mono > 2.0:
        return [finding("monotony", "training", "warning", "Training is too monotonous",
                        f"Foster monotony is {min(mono, 9.9):.1f} (>2 is high): days are too similar, with no real easy or rest days. "
                        f"High monotony × high load is linked to illness and overreaching.",
                        "Make easy days easier, hard days harder, and include at least one rest day.")]
    return []


@rule
def intensity_distribution(ctx):
    since = ctx["today"] - timedelta(days=28)
    secs = {"easy": 0.0, "moderate": 0.0, "hard": 0.0}
    for a in ctx["activities"]:
        zone = intensity_class(a, ctx["settings"])
        if d(a["date"]) > since and zone and a.get("sport") not in ("strength", "yoga", "walk"):
            secs[zone] += a.get("moving_s") or a.get("duration_s") or 0
    total = sum(secs.values())
    if total < 5 * 3600:
        return []
    mod, easy = secs["moderate"] / total, secs["easy"] / total
    if mod > 0.4:
        return [finding("grey-zone", "training", "warning", "Too much 'grey-zone' training",
                        f"{mod:.0%} of endurance time in the last 4 weeks averaged a moderate heart rate (77–87% max). "
                        f"Too hard to recover from, too easy to drive big adaptations.",
                        "Aim for ~80% genuinely easy (conversational) and ~20% hard. Slow the easy sessions down.")]
    if easy >= 0.7:
        return [finding("polarised", "training", "good", "Good intensity balance",
                        f"{easy:.0%} of endurance time in the last 4 weeks was easy — a solid aerobic base pattern.")]
    return []


@rule
def efficiency_trend(ctx):
    from .analytics import efficiency_series
    out = []
    series = efficiency_series(ctx["activities"], ctx["settings"])
    for sport, label in (("run", "running"), ("ride", "cycling")):
        pts = [p for p in series if p["sport"] == sport]
        recent = [p["ef"] for p in pts if d(p["date"]) > ctx["today"] - timedelta(days=28)]
        prior = [p["ef"] for p in pts if ctx["today"] - timedelta(days=84) < d(p["date"]) <= ctx["today"] - timedelta(days=28)]
        if len(recent) < 3 or len(prior) < 4:
            continue
        change = mean(recent) / mean(prior) - 1
        if change <= -0.05:
            out.append(finding(f"ef-down-{sport}", "training", "warning", f"Aerobic efficiency dropping ({label})",
                               f"Output per heartbeat on easy/steady {label} sessions is {abs(change):.0%} lower than the previous 8 weeks. "
                               f"Common causes: accumulated fatigue, illness, heat, dehydration, poor sleep, or under-fuelling.",
                               "Cross-check with HRV, resting HR and sleep below. If those are also off, prioritise recovery."))
        elif change >= 0.03:
            out.append(finding(f"ef-up-{sport}", "training", "good", f"Aerobic fitness improving ({label})",
                               f"Output per heartbeat on easy/steady {label} sessions is up {change:.0%} vs the previous 8 weeks."))
    return out


@rule
def consistency(ctx):
    acts = ctx["activities"]
    if not acts:
        return [finding("no-data", "training", "info", "No activities yet",
                        "Connect Garmin and/or Strava in Settings, then press Sync.")]
    last = max(d(a["date"]) for a in acts)
    gap = (ctx["today"] - last).days
    if gap >= 7 and ctx["load"] and ctx["load"][-1]["ctl"] > 20:
        return [finding("gap", "training", "warning", f"No training for {gap} days",
                        "Fitness starts to decline noticeably after 1–2 weeks off.",
                        "If you're unwell or injured, rest is right. Otherwise ease back in with short easy sessions.")]
    return []


# ---- recovery ----------------------------------------------------------------

@rule
def resting_hr(ctx):
    days = ctx["days"]
    recent = _last(days, 3, "resting_hr")
    base = [r["resting_hr"] for r in days[-33:-3] if r.get("resting_hr") is not None]
    if len(recent) < 2 or len(base) < 10:
        return []
    diff = mean(recent) - mean(base)
    if diff >= 7:
        return [finding("rhr-high", "recovery", "critical", "Resting heart rate sharply elevated",
                        f"3-day resting HR is {mean(recent):.0f} bpm, {diff:.0f} above your 30-day baseline ({mean(base):.0f}). "
                        f"Often an early sign of illness, overreaching, dehydration or alcohol.",
                        "Skip intensity today. If you feel unwell, rest until it returns to baseline.")]
    if diff >= 4:
        return [finding("rhr-elevated", "recovery", "serious", "Resting heart rate elevated",
                        f"3-day resting HR is {mean(recent):.0f} bpm, {diff:.0f} above your 30-day baseline ({mean(base):.0f}).",
                        "Keep training easy until it settles.")]
    if diff <= -2:
        return [finding("rhr-low", "recovery", "good", "Resting heart rate trending down",
                        f"3-day resting HR is {abs(diff):.0f} bpm below your 30-day baseline — typically a sign of improving fitness.")]
    return []


@rule
def hrv(ctx):
    days = ctx["days"]
    recent = _last(days, 7, "hrv")
    base = [r["hrv"] for r in days[-67:-7] if r.get("hrv") is not None]
    if len(recent) < 4 or len(base) < 14:
        return []
    m, s = mean(base), sd(base) or 1
    z = (mean(recent) - m) / s
    low_streak = 0
    for r in reversed(days):
        if r.get("hrv") is None:
            continue
        if r["hrv"] < m - s:
            low_streak += 1
        else:
            break
    if z < -1.5 or low_streak >= 4:
        return [finding("hrv-suppressed", "recovery", "serious", "HRV suppressed",
                        f"7-day HRV average is {mean(recent):.0f} ms vs a 60-day baseline of {m:.0f} ± {s:.0f} ms"
                        + (f"; {low_streak} nights in a row below your normal range." if low_streak >= 2 else "."),
                        "Your nervous system isn't recovering. Reduce intensity, and look at sleep, alcohol and stress.")]
    if z < -0.75:
        return [finding("hrv-low", "recovery", "warning", "HRV below normal",
                        f"7-day HRV average is {mean(recent):.0f} ms vs a 60-day baseline of {m:.0f} ms.")]
    if z > 0.75:
        return [finding("hrv-high", "recovery", "good", "HRV above baseline",
                        f"7-day HRV average is {mean(recent):.0f} ms vs a 60-day baseline of {m:.0f} ms — well recovered.")]
    return []


@rule
def combined_overreach(ctx):
    """The pattern that matters most: heavy load AND physiological markers going the wrong way."""
    load = ctx["load"]
    days = ctx["days"]
    if len(load) < 28:
        return []
    acute = sum(r["load"] for r in load[-7:])
    chronic = sum(r["load"] for r in load[-28:]) / 4 or 1
    rhr_r, rhr_b = _last(days, 3, "resting_hr"), [r["resting_hr"] for r in days[-33:-3] if r.get("resting_hr")]
    hrv_r, hrv_b = _last(days, 7, "hrv"), [r["hrv"] for r in days[-67:-7] if r.get("hrv")]
    flags = 0
    if rhr_r and len(rhr_b) >= 10 and mean(rhr_r) - mean(rhr_b) >= 3:
        flags += 1
    if hrv_r and len(hrv_b) >= 14 and mean(hrv_r) < mean(hrv_b) - (sd(hrv_b) or 0) * 0.75:
        flags += 1
    if load[-1]["tsb"] < -20 or acute / chronic > 1.3:
        flags += 1
    if flags >= 3:
        return [finding("overreaching", "recovery", "critical", "Signs of functional overreaching",
                        "High training load together with elevated resting HR and suppressed HRV. Pushing on from here "
                        "tends to end in illness, injury or weeks of flat performance.",
                        "Take 3–5 days of rest or very easy movement, prioritise 8+ h sleep and no alcohol, then reassess.")]
    return []


# ---- sleep -------------------------------------------------------------------

@rule
def sleep_duration(ctx):
    s = ctx["settings"]
    vals = _last(ctx["days"], 7, "sleep_s")
    if len(vals) < 4:
        return []
    h = mean(vals) / 3600
    target = float(s.get("sleep_target_h") or 8)
    debt = sum(max(0, target * 3600 - v) for v in vals) / 3600
    if h < 6:
        return [finding("sleep-short", "sleep", "serious", "Chronically short sleep",
                        f"Averaging {h:.1f} h over the last week ({debt:.1f} h short of your {target:g} h target).",
                        "Short sleep blunts adaptation, raises injury risk and drives cravings. Move bedtime earlier by 30 min.")]
    if h < target - 0.75:
        return [finding("sleep-under", "sleep", "warning", "Under your sleep target",
                        f"Averaging {h:.1f} h over the last week — {debt:.1f} h of sleep debt vs your {target:g} h target.",
                        "Protect sleep especially after hard sessions.")]
    if h >= target - 0.25:
        return [finding("sleep-good", "sleep", "good", "Sleep volume on target", f"Averaging {h:.1f} h over the last week.")]
    return []


@rule
def sleep_regularity(ctx):
    days = ctx["days"][-14:]
    beds = [r["bed_min"] for r in days if r.get("bed_min") is not None]
    wakes = [r["wake_min"] for r in days if r.get("wake_min") is not None]
    out = []
    if len(beds) >= 7:
        bsd = sd(beds) or 0
        avg_bed = minutes_to_clock(mean(beds))
        if bsd > 75:
            out.append(finding("bedtime-irregular", "sleep", "serious", "Very irregular bedtime",
                               f"Bedtime varies by ±{bsd:.0f} min (avg {avg_bed}) over the last 2 weeks. "
                               "An irregular schedule worsens sleep quality and HRV even when total hours are fine.",
                               "Pick a consistent lights-out time within a 30-minute window, weekends included."))
        elif bsd > 45:
            out.append(finding("bedtime-variable", "sleep", "warning", "Bedtime is inconsistent",
                               f"Bedtime varies by ±{bsd:.0f} min (avg {avg_bed}) over the last 2 weeks.",
                               "Aim to keep bedtime within ±30 min."))
        elif bsd <= 30:
            out.append(finding("bedtime-regular", "sleep", "good", "Consistent sleep schedule",
                               f"Bedtime has stayed within ±{bsd:.0f} min of {avg_bed}."))
        if mean(beds) > 24 * 60 + 30:
            out.append(finding("bedtime-late", "sleep", "warning", "Late bedtimes",
                               f"Average bedtime is {avg_bed}.",
                               "Late nights usually mean less deep sleep, which is front-loaded in the night."))
    # Social jet lag: weekend vs weekday midpoint of sleep
    wk, we = [], []
    for r in days:
        if r.get("bed_min") is not None and r.get("wake_min") is not None:
            mid = (r["bed_min"] + r["wake_min"] + 24 * 60) / 2
            (we if d(r["date"]).weekday() >= 5 else wk).append(mid)
    if len(wk) >= 4 and len(we) >= 2 and abs(mean(we) - mean(wk)) >= 75:
        out.append(finding("social-jetlag", "sleep", "warning", "Weekend 'social jet lag'",
                           f"Your sleep midpoint shifts {abs(mean(we) - mean(wk)):.0f} min on weekends — like flying a time zone every week.",
                           "Keep weekend wake time within an hour of weekdays."))
    if len(wakes) >= 7 and (sd(wakes) or 0) > 75:
        out.append(finding("wake-irregular", "sleep", "warning", "Irregular wake time",
                           f"Wake time varies by ±{sd(wakes):.0f} min over the last 2 weeks.",
                           "A fixed wake time is the strongest anchor for your body clock."))
    return out


# ---- alcohol -----------------------------------------------------------------

@rule
def alcohol(ctx):
    days = ctx["days"]
    week = [r["drinks"] for r in days[-7:] if r.get("drinks") is not None]
    if not week:
        return []
    total = sum(week)
    limit = float(ctx["settings"].get("weekly_drink_limit") or 7)
    out = []
    if total > limit * 2:
        out.append(finding("alcohol-heavy", "alcohol", "serious", "Heavy drinking week",
                           f"{total:g} drinks logged in the last 7 days (your limit: {limit:g}).",
                           "Alcohol is one of the biggest drags on HRV, sleep quality and recovery."))
    elif total > limit:
        out.append(finding("alcohol-over", "alcohol", "warning", "Over your weekly drink limit",
                           f"{total:g} drinks in the last 7 days (limit {limit:g})."))
    elif len(week) >= 5 and total == 0:
        out.append(finding("alcohol-free", "alcohol", "good", "Alcohol-free week", "No drinks logged in the last 7 days."))
    binge = [r for r in days[-7:] if (r.get("drinks") or 0) >= 5]
    if binge:
        out.append(finding("alcohol-binge", "alcohol", "warning", "Heavy single session",
                           f"{len(binge)} day(s) with 5+ drinks this week. One heavy night can suppress HRV for 2–4 days."))
    # Personal effect size, if there's enough history
    from .analytics import habit_correlations
    for c in habit_correlations(days):
        if c["habit"].startswith("2+ drinks") and c["metric"] == "hrv" and c["worse"] and abs(c["diff"]) >= 3:
            out.append(finding("alcohol-hrv-effect", "alcohol", "info", "Your data: alcohol lowers your HRV",
                               f"After evenings with 2+ drinks your HRV averages {c['with']} ms vs {c['without']} ms otherwise "
                               f"({c['diff']:+.0f} ms, {c['n_with']} vs {c['n_without']} nights)."))
    return out


# ---- nutrition ---------------------------------------------------------------

@rule
def nutrition(ctx):
    s = ctx["settings"]
    days = ctx["days"][-7:]
    out = []
    protein = [r["protein_g"] for r in days if r.get("protein_g")]
    weight = float(s.get("weight_kg") or 0)
    if len(protein) >= 3 and weight:
        gkg = mean(protein) / weight
        target = float(s.get("protein_target_g_per_kg") or 1.6)
        if gkg < target * 0.8:
            out.append(finding("protein-low", "nutrition", "warning", "Protein intake low",
                               f"Averaging {mean(protein):.0f} g/day ({gkg:.1f} g/kg) vs a target of {target:g} g/kg.",
                               "Spread 25–40 g protein across 3–5 meals, including after training."))
        elif gkg >= target:
            out.append(finding("protein-ok", "nutrition", "good", "Protein on target", f"Averaging {gkg:.1f} g/kg/day."))

    # Energy availability: logged intake vs Garmin's total daily burn.
    pairs = [(r["calories"], r["kcal_burned"]) for r in days if r.get("calories") and r.get("kcal_burned")]
    if len(pairs) >= 4:
        deficit = mean(b - i for i, b in pairs)
        if deficit > 750:
            out.append(finding("underfuelling", "nutrition", "serious", "Large energy deficit",
                               f"Logged intake is averaging {deficit:.0f} kcal/day below what your watch says you burn. "
                               "Sustained low energy availability (RED-S) impairs recovery, hormones, bone health and performance.",
                               "If weight loss isn't the goal, add carbs around training. If it is, keep the deficit under ~500 kcal."))
        elif deficit > 500:
            out.append(finding("deficit", "nutrition", "warning", "Moderate energy deficit",
                               f"Logged intake averages {deficit:.0f} kcal/day below estimated burn.",
                               "Prioritise fuelling before and after key sessions."))

    logged = sum(1 for r in days if r.get("calories") or r.get("protein_g"))
    if logged == 0 and ctx["activities"]:
        out.append(finding("nutrition-unlogged", "nutrition", "info", "No nutrition logged this week",
                           "Log calories/protein in the Habits tab (or in Garmin Connect) to get fuelling diagnostics."))
    return out


@rule
def stress(ctx):
    vals = _last(ctx["days"], 7, "stress_avg")
    bb = _last(ctx["days"], 7, "body_battery_high")
    out = []
    if len(vals) >= 4 and mean(vals) > 45:
        out.append(finding("stress-high", "recovery", "warning", "High all-day stress",
                           f"Garmin all-day stress is averaging {mean(vals):.0f} (above 45 is high). Life stress and training stress share the same budget."))
    if len(bb) >= 4 and mean(bb) < 50:
        out.append(finding("bb-low", "recovery", "warning", "Body Battery not recharging",
                           f"Morning Body Battery is averaging {mean(bb):.0f}; you're not fully recharging overnight."))
    return out


def diagnose(ctx: dict) -> list[dict]:
    out = []
    for r in RULES:
        try:
            out.extend(r(ctx))
        except Exception as e:  # a buggy rule must never take the dashboard down
            out.append(finding(f"rule-error-{r.__name__}", "system", "info", f"Rule '{r.__name__}' failed", str(e)))
    return sorted(out, key=lambda f: SEVERITY_ORDER[f["severity"]])


def readiness_score(ctx: dict, findings: list[dict]) -> dict:
    """A simple 0–100 'ready to train' score. Each category subtracts only its worst open issue,
    so several small sleep warnings don't outweigh one serious recovery red flag."""
    penalties = {  # category: (critical, serious, warning)
        "recovery": (40, 25, 12), "training": (35, 25, 10), "sleep": (20, 12, 6),
        "alcohol": (12, 8, 5), "nutrition": (12, 8, 5),
    }
    idx = {"critical": 0, "serious": 1, "warning": 2}
    worst: dict[str, int] = {}
    for f in findings:
        if f["category"] in penalties and f["severity"] in idx:
            worst[f["category"]] = max(worst.get(f["category"], 0), penalties[f["category"]][idx[f["severity"]]])
    score = max(0, 100 - sum(worst.values()))
    label = "Ready to train hard" if score >= 80 else "Train, but keep it controlled" if score >= 60 else \
        "Go easy today" if score >= 40 else "Recovery day"
    return {"score": score, "label": label}
