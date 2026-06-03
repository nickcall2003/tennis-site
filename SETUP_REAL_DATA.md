# Going Live with Real Matches (API-Tennis)

This turns the site from simulated demo data into **real ATP / WTA / Challenger
matches** with live scores and predictions. ITF is intentionally excluded.

## Before you start — your API key is a secret

Your key shouldn't appear in any code or get committed to GitHub. It goes in an
**environment variable** only. If a key has ever been pasted into a chat or
shared, regenerate it from the API-Tennis admin page once everything works.

---

## 1. Run it locally first (to confirm it works)

From the `tennis-site` folder:

```bash
pip install -r requirements.txt

# tell the app to use the real feed + your key (Mac/Linux)
export TENNIS_PROVIDER=apitennis
export TENNIS_API_KEY=PASTE_YOUR_KEY_HERE
export TENNIS_TZ=America/Chicago      # your timezone (optional)

uvicorn main:app --reload
```

On Windows PowerShell, use `$env:TENNIS_PROVIDER="apitennis"` etc. instead of `export`.

Open http://127.0.0.1:8000 — you should see today's real matches. On first
start it downloads ~2 years of free match history to train the prediction
model (a few seconds). If that download is blocked, matches and scores are
still real; predictions just show 50/50 and a "low-confidence" tag until the
model trains.

## 2. Deploy it (so it has a public address)

Host the **backend** (this Python app), not just a static file — the backend is
what holds your key safely and calls the feed on a schedule. Render and Railway
both work and have free/cheap tiers.

On the host's dashboard, set these environment variables (NOT in code):

| Variable          | Value                         |
|-------------------|-------------------------------|
| `TENNIS_PROVIDER` | `apitennis`                   |
| `TENNIS_API_KEY`  | your key                      |
| `TENNIS_TZ`       | `America/Chicago` (optional)  |
| `TRAIN_YEARS`     | `2` (optional)                |

Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

For more than a little traffic, also set `DATABASE_URL` to a Postgres database
(the models are already Postgres-ready) so data survives restarts.

## 3. How it behaves

- **Daily, automatic:** open any date in the bar; the app fetches that day's
  fixtures from the feed, predicts each match, and stores it.
- **Live:** a background poller refreshes in-play scores every few seconds and
  pushes them to open browsers — no page refresh.
- **Grading:** when a match finishes, the actual winner comes from the feed and
  each pick gets a ✓ or ✗; the day's accuracy bubbles update automatically.

## What's real vs. still approximate

- **Real:** schedules, players, live scores, final results, ✓/✗ grading.
- **Approximate for now:** predictions use an Elo model trained on free
  historical data, matched to feed names by last-name + initial; unmatched
  players fall back to 50/50 (flagged). Surface isn't in the feed's fixture
  data, so predictions currently use overall (not surface-specific) ratings.
  Both are straightforward to improve later.

## Rate limits

The poller + once-per-day fixture pulls are designed to stay within free-tier
limits because only the *server* calls the feed — your visitors read from the
database. If you expand to many tiers or very frequent polling, check your
plan's limits and add Postgres + caching.

Not betting or financial advice.

---

## Betting metrics: Odds, CLV, ROI, Units (The Odds API)

The performance metrics (units won/lost, ROI, CLV, and market odds on each pick)
need a real sportsbook-odds source. We use **The Odds API** (the-odds-api.com).

### Get a key (free tier)
1. Go to the-odds-api.com and sign up for the free plan (about 500 requests/month).
2. Copy your API key.

### Add it to Render
1. Render dashboard -> your service -> Environment.
2. Add a variable: key = `ODDS_API_KEY`, value = your key.
3. Save. The service redeploys and odds/CLV/ROI/units turn on automatically.

### What works on the free tier (honest scope)
- **Live/upcoming odds** for MLB, NBA, NFL moneylines, spreads, totals — shown on
  picks and used to record the line we "took".
- **Units & ROI**: computed from settled picks at flat 1-unit stakes.
- **CLV**: we capture the opening line when a pick first appears and the latest
  line near game time as a CLOSING PROXY. True official closing lines require
  The Odds API's *historical* endpoint, which is PAID. Until then, CLV is labeled
  as measured against our best near-close line, not a verified official close.
- Quota is protected by 15-minute caching. Each refresh pulls one combined
  request per league.

### Without a key
Everything still runs. Picks show the model's own **fair odds** (derived from its
probability), and the performance strip says metrics activate once odds are
connected. Win/loss and accuracy tracking work regardless.
