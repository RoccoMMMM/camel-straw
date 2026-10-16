from datetime import date, timedelta

from app import analytics, diagnostics

S = {"max_hr": 190, "resting_hr": 50, "sex": "male", "weight_kg": 70, "sleep_target_h": 8,
     "protein_target_g_per_kg": 1.6, "weekly_drink_limit": 7}
TODAY = date(2026, 6, 30)


def act(day, mins=60, hr=140, sport="run", speed=3.0, **kw):
    return {"date": day.isoformat(), "start_time": f"{day.isoformat()}T07:00:00", "sport": sport,
            "moving_s": mins * 60, "duration_s": mins * 60, "avg_hr": hr, "avg_speed": speed, **kw}


def test_trimp_scale_and_sex():
    t = analytics.trimp(3600, 140, 50, 190)
    assert 70 < t < 110
    assert analytics.trimp(3600, 140, 50, 190, "female") != t
    assert analytics.trimp(3600, 40, 50, 190) == 0


def test_load_fallbacks():
    assert analytics.activity_load({"moving_s": 3600, "garmin_load": 120}, S) == 120
    assert analytics.activity_load({"moving_s": 3600, "suffer_score": 55}, S) == 55
    assert analytics.activity_load({"moving_s": 3600}, S) == 48


def test_ctl_atl_tsb():
    acts = [act(TODAY - timedelta(days=i)) for i in range(60)]
    series = analytics.load_series(acts, S, TODAY - timedelta(days=10), TODAY)
    last = series[-1]
    assert len(series) == 11
    assert last["atl"] > last["ctl"] > 0          # constant load: ATL converges faster
    assert abs(last["atl"] - last["load"]) < 5


def test_clock_helpers():
    assert analytics.clock_to_minutes("23:30", bedtime=True) == 1410
    assert analytics.clock_to_minutes("00:30", bedtime=True) == 1470
    assert analytics.clock_to_minutes("06:45") == 405
    assert analytics.minutes_to_clock(1470) == "00:30"
    assert analytics.clock_to_minutes("bad") is None


def test_merge_days_manual_overrides_garmin_and_estimates_sleep():
    day = TODAY.isoformat()
    rows = analytics.merge_days(
        [{"date": day, "bedtime": "23:00", "wake_time": "07:00", "kcal_in": 1800, "drinks": 1}],
        [{"date": day, "bedtime": "22:30", "wake_time": "06:30", "drinks": 3}], TODAY, TODAY)
    r = rows[0]
    assert r["bedtime"] == "22:30" and r["drinks"] == 3 and r["calories"] == 1800
    assert r["sleep_s"] == 8 * 3600 and r["sleep_estimated"]


def test_habit_correlations_next_day_alcohol_effect():
    days = []
    for i in range(30):
        drinks = 3 if i % 3 == 0 else 0
        days.append({"date": (TODAY - timedelta(days=30 - i)).isoformat(), "drinks": drinks, "hrv": None,
                     "bed_min": None, "sleep_s": None})
    for i in range(1, 30):  # HRV the morning after drinking is lower
        days[i]["hrv"] = 50 if days[i - 1]["drinks"] else 70
    res = [c for c in analytics.habit_correlations(days) if c["metric"] == "hrv" and "drinks" in c["habit"]]
    assert res and res[0]["with"] == 50 and res[0]["without"] == 70 and res[0]["worse"]


def _ctx(acts, days):
    return {"today": TODAY, "settings": S, "activities": acts,
            "load": analytics.load_series(acts, S, TODAY - timedelta(days=90), TODAY), "days": days}


def _days(n=90, **fields):
    return [{"date": (TODAY - timedelta(days=n - 1 - i)).isoformat(), **{k: (v(i) if callable(v) else v) for k, v in fields.items()}}
            for i in range(n)]


def ids(findings):
    return {f["id"] for f in findings}


def test_diagnose_spike_and_overreaching():
    acts = [act(TODAY - timedelta(days=i), mins=45) for i in range(8, 90, 2)]
    acts += [act(TODAY - timedelta(days=i), mins=150, hr=165) for i in range(0, 8)]
    days = _days(resting_hr=lambda i: 58 if i >= 87 else 50, hrv=lambda i: 40 if i >= 83 else 70 + (i % 5))
    f = diagnostics.diagnose(_ctx(acts, days))
    assert {"acwr-spike", "form-deep-fatigue", "rhr-high", "hrv-suppressed", "overreaching"} <= ids(f)
    assert f[0]["severity"] == "critical"
    assert diagnostics.readiness_score(_ctx(acts, days), f)["score"] < 40


def test_diagnose_sleep_alcohol_nutrition():
    days = _days(sleep_s=5.5 * 3600, bed_min=lambda i: 1380 + (i % 4) * 80, wake_min=400,
                 drinks=lambda i: 4 if i % 2 else 0, protein_g=60, calories=1500, kcal_burned=2800)
    f = ids(diagnostics.diagnose(_ctx([], days)))
    assert {"sleep-short", "bedtime-irregular", "alcohol-heavy", "protein-low", "underfuelling"} <= f


def test_diagnose_healthy_athlete_gets_good_news():
    acts = [act(TODAY - timedelta(days=i), mins=60, hr=125) for i in range(0, 90) if i % 7 != 0]
    days = _days(sleep_s=8 * 3600, bed_min=1350, wake_min=420, drinks=0, resting_hr=48, hrv=70)
    f = diagnostics.diagnose(_ctx(acts, days))
    sev = {x["severity"] for x in f}
    assert "critical" not in sev and "serious" not in sev
    assert {"sleep-good", "bedtime-regular", "alcohol-free", "polarised"} <= ids(f)
    assert diagnostics.readiness_score(_ctx(acts, days), f)["score"] >= 80


def test_rules_never_crash_on_empty_data():
    f = diagnostics.diagnose({"today": TODAY, "settings": S, "activities": [], "load": [], "days": []})
    assert ids(f) == {"no-data"}
