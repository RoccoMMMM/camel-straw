"""Turn uploaded or pasted training programming into planned sessions.

Three readers, tried in order of what fits:
  1. CSV with a header row (date or day, sport, duration, intensity, ...). No AI needed.
  2. Simple lines such as "Mon: 60 min easy run" or "2026-11-10 bike 90min intervals + 30 min gym". No AI needed.
  3. Claude, for anything else: coach spreadsheets pasted as text, PDFs, screenshots, multi-week blocks.
     Needs ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, timedelta
from typing import Any

from .sync.common import normalise_sport

INTENSITIES = ("easy", "moderate", "hard", "race", "rest")
SPORTS = ("run", "ride", "swim", "ruck", "walk", "hike", "strength", "row", "ski", "yoga", "multisport", "other", "rest")
WEEKDAYS = {name: i for i, names in enumerate([
    ("mon", "monday"), ("tue", "tues", "tuesday"), ("wed", "weds", "wednesday"), ("thu", "thur", "thurs", "thursday"),
    ("fri", "friday"), ("sat", "saturday"), ("sun", "sunday")]) for name in names}

INTENSITY_WORDS = [
    ("race", ("race", "competition", "time trial", "tt ", "parkrun", "event")),
    ("hard", ("interval", "vo2", "threshold", "hard", "sprint", "hill rep", "repeats", "max", "anaerobic", "z5", "zone 5",
              "fartlek", "speed", "track")),
    ("moderate", ("tempo", "steady", "moderate", "sweet spot", "sweetspot", "z3", "zone 3", "marathon pace", "mp ",
                  "progression")),
    ("easy", ("easy", "recovery", "z1", "z2", "zone 1", "zone 2", "aerobic", "long slow", "lsd", "shakeout", "base",
              "conversational", "mobility")),
]
SPORT_WORDS = [
    ("rest", ("rest day", "rest", "off day", "day off")),
    ("ruck", ("ruck",)), ("swim", ("swim", "pool", "open water")),
    ("ride", ("bike", "ride", "cycle", "cycling", "zwift", "trainer", "spin", "turbo")),
    ("strength", ("gym", "strength", "lift", "weights", "squat", "deadlift", "bench", "kettlebell", "core", "hyrox", "crossfit")),
    ("row", ("row", "erg")), ("hike", ("hike",)), ("walk", ("walk",)), ("yoga", ("yoga", "pilates", "stretch")),
    ("multisport", ("brick", "triathlon", "duathlon")),
    ("run", ("run", "jog", "5k", "10k", "half marathon", "marathon", "km", "miles", "mi ")),
]


def _intensity(text: str) -> str:
    t = f" {text.lower()} "
    for level, words in INTENSITY_WORDS:
        if any(w in t for w in words):
            return level
    return "moderate"


def _sport(text: str) -> str | None:
    t = f" {text.lower()} "
    for sport, words in SPORT_WORDS:
        # Whole words only, so "min" isn't read as "mi" (miles); a number may sit right before it ("10km").
        if any(re.search(rf"(?<![a-z]){re.escape(w.strip())}(?![a-z])", t) for w in words):
            return sport
    if re.search(r"\d+\s*x\s*\d+\s*m\b", t):      # 6x800m, 10 x 400m: track intervals
        return "run"
    return None


def _minutes(text: str) -> float | None:
    t = text.lower()
    if m := re.search(r"(\d+)\s*h(?:ours?|rs?)?\s*(\d+)\s*(?:m|min|mins|minutes)?\b", t):
        return int(m.group(1)) * 60 + int(m.group(2))
    # 1:30 means 1 h 30 — but not a pace like 4:30/km or a clock time like 6:30am.
    if m := re.search(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\b(?!\s*(?:am|pm|/|per\b))", t):
        return int(m.group(1)) * 60 + int(m.group(2))
    if m := re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", t):
        return float(m.group(1)) * 60
    # A bare "m" is metres in most programming (6x800m), so only min/mins/minutes/' count as minutes.
    if m := re.search(r"(\d+(?:\.\d+)?)\s*(?:min|mins|minutes|')(?:\b|$|\s)", t):
        return float(m.group(1))
    return None


def _km(text: str) -> float | None:
    t = text.lower()
    if m := re.search(r"(\d+(?:\.\d+)?)\s*(?:km|k)\b", t):
        return float(m.group(1))
    if m := re.search(r"(\d+(?:\.\d+)?)\s*(?:mi|mile|miles)\b", t):
        return round(float(m.group(1)) * 1.609, 1)
    if "half marathon" in t:
        return 21.1
    if "marathon" in t:
        return 42.2
    return None


def _date_for(token: str, week_start: date) -> date | None:
    token = token.strip().lower().rstrip(":.-")
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", token):
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if token in WEEKDAYS:
        return week_start + timedelta(days=WEEKDAYS[token])
    return None


def session(day: date, sport: str, title: str, minutes: float | None, km: float | None, intensity: str,
            notes: str = "") -> dict[str, Any]:
    if sport == "rest":
        intensity, minutes = "rest", 0
    return {"date": day.isoformat(), "sport": sport, "title": title.strip()[:120] or sport.title(),
            "duration_min": round(minutes) if minutes else None, "distance_km": km, "intensity": intensity,
            "notes": notes.strip()[:500]}


# ---- 1. CSV ------------------------------------------------------------------

def parse_csv(text: str, week_start: date) -> list[dict] | None:
    sample = text.strip().splitlines()[:1]
    if not sample or ("," not in sample[0] and "\t" not in sample[0]):
        return None
    reader = csv.DictReader(io.StringIO(text.strip()), dialect=csv.Sniffer().sniff(sample[0], delimiters=",\t;"))
    fields = {(f or "").strip().lower(): f for f in reader.fieldnames or []}
    def col(*names):
        return next((fields[n] for n in names if n in fields), None)
    c_date, c_day = col("date"), col("day", "weekday")
    c_sport = col("sport", "type", "activity", "discipline")
    c_dur = col("duration_min", "duration", "minutes", "time", "mins")
    if not (c_date or c_day) or not (c_sport or col("session", "workout", "description")):
        return None
    c_desc = col("session", "workout", "description", "title", "name")
    c_int, c_km, c_notes = col("intensity", "effort", "zone"), col("distance_km", "distance", "km"), col("notes", "comments")
    out = []
    for r in reader:
        raw_day = (r.get(c_date) if c_date else None) or (r.get(c_day) if c_day else "") or ""
        day = _date_for(raw_day, week_start)
        if not day:
            continue
        desc = (r.get(c_desc) or "") if c_desc else ""
        text_all = " ".join(str(v) for v in r.values() if v)
        sport = _sport(r.get(c_sport) or "") if c_sport else None
        sport = sport or _sport(desc) or (normalise_sport(r.get(c_sport)) if c_sport else "other")
        dur = r.get(c_dur) if c_dur else None
        minutes = (float(dur) if dur and re.fullmatch(r"\d+(\.\d+)?", dur.strip()) else _minutes(dur or "")) if dur else _minutes(desc)
        km_raw = r.get(c_km) if c_km else None
        km = float(km_raw) if km_raw and re.fullmatch(r"\d+(\.\d+)?", km_raw.strip()) else _km(km_raw or desc)
        intensity = (r.get(c_int) or "").strip().lower() if c_int else ""
        intensity = intensity if intensity in INTENSITIES else _intensity(intensity + " " + text_all)
        out.append(session(day, sport, desc or (r.get(c_sport) or ""), minutes, km, intensity, r.get(c_notes) or "" if c_notes else ""))
    return out or None


# ---- 2. simple lines ---------------------------------------------------------

def _clean_title(text: str) -> str:
    """Drop the time from a title, since duration is shown separately: "gym 45 min" -> "Gym"."""
    t = re.sub(r"\b\d+\s*h(?:ours?|rs?)?\s*\d+\s*(?:min|mins|minutes)?\b|\b\d{1,2}:\d{2}(?::\d{2})?\b(?!\s*/)"
               r"|\b\d+(?:\.\d+)?\s*(?:h|hr|hrs|hour|hours|min|mins|minutes)\b|\d+(?:\.\d+)?'", " ", text)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,-–:")
    return (t[:1].upper() + t[1:]) if t else text

LINE_RE = re.compile(r"^\s*(?P<day>\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9})\b[\s:.\-–]*(?P<rest>.*)$")


def parse_lines(text: str, week_start: date) -> list[dict] | None:
    out, unmatched = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        m = LINE_RE.match(line)
        day = _date_for(m.group("day"), week_start) if m else None
        if not day:
            unmatched += 1
            continue
        for part in re.split(r"\s+\+\s+|;|\s+then\s+|\s+&\s+", m.group("rest")):
            part = part.strip(" ,")
            if not part:
                continue
            sport = _sport(part)
            if sport is None:
                if _minutes(part) is None:
                    unmatched += 1
                    continue
                sport = "other"     # a timed session with no sport named, e.g. "tempo 40 min"
            out.append(session(day, sport, _clean_title(part), _minutes(part), _km(part), _intensity(part)))
    # If most lines didn't fit the simple format, let Claude read it instead.
    if not out or unmatched > len(out):
        return None
    return out


# ---- 3. Claude ---------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "sessions": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "sport": {"type": "string", "enum": list(SPORTS)},
                "title": {"type": "string"},
                "duration_min": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                "distance_km": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                "intensity": {"type": "string", "enum": list(INTENSITIES)},
                "notes": {"type": "string"},
            },
            "required": ["date", "sport", "title", "duration_min", "distance_km", "intensity", "notes"],
            "additionalProperties": False,
        }},
        "assumptions": {"type": "string"},
    },
    "required": ["sessions", "assumptions"],
    "additionalProperties": False,
}

PROMPT = """Extract every training session from this athlete's programming into structured sessions.

