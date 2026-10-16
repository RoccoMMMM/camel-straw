# camel-straw — personal training dashboard

A self-hosted dashboard that pulls your **Garmin** and **Strava** data, tracks fitness progress,
**diagnoses problems as they show up** (overreaching, poor recovery, sleep, alcohol, under-fuelling),
and logs daily habits: nutrition, sleep, drinks, bed and wake times. It also has a **Coach chat**: ask questions
about your own numbers ("why was my ruck so bad today?") and log habits by just typing them.

Everything runs on your own computer. Data is stored in one SQLite file (`data/fitness.db`).

## What you get

| Tab | What's in it |
|---|---|
| **Overview** | Readiness score (0–100), key numbers, a ranked list of issues with what to do about each, fitness/fatigue chart |
| **Coach** | Chat with an AI coach that reads your synced data before answering, explains good and bad sessions, and logs habits you type in |
| **Training** | Fitness (CTL) / fatigue (ATL) / form (TSB), weekly hours by sport, aerobic-efficiency trend, VO₂ max, intensity mix, activity list |
| **Recovery & Sleep** | HRV vs baseline, resting HR, sleep duration vs target, bed→wake schedule, sleep score, stress and Body Battery |
| **Habits** | Daily log (bed/wake time, sleep quality, drinks, calories, macros, water, caffeine, energy, soreness, notes), charts, and **your own data on how habits affect recovery** (for example "after 2+ drinks your HRV is 7 ms lower") |
| **Settings** | Connect Garmin and Strava, athlete profile (max HR, resting HR, weight, targets), sync controls and log |

### What it diagnoses

Rules live in `app/diagnostics.py`. Each finding has a severity (critical, serious, warning, good or info) and a suggested action.

