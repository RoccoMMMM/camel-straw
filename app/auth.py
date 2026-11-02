"""Optional passcode protection, needed once the dashboard is reachable from your phone.

With APP_PASSCODE unset (the default) the app only listens on this computer and needs no login.
With it set, every page and API call requires a session cookie obtained by entering the passcode.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config

COOKIE = "session"
MAX_AGE = 180 * 24 * 3600
OPEN_PATHS = ("/login", "/logout", "/static/", "/manifest.webmanifest", "/sw.js", "/favicon.ico")
_failures: dict[str, deque] = defaultdict(deque)
MAX_FAILURES, WINDOW_S = 10, 15 * 60


def passcode() -> str | None:
    return os.getenv("APP_PASSCODE") or None


def _secret() -> bytes:
    path = config.DATABASE_PATH.parent / "session.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


def _token() -> str:
    # Bound to the passcode, so changing APP_PASSCODE signs every device out.
    return hmac.new(_secret(), b"v1:" + hashlib.sha256((passcode() or "").encode()).digest(), "sha256").hexdigest()


def is_authenticated(request: Request) -> bool:
    if passcode() is None:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _token())


async def middleware(request: Request, call_next):
    path = request.url.path
    if passcode() is None or path.startswith(OPEN_PATHS) or is_authenticated(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not signed in"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Sign in</title><link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<link rel="stylesheet" href="/static/styles.css"></head>
<body class="login-body"><form class="card login" method="post" action="/login">
<img src="/static/icons/icon-192.png" alt="" width="56" height="56">
<h1>Training Dashboard</h1>
<label>Passcode <input type="password" name="passcode" autocomplete="current-password" autofocus required></label>
<p class="login-error">__ERROR__</p>
<button class="btn" type="submit">Sign in</button></form></body></html>"""


def login_page(error: str = "") -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML.replace("__ERROR__", html.escape(error)))


async def login(request: Request):
    if passcode() is None:
        return RedirectResponse("/", status_code=303)
    ip = request.client.host if request.client else "?"
    now = time.time()
    fails = _failures[ip]
    while fails and now - fails[0] > WINDOW_S:
        fails.popleft()
    if len(fails) >= MAX_FAILURES:
        return login_page("Too many attempts. Wait 15 minutes and try again.")
    form = parse_qs((await request.body()).decode(errors="replace"))
    given = (form.get("passcode") or [""])[0]
    if not hmac.compare_digest(given.encode(), passcode().encode()):
        fails.append(now)
        return login_page("Wrong passcode.")
    fails.clear()
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, _token(), max_age=MAX_AGE, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https")
    return resp


def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
