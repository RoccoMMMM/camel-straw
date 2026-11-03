from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db, demo, nutrition, plan_import

S = {"max_hr": 190, "resting_hr": 50, "sex": "male", "weight_kg": 75, "sleep_target_h": 8,
     "protein_target_g_per_kg": 1.8, "weekly_drink_limit": 7, "height_cm": 180, "birth_year": 1996,
     "activity_level": "light", "goal": "maintain", "goal_rate_kg_week": 0.5, "fat_pct": 25}
TODAY = date(2026, 11, 4)


def test_bmr_mifflin_and_defaults():
    v, notes = nutrition.bmr(S, TODAY)                      # 10*75 + 6.25*180 - 5*30 + 5
    assert round(v) == 1730 and notes == []
    _, notes = nutrition.bmr({**S, "height_cm": None, "birth_year": None}, TODAY)
    assert len(notes) == 2                                  # tells you what it assumed


def test_more_training_means_more_food_and_carbs():
    base, _ = nutrition.bmr(S, TODAY)
    rest = nutrition.day_targets(TODAY, [], "planned", S, base, 1.35, {})
    long = nutrition.day_targets(TODAY, [{"sport": "ride", "duration_min": 180, "intensity": "moderate"}],
                                 "planned", S, base, 1.35, {})
    assert long["kcal"] > rest["kcal"] + 1000
    assert long["carbs_g_per_kg"] >= long["carb_band"][0] and rest["carb_band"] == [3, 5]
    assert long["protein_g"] == rest["protein_g"] == 135     # 1.8 g/kg every day
    assert any("60–90 g carbs per hour" in n for n in long["notes"])
    for d in (rest, long):                                  # macros add back up to the calories
        assert abs(d["protein_g"] * 4 + d["carbs_g"] * 4 + d["fat_g"] * 9 - d["kcal"]) < 15
        assert d["fat_g"] >= 0.6 * 75 - 1


def test_deficit_is_halved_on_big_days_and_floor_holds():
    base, _ = nutrition.bmr(S, TODAY)
    lose = {**S, "goal": "lose", "goal_rate_kg_week": 0.5}
    easy = nutrition.day_targets(TODAY, [], "planned", lose, base, 1.35, {})
    big = nutrition.day_targets(TODAY, [{"sport": "run", "duration_min": 130, "intensity": "easy"}], "planned", lose, base, 1.35, {})
    assert easy["goal_adjust_kcal"] == -550 and big["goal_adjust_kcal"] == -275
    tiny = nutrition.day_targets(TODAY, [], "planned", {**lose, "weight_kg": 45, "goal_rate_kg_week": 1}, base, 1.2, {})
    assert tiny["kcal"] >= 1500


def test_carb_load_before_long_race():
    base, _ = nutrition.bmr(S, TODAY)
    race = {"title": "Marathon", "sport": "run", "duration_min": 210, "intensity": "race"}
    d = nutrition.day_targets(TODAY, [], "planned", S, base, 1.35, {}, race_soon=race)
    assert d["carbs_g_per_kg"] >= 8 and "carb load" in d["carb_band_label"]


def test_personal_calibration_is_used():
    acts = [{"date": (TODAY - timedelta(days=i)).isoformat(), "sport": "run", "moving_s": 3600, "calories": 900}
            for i in range(1, 6)]
    calib = nutrition.personal_kcal_per_hour(acts, 1730, TODAY)
    assert 820 < calib["run"] < 840                         # 900 minus an hour of resting burn
    assert nutrition.session_kcal("run", 60, "moderate", 75, calib) == pytest.approx(calib["run"])
    assert nutrition.session_kcal("swim", 60, "moderate", 75, calib) == pytest.approx(7 * 75)   # MET fallback


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.set_conn(c)
    demo.seed(c)
    c.execute("DELETE FROM planned_sessions")      # start these tests from an empty plan
    c.commit()
    yield c
    db.set_conn(None)


def test_demo_has_a_plan(tmp_path):
    c = db.connect(tmp_path / "d.db")
    demo.seed(c)
    assert c.execute("SELECT COUNT(*) FROM planned_sessions").fetchone()[0] == 15


