import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app import coach, db, demo
from app.sync import garmin
from app.sync.common import normalise_sport


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.set_conn(c)
    demo.seed(c)
    coach.ensure_schema(c)
    yield c
    db.set_conn(None)


def test_explain_activity_finds_ruck_and_context(conn):
    out = coach.tool_explain_activity(conn, {"date": date.today().isoformat(), "sport": "ruck"})
    assert out["session"]["sport"] == "ruck"
    comp = out["comparable_sessions_last_90d"]
    assert comp["n_sessions"] == 4 and comp["efficiency_vs_usual_pct"] < -10   # slower at a higher HR
    ev = out["multisport_events_prior_21_days"]
    assert len(ev) == 1 and ev[0]["kind"] == "Triathlon" and ev[0]["sports"] == ["swim", "ride", "run"]
    assert ev[0]["load"] > 300 and ev[0]["total_min"] == 153
    assert out["recovery_last_4_mornings"] and out["personal_baselines_60d"]["hrv"]


def test_explain_activity_missing(conn):
    out = coach.tool_explain_activity(conn, {"date": "2001-01-01"})
    assert "error" in out


def test_log_habits_merges_and_validates(conn):
    d = (date.today() - timedelta(days=1)).isoformat()
    conn.execute("DELETE FROM habits WHERE date=?", (d,))
    assert coach.tool_log_habits(conn, {"date": d, "drinks": 3, "notes": "work party"})["saved"]
    assert coach.tool_log_habits(conn, {"date": d, "bedtime": "00:30", "notes": "felt rough"})["saved"]
    row = dict(conn.execute("SELECT * FROM habits WHERE date=?", (d,)).fetchone())
    assert row["drinks"] == 3 and row["bedtime"] == "00:30" and row["notes"] == "work party\nfelt rough"
    assert "error" in coach.tool_log_habits(conn, {"date": d, "bedtime": "25:99"})
    assert "error" in coach.tool_log_habits(conn, {"date": d})


def test_every_tool_returns_json(conn):
    for name in coach.TOOLS:
        if name in ("sync_data", "log_habits"):
            continue
        out, err = coach.run_tool(conn, name, {})
        json.loads(out)
        assert not err, (name, out)
    assert coach.run_tool(conn, "get_activities", {"start_date": "not-a-date"})[1] is True
    assert coach.run_tool(conn, "nope", {})[1] is True


class FakeClient:
    """Plays back: a tool call (explain_activity + log_habits), then a text answer."""

    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        blk = lambda **d: SimpleNamespace(model_dump=lambda exclude_none=True, d=d: d)
        if len(self.calls) == 1:
            return SimpleNamespace(stop_reason="tool_use", content=[
                blk(type="thinking", thinking="", signature="sig"),
                blk(type="tool_use", id="t1", name="explain_activity", input={"sport": "ruck"}),
                blk(type="tool_use", id="t2", name="log_habits",
                    input={"date": date.today().isoformat(), "sleep_quality": 2}),
            ])
        return SimpleNamespace(stop_reason="end_turn", content=[blk(type="text", text="Your triathlon is the main reason.")])


def test_chat_loop_runs_tools_and_persists(conn):
    fake = FakeClient()
    r = coach.chat("Why was my ruck so bad today?", client=fake)
    assert r["reply"] == "Your triathlon is the main reason."
    assert set(r["tools"]) == {"session analysis", "habit log"} and r["logged"][0]["fields"] == {"sleep_quality": 2}
    second = fake.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert {b["tool_use_id"] for b in second[2]["content"]} == {"t1", "t2"}
    assert second[1]["content"][0]["type"] == "thinking"       # replayed verbatim
    assert fake.calls[0]["fallbacks"] == "default" and fake.calls[0]["model"] == coach.MODEL
    t = coach.transcript(conn, r["conversation_id"])
    assert [m["role"] for m in t] == ["user", "assistant"] and t[1]["tools"]
    # A follow-up continues the same conversation
    fake2 = FakeClient()
    fake2.calls.append({})   # skip straight to the text reply
    coach.chat("thanks", r["conversation_id"], client=fake2)
    assert [m["role"] for m in fake2.calls[1]["messages"]] == ["user", "assistant", "user", "assistant", "user"]


