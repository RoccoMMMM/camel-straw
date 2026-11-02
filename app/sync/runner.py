"""Run provider syncs with logging and a lock so manual and scheduled syncs never overlap."""
from __future__ import annotations

import threading

from .. import db
from . import garmin, strava

_lock = threading.Lock()


def run(provider: str = "all", days: int | None = None) -> list[dict]:
    if not _lock.acquire(blocking=False):
        return [{"provider": provider, "ok": False, "message": "A sync is already running."}]
    try:
        results = []
        # Garmin first: it's the richer record, so Strava copies of the same workout merge into it.
        for name, mod in (("garmin", garmin), ("strava", strava)):
            if provider not in ("all", name):
                continue
            if provider == "all" and not (mod.configured() and (name != "strava" or db.get_token("strava"))):
                continue
            started = db.now()
            try:
                msg = mod.sync(days)
                ok = True
            except Exception as e:
                msg, ok = f"{name} sync failed: {e}", False
            db.log_sync(name, started, ok, msg)
            results.append({"provider": name, "ok": ok, "message": msg})
        if not results:
            results.append({"provider": provider, "ok": False,
                            "message": "Nothing to sync — connect Garmin or Strava in Settings first."})
        return results
    finally:
        _lock.release()