def test_week_plan_uses_done_then_planned(conn):
    wk = nutrition.monday(date.today())
    future = date.today() + timedelta(days=1)
    conn.execute("INSERT INTO planned_sessions (date, sport, title, duration_min, intensity) VALUES (?,?,?,?,?)",
                 (future.isoformat(), "ride", "Long ride", 150, "easy"))
    conn.commit()
    w = nutrition.week_plan(conn, db.get_settings(conn), wk)
    assert len(w["days"]) == 7 and w["warnings"] == []
    by_date = {d["date"]: d for d in w["days"]}
    if future.isoformat() in by_date:
        assert by_date[future.isoformat()]["source"] == "planned" and by_date[future.isoformat()]["training_min"] == 150
    for d in w["days"]:
        if d["date"] < date.today().isoformat():
            assert d["source"] == "done"


def test_parsers():
    ws = date(2026, 11, 9)
    r = plan_import.parse("Mon: rest\nTue: 6x800m intervals 60 min + gym 45 min\nSun: half marathon race 1:45",
                          ws, TODAY)
    got = [(s["date"], s["sport"], s["duration_min"], s["intensity"]) for s in r["sessions"]]
    assert r["method"] == "lines" and got == [
        ("2026-11-09", "rest", None, "rest"), ("2026-11-10", "run", 60, "hard"),
        ("2026-11-10", "strength", 45, "moderate"), ("2026-11-15", "run", 105, "race")]
    assert plan_import._minutes("4:30/km tempo") is None and plan_import._minutes("6x800m") is None
    r = plan_import.parse("day,session,duration\nWed,Easy run 10k,55\nThu,Bike sweet spot,1h30\n", ws, TODAY)
    assert r["method"] == "csv" and [(s["sport"], s["duration_min"], s["intensity"]) for s in r["sessions"]] == [
        ("run", 55, "easy"), ("ride", 90, "moderate")]
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        plan_import.parse("A hard block this month, keep long days steady", ws, TODAY)


def test_claude_reader_maps_schema(monkeypatch):
    import json
    from types import SimpleNamespace

    class Fake:
        def __init__(self):
            self.kw = None
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        def create(self, **kw):
            self.kw = kw
            payload = {"sessions": [{"date": "2026-11-11", "sport": "swim", "title": "CSS set", "duration_min": 50,
                                     "distance_km": 2.5, "intensity": "hard", "notes": "10x200 @ CSS"},
                                    {"date": "bad", "sport": "run", "title": "x", "duration_min": 1, "distance_km": None,
                                     "intensity": "easy", "notes": ""}], "assumptions": "Week 1 = Nov 9."}
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    fake = Fake()
    sessions, note = plan_import.parse_with_claude("Wk1 Wed CSS 10x200", date(2026, 11, 9), TODAY, client=fake)
    assert [s["title"] for s in sessions] == ["CSS set"] and note == "Week 1 = Nov 9."
    assert fake.kw["output_config"]["format"]["type"] == "json_schema" and fake.kw["fallbacks"] == "default"


def test_plan_endpoints(conn):
    from app.main import app
    c = TestClient(app)
    wk = nutrition.monday(date.today()) + timedelta(days=7)
    r = c.post("/api/plan/parse", json={"week_start": wk.isoformat(), "text": "Mon: 1h easy run\nWed: gym 45 min"})
    assert r.status_code == 200 and len(r.json()["sessions"]) == 2
    assert c.post("/api/plan/sessions", json={"sessions": r.json()["sessions"]}).json() == {"saved": 2}
    assert c.post("/api/plan/sessions", json={"sessions": r.json()["sessions"][:1], "replace": True}).json() == {"saved": 1}
    w = c.get(f"/api/plan?week={wk.isoformat()}").json()
    assert [p["title"] for p in w["planned_sessions"]] == ["Easy run", "Gym"]   # Monday replaced, Wednesday kept
    assert w["days"][0]["training_min"] == 60 and w["days"][0]["source"] == "planned"
    assert c.delete(f"/api/plan/sessions/{w['planned_sessions'][0]['id']}").json() == {"ok": True}
    assert c.delete(f"/api/plan/week/{wk.isoformat()}").json() == {"deleted": 1}
    assert c.post("/api/plan/sessions", json={"sessions": [{"date": wk.isoformat(), "sport": "bogus"}]}).status_code == 422
    assert c.post("/api/plan/parse", json={"week_start": wk.isoformat()}).status_code == 400
    assert "## Macro targets for this week" in c.get("/api/export?sections=macros").text
