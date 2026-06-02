"""
main.py
-------
The web server. Run it with:

    uvicorn app.main:app --reload

Then open http://127.0.0.1:8000.

DATA FEED SELECTION (via environment variables):
  TENNIS_PROVIDER=apitennis   + TENNIS_API_KEY=<your key>   -> REAL matches
  (anything else)                                           -> simulated demo

For real predictions, the model trains on Jeff Sackmann's free history at
startup (TRAIN_YEARS controls how many recent years; needs internet on the
host). If training is unavailable, matches/scores are still real and the
prediction falls back to 50/50 (flagged as low-confidence).

REST endpoints:
    GET /api/matches?date=YYYY-MM-DD   -> that day's matches + predictions + score
    GET /api/matches/{id}              -> one match incl. latest stats snapshot
WebSocket:
    /ws/live                           -> pushed live score updates
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .db import SessionLocal
from .live import LiveEngine
from .models import LiveState, Match, MatchAnalysis, Prediction, StatSnapshot
from .predictions import PredictionEngine
from .ws import manager

# ---- choose the data feed from the environment --------------------------
PROVIDER_NAME = os.environ.get("TENNIS_PROVIDER", "mock").lower()
USE_REAL = PROVIDER_NAME == "apitennis"

if USE_REAL:
    from .providers.apitennis import APITennisProvider
    from .seed import build_day
    provider = APITennisProvider()          # reads TENNIS_API_KEY from env
    engine = PredictionEngine()
    try:
        years = int(os.environ.get("TRAIN_YEARS", "2"))
        this_year = dt.date.today().year
        engine.train_from_sackmann(range(this_year - years + 1, this_year + 1))
        print(f"[predictions] trained on {len(engine._by_key)} players")
    except Exception as e:
        print(f"[predictions] training skipped ({e}); predictions default to 50/50")
else:
    from .providers.mock import MockTennisProvider
    from .seed import build_today
    provider = MockTennisProvider()
    engine = None
# -------------------------------------------------------------------------

live_engine = LiveEngine(provider)
_built_dates: set[str] = set()


def _ensure_day(day: dt.date) -> None:
    """For the real feed, build a day's fixtures on demand (idempotent)."""
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


def _sets_list(csv: str) -> list[int]:
    return [int(x) for x in csv.split(",")] if csv else []


@app.get("/api/matches")
def list_matches(date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_day(target)
    with SessionLocal() as db:
        rows = (db.query(Match)
                  .filter(Match.scheduled >= dt.datetime.combine(target, dt.time.min),
                          Match.scheduled <= dt.datetime.combine(target, dt.time.max))
                  .order_by(Match.scheduled).all())
        out = []
        for m in rows:
            pred = db.query(Prediction).filter_by(match_id=m.id).one_or_none()
            live = db.query(LiveState).filter_by(match_id=m.id).one_or_none()
            predicted = None
            correct = None
            if pred is not None:
                predicted = "a" if pred.prob_a >= 0.5 else "b"
                if live and live.status == "finished" and live.winner in ("a", "b"):
                    correct = (predicted == live.winner)
            out.append({
                "id": m.id, "tier": m.tier, "tournament": m.tournament,
                "surface": m.surface, "player_a": m.player_a, "player_b": m.player_b,
                "scheduled": m.scheduled.isoformat(), "status": m.status,
                "predicted_winner": predicted, "correct": correct,
                "prediction": None if not pred else {
                    "prob_a": pred.prob_a, "confident": pred.confident,
                    "fair_prob_a": pred.fair_prob_a, "edge_a": pred.edge_a,
                },
                "score": None if not live else {
                    "sets_a": _sets_list(live.sets_a), "sets_b": _sets_list(live.sets_b),
                    "game_a": live.game_a, "game_b": live.game_b,
                    "server": live.server, "status": live.status, "winner": live.winner,
                },
            })
        return out


@app.get("/api/matches/{match_id}")
def match_detail(match_id: int):
    with SessionLocal() as db:
        m = db.get(Match, match_id)
        if not m:
            return {"error": "not found"}
        snap = (db.query(StatSnapshot).filter_by(match_id=match_id)
                  .order_by(StatSnapshot.captured_at.desc()).first())
        stats = json.loads(snap.payload) if snap and snap.payload else None
        return {"id": m.id, "player_a": m.player_a, "player_b": m.player_b,
                "tier": m.tier, "stats": stats, "stats_available": stats is not None}


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
    return FileResponse("app/static/index.html")


app.mount("/static", StaticFiles(directory="app/static"), name="static")
