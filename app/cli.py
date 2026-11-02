"""Command line helpers.

  python -m app.cli serve [--demo] [--port 8000]   run the dashboard
  python -m app.cli garmin-login                   log in to Garmin once (handles MFA), saves tokens
  python -m app.cli sync [--provider all] [--days N]
  python -m app.cli demo                           (re)generate data/demo.db with synthetic data
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from . import config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--demo", action="store_true", help="use data/demo.db (generated if missing)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
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
        print(f"Dashboard: http://{args.host}:{args.port}")
        uvicorn.run("app.main:app", host=args.host, port=args.port)
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


if __name__ == "__main__":
    sys.exit(main())
