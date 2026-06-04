"""
main.py
-------
Run:   uvicorn main:app --reload   (locally)
On Render the start command is: uvicorn main:app --host 0.0.0.0 --port $PORT

DATA FEED (env vars):
  TENNIS_PROVIDER=apitennis + TENNIS_API_KEY=<key>  -> real matches
  (anything else)                                   -> simulated demo

Endpoints:
  GET /api/matches?date=YYYY-MM-DD   -> day's matches (+ prediction, score, prominence)
  GET /api/tournaments?date=...      -> tournaments that day (for the sidebar)
  GET /api/match/{id}                -> detail: analysis, H2H, form, live stats
  WS  /ws/live                       -> pushed live score updates
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time as _t
import threading as _thr
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func

from db import SessionLocal
from live import LiveEngine
from models import LiveState, Match, Prediction, StatSnapshot
from predictions import PredictionEngine
from ws import manager

PROVIDER_NAME = os.environ.get("TENNIS_PROVIDER", "mock").lower()
USE_REAL = PROVIDER_NAME == "apitennis"

# Optional AI narrative for write-ups. If ANTHROPIC_API_KEY is set in the
# environment, we use Claude to turn the computed FACTS into richer prose
# (under a strict "use only these facts" instruction). With no key, the
# deterministic template is used — same facts, plainer wording, no cost.
def _make_llm_complete():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    model = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
    except Exception as e:
        print(f"[ai] anthropic unavailable, using template ({e})")
        return None

    def complete(prompt: str) -> str:
        msg = client.messages.create(
            model=model, max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        return "\n".join(parts).strip()
    print(f"[ai] AI narrative enabled with {model}")
    return complete

LLM_COMPLETE = _make_llm_complete()

if USE_REAL:
    from apitennis import APITennisProvider
    from seed import build_day
    provider = APITennisProvider()
    engine = PredictionEngine()
    # Prefer the precomputed ratings file (low memory, no pandas at runtime).
    loaded = engine.load_ratings(os.environ.get("RATINGS_FILE", "ratings.json"))
    if loaded:
        print(f"[predictions] loaded {loaded} precomputed ratings (low-memory mode)")
    elif os.environ.get("TRAIN_AT_RUNTIME", "").lower() in ("1", "true", "yes"):
        # Heavy fallback: train live from CSVs. Only if explicitly enabled, since
        # it loads pandas and can exceed small instances' memory.
        try:
            years = int(os.environ.get("TRAIN_YEARS", "2"))
            this_year = dt.date.today().year
            n = engine.train_from_sackmann(range(this_year - years + 1, this_year + 1))
            print(f"[predictions] trained on {n} matches, {len(engine._by_key)} players")
        except Exception as e:
            print(f"[predictions] history training skipped ({e})")
    else:
        print("[predictions] no ratings.json found; running lean (ranking-only). "
              "Generate ratings.json via build_ratings.py for full strength.")
    try:
        ranks = provider.get_rankings()
        engine.load_rankings(ranks)
        print(f"[predictions] loaded {len(ranks)} ranked players as fallback")
    except Exception as e:
        print(f"[predictions] ranking fallback skipped ({e})")
else:
    from mock import MockTennisProvider
    from seed import build_today
    provider = MockTennisProvider()
    engine = None

live_engine = LiveEngine(provider)
_built_dates: set[str] = set()
_build_attempts = {}   # key -> last attempt timestamp (throttle re-tries)

def _ensure_day(day: dt.date) -> None:
    if not USE_REAL:
        return
    key = day.isoformat()
    if key in _built_dates:
        return
    # Throttle: even if a build doesn't fully succeed, don't re-attempt more
    # than once every 60s. This stops the endless slow rebuild-on-every-click.
    last = _build_attempts.get(key, 0)
    if _t.time() - last < 60:
        return
    _build_attempts[key] = _t.time()
    try:
        build_day(provider, engine, dt.datetime(day.year, day.month, day.day))
        # Mark built once the day has matches stored, so future requests are instant.
        with SessionLocal() as db:
            start = dt.datetime.combine(day, dt.time.min)
            end = dt.datetime.combine(day, dt.time.max)
            has = db.query(Match.id).filter(Match.scheduled >= start,
                                            Match.scheduled <= end).first()
        if has:
            _built_dates.add(key)
    except Exception as e:
        print(f"[build] could not build {key}: {e}")


def _backfill_recent(days: int) -> None:
    """Build the past `days` days so 30-day accuracy has data. Throttled and
    fully guarded so it can never take the app down. Opt-in via BACKFILL_DAYS."""
    if not USE_REAL or days <= 0:
        return
    today = dt.date.today()
    for off in range(1, days + 1):
        try:
            _ensure_day(today - dt.timedelta(days=off))
            _t.sleep(1.0)   # breathe between days so a tiny instance isn't pegged
        except Exception as e:
            print(f"[backfill] skipped a day: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # CRITICAL: never block startup. The server must become ready immediately so
    # the page always loads; all data-building/warming happens in the background.
    # First, create DB tables right away (fast, no network) so nothing errors with
    # "no such table" on a fresh instance (e.g. a new Railway deploy).
    try:
        from db import init_db
        init_db()
    except Exception as e:
        print(f"[startup] init_db failed: {e}")
    # When running MULTIPLE workers (uvicorn --workers N), only ONE of them should
    # run the background jobs (live engine, slate builder, pre-warm) — otherwise
    # every worker duplicates them. Set RUN_BACKGROUND=0 on extra workers, or rely
    # on the default below (single-process = background on).
    run_bg = os.environ.get("RUN_BACKGROUND", "1") == "1"
    if USE_REAL and run_bg:
        def _startup_bg():
            _t.sleep(5)
            try:
                _ensure_day(dt.date.today())
            except Exception as e:
                print(f"[startup] ensure_day failed: {e}")
            try:
                import warrennolan
                warrennolan.warm()
            except Exception as e:
                print(f"[startup] warrennolan warm failed: {e}")
            bf = int(os.environ.get("BACKFILL_DAYS", "0") or 0)
            if bf > 0:
                try:
                    _backfill_recent(bf)
                except Exception as e:
                    print(f"[startup] backfill failed: {e}")
        _thr.Thread(target=_startup_bg, daemon=True).start()
        _thr.Thread(target=_prewarm_all, daemon=True).start()
    elif USE_REAL is False:
        build_today(provider)
    task = asyncio.create_task(live_engine.run()) if run_bg else None
    yield
    live_engine.running = False
    if task:
        task.cancel()


def _prewarm_all():
    """Keep the current slate warm so navigation is fast. OFF by default because
    on a single CPU the background fetching competes with serving pages. Enable
    with PREWARM=1 once on a 2+ CPU instance. (Added during the NCAA work; left
    running it was overloading the one core, causing the whole site to hang.)"""
    if os.environ.get("PREWARM", "0") != "1":
        print("[prewarm] disabled (set PREWARM=1 to enable, recommended only on 2+ CPU)")
        return
    _t.sleep(45)
    first = True
    while True:
        try:
            today = dt.date.today()
            for off in range(0, 8):
                d = today + dt.timedelta(days=off)
                try:
                    # Best-effort call to college baseball dependencies if loaded
                    from mlb_provider import get_games
                    get_games(d)
                except Exception as e:
                    print(f"[prewarm] sports sync failure for {d}: {e}")
                _t.sleep(3.0)
            if first:
                print("[prewarm] initial slate cached"); first = False
        except Exception as e:
            print(f"[prewarm] loop error: {e}")
        _t.sleep(1800)


def _prewarm_ncaabb():
    """Kept for compatibility; the unified _prewarm_all covers college baseball."""
    _prewarm_all()


app = FastAPI(title="Tennis Predictions", lifespan=lifespan)


@app.middleware("http")
async def _no_cache_api(request, call_next):
    """Prevent edge/CDN/browser caching of API responses. A stale cached empty
    response on /api/ncaabb/games was masking live code for hours; this ensures
    every API call reflects current server state."""
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


def _sets_list(csv: str):
    return [int(x) for x in csv.split(",")] if csv else []


def _match_row(db, m):
    pred = db.query(Prediction).filter_by(match_id=m.id).one_or_none()
    live = db.query(LiveState).filter_by(match_id=m.id).one_or_none()
    predicted = correct = None
    confidence = "high"
    if pred is not None:
        predicted = "a" if pred.prob_a >= 0.5 else "b"
        confidence = getattr(pred, "confidence", "high")
        if live and live.status == "finished" and live.winner in ("a", "b"):
            correct = (predicted == live.winner)
    return {
        "id": m.id, "tier": m.tier, "tournament": m.tournament,
        "tournament_key": m.tournament_key, "round": m.round,
        "surface": m.surface, "player_a": m.player_a, "player_b": m.player_b,
        "scheduled": m.scheduled.isoformat(), "event_time": m.event_time,
        "status": m.status, "prominence": m.prominence or 0,
        "weather": getattr(m, "weather", None),
        "predicted_winner": predicted, "correct": correct,
        "prediction": None if not pred else {
            "prob_a": pred.prob_a, "confidence": confidence,
        },
        "score": None if not live else {
            "sets_a": _sets_list(live.sets_a), "sets_b": _sets_list(live.sets_b),
            "game_a": live.game_a, "game_b": live.game_b,
            "server": live.server, "status": live.status, "winner": live.winner,
        },
    }


@app.get("/")
def health_check():
    return {"status": "healthy", "service": "tennis-backend"}


@app.get("/api/matches")
def list_matches(date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_day(target)
    with SessionLocal() as db:
        rows = (db.query(Match)
                  .filter(Match.scheduled >= dt.datetime.combine(target, dt.time.min),
                          Match.scheduled <= dt.datetime.combine(target, dt.time.max))
                  .order_by(Match.scheduled).all())
        result = [_match_row(db, m) for m in rows]
        # log settled tennis picks for the accuracy tracker (best-effort)
        try:
            wrote = False
            for r in result:
                sc = r.get("score") or {}
                if r["status"] == "finished" and r.get("predicted_winner") and sc.get("winner") in ("a", "b"):
                    _record_result(db, "tennis", r["id"], r["predicted_winner"], sc["winner"])
                    wrote = True
            if wrote:
                db.commit()
        except Exception as e:
            db.rollback()
            print(f"[accuracy] tennis log skipped: {e}")
        return result


@app.get("/api/tournaments")
def list_tournaments(date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_day(target)
    with SessionLocal() as db:
        rows = (db.query(Match)
                  .filter(Match.scheduled >= dt.datetime.combine(target, dt.time.min),
                          Match.scheduled <= dt.datetime.combine(target, dt.time.max))
                  .all())
        groups = {}
        for m in rows:
            k = m.tournament_key or m.tournament
            g = groups.setdefault(k, {"key": k, "name": m.tournament, "tier": m.tier,
                                      "count": 0, "live": 0})
            g["count"] += 1
            if m.status == "live":
                g["live"] += 1
        # sort: live first, then by size
        return sorted(groups.values(), key=lambda g: (-g["live"], -g["count"], g["name"]))


def _parse_h2h(raw, name_a, name_b):
    """Best-effort parse of get_H2H into (h2h_string, form_a, form_b, recent_a, recent_b)."""
    if not raw:
        return None, None, None, [], []
    h2h_list = raw.get("H2H") or raw.get("firstPlayer_VS_secondPlayer") or []
    a_wins = b_wins = 0
    for g in h2h_list:
        w = g.get("event_winner")
        fp = g.get("event_first_player", "")
        if w == "First Player":
            winner_name = fp
        elif w == "Second Player":
            winner_name = g.get("event_second_player", "")
        else:
            continue
        if name_a.split()[-1].lower() in (winner_name or "").lower():
            a_wins += 1
        elif name_b.split()[-1].lower() in (winner_name or "").lower():
            b_wins += 1
    h2h_str = None
    if a_wins or b_wins:
        if a_wins > b_wins:
            h2h_str = f"{name_a} leads {a_wins}-{b_wins}"
        elif b_wins > a_wins:
            h2h_str = f"{name_b} leads {b_wins}-{a_wins}"
        else:
            h2h_str = f"tied {a_wins}-{b_wins}"

    def form_of(results, who_last):
        if not results:
            return None, []
        wins = 0
        recent = []
        for g in results[:10]:
            w = g.get("event_winner")
            fp, sp = g.get("event_first_player", ""), g.get("event_second_player", "")
            won = (w == "First Player" and who_last in fp.lower()) or \
                  (w == "Second Player" and who_last in sp.lower())
            opp = sp if who_last in fp.lower() else fp
            recent.append(("W" if won else "L") + f" vs {opp.split()[-1] if opp else '?'}")
            if won:
                wins += 1
        n = min(10, len(results))
        return (f"Won {wins} of last {n}" if n else None), recent

    fa, ra = form_of(raw.get("firstPlayerResults"), name_a.split()[-1].lower())
    fb, rb = form_of(raw.get("secondPlayerResults"), name_b.split()[-1].lower())
    return h2h_str, fa, fb, ra, rb


@app.get("/api/match/{match_id}")
def match_detail(match_id: int):
    from analysis import MatchContext, generate_writeup
    from tennis_stats import compute_stats
    with SessionLocal() as db:
        m = db.get(Match, match_id)
        if not m:
            return {"error": "not found"}
        pred = db.query(Prediction).filter_by(match_id=m.id).one_or_none()
        live = db.query(LiveState).filter_by(match_id=m.id).one_or_none()
        prob_a = pred.prob_a if pred else 0.5
        confidence = getattr(pred, "confidence", "high") if pred else "low"

        h2h = fa = fb = None
        ra_list = rb_list = []
        stats = None

        if USE_REAL and hasattr(provider, "raw_fixture"):
            try:
                raw_h2h = provider.get_h2h(m.player_a_key, m.player_b_key)
                h2h, fa, fb, ra_list, rb_list = _parse_h2h(raw_h2h, m.player_a, m.player_b)
            except Exception as e:
                print(f"[detail] h2h failed: {e}")
            try:
                fix = provider.raw_fixture(m.provider_match_id)
                stats = compute_stats(fix.get("pointbypoint"))
            except Exception as e:
                print(f"[detail] stats failed: {e}")

        # data-backed facts for the deeper writeup
        facts = {}
        if USE_REAL and engine is not None:
            try:
                facts = engine.analysis_facts(m.player_a, m.player_b, m.surface)
            except Exception as e:
                print(f"[detail] facts failed: {e}")

        ctx = MatchContext(
            player_a=m.player_a, player_b=m.player_b, tier=m.tier, surface=m.surface,
            prob_a=prob_a, confidence=confidence, form_a=fa, form_b=fb,
            h2h=h2h, recent_a=ra_list, recent_b=rb_list,
            facts=facts, weather=m.weather, weather_effect=m.weather_effect,
        )
        writeup = generate_writeup(ctx, LLM_COMPLETE)

        lines = None
        props = None