def test_ruck_and_multisport_normalisation():
    assert normalise_sport("hiking", "Morning Ruck") == "ruck"
    assert normalise_sport("multi_sport") == "multisport"
    assert normalise_sport("Triathlon") == "multisport"


def test_garmin_multisport_parent_dropped_when_legs_present():
    acts = [{"activityId": 1, "childIds": [2, 3]}, {"activityId": 2}, {"activityId": 3}, {"activityId": 4, "childIds": [9]}]
    assert [a["activityId"] for a in garmin.drop_multisport_parents(acts)] == [2, 3, 4]


def test_strava_tri_legs_fold_into_garmin_multisport(tmp_path):
    from app.sync import strava
    c = db.connect(tmp_path / "x.db")
    legs = [("Swim", "07:00:00", 1500), ("Ride", "07:30:00", 4200), ("Run", "08:45:00", 2700)]
    for i, (sport, t, dur) in enumerate(legs):
        strava.store_activity(c, {"id": 100 + i, "name": f"Tri {sport}", "sport_type": sport,
                                  "start_date_local": f"2026-06-07T{t}Z", "elapsed_time": dur, "suffer_score": 50})
    assert garmin.store_activity(c, {"activityId": 77, "activityName": "Triathlon", "activityType": {"typeKey": "multi_sport"},
                                     "startTimeLocal": "2026-06-07 07:00:00", "duration": 9000}) == "merged"
    rows = db.rows(c, "SELECT * FROM activities")
    assert len(rows) == 1 and rows[0]["sport"] == "multisport" and rows[0]["suffer_score"] == 150
    # and a Strava leg arriving after the Garmin parent is merged, not duplicated
    strava.store_activity(c, {"id": 200, "name": "Tri Run", "sport_type": "Run",
                              "start_date_local": "2026-06-07T08:46:00Z", "elapsed_time": 2600})
    assert c.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1


def test_group_events():
    from app.analytics import group_events
    def a(i, sport, t, mins, day="2026-06-07"):
        return {"id": str(i), "sport": sport, "date": day, "start_time": f"{day}T{t}", "duration_s": mins * 60, "load": 10}
    acts = [a(1, "swim", "07:00:00", 30), a(2, "ride", "07:33:00", 70), a(3, "run", "08:50:00", 45),   # tri
            a(4, "ride", "07:00:00", 90, "2026-06-08"), a(5, "run", "08:35:00", 20, "2026-06-08"),       # brick
            a(6, "run", "07:00:00", 40, "2026-06-09"), a(7, "ride", "18:00:00", 60, "2026-06-09"),       # separate
            a(8, "strength", "08:00:00", 30, "2026-06-07")]
    ev = group_events(acts)
    assert [e["kind"] for e in ev] == ["Triathlon", "Brick"]
    assert ev[0]["legs"] == ["1", "2", "3"] and ev[0]["load"] == 30 and ev[0]["total_min"] == 155
    assert acts[0]["event"] == "Triathlon" and "event" not in acts[5] and "event" not in acts[7]


def test_unworn_watch_night_is_not_zero_sleep():
    class G:
        def get_sleep_data(self, d): return {"dailySleepDTO": {"sleepTimeSeconds": 0, "sleepStartTimestampLocal": None,
                                                               "sleepScores": {"overall": {"value": 0}}}}
        def get_stats(self, d): return {"restingHeartRate": 52, "totalSteps": 4000}
        def __getattr__(self, n): return lambda *a: None
    r = garmin.daily_row(G(), "2026-06-02")
    assert r["sleep_s"] is None and r["sleep_score"] is None and r["bedtime"] is None and r["resting_hr"] == 52


def test_manual_sleep_fills_unworn_nights():
    from app.analytics import merge_days
    d1, d2 = date(2026, 6, 1), date(2026, 6, 2)
    rows = merge_days([{"date": "2026-06-01", "sleep_s": 27000, "bedtime": "23:00", "wake_time": "06:30"}],
                      [{"date": "2026-06-02", "bedtime": "23:30", "wake_time": "07:00", "drinks": 1}], d1, d2)
    assert rows[0]["sleep_source"] == "watch" and rows[1]["sleep_source"] == "manual log"
    assert rows[1]["sleep_s"] == 7.5 * 3600