Rules:
- Week 1 (or the first week, or an undated plan) starts on {week_start} ({week_start_day}). Map weekdays and \
"Week N" labels onto real dates from there; dated plans keep their dates. Today is {today}.
- One entry per session. A day with a run and a gym session is two entries. Include explicit rest days as \
sport "rest".
- duration_min: total time including warm-up and cool-down. If only a distance is given, estimate the time \
(the athlete's easy run pace is about {easy_pace} min/km) and say "(time estimated)" in notes. Strength or \
other sessions without a time: estimate a typical length and say so.
- intensity: easy (Z1-2, recovery, long easy), moderate (tempo, steady, sweet spot), hard (intervals, \
threshold, VO2, hills, heavy lifting), race (races, time trials, test events), rest.
- title: short, like "6x800m intervals" or "Long ride". notes: the key details of the session (sets, paces, \
zones, loads), briefly.
- assumptions: one or two sentences on anything you had to guess (dates, durations), or "" if nothing."""


def parse_with_claude(text: str | None, week_start: date, today: date, file: dict | None = None,
                      easy_pace: float = 6.0, client: Any = None) -> tuple[list[dict], str]:
    import anthropic

    from .coach import MODEL

    content: list[dict] = []
    if file:
        mt = file["media_type"]
        kind = "document" if mt == "application/pdf" else "image"
        content.append({"type": kind, "source": {"type": "base64", "media_type": mt, "data": file["data"]}})
    instructions = PROMPT.format(week_start=week_start.isoformat(), week_start_day=week_start.strftime("%A"),
                                 today=today.isoformat(), easy_pace=f"{easy_pace:.1f}")
    content.append({"type": "text", "text": instructions + ("\n\nProgramming:\n" + text if text else "")})
    client = client or anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=16000, messages=[{"role": "user", "content": content}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise ValueError("Claude couldn't read this programming. Try pasting it as text.")
    if resp.stop_reason == "max_tokens":
        raise ValueError("The programming is too long to read in one go. Upload a few weeks at a time.")
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    sessions = []
    for s in data["sessions"]:
        try:
            day = date.fromisoformat(s["date"])
        except ValueError:
            continue
        sessions.append(session(day, s["sport"] if s["sport"] in SPORTS else "other", s["title"], s["duration_min"],
                                s["distance_km"], s["intensity"] if s["intensity"] in INTENSITIES else "moderate",
                                s.get("notes") or ""))
    return sessions, data.get("assumptions") or ""


def parse(text: str | None, week_start: date, today: date, file: dict | None = None, ai_available: bool = False,
          easy_pace: float = 6.0) -> dict[str, Any]:
    """Returns {"sessions": [...], "method": "csv" | "lines" | "claude", "assumptions": str}."""
    if not file and text:
        for method, fn in (("csv", parse_csv), ("lines", parse_lines)):
            try:
                found = fn(text, week_start)
            except (csv.Error, ValueError):
                found = None
            if found:
                return {"sessions": found, "method": method, "assumptions": ""}
    if not ai_available:
        raise ValueError(
            "Couldn't read this without AI. Either add ANTHROPIC_API_KEY to .env so Claude can read it, or use "
            "one line per session like \"Mon: 60 min easy run\" or \"2026-11-10 bike 90 min intervals\".")
    sessions, assumptions = parse_with_claude(text, week_start, today, file, easy_pace)
    if not sessions:
        raise ValueError("No training sessions found in that.")
    return {"sessions": sessions, "method": "claude", "assumptions": assumptions}
