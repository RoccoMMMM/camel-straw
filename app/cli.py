"""Command line helpers.

  python -m app.cli serve [--demo] [--port 8000]   run the dashboard
  python -m app.cli serve --phone                  ...and let your phone connect (needs APP_PASSCODE)
  python -m app.cli garmin-login                   log in to Garmin once (handles MFA), saves tokens
  python -m app.cli sync [--provider all] [--days N]
  python -m app.cli demo                           (re)generate data/demo.db with synthetic data
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

if sys.version_info < (3, 10):
    sys.exit(f"This app needs Python 3.10 or newer; you're running {sys.version.split()[0]}. "
             "See 'Quick start' in README.md for how to install a newer Python.")

from . import config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--demo", action="store_true", help="use data/demo.db (generated if missing)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--phone", action="store_true",
                   help="let your phone connect (listens on your network; requires APP_PASSCODE in .env)")
    sub.add_parser("garmin-login")
    y = sub.add_parser("sync")
    y.add_argument("--provider", default="all", choices=["all", "garmin", "strava"])
    y.add_argument("--days", type=int, default=None, help="backfill this many days (default: incremental)")
    sub.add_parser("demo")
    args = p.parse_args(argv)

    demo_path = config.ROOT / "data" / "demo.db"

    if args.cmd == "demo" or (args.cmd == "serve" and args.demo and not demo_path.exists()):
        from . import db, demo
        conn = db.connect(demo_path)
        demo.seed(conn)
        print(f"Demo data written to {demo_path}")
        if args.cmd == "demo":
            return 0

    if args.cmd == "serve":
        if args.demo:
            os.environ["DATABASE_PATH"] = str(demo_path)
            config.DATABASE_PATH = demo_path
            os.environ["AUTO_SYNC_HOURS"] = "0"
            config.AUTO_SYNC_HOURS = 0
        import uvicorn
        if args.phone:
            if not os.getenv("APP_PASSCODE"):
                print("To use the dashboard from your phone, first set a passcode: add a line like\n"
                      "  APP_PASSCODE=pick-something-long\n"
                      "to your .env file (nano .env), then run this command again.")
                return 1
            args.host = "0.0.0.0"
            print(f"Dashboard on this computer: http://localhost:{args.port}")
            ip = _lan_ip()
            if ip:
                print(f"On your phone (same Wi-Fi):  http://{ip}:{args.port}")
            print("Tailscale users: use this computer's Tailscale name or IP instead (see README → Phone app).")
        else:
            print(f"Dashboard: http://{args.host}:{args.port}")
        uvicorn.run("app.main:app", host=args.host, port=args.port, proxy_headers=True)
        return 0

    if args.cmd == "garmin-login":
        from .sync import garmin
        from garminconnect import Garmin
        email = config.GARMIN_EMAIL or input("Garmin email: ")
        password = config.GARMIN_PASSWORD or getpass.getpass("Garmin password: ")
        g = Garmin(email, password, prompt_mfa=lambda: input("MFA code: "))
        g.login()
        g.client.dump(garmin.token_dir())
        print(f"Logged in as {g.get_full_name()}. Tokens saved to {config.GARMIN_TOKEN_DIR}")
        return 0

    if args.cmd == "sync":
        from .sync import runner
        ok = True
        for r in runner.run(args.provider, args.days):
            print(("✓ " if r["ok"] else "✗ ") + r["message"])
            ok &= r["ok"]
        return 0 if ok else 1
    return 1


def _lan_ip() -> str | None:
    """This computer's address on the local network (no traffic is sent)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            return sock.getsockname()[0]
    except OSError:
        return None


if __name__ == "__main__":
    sys.exit(main())
