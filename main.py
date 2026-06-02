"""
main.py
-------
Run:  uvicorn main:app --reload   (locally)
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
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import func

from db import SessionLocal
from live import LiveEngine
from models import LiveState, Match, Prediction, StatSnapshot
from predictions import PredictionEngine
from ws import manager

PROVIDER_NAME = os.environ.get("TENNIS_PROVIDER", "mock").lower()
USE_REAL = PROVIDER_NAME == "apitennis"

# Optional LLM for nicer write-ups. Leave None to use the deterministic template.
LLM_COMPLETE = None

if USE_REAL:
    from apitennis import APITennisProvider
    from seed import build_day
    provider = APITennisProvider()
    engine = PredictionEngine()
    try:
        years = int(os.environ.get("TRAIN_YEARS", "2"))
        this_year = dt.date.today().year
        n = engine.train_from_sackmann(range(this_year - years + 1, this_year + 1))
        print(f"[predictions] trained on {n} matches, {len(engine._by_key)} players")
    except Exception as e:
        print(f"[predictions] history training skipped ({e})")
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


def _ensure_day(day: dt.date) -> None:
    if not USE_REAL:
        return
    key = day.isoformat()
    if key in _built_dates:
        return
    try:
        build_day(provider, engine, dt.datetime(day.year, day.month, day.day))
        _built_dates.add(key)
    except Exception as e:
        print(f"[build] could not build {key}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if USE_REAL:
        _ensure_day(dt.date.today())
    else:
        build_today(provider)
    task = asyncio.create_task(live_engine.run())
    yield
    live_engine.running = False
    task.cancel()


app = FastAPI(title="Tennis Predictions", lifespan=lifespan)


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


@app.get("/api/matches")
def list_matches(date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_day(target)
    with SessionLocal() as db:
        rows = (db.query(Match)
                  .filter(Match.scheduled >= dt.datetime.combine(target, dt.time.min),
                          Match.scheduled <= dt.datetime.combine(target, dt.time.max))
                  .order_by(Match.scheduled).all())
        return [_match_row(db, m) for m in rows]


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

        ctx = MatchContext(
            player_a=m.player_a, player_b=m.player_b, tier=m.tier, surface=m.surface,
            prob_a=prob_a, confidence=confidence, form_a=fa, form_b=fb,
            h2h=h2h, recent_a=ra_list, recent_b=rb_list,
        )
        writeup = generate_writeup(ctx, LLM_COMPLETE)

        return {
            "id": m.id, "tier": m.tier, "tournament": m.tournament, "round": m.round,
            "surface": m.surface, "player_a": m.player_a, "player_b": m.player_b,
            "event_time": m.event_time, "status": m.status,
            "prediction": {"prob_a": prob_a, "confidence": confidence},
            "analysis": writeup,
            "h2h": h2h, "form_a": fa, "form_b": fb,
            "recent_a": ra_list, "recent_b": rb_list,
            "score": None if not live else {
                "sets_a": _sets_list(live.sets_a), "sets_b": _sets_list(live.sets_b),
                "game_a": live.game_a, "game_b": live.game_b,
                "server": live.server, "status": live.status, "winner": live.winner,
            },
            "stats": stats,
        }


@app.get("/api/accuracy")
def accuracy(days: int = 30):
    """Rolling accuracy over finished matches in the last N days (from the DB)."""
    since = dt.datetime.now() - dt.timedelta(days=days)
    picks = correct = 0
    with SessionLocal() as db:
        rows = (db.query(Match, Prediction, LiveState)
                  .join(Prediction, Prediction.match_id == Match.id)
                  .join(LiveState, LiveState.match_id == Match.id)
                  .filter(Match.scheduled >= since,
                          LiveState.status == "finished")
                  .all())
        for m, pred, live in rows:
            if live.winner not in ("a", "b"):
                continue
            predicted = "a" if pred.prob_a >= 0.5 else "b"
            picks += 1
            if predicted == live.winner:
                correct += 1
    pct = round(100 * correct / picks) if picks else None
    return {"days": days, "picks": picks, "correct": correct, "accuracy": pct}


@app.get("/api/mlb/games")
def mlb_games(date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from mlb_provider import get_games
        return get_games(target)
    except Exception as e:
        print(f"[mlb] games failed: {e}")
        return []


def _mlb_writeup(g):
    fav_home = g["prob_home"] >= 0.5
    fav = g["home"] if fav_home else g["away"]
    dog = g["away"] if fav_home else g["home"]
    favp = round((g["prob_home"] if fav_home else 1 - g["prob_home"]) * 100)
    s = [f"The model favors {fav['name']} at {favp}% to win at home."
         if fav_home else
         f"The model favors {fav['name']} at {favp}% on the road."]
    fs, ds = fav["starter"], dog["starter"]
    if fs.get("era") is not None and ds.get("era") is not None:
        s.append(f"On the mound: {fs['name']} ({fs['era']:.2f} ERA) versus "
                 f"{ds['name']} ({ds['era']:.2f} ERA).")
    elif fs.get("name") or ds.get("name"):
        s.append(f"Probables: {fs.get('name','TBD')} vs {ds.get('name','TBD')}.")
    if g.get("park_factor") and abs(g["park_factor"] - 1.0) >= 0.04:
        env = "hitter-friendly" if g["park_factor"] > 1 else "pitcher-friendly"
        s.append(f"{g.get('venue','The park')} plays {env} (park factor {g['park_factor']:.2f}).")
    if g.get("weather"):
        s.append(f"Conditions: {g['weather']}.")
    s.append(f"Projected runs: {g['exp_runs_home']} (home) to {g['exp_runs_away']} (away). "
             f"Confidence is {g['confidence']}.")
    return " ".join(s)


@app.get("/api/mlb/game/{game_id}")
def mlb_game(game_id: int, date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from mlb_provider import get_game
        g = get_game(target, game_id)
    except Exception as e:
        print(f"[mlb] game failed: {e}")
        g = None
    if not g:
        return {"error": "not found"}
    g = dict(g)
    g["analysis"] = _mlb_writeup(g)
    return g


@app.get("/api/mlb/matchups/{game_id}")
def mlb_matchups(game_id: int, date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from mlb_provider import get_matchups
        return get_matchups(target, game_id)
    except Exception as e:
        print(f"[mlb] matchups failed: {e}")
        return {"error": "unavailable"}


@app.get("/api/mlb/injuries/{game_id}")
def mlb_injuries(game_id: int, date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from mlb_provider import get_game_injuries
        return get_game_injuries(target, game_id)
    except Exception as e:
        print(f"[mlb] injuries failed: {e}")
        return {"error": "unavailable"}


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(ws)


@app.get("/")
def index():
    return FileResponse("index.html")
