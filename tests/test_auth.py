import pytest
from fastapi.testclient import TestClient

from app import auth, config, db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "t.db")
    db.set_conn(db.connect(tmp_path / "t.db"))
    auth._failures.clear()
    from app.main import app
    yield TestClient(app)
    db.set_conn(None)


def test_open_when_no_passcode(client, monkeypatch):
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    assert client.get("/api/settings").status_code == 200
    assert client.get("/login", follow_redirects=False).status_code == 303


def test_passcode_required(client, monkeypatch):
    monkeypatch.setenv("APP_PASSCODE", "correct horse")
    assert client.get("/api/settings").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    # The login page, manifest, icons and service worker stay reachable.
    for path in ("/login", "/manifest.webmanifest", "/sw.js", "/static/icons/icon-192.png"):
        assert client.get(path).status_code == 200, path

    r = client.post("/login", data={"passcode": "wrong"}, follow_redirects=False)
    assert r.status_code == 200 and "Wrong passcode" in r.text
    assert client.get("/api/settings").status_code == 401

    r = client.post("/login", data={"passcode": "correct horse"}, follow_redirects=False)
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/status").json()["passcode_enabled"] is True

    # Changing the passcode signs every device out.
    monkeypatch.setenv("APP_PASSCODE", "new passcode")
    assert client.get("/api/settings").status_code == 401

    client.get("/logout")
    monkeypatch.setenv("APP_PASSCODE", "correct horse")
    assert client.get("/api/settings").status_code == 401


def test_lockout_after_repeated_failures(client, monkeypatch):
    monkeypatch.setenv("APP_PASSCODE", "pw")
    for _ in range(auth.MAX_FAILURES):
        client.post("/login", data={"passcode": "nope"})
    r = client.post("/login", data={"passcode": "pw"}, follow_redirects=False)
    assert "Too many attempts" in r.text and client.get("/api/settings").status_code == 401


def test_phone_mode_refuses_without_passcode(monkeypatch, capsys):
    from app import cli
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    assert cli.main(["serve", "--phone"]) == 1
    assert "APP_PASSCODE" in capsys.readouterr().out
