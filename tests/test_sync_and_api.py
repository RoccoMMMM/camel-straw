import pytest
from fastapi.testclient import TestClient

from app import db
from app.sync import garmin, strava
from app.sync.common import find_number, normalise_sport


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.set_conn(c)
    yield c
    db.set_conn(None)


def test_normalise_sport():
    assert normalise_sport("trail_running") == "run"
    assert normalise_sport("VirtualRide") == "ride"
    assert normalise_sport("lap_swimming") == "swim"
    assert normalise_sport("strength_training") == "strength"
    assert normalise_sport("paddleboarding") == "other"


def test_find_number_nested():
    assert find_number({"a": [{"b": {"protein": "42.5"}}]}, "protein") == 42.5
    assert find_number({"flag": True}, "flag") is None


GARMIN_ACT = {"activityId": 1, "activityName": "Morning Run", "activityType": {"typeKey": "running"},
              "startTimeLocal": "2026-06-01 07:00:00", "duration": 3600, "distance": 10000, "averageHR": 150}
STRAVA_ACT = {"id": 99, "name": "Morning Run", "sport_type": "Run", "start_date_local": "2026-06-01T07:01:00Z",
              "elapsed_time": 3620, "moving_time": 3550, "distance": 10010, "suffer_score": 80}


@pytest.mark.parametrize("order", ["garmin_first", "strava_first"])
def test_same_workout_from_both_sources_is_stored_once(conn, order):
    if order == "garmin_first":
        garmin.store_activity(conn, GARMIN_ACT)
        assert strava.store_activity(conn, STRAVA_ACT) == "merged"
    else:
        strava.store_activity(conn, STRAVA_ACT)
        assert garmin.store_activity(conn, GARMIN_ACT) == "merged"
    rows = db.rows(conn, "SELECT * FROM activities")
    assert len(rows) == 1
    r = rows[0]
    assert r["source"] == "garmin" and r["strava_id"] == "99" and r["suffer_score"] == 80 and r["avg_hr"] == 150
    # Re-syncing Strava doesn't create a duplicate
    strava.store_activity(conn, STRAVA_ACT)
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1


def test_different_workouts_same_day_are_kept(conn):
    garmin.store_activity(conn, GARMIN_ACT)
    strava.store_activity(conn, {**STRAVA_ACT, "id": 100, "start_date_local": "2026-06-01T18:00:00Z"})
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 2


def test_garmin_daily_row_parses_payloads():
    class FakeGarmin:
        def get_sleep_data(self, d):
            return {"dailySleepDTO": {"sleepTimeSeconds": 27000, "deepSleepSeconds": 5000,
                                      "sleepStartTimestampLocal": 1780354800000,  # 2026-06-01 23:00 "local-as-UTC"
                                      "sleepEndTimestampLocal": 1780383600000,
                                      "sleepScores": {"overall": {"value": 81}}}}
        def get_hrv_data(self, d):
            return {"hrvSummary": {"lastNightAvg": 64, "baseline": {"balancedLow": 55, "balancedUpper": 72}, "status": "BALANCED"}}
        def get_stats(self, d):
            return {"restingHeartRate": 48, "totalSteps": 9000, "totalKilocalories": 2600}
        def get_max_metrics(self, d):
            return [{"generic": {"vo2MaxPreciseValue": 54.2}, "cycling": None}]
        def get_training_readiness(self, d):
            raise RuntimeError("not supported on this watch")
        def get_hydration_data(self, d):
            return {"valueInML": 2100}
        def get_nutrition_daily_food_log(self, d, *a):
            return {"dailyNutritionContent": {"consumedCalories": 2300, "protein": 130}}
        def get_lifestyle_logging_data(self, d):
            return {"dailyLogsReport": [{"name": "Alcohol", "logs": [{"amount": 2}]}]}

    r = garmin.daily_row(FakeGarmin(), "2026-06-02")
    assert r["sleep_s"] == 27000 and r["sleep_score"] == 81
    assert r["bedtime"] == "23:00" and r["wake_time"] == "07:00"
    assert r["hrv"] == 64 and r["resting_hr"] == 48 and r["vo2max_run"] == 54.2 and r["vo2max_ride"] is None
    assert r["readiness"] is None and r["water_ml"] == 2100
    assert r["kcal_in"] == 2300 and r["protein_g"] == 130 and r["drinks"] == 2


def test_api_end_to_end_with_demo_data(conn):
    from app import demo
    from app.main import app

    demo.seed(conn)
    c = TestClient(app)
    j = c.get("/api/dashboard?days=30").json()
    assert len(j["load"]) == 30 and len(j["days"]) == 30
    assert j["findings"] and 0 <= j["readiness"]["score"] <= 100

    assert c.put("/api/habits/2026-01-05", json={"bedtime": "23:10", "drinks": 2, "protein_g": 140}).status_code == 200
    h = c.get("/api/habits/2026-01-05").json()["manual"]
    assert h["bedtime"] == "23:10" and h["drinks"] == 2
    assert c.put("/api/habits/2026-01-05", json={"bedtime": "25:00"}).status_code == 422
    assert c.get("/api/habits/not-a-date").status_code == 400

    s = c.get("/api/settings").json()
    s["max_hr"] = 192
    assert c.put("/api/settings", json=s).json()["max_hr"] == 192
    assert c.put("/api/settings", json={**s, "max_hr": 500}).status_code == 422

    st = c.get("/api/status").json()
    assert st["counts"]["activities"] > 50 and st["strava"]["connected"] is False