- **Training load:** deep fatigue (TSB below −30), detraining, ramp rate above 5–8 CTL/week, acute:chronic workload spikes (above 1.3 or 1.5), training monotony (Foster), too much "grey-zone" intensity, gaps in training
- **Fitness trend:** aerobic efficiency (pace or power per heartbeat on easy sessions) rising or falling compared with the previous 8 weeks
- **Recovery:** resting HR above your 30-day baseline, HRV suppressed against your 60-day baseline, high all-day stress, Body Battery not recharging, and a combined **overreaching** alert when load, RHR and HRV all point the wrong way
- **Sleep:** short sleep and sleep debt, irregular bedtime or wake time, late bedtimes, weekend "social jet lag"
- **Alcohol:** weekly total against your limit, heavy single sessions, and your personal HRV cost of drinking
- **Nutrition:** protein in g/kg against your target, and energy deficit (logged intake against Garmin's calorie burn, a RED-S risk)

These are coaching heuristics, not medical advice.

### The Coach chat

The Coach tab is a conversation with Claude (Anthropic's AI model). It has tools that read your data, so its answers
come from your actual numbers rather than generic advice. For example:

- **"Why was my ruck so bad today?"** It compares the session with your similar sessions from the last 90 days
  (pace, heart rate, efficiency). It then checks your training load and form going in, your biggest efforts in the
  previous two weeks (a triathlon, say), and the last few nights of sleep, HRV, resting HR, alcohol and food.
  It ranks the likely causes and tells you what to do next.
- **"Last night: 3 beers, bed at 00:30, up at 6:45, slept badly."** It saves these to your habit log for the right
  date and confirms what it saved. The charts and diagnostics update straight away.
- **"Should I train hard tomorrow?"** or **"What's holding back my fitness?"** It answers from your readiness,
  findings and trends.

If an activity hasn't synced yet, the coach can trigger a sync itself. Conversations are saved locally and listed under Recent.

**Setup:** create an API key at <https://platform.claude.com/>, put it in `.env` as `ANTHROPIC_API_KEY`, and restart.
The coach uses the `claude-opus-5-5` model at medium effort by default. Change this with `COACH_MODEL` and
`COACH_EFFORT` in `.env`.

**Cost and privacy:** each message is a paid API call, typically a few cents. A question that pulls months of
data costs more. To answer, the app sends the data the coach looks up (activities, sleep, HRV, habits and notes)
to Anthropic's API. Everything else stays on your computer.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Try it with 6 months of realistic sample data (no accounts needed):
python -m app.cli serve --demo          # → http://localhost:8000
# The demo includes a triathlon 9 days ago and a rough ruck today. Add ANTHROPIC_API_KEY to .env
# and ask the Coach "why was my ruck so bad today?"
```

To use your own data:

```bash
cp .env.example .env                    # then fill it in (see below)
python -m app.cli serve                 # → http://localhost:8000, then Settings → connect → Sync
```

### Connect Garmin

Garmin's official API is only open to approved companies, so this uses the
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library, the same one most
self-hosted Garmin tools use. You have two options:

- **In the app:** go to Settings, enter your Garmin email and password, then the MFA code if asked.
- **From the terminal:** run `python -m app.cli garmin-login`.

Either way, only the login **tokens** are saved, in `data/garmin_tokens/`. Your password is not stored.

What gets pulled: activities, sleep (stages, score, bed and wake times), overnight HRV, resting HR, steps,
stress, Body Battery, calories burned, VO₂ max and training readiness. If you log them in Garmin Connect,
it also pulls **food, hydration and alcohol**.

### Connect Strava

1. Create an API app at <https://www.strava.com/settings/api> and set **Authorization Callback Domain** to `localhost`.
2. Put `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` in `.env`.
3. Restart the app, then go to Settings → **Connect Strava**.

A workout recorded on your Garmin **and** uploaded to Strava is matched by start time and duration and
**counted once**. The richer Garmin record is kept, and Strava's relative effort is attached to it.

**Triathlons and rucks:** a Garmin multisport activity counts once. If Garmin also lists the separate swim, bike
and run legs, the legs are kept and the multisport total is dropped. If Strava has the legs as separate activities,
they're folded into the Garmin race. Neither Garmin nor Strava has a rucking sport type, so any activity with
"ruck" in its name is treated as a ruck.

### Syncing

- The **Sync now** button pulls new data. The first sync backfills `INITIAL_SYNC_DAYS`, 120 by default.
  Use **Backfill 1 year** in Settings for more history.
- While the server runs it re-syncs every `AUTO_SYNC_HOURS`, 6 by default.
- From the terminal: `python -m app.cli sync [--provider garmin|strava] [--days 365]`. This also works from cron.

### Logging habits

Each day's entry holds **last night's** sleep (bedtime, wake time and quality) plus **that day's** food
and drinks. Garmin keys sleep the same way. Leave a field blank to use whatever Garmin recorded.
Anything you type overrides Garmin.

## How the numbers work

- **Training load per activity:** Banister TRIMP from average HR when HR was recorded. If not, Garmin training load,
  then Strava relative effort, then a duration estimate. Set your max and resting HR in Settings so this is accurate.
- **Fitness (CTL)** is the 42-day exponentially weighted average of daily load. **Fatigue (ATL)** is the 7-day average.
  **Form (TSB)** = fitness − fatigue going into the day.
- **Readiness** starts at 100 and subtracts the worst open issue in each category (recovery, training, sleep,
  alcohol, nutrition).

## Project layout

```
app/
  main.py          FastAPI: JSON API + serves the dashboard
  analytics.py     load model, CTL/ATL/TSB, weekly volume, efficiency, habit correlations
  diagnostics.py   the rule engine and readiness score
  coach.py         Coach chat: Claude conversation, data-lookup tools, habit logging
  db.py            SQLite schema and helpers
  sync/garmin.py   Garmin import (activities + daily wellness)
  sync/strava.py   Strava OAuth + import, dedupe against Garmin
  demo.py          synthetic data generator
  cli.py           serve / sync / garmin-login / demo
  static/          index.html, app.js, styles.css, vendored Chart.js 4.4.1 (MIT)
tests/             pytest suite (python -m pytest)
```

## Notes

- Keep it on `localhost`. There is no login screen, and anyone who can reach the server can read your data and change your settings.
- Garmin can change its private endpoints without warning. The importer looks values up by name and keeps
  going if one endpoint fails, so you lose one metric rather than the whole sync.
