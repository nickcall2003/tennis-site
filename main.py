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
import concurrent.futures
import datetime as dt
import json
import os
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import func

from db import SessionLocal
from live import LiveEngine
from models import LiveState, Match, Prediction, StatSnapshot
from predictions import PredictionEngine
from ws import manager
import sports

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
            # Cache the trained ratings to disk so we DON'T retrain on every boot.
            # Point RATINGS_FILE at the persistent volume (e.g. /data/ratings.json)
            # and the next boot loads this instead of training again.
            try:
                _rf = os.environ.get("RATINGS_FILE", "ratings.json")
                engine.export_ratings(_rf)
                print(f"[predictions] cached ratings -> {_rf} (skips retraining next boot)")
            except Exception as _e:
                print(f"[predictions] could not cache ratings ({_e})")
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
    # Today and future days are still filling in — on grass/clay swing days the
    # singles main draws post hours after the doubles and qualifying do, so we
    # must NOT freeze a day on its first partial build. Only PAST days are
    # immutable once built.
    is_open = day >= dt.date.today()
    if key in _built_dates and not is_open:
        return
    start = dt.datetime.combine(day, dt.time.min)
    end = dt.datetime.combine(day, dt.time.max)
    # PAST days: if rows already exist (e.g. persisted across a restart), mark
    # built and skip the expensive rebuild — this is what stops the
    # rebuild-on-every-boot loop. We deliberately DON'T take this shortcut for
    # today/future, so a stale partial slate can refresh into the full one.
    if not is_open:
        try:
            with SessionLocal() as db:
                has = db.query(Match.id).filter(Match.scheduled >= start,
                                                Match.scheduled <= end).first()
            if has:
                _built_dates.add(key)
                return
        except Exception as e:
            print(f"[build] db check failed for {key}: {e}")
    # Throttle rebuilds. Open days refresh every TENNIS_REFRESH_MINUTES so new
    # singles get pulled in through the day; past empty days retry once a minute.
    throttle = int(os.environ.get("TENNIS_REFRESH_MINUTES", "30")) * 60 if is_open else 60
    last = _build_attempts.get(key, 0)
    if _t.time() - last < throttle:
        return
    _build_attempts[key] = _t.time()
    try:
        build_day(provider, engine, dt.datetime(day.year, day.month, day.day))
        # Only PAST days get permanently marked built; open days stay refreshable.
        if not is_open:
            with SessionLocal() as db:
                has = db.query(Match.id).filter(Match.scheduled >= start,
                                                Match.scheduled <= end).first()
            if has:
                _built_dates.add(key)
    except Exception as e:
        print(f"[build] could not build {key}: {e}")



