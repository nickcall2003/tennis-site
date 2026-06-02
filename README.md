# Tennis Site — Full Backbone

A runnable skeleton of the automated tennis prediction + live-scoring site:
daily predictions across ATP / WTA / Challenger / ITF, live point-by-point
scores pushed to the browser, and an expandable per-match stats panel.

It runs **today with zero cost and no API keys** using a built-in mock feed
that simulates live matches. Swapping in a real paid feed is a one-line change.

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

You'll see five matches across all four tiers. Scores tick point-by-point,
the LIVE badge pulses, the prediction bar shows the model's probability, value
edges are flagged, and tapping a card opens serve stats — except the ITF match,
which shows "stats not available" on purpose, exactly as real ITF data behaves.

## How the whole thing fits together

```
   data provider (mock now, real feed later)
            │  poll every few seconds   (app/live.py)
            ▼
        Postgres / SQLite  ◄── daily predictions (app/seed.py + app/predictions.py)
            │
            ├── REST  /api/matches        (app/main.py)
            └── WebSocket  /ws/live  ──►  every open browser  (app/ws.py)
```

The rule that keeps costs sane: **your server talks to the paid feed; users
talk to your database.** One poller process hits the provider on a schedule;
thousands of users read cached data and get pushed updates. Users never trigger
a provider call.

## The files

| File | Role |
|------|------|
| `app/providers/base.py` | The adapter contract — neutral dataclasses every feed maps to. |
| `app/providers/mock.py` | Simulated live feed (point-by-point match engine) so the demo runs free. |
| `app/providers/goalserve.py` | Stub showing exactly where a real paid feed plugs in. |
| `app/predictions.py` | Wraps the Elo engine; presets demo ratings or trains from a CSV. |
| `app/weather.py` | Composes match weather: venue registry + Open-Meteo + indoor/outdoor gating. |
| `app/analysis.py` | Auto-generated match writeups (template backend + optional LLM backend). |
| `app/elo.py`, `app/odds.py` | The rating engine and the de-vig / value math (from the prototype). |
| `app/models.py`, `app/db.py` | Database schema (SQLite dev → Postgres prod via `DATABASE_URL`). |
| `app/live.py` | The poller: pulls scores/stats, diffs, persists, broadcasts. |
| `app/ws.py` | WebSocket connection manager (the push channel). |
| `app/seed.py` | The "daily job" in miniature: schedule → predict → de-vig → store. |
| `app/main.py` | FastAPI app: REST + WebSocket + serves the demo page. |
| `app/static/index.html` | Self-contained demo UI (vanilla JS) so the backend runs standalone. |
| `frontend/MatchCard.jsx` | The React/Next.js version of the card for your real frontend. |

## Going live (the one-line change)

In `app/main.py`:

```python
provider = MockTennisProvider()
```

becomes, once you have credentials:

```python
from app.providers.goalserve import GoalserveProvider
provider = GoalserveProvider()   # reads GOALSERVE_API_KEY
```

Then implement the three methods in `goalserve.py` (or whichever provider you
pick) by mapping their fields onto `LiveScore` / `MatchStats`. **Nothing else
in the codebase changes** — that's the point of the adapter.

For real predictions, in `app/seed.py` swap `engine.preset_demo_ratings()` for
`engine.train_from_csv("matches.csv")` pointed at Sackmann's data.

## Weather & AI writeups

**Weather** isn't a tennis API — you compose it. `app/weather.py` maps each
tournament to its venue's lat/long (the `VENUES` table — geocode once, store
it), calls Open-Meteo for the match's time, and **only** for outdoor venues.
Indoor/roofed events return a "not a factor" report so the UI never shows
irrelevant wind speeds. Open-Meteo is free for non-commercial use with no key;
budget for their paid plan (or self-hosting) once the site is commercial.

**Writeups** are in `app/analysis.py`. The reliable pattern: do the thinking in
code, let the model only do the wording. `assemble`-style `MatchContext` gathers
facts (probability, edge, form, surface, H2H, weather); the template backend
formats them deterministically and *cannot* invent a number. To get natural
prose, set `LLM_COMPLETE` in `app/seed.py` to a function that calls your model
(an Anthropic example is in the comments) — the same facts go in, with a strict
"use only these facts" prompt. A 2-3 sentence writeup costs a fraction of a cent,
so generating one per match in the daily job is negligible.

## Production notes (when you're ready)

- **Database:** set `DATABASE_URL` to Postgres. The models are already portable.
- **The poller** should run as its own process (or a Celery/RQ worker), not
  inside the web server. The in-server task here is for the demo.
- **Daily job:** run `seed.build_today()` on a schedule (cron / GitHub Actions)
  every morning to publish the day's predictions.
- **Speed of the demo:** matches play at `POLL_SECONDS = 1.5` in `app/live.py`.
  Lower it to watch matches finish faster while testing.
- **Caching:** add Redis for live state once you have real traffic; the diff +
  broadcast pattern is already here.

Not betting or financial advice. ITF-level detailed stats often don't exist —
design around that rather than promising them.
