from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import analytics, db, demo, export


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.set_conn(c)
    demo.seed(c)
    yield c
    db.set_conn(None)


def test_export_has_every_section(conn):
    ctx = analytics.load_context(conn, db.get_settings(conn), days=120)
    md = export.build(ctx, 30)
    for heading in ("# My training data", "## Profile and targets", "## Status today", "## Issues the dashboard flagged",
                    "## Daily log", "## Activities", "## Weekly totals"):
        assert heading in md, heading
    assert "Ruck 20kg" in md and "Triathlon" in md          # demo ruck + grouped Strava tri legs
    assert "| W |" in md                                    # watch nights are marked
    md90 = export.build(ctx, 90, ["daily"])
    assert "| M |" in md90                                  # ...and so are nights worked out from the manual log
    assert "resting_hr" not in md                           # readable metric names only
    daily = md.split("## Daily log")[1].split("## Activities")[0]
    assert daily.count("\n| ") - 1 == 30                    # header row + 30 days


def test_export_sections_and_period(conn):
    ctx = analytics.load_context(conn, db.get_settings(conn), days=120)
    md = export.build(ctx, 7, ["activities"])
    assert "## Activities" in md and "## Daily log" not in md and "## Status today" not in md
    assert md.startswith("# My training data: ") and f"to {date.today().isoformat()} (7 days)" in md


def test_number_formatting():
    assert export._num(10) == "10" and export._num(2.5) == "2.5" and export._num(0) == "0" and export._num(None) == ""
    assert export._table(["A", "B"], [["1", ""], ["2", ""]]).splitlines()[0] == "| A |"   # empty column dropped


def test_export_endpoint(conn):
    from app.main import app
    c = TestClient(app)
    r = c.get("/api/export?days=14&sections=summary,weekly")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "## Weekly totals" in r.text and "## Activities" not in r.text
    assert c.get("/api/export?days=1000").status_code == 422
    assert c.get("/api/export?sections=<script>").status_code == 422