def _backfill_results(days: int) -> None:
    """Populate accuracy history: replay the past `days` days through each sport's
    board so finished games settle into PickResults (which power the 30-day
    figures). Reuses the existing endpoint settling logic. Throttled + guarded.

    Pass 1 (team sports) is cheap — just provider reads. Pass 2 (tennis) is heavy
    because it builds each past day, so it's last and opt-in."""
    if not USE_REAL or days <= 0:
        return
    import time as _t
    today = dt.date.today()

    # Pass 1: team sports (cheap reads) — settles NCAA / MLB / NBA / NFL.
    for off in range(1, days + 1):
        d = (today - dt.timedelta(days=off)).isoformat()
        for label in ("ncaabb", "mlb", "nba", "nfl"):
            try:
                if label == "ncaabb":
                    ncaabb_games(date=d)
                elif label == "mlb":
                    mlb_games(date=d)
                else:
                    team_games(label, date=d)
            except Exception as e:
                print(f"[results-backfill] {d}/{label} skipped: {e}")
            _t.sleep(0.4)
        _t.sleep(0.5)
    print(f"[results-backfill] team sports done ({days} days)")

    # Pass 2: tennis — builds each past day (heavier). Opt-in via env var.
    if os.environ.get("RESULTS_BACKFILL_TENNIS", "0") == "1":
        for off in range(1, days + 1):
            d = (today - dt.timedelta(days=off)).isoformat()
            try:
                list_matches(date=d)   # builds (if needed) + settles tennis
            except Exception as e:
                print(f"[results-backfill] {d}/tennis skipped: {e}")
            _t.sleep(1.5)
        print(f"[results-backfill] tennis done ({days} days)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from db import init_db
        init_db()
    except Exception as e:
        print(f"[startup] init_db failed: {e}")

    run_bg = os.environ.get("RUN_BACKGROUND", "1") == "1"
    startup_build = os.environ.get("STARTUP_BUILD", "0") == "1"

    # Warren Nolan RPI warm-up — light, runs whenever background is on.
    if USE_REAL and run_bg:
        # Delay the RPI warm-up so the heavy parse doesn't run during the Railway
        # healthcheck window. The app passes /healthz first; RPI loads ~60s later.
        def _warm_rpi():
            import time as _t
            _t.sleep(60)
            try:
                import warrennolan
                warrennolan.warm()
            except Exception as e:
                print(f"[startup] warrennolan warm failed: {e}")
        import threading as _thr
        _thr.Thread(target=_warm_rpi, daemon=True).start()


    # Results backfill — fills the 30-day accuracy by replaying past finished
    # games into PickResults. Own daemon thread, delayed so the app is healthy
    # first; gated + throttled so it stays safe.
    rb_days = int(os.environ.get("RESULTS_BACKFILL_DAYS", "0") or 0)
    if USE_REAL and run_bg and rb_days > 0:
        def _results_bg():
            import time as _t
            _t.sleep(30)
            try:
                _backfill_results(rb_days)
            except Exception as e:
                print(f"[startup] results backfill failed: {e}")
        import threading as _thr
        _thr.Thread(target=_results_bg, daemon=True).start()

    if USE_REAL and run_bg:
        import threading as _thr_tla
        _thr_tla.Thread(target=_tennis_lookahead, daemon=True).start()

    # AI narration warmer — pre-narrates today's board in the background so user
    # page loads are instant and fully Claude-written. No-op without a key.
    if run_bg and LLM_COMPLETE is not None:
        def _narration_warmer():
            import time as _t
            import narrate as _nar, premium as _prem
            _t.sleep(90)   # pass healthcheck + let the first build settle
            while True:
                try:
                    target = dt.date.today()
                    plays = _gather_plays(target)
                    slate = [dict(p) for p in plays]
                    for p in plays:
                        _nar.warm(_long_reason(p), kind="reason",
                                  sport=p["sport"], llm=LLM_COMPLETE)
                        pf = _prem.premium_facts(p, slate, SessionLocal)
                        _nar.warm(pf["text"], kind="premium",
                                  sport=p["sport"], llm=LLM_COMPLETE)
                except Exception as e:
                    print(f"[ai] narration warmer failed: {e}")
                _t.sleep(int(os.environ.get("AI_WARM_INTERVAL", "1800")))
        import threading as _thr_nw
        _thr_nw.Thread(target=_narration_warmer, daemon=True).start()
        print("[ai] narration warmer started")

    if USE_REAL and run_bg and startup_build:
        def _startup_bg():
            import time as _t
            _t.sleep(5)
            try:
                _ensure_day(dt.date.today())
            except Exception as e:
                print(f"[startup] ensure_day failed: {e}")
            bf = int(os.environ.get("BACKFILL_DAYS", "0") or 0)
            if bf > 0:
                try:
                    _backfill_recent(bf)
                except Exception as e:
                    print(f"[startup] backfill failed: {e}")
        import threading as _thr
        _thr.Thread(target=_startup_bg, daemon=True).start()
        _thr.Thread(target=_prewarm_all, daemon=True).start()
    elif USE_REAL is False:
        build_today(provider)

    task = asyncio.create_task(live_engine.run()) if run_bg else None
    yield
    live_engine.running = False
    if task:
        task.cancel()




def _tennis_lookahead():
    """Auto-build today + the next few days of the tennis slate so new
    tournaments appear on the site without anyone navigating to them.

    Light by design: _ensure_day skips any day already in the DB, so this only
    does real fetch work when a brand-new day or freshly-released draw first
    shows up. Re-checks a few times a day, which also covers the date rolling
    over at midnight. Controlled by TENNIS_LOOKAHEAD_DAYS (0 disables)."""
    import time as _t
    n = int(os.environ.get("TENNIS_LOOKAHEAD_DAYS", "3") or 0)
    if n <= 0:
        print("[tennis-lookahead] disabled (set TENNIS_LOOKAHEAD_DAYS>0)")
        return
    _t.sleep(60)  # let the app finish coming up first
    while True:
        try:
            today = dt.date.today()
            for off in range(0, n + 1):
                d = today + dt.timedelta(days=off)
                try:
                    _ensure_day(d)
                except Exception as e:
                    print(f"[tennis-lookahead] {d}: {e}")
                _t.sleep(2.0)
        except Exception as e:
            print(f"[tennis-lookahead] loop error: {e}")
        _t.sleep(6 * 3600)  # ~4x/day: picks up new draws and the date rollover


def _prewarm_all():
    """Keep the current slate warm so navigation is fast. OFF by default because
    on a single CPU the background fetching competes with serving pages. Enable
    with PREWARM=1 once on a 2+ CPU instance. (Added during the NCAA work; left
    running it was overloading the one core, causing the whole site to hang.)"""
    import time as _t
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
                    ncaabb_games(date=d.isoformat())
                except Exception as e:
                    print(f"[prewarm] ncaabb {d}: {e}")
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


@app.get("/api/tennis/debug")
def tennis_debug(date: str | None = None):
    """Read-only diagnostic: what does the tennis API actually return for a date?
    Shows raw fixture count, the event-type breakdown, and how _classify_tier
    sorts them — so we can tell 'no data yet' from 'filtered out' from 'error'."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    out = {"date": target.isoformat()}
    try:
        if not hasattr(provider, "_call"):
            return {"error": "not using the live API-Tennis provider", **out}
        from collections import Counter
        d = target.strftime("%Y-%m-%d")
        raw = provider._call("get_fixtures", date_start=d, date_stop=d)
        out["raw_fixtures"] = len(raw)
        out["event_types"] = dict(Counter((f.get("event_type_type") or "?") for f in raw))
        try:
            from apitennis import _classify_tier
            tiers = Counter()
            for f in raw:
                tiers[_classify_tier(f) or "EXCLUDED"] += 1
            out["classified"] = dict(tiers)
        except Exception as e:
            out["classify_error"] = str(e)
        out["sample"] = [{"tournament": f.get("tournament_name"),
                          "type": f.get("event_type_type"),
                          "date": f.get("event_date"),
                          "a": f.get("event_first_player"),
                          "b": f.get("event_second_player")} for f in raw[:10]]
    except Exception as e:
        out["error"] = str(e)
    return out


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

        tns_odds = None
        try:
            import odds_api
            if odds_api.enabled():
                book = odds_api.get_tennis_odds()
                from odds_api import _norm
                rec = book.get(_norm(m.player_a) + "|" + _norm(m.player_b))
                if rec:
                    if _norm(rec["a"]) == _norm(m.player_a):
                        dec_a, dec_b = rec.get("odds_a"), rec.get("odds_b")
                    else:
                        dec_a, dec_b = rec.get("odds_b"), rec.get("odds_a")
                    tns_odds = {"ml_a": odds_api.american_from_decimal(dec_a) if dec_a else None,
                                "ml_b": odds_api.american_from_decimal(dec_b) if dec_b else None}
        except Exception as e:
            print(f"[detail] tennis odds failed: {e}")

        return {
            "id": m.id, "tier": m.tier, "tournament": m.tournament, "round": m.round,
            "surface": m.surface, "player_a": m.player_a, "player_b": m.player_b,
            "event_time": m.event_time, "status": m.status,
            "best_of": m.best_of, "odds": tns_odds,
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


def _norm_team(s):
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _odds_rec_sides(home_name, o):
    """Map an odds_api record's prices to OUR home/away by team name (The Odds
    API's home/away designation can be the reverse of ESPN's)."""
    if _norm_team(home_name) == _norm_team(o.get("home_team", "")):
        return o.get("ml_home"), o.get("ml_away")
    return o.get("ml_away"), o.get("ml_home")


def _attach_odds(sport, games):
    """Attach real market odds to each game and snapshot the pick's line.
    Tries The Odds API first, then SportsGameOdds (free) as a fallback so the
    model-vs-market edge can render even without an Odds API plan."""
    book = {}
    try:
        import odds_api
        if odds_api.enabled():
            book = odds_api.get_odds(sport) or {}
    except Exception as e:
        print(f"[odds] odds-api {sport} skipped: {e}")
    sgo = None
    try:
        import sgo_api
        if sgo_api.enabled():
            sgo = sgo_api
    except Exception:
        sgo = None
    for g in games:
        if g.get("odds"):
            continue                          # provider already attached (soccer)
        o = book.get(_norm_team(g["home"]["name"]) + "|" + _norm_team(g["away"]["name"])) if book else None
        if o:
            mlh, mla = _odds_rec_sides(g["home"]["name"], o)
            g["odds"] = {"ml_home": mlh, "ml_away": mla,
                         "spread_home": o.get("spread_home"), "total": o.get("total"),
                         "books": o.get("books")}
        elif sgo is not None:
            try:
                so = sgo.get_game_odds(sport, g["home"]["name"], g["away"]["name"])
            except Exception:
                so = None
            if so and (so.get("ml_home") is not None or so.get("ml_away") is not None):
                g["odds"] = {"ml_home": so.get("ml_home"), "ml_away": so.get("ml_away"),
                             "spread_home": None, "total": None,
                             "books": ["SportsGameOdds"]}
        if g.get("odds"):                     # snapshot the side we pick (CLV)
            side = "home" if g["prob_home"] >= 0.5 else "away"
            taken = g["odds"]["ml_home"] if side == "home" else g["odds"]["ml_away"]
            if taken is not None:
                try:
                    _snapshot_odds(sport, str(g["id"]), side, int(round(taken)))
                except Exception:
                    pass
    return games


def _attach_odds_one(sport, g):
    """Attach market odds to a single detail game so the live edge can render.
    Odds API first, then SportsGameOdds (free) fallback. Pure read (no CLV
    snapshot); no-op when nothing matches."""
    if g.get("odds"):
        return g
    o = None
    try:
        import odds_api
        if odds_api.enabled():
            book = odds_api.get_odds(sport) or {}
            o = book.get(_norm_team(g["home"]["name"]) + "|" + _norm_team(g["away"]["name"]))
    except Exception as e:
        print(f"[odds] detail attach {sport} skipped: {e}")
    if o:
        mlh, mla = _odds_rec_sides(g["home"]["name"], o)
        g["odds"] = {"ml_home": mlh, "ml_away": mla,
                     "spread_home": o.get("spread_home"), "total": o.get("total"),
                     "books": o.get("books")}
        return g
    try:
        import sgo_api
        if sgo_api.enabled():
            so = sgo_api.get_game_odds(sport, g["home"]["name"], g["away"]["name"])
            if so and (so.get("ml_home") is not None or so.get("ml_away") is not None):
                g["odds"] = {"ml_home": so.get("ml_home"), "ml_away": so.get("ml_away"),
                             "spread_home": None, "total": None,
                             "books": ["SportsGameOdds"]}
    except Exception as e:
        print(f"[odds] detail sgo {sport} skipped: {e}")
    return g


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


def _is_soccer_push(r):
    """A soccer 'to win' pick whose match ended in a draw is a push: it counts
    neither for nor against the record (draws don't count against soccer)."""
    return (getattr(r, "sport", None) == "soccer"
            and str(getattr(r, "actual", "")) == "draw"
            and str(getattr(r, "predicted", "")) in ("home", "away"))


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
            if _is_soccer_push(r):
                continue                       # draw = push, excluded from record
            s = by_sport.setdefault(r.sport, {"picks": 0, "correct": 0})
            s["picks"] += 1
            tot_p += 1
            if r.correct:
                s["correct"] += 1
                tot_c += 1
        # all-time record (no date filter), per sport and overall
        allrows = db.query(PickResult).all()
        for r in allrows:
            if _is_soccer_push(r):
                continue                       # draw = push, excluded from record
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
    g = _attach_odds_one("mlb", g)
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
    # --- team sports via the registry (nba, nfl, ncaabb, nhl) — one loop ---
    for _key in sports.GENERIC_TEAM_KEYS:
        _sp = sports.get(_key)
        try:
            for g in _sp.games(target):
                if g.get("status") == "finished":
                    continue
                prob = max(g["prob_home"], 1 - g["prob_home"])
                pick = g["home"]["name"] if g["prob_home"] >= 0.5 else g["away"]["name"]
                fav = g["home"] if g["prob_home"] >= 0.5 else g["away"]
                dog = g["away"] if g["prob_home"] >= 0.5 else g["home"]
                plays.append({
                    "sport": _key, "id": g["id"], "kind": "moneyline",
                    "match": f"{g['away']['name']} @ {g['home']['name']}",
                    "tournament": g.get("venue", ""),
                    "pick": f"{pick} to win", "prob": round(prob, 3),
                    "confidence": g["confidence"], "event_time": g.get("event_time"),
                    "ctx": {"fav_record": fav.get("record"), "dog_record": dog.get("record"),
                            "exp_margin": g.get("exp_margin"), "fav_name": fav.get("name"),
                            "dog_name": dog.get("name"), "factors": g.get("factors"),
                            "avg_total": g.get("avg_total")},
                    "score_key": prob + 0.05 * _confidence_rank(g["confidence"]),
                })
        except Exception as e:
            print(f"[picks] {_key} gather failed: {e}")
    # --- soccer (all 15 leagues, live-today aggregate) ---
    try:
        import soccer_provider
        for g in soccer_provider.get_today(target):
            if g.get("status") == "finished":
                continue
            ph, pdw, pa = g["prob_home"], (g.get("prob_draw") or 0.0), g["prob_away"]
            probs = {"home": ph, "draw": pdw, "away": pa}
            side = max(probs, key=probs.get)
            prob = probs[side]
            od = g.get("odds") or {}
            if side == "home":
                pick = f"{g['home']['name']} to win"; opp = g["away"]["name"]
            elif side == "away":
                pick = f"{g['away']['name']} to win"; opp = g["home"]["name"]
            else:
                pick = "Draw"; opp = f"{g['away']['name']} / {g['home']['name']}"
            conf = "high" if prob >= 0.55 else "medium" if prob >= 0.42 else "low"
            plays.append({
                "sport": "soccer", "id": g["id"], "league": g["league"], "kind": "moneyline",
                "match": f"{g['away']['name']} @ {g['home']['name']}",
                "tournament": g.get("league_label", ""),
                "pick": pick, "prob": round(prob, 3),
                "confidence": conf, "event_time": g.get("event_time"),
                "ctx": {"pick_side": side, "opponent": opp,
                        "prob_home": g["prob_home"], "prob_draw": g.get("prob_draw"),
                        "prob_away": g["prob_away"],
                        "exp_goals_home": g.get("exp_goals_home"),
                        "exp_goals_away": g.get("exp_goals_away"),
                        "home_name": g["home"]["name"], "away_name": g["away"]["name"],
                        "home_record": g["home"].get("record"),
                        "away_record": g["away"].get("record"),
                        "league_label": g.get("league_label"), "venue": g.get("venue"),
                        "minute": g.get("minute"), "status": g["status"],
                        "score_home": g["score"]["home"], "score_away": g["score"]["away"],
                        "market_home": od.get("ml_home"), "market_draw": od.get("ml_draw"),
                        "market_away": od.get("ml_away")},
                "score_key": prob + 0.05 * _confidence_rank(conf),
            })
    except Exception as e:
        print(f"[picks] soccer gather failed: {e}")
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
    # soccer: ESPN 3-way moneylines (carried in ctx) -> market price + de-vigged edge
    if p["sport"] == "soccer":
        sctx = p.get("ctx") or {}
        mh, md, ma = sctx.get("market_home"), sctx.get("market_draw"), sctx.get("market_away")
        side = sctx.get("pick_side")
        pick_ml = md if side == "draw" else mh if side == "home" else ma
        if pick_ml is not None:
            p["market_odds"] = pick_ml
            ih = american_to_prob(mh) if mh is not None else None
            idr = american_to_prob(md) if md is not None else None
            ia = american_to_prob(ma) if ma is not None else None
            tot = sum(x for x in (ih, idr, ia) if x is not None)
            side_imp = idr if side == "draw" else ih if side == "home" else ia
            if tot and side_imp is not None:
                p["edge_pct"] = round((p["prob"] - side_imp / tot) * 100, 1)
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


def _amer_to_dec(a):
    a = float(a)
    return 1 + (a / 100 if a > 0 else 100 / (-a))


def _dec_to_amer(d):
    if d <= 1:
        return None
    return round((d - 1) * 100) if d >= 2 else -round(100 / (d - 1))


STAKE_BY_LEGS = {2: 1.0, 3: 0.75, 4: 0.5}
# Skip parlay legs that are heavier favorites than this (American). Stacking
# -1400 chalk yields a -700 two-leg with no value; this keeps real payouts.
PARLAY_LEG_FLOOR = int(os.environ.get("PARLAY_MIN_LEG_ODDS", "-350"))


def _build_parlays(target):
    """Build (but do NOT persist) the day's parlays from the best plays."""
    try:
        plays = [dict(p) for p in _gather_plays(target)]
    except Exception as e:
        print(f"[parlays] gather failed: {e}")
        return []
    cands = []
    for p in plays:
        try:
            _enrich_odds(p)
        except Exception:
            pass
        prob = p.get("prob")
        if prob is None or prob < 0.5 or prob > 0.97:
            continue
        odds = p.get("market_odds")
        priced = "market" if odds is not None else "model"
        if odds is None:
            odds = p.get("fair_odds")
        if odds is None:
            continue
        if int(odds) < PARLAY_LEG_FLOOR:
            continue                          # too chalky to add value
        edge = p.get("edge_pct")
        cands.append({
            "sport": p["sport"], "game_id": str(p.get("id")),
            "league": p.get("league"),
            "match": p.get("match", ""), "pick": p.get("pick", ""),
            "odds": int(odds), "prob": round(prob, 3),
            "edge": edge, "priced": priced, "event_time": p.get("event_time"),
            "_score": (edge if edge is not None else 0.0) + prob * 10,
        })
    if len(cands) < 2:
        return []

    def pick_legs(n, key, min_odds=None):
        chosen, seen = [], set()
        for c in sorted(cands, key=key, reverse=True):
            if c["game_id"] in seen:
                continue
            if min_odds is not None and c["odds"] < min_odds:
                continue
            chosen.append({k: v for k, v in c.items() if k != "_score"})
            seen.add(c["game_id"])
            if len(chosen) == n:
                break
        return chosen

    def make(legs, name, blurb):
        dec, prob = 1.0, 1.0
        any_model = False
        for L in legs:
            dec *= _amer_to_dec(L["odds"])
            prob *= L["prob"]
            if L["priced"] == "model":
                any_model = True
        n = len(legs)
        return {"name": name, "blurb": blurb, "legs": legs, "leg_count": n,
                "stake_units": STAKE_BY_LEGS.get(n, 0.5),
                "decimal": round(dec, 2), "american": _dec_to_amer(dec),
                "model_prob": round(prob, 3), "payout_10": round(10 * dec, 2),
                "ev_pct": round((prob * dec - 1) * 100, 1),
                "any_model_priced": any_model, "result": "pending", "units_pl": None}

    out = []
    safe = pick_legs(2, lambda c: c["prob"])
    if len(safe) == 2:
        out.append(make(safe, "Safe Two",
                        "The two highest-confidence model picks, in different games."))
    value = pick_legs(3, lambda c: c["_score"])
    if len(value) == 3:
        out.append(make(value, "Value Three",
                        "The three best model-edge plays across the slate."))
    longshot = pick_legs(4, lambda c: c["_score"], min_odds=-160)
    if len(longshot) < 3:
        longshot = pick_legs(4, lambda c: c["_score"])
    if len(longshot) >= 3:
        out.append(make(longshot,
                        "Longshot " + ("Four" if len(longshot) == 4 else "Three"),
                        "A bigger-payout stack of edge plays at longer prices."))
    return out


_parlay_table_ready = False


def _ensure_parlay_table():
    global _parlay_table_ready
    if _parlay_table_ready:
        return
    try:
        from models import ParlaySlip
        with SessionLocal() as db:
            bind = db.get_bind()
        ParlaySlip.__table__.create(bind=bind, checkfirst=True)
        _parlay_table_ready = True
    except Exception as e:
        print(f"[parlays] ensure table failed: {e}")


def _load_slips(target):
    """Locked parlays for a date as full parlay dicts (with result + units), or []."""
    _ensure_parlay_table()
    from models import ParlaySlip
    d0 = dt.datetime.combine(target, dt.time.min)
    d1 = dt.datetime.combine(target, dt.time.max)
    out = []
    try:
        with SessionLocal() as db:
            rows = (db.query(ParlaySlip)
                      .filter(ParlaySlip.slip_date >= d0, ParlaySlip.slip_date <= d1)
                      .order_by(ParlaySlip.id).all())
            for r in rows:
                try:
                    p = json.loads(r.legs_json)
                except Exception:
                    continue
                p["result"] = r.result
                p["units_pl"] = r.units_pl
                p["stake_units"] = r.stake_units
                out.append(p)
    except Exception as e:
        print(f"[parlays] load failed: {e}")
    return out


def _save_slips(target, parlays):
    _ensure_parlay_table()
    from models import ParlaySlip
    d = dt.datetime.combine(target, dt.time.min)
    try:
        with SessionLocal() as db:
            for p in parlays:
                if db.query(ParlaySlip).filter_by(slip_date=d, name=p["name"]).first():
                    continue
                db.add(ParlaySlip(
                    slip_date=d, name=p["name"], leg_count=p["leg_count"],
                    stake_units=p["stake_units"], decimal_odds=p["decimal"],
                    american=p.get("american"), model_prob=p["model_prob"],
                    legs_json=json.dumps(p), result="pending"))
            db.commit()
    except Exception as e:
        print(f"[parlays] save failed: {e}")


def _settle_parlays():
    """Grade pending slips once every leg has a settled game result. Soccer legs
    are graded on demand here (other sports settle via their own boards)."""
    _ensure_parlay_table()
    from models import ParlaySlip, PickResult
    try:
        with SessionLocal() as db:
            pend = db.query(ParlaySlip).filter(ParlaySlip.result == "pending").all()
            pend_data = [(s.slip_date, json.loads(s.legs_json).get("legs", [])) for s in pend]
    except Exception as e:
        print(f"[parlays] settle load failed: {e}")
        return
    if not pend_data:
        return
    # grade any soccer legs that aren't settled yet (targeted, cheap)
    try:
        import soccer_provider
        with SessionLocal() as db:
            have = {str(r.ref) for r in
                    db.query(PickResult).filter(PickResult.sport == "soccer").all()}
            touched = False
            for sdate, legs in pend_data:
                for L in legs:
                    if L.get("sport") != "soccer":
                        continue
                    gid = str(L.get("game_id"))
                    if gid in have:
                        continue
                    try:
                        g = soccer_provider.get_game(sdate.date(), gid, L.get("league") or "epl")
                    except Exception:
                        g = None
                    if g and g.get("status") == "finished" and g.get("winner"):
                        _sp = {"home": g["prob_home"], "draw": (g.get("prob_draw") or 0.0),
                               "away": g["prob_away"]}
                        predicted = max(_sp, key=_sp.get)
                        _record_result(db, "soccer", gid, predicted, g["winner"])
                        have.add(gid)
                        touched = True
            if touched:
                db.commit()
    except Exception as e:
        print(f"[parlays] soccer leg settle failed: {e}")
    # grade any tennis legs not settled yet (mirror the soccer path so parlays
    # don't hang waiting for someone to open the tennis board)
    try:
        with SessionLocal() as db:
            have_t = {str(r.ref) for r in
                      db.query(PickResult).filter(PickResult.sport == "tennis").all()}
            touched = False
            for sdate, legs in pend_data:
                for L in legs:
                    if L.get("sport") != "tennis":
                        continue
                    mid = str(L.get("game_id"))
                    if mid in have_t or not mid.isdigit():
                        continue
                    mt = db.query(Match).filter(Match.id == int(mid)).first()
                    if not mt:
                        continue
                    row = _match_row(db, mt)
                    sc = row.get("score") or {}
                    if (row.get("status") == "finished" and row.get("predicted_winner")
                            and sc.get("winner") in ("a", "b")):
                        _record_result(db, "tennis", row["id"],
                                       row["predicted_winner"], sc["winner"])
                        have_t.add(mid)
                        touched = True
            if touched:
                db.commit()
    except Exception as e:
        print(f"[parlays] tennis leg settle failed: {e}")
    # grade the slips themselves
    try:
        with SessionLocal() as db:
            pend = db.query(ParlaySlip).filter(ParlaySlip.result == "pending").all()
            res = {(r.sport, str(r.ref)): r for r in
                   db.query(PickResult).filter(
                       PickResult.settled_date >= dt.datetime.now() - dt.timedelta(days=60)).all()}
            changed = False
            for slip in pend:
                try:
                    legs = json.loads(slip.legs_json).get("legs", [])
                except Exception:
                    continue
                rows = [res.get((L.get("sport"), str(L.get("game_id")))) for L in legs]
                if not rows or any(r is None for r in rows):
                    continue                       # not fully settled yet
                # soccer draws are pushes: the leg is voided, the slip pays on the
                # remaining legs (draws never sink a parlay).
                live = [(L, r) for L, r in zip(legs, rows) if not _is_soccer_push(r)]
                if not live:                       # every leg pushed -> void slip
                    slip.result = "push"
                    slip.units_pl = 0.0
                    slip.settled_date = dt.datetime.now()
                    changed = True
                    continue
                won = all(bool(r.correct) for _, r in live)
                if won:
                    dec = 1.0
                    for L, _ in live:
                        dec *= (_amer_to_dec(L.get("odds")) or 1.0)
                    slip.units_pl = round(slip.stake_units * (dec - 1), 3)
                    slip.result = "win"
                else:
                    slip.units_pl = -slip.stake_units
                    slip.result = "loss"
                slip.settled_date = dt.datetime.now()
                changed = True
            if changed:
                db.commit()
    except Exception as e:
        print(f"[parlays] settle grade failed: {e}")


@app.get("/api/parlays")
def parlays(date: str | None = None):
    """Locked daily parlays — frozen on first view so they don't drift through
    the day — each with its stake (units), settled result, and unit P&L."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _settle_parlays()
    stored = _load_slips(target)
    if stored:
        return {"parlays": stored, "date": target.isoformat(), "locked": True}
    today = dt.date.today()
    built = _build_parlays(target) if target >= today else []
    if built and target == today:
        _save_slips(target, built)
        return {"parlays": _load_slips(target) or built,
                "date": target.isoformat(), "locked": True}
    return {"parlays": built, "date": target.isoformat(), "locked": False}


@app.get("/api/parlays/record")
def parlays_record(days: int = 30):
    """Rolling W/L and unit P&L across the locked parlays."""
    _settle_parlays()
    _ensure_parlay_table()
    from models import ParlaySlip
    since = dt.datetime.now() - dt.timedelta(days=days)
    wins = losses = pending = pushes = 0
    units_pl = units_staked = 0.0
    try:
        with SessionLocal() as db:
            rows = db.query(ParlaySlip).filter(ParlaySlip.slip_date >= since).all()
        for r in rows:
            if r.result == "win":
                wins += 1
            elif r.result == "loss":
                losses += 1
            elif r.result == "push":
                pushes += 1            # void (a drawn soccer leg): no stake/PL effect
                continue
            else:
                pending += 1
                continue
            units_staked += (r.stake_units or 0)
            units_pl += (r.units_pl or 0)
    except Exception as e:
        print(f"[parlays] record failed: {e}")
    decided = wins + losses
    return {"days": days, "wins": wins, "losses": losses, "pending": pending, "pushes": pushes,
            "win_pct": round(100 * wins / decided, 1) if decided else None,
            "units_pl": round(units_pl, 2), "units_staked": round(units_staked, 2),
            "roi_pct": round(100 * units_pl / units_staked, 1) if units_staked else None}


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
    elif p["sport"] == "nhl":
        s.append(f"The model makes {name} a {pct}% NHL moneyline pick.")
        if ctx.get("fav_record") and ctx.get("dog_record"):
            s.append(f"Records (W-L-OTL): {ctx.get('fav_name','the favorite')} {ctx['fav_record']} "
                     f"versus {ctx.get('dog_name','the opponent')} {ctx['dog_record']}.")
        facts = ctx.get("factors") or []
        xg = next((f for f in facts if "xG model" in f), None)
        if xg:
            tot = ctx.get("avg_total")
            s.append(xg + (f" Projected total about {tot} goals." if tot else "")
                     + " Expected goals come from each side's scoring and goals-against rates, with home ice.")
        else:
            s.append("Team goal stats weren't available here, so this rests on records alone "
                     "— lower confidence.")
        s.append(f"Confidence: {p['confidence']}. Hockey is high-variance, and this model doesn't "
                 f"include the starting goalie or injuries — confirm the projected starter yourself.")
    elif p["sport"] == "soccer" and ctx.get("pick_side") == "draw":
        home = ctx.get("home_name", "the home side")
        away = ctx.get("away_name", "the away side")
        lg = ctx.get("league_label", "this competition")
        ph = round((ctx.get("prob_home") or 0) * 100)
        pa = round((ctx.get("prob_away") or 0) * 100)
        s.append(f"In {lg}, the model's edge is on the DRAW at {pct}% — it reads "
                 f"{home} vs {away} as tight and evenly matched, with {home} at {ph}% "
                 f"and {away} at {pa}%.")
        eh, ea = ctx.get("exp_goals_home"), ctx.get("exp_goals_away")
        if eh is not None and ea is not None:
            s.append(f"Projected goals are close ({eh} vs {ea}) — the profile that most "
                     f"often finishes level.")
        hr, ar = ctx.get("home_record"), ctx.get("away_record")
        if hr or ar:
            s.append(f"Form and standing: {home} {hr or 'n/a'}, {away} {ar or 'n/a'}.")
        if ctx.get("status") == "live":
            sh, sa, mn = ctx.get("score_home"), ctx.get("score_away"), ctx.get("minute")
            s.append(f"Live at {mn}': {away} {sa}–{sh} {home}; the model still leans level.")
        s.append(f"Confidence: {p['confidence']}. The draw is a genuine 3-way market play, "
                 f"usually at a longer price than either side.")
    elif p["sport"] == "soccer":
        is_home = ctx.get("pick_side") == "home"
        home = ctx.get("home_name", "the home side")
        away = ctx.get("away_name", "the away side")
        opp = ctx.get("opponent", "the opponent")
        lg = ctx.get("league_label", "this competition")
        pdw = round((ctx.get("prob_draw") or 0) * 100)
        opp_pct = round(((ctx.get("prob_away") if is_home else ctx.get("prob_home")) or 0) * 100)
        where = "at home" if is_home else "on the road"
        s.append(f"In {lg}, the model makes {name} a {pct}% pick to win {where}, "
                 f"with the draw at {pdw}% and {opp} at {opp_pct}%.")
        eh, ea = ctx.get("exp_goals_home"), ctx.get("exp_goals_away")
        if eh is not None and ea is not None:
            mine, theirs = (eh, ea) if is_home else (ea, eh)
            tail = (" plus a quantified home-field bump." if is_home
                    else f" — and {name} keep the expected-goals edge even away from home.")
            s.append(f"The goals model projects about {mine} for {name} versus {theirs} for "
                     f"{opp}, from each side's scoring and conceding rates{tail}")
        if is_home:
            s.append(f"Home advantage is a real, measured edge here — familiar pitch, the "
                     f"crowd, and no travel — stacked on top of {name}'s underlying form.")
        else:
            s.append(f"Being favored on the road means {name} grade out clearly stronger than "
                     f"{home}; the model only tilts away when that gap is large enough to beat home edge.")
        hr, ar = ctx.get("home_record"), ctx.get("away_record")
        if hr or ar:
            s.append(f"Form and standing: {home} {hr or 'n/a'}, {away} {ar or 'n/a'}.")
        if ctx.get("status") == "live":
            sh, sa, mn = ctx.get("score_home"), ctx.get("score_away"), ctx.get("minute")
            s.append(f"Live at {mn}': {away} {sa}–{sh} {home}. The win probability already "
                     f"reflects the current scoreline and the time left to play.")
        s.append(f"Confidence: {p['confidence']}. Soccer carries genuine draw risk, so this is a "
                 f"moneyline lean rather than a lock — the three-way split shows how much the "
                 f"draw absorbs.")
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
    """The FOUR best plays of the day, LOCKED once chosen so the same picks stay
    listed all day (each showing its win/loss as games finish) and the record
    matches exactly what's shown — they no longer drop off when a game ends."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_day(target)
    from models import LockedPickSet
    d0 = dt.datetime.combine(target, dt.time.min)

    locked = None
    try:
        with SessionLocal() as db:
            row = db.query(LockedPickSet).filter_by(view="free", pick_date=d0).first()
            if row:
                locked = json.loads(row.payload)
    except Exception as e:
        print(f"[free] lock load failed: {e}")

    if locked is None:
        plays = _gather_plays(target)
        ranked = sorted(plays, key=lambda p: -(p.get("score_key", p["prob"])))
        strong = ranked[:4]
        import narrate
        budget = {"left": int(os.environ.get("AI_MAX_PER_REQUEST", "10"))}
        out = []
        for p in strong:
            p["reason"] = narrate.prose(_long_reason(p), kind="reason",
                                        sport=p["sport"], llm=LLM_COMPLETE, budget=budget)
            _enrich_odds(p)
            p["pick_side"] = (p.get("ctx") or {}).get("pick_side")   # keep for settling
            p.pop("score_key", None)
            p.pop("ctx", None)
            out.append(p)
        if out:                                   # lock the set for the day (once)
            try:
                with SessionLocal() as db:
                    if not db.query(LockedPickSet).filter_by(view="free", pick_date=d0).first():
                        db.add(LockedPickSet(view="free", pick_date=d0,
                                             payload=json.dumps(out, default=str)))
                        db.commit()
            except Exception as e:
                print(f"[free] lock save failed: {e}")
            _log_shown_picks("free", target, out)
        locked = out

    # each load: settle any finished locked games + refresh win/loss badges
    for p in locked:
        _settle_locked_pick(p, target)
        p["result"] = _pick_result_status(p["sport"], str(p["id"]))
    return {"date": target.isoformat(), "picks": locked, "locked": True}


def _settle_locked_pick(p, target):
    """Best-effort settle of a locked pick's game so its W/L shows even if that
    sport's board was never opened. No-op once a result already exists."""
    sport, ref = p.get("sport"), str(p.get("id"))
    side = p.get("pick_side")
    if not sport or not side or sport == "tennis":
        return
    from models import PickResult
    try:
        with SessionLocal() as db:
            if db.query(PickResult).filter_by(sport=sport, ref=ref).first():
                return
    except Exception:
        return
    g = None
    try:
        if sport == "soccer":
            import soccer_provider
            g = soccer_provider.get_game(target, ref, p.get("league") or "epl")
        elif sport == "ufc":
            import ufc_provider
            g = ufc_provider.get_game(target, ref)
        elif sport == "mlb":
            import mlb_provider
            g = mlb_provider.get_game(target, int(ref))
        else:
            sp = sports.get(sport)
            if sp and sp.game:
                g = sp.game(target, ref)
    except Exception:
        g = None
    if not g or g.get("status") != "finished" or not g.get("winner"):
        return
    try:
        with SessionLocal() as db:
            _record_result(db, sport, ref, side, g["winner"])
            db.commit()
    except Exception as e:
        print(f"[free] settle {sport}:{ref} failed: {e}")


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
    import premium, narrate
    slate = [dict(p) for p in plays]        # stable snapshot for slate ranking
    budget = {"left": int(os.environ.get("AI_MAX_PER_REQUEST", "10"))}
    out = []
    for p in plays:
        if sport and p["sport"] != sport:
            continue
        if p["prob"] < min_prob:
            continue
        p["reason"] = narrate.prose(_long_reason(p), kind="reason",
                                    sport=p["sport"], llm=LLM_COMPLETE, budget=budget)
        _enrich_odds(p)
        # premium "why it's a best bet" layer (paywall-ready): standout vs the
        # day's board, model-derived stake sizing, and the model's track record.
        pf = premium.premium_facts(p, slate, SessionLocal)
        pf["text"] = narrate.prose(pf["text"], kind="premium",
                                   sport=p["sport"], llm=LLM_COMPLETE, budget=budget)
        p["premium"] = pf
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


def _nhl_writeup(g):
    """Hockey analysis writeup, honest about scope (no goalie/injury data)."""
    fav = g["home"] if g["prob_home"] >= 0.5 else g["away"]
    dog = g["away"] if g["prob_home"] >= 0.5 else g["home"]
    favp = round((g["prob_home"] if g["prob_home"] >= 0.5 else 1 - g["prob_home"]) * 100)
    home_side = "at home" if g["prob_home"] >= 0.5 else "on the road"
    factors = g.get("factors") or []
    total = g.get("avg_total")
    paras = []
    p1 = [f"The model makes {fav['name']} the pick at {favp}% to win, playing {home_side}."]
    if fav["record"] and dog["record"]:
        p1.append(f"Records (W-L-OTL): {fav['name']} {fav['record']} versus {dog['name']} {dog['record']}.")
    paras.append(" ".join(p1))

    xg_fact = next((f for f in factors if "xG model" in f), None)
    if xg_fact:
        paras.append("This uses a Poisson goals model: each side's expected goals come from their "
                     "scoring and goals-against rates against the opponent, with home ice. "
                     + xg_fact + (f" Projected total: about {total} goals." if total else ""))
    else:
        paras.append("Note: team goal stats weren't available for this matchup, so the projection "
                     "rests on records alone \u2014 treat the edge as lower-confidence.")

    paras.append("Hockey is high-variance \u2014 a hot goalie or an OT bounce swings games \u2014 so weigh the "
                 "edge accordingly. The model uses team goals-for/against and home ice; it does not "
                 "yet account for the starting goalie, injuries, or rest, so confirm the projected "
                 f"starter yourself. Model confidence: {g['confidence']}.")
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


# Active months per sport (1-12). Out-of-season sports are hidden from the home
# grid + menu and reappear when their season window opens (windows are generous
# on the front edge so a sport returns ahead of its first games).
SPORT_SEASON = {
    "tennis": set(range(1, 13)),
    "soccer": set(range(1, 13)),
    "mlb":    {2, 3, 4, 5, 6, 7, 8, 9, 10, 11},
    "nba":    {10, 11, 12, 1, 2, 3, 4, 5, 6},
    "nhl":    {9, 10, 11, 12, 1, 2, 3, 4, 5, 6},
    "ncaabb": {2, 3, 4, 5, 6},
    "nfl":    {8, 9, 10, 11, 12, 1, 2},
    "ncaaf":  {8, 9, 10, 11, 12, 1},
    "ncaab":  {11, 12, 1, 2, 3, 4},
    "wncaab": {11, 12, 1, 2, 3, 4},
    "ufc":    set(range(1, 13)),
}


@app.get("/api/sports")
def sports_meta():
    """Registry metadata so the frontend builds its tabs/tiles/labels/colors
    dynamically. Adding a sport in sports.py surfaces it here automatically.
    Each entry carries an `active` flag (in-season) so the UI can hide
    out-of-season sports."""
    meta = sports.public_meta()
    mo = dt.date.today().month
    for entry in meta:
        entry["active"] = mo in SPORT_SEASON.get(entry["key"], set(range(1, 13)))
    return meta


# ----------------------------- SOCCER (multi-league) -----------------------
# Defined BEFORE the generic /api/{sport}/... routes so these literal paths win
# route matching and the league query param is honored.
@app.get("/api/soccer/leagues")
def soccer_leagues():
    import soccer_provider
    return {"leagues": soccer_provider.leagues()}


def _settle_soccer(games):
    """Grade finished soccer matches into PickResult (home/away/draw). A draw
    means a moneyline 'to win' pick was wrong."""
    try:
        with SessionLocal() as db:
            for g in games:
                if g.get("status") != "finished" or not g.get("winner"):
                    continue
                _sp = {"home": g["prob_home"], "draw": (g.get("prob_draw") or 0.0),
                       "away": g["prob_away"]}
                predicted = max(_sp, key=_sp.get)
                _record_result(db, "soccer", g["id"], predicted, g["winner"])
            db.commit()
    except Exception as e:
        print(f"[soccer] settle failed: {e}")


@app.get("/api/soccer/games")
def soccer_games(date: str | None = None, league: str | None = None):
    import soccer_provider
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    lg = league or "all"
    if lg in ("all", "today"):
        games = soccer_provider.get_today(target)
    else:
        games = soccer_provider.get_games(target, lg)
    _settle_soccer(games)
    return {"games": games, "league": ("all" if lg in ("all", "today") else lg),
            "leagues": soccer_provider.leagues()}


def _settle_ufc(games):
    """Grade finished bouts into PickResult (predicted = higher win prob)."""
    try:
        with SessionLocal() as db:
            for g in games:
                if g.get("status") != "finished" or not g.get("winner"):
                    continue
                predicted = "home" if g["prob_home"] >= g["prob_away"] else "away"
                _record_result(db, "ufc", g["id"], predicted, g["winner"])
            db.commit()
    except Exception as e:
        print(f"[ufc] settle failed: {e}")


@app.get("/api/ufc/games")
def ufc_games(date: str | None = None):
    import ufc_provider
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    games = ufc_provider.get_games(target)
    _attach_odds("ufc", games)
    _settle_ufc(games)
    label = games[0]["event_label"] if games else "UFC"
    return {"games": games, "event": label}


@app.get("/api/ufc/game/{game_id}")
def ufc_game(game_id: str, date: str | None = None):
    import ufc_provider
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    g = ufc_provider.get_game(target, game_id)
    if g:
        _attach_odds_one("ufc", g)
        try:
            tale = ufc_provider.fighter_tale(target, g)   # API-Sports (lazy, cached)
            if tale:
                g["tale"] = tale
        except Exception as e:
            print(f"[ufc] tale failed: {e}")
    return g or {"error": "not found"}


@app.get("/api/mma/raw/{endpoint}")
def _mma_raw(endpoint: str, date: str | None = None, id: str | None = None,
             fight: str | None = None):
    """Inspect raw API-Sports MMA responses (for locking field mappings):
      /api/mma/raw/fights?date=YYYY-MM-DD
      /api/mma/raw/fighters?id=<fighterId>
      /api/mma/raw/statistics?fight=<fightId>
    """
    import apisports_mma
    if endpoint == "fights":
        params = {"date": date or dt.date.today().isoformat()}
        body = apisports_mma.raw_get("/fights", params)
    elif endpoint == "fighters":
        body = apisports_mma.raw_get("/fighters", {"id": id} if id else {})
    elif endpoint == "statistics":
        body = apisports_mma.raw_get("/fights/statistics/fighters",
                                     {"fight": fight} if fight else {})
    else:
        body = {"error": "endpoint must be: fights | fighters | statistics"}
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@app.get("/api/mma/diag")
def _mma_diag():
    try:
        import apisports_mma
        return JSONResponse(apisports_mma.diag(), headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/soccer/game/{game_id}")
def soccer_game(game_id: str, date: str | None = None, league: str | None = None):
    import soccer_provider
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    lg = league or soccer_provider.DEFAULT_LEAGUE
    g = soccer_provider.get_game(target, game_id, lg)
    if g:
        try:
            import soccer_stats
            d = soccer_stats.match_depth(lg, g["home"]["name"], g["away"]["name"])
            if d:
                g["depth"] = d
        except Exception as e:
            print(f"[soccer] depth failed: {e}")
    return g or {"error": "not found"}


@app.get("/api/soccer/stats/diag")
def _soccer_stats_diag(league: str | None = None):
    try:
        import soccer_stats
        lg = league or "epl"
        t = soccer_stats.get_table(lg)
        return JSONResponse({"enabled": soccer_stats.enabled(), "league": lg,
                             "teams": len(t), "sample": list(t.keys())[:6]},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/soccer/props/{game_id}")
def soccer_props(game_id: str, date: str | None = None,
                 league: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    lg = league or soccer_provider.DEFAULT_LEAGUE
    try:
        g = soccer_provider.get_game(target, game_id, lg)
        if g:
            b = _book_props("soccer", g)
            if b:
                return _enrich_props("soccer", game_id, target,
                                     {"game_id": game_id, "props": b,
                                      "source": "book"}, g.get("status"), lg)
    except Exception as e:
        print(f"[soccer] props failed: {e}")
    return {"props": []}


@app.get("/api/soccer/boxscore/{game_id}")
def soccer_boxscore(game_id: str, date: str | None = None, league: str | None = None):
    import soccer_provider
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    lg = league or soccer_provider.DEFAULT_LEAGUE
    return soccer_provider.get_boxscore(target, game_id, lg)


@app.get("/api/{sport}/games")
def team_games(sport: str, date: str | None = None, debug: int = 0):
    # College baseball has its own dedicated handler (Highlightly + ESPN). This
    # generic route is registered BEFORE /api/ncaabb/games, so without this
    # delegation it would shadow it and return [] for every college game. That
    # shadowing was the root cause of college baseball never displaying.
    if sport == "ncaabb":
        return ncaabb_games(date=date, debug=debug)
    if sport == "nhl":
        return nhl_slate(date=date, debug=debug)
    s = sports.get(sport)
    if not s or s.kind != "espn":      # only nba/nfl are served here
        return []
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        games = s.games(target)
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
    try:
        import game_store
        game_store.save_games(sport, target, games)
    except Exception:
        pass
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
    try:
        import game_store
        game_store.save_games("ncaabb", target, games)
    except Exception:
        pass
    _NOCACHE = {"Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache", "Expires": "0"}
    if debug:
        return JSONResponse({"diag": diag, "count": len(games), "games": games},
                            headers=_NOCACHE)
    return JSONResponse(games, headers=_NOCACHE)


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
    g = _attach_odds_one("ncaabb", g)
    return g


@app.get("/api/nhl/games")
def nhl_slate(date: str | None = None, debug: int = 0):
    """NHL games for a date: ESPN scoreboard backbone + the Poisson xG model."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    games = []
    try:
        from nhl_games import get_games as nhl_get
        games = nhl_get(target) or []
    except Exception as e:
        print(f"[nhl] games failed: {e}")
    # settle finished games for accuracy
    try:
        with SessionLocal() as db:
            wrote = False
            for g in games:
                if g.get("status") == "finished" and g.get("winner") in ("home", "away"):
                    predicted = "home" if g["prob_home"] >= 0.5 else "away"
                    _record_result(db, "nhl", g["id"], predicted, g["winner"])
                    wrote = True
            if wrote:
                db.commit()
    except Exception as e:
        print(f"[accuracy] nhl log skipped: {e}")
    try:
        import game_store
        game_store.save_games("nhl", target, games)
    except Exception:
        pass
    _NOCACHE = {"Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache", "Expires": "0"}
    if debug:
        return JSONResponse({"count": len(games), "games": games}, headers=_NOCACHE)
    return JSONResponse(games, headers=_NOCACHE)


@app.get("/api/nhl/game/{game_id}")
def nhl_game(game_id: str, date: str | None = None):
    """One NHL game with analysis writeup."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from nhl_games import get_games
        games = get_games(target)
    except Exception:
        games = []
    g = next((x for x in games if str(x["id"]) == str(game_id)), None)
    if not g:
        return {"error": "not found"}
    g = dict(g)
    g["analysis"] = _nhl_writeup(g)
    g = _attach_odds_one("nhl", g)
    return g


@app.get("/api/{sport}/boxscore/{game_id}")
def game_boxscore(sport: str, game_id: str, date: str | None = None):
    """Live player box score for one game (powers the per-game Live Stats tab)."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        if sport == "ncaabb":
            from ncaab_baseball import get_boxscore
            return get_boxscore(target, game_id)
        if sport == "mlb":
            from mlb_provider import get_boxscore
            return get_boxscore(target, int(game_id))
        from espn_provider import get_boxscore
        return get_boxscore(sport, target, game_id)
    except Exception as e:
        print(f"[{sport}] boxscore failed: {e}")
        return {"teams": []}


@app.get("/api/{sport}/game/{game_id}")
def team_game(sport: str, game_id: str, date: str | None = None):
    if sport == "ncaabb":
        return ncaabb_game(game_id=game_id, date=date)
    if sport == "nhl":
        return nhl_game(game_id=game_id, date=date)
    s = sports.get(sport)
    if not s or s.kind != "espn":
        return {"error": "bad sport"}
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        g = s.game(target, game_id)
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
    g = _attach_odds_one(sport, g)
    return g


def _book_props(sport, g):
    """Resolve real sportsbook player props: SportsGameOdds (free tier includes
    props) first, then The Odds API. Returns a props list or None. Soccer
    passes its per-match league (one of 15) to SportsGameOdds."""
    home, away = g["home"]["name"], g["away"]["name"]
    sgo_league = None
    if sport == "soccer":
        try:
            import sgo_api
            sgo_league = sgo_api.SGO_SOCCER.get(g.get("league"))
        except Exception:
            sgo_league = None
        if not sgo_league:
            return None
    try:
        import sgo_api
        if sgo_api.enabled():
            b = sgo_api.get_player_props(sport, home, away, league=sgo_league)
            if sport == "mlb" and b:
                b = [p for p in b
                     if (p.get("stat") or "").strip().lower() != "points"]
            if b:
                return b
    except Exception as e:
        print(f"[{sport}] sgo props failed: {e}")
    if sport == "soccer":
        return None                       # no Odds-API soccer player props
    try:
        import odds_api
        b = odds_api.get_player_props(sport, home, away)
        if b:
            return b
    except Exception as e:
        print(f"[{sport}] odds-api props failed: {e}")
    return None


# ===== NEW PROP FORMAT: actual / hit grading from the box score =====
# Map a prop's stat -> (box-score group hint, [candidate column labels]).
# group hint None = search every group. Column match is case-insensitive and
# prefix-tolerant; multi-column entries are summed (combo props like PRA).
_BOX_COLS = {
    "mlb": {
        # SportsGameOdds labels (lowercased) -> (box group hint, [columns])
        "batting basesonballs": ("batting", [["BB"]]),
        "batting doubles": ("batting", [["2B"]]),
        "batting hits": ("batting", [["H"]]),
        "batting homeruns": ("batting", [["HR"]]),
        "batting rbi": ("batting", [["RBI"]]),
        "batting stolenbases": ("batting", [["SB"]]),
        "batting strikeouts": ("batting", [["K"]]),
        "batting totalbases": ("batting", [["TB"]]),
        "batting triples": ("batting", [["3B"]]),
        "batting hits+runs+rbi": ("batting", [["H"], ["R"], ["RBI"]]),
        "pitching basesonballs": ("pitching", [["BB"]]),
        "pitching earnedruns": ("pitching", [["ER"]]),
        "pitching hits": ("pitching", [["H"]]),
        "pitching outs": ("pitching", [["OUT", "IP"]]),
        "pitching strikeouts": ("pitching", [["K", "SO"]]),
        # legacy / canonical aliases
        "strikeouts": ("pitching", [["K", "SO"]]),
        "hits": ("batting", [["H"]]),
        "total bases": ("batting", [["TB"]]),
        "home runs": ("batting", [["HR"]]),
        "rbis": ("batting", [["RBI"]]),
        "runs": ("batting", [["R"]]),
        "stolen bases": ("batting", [["SB"]]),
        "walks": ("pitching", [["BB"]]),
    },
    "nba": {
        "points": (None, [["PTS"]]),
        "rebounds": (None, [["REB"]]),
        "assists": (None, [["AST"]]),
        "threes": (None, [["3PT"]]),
        "3-pt made": (None, [["3PT"]]),
        "three pointers": (None, [["3PT"]]),
        "steals": (None, [["STL"]]),
        "blocks": (None, [["BLK"]]),
        "turnovers": (None, [["TO"]]),
        "points + rebounds + assists": (None, [["PTS"], ["REB"], ["AST"]]),
        "pts + reb + ast": (None, [["PTS"], ["REB"], ["AST"]]),
        "points + rebounds": (None, [["PTS"], ["REB"]]),
        "points + assists": (None, [["PTS"], ["AST"]]),
        "rebounds + assists": (None, [["REB"], ["AST"]]),
        "threepointersmade": (None, [["3PT"]]),
        "points+assists": (None, [["PTS"], ["AST"]]),
        "points+rebounds": (None, [["PTS"], ["REB"]]),
        "rebounds+assists": (None, [["REB"], ["AST"]]),
        "blocks+steals": (None, [["BLK"], ["STL"]]),
        "points+rebounds+assists": (None, [["PTS"], ["REB"], ["AST"]]),
    },
    "nfl": {
        "passing yards": ("passing", [["YDS"]]),
        "pass yards": ("passing", [["YDS"]]),
        "passing touchdowns": ("passing", [["TD"]]),
        "passing tds": ("passing", [["TD"]]),
        "interceptions": ("passing", [["INT"]]),
        "completions": ("passing", [["C/ATT", "COMP"]]),
        "rushing yards": ("rushing", [["YDS"]]),
        "rush yards": ("rushing", [["YDS"]]),
        "rushing attempts": ("rushing", [["CAR", "ATT"]]),
        "carries": ("rushing", [["CAR", "ATT"]]),
        "receiving yards": ("receiving", [["YDS"]]),
        "rec yards": ("receiving", [["YDS"]]),
        "receptions": ("receiving", [["REC"]]),
    },
    "soccer": {
        "shots": (None, [["totalshots", "shots"]]),
        "total shots": (None, [["totalshots", "shots"]]),
        "shots on target": (None, [["shotsontarget", "shotsongoal"]]),
        "shots on goal": (None, [["shotsontarget", "shotsongoal"]]),
        "goals": (None, [["totalgoals", "goals"]]),
        "assists": (None, [["goalassists", "assists"]]),
        "goals + assists": (None, [["totalgoals", "goals"], ["goalassists", "assists"]]),
        "saves": (None, [["saves"]]),
        "passes": (None, [["totalpasses", "passes"]]),
        "tackles": (None, [["totaltackles", "tackles"]]),
        "fouls": (None, [["foulscommitted", "fouls"]]),
        "tackles won": (None, [["effectivetackles", "totaltackles"]]),
    },
}


def _box_num(raw):
    """Parse a box-score cell to a number. 'made-att' / 'comp/att' -> first part."""
    if raw is None:
        raise ValueError("none")
    s = str(raw).strip()
    if not s or s in ("-", "--"):
        raise ValueError("blank")
    for sep in ("-", "/"):
        if sep in s:
            s = s.split(sep)[0]
    return float(s)


def _norm_name(s):
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", s.lower()).strip()


def _name_match(box_name, prop_name):
    a, b = _norm_name(box_name).split(), _norm_name(prop_name).split()
    if not a or not b:
        return False
    if " ".join(a) == " ".join(b):
        return True
    return a[-1] == b[-1] and a[0][:1] == b[0][:1]   # last name + first initial


def _actual_from_box(box, player, stat, sport):
    """Best-effort: a player's actual value for a stat from a parsed box score."""
    if not box or not box.get("teams"):
        return None
    spec = (_BOX_COLS.get(sport) or {}).get((stat or "").lower())
    if not spec:
        return None
    grp_hint, col_names = spec
    for team in box["teams"]:
        for grp in team.get("groups", []):
            if grp_hint and grp_hint not in (grp.get("title") or "").lower():
                continue
            cols = [str(c).lower() for c in (grp.get("columns") or [])]
            idxs = []
            for aliases in col_names:          # each term: match ANY alias, then SUM terms
                ci = None
                for cn in aliases:
                    cn = cn.lower()
                    ci = next((i for i, c in enumerate(cols)
                               if c == cn or c.startswith(cn)), None)
                    if ci is not None:
                        break
                if ci is None:
                    idxs = None
                    break
                idxs.append(ci)
            if not idxs:
                continue
            for row in grp.get("rows", []):
                if not _name_match(row.get("name", ""), player or ""):
                    continue
                stats = row.get("stats") or []
                try:
                    return round(sum(_box_num(stats[i]) for i in idxs), 1)
                except (IndexError, ValueError, TypeError):
                    return None
    return None


def _grade_prop(p):
    """Set hit (over/under/push) vs line and whether the model's lean was right."""
    line, actual = p.get("line"), p.get("actual")
    if line is None or actual is None:
        return
    try:
        line, actual = float(line), float(actual)
    except (ValueError, TypeError):
        return
    p["hit"] = "push" if actual == line else ("over" if actual > line else "under")
    lean = p.get("lean")
    if p["hit"] == "push":
        p["model_correct"] = None
    elif lean in ("over", "under"):
        p["model_correct"] = (lean == p["hit"])


_proj_cache = {}          # (sport, player, stat) -> (ts, projection)
_PROJ_TTL = 3 * 3600


def _project_from_games(games):
    """Recency-weighted projection from a last-N game log. Sorts by date
    (most recent first) when dates are usable; linear weights give the newest
    game ~N x the oldest. Falls back to a flat average when order is unknown."""
    vals = [g.get("value") for g in (games or [])
            if isinstance(g.get("value"), (int, float))]
    if not vals:
        return None
    dated = [(g.get("date") or "", g.get("value")) for g in games
             if isinstance(g.get("value"), (int, float))]
    if all(d for d, _ in dated) and len({d for d, _ in dated}) > 1:
        try:
            dated.sort(key=lambda x: x[0], reverse=True)   # most recent first
            ordered = [v for _, v in dated]
            n = len(ordered)
            acc = wsum = 0.0
            for i, v in enumerate(ordered):
                w = n - i
                acc += w * v
                wsum += w
            if wsum:
                return round(acc / wsum, 1)
        except Exception:
            pass
    return round(sum(vals) / len(vals), 1)


def _prop_log(sport, game_id, date, player, stat, line, league=None):
    try:
        if sport == "soccer":
            import soccer_provider
            return soccer_provider.get_prop_history(
                date, game_id, player, stat, line,
                league or soccer_provider.DEFAULT_LEAGUE)
        if sport == "mlb":
            from mlb_provider import get_prop_history
            return get_prop_history(date, int(game_id), player, stat, line)
        if sport in ("nba", "nfl"):
            from espn_provider import get_prop_history
            return get_prop_history(sport, date, game_id, player, stat, line)
    except Exception:
        return None
    return None


def _projection_for(sport, game_id, date, player, stat, line, league=None):
    key = (sport, (player or "").lower(), (stat or "").lower())
    c = _proj_cache.get(key)
    if c and time.time() - c[0] < _PROJ_TTL:
        return c[1]
    h = _prop_log(sport, game_id, date, player, stat, line, league)
    proj = _project_from_games((h or {}).get("games") or []) if h else None
    _proj_cache[key] = (time.time(), proj)
    return proj


_STAT_SYN = {
    "ks": "strikeouts", "k": "strikeouts", "so": "strikeouts",
    "pitcher strikeouts": "strikeouts", "strikeout": "strikeouts",
    "pts": "points", "reb": "rebounds", "rebound": "rebounds",
    "ast": "assists", "assist": "assists", "rbis": "rbi",
    "hr": "home runs", "homerun": "home runs", "homeruns": "home runs",
    "tb": "total bases", "rush yds": "rushing yards", "rec yds": "receiving yards",
    "pass yds": "passing yards", "3 pointers": "threes", "3pt made": "threes",
    "three pointers made": "threes", "passing tds": "passing touchdowns",
    "3 pt made": "threes", "3 pointers": "threes", "3pt made": "threes",
    "3s": "threes", "3 pointers made": "threes", "3 point made": "threes",
}


def _norm_player(s):
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _norm_stat(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9+ ]", " ", s)
    s = re.sub(r"\b(player|total|o u|over under|prop|alt)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _STAT_SYN.get(s, s)


def _model_projection_map(sport, game_id, date):
    """{(player_norm, stat_norm): projection} from the model props, used to
    backfill book props that have no game-log projection (restores the
    projection that used to show before book lines became the source)."""
    try:
        if sport == "mlb":
            from mlb_provider import get_props
            mp = get_props(date, int(game_id))
        elif sport in ("nba", "nfl", "ncaaf", "ncaab", "wncaab"):
            from espn_provider import get_props
            mp = get_props(sport, date, game_id)
        else:
            return {}
    except Exception as e:
        print(f"[{sport}] model projection map failed: {e}")
        return {}
    out = {}
    for p in (mp or {}).get("props", []):
        proj = p.get("projection")
        if proj is None:
            continue
        out[(_norm_player(p.get("player")),
             _norm_stat(p.get("label") or p.get("stat")))] = proj
    return out


def _enrich_projections(sport, game_id, date, props, league=None):
    """Best-effort, time-boxed parallel fill of `projection` for props that
    lack one (book lines). Cached per player+stat; whatever doesn't finish in
    the budget is filled lazily on the client instead."""
    if sport not in ("mlb", "nba", "nfl", "soccer"):
        return
    need = [p for p in props if p.get("projection") is None][:30]
    if not need:
        return
    ex = None
    try:
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        futs = {ex.submit(_projection_for, sport, game_id, date,
                          p.get("player"), p.get("stat") or p.get("label"),
                          p.get("line"), league): p for p in need}
        deadline = time.time() + 7.0
        for fut in concurrent.futures.as_completed(
                futs, timeout=max(0.1, deadline - time.time())):
            p = futs[fut]
            try:
                pr = fut.result()
                if pr is not None:
                    p["projection"] = pr
            except Exception:
                pass
            if time.time() >= deadline:
                break
    except Exception:
        pass
    finally:
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
    # backfill any still-missing projections from the model's own numbers
    missing = [p for p in props if p.get("projection") is None]
    if missing and sport in ("mlb", "nba", "nfl"):
        mm = _model_projection_map(sport, game_id, date)
        if mm:
            for p in missing:
                k = (_norm_player(p.get("player")),
                     _norm_stat(p.get("stat") or p.get("label")))
                if k in mm:
                    p["projection"] = mm[k]


def _props_boxscore(sport, game_id, date, league=None):
    if sport == "soccer":
        import soccer_provider
        return soccer_provider.get_player_boxscore(
            date, game_id, league or soccer_provider.DEFAULT_LEAGUE)
    if sport == "mlb":
        from mlb_provider import get_boxscore
        return get_boxscore(date, int(game_id))
    if sport == "ncaabb":
        from ncaab_baseball import get_boxscore
        return get_boxscore(date, game_id)
    from espn_provider import get_boxscore
    return get_boxscore(sport, date, game_id)


_LIVE_OR_FINAL = ("live", "final", "finished", "in", "post",
                  "in_progress", "completed", "closed")


def _enrich_props(sport, game_id, date, result, status, league=None):
    """Grade each prop's actual vs the line from the box score (cheap: one
    fetch). Pre-game props simply keep actual=None. Projection is passed
    through if the model already set it; book props get theirs lazily on the
    client via prop-history."""
    props = (result or {}).get("props") or []
    if not props:
        return result
    box = None
    if sport in ("mlb", "nba", "nfl", "soccer") and status and str(status).lower() in _LIVE_OR_FINAL:
        try:
            box = _props_boxscore(sport, game_id, date, league)
        except Exception as e:
            print(f"[{sport}] props boxscore failed: {e}")
            box = None
    _enrich_projections(sport, game_id, date, props, league)
    for p in props:
        if box is not None and p.get("actual") is None:
            try:
                a = _actual_from_box(box, p.get("player"),
                                     p.get("stat") or p.get("label"), sport)
                if a is not None:
                    p["actual"] = a
            except Exception:
                pass
        _grade_prop(p)
    result["graded"] = (box is not None)
    return result


@app.get("/api/mlb/props/{game_id}")
def mlb_props(game_id: int, date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    status = None
    # 1) real sportsbook lines: SportsGameOdds (free props) then The Odds API
    try:
        from mlb_provider import get_game
        g = get_game(target, game_id)
        if g:
            status = g.get("status")
            b = _book_props("mlb", g)
            if b:
                return _enrich_props("mlb", game_id, target,
                                     {"game_id": game_id, "props": b,
                                      "source": "book"}, status)
    except Exception as e:
        print(f"[mlb] book props failed: {e}")
    # 2) fall back to model projections
    try:
        from mlb_provider import get_props
        return _enrich_props("mlb", game_id, target,
                             get_props(target, game_id), status)
    except Exception as e:
        print(f"[mlb] props failed: {e}")
        return {"props": []}


@app.get("/api/{sport}/props/{game_id}")
def team_props(sport: str, game_id: str, date: str | None = None):
    if sport not in ("nba", "nfl"):
        return {"props": []}
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    status = None
    # 1) real sportsbook lines: SportsGameOdds (free props) then The Odds API
    try:
        from espn_provider import get_game
        g = get_game(sport, target, game_id)
        if g:
            status = g.get("status")
            b = _book_props(sport, g)
            if b:
                return _enrich_props(sport, game_id, target,
                                     {"game_id": game_id, "props": b,
                                      "source": "book"}, status)
    except Exception as e:
        print(f"[{sport}] book props failed: {e}")
    # 2) fall back to model projections
    try:
        from espn_provider import get_props
        return _enrich_props(sport, game_id, target,
                             get_props(sport, target, game_id), status)
    except Exception as e:
        print(f"[{sport}] props failed: {e}")
        return {"props": []}


@app.get("/api/soccer/prop-history/{game_id}")
def soccer_prop_history(game_id: str, player: str, stat: str, line: float,
                        date: str | None = None, league: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        import soccer_provider
        return soccer_provider.get_prop_history(
            target, game_id, player, stat, line,
            league or soccer_provider.DEFAULT_LEAGUE)
    except Exception as e:
        print(f"[soccer] prop-history failed: {e}")
        return {"games": []}


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


@app.get("/api/clv")
def clv_report(days: int = 30):
    """Per-sport Closing Line Value over the last N days, from settled picks that
    captured both a taken and a closing line. CLV only -- no ROI/units."""
    from models import PickResult
    from clv import summarize
    import odds_api
    since = dt.datetime.now() - dt.timedelta(days=days)
    persport, allbets = {}, []
    with SessionLocal() as db:
        rows = db.query(PickResult).filter(
            PickResult.settled_date >= since,
            PickResult.taken_odds.isnot(None)).all()
    for r in rows:
        bet = {"odds": r.taken_odds, "won": bool(r.correct), "close_odds": r.close_odds}
        persport.setdefault(r.sport, []).append(bet)
        allbets.append(bet)
    def _clv_only(bets):
        s = summarize(bets)
        return {"avg_clv": s["avg_clv"], "beat_close_pct": s["beat_close_pct"],
                "clv_sample": s["clv_sample"], "beat_close": s["beat_close"]}
    return {"days": days,
            "by_sport": {sp: _clv_only(b) for sp, b in persport.items()},
            "overall": _clv_only(allbets),
            "odds_enabled": odds_api.enabled()}


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
            if _is_soccer_push(r):
                continue                       # draw = push, excluded from record
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
    except Exception:
        # ANY disconnect/error path must clean up, or dead sockets pile up in the
        # broadcast list and the live engine wastes the single CPU pinging them.
        pass
    finally:
        await manager.disconnect(ws)


@app.get("/api/cbb_v53")
def cbb_v53(date: str | None = None):
    """Brand-new endpoint name (never existed before) to bypass any possibility
    of a stale route. Returns full diagnostics always. If THIS returns games but
    /api/ncaabb/games doesn't, the problem is route/deploy staleness."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    out = {"date": target.isoformat(), "hl_enabled": False, "hl_count": 0,
           "hl_error": None, "espn_count": 0, "espn_error": None,
           "hl_sample": [], "espn_sample": []}
    try:
        import highlightly as hl
        out["hl_enabled"] = hl.enabled()
        if hl.enabled():
            hg = hl.get_games(target) or []
            out["hl_count"] = len(hg)
            out["hl_sample"] = [g["away"]["name"] + " @ " + g["home"]["name"] for g in hg[:6]]
    except Exception as e:
        import traceback
        out["hl_error"] = traceback.format_exc()[-500:]
    try:
        from ncaab_baseball import get_games as espn_get
        eg = espn_get(target) or []
        out["espn_count"] = len(eg)
        out["espn_sample"] = [g["away"]["name"] + " @ " + g["home"]["name"] for g in eg[:6]]
    except Exception as e:
        import traceback
        out["espn_error"] = traceback.format_exc()[-500:]
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@app.get("/api/version")
def version():
    """Backend build marker that PROVES which ncaabb_games code is live by
    introspecting the actual function signature, not a hardcoded string."""
    import inspect
    try:
        sig = str(inspect.signature(ncaabb_games))
        src = inspect.getsource(ncaabb_games)
        has_debug_return = '"diag": diag' in src
        has_jsonresponse = "JSONResponse" in src
        line_count = src.count("\n")
    except Exception:
        sig = "?"; has_debug_return = False; has_jsonresponse = False; line_count = 0
    return {"backend_build": "v68",
            "ncaabb_games_signature": sig,
            "has_debug_return": has_debug_return,
            "uses_JSONResponse": has_jsonresponse,
            "function_line_count": line_count}


@app.get("/healthz")
@app.head("/healthz")
def healthz():
    """Lightweight health check for the platform's router (Railway/Render). Always
    fast, no DB or network, so the app is marked healthy and receives traffic."""
    return {"ok": True}


@app.get("/api/odds/diag")
def _odds_diag():
    out = {"odds_api": None, "sgo_enabled": False, "sgo_game_odds_sample": None}
    try:
        import odds_api
        out["odds_api"] = odds_api.diag()
    except Exception as e:
        out["odds_api"] = {"error": str(e)}
    try:
        import sgo_api
        out["sgo_enabled"] = sgo_api.enabled()
    except Exception as e:
        out["sgo_enabled"] = f"error: {e}"
    # live test: does the SGO fallback return a game line for today's first MLB game?
    try:
        games = mlb_provider.get_games(dt.date.today()) or []
        if games:
            g = games[0]
            import sgo_api
            so = sgo_api.get_game_odds("mlb", g["home"]["name"], g["away"]["name"])
            out["sgo_game_odds_sample"] = {"match": g["away"]["name"] + " @ " + g["home"]["name"],
                                           "odds": so}
    except Exception as e:
        out["sgo_game_odds_sample"] = {"error": str(e)}
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


# ---- favicon / app icons (embedded so they survive a forgotten git add) ----
import base64 as _b64
_FAVICON_ICO = _b64.b64decode("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")
_ICON_180 = _b64.b64decode("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")

@app.get("/favicon.ico")
def _favicon():
    return Response(content=_FAVICON_ICO, media_type="image/x-icon",
                    headers={"Cache-Control": "public, max-age=86400"})

@app.get("/icon-180.png")
def _icon180():
    return Response(content=_ICON_180, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})

@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def _apple_icon():
    return Response(content=_ICON_180, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/")
@app.head("/")
def index():
    # No-cache so every deploy reaches the browser immediately. index.html is
    # small; the cost is negligible and it prevents stale-frontend confusion.
    # HEAD is accepted too so platform health checks don't get a 405.
    return FileResponse("index.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})
