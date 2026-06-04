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
    import time as _t
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
    import time as _t
    today = dt.date.today()
    for off in range(1, days + 1):
        try:
            _ensure_day(today - dt.timedelta(days=off))
            _t.sleep(1.0)   # breathe between days so a tiny instance isn't pegged
        except Exception as e:
            print(f"[backfill] skipped a day: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if USE_REAL:
        _ensure_day(dt.date.today())
        # Past-days backfill is OFF by default to keep small instances stable.
        # Set BACKFILL_DAYS=30 (and ideally upgrade RAM) to enable rolling accuracy.
        bf = int(os.environ.get("BACKFILL_DAYS", "0") or 0)
        if bf > 0:
            asyncio.get_event_loop().run_in_executor(None, _backfill_recent, bf)
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

        form_form_a = form_form_b = None
        ctx = MatchContext(
            player_a=m.player_a, player_b=m.player_b, tier=m.tier, surface=m.surface,
            prob_a=prob_a, confidence=confidence, form_a=fa, form_b=fb,
            h2h=h2h, recent_a=ra_list, recent_b=rb_list,
            facts=facts, weather=m.weather, weather_effect=m.weather_effect,
        )
        writeup = generate_writeup(ctx, LLM_COMPLETE)

        lines = None
        props = None
        try:
            from betting import tennis_lines, tennis_props
            bo = 5 if (m.best_of == 5) else 3
            lines = tennis_lines(prob_a, bo)
            props = tennis_props(prob_a, bo)
        except Exception as e:
            print(f"[detail] lines failed: {e}")

        return {
            "id": m.id, "tier": m.tier, "tournament": m.tournament, "round": m.round,
            "surface": m.surface, "player_a": m.player_a, "player_b": m.player_b,
            "event_time": m.event_time, "status": m.status,
            "prediction": {"prob_a": prob_a, "confidence": confidence},
            "analysis": writeup,
            "h2h": h2h, "form_a": fa, "form_b": fb,
            "recent_a": ra_list, "recent_b": rb_list,
            "weather": m.weather, "weather_effect": m.weather_effect,
            "lines": lines, "props": props,
            "score": None if not live else {
                "sets_a": _sets_list(live.sets_a), "sets_b": _sets_list(live.sets_b),
                "game_a": live.game_a, "game_b": live.game_b,
                "server": live.server, "status": live.status, "winner": live.winner,
            },
            "stats": stats,
        }


_recorded_refs = set()   # in-memory guard: skip results we've already logged this run


def _attach_odds(sport, games):
    """Attach real market odds to each game and snapshot the pick's line.
    No-op (returns games unchanged) when no odds key is configured."""
    try:
        import odds_api
        if not odds_api.enabled():
            return games
        book = odds_api.get_odds(sport)
        if not book:
            return games
        from odds_api import _norm
        from clv import american_to_prob
        for g in games:
            hk = _norm(g["home"]["name"]) + "|" + _norm(g["away"]["name"])
            o = book.get(hk)
            if not o:
                continue
            g["odds"] = {"ml_home": o["ml_home"], "ml_away": o["ml_away"],
                         "spread_home": o["spread_home"], "total": o["total"],
                         "books": o["books"]}
            # snapshot the line on the side we pick
            side = "home" if g["prob_home"] >= 0.5 else "away"
            taken = o["ml_home"] if side == "home" else o["ml_away"]
            if taken is not None:
                _snapshot_odds(sport, str(g["id"]), side, int(round(taken)))
    except Exception as e:
        print(f"[odds] attach {sport} skipped: {e}")
    return games


def _snapshot_odds(sport, ref, side, odds):
    """Record/refresh the market line for a pick (open = first seen, last = now)."""
    from models import OddsSnapshot
    now = dt.datetime.utcnow()
    try:
        with SessionLocal() as db:
            row = db.query(OddsSnapshot).filter_by(sport=sport, ref=ref).first()
            if row is None:
                db.add(OddsSnapshot(sport=sport, ref=ref, side=side,
                                    open_odds=odds, last_odds=odds,
                                    first_seen=now, last_seen=now))
            else:
                row.last_odds = odds
                row.last_seen = now
                row.side = side
            db.commit()
    except Exception:
        pass


def _record_result(db, sport, ref, predicted, actual):
    """Upsert a settled pick into the results log (no-op if already recorded).
    If we captured a market line for this pick, store taken (open) and close
    (last-seen) odds so the performance metrics can compute units/ROI/CLV."""
    from models import PickResult, OddsSnapshot
    ref = str(ref)
    memo = (sport, ref)
    if memo in _recorded_refs:
        return
    exists = db.query(PickResult).filter_by(sport=sport, ref=ref).first()
    if exists:
        _recorded_refs.add(memo)
        return
    taken = close = None
    try:
        snap = db.query(OddsSnapshot).filter_by(sport=sport, ref=ref).first()
        if snap:
            taken = snap.open_odds
            close = snap.last_odds
    except Exception:
        pass
    db.add(PickResult(sport=sport, ref=ref, settled_date=dt.datetime.now(),
                      predicted=str(predicted), actual=str(actual),
                      correct=(str(predicted) == str(actual)),
                      taken_odds=taken, close_odds=close))
    _recorded_refs.add(memo)


_acc_cache = {"ts": 0.0, "data": None}


@app.get("/api/accuracy")
def accuracy(days: int = 30):
    """Per-sport rolling accuracy from the settled-results log (cached 2 min)."""
    import time as _t
    from models import PickResult
    if _acc_cache["data"] and _t.time() - _acc_cache["ts"] < 120 and _acc_cache["data"]["days"] == days:
        return _acc_cache["data"]
    since = dt.datetime.now() - dt.timedelta(days=days)
    by_sport = {}
    tot_p = tot_c = 0
    alltime = {}
    at_p = at_c = 0
    with SessionLocal() as db:
        rows = db.query(PickResult).filter(PickResult.settled_date >= since).all()
        for r in rows:
            s = by_sport.setdefault(r.sport, {"picks": 0, "correct": 0})
            s["picks"] += 1
            tot_p += 1
            if r.correct:
                s["correct"] += 1
                tot_c += 1
        # all-time record (no date filter), per sport and overall
        allrows = db.query(PickResult).all()
        for r in allrows:
            a = alltime.setdefault(r.sport, {"wins": 0, "losses": 0})
            at_p += 1
            if r.correct:
                a["wins"] += 1
                at_c += 1
            else:
                a["losses"] += 1
    for s, v in by_sport.items():
        v["accuracy"] = round(100 * v["correct"] / v["picks"]) if v["picks"] else None
        at = alltime.get(s, {"wins": 0, "losses": 0})
        v["alltime_wins"] = at["wins"]
        v["alltime_losses"] = at["losses"]
        tot = at["wins"] + at["losses"]
        v["alltime_pct"] = round(100 * at["wins"] / tot) if tot else None
    data = {
        "days": days,
        "overall": {"picks": tot_p, "correct": tot_c,
                    "accuracy": round(100 * tot_c / tot_p) if tot_p else None,
                    "alltime_wins": at_c, "alltime_losses": at_p - at_c,
                    "alltime_pct": round(100 * at_c / at_p) if at_p else None},
        "by_sport": by_sport,
    }
    _acc_cache["ts"] = _t.time()
    _acc_cache["data"] = data
    return data


@app.get("/api/picks/record")
def picks_record(view: str = "free", date: str | None = None):
    """
    W/L for a specific view (free|best): today's settled picks AND a rolling
    30-day figure, counting only games that view actually surfaced.
    """
    from models import PickResult, PickLog
    target = dt.date.fromisoformat(date) if date else dt.date.today()

    def tally(since_dt, until_dt):
        wins = losses = 0
        items = []
        with SessionLocal() as db:
            logged = db.query(PickLog).filter(PickLog.view == view,
                                              PickLog.shown_date >= since_dt,
                                              PickLog.shown_date <= until_dt).all()
            keys = {(l.sport, l.ref) for l in logged}
            if not keys:
                return 0, 0, []
            results = {(r.sport, r.ref): r for r in
                       db.query(PickResult).filter(PickResult.settled_date >= since_dt,
                                                   PickResult.settled_date <= until_dt + dt.timedelta(days=2)).all()}
            for k in keys:
                r = results.get(k)
                if not r:
                    continue
                if r.correct:
                    wins += 1
                else:
                    losses += 1
                items.append({"sport": k[0], "ref": k[1], "won": bool(r.correct)})
        return wins, losses, items

    # today (the picks shown for `target`)
    d0 = dt.datetime.combine(target, dt.time.min)
    d1 = dt.datetime.combine(target, dt.time.max)
    tw, tl, items = tally(d0, d1)
    tt = tw + tl

    # rolling 30 days
    m0 = dt.datetime.combine(target - dt.timedelta(days=30), dt.time.min)
    mw, ml, _ = tally(m0, d1)
    mt = mw + ml

    return {
        "view": view, "date": target.isoformat(),
        "today": {"wins": tw, "losses": tl, "total": tt,
                  "hit_rate": round(100 * tw / tt) if tt else None, "items": items},
        "month": {"wins": mw, "losses": ml, "total": mt,
                  "hit_rate": round(100 * mw / mt) if mt else None},
    }


@app.get("/api/mlb/games")
def mlb_games(date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from mlb_provider import get_games
        games = get_games(target)
    except Exception as e:
        print(f"[mlb] games failed: {e}")
        return []
    games = _attach_odds("mlb", games)
    try:
        with SessionLocal() as db:
            wrote = False
            for g in games:
                if g.get("status") == "finished" and g.get("winner") in ("home", "away"):
                    predicted = "home" if g["prob_home"] >= 0.5 else "away"
                    _record_result(db, "mlb", g["id"], predicted, g["winner"])
                    wrote = True
            if wrote:
                db.commit()
    except Exception as e:
        print(f"[accuracy] mlb log skipped: {e}")
    return games


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


_MLB_AI_PROMPT = """You are a baseball analyst writing a short MLB game preview.
Write 2-3 tight paragraphs on why the model favors the pick. Use ONLY these facts; do not
invent any stat, injury, or result. Cover the projected runs, starting pitching matchup,
bullpen, park and weather. Natural prose, no markdown.

FACTS:
{facts}
"""


def _mlb_analysis(g):
    base = _mlb_writeup(g)
    if LLM_COMPLETE is None:
        return base
    import json as _j
    facts = {
        "favorite": g["home"]["name"] if g["prob_home"] >= 0.5 else g["away"]["name"],
        "home_team": g["home"]["name"], "away_team": g["away"]["name"],
        "home_win_pct": round(g["prob_home"] * 100), "away_win_pct": round((1 - g["prob_home"]) * 100),
        "proj_runs_home": g["exp_runs_home"], "proj_runs_away": g["exp_runs_away"],
        "home_starter": g["home"]["starter"], "away_starter": g["away"]["starter"],
        "home_bullpen_era": g["home"].get("bullpen_era"), "away_bullpen_era": g["away"].get("bullpen_era"),
        "park_factor": g.get("park_factor"), "venue": g.get("venue"), "weather": g.get("weather"),
        "confidence": g["confidence"],
    }
    try:
        text = LLM_COMPLETE(_MLB_AI_PROMPT.format(facts=_j.dumps(facts, indent=2))).strip()
        return text or base
    except Exception:
        return base


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
    g["analysis"] = _mlb_analysis(g)
    try:
        from betting import mlb_lines
        g["lines"] = mlb_lines(g["exp_runs_home"], g["exp_runs_away"])
    except Exception as e:
        print(f"[mlb] lines failed: {e}")
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


def _confidence_rank(conf):
    return {"high": 3, "medium": 2, "low": 1}.get(conf, 0)


_plays_cache = {}   # date -> (ts, plays)
_PLAYS_TTL = 120     # seconds; picks don't need to refetch every click


def _gather_plays(target: dt.date):
    """Collect candidate plays across sports for a day (cached to stay fast)."""
    import time as _t
    key = target.isoformat()
    c = _plays_cache.get(key)
    if c and _t.time() - c[0] < _PLAYS_TTL:
        return [dict(p) for p in c[1]]   # copy so callers can mutate freely
    plays = _gather_plays_uncached(target)
    _plays_cache[key] = (_t.time(), [dict(p) for p in plays])
    return plays


def _gather_plays_uncached(target: dt.date):
    plays = []
    # --- tennis (from DB) ---
    with SessionLocal() as db:
        rows = (db.query(Match, Prediction, LiveState)
                  .join(Prediction, Prediction.match_id == Match.id)
                  .outerjoin(LiveState, LiveState.match_id == Match.id)
                  .filter(Match.scheduled >= dt.datetime.combine(target, dt.time.min),
                          Match.scheduled <= dt.datetime.combine(target, dt.time.max))
                  .all())
        for m, pred, live in rows:
            if live and live.status == "finished":
                continue
            prob = max(pred.prob_a, 1 - pred.prob_a)
            pick = m.player_a if pred.prob_a >= 0.5 else m.player_b
            other = m.player_b if pred.prob_a >= 0.5 else m.player_a
            tctx = {"opponent": other, "round": m.round, "surface": m.surface,
                    "tournament": m.tournament, "weather": getattr(m, "weather", None),
                    "weather_effect": getattr(m, "weather_effect", None)}
            try:
                if engine is not None:
                    facts = engine.analysis_facts(m.player_a, m.player_b, m.surface or "Unknown")
                    tctx["rating_gap"] = facts.get("rating_gap")
                    tctx["edge_size"] = facts.get("edge_size")
                    tctx["surface_note"] = facts.get("surface_note")
            except Exception:
                pass
            plays.append({
                "sport": "tennis", "id": m.id, "kind": "moneyline",
                "match": f"{m.player_a} vs {m.player_b}", "tournament": m.tournament,
                "pick": f"{pick} to win", "prob": round(prob, 3),
                "confidence": getattr(pred, "confidence", "high"),
                "event_time": m.event_time, "surface": m.surface,
                "ctx": tctx,
                "score_key": prob + 0.05 * _confidence_rank(getattr(pred, "confidence", "high")),
            })
    # --- MLB (live from provider) ---
    if USE_REAL:
        try:
            from mlb_provider import get_games
            for g in get_games(target):
                if g["status"] == "finished":
                    continue
                prob = max(g["prob_home"], 1 - g["prob_home"])
                pick = g["home"]["name"] if g["prob_home"] >= 0.5 else g["away"]["name"]
                fav = g["home"] if g["prob_home"] >= 0.5 else g["away"]
                dog = g["away"] if g["prob_home"] >= 0.5 else g["home"]
                plays.append({
                    "sport": "mlb", "id": g["id"], "kind": "moneyline",
                    "match": f"{g['away']['name']} @ {g['home']['name']}",
                    "tournament": g.get("venue", ""),
                    "pick": f"{pick} to win", "prob": round(prob, 3),
                    "confidence": g["confidence"], "event_time": g.get("event_time"),
                    "ctx": {"exp_runs_fav": fav.get("exp_runs") if isinstance(fav, dict) else None,
                            "fav_starter": (fav.get("starter") or {}).get("name"),
                            "fav_era": (fav.get("starter") or {}).get("era"),
                            "exp_runs_home": g.get("exp_runs_home"), "exp_runs_away": g.get("exp_runs_away"),
                            "venue": g.get("venue"), "weather": g.get("weather")},
                    "score_key": prob + 0.05 * _confidence_rank(g["confidence"]),
                })
        except Exception as e:
            print(f"[picks] mlb gather failed: {e}")
    # --- NBA & NFL (live from ESPN) ---
    for sp in ("nba", "nfl"):
        try:
            from espn_provider import get_games as _espn_games
            for g in _espn_games(sp, target):
                if g["status"] == "finished":
                    continue
                prob = max(g["prob_home"], 1 - g["prob_home"])
                pick = g["home"]["name"] if g["prob_home"] >= 0.5 else g["away"]["name"]
                fav = g["home"] if g["prob_home"] >= 0.5 else g["away"]
                dog = g["away"] if g["prob_home"] >= 0.5 else g["home"]
                plays.append({
                    "sport": sp, "id": g["id"], "kind": "moneyline",
                    "match": f"{g['away']['name']} @ {g['home']['name']}",
                    "tournament": g.get("venue", ""),
                    "pick": f"{pick} to win", "prob": round(prob, 3),
                    "confidence": g["confidence"], "event_time": g.get("event_time"),
                    "ctx": {"fav_record": fav.get("record"), "dog_record": dog.get("record"),
                            "exp_margin": g.get("exp_margin"), "fav_name": fav.get("name"),
                            "dog_name": dog.get("name")},
                    "score_key": prob + 0.05 * _confidence_rank(g["confidence"]),
                })
        except Exception as e:
            print(f"[picks] {sp} gather failed: {e}")
    # --- College baseball (ESPN + Warren Nolan RPI) ---
    try:
        from ncaab_baseball import get_games as _cbb_games
        for g in _cbb_games(target):
            if g["status"] == "finished":
                continue
            prob = max(g["prob_home"], 1 - g["prob_home"])
            pick = g["home"]["name"] if g["prob_home"] >= 0.5 else g["away"]["name"]
            fav = g["home"] if g["prob_home"] >= 0.5 else g["away"]
            dog = g["away"] if g["prob_home"] >= 0.5 else g["home"]
            plays.append({
                "sport": "ncaabb", "id": g["id"], "kind": "moneyline",
                "match": f"{g['away']['name']} @ {g['home']['name']}",
                "tournament": g.get("venue", ""),
                "pick": f"{pick} to win", "prob": round(prob, 3),
                "confidence": g["confidence"], "event_time": g.get("event_time"),
                "ctx": {"fav_record": fav.get("record"), "dog_record": dog.get("record"),
                        "exp_margin": g.get("exp_margin"), "fav_name": fav.get("name"),
                        "dog_name": dog.get("name"), "factors": g.get("factors")},
                "score_key": prob + 0.05 * _confidence_rank(g["confidence"]),
            })
    except Exception as e:
        print(f"[picks] ncaabb gather failed: {e}")
    plays.sort(key=lambda p: -p["score_key"])
    return plays


def _enrich_odds(p):
    """Attach the model's fair odds (always) and, if available, the market line
    and CLV for this pick. Honest: fair odds are derived from the model's own
    probability; market odds come from the configured odds source if any."""
    from clv import american_to_prob
    prob = p.get("prob")
    if prob:
        # fair American odds from model probability (no vig)
        if prob >= 0.5:
            fair = -round(100 * prob / (1 - prob))
        else:
            fair = round(100 * (1 - prob) / prob)
        p["fair_odds"] = fair
    # market odds: team sports via snapshot, tennis via per-tournament feed
    try:
        import odds_api
        if odds_api.enabled() and p["sport"] in ("mlb", "nba", "nfl"):
            from models import OddsSnapshot
            with SessionLocal() as db:
                snap = db.query(OddsSnapshot).filter_by(sport=p["sport"], ref=str(p["id"])).first()
            if snap and snap.last_odds is not None:
                p["market_odds"] = snap.last_odds
                if p.get("fair_odds") is not None:
                    fp = american_to_prob(p["fair_odds"])
                    mp = american_to_prob(snap.last_odds)
                    if fp is not None and mp is not None:
                        p["edge_pct"] = round((fp - mp) * 100, 1)
        elif odds_api.enabled() and p["sport"] == "tennis":
            book = odds_api.get_tennis_odds()
            if book:
                from odds_api import _norm
                pick_name = p.get("pick", "").replace(" to win", "").strip()
                names = [n.strip() for n in p.get("match", "").split(" vs ")]
                if len(names) == 2:
                    rec = book.get(_norm(names[0]) + "|" + _norm(names[1]))
                    if rec:
                        mo = rec["odds_a"] if _norm(rec["a"]) == _norm(pick_name) else rec["odds_b"]
                        am = odds_api.american_from_decimal(mo) if mo else None
                        if am is not None:
                            p["market_odds"] = am
                            if p.get("fair_odds") is not None:
                                fp = american_to_prob(p["fair_odds"])
                                mp = american_to_prob(am)
                                if fp is not None and mp is not None:
                                    p["edge_pct"] = round((fp - mp) * 100, 1)
                            _snapshot_odds("tennis", str(p["id"]),
                                           "a" if _norm(rec["a"]) == _norm(pick_name) else "b", am)
    except Exception:
        pass


def _short_reason(p):
    pct = round(p["prob"] * 100)
    name = p["pick"].replace(" to win", "")
    return f"{name} \u2014 {pct}% to win, {p['confidence']} confidence."


def _long_reason(p):
    """In-depth, premium-grade rationale for Best Bets, from gathered context."""
    pct = round(p["prob"] * 100)
    name = p["pick"].replace(" to win", "")
    ctx = p.get("ctx") or {}
    s = []
    if p["sport"] == "tennis":
        opp = ctx.get("opponent", "the field")
        surf = (ctx.get("surface") or "").lower()
        lead = f"The model rates {name} a {pct}% favorite over {opp}"
        if surf and surf != "unknown":
            lead += f" on {surf}"
        s.append(lead + ".")
        gap = ctx.get("rating_gap")
        edge = ctx.get("edge_size")
        if gap is not None and edge:
            s.append(f"That stems from a {edge} rating edge of about {gap} Elo points, "
                     f"the model's core measure of head-to-head strength.")
        if ctx.get("surface_note"):
            s.append(ctx["surface_note"].capitalize() + ".")
        if ctx.get("round"):
            s.append(f"Round: {ctx['round']}. The projection also folds in each player's "
                     f"recent form, days of rest, and prior meetings.")
        else:
            s.append("The projection folds in recent form, rest, and prior meetings.")
        if ctx.get("weather"):
            wx = f"Conditions at the venue: {ctx['weather']}."
            if ctx.get("weather_effect"):
                wx += f" {ctx['weather_effect']}"
            s.append(wx)
        s.append(f"Overall confidence: {p['confidence']}.")
    elif p["sport"] == "mlb":
        s.append(f"The model makes {name} a {pct}% favorite on the moneyline.")
        eh, ea = ctx.get("exp_runs_home"), ctx.get("exp_runs_away")
        if eh is not None and ea is not None:
            s.append(f"Run projection: {ea} for the away side and {eh} for the home side, "
                     f"from each lineup's offense against the opposing staff.")
        if ctx.get("fav_starter") and ctx.get("fav_era") is not None:
            s.append(f"{name}'s probable starter {ctx['fav_starter']} carries a "
                     f"{ctx['fav_era']:.2f} ERA, weighted with recent bullpen workload.")
        extras = []
        if ctx.get("venue"):
            extras.append(f"park factor at {ctx['venue']}")
        if ctx.get("weather"):
            extras.append(f"weather ({ctx['weather']})")
        if extras:
            s.append("The run environment is adjusted for " + " and ".join(extras) + ".")
        s.append("Recent form over each team's last 10 games is blended into the offense estimate. "
                 f"Confidence: {p['confidence']}.")
    elif p["sport"] == "ncaabb":
        s.append(f"The model makes {name} a {pct}% college baseball pick.")
        if ctx.get("fav_record") and ctx.get("dog_record"):
            s.append(f"Records: {ctx.get('fav_name','favorite')} at {ctx['fav_record']} "
                     f"versus {ctx.get('dog_name','opponent')} at {ctx['dog_record']}.")
        facts = ctx.get("factors") or []
        rpi = next((f for f in facts if "RPI" in f), None)
        if rpi:
            s.append(rpi + " — strength of schedule is decisive in college baseball, "
                     "so the model leans on RPI to correct raw records.")
        else:
            s.append("Live RPI wasn't available here, so this rests on records alone "
                     "— lower confidence.")
        if ctx.get("exp_margin") is not None:
            mg = abs(ctx["exp_margin"])
            s.append(f"Projected margin about {mg:.0f} run{'s' if mg != 1 else ''}.")
        s.append(f"Confidence: {p['confidence']}. Confirm the weekend rotation yourself — "
                 f"probable starters aren't in the free college feed.")
    else:  # nba / nfl
        league = p["sport"].upper()
        s.append(f"The model makes {name} a {pct}% {league} favorite.")
        if ctx.get("fav_record") and ctx.get("dog_record"):
            s.append(f"Season records: {ctx.get('fav_name','the favorite')} at {ctx['fav_record']} "
                     f"versus {ctx.get('dog_name','the opponent')} at {ctx['dog_record']}.")
        if ctx.get("exp_margin") is not None:
            mg = abs(ctx["exp_margin"])
            s.append(f"The projected scoring margin is roughly {mg:.0f} point{'s' if mg != 1 else ''}, "
                     f"derived from an Elo rating seeded by record and adjusted for home advantage.")
        s.append(f"Confidence: {p['confidence']}. (This v1 model does not yet include injuries "
                 f"or rest; treat those as your own final check.)")
    return " ".join(s)


def _log_shown_picks(view, target, picks):
    """Record which picks a view surfaced today, so its W/L can be attributed."""
    if not picks:
        return
    from models import PickLog
    try:
        with SessionLocal() as db:
            for p in picks:
                exists = db.query(PickLog).filter_by(view=view, sport=p["sport"],
                                                     ref=str(p["id"]),
                                                     shown_date=dt.datetime.combine(target, dt.time.min)).first()
                if not exists:
                    db.add(PickLog(view=view, sport=p["sport"], ref=str(p["id"]),
                                   shown_date=dt.datetime.combine(target, dt.time.min)))
            db.commit()
    except Exception as e:
        print(f"[picklog] {view} skipped: {e}")


@app.get("/api/picks/free")
def free_picks(date: str | None = None):
    """The FOUR best (highest-confidence) plays of the day. Always 4 when
    available, each showing its settled win/loss result once the game finishes."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_day(target)
    plays = _gather_plays(target)
    # rank by confidence then probability; take the top 4 regardless of threshold
    ranked = sorted(plays, key=lambda p: -(p.get("score_key", p["prob"])))
    strong = ranked[:4]
    out = []
    for p in strong:
        p["reason"] = _short_reason(p)
        _enrich_odds(p)
        p["result"] = _pick_result_status(p["sport"], str(p["id"]))
        p.pop("score_key", None)
        p.pop("ctx", None)
        out.append(p)
    _log_shown_picks("free", target, out)
    return {"date": target.isoformat(), "picks": out}


def _pick_result_status(sport, ref):
    """Return 'win'/'loss'/None for a pick that has settled (for Free Picks)."""
    from models import PickResult
    try:
        with SessionLocal() as db:
            r = db.query(PickResult).filter_by(sport=sport, ref=str(ref)).first()
            if r is None:
                return None
            return "win" if r.correct else "loss"
    except Exception:
        return None


@app.get("/api/picks/best")
def best_bets(date: str | None = None, sport: str | None = None, min_prob: float = 0.0):
    """Larger, filterable board with in-depth rationale (premium-style)."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_day(target)
    plays = _gather_plays(target)
    out = []
    for p in plays:
        if sport and p["sport"] != sport:
            continue
        if p["prob"] < min_prob:
            continue
        p["reason"] = _long_reason(p)       # in-depth for Best Bets
        _enrich_odds(p)
        p.pop("score_key", None)
        p.pop("ctx", None)
        out.append(p)
    # log the unfiltered top board (so the record reflects the view's real picks)
    if not sport and min_prob == 0.0:
        _log_shown_picks("best", target, out)
    return {"date": target.isoformat(), "count": len(out), "picks": out}


def _team_writeup(g, sport):
    league = sport.upper()
    fav = g["home"] if g["prob_home"] >= 0.5 else g["away"]
    dog = g["away"] if g["prob_home"] >= 0.5 else g["home"]
    favp = round((g["prob_home"] if g["prob_home"] >= 0.5 else 1 - g["prob_home"]) * 100)
    margin = abs(g["exp_margin"])
    home_side = "at home" if g["prob_home"] >= 0.5 else "on the road"
    paras = []

    # Paragraph 1: the pick and the edge
    p1 = [f"The model makes {fav['name']} the {league} pick at {favp}% to win, playing {home_side}."]
    if fav["record"] and dog["record"]:
        p1.append(f"On the season, {fav['name']} sit at {fav['record']} against "
                  f"{dog['name']}'s {dog['record']}, and the rating model weighs that body of "
                  f"work along with the quality of opponents each has faced.")
    paras.append(" ".join(p1))

    # Paragraph 2: the margin and what drives it
    p2 = [f"Projected scoring margin is about {margin:.0f} point{'s' if margin != 1 else ''} in "
          f"{fav['name']}'s favor."]
    p2.append("That number comes from an Elo rating seeded by each team's record and adjusted "
              "for home-court advantage — " + ("a meaningful edge in the NBA, worth roughly "
              "two to three points a night." if sport == "nba" else
              "worth roughly two to three points for the home side in the NFL."))
    if favp >= 65:
        p2.append("The gap here is wide enough that the model treats it as a clear lean rather "
                  "than a coin-flip.")
    elif favp <= 56:
        p2.append("This is a close matchup, so the edge is slim and the pick is low-conviction.")
    paras.append(" ".join(p2))

    # Paragraph 3: honest limitations
    paras.append("One caveat worth your own check: this model is built on team strength, record, "
                 "and home advantage. It does not yet account for injuries, rest, back-to-backs, "
                 "or late lineup news — so confirm availability of key players before relying on it. "
                 f"Model confidence on this game: {g['confidence']}.")
    return "\n\n".join(paras)


_TEAM_AI_PROMPT = """You are a {league} analyst writing a short game preview.
Use ONLY these facts; invent nothing. 2-3 tight paragraphs on why the model favors the pick,
covering records, the projected margin, and home/road. Natural prose, no markdown.

FACTS:
{facts}
"""


def _ncaabb_writeup(g):
    """Multi-paragraph analysis for a college baseball game, honest about scope."""
    fav = g["home"] if g["prob_home"] >= 0.5 else g["away"]
    dog = g["away"] if g["prob_home"] >= 0.5 else g["home"]
    favp = round((g["prob_home"] if g["prob_home"] >= 0.5 else 1 - g["prob_home"]) * 100)
    margin = abs(g["exp_margin"])
    home_side = "at home" if g["prob_home"] >= 0.5 else "on the road"
    factors = g.get("factors") or []
    model = g.get("model", "")
    paras = []
    p1 = [f"The model makes {fav['name']} the pick at {favp}% to win, playing {home_side}."]
    if fav["record"] and dog["record"]:
        p1.append(f"Season records: {fav['name']} at {fav['record']} versus "
                  f"{dog['name']} at {dog['record']}.")
    paras.append(" ".join(p1))

    run_fact = next((f for f in factors if "Run model" in f), None)
    rpi_fact = next((f for f in factors if "RPI" in f), None)
    if run_fact:
        tot = g.get("avg_total")
        paras.append("This projection uses the full run-expectancy engine — the same "
                     "model as MLB — driven by each team's actual offense and pitching. "
                     + run_fact + (f" Projected total: about {tot} runs." if tot else ""))
    elif rpi_fact:
        paras.append("Strength of schedule matters enormously in college baseball, where "
                     "a team can pad its record against weak opponents. " + rpi_fact +
                     " — RPI weighs who they've actually played, and the model leans on it "
                     "to correct raw win-loss records.")
    else:
        paras.append("Note: detailed team stats and RPI weren't available for this matchup, "
                     "so the projection rests on win-loss records and ranking alone — treat "
                     "the edge as lower-confidence.")

    tail = (f"Projected margin is about {margin:.0f} run{'s' if margin != 1 else ''} "
            f"in {fav['name']}'s favor. Model confidence: {g['confidence']}.")
    if model != "run-expectancy":
        tail += (" Honest caveat: this is a team-strength model — ESPN's free college feed "
                 "doesn't expose probable starters or bullpen usage, so confirm the weekend "
                 "rotation yourself.")
    else:
        tail += (" Caveat: team-level offense and pitching drive this; it does not yet model "
                 "the specific weekend starter, so confirm the probable pitcher yourself.")
    paras.append(tail)
    return "\n\n".join(paras)


def _team_analysis(g, sport):
    base = _team_writeup(g, sport)
    if LLM_COMPLETE is None:
        return base
    import json as _j
    facts = {
        "league": sport.upper(), "home": g["home"]["name"], "away": g["away"]["name"],
        "home_record": g["home"]["record"], "away_record": g["away"]["record"],
        "home_win_pct": round(g["prob_home"] * 100), "exp_margin": g["exp_margin"],
        "venue": g.get("venue"), "confidence": g["confidence"],
    }
    try:
        return LLM_COMPLETE(_TEAM_AI_PROMPT.format(league=sport.upper(),
                                                   facts=_j.dumps(facts, indent=2))).strip() or base
    except Exception:
        return base


@app.get("/api/{sport}/games")
def team_games(sport: str, date: str | None = None):
    if sport not in ("nba", "nfl"):
        return []
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from espn_provider import get_games
        games = get_games(sport, target)
    except Exception as e:
        print(f"[{sport}] games failed: {e}")
        return []
    games = _attach_odds(sport, games)
    try:
        with SessionLocal() as db:
            wrote = False
            for g in games:
                if g.get("status") == "finished" and g.get("winner") in ("home", "away"):
                    predicted = "home" if g["prob_home"] >= 0.5 else "away"
                    _record_result(db, sport, g["id"], predicted, g["winner"])
                    wrote = True
            if wrote:
                db.commit()
    except Exception as e:
        print(f"[accuracy] {sport} log skipped: {e}")
    return games


@app.get("/api/ncaabb/ping")
def ncaabb_ping():
    """
    Connectivity test that CANNOT hang. Each external host is pinged exactly once
    with a hard 3-second timeout, fully isolated so one failure never blocks the
    others, and NO per-game logic runs. Tells us definitively which hosts the
    Render server can reach and whether the Highlightly key authenticates.
    """
    import time as _t
    results = {}

    def _probe(label, method):
        t0 = _t.time()
        try:
            info = method()
            results[label] = {"ok": True, "ms": int((_t.time() - t0) * 1000), **info}
        except Exception as e:
            results[label] = {"ok": False, "ms": int((_t.time() - t0) * 1000),
                              "error": type(e).__name__, "detail": str(e)[:200]}

    def _espn():
        import httpx
        url = ("https://site.api.espn.com/apis/site/v2/sports/baseball/"
               "college-baseball/scoreboard")
        r = httpx.get(url, params={"limit": 5}, timeout=3.0)
        j = r.json()
        return {"status": r.status_code, "events": len(j.get("events", []) or [])}

    def _highlightly():
        import httpx
        import highlightly as hl
        if not hl.enabled():
            return {"note": "no key set / breaker open"}
        headers = {"x-rapidapi-key": hl.API_KEY}
        if hl.PLATFORM == "rapidapi":
            headers["x-rapidapi-host"] = hl.HOST
        r = httpx.get(hl.BASE + "/teams", params={"league": "NCAA"},
                      headers=headers, timeout=3.0)
        return {"status": r.status_code, "host": hl.HOST,
                "platform": hl.PLATFORM, "body": r.text[:300]}

    def _highlightly_matches():
        import httpx
        import highlightly as hl
        if not hl.enabled():
            return {"note": "no key set"}
        headers = {"x-rapidapi-key": hl.API_KEY}
        if hl.PLATFORM == "rapidapi":
            headers["x-rapidapi-host"] = hl.HOST
        # probe the games endpoint for the super-regional weekend
        r = httpx.get(hl.BASE + "/matches",
                      params={"league": "NCAA", "date": "2026-06-07",
                              "timezone": "America/Chicago"},
                      headers=headers, timeout=3.0)
        return {"status": r.status_code, "body": r.text[:400]}

    def _warrennolan():
        import httpx
        r = httpx.get("https://www.warrennolan.com/baseball/2026/rpi-live",
                      timeout=3.0,
                      headers={"User-Agent": "LineLogic/1.0 connectivity check"})
        return {"status": r.status_code, "bytes": len(r.text)}

    _probe("espn", _espn)
    _probe("highlightly", _highlightly)
    _probe("highlightly_matches", _highlightly_matches)
    _probe("warrennolan", _warrennolan)
    results["_summary"] = {
        "reachable": [k for k, v in results.items()
                      if isinstance(v, dict) and v.get("ok")],
        "note": "Each host hard-capped at 3s; total worst case ~9s.",
    }
    return results


@app.get("/api/ncaabb/hl-debug")
def ncaabb_hl_debug(team: str = "Texas"):
    """Deep diagnostic: hits several plausible Highlightly endpoints and shows the
    RAW response shape so we can see exactly what the direct platform returns."""
    import highlightly as hl
    out = {"enabled": hl.enabled(), "host": hl.HOST, "platform": hl.PLATFORM}
    if not hl.enabled():
        out["note"] = "Set HIGHLIGHTLY_API_KEY in Render to enable."
        return out
    import httpx
    headers = ({"x-rapidapi-key": hl.API_KEY, "x-rapidapi-host": hl.HOST}
               if hl.PLATFORM == "rapidapi" else {"x-api-key": hl.API_KEY})
    # try a range of likely endpoints/params and capture status + a small sample
    probes = [
        ("/teams?name=Texas&limit=3", "/teams", {"name": "Texas", "limit": 3}),
        ("/teams?league=NCAA&limit=3", "/teams", {"league": "NCAA", "limit": 3}),
        ("/teams?limit=3", "/teams", {"limit": 3}),
        ("/leagues?limit=10", "/leagues", {"limit": 10}),
        ("/baseball/teams?name=Texas", "/baseball/teams", {"name": "Texas", "limit": 3}),
    ]
    out["probes"] = {}
    for label, path, params in probes:
        try:
            r = httpx.get(hl.BASE + path, params=params, headers=headers, timeout=15)
            body = r.text[:600]
            out["probes"][label] = {"status": r.status_code, "body": body}
        except Exception as e:
            out["probes"][label] = {"error": str(e)}
    return out


@app.get("/api/ncaabb/debug")
def ncaabb_debug(date: str | None = None):
    """Diagnostic: shows raw ESPN counts per query variant AND what the real
    provider returns after processing, so we can see exactly where games drop."""
    import httpx
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    ds = target.strftime("%Y%m%d")
    nxt = (target + dt.timedelta(days=1)).strftime("%Y%m%d")
    prv = (target - dt.timedelta(days=1)).strftime("%Y%m%d")
    SB = "https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/scoreboard"
    variants = {
        "single": {"dates": ds, "limit": 400},
        "range": {"dates": f"{prv}-{nxt}", "limit": 400},
    }
    out = {"date": target.isoformat(), "raw_espn": {}}
    for label, params in variants.items():
        try:
            r = httpx.get(SB, params=params, timeout=15)
            j = r.json()
            evs = j.get("events", []) or []
            sample = []
            for e in evs[:6]:
                comp = (e.get("competitions") or [{}])[0]
                cs = comp.get("competitors", [])
                names = [c.get("team", {}).get("displayName", "?") for c in cs]
                sample.append({"raw_date": e.get("date", ""), "teams": names})
            out["raw_espn"][label] = {"status": r.status_code, "event_count": len(evs),
                                      "sample": sample}
        except Exception as e:
            out["raw_espn"][label] = {"error": str(e)}
    # Highlightly games path (the new primary source)
    try:
        import highlightly as hl
        if hl.enabled():
            hg = hl.get_games(target)
            out["highlightly_games"] = {
                "enabled": True, "count": len(hg),
                "sample": [{"teams": x["away"]["name"] + " @ " + x["home"]["name"],
                            "status": x["status"], "time": x.get("event_time")}
                           for x in hg[:6]]}
        else:
            out["highlightly_games"] = {"enabled": False,
                                        "note": "no key or breaker open"}
    except Exception as e:
        import traceback
        out["highlightly_games"] = {"error": str(e), "trace": traceback.format_exc()[-600:]}
    # ESPN provider output
    try:
        from ncaab_baseball import get_games as espn_games, _cache
        _cache.clear()
        g = espn_games(target)
        out["espn_provider"] = {"count": len(g),
                                "sample": [x["away"]["name"] + " @ " + x["home"]["name"]
                                           for x in g[:6]]}
    except Exception as e:
        import traceback
        out["espn_provider"] = {"error": str(e), "trace": traceback.format_exc()[-600:]}
    # the ACTUAL endpoint the frontend calls (Highlightly-first, ESPN fallback)
    try:
        final = ncaabb_games(date=target.isoformat())
        if isinstance(final, dict):
            gl = final.get("games", final)
        else:
            gl = final
        out["FINAL_endpoint"] = {"count": len(gl),
                                 "sample": [x["away"]["name"] + " @ " + x["home"]["name"]
                                            for x in gl[:6]] if gl else []}
    except Exception as e:
        import traceback
        out["FINAL_endpoint"] = {"error": str(e), "trace": traceback.format_exc()[-600:]}
    return out


@app.get("/api/ncaabb/games")
def ncaabb_games(date: str | None = None, debug: int = 0):
    """College baseball games for a date. Tries BOTH sources and returns whichever
    has games (Highlightly preferred), so one source failing never yields an empty
    board when the other has data. ?debug=1 returns a diagnostic wrapper."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    diag = {"hl_enabled": False, "hl_count": 0, "hl_error": None,
            "espn_count": 0, "espn_error": None, "source": "none"}
    hl_games, espn_games = [], []
    # 1) Highlightly
    try:
        import highlightly as hl
        diag["hl_enabled"] = hl.enabled()
        if hl.enabled():
            hl_games = hl.get_games(target) or []
            diag["hl_count"] = len(hl_games)
    except Exception as e:
        diag["hl_error"] = str(e)[:200]
        print(f"[ncaabb] highlightly games failed: {e}")
    # 2) ESPN — fetch it whenever Highlightly didn't yield games (independent path)
    if not hl_games:
        try:
            from ncaab_baseball import get_games as espn_get
            espn_games = espn_get(target) or []
            diag["espn_count"] = len(espn_games)
        except Exception as e:
            diag["espn_error"] = str(e)[:200]
            print(f"[ncaabb] espn games failed: {e}")
    games = hl_games or espn_games
    diag["source"] = "highlightly" if hl_games else ("espn" if espn_games else "none")
    # settle finished games for accuracy
    try:
        with SessionLocal() as db:
            wrote = False
            for g in games:
                if g.get("status") == "finished" and g.get("winner") in ("home", "away"):
                    predicted = "home" if g["prob_home"] >= 0.5 else "away"
                    _record_result(db, "ncaabb", g["id"], predicted, g["winner"])
                    wrote = True
            if wrote:
                db.commit()
    except Exception as e:
        print(f"[accuracy] ncaabb log skipped: {e}")
    if debug:
        return {"diag": diag, "count": len(games), "games": games}
    return games


@app.get("/api/ncaabb/game/{game_id}")
def ncaabb_game(game_id: str, date: str | None = None):
    """One college baseball game with analysis writeup."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from ncaab_baseball import get_games
        games = get_games(target)
    except Exception:
        games = []
    g = next((x for x in games if str(x["id"]) == str(game_id)), None)
    if not g:
        return {"error": "not found"}
    g = dict(g)
    g["analysis"] = _ncaabb_writeup(g)
    return g


@app.get("/api/{sport}/game/{game_id}")
def team_game(sport: str, game_id: str, date: str | None = None):
    if sport not in ("nba", "nfl"):
        return {"error": "bad sport"}
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from espn_provider import get_game
        g = get_game(sport, target, game_id)
    except Exception as e:
        print(f"[{sport}] game failed: {e}")
        g = None
    if not g:
        return {"error": "not found"}
    g = dict(g)
    g["analysis"] = _team_analysis(g, sport)
    try:
        from betting import team_lines
        g["lines"] = team_lines(g["prob_home"], g["exp_margin"], sport)
    except Exception as e:
        print(f"[{sport}] lines failed: {e}")
    return g


@app.get("/api/mlb/props/{game_id}")
def mlb_props(game_id: int, date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from mlb_provider import get_props
        return get_props(target, game_id)
    except Exception as e:
        print(f"[mlb] props failed: {e}")
        return {"props": []}


@app.get("/api/{sport}/props/{game_id}")
def team_props(sport: str, game_id: str, date: str | None = None):
    if sport not in ("nba", "nfl"):
        return {"props": []}
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from espn_provider import get_props
        return get_props(sport, target, game_id)
    except Exception as e:
        print(f"[{sport}] props failed: {e}")
        return {"props": []}


@app.get("/api/mlb/prop-history/{game_id}")
def mlb_prop_history(game_id: int, player: str, stat: str, line: float,
                     date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from mlb_provider import get_prop_history
        return get_prop_history(target, game_id, player, stat, line)
    except Exception as e:
        print(f"[mlb] prop-history failed: {e}")
        return {"games": []}


@app.get("/api/{sport}/prop-history/{game_id}")
def team_prop_history(sport: str, game_id: str, player: str, stat: str,
                      line: float, date: str | None = None):
    if sport not in ("nba", "nfl"):
        return {"history": []}
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from espn_provider import get_prop_history
        return get_prop_history(sport, target, game_id, player, stat, line)
    except Exception as e:
        print(f"[{sport}] prop-history failed: {e}")
        return {"history": []}


@app.get("/api/news/{sport}")
def sport_news(sport: str, date: str | None = None):
    """News headlines + today's injury report for a sport.
    Injuries from ESPN (structured); headlines from Yardbarker (trades, signings,
    free agency, transfer portal) with ESPN headlines as a fallback."""
    if sport not in ("nba", "nfl", "mlb"):
        return {"news": [], "injuries": [], "headlines": []}
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    news = injuries = headlines = []
    try:
        from espn_provider import get_news, get_injuries
        news = get_news(sport)
        injuries = get_injuries(sport, target)
    except Exception as e:
        print(f"[news] {sport} espn failed: {e}")
    try:
        from yardbarker import get_headlines
        headlines = get_headlines(sport, limit=20)
    except Exception as e:
        print(f"[news] {sport} yardbarker failed: {e}")
    return {"sport": sport, "news": news, "injuries": injuries, "headlines": headlines}


@app.get("/api/performance")
def performance(days: int = 30, sport: str | None = None):
    """
    Units won/lost, ROI, and CLV over the last N days, from settled picks that
    have captured odds. Honest about coverage: only picks with a recorded line
    count toward units/ROI/CLV; everything else still counts toward W/L.
    """
    from models import PickResult
    from clv import summarize
    import odds_api
    since = dt.datetime.now() - dt.timedelta(days=days)
    bets = []
    wins = losses = 0
    with SessionLocal() as db:
        q = db.query(PickResult).filter(PickResult.settled_date >= since)
        if sport:
            q = q.filter(PickResult.sport == sport)
        for r in q.all():
            if r.correct:
                wins += 1
            else:
                losses += 1
            if r.taken_odds is not None:
                bets.append({"odds": r.taken_odds, "won": bool(r.correct),
                             "close_odds": r.close_odds})
    s = summarize(bets)
    # overall record includes picks without odds; betting metrics only the priced ones
    s["record_wins"] = wins
    s["record_losses"] = losses
    s["record_total"] = wins + losses
    s["odds_enabled"] = odds_api.enabled()
    s["odds_quota"] = odds_api.quota() if odds_api.enabled() else None
    s["odds_spend_today"] = odds_api.spend_today() if odds_api.enabled() else None
    s["days"] = days
    return s


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(ws)


@app.get("/api/version")
def version():
    """Backend build marker. If this lags the footer build number, the Python
    process didn't redeploy (frontend updated but backend stale) — which would
    explain new UI behavior not matching backend behavior."""
    return {"backend_build": "v50",
            "has_ncaabb_debug_param": True,
            "ncaabb_sources": ["highlightly", "espn"]}


@app.get("/")
def index():
    # No-cache so every deploy reaches the browser immediately. index.html is
    # small; the cost is negligible and it prevents stale-frontend confusion.
    return FileResponse("index.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})
