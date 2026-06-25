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
    _MODEL_STATUS = {"ratings_loaded": 0, "ranking_fallback": 0, "mode": "ranking-only"}
    # Prefer the feed-built ratings on the persistent volume (survives redeploys),
    # then any committed ratings.json. Either gets us out of ranking-only mode.
    _rfile = os.environ.get("RATINGS_FILE", "ratings.json")
    loaded = engine.load_ratings("/data/ratings.json") or engine.load_ratings(_rfile)
    if loaded:
        _MODEL_STATUS["ratings_loaded"] = loaded
        _MODEL_STATUS["mode"] = "surface-elo"
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
        _MODEL_STATUS["ranking_fallback"] = len(ranks)
        print(f"[predictions] loaded {len(ranks)} ranked players as fallback")
    except Exception as e:
        print(f"[predictions] ranking fallback skipped ({e})")
else:
    from mock import MockTennisProvider
    from seed import build_today
    provider = MockTennisProvider()
    engine = None

live_engine = LiveEngine(provider)

# Per-player surface win/loss records (career + by year), generated offline by
# build_surface_records.py from Sackmann CSVs and committed as a small JSON. Used
# by the match detail's Surface tab. Absent file => feature degrades to "no data".
import unicodedata as _ud

def _norm_surface_name(name: str) -> str:
    """Space-preserving normalization that MATCHES build_surface_records.norm_name
    (lowercase, accent-stripped, whitespace-collapsed) so live names line up with
    the dataset keys. Distinct from the alphanumeric-only _norm_player used by the
    props matcher below."""
    if not name:
        return ""
    s = _ud.normalize("NFKD", str(name))
    s = "".join(c for c in s if not _ud.combining(c))
    return " ".join(s.lower().split())

SURFACE_RECORDS: dict = {}
# Live tennis feeds give abbreviated names ("A. Zverev"); the Sackmann dataset is
# keyed by full names ("alexander zverev"). This secondary index maps
# "<initial> <lastname...>" -> full key so abbreviated names still resolve. A key
# that maps to two different players is set to None (ambiguous) and not matched.
SURFACE_ABBREV: dict = {}
_srf = os.environ.get("SURFACE_RECORDS_FILE", "surface_records.json")

def _abbrev_key(norm_full: str):
    toks = [t.strip(".") for t in norm_full.split() if t.strip(".")]
    if len(toks) < 2:
        return None
    return toks[0][0] + " " + " ".join(toks[1:])

def _rebuild_surface_abbrev():
    global SURFACE_ABBREV
    idx: dict = {}
    for k in SURFACE_RECORDS.keys():
        ak = _abbrev_key(k)
        if not ak:
            continue
        if ak in idx and idx[ak] != k:
            idx[ak] = None          # collision -> ambiguous, don't guess
        else:
            idx[ak] = k
    SURFACE_ABBREV = idx

def _resolve_surface_rec(name: str):
    """Find a player's record by exact name, then by initial+lastname fallback."""
    norm = _norm_surface_name(name)
    rec = SURFACE_RECORDS.get(norm)
    if rec:
        return rec
    toks = [t.strip(".") for t in norm.split() if t.strip(".")]
    if len(toks) >= 2 and len(toks[0]) == 1:
        fk = SURFACE_ABBREV.get(toks[0][0] + " " + " ".join(toks[1:]))
        if fk:
            return SURFACE_RECORDS.get(fk)
    return None

def _build_surface_records_bg():
    """Build records from Sackmann CSVs and cache to _srf. Runs in a background
    thread so a slow first boot (pulling ~20 CSVs) never blocks the server from
    answering Railway's health check. The freshly built set REPLACES the cache
    only if it is non-empty and not smaller than what we already have, so a fetch
    outage can never wipe a good cache (which is what happened before)."""
    global SURFACE_RECORDS
    try:
        import build_surface_records as _bsr
        _y0 = int(os.environ.get("SURFACE_START_YEAR", "2015"))
        _y1 = dt.date.today().year
        _store, _tot = {}, 0
        _atp_rows, _wta_rows = 0, 0
        print(f"[surface] background build {_y0}..{_y1} started (server is already live) ...")
        for _yr in range(_y0, _y1 + 1):
            _ar = _bsr.aggregate(_bsr._fetch_csv(_bsr.ATP_URL.format(year=_yr)), _store)
            _wr = _bsr.aggregate(_bsr._fetch_csv(_bsr.WTA_URL.format(year=_yr)), _store)
            _atp_rows += _ar; _wta_rows += _wr; _tot += _ar + _wr
        print(f"[surface] fetch tally: ATP rows={_atp_rows:,}, WTA rows={_wta_rows:,}, "
              f"unique players={len(_store):,}")
        _mm = int(os.environ.get("SURFACE_MIN_MATCHES", "0"))
        if _mm > 0:
            _store = {k: v for k, v in _store.items() if _bsr._career_total(v) >= _mm}
        # Safety: never replace a good cache with an empty/smaller partial build.
        _prev = len(SURFACE_RECORDS)
        _floor = int(os.environ.get("SURFACE_MIN_PLAYERS", "800"))
        if not _store or (len(_store) < _prev and len(_store) < _floor):
            print(f"[surface] build yielded {len(_store):,} players (have {_prev:,}); "
                  f"keeping existing cache, NOT overwriting. Check the fetch tally above "
                  f"\u2014 0 rows means the data host is unreachable from this server.")
            return
        SURFACE_RECORDS = _store
        _rebuild_surface_abbrev()
        try:
            with open(_srf, "w") as _f:
                json.dump(_store, _f, separators=(",", ":"))
            print(f"[surface] built {len(_store):,} players from {_tot:,} matches -> {_srf} "
                  f"(cached; future boots load instantly)")
        except Exception as _e:
            print(f"[surface] built {len(_store):,} players in-memory; cache write failed ({_e})")
    except Exception as _e:
        print(f"[surface] background build failed ({_e})")

def _load_surface_records():
    """Load surface records from the first source that actually has data:
      1. SURFACE_RECORDS_FILE (e.g. a persistent volume), then
      2. ./surface_records.json committed in the repo (built by GitHub Actions).
    Runtime fetching from Sackmann is intentionally NOT the primary path: GitHub
    and jsDelivr block this host's datacenter IP (403/404), so the data is built
    elsewhere and shipped in the repo. Returns the path that loaded, or None."""
    global SURFACE_RECORDS
    candidates = []
    _here = os.path.dirname(os.path.abspath(__file__))
    for p in ("/data/surface_records.json", _srf, "surface_records.json",
              os.path.join(_here, "surface_records.json"),
              "/app/surface_records.json"):
        if p and p not in candidates:
            candidates.append(p)
    for p in candidates:
        try:
            with open(p) as _f:
                data = json.load(_f)
        except FileNotFoundError:
            continue
        except Exception as _e:
            print(f"[surface] could not parse {p} ({_e})")
            continue
        if data:
            SURFACE_RECORDS = data
            _rebuild_surface_abbrev()
            print(f"[surface] loaded records for {len(SURFACE_RECORDS):,} players from {p}")
            return p
    return None

_loaded_from = _load_surface_records()
if _loaded_from is None:
    # Nothing usable on disk yet. Runtime self-build only happens if explicitly
    # enabled AND can reach the data (it usually can't from Railway); the builder
    # is guarded so it can never overwrite a good cache.
    if os.environ.get("BUILD_SURFACE_AT_RUNTIME", "").lower() in ("1", "true", "yes"):
        import threading
        threading.Thread(target=_build_surface_records_bg, daemon=True).start()
        print("[surface] no data on disk; attempting background self-build (note: "
              "GitHub/jsDelivr block this host, so this may yield 0 \u2014 the "
              "GitHub Actions workflow is the reliable source).")
    else:
        print("[surface] no surface_records.json on disk; Surface tab hidden until "
              "the committed data file is present (built by the GitHub Action).")


def _player_surface_card(name: str):
    """Both-surface record block for one player, current year highlighted by the
    caller. Returns None if we have no history for this player."""
    rec = _resolve_surface_rec(name)
    if not rec:
        return None
    this_year = str(dt.date.today().year)
    out = {"name": rec.get("name", name), "surfaces": {}}
    for surf in ("Hard", "Clay", "Grass", "Carpet"):
        s = (rec.get("surfaces") or {}).get(surf)
        if not s:
            continue
        cw, cl = s.get("career", [0, 0])
        yw, yl = (s.get("by_year") or {}).get(this_year, [0, 0])
        if cw + cl == 0:
            continue
        out["surfaces"][surf] = {
            "career": {"w": cw, "l": cl,
                       "pct": round(100 * cw / (cw + cl)) if (cw + cl) else None},
            "year": {"w": yw, "l": yl,
                     "pct": round(100 * yw / (yw + yl)) if (yw + yl) else None},
        }
    return out if out["surfaces"] else None


def _surface_record_str(name: str, surface: str):
    """Career W-L (pct) for a player on one surface, e.g. '42\u20138 (84%)'. For
    the analysis write-up. None if unknown."""
    rec = _resolve_surface_rec(name)
    if not rec or not surface:
        return None
    s = (rec.get("surfaces") or {}).get(str(surface).title())
    if not s:
        return None
    w, l = s.get("career", [0, 0])
    if w + l == 0:
        return None
    return f"{w}\u2013{l} ({round(100 * w / (w + l))}%)"


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
    try:
        _settle_stale_tennis()   # clear canceled/stuck tennis bets on startup too
    except Exception as e:
        print(f"[backfill] stale-tennis sweep failed: {e}")

    # Pass 1: team sports (cheap reads) — settles NCAA / MLB / NBA / NFL / UFC / soccer.
    for off in range(1, days + 1):
        d = (today - dt.timedelta(days=off)).isoformat()
        for label in ("ncaabb", "mlb", "nba", "nfl", "ufc", "soccer"):
            try:
                if label == "ncaabb":
                    ncaabb_games(date=d)
                elif label == "mlb":
                    mlb_games(date=d)
                elif label == "ufc":
                    ufc_games(date=d)
                elif label == "soccer":
                    soccer_games(date=d, league="all")
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

    # Tennis odds warmer — snapshots today's tennis picks' market line from
    # api-tennis (included in the plan) every ~30 min, so units/ROI/CLV settle
    # even for matches no one opened. Independent of the Odds API; the provider
    # caches the whole-day odds pull (~1 request per cycle). Disable: TENNIS_ODDS=0.
    if USE_REAL and run_bg and os.environ.get("TENNIS_ODDS", "1") == "1":
        def _tennis_odds_bg():
            import time as _t
            from models import OddsSnapshot
            _t.sleep(160)   # let the first build settle + pass healthcheck
            refresh = int(os.environ.get("TENNIS_ODDS_REFRESH", "3600") or 3600)
            while True:
                try:
                    plays = _gather_plays(dt.date.today())
                    tennis = [p for p in plays
                              if p.get("sport") == "tennis" and p.get("pmid")]
                    # Skip matches snapshotted within `refresh` secs so each match
                    # costs ~1 odds request/hour (opening line captured once, then
                    # occasional refresh for CLV) — keeps us well under the cap.
                    recent = set()
                    if tennis:
                        cutoff = dt.datetime.utcnow() - dt.timedelta(seconds=refresh)
                        with SessionLocal() as db:
                            for s in (db.query(OddsSnapshot)
                                        .filter(OddsSnapshot.sport == "tennis",
                                                OddsSnapshot.last_seen >= cutoff).all()):
                                recent.add(s.ref)
                    n = 0
                    for p in tennis:
                        if str(p["id"]) in recent:
                            continue
                        try:
                            _attach_tennis_market(p)
                            if p.get("market_odds") is not None:
                                n += 1
                        except Exception:
                            pass
                    if n:
                        print(f"[tennis-odds] snapshotted {n} new match lines")
                except Exception as e:
                    print(f"[tennis-odds] warmer error: {e}")
                _t.sleep(int(os.environ.get("TENNIS_ODDS_SECS", "1800") or 1800))
        import threading as _thr_to
        _thr_to.Thread(target=_tennis_odds_bg, daemon=True).start()

    # Odds-snapshot warmer — OFF by default. On a free Odds API tier this loop
    # can consume the whole monthly quota and starve the live board's edge/odds.
    # Only enable (ODDS_SNAPSHOT=1) if you have quota headroom (paid plan).
    if run_bg and os.environ.get("ODDS_SNAPSHOT", "0") == "1":
        def _odds_snapshot_bg():
            import time as _t
            _t.sleep(150)
            every = max(1, int(os.environ.get("ODDS_SNAPSHOT_HOURS", "12") or 12)) * 3600
            while True:
                try:
                    import odds_api
                    if odds_api.enabled():
                        today = dt.date.today().isoformat()
                        jobs = [
                            ("mlb", lambda: mlb_games(date=today)),
                            ("ncaabb", lambda: ncaabb_games(date=today)),
                            ("ufc", lambda: ufc_games(date=None)),
                            ("nba", lambda: team_games("nba", date=today)),
                            ("nfl", lambda: team_games("nfl", date=today)),
                            ("nhl", lambda: team_games("nhl", date=today)),
                            ("ncaaf", lambda: team_games("ncaaf", date=today)),
                            ("ncaab", lambda: team_games("ncaab", date=today)),
                            ("soccer", lambda: soccer_games(date=today, league="all")),
                        ]
                        n = 0
                        for name, fn in jobs:
                            try:
                                fn()
                                n += 1
                            except Exception as e:
                                print(f"[odds-snapshot] {name} failed: {e}")
                            _t.sleep(3)
                        print(f"[odds-snapshot] cycle done ({n} boards)")
                except Exception as e:
                    print(f"[odds-snapshot] loop error: {e}")
                _t.sleep(every)
        import threading as _thr_os
        _thr_os.Thread(target=_odds_snapshot_bg, daemon=True).start()

    # Golf matchup tracker — records DataGolf 3-balls at tee-off and grades them
    # on round scores so golf shows up in /api/accuracy units/ROI. No-op without
    # DATAGOLF_KEY. Disable with GOLF_TRACKER=0.
    if run_bg and os.environ.get("GOLF_TRACKER", "1") == "1":
        def _golf_tracker_bg():
            import time as _t
            _t.sleep(180)
            try:
                import datagolf_api
            except Exception:
                return
            tours = [t.strip() for t in
                     os.environ.get("GOLF_TRACKER_TOURS", "pga").split(",") if t.strip()]
            while True:
                if datagolf_api.enabled():
                    import golf_tracker
                    for tr in tours:
                        try:
                            a = golf_tracker.record(tr)
                            s = golf_tracker.settle(tr)
                            if a or s:
                                print(f"[golf-tracker] {tr}: +{a} recorded, {s} settled")
                        except Exception as e:
                            print(f"[golf-tracker] {tr} error: {e}")
                _t.sleep(int(os.environ.get("GOLF_TRACKER_SECS", "3600") or 3600))
        import threading as _thr_gt
        _thr_gt.Thread(target=_golf_tracker_bg, daemon=True).start()

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
    Surfaces the raw success/error (api-tennis returns HTTP 200 even on an
    expired key or blown quota, with success:0 + a message that _call swallows),
    so we can tell 'no data yet' from 'filtered out' from 'auth/quota error'."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    out = {"date": target.isoformat()}
    try:
        if not hasattr(provider, "_call"):
            return {"error": "not using the live API-Tennis provider", **out}
        import httpx
        from apitennis import BASE_URL
        d = target.strftime("%Y-%m-%d")
        # Direct call that does NOT swallow the API's own status fields.
        params = {"method": "get_fixtures", "APIkey": provider.api_key,
                  "timezone": getattr(provider, "timezone", "America/Chicago"),
                  "date_start": d, "date_stop": d}
        r = httpx.get(BASE_URL, params=params, timeout=20.0)
        out["http_status"] = r.status_code
        try:
            data = r.json()
        except Exception:
            data = {}
            out["raw_text"] = (r.text or "")[:300]
        if isinstance(data, dict):
            out["resp_keys"] = list(data.keys())
            out["api_success"] = data.get("success")
            err = data.get("error") or data.get("Error") or data.get("message") or data.get("msg")
            if err:
                out["api_error"] = err
            res = data.get("result")
            out["result_count"] = len(res) if isinstance(res, list) else (
                "none" if res is None else "non-list:" + type(res).__name__)
            if isinstance(res, list) and res:
                out["result0"] = str(res[0])[:500]
            elif res is not None and not isinstance(res, list):
                out["result_value"] = str(res)[:500]
        out["raw_text"] = (r.text or "")[:700]
        out["key_set"] = bool(getattr(provider, "api_key", None))
        out["key_tail"] = ("\u2026" + provider.api_key[-4:]) if getattr(provider, "api_key", None) else None
        out["req_count_today"] = getattr(provider, "_req_count", None)
        out["last_error"] = getattr(provider, "last_error", None)
        # Then the normal (swallowed) path + classification, for comparison.
        from collections import Counter
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


_RATINGS_BUILD = {"running": False, "report": None}


def _run_ratings_build(start, chunk_days=1):
    """Wrapper: always clears the running flag, captures any fatal error."""
    try:
        _run_ratings_build_inner(start, chunk_days)
    except Exception as e:
        import traceback
        cur = _RATINGS_BUILD.get("report") or {}
        cur["fatal"] = f"{type(e).__name__}: {e}"
        cur["trace"] = traceback.format_exc()[-1200:]
        _RATINGS_BUILD["report"] = cur
    finally:
        _RATINGS_BUILD["running"] = False


def _run_ratings_build_inner(start, chunk_days=1):
    """Train the surface-Elo from the api-tennis feed (which Railway reaches) and
    export ratings.json to the volume, then hot-load it — moving the tennis model
    from ranking-only to full surface-Elo. Mirrors the surface-records build."""
    global engine
    import apitennis as _at
    from predictions import PredictionEngine, _name_key
    report = {"start": start, "chunk_days": chunk_days, "matches": 0,
              "players_so_far": 0, "calls": 0, "errors": [], "by_year": {}}
    _RATINGS_BUILD["report"] = report
    prov = _at.APITennisProvider()
    eng = PredictionEngine()                      # fresh, empty surface-Elo

    def _grab(d0, d1):
        return prov._call("get_fixtures", date_start=d0.isoformat(), date_stop=d1.isoformat())

    today = dt.date.today()
    if start < 2010:
        start = 2010
    span = max(1, min(31, chunk_days))
    cur = dt.date(start, 1, 1)
    empty = 0
    while cur <= today:
        cend = min(cur + dt.timedelta(days=span - 1), today)
        try:
            rows = _grab(cur, cend)
            report["calls"] += 1
        except Exception as ex:
            rows = []
            if "timeout" not in (type(ex).__name__ + str(ex)).lower():
                d = cur
                while d <= cend:
                    try:
                        rows += _grab(d, d) or []
                        report["calls"] += 1
                    except Exception:
                        pass
                    d += dt.timedelta(days=1)
        n = 0
        for fix in rows or []:
            if not fix.get("event_winner"):
                continue
            win = _at._winner(fix.get("event_winner"))
            if not win:
                continue
            pa = (fix.get("event_first_player") or "").strip()
            pb = (fix.get("event_second_player") or "").strip()
            if not pa or not pb or "/" in pa or "/" in pb:
                continue
            tier = _at._classify_tier(fix)
            if tier not in ("ATP", "WTA"):
                continue
            ds = (fix.get("event_date") or "").strip()
            try:
                when = dt.date.fromisoformat(ds)
            except Exception:
                when = cur
            surface = _at._infer_surface(fix.get("tournament_name") or "", tier, when)
            w = pa if win == "a" else pb
            l = pb if win == "a" else pa
            eng.model.update(w, l, surface)       # sequential Elo update (chronological)
            n += 1
        report["matches"] += n
        if n:
            report["by_year"][f"{cur:%Y}"] = report["by_year"].get(f"{cur:%Y}", 0) + n
            empty = 0
        else:
            empty += 1
        report["players_so_far"] = len(eng.model.overall)
        _RATINGS_BUILD["report"] = dict(report)
        if report["matches"] == 0 and empty >= 8:
            report["aborted"] = f"no matches from {start} — try a later &start="
            break
        cur = cend + dt.timedelta(days=1)

    # index best rating per name-key (mirrors train_from_sackmann)
    for name, rating in eng.model.overall.items():
        k = _name_key(name)
        if k and rating > eng._by_key.get(k, 0):
            eng._by_key[k] = rating
    report["players"] = len(eng.model.overall)
    if len(eng.model.overall) >= 100:
        path = "/data/ratings.json"
        try:
            eng.export_ratings(path)
            report["saved_to"] = path
        except Exception as e:
            report["save_error"] = str(e)
        try:
            if engine is not None:
                n2 = engine.load_ratings(path)
                if "_MODEL_STATUS" in globals():
                    _MODEL_STATUS["ratings_loaded"] = n2
                    _MODEL_STATUS["mode"] = "surface-elo"
                report["live_loaded"] = n2
        except Exception as e:
            report["load_error"] = str(e)
        report["status"] = "DONE \u2014 ratings.json saved + loaded; tennis model is now surface-Elo"
    else:
        report["status"] = f"too few players ({len(eng.model.overall)}) \u2014 not saved"
    _RATINGS_BUILD["report"] = report


@app.get("/api/ratings/build-from-feed")
def ratings_build_from_feed(confirm: str = "", start: int = 2024, chunk: int = 1, force: str = ""):
    """Train the tennis surface-Elo from the api-tennis feed and write ratings.json
    to the volume (then hot-load it). Moves the model off ranking-only. Background;
    poll /api/ratings/build-status. &force=yes clears a stuck run."""
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes to run",
                             "effect": "train surface-Elo from feed -> /data/ratings.json -> live"})
    if _RATINGS_BUILD["running"] and force != "yes":
        return JSONResponse({"status": "already running",
                             "tip": "add &force=yes if stuck", "poll": "/api/ratings/build-status"})
    _RATINGS_BUILD["running"] = True
    _RATINGS_BUILD["report"] = None
    import threading
    threading.Thread(target=_run_ratings_build, args=(start, chunk), daemon=True).start()
    return JSONResponse({"status": "ratings build started", "poll": "/api/ratings/build-status"})


@app.get("/api/ratings/build-status")
def ratings_build_status():
    return JSONResponse({"running": _RATINGS_BUILD["running"], "report": _RATINGS_BUILD["report"]},
                        headers={"Cache-Control": "no-store"})


_ELO_BUILD = {"running": False, "report": None}


def _run_elo_build(sport, start, end, seasons):
    try:
        import espn_elo
        s = dt.date.fromisoformat(start) if start else None
        e = dt.date.fromisoformat(end) if end else None
        rep = espn_elo.build(sport, s, e, seasons=seasons,
                             progress=lambda r: _ELO_BUILD.__setitem__("report", r))
        try:
            espn_elo.reload(sport)
        except Exception:
            pass
        _ELO_BUILD["report"] = rep
    except Exception as ex:
        import traceback
        _ELO_BUILD["report"] = {"fatal": f"{type(ex).__name__}: {ex}",
                                "trace": traceback.format_exc()[-1000:]}
    finally:
        _ELO_BUILD["running"] = False


@app.get("/api/elo/build")
def elo_build(sport: str = "", confirm: str = "", start: str = "", end: str = "",
              seasons: int = 2, force: str = ""):
    """Build MOV-weighted team Elo from ESPN results (nba/nfl/ncaaf/ncaab) with
    multi-season carryover, writing /data/{sport}_elo.json and hot-loading it.
    &seasons=N controls how many past seasons to fold in (default 2). Background;
    poll /api/elo/build-status."""
    sport = (sport or "").lower()
    if confirm != "yes" or not sport:
        return JSONResponse({"note": "append ?sport=nba&confirm=yes (optional &seasons=2 &start=&end=)",
                             "sports": ["nba", "nfl", "ncaaf", "ncaab", "wncaab"]})
    if _ELO_BUILD["running"] and force != "yes":
        return JSONResponse({"status": "already running", "poll": "/api/elo/build-status",
                             "tip": "add &force=yes if stuck"})
    _ELO_BUILD["running"] = True
    _ELO_BUILD["report"] = None
    import threading
    threading.Thread(target=_run_elo_build, args=(sport, start or None, end or None, max(1, seasons)),
                     daemon=True).start()
    return JSONResponse({"status": f"{sport} elo build started ({seasons} season(s), MOV)",
                         "poll": "/api/elo/build-status"})


@app.get("/api/elo/build-status")
def elo_build_status():
    return JSONResponse({"running": _ELO_BUILD["running"], "report": _ELO_BUILD["report"]},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/refresh/ncaaf")
def refresh_ncaaf(confirm: str = "", year: int = 0):
    """Build NCAAF SP+ ratings from CFBD (your CFBD_API_KEY) -> /data, hot-reload.
    SP+ preseason ratings are published over the summer, so this is useful now."""
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes (optional &year=2026)"})
    path = "/data/ncaaf_sp.json"
    try:
        import refresh_cfbd_sp as _r
        _r.OUT = path
        data = _r.build(year or None)
    except SystemExit as e:
        return JSONResponse({"error": str(e), "tip": "set CFBD_API_KEY on Railway"}, status_code=400)
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]},
                            status_code=500)
    n = 0
    try:
        import ncaaf_provider
        ncaaf_provider._PATH = path
        n = ncaaf_provider.reload()
    except Exception as e:
        return JSONResponse({"status": "built but reload failed", "error": str(e),
                             "teams": len(data.get("teams", {})), "path": path})
    return JSONResponse({"status": "done", "teams": len(data.get("teams", {})),
                         "loaded": n, "path": path, "season": data.get("season")})


@app.get("/api/refresh/ncaab")
def refresh_ncaab(confirm: str = "", season: int = 0):
    """Build NCAAB adjusted ratings from CBBD (your CBBD_API_KEY) -> /data, hot-reload.
    Note: college-hoops ratings may be sparse until the season tips off (Nov)."""
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes (optional &season=2026)"})
    path = "/data/ncaab_ratings.json"
    try:
        import refresh_cbbd_ratings as _r
        _r.OUT = path
        data = _r.build(season or None)
    except SystemExit as e:
        return JSONResponse({"error": str(e), "tip": "set CBBD_API_KEY on Railway"}, status_code=400)
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]},
                            status_code=500)
    n = 0
    try:
        import ncaab_provider
        ncaab_provider._PATH = path
        n = ncaab_provider.reload()
    except Exception as e:
        return JSONResponse({"status": "built but reload failed", "error": str(e),
                             "teams": len(data.get("teams", {})), "path": path})
    return JSONResponse({"status": "done", "teams": len(data.get("teams", {})),
                         "loaded": n, "path": path, "season": data.get("season")})


@app.get("/api/models/diag")
def models_diag():
    """One-shot health check of every sport's model: is it running on real ratings
    or a fallback? For each team sport we look for its ratings/stats file (the
    thing a refresh script builds from an external stats API) and report whether
    it's present, how fresh it is, and how many teams it covers. Mirrors what
    tennis model-diag does, across all sports."""
    import time as _t

    def _probe(fname, envk):
        cands = []
        if envk and os.environ.get(envk):
            cands.append(os.environ[envk])
        cands += [fname, f"/data/{fname}", f"/app/{fname}",
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)]
        for p in cands:
            try:
                if os.path.exists(p):
                    age_h = round((_t.time() - os.path.getmtime(p)) / 3600.0, 1)
                    teams = None
                    try:
                        with open(p) as f:
                            d = json.load(f)
                        if isinstance(d, dict) and isinstance(d.get("teams"), (dict, list)):
                            teams = len(d["teams"])           # nba/nfl/ncaa*/nhl stats files
                        elif isinstance(d, dict) and isinstance(d.get("ratings"), dict):
                            teams = len(d["ratings"])         # espn_elo files
                        elif isinstance(d, (dict, list)):
                            teams = len(d)
                    except Exception:
                        pass
                    status = "full" if age_h <= 24 * 10 else "STALE (refresh)"
                    return {"file": p, "teams": teams, "age_hours": age_h, "status": status}
            except Exception:
                continue
        return {"file": None, "status": "FALLBACK (no ratings file \u2014 records-only)"}

    out = {}
    out["tennis"] = dict(globals().get("_MODEL_STATUS",
                         {"ratings_loaded": 0, "mode": "ranking-only"}))
    for sport, (fname, envk) in {
        "nba": ("nba_stats.json", "NBA_STATS_PATH"),
        "nfl": ("nfl_stats.json", "NFL_STATS_PATH"),
        "nhl": ("nhl_stats.json", "NHL_STATS_PATH"),
        "ncaab": ("ncaab_ratings.json", "NCAAB_RATINGS_PATH"),
        "ncaaf": ("ncaaf_sp.json", "NCAAF_SP_PATH"),
        "ncaa_baseball": ("ncaa_stats.json", None),
    }.items():
        out[sport] = _probe(fname, envk)
    # Sports with no stats file may still have an ESPN-results Elo we built.
    for sport in ("nba", "nfl", "ncaaf", "ncaab"):
        if out.get(sport, {}).get("file") is None:
            ep = f"/data/{sport}_elo.json"
            try:
                if os.path.exists(ep):
                    with open(ep) as f:
                        ed = json.load(f)
                    n = len(ed.get("ratings", {}) or {})
                    age = round((_t.time() - os.path.getmtime(ep)) / 3600.0, 1)
                    out[sport] = {"file": ep, "teams": n, "age_hours": age,
                                  "status": "elo (ESPN results)" if n else "FALLBACK (empty elo)"}
            except Exception:
                pass
    try:
        import datagolf_api
        out["golf"] = {"status": "full (DataGolf live)" if datagolf_api.enabled()
                       else "FALLBACK (DataGolf not configured)"}
    except Exception as e:
        out["golf"] = {"status": f"unknown ({e})"}
    out["mlb"] = {"status": "live (MLB Stats API records/Elo \u2014 no static ratings file)"}
    out["soccer"] = {"status": "live (provider form/odds \u2014 no static ratings file)"}
    out["_legend"] = ("full = real ratings loaded; FALLBACK = running on records/win% "
                      "only (weaker, like tennis ranking-only was); STALE = file too old, re-run refresh")
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@app.get("/api/tennis/model-diag")
def tennis_model_diag():
    """Reports whether the tennis model is running its full surface-Elo (a
    ratings.json was loaded) or the weak ranking-only fallback. Ranking-only is
    what makes it over-pick underdogs the market correctly fades."""
    st = dict(globals().get("_MODEL_STATUS",
                            {"ratings_loaded": 0, "ranking_fallback": 0, "mode": "ranking-only"}))
    st["explanation"] = ("surface-elo = full strength; ranking-only = predicting from "
                         "ATP/WTA rank position only (no form/surface/matchups), which "
                         "disagrees with the market and over-picks underdogs")
    st["ratings_file"] = os.environ.get("RATINGS_FILE", "ratings.json")
    for p in ("/data/ratings.json", "ratings.json", "/app/ratings.json"):
        if os.path.exists(p):
            st["ratings_file_on_disk"] = p
            break
    return JSONResponse(st, headers={"Cache-Control": "no-store"})


@app.get("/api/tennis/odds-diag")
def tennis_odds_diag(date: str | None = None):
    """Read-only: is api-tennis returning Home/Away odds per match, and are
    tennis picks snapshotting a line? get_odds is per-match (keyed by event id),
    so we sample a few of today's tour matches and fetch each one's odds."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    out = {"date": target.isoformat()}
    try:
        if not hasattr(provider, "get_odds"):
            return {"error": "not using the live API-Tennis provider", **out}
        _ensure_day(target)
        with SessionLocal() as db:
            rows = (db.query(Match)
                      .filter(Match.scheduled >= dt.datetime.combine(target, dt.time.min),
                              Match.scheduled <= dt.datetime.combine(target, dt.time.max))
                      .order_by(Match.scheduled).limit(8).all())
            matches = [(m.provider_match_id, m.player_a, m.player_b) for m in rows]
        out["sampled_matches"] = len(matches)
        sample = []
        priced = 0
        for pmid, a, b in matches:
            dec_a, dec_b = _tennis_odds_for(pmid, a, b)
            if dec_a and dec_b:
                priced += 1
            sample.append({"pmid": pmid, "a": a, "b": b,
                           "dec_a": dec_a, "dec_b": dec_b,
                           "amer_a": _dec_to_amer(dec_a) if dec_a else None,
                           "amer_b": _dec_to_amer(dec_b) if dec_b else None})
        out["matches_priced"] = priced
        out["sample"] = sample
        # raw get_odds for the first match, so we can see exactly what comes back
        if matches:
            pmid0 = matches[0][0]
            try:
                raw = provider._call("get_odds", match_key=str(pmid0))
                out["raw_first"] = {"pmid": pmid0,
                                    "type": type(raw).__name__,
                                    "keys": list(raw.keys())[:5] if isinstance(raw, dict) else None,
                                    "snippet": str(raw)[:400]}
            except Exception as e:
                out["raw_first_error"] = str(e)
        out["req_count_today"] = getattr(provider, "_req_count", None)
        out["last_error"] = getattr(provider, "last_error", None)
        try:
            from models import OddsSnapshot, PickResult
            with SessionLocal() as db:
                out["tennis_snapshots"] = db.query(OddsSnapshot).filter_by(sport="tennis").count()
                settled = db.query(PickResult).filter_by(sport="tennis").all()
                out["tennis_settled"] = len(settled)
                out["tennis_settled_with_odds"] = sum(1 for r in settled if r.taken_odds is not None)
        except Exception as e:
            out["store_error"] = str(e)
    except Exception as e:
        out["error"] = str(e)
    return out


_TEAM_EVENT_KEYS = ("davis cup", "billie jean king", "bjk cup", "united cup",
                    "laver cup", "atp cup", "fed cup", "world team", "teams")


def _is_tennis_team_event(m):
    """True for national-team competitions (Davis Cup, BJK Cup, United/Laver Cup).
    These are country-vs-country with no player-level odds; the classifier now
    excludes them from new slates, but this also hides any already-loaded rows so
    they stop showing as phantom 'awaiting market' cards immediately."""
    name = (getattr(m, "tournament", "") or "").lower()
    return any(k in name for k in _TEAM_EVENT_KEYS)


@app.get("/api/matches")
def list_matches(date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_day(target)
    with SessionLocal() as db:
        rows = (db.query(Match)
                  .filter(Match.scheduled >= dt.datetime.combine(target, dt.time.min),
                          Match.scheduled <= dt.datetime.combine(target, dt.time.max))
                  .order_by(Match.scheduled).all())
        rows = [m for m in rows if not _is_tennis_team_event(m)]
        # Pull the day's market lines in ONE cached call so each card can show the
        # market price + model edge like the other sports. Aligned by player name
        # so ml_home always = player_a's price (the model's prob_a side).
        odds_book = {}
        try:
            prov = globals().get("provider")
            if prov is not None and hasattr(prov, "get_odds"):
                odds_book = prov.get_odds(day=target) or {}
        except Exception:
            odds_book = {}

        def _ml_pair(m):
            od = odds_book.get(str(m.provider_match_id))
            if not od or not (od.get("a") and od.get("b")):
                return None
            from odds_api import _norm
            da, db2, f, s = od["a"], od["b"], od.get("first"), od.get("second")

            def _same(x, y):
                x, y = _norm(x or ""), _norm(y or "")
                return bool(x) and bool(y) and (x == y or x.split()[-1] == y.split()[-1])
            if _same(m.player_a, s) or _same(m.player_b, f):
                da, db2 = db2, da           # feed order is flipped vs player_a/b
            am_a, am_b = _dec_to_amer(da), _dec_to_amer(db2)
            if am_a is None or am_b is None:
                return None
            return {"ml_home": am_a, "ml_away": am_b}

        result = []
        for m in rows:
            row = _match_row(db, m)
            ml = _ml_pair(m)
            if ml:
                row["odds"] = ml
            result.append(row)
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
            if _is_tennis_team_event(m):
                continue
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
        # Same in-depth, AI-written rationale used under Best Bets, for EVERY match:
        # who the model favours and by how much (Elo edge), whether the surface
        # suits them, plus form/rest/H2H and conditions. Falls back to the
        # standard writeup if the Best Bets pipeline is unavailable.
        writeup = None
        try:
            import narrate
            prob = max(prob_a, 1 - prob_a)
            pick = m.player_a if prob_a >= 0.5 else m.player_b
            opp = m.player_b if prob_a >= 0.5 else m.player_a
            _tctx = {"opponent": opp, "round": m.round, "surface": m.surface,
                     "tournament": m.tournament, "weather": m.weather,
                     "weather_effect": m.weather_effect,
                     "rating_gap": facts.get("rating_gap"),
                     "edge_size": facts.get("edge_size"),
                     "surface_note": facts.get("surface_note")}
            _tctx["surface_record"] = _surface_record_str(pick, m.surface)
            _p = {"sport": "tennis", "prob": round(prob, 3),
                  "pick": f"{pick} to win", "confidence": confidence, "ctx": _tctx}
            writeup = narrate.prose(_long_reason(_p), kind="reason", sport="tennis",
                                    llm=LLM_COMPLETE, budget={"left": 1})
        except Exception as e:
            print(f"[detail] best-bets rationale failed: {e}")
        if not writeup:
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
            # Match-winner moneyline from api-tennis (included in the plan).
            dec_a, dec_b = _tennis_odds_for(m.provider_match_id, m.player_a, m.player_b)
            if dec_a and dec_b:
                tns_odds = {"ml_a": _dec_to_amer(dec_a), "ml_b": _dec_to_amer(dec_b)}
        except Exception as e:
            print(f"[detail] tennis odds failed: {e}")

        return {
            "id": m.id, "tier": m.tier, "tournament": m.tournament, "round": m.round,
            "surface": m.surface, "player_a": m.player_a, "player_b": m.player_b,
            "event_time": m.event_time, "status": m.status,
            "best_of": m.best_of, "odds": tns_odds,
            "prediction": {"prob_a": prob_a, "confidence": confidence},
            "analysis": writeup,
            "surface_records": {
                "current": m.surface,
                "a": _player_surface_card(m.player_a),
                "b": _player_surface_card(m.player_b),
            },
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

    Load-balances two free odds sources: SportsGameOdds (SGO) covers the major
    team leagues + UFC on its own quota, so for those sports we DON'T spend a
    scarce Odds API call — we reserve the Odds API's limited monthly quota for
    the sports SGO can't do (tennis, golf, NCAA baseball, WNBA). Falls through to
    SGO below so the model-vs-market edge renders on either source."""
    book = {}
    sgo_covers = False
    try:
        import sgo_api
        sgo_covers = (sgo_api.available() and sport in getattr(sgo_api, "SGO_LEAGUE", {}))
    except Exception:
        sgo_covers = False
    try:
        import odds_api
        if odds_api.enabled() and not sgo_covers:
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


def _is_push(r):
    """True if a settled pick is a PUSH (no win, no loss): a soccer 'to win' pick
    that drew, OR any match recorded as canceled/walkover/abandoned. Pushes are
    excluded from W/L, units, ROI and CLV, and they void a parlay leg (the slip
    pays on its remaining legs)."""
    if _is_soccer_push(r):
        return True
    return str(getattr(r, "actual", "")).strip().lower() in (
        "canceled", "cancelled", "push", "void", "walkover", "abandoned",
        "postponed", "suspended", "ppd")


STALE_TENNIS_HOURS = int(os.environ.get("TENNIS_STALE_HOURS", "48"))


def _settle_stale_tennis(hours=None):
    """A tennis match that never reached 'finished' but whose start time is well
    past (canceled / walkover / postponed / abandoned) settles as a PUSH, so single
    bets and parlay legs stop hanging in 'pending' forever. Also mops up any
    finished-but-unrecorded matches in the same window."""
    from models import PickResult
    now = dt.datetime.now()
    cutoff = now - dt.timedelta(hours=(hours or STALE_TENNIS_HOURS))
    lo = now - dt.timedelta(days=14)
    pushed = 0
    try:
        with SessionLocal() as db:
            have = {str(x.ref) for x in
                    db.query(PickResult).filter(PickResult.sport == "tennis").all()}
            stale = (db.query(Match)
                       .filter(Match.scheduled < cutoff, Match.scheduled >= lo)
                       .all())
            wrote = False
            for m in stale:
                if str(m.id) in have:
                    continue
                row = _match_row(db, m)
                pw = row.get("predicted_winner")
                if not pw:
                    continue                       # not a tracked pick
                sc = row.get("score") or {}
                if row.get("status") == "finished" and sc.get("winner") in ("a", "b"):
                    _record_result(db, "tennis", m.id, pw, sc["winner"])
                else:
                    _record_result(db, "tennis", m.id, pw, "canceled")
                    pushed += 1
                wrote = True
            if wrote:
                db.commit()
    except Exception as e:
        print(f"[stale-tennis] settle failed: {e}")
    return pushed


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


def _tennis_odds_for(pmid, player_a, player_b):
    """Best Home/Away decimals for ONE match, fetched per-match from api-tennis
    (get_odds is per-match, keyed by event id), with the first value aligned to
    player_a. api-tennis Home == first player == player_a, so alignment holds by
    construction; we double-check by name when the feed carries them. Returns
    (None, None) if no odds are posted for this match yet."""
    prov = globals().get("provider")
    if prov is None or not hasattr(prov, "get_odds") or not pmid:
        return None, None
    try:
        from odds_api import _norm
        book = prov.get_odds(match_key=pmid)   # {pmid: {...}}, cached per match
        od = book.get(str(pmid))
        if not od:
            return None, None
        f, s = od.get("first"), od.get("second")
        if f and s and _norm(f) == _norm(player_b) and _norm(s) == _norm(player_a):
            return od["b"], od["a"]            # feed listed them flipped
        return od["a"], od["b"]
    except Exception:
        return None, None


def _attach_tennis_market(p):
    """Attach api-tennis market odds + de-vigged edge to a tennis pick. The price
    is aligned by matching the pick's NAME against the odds feed's own player
    names (not by list position, which can differ from the board order and was
    handing the pick the OPPONENT's price — a favorite showing +1176). Settlement
    side stays in board order so grading is unaffected."""
    from clv import american_to_prob
    from odds_api import _norm
    names = [n.strip() for n in p.get("match", "").split(" vs ")]
    if len(names) != 2:
        return
    pmid = p.get("pmid")
    prov = globals().get("provider")
    if not pmid or prov is None or not hasattr(prov, "get_odds"):
        return
    try:
        od = (prov.get_odds(match_key=pmid) or {}).get(str(pmid))
    except Exception:
        return
    if not od:
        return
    da, db = od.get("a"), od.get("b")          # 'a' aligns to feed 'first', 'b' to 'second'
    if not (da and db):
        return
    f, s = od.get("first"), od.get("second")

    def _same(x, y):
        x, y = _norm(x or ""), _norm(y or "")
        if not x or not y:
            return False
        return x == y or (x.split()[-1] == y.split()[-1])

    pick = p.get("pick", "").replace(" to win", "").strip()
    # Price alignment: prefer the odds feed's own names; fall back to board order.
    if _same(pick, f):
        dec, used = da, "a"
    elif _same(pick, s):
        dec, used = db, "b"
    elif _same(pick, names[0]):
        dec, used = da, "a"
    elif _same(pick, names[1]):
        dec, used = db, "b"
    else:
        return                                  # can't align confidently -> keep fair odds only
    other = db if used == "a" else da
    # Backstop: a model favorite handed a big-longshot price while the opponent is
    # a heavy favorite means the alignment is still wrong -> take the short side.
    if (p.get("prob") or 0) >= 0.55 and dec >= 4.0 and other <= 1.5:
        dec = other
    am = _dec_to_amer(dec)
    if am is None:
        return
    p["market_odds"] = am
    if p.get("fair_odds") is not None:
        fp = american_to_prob(p["fair_odds"])
        mp = american_to_prob(am)
        if fp is not None and mp is not None:
            p["edge_pct"] = round((fp - mp) * 100, 1)
    bside = "a" if _same(pick, names[0]) else "b"   # settlement stays in board order
    _snapshot_odds("tennis", str(p["id"]), bside, am)


_acc_cache = {"ts": 0.0, "data": None}


@app.get("/api/accuracy")
def accuracy(days: int = 30):
    """Per-sport rolling accuracy from the settled-results log (cached 2 min)."""
    import time as _t
    from models import PickResult
    if _acc_cache["data"] and _t.time() - _acc_cache["ts"] < 30 and _acc_cache["data"]["days"] == days:
        return _acc_cache["data"]
    since = dt.datetime.now() - dt.timedelta(days=days)
    _today = dt.date.today()
    by_sport = {}
    tot_p = tot_c = 0
    tot_tp = tot_tc = 0
    tot_units = 0.0
    tot_priced = 0
    alltime = {}
    at_p = at_c = 0
    with SessionLocal() as db:
        rows = db.query(PickResult).filter(PickResult.settled_date >= since).all()
        for r in rows:
            if _is_push(r):
                continue                       # draw/canceled = push, off the record
            s = by_sport.setdefault(r.sport, {"picks": 0, "correct": 0, "today_picks": 0,
                                              "today_correct": 0, "units": 0.0, "priced": 0})
            s["picks"] += 1
            tot_p += 1
            is_today = bool(r.settled_date) and r.settled_date.date() == _today
            if is_today:
                s["today_picks"] += 1
                tot_tp += 1
            if r.correct:
                s["correct"] += 1
                tot_c += 1
                if is_today:
                    s["today_correct"] += 1
                    tot_tc += 1
            if r.taken_odds is not None:                       # flat 1u staking -> ROI
                prof = (r.taken_odds / 100.0) if r.taken_odds > 0 else (100.0 / (-r.taken_odds))
                pl = prof if r.correct else -1.0
                s["priced"] += 1
                s["units"] += pl
                tot_priced += 1
                tot_units += pl
        # all-time record (no date filter), per sport and overall
        allrows = db.query(PickResult).all()
        for r in allrows:
            if _is_push(r):
                continue                       # draw/canceled = push, off the record
            a = alltime.setdefault(r.sport, {"wins": 0, "losses": 0})
            at_p += 1
            if r.correct:
                a["wins"] += 1
                at_c += 1
            else:
                a["losses"] += 1
    for s, v in by_sport.items():
        v["accuracy"] = round(100 * v["correct"] / v["picks"]) if v["picks"] else None
        v["wins_30d"] = v["correct"]
        v["losses_30d"] = v["picks"] - v["correct"]
        v["today_wins"] = v.get("today_correct", 0)
        v["today_losses"] = v.get("today_picks", 0) - v.get("today_correct", 0)
        at = alltime.get(s, {"wins": 0, "losses": 0})
        v["alltime_wins"] = at["wins"]
        v["alltime_losses"] = at["losses"]
        tot = at["wins"] + at["losses"]
        v["alltime_pct"] = round(100 * at["wins"] / tot) if tot else None
        v["units_30d"] = round(v.get("units", 0.0), 2)
        v["priced_30d"] = v.get("priced", 0)
        v["roi_30d"] = round(100 * v["units"] / v["priced"], 1) if v.get("priced") else None
    data = {
        "days": days,
        "overall": {"picks": tot_p, "correct": tot_c,
                    "accuracy": round(100 * tot_c / tot_p) if tot_p else None,
                    "wins_30d": tot_c, "losses_30d": tot_p - tot_c,
                    "today_wins": tot_tc, "today_losses": tot_tp - tot_tc,
                    "units_30d": round(tot_units, 2), "priced_30d": tot_priced,
                    "roi_30d": round(100 * tot_units / tot_priced, 1) if tot_priced else None,
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
                elif g.get("status") == "postponed":
                    # clear any bogus result a prior postponed-as-finished bug recorded
                    from models import PickResult
                    if db.query(PickResult).filter_by(sport="mlb", ref=str(g["id"])).delete():
                        wrote = True
            if wrote:
                db.commit()
    except Exception as e:
        print(f"[accuracy] mlb log skipped: {e}")
    return games


@app.get("/api/mlb/regrade")
def mlb_regrade(confirm: str = "", days: int = 4):
    """Re-sweep recent MLB days: records finished games and voids any bogus result
    a postponed/cancelled game left behind. Use to clear a postponed game (e.g. a
    rain-out) that was wrongly marked a loss."""
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes to re-grade recent MLB days"})
    today = dt.date.today()
    swept = []
    for off in range(0, max(1, days)):
        d = (today - dt.timedelta(days=off)).isoformat()
        try:
            mlb_games(d)
            swept.append(d)
        except Exception as e:
            swept.append(f"{d}: {type(e).__name__}")
    return JSONResponse({"reswept": swept, "note": "postponed games voided, finished re-recorded"},
                        headers={"Cache-Control": "no-store"})


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
    _attach_depth("mlb", g)
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
            if _is_tennis_team_event(m):
                continue
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
            tctx["surface_record"] = _surface_record_str(pick, m.surface)
            plays.append({
                "sport": "tennis", "id": m.id, "kind": "moneyline",
                "pmid": m.provider_match_id,
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
    except Exception:
        pass
    # tennis market odds come from api-tennis (included in the plan), not the
    # Odds API — keeps Odds API credits for the team sports + soccer.
    if p["sport"] == "tennis":
        try:
            _attach_tennis_market(p)
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
    _settle_stale_tennis()   # push out canceled/stuck tennis so legs stop hanging
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
                live = [(L, r) for L, r in zip(legs, rows) if not _is_push(r)]
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


@app.get("/api/parlays/rebuild")
def parlays_rebuild(confirm: str = "", date: str | None = None):
    """Clear the frozen slips for a date and rebuild them at current prices (slips
    are normally locked on first view, so use this after a pricing fix). Returns a
    preview of the new legs + odds so you can verify favorites read correctly.
    Defaults to today."""
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes to clear & rebuild this date's slips"})
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_parlay_table()
    from models import ParlaySlip
    d0 = dt.datetime.combine(target, dt.time.min)
    d1 = d0 + dt.timedelta(days=1)
    try:
        with SessionLocal() as db:
            n = (db.query(ParlaySlip)
                   .filter(ParlaySlip.slip_date >= d0, ParlaySlip.slip_date < d1)
                   .delete())
            db.commit()
    except Exception as e:
        return JSONResponse({"error": str(e)})
    built = _build_parlays(target)
    if built:
        _save_slips(target, built)
    preview = [{"name": p["name"],
                "legs": [{"pick": L.get("pick"), "odds": L.get("odds"),
                          "priced": L.get("priced")} for L in p["legs"]]}
               for p in built]
    return JSONResponse({"cleared": n, "rebuilt": len(built), "date": target.isoformat(),
                         "preview": preview}, headers={"Cache-Control": "no-store"})


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
        if ctx.get("surface_record"):
            on = surf if (surf and surf != "unknown") else "this surface"
            s.append(f"On {on}, {name} carries a career record of {ctx['surface_record']}.")
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
            if _is_push(r):
                return "push"
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
    # Force-hide sports regardless of season. Defaults to NBA + NHL (seasons over);
    # set the HIDDEN_SPORTS env var (comma-separated keys, or empty) to change.
    _hs = os.environ.get("HIDDEN_SPORTS")
    hidden = {s.strip().lower() for s in
              ((_hs if _hs is not None else "nba,nhl").split(",")) if s.strip()}
    for entry in meta:
        in_season = mo in SPORT_SEASON.get(entry["key"], set(range(1, 13)))
        entry["active"] = in_season and entry["key"] not in hidden
    # Golf is served by a dedicated leaderboard view, not the matchup registry,
    # so it isn't in sports.py SPORTS. Surface it here so the home grid shows it.
    if not any(e.get("key") == "golf" for e in meta):
        meta.append({"key": "golf", "label": "Golf", "emoji": "\u26F3",
                     "color": "#3f9b59", "team": False, "has_props": False,
                     "blurb": "Leaderboards \u00b7 projections \u00b7 matchups",
                     "active": True})
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
    # Attach real 3-way odds for the viewed league and snapshot the predicted
    # side so soccer can track units/CLV (quota-light: one league at a time,
    # cached; no-ops without an odds key, quota, or a match).
    if lg not in ("all", "today"):
        try:
            import odds_api
            if odds_api.enabled():
                sodds = odds_api.get_soccer_odds(lg) or {}
                for g in games:
                    if not g.get("odds"):
                        o = sodds.get(_norm_team(g["home"]["name"]) + "|" + _norm_team(g["away"]["name"]))
                        if o:
                            g["odds"] = {"ml_home": o.get("ml_home"), "ml_draw": o.get("ml_draw"),
                                         "ml_away": o.get("ml_away"), "books": o.get("books")}
                    if g.get("odds"):
                        sp = {"home": g.get("prob_home", 0), "draw": g.get("prob_draw", 0),
                              "away": g.get("prob_away", 0)}
                        side = max(sp, key=sp.get)
                        taken = {"home": g["odds"].get("ml_home"), "draw": g["odds"].get("ml_draw"),
                                 "away": g["odds"].get("ml_away")}.get(side)
                        if taken is not None:
                            try:
                                _snapshot_odds("soccer", str(g["id"]), side, int(round(taken)))
                            except Exception:
                                pass
        except Exception as e:
            print(f"[soccer] odds attach failed: {e}")
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


_golf_proj_cache = {}

_golf_edge_cache = {}

def _dec_to_american(d):
    try:
        d = float(d)
    except (TypeError, ValueError):
        return None
    if d <= 1.0:
        return None
    return int(round((d - 1.0) * 100)) if d >= 2.0 else int(round(-100.0 / (d - 1.0)))


@app.get("/api/golf/edge")
def golf_edge(tour: str = "pga"):
    c = _golf_edge_cache.get(tour)
    if c and time.time() - c[0] < 60:
        return JSONResponse(c[1], headers={"Cache-Control": "no-store"})
    import datagolf_api, golf_provider
    out = {"ready": False, "tour": tour}
    try:
        if not datagolf_api.enabled():
            out["reason"] = "no_datagolf_key"
        else:
            o = datagolf_api.outrights(tour, "win")
            players = (o or {}).get("players") or {}
            if not players:
                out["reason"] = "no_market"
            else:
                # map to the board (if any) for a clickable id + score
                board = golf_provider.get_board(tour)
                bmap = {datagolf_api._norm(p.get("name") or ""): p
                        for p in (board.get("players") or [])}
                rows = []
                for nm, m in players.items():
                    md, bd = m.get("model_dec"), m.get("book_dec")
                    if not md or not bd:
                        continue
                    model_p = round(100.0 / md, 1)
                    mkt_p = round(100.0 / bd, 1)
                    bp = bmap.get(nm) or {}
                    rows.append({"id": bp.get("id", ""), "name": m["name"],
                                 "total": bp.get("total", ""), "model": model_p,
                                 "market": mkt_p, "edge": round(model_p - mkt_p, 1),
                                 "american": _dec_to_american(bd), "book": m.get("book")})
                rows.sort(key=lambda x: -x["edge"])
                ev = board.get("event") or {}
                out = {"ready": True, "tour": tour, "source": "datagolf",
                       "event": (o or {}).get("event") or ev.get("name"),
                       "market": "win", "matched": len(rows),
                       "pre": not ev.get("is_live"), "rows": rows[:50]}
    except Exception as e:
        out = {"ready": False, "tour": tour, "reason": "error", "error": str(e)}
    _golf_edge_cache[tour] = (time.time(), out)
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


def _golf_pretourney_matchup(board, id_list, tour="pga"):
    """Pre-tournament 3-ball when there's no live scoring to simulate: price each
    selected player's chance to finish best of the group from DataGolf's model
    win probabilities (Harville normalization). Returns None if DataGolf isn't
    configured or doesn't have all the picked players."""
    import datagolf_api
    if not datagolf_api.enabled():
        return None
    by_id = {p["id"]: p for p in (board.get("players") or [])}
    sel = [by_id[i] for i in id_list if i in by_id][:3]
    if len(sel) < 2:
        return None
    pred = datagolf_api.pre_tournament(tour)
    mkt = (pred or {}).get("players") or {}
    if not mkt:
        return None
    ws = []
    for p in sel:
        m = mkt.get(datagolf_api._norm(p["name"]))
        if not m or m.get("win") is None:
            return None          # need every picked player in the DataGolf field
        ws.append(max(float(m["win"]), 0.0001))
    tot = sum(ws) or 1.0
    ev = board.get("event") or {}
    out = [{"id": p["id"], "name": p["name"], "pos": p.get("pos"),
            "total": p.get("total"), "prob": round(100 * w / tot, 1)}
           for p, w in zip(sel, ws)]
    out.sort(key=lambda x: -x["prob"])
    return {"ready": True, "scope": "pretourney", "source": "datagolf",
            "event": ev.get("name"), "round": ev.get("round"), "players": out}


@app.get("/api/golf/matchup")
def golf_matchup(tour: str = "pga", ids: str = "", scope: str = "tournament"):
    import golf_provider, golf_model
    id_list = [x for x in ids.split(",") if x]
    if len(id_list) < 2:
        return JSONResponse({"ready": False, "reason": "need_2"})
    board = golf_provider.get_board(tour)
    res = golf_model.matchup(board, id_list, scope=scope)
    # Pre-tournament (no live scoring): fall back to DataGolf model probabilities.
    if (not res.get("ready")) and res.get("reason") == "no_field":
        alt = _golf_pretourney_matchup(board, id_list, tour=tour)
        if alt:
            res = alt
    return JSONResponse(res, headers={"Cache-Control": "no-store"})


@app.get("/api/golf/projections")
def golf_projections(tour: str = "pga"):
    import golf_provider
    c = _golf_proj_cache.get(tour)
    if c and time.time() - c[0] < 60:
        return JSONResponse(c[1], headers={"Cache-Control": "no-store"})
    try:
        import golf_model
        board = golf_provider.get_board(tour)
        data = golf_model.project(board)
        # Pre-tournament (no live scoring): fill projections from DataGolf's model.
        if (not data.get("ready")) and data.get("reason") == "no_field":
            import datagolf_api
            if datagolf_api.enabled():
                pb = datagolf_api.pre_tournament(tour, "baseline")
                pf = datagolf_api.pre_tournament(tour, "fit")
                mb = (pb or {}).get("players") or {}
                mf = (pf or {}).get("players") or {}
                # is the course-fit model actually different from baseline?
                has_fit = bool(mf) and any(
                    (mf.get(k, {}).get("win") != mb.get(k, {}).get("win"))
                    for k in list(mb.keys())[:60])
                if mb:
                    rows = []
                    for p in (board.get("players") or []):
                        key = datagolf_api._norm(p["name"])
                        b = mb.get(key)
                        if not b or b.get("win") is None:
                            continue
                        f = mf.get(key) or {}
                        base = {"win": b.get("win"), "top5": b.get("top5"),
                                "top10": b.get("top10"), "top20": b.get("top20"),
                                "make_cut": b.get("make_cut")}
                        row = {"id": p["id"], "name": p["name"],
                               "pos": p.get("pos"), "total": p.get("total"),
                               "win": base["win"], "top5": base["top5"],
                               "top10": base["top10"], "top20": base["top20"],
                               "make_cut": base["make_cut"], "base": base}
                        fnum, flab = golf_model.estimate_finish(
                            base["win"], base["top5"], base["top10"], base["top20"],
                            base["make_cut"], len(board.get("players") or []))
                        if flab:
                            row["proj_finish"] = flab
                            row["proj_finish_num"] = fnum
                        if has_fit and f:
                            row["fit"] = {"win": f.get("win"), "top5": f.get("top5"),
                                          "top10": f.get("top10"), "top20": f.get("top20"),
                                          "make_cut": f.get("make_cut")}
                        rows.append(row)
                    if rows:
                        rows.sort(key=lambda r: (-(r.get("win") or 0),
                                                 -(r.get("top5") or 0)))
                        ev = board.get("event") or {}
                        data = {"ready": True, "scope": "pretourney",
                                "source": "datagolf", "pre_cut": True,
                                "has_fit": has_fit,
                                "event": ev.get("name") or (pb or {}).get("event"),
                                "field": len(rows), "projections": rows}
        data["tour"] = tour
        _golf_proj_cache[tour] = (time.time(), data)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"ready": False, "error": str(e)})


@app.get("/api/golf/dg-diag")
def golf_dg_diag(tour: str = "pga"):
    """Confirms the DataGolf key works and shows a few parsed players so the
    field mapping can be verified against a real response."""
    try:
        import datagolf_api
        return JSONResponse(datagolf_api.diag(tour),
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"enabled": False, "error": str(e)})


@app.get("/api/golf/dg-matchups-diag")
def golf_dg_matchups_diag(tour: str = "pga", market: str = "3_balls"):
    """Shows the raw structure of DataGolf's offered matchups (top-level keys +
    the first match) so the matchup-tracker parser can be built against the real
    field names. Try market=3_balls, then tournament_matchups if empty."""
    try:
        import datagolf_api
        data = datagolf_api.matchups(tour, market)
        if data is None:
            return JSONResponse({"enabled": datagolf_api.enabled(),
                                 "note": "no data (not enabled, or feed empty)"})
        out = {"market_requested": market}
        if isinstance(data, dict):
            out["top_keys"] = list(data.keys())
            out["event"] = data.get("event_name")
            out["round"] = data.get("round_num")
            out["market"] = data.get("market")
            ml = data.get("match_list")
            out["match_list_type"] = type(ml).__name__
            if isinstance(ml, list):
                out["count"] = len(ml)
                out["first"] = ml[0] if ml else None
            elif isinstance(ml, str):
                out["match_list_note"] = ml      # DataGolf returns a note when none offered
        elif isinstance(data, list):
            out["top_keys"] = "list"
            out["count"] = len(data)
            out["first"] = data[0] if data else None
        return JSONResponse(out, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/golf/weather")
def golf_weather(tour: str = "pga"):
    """Tournament weather (free, via Open-Meteo). Geocodes the venue city, then
    returns a per-day forecast across the event dates. Wind is the golf driver."""
    try:
        import golf_provider, golf_weather
        board = golf_provider.get_board(tour)
        ev = (board or {}).get("event") or {}
        if not ev:
            return JSONResponse({"ready": False, "reason": "no_event"})
        start = (ev.get("start") or "")[:10] or None
        end = (ev.get("end") or "")[:10] or None
        # Open-Meteo's forecast endpoint only covers today forward; clamp a start
        # that's already in the past so we still get the remaining rounds.
        today = dt.date.today().isoformat()
        if start and start < today:
            start = today
        if end and end < today:
            end = today
        w = golf_weather.tournament_weather(ev.get("city"), ev.get("state"),
                                       start, end, ev.get("venue"))
        w["event"] = ev.get("name")
        w["tour"] = tour
        return JSONResponse(w, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"ready": False, "reason": "error", "error": str(e)})


@app.get("/api/golf/weather-diag")
def golf_weather_diag(tour: str = "pga"):
    try:
        import golf_provider
        ev = (golf_provider.get_board(tour) or {}).get("event") or {}
        return JSONResponse({"event": ev.get("name"), "venue": ev.get("venue"),
                             "city": ev.get("city"), "state": ev.get("state"),
                             "start": ev.get("start"), "end": ev.get("end")},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/golf/matchup-board")
def golf_matchup_board(tour: str = "pga"):
    """Full value board of every offered matchup (model price vs best book +
    edge per player), for the rebuilt Matchups tab."""
    try:
        import datagolf_api
        if not datagolf_api.enabled():
            return JSONResponse({"ready": False, "reason": "no_datagolf_key"})
        b = datagolf_api.matchup_board(tour)
        if not b or not b.get("groups"):
            return JSONResponse({"ready": False, "reason": "no_market"})
        # attach each player's live score + decide a completed-matchup result
        import golf_provider
        from golf_tracker import _round_score as _gscore, _board_index as _gidx, _lookup as _glookup
        board = golf_provider.get_board(tour)
        gidx = _gidx(board.get("players") or [])
        for g in b["groups"]:
            rnum = g.get("round")
            scored = {}
            for pl in g["players"]:
                bp = _glookup(gidx, datagolf_api._norm(pl["name"]))
                if bp:
                    pl["total"] = bp.get("total")
                    pl["thru"] = bp.get("thru")
                    pl["pos"] = bp.get("pos")
                sc = _gscore(bp, rnum) if bp else None
                if sc is not None:
                    scored[pl["name"]] = sc
            # A group is "complete" only once every player has a finished round
            # score (the >=55 guard). Then grade the model favorite: strict low =
            # win, tie for low = push, otherwise loss.
            fav = next((p for p in g["players"] if p.get("fav")), None)
            if rnum and g["players"] and len(scored) == len(g["players"]):
                low = min(scored.values())
                winners = [n for n, v in scored.items() if v == low]
                g["complete"] = True
                if fav is None:
                    g["result"] = None
                elif len(winners) != 1:
                    g["result"] = "push"
                else:
                    g["result"] = "win" if winners[0] == fav["name"] else "loss"
            else:
                g["complete"] = False
                g["result"] = None
        ev = board.get("event") or {}
        b["live"] = bool(ev.get("is_live"))
        # Enrich each player with tournament skill (baseline win%) + course-fit
        # delta (fit model win% minus baseline) so the detail page can explain the
        # edge: quality of player vs whether the venue suits them.
        try:
            base = datagolf_api.pre_tournament(tour, "baseline") or {}
            fitm = datagolf_api.pre_tournament(tour, "fit") or {}
            bpl, fpl = base.get("players") or {}, fitm.get("players") or {}
            for g in b["groups"]:
                for pl in g["players"]:
                    nm = datagolf_api._norm(pl["name"])
                    bm, fm = bpl.get(nm), fpl.get(nm)
                    if bm:
                        pl["win_pct"] = bm.get("win")
                        pl["top20_pct"] = bm.get("top20")
                    if bm and fm and bm.get("win") is not None and fm.get("win") is not None:
                        pl["fit_delta"] = round(fm["win"] - bm["win"], 1)
        except Exception:
            pass
        try:
            from golf_tracker import record_summary
            b["record"] = record_summary()
        except Exception:
            b["record"] = None
        b["ready"] = True
        b["tour"] = tour
        return JSONResponse(b, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"ready": False, "reason": "error", "error": str(e)})


@app.get("/api/golf/dg-outrights-diag")
def golf_dg_outrights_diag(tour: str = "pga", market: str = "win"):
    """Shows the parsed outrights (model + best book per player) so the Edge
    tab's source can be verified against a real response."""
    try:
        import datagolf_api
        o = datagolf_api.outrights(tour, market)
        if not o:
            return JSONResponse({"enabled": datagolf_api.enabled(),
                                 "note": "no data (not enabled or feed empty)"})
        pl = o.get("players") or {}
        sample = list(pl.values())[:5]
        return JSONResponse({"event": o.get("event"), "market": o.get("market"),
                             "players_loaded": len(pl), "sample": sample},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/golf/tracker-settle")
def golf_tracker_settle(confirm: str = "", tour: str = "pga"):
    """Force a settle pass right now and report how many graded — or the exact
    error + traceback if it throws. The hourly background settle swallows its
    errors, so this surfaces why gradeable matchups aren't settling."""
    if confirm != "yes":
        return JSONResponse({"note": "add ?confirm=yes to force a settle pass", "tour": tour})
    try:
        import golf_tracker
        n = golf_tracker.settle(tour)
        return JSONResponse({"settled_this_pass": n, "tour": tour},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()[-1800:]},
                            headers={"Cache-Control": "no-store"})


@app.get("/api/golf/tracker-reset")
def golf_tracker_reset(confirm: str = ""):
    """Wipes golf tracking: deletes golf PickResults and re-arms every tracked
    matchup so the corrected grader re-settles only completed rounds. Use once
    to clear records that settled early. Add ?confirm=yes."""
    if confirm != "yes":
        return JSONResponse({"ok": False, "note": "add ?confirm=yes to reset golf tracking"})
    try:
        from db import SessionLocal
        from models import GolfMatchupPick, PickResult
        with SessionLocal() as db:
            n_pr = db.query(PickResult).filter_by(sport="golf").delete()
            n_mp = 0
            for mp in db.query(GolfMatchupPick).all():
                mp.settled = False
                mp.result = None
                mp.settled_date = None
                n_mp += 1
            db.commit()
        return JSONResponse({"ok": True, "deleted_golf_pickresults": n_pr,
                             "re_armed_matchups": n_mp})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/golf/tracker-diag")
def golf_tracker_diag(tour: str = "pga"):
    """Tracked/pending/settled counts + record so the matchup tracker can be
    verified."""
    try:
        import golf_tracker
        return JSONResponse(golf_tracker.diag(tour),
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/golf/board")
def golf_board(tour: str = "pga"):
    import golf_provider
    b = golf_provider.get_board(tour)
    return JSONResponse(b, headers={"Cache-Control": "no-store"})


@app.get("/api/golf/schedule")
def golf_schedule(tour: str = "pga"):
    import golf_provider
    return golf_provider.get_schedule(tour)


@app.get("/api/golf/raw")
def golf_raw(tour: str = "pga"):
    import golf_provider
    return JSONResponse(golf_provider.raw(tour), headers={"Cache-Control": "no-store"})


@app.get("/api/mma/diag")
def _mma_diag():
    try:
        import apisports_mma
        return JSONResponse(apisports_mma.diag(), headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/ufc/tale/diag")
def _ufc_tale_diag(name: str = "Ilia Topuria"):
    out = {}
    try:
        import ufcstats
        out["ufcstats"] = ufcstats.diag(name)
    except Exception as e:
        out["ufcstats_error"] = str(e)
    try:
        import apisports_mma
        out["apisports"] = apisports_mma.diag()
    except Exception as e:
        out["apisports_error"] = str(e)
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@app.get("/api/soccer/game/{game_id}")
def soccer_game(game_id: str, date: str | None = None, league: str | None = None):
    import soccer_provider
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    lg = league or soccer_provider.DEFAULT_LEAGUE
    g = soccer_provider.get_game(target, game_id, lg)
    if g:
        try:
            import espn_depth, understat
            slug = soccer_provider._SLUG.get(lg, lg)
            d = espn_depth.match_depth("soccer", g["home"]["name"], g["away"]["name"], league=slug)
            xg = understat.xg_bars(lg, g["home"]["name"], g["away"]["name"])
            if d and xg:
                d["bars"] = xg + d["bars"]
                d["source"] = "ESPN + Understat"
            elif not d and xg:
                d = {"source": "Understat",
                     "away": {"name": g["away"]["name"], "bio": []},
                     "home": {"name": g["home"]["name"], "bio": []}, "bars": xg}
            if d:
                g["depth"] = d
        except Exception as e:
            print(f"[soccer] depth failed: {e}")
    return g or {"error": "not found"}


@app.get("/api/soccer/stats/diag")
def _soccer_stats_diag(league: str | None = None):
    try:
        lg = league or "epl"
        import espn_depth, understat, soccer_provider
        esp = espn_depth.diag("soccer", soccer_provider._SLUG.get(lg, lg))
        und = understat.diag(lg)
        return JSONResponse({"league": lg, "espn": esp, "understat": und},
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
    games = _attach_odds("ncaabb", games)   # attach market + snapshot pick line (units/CLV)
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
    _attach_depth("nhl", g)
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


def _attach_depth(sport, g):
    """Attach season team depth (ESPN standings) to a team-sport game, lazy."""
    try:
        import espn_depth
        d = espn_depth.match_depth(sport, g["home"]["name"], g["away"]["name"])
        if d:
            g["depth"] = d
    except Exception as e:
        print(f"[{sport}] depth failed: {e}")
    return g


@app.get("/api/{sport}/depth/diag")
def _depth_diag(sport: str):
    try:
        import espn_depth
        return JSONResponse(espn_depth.diag(sport), headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


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
    _attach_depth(sport, g)
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
        if _is_push(r):
            continue
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


@app.get("/api/edges")
def edges_report(days: int = 90, sport: str | None = None):
    """Profitability x-ray: settled priced picks sliced by odds range and by
    sport, each with hit rate, units (flat 1u), ROI, and CLV (did we beat the
    close). This is the bet-selection tool — it shows WHERE the edge actually
    is, so you can lean into the profitable buckets and skip the rest."""
    from models import PickResult

    def _dec(a):
        return 1.0 + (a / 100.0 if a > 0 else 100.0 / (-a))

    def _imp(a):
        return (100.0 / (a + 100.0)) if a > 0 else ((-a) / ((-a) + 100.0))

    def _bucket(o):
        if o <= -250: return "1 heavy_fav (<=-250)"
        if o <= -120: return "2 fav (-250..-120)"
        if o < 120:   return "3 pickem (-120..+120)"
        if o < 250:   return "4 dog (+120..+250)"
        return "5 big_dog (>=+250)"

    def _summarize(bets):
        n = len(bets)
        if not n:
            return None
        wins = sum(1 for b in bets if b["won"])
        units = sum((_dec(b["odds"]) - 1.0) if b["won"] else -1.0 for b in bets)
        clv = [b for b in bets if b["close"] is not None]
        beat = sum(1 for b in clv if _imp(b["odds"]) < _imp(b["close"]))
        avg_clv = (sum(_imp(b["close"]) - _imp(b["odds"]) for b in clv) / len(clv) * 100.0) if clv else None
        return {
            "n": n, "wins": wins, "losses": n - wins,
            "hit_rate": round(100.0 * wins / n, 1),
            "units": round(units, 2), "roi_pct": round(100.0 * units / n, 1),
            "clv_beat_pct": round(100.0 * beat / len(clv), 1) if clv else None,
            "avg_clv_pts": round(avg_clv, 2) if avg_clv is not None else None,
            "priced_for_clv": len(clv),
        }

    since = dt.datetime.now() - dt.timedelta(days=days)
    by_bucket, by_sport = {}, {}
    with SessionLocal() as db:
        q = db.query(PickResult).filter(PickResult.settled_date >= since)
        if sport:
            q = q.filter(PickResult.sport == sport)
        for r in q.all():
            if _is_push(r) or r.taken_odds is None:
                continue
            bet = {"odds": r.taken_odds, "won": bool(r.correct), "close": r.close_odds}
            by_bucket.setdefault(_bucket(r.taken_odds), []).append(bet)
            by_sport.setdefault(r.sport, []).append(bet)

    out = {"days": days, "sport": sport or "all",
           "by_odds_bucket": {k: _summarize(v) for k, v in sorted(by_bucket.items())},
           "by_sport": {k: _summarize(v) for k, v in sorted(by_sport.items())},
           "note": ("CLV beat% > 50 and positive avg_clv_pts means you're systematically "
                    "getting better prices than the close \u2014 the strongest signal you're +EV. "
                    "Chase ROI/units, not hit rate.")}
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


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
            if _is_push(r):
                continue                       # draw/canceled = push, off the record
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
        import mlb_provider
        games = mlb_provider.get_games(dt.date.today()) or []
        if games:
            g = games[0]
            import sgo_api
            so = sgo_api.get_game_odds("mlb", g["home"]["name"], g["away"]["name"])
            out["sgo_game_odds_sample"] = {"match": g["away"]["name"] + " @ " + g["home"]["name"],
                                           "odds": so}
            try:
                out["sgo_debug"] = sgo_api.diag_game("mlb", g["home"]["name"], g["away"]["name"])
            except Exception as e:
                out["sgo_debug"] = {"error": str(e)}
        else:
            out["sgo_game_odds_sample"] = {"note": "no MLB games today to sample"}
    except Exception as e:
        out["sgo_game_odds_sample"] = {"error": str(e)}
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@app.get("/api/surface/diag")
def _surface_diag(name: str = "A. Zverev", name2: str = "V. Kopriva"):
    """Diagnose surface-record lookups. ?name=...&name2=... to test specific
    players. Shows whether they resolve and (if so) the matched full key."""
    def probe(n):
        norm = _norm_surface_name(n)
        toks = [t.strip(".") for t in norm.split() if t.strip(".")]
        abbr = (toks[0][0] + " " + " ".join(toks[1:])) if (len(toks) >= 2 and len(toks[0]) == 1) else None
        rec = _resolve_surface_rec(n)
        return {
            "input": n, "normalized": norm, "abbrev_key": abbr,
            "direct_hit": norm in SURFACE_RECORDS,
            "abbrev_hit": SURFACE_ABBREV.get(abbr) if abbr else None,
            "resolved": bool(rec),
            "matched_name": (rec.get("name") if rec else None),
        }
    keys = list(SURFACE_RECORDS.keys())
    out = {
        "players_loaded": len(SURFACE_RECORDS),
        "abbrev_index_size": len(SURFACE_ABBREV),
        "sample_keys": keys[:25],
        "zverev_like_keys": [k for k in keys if "zverev" in k][:10],
        "kopriva_like_keys": [k for k in keys if "kopriva" in k][:10],
        "wta_coverage_probe": {nm: bool(_resolve_surface_rec(nm)) for nm in
                               ("Iga Swiatek", "Aryna Sabalenka", "Coco Gauff",
                                "Elena Rybakina", "Jessica Pegula")},
        "probe_a": probe(name),
        "probe_b": probe(name2),
    }
    try:
        from apitennis import _infer_surface
        out["surface_inference_examples"] = {
            t: _infer_surface(t, tr) for t, tr in (
                ("French Open", "ATP"), ("Wimbledon", "WTA"),
                ("Porsche Tennis Grand Prix Stuttgart", "WTA"),
                ("Boss Open Stuttgart", "ATP"), ("Cincinnati Open", "ATP"))}
    except Exception as e:
        out["surface_inference_examples"] = {"error": str(e)}
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@app.get("/api/tennis/surface-backfill")
def _surface_backfill(confirm: str = ""):
    """One-time fill-in: tennis matches stored before surface inference existed
    carry surface='Unknown'. Recompute them with the same tournament inference new
    matches now use. Safe to re-run; only touches rows still marked Unknown."""
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes to run",
                             "effect": "sets surface for tennis matches currently 'Unknown'"})
    try:
        from apitennis import _infer_surface
    except Exception as e:
        return JSONResponse({"error": f"infer import failed: {e}"})
    updated, by_surface = 0, {}
    try:
        with SessionLocal() as db:
            rows = db.query(Match).filter(Match.surface == "Unknown").all()
            for m in rows:
                surf = _infer_surface(m.tournament, m.tier, m.scheduled)
                if surf and surf != "Unknown":
                    m.surface = surf
                    by_surface[surf] = by_surface.get(surf, 0) + 1
                    updated += 1
            if updated:
                db.commit()
    except Exception as e:
        return JSONResponse({"error": str(e)})
    return JSONResponse({"updated": updated, "by_surface": by_surface},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/surface/fetchtest")
def _surface_fetchtest():
    """Probe, from the running server, several URL forms for one Sackmann CSV so
    we can see which (if any) the host actually serves. Decides network-vs-URL."""
    import urllib.request as _ur, urllib.error as _ue
    repo = "tennis_wta"
    fname = "wta_matches_2024.csv"
    _tok = (os.environ.get("DATA_TOKEN")
            or os.environ.get("GITHUB_DATA_TOKEN")
            or os.environ.get("GH_DATA_TOKEN") or "")
    _ua = {"User-Agent": "linelogic-surface/1.0"}
    # GitHub's contents API with the "raw" media type streams the file directly
    # (no 1MB base64 cap) and api.github.com is essentially never IP-blocked.
    _api_h = dict(_ua); _api_h["Accept"] = "application/vnd.github.raw"
    if _tok:
        _api_h["Authorization"] = f"Bearer {_tok}"
    candidates = [
        ("raw_master",    f"https://raw.githubusercontent.com/JeffSackmann/{repo}/master/{fname}", _ua),
        ("github_api_raw", f"https://api.github.com/repos/JeffSackmann/{repo}/contents/{fname}?ref=master", _api_h),
        # --- host-reachability checks (do GitHub hosts answer Railway AT ALL?) ---
        ("api_ratelimit", "https://api.github.com/rate_limit", _api_h),
        ("github_git_refs", f"https://github.com/JeffSackmann/{repo}.git/info/refs?service=git-upload-pack", _ua),
    ]
    results = []
    for label, url, hdrs in candidates:
        row = {"label": label, "url": url}
        if label in ("github_api_raw", "api_ratelimit"):
            row["auth"] = "bearer-token" if _tok else "anonymous(60/hr)"
        try:
            req = _ur.Request(url, headers=hdrs)
            with _ur.urlopen(req, timeout=30) as r:
                data = r.read().decode("utf-8", "replace")
            row["status"] = getattr(r, "status", 200)
            row["bytes"] = len(data)
            row["lines"] = data.count("\n")
            row["first_line"] = data.split("\n", 1)[0][:200]
        except _ue.HTTPError as e:
            row["error"] = f"HTTPError {e.code}"
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"
        results.append(row)
    reachable = [r["label"] for r in results if "status" in r]
    return JSONResponse({"file": fname, "hosts_that_answered": reachable, "results": results},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/surface/rebuild")
def _surface_rebuild(confirm: str = "", start: int = 2015):
    """Build surface_records.json on Railway via the GitHub Trees+Blobs API
    (api.github.com is reachable here; raw/CDN hosts are not). Blobs come back
    base64-inline in JSON, so nothing redirects to the blocked githubusercontent
    CDN. Saves to the /data volume (survives redeploys) and hot-reloads memory.
    Append ?confirm=yes to run. Optional &start=YYYY (default 2015)."""
    if confirm != "yes":
        return JSONResponse({
            "note": "append ?confirm=yes to run",
            "effect": "fetches ATP+WTA match CSVs via api.github.com, rebuilds "
                      "surface_records.json, saves to /data, reloads in memory",
            "start_year": start})
    import urllib.request as _ur, urllib.error as _ue, base64 as _b64, csv as _csv, io as _io
    global SURFACE_RECORDS
    try:
        import build_surface_records as _bsr
    except Exception as e:
        return JSONResponse({"error": f"cannot import build_surface_records: {e}"})
    tok = (os.environ.get("DATA_TOKEN") or os.environ.get("GITHUB_DATA_TOKEN")
           or os.environ.get("GH_DATA_TOKEN") or "")

    def _api(url, accept="application/vnd.github+json"):
        h = {"User-Agent": "linelogic-surface/1.0", "Accept": accept}
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        req = _ur.Request(url, headers=h)
        with _ur.urlopen(req, timeout=90) as r:
            return r.read()

    end = dt.date.today().year
    store: dict = {}
    report = {"auth": "bearer-token" if tok else "anonymous(60/hr)",
              "start": start, "end": end, "repos": {}, "errors": []}
    if tok:
        try:
            rl = json.loads(_api("https://api.github.com/rate_limit"))
            lim = rl.get("resources", {}).get("core", {}).get("limit", 0)
            report["token_check"] = ("VALID (authenticated, limit %d/hr)" % lim
                                     if lim >= 5000 else
                                     "NOT APPLIED (limit %d \u2014 token missing/invalid)" % lim)
        except Exception as e:
            report["token_check"] = f"could not verify: {e}"
    for repo, pre in (("tennis_atp", "atp_matches_"), ("tennis_wta", "wta_matches_")):
        try:
            tree = json.loads(_api(
                f"https://api.github.com/repos/JeffSackmann/{repo}/git/trees/master?recursive=1"))
            wanted = []
            for t in tree.get("tree", []):
                p = t.get("path", "")
                if p.startswith(pre) and p.endswith(".csv"):
                    yr = p[len(pre):-4]
                    if yr.isdigit() and start <= int(yr) <= end:
                        wanted.append((p, t["sha"]))
            picked, total = [], 0
            for p, sha in sorted(wanted):
                blob = json.loads(_api(
                    f"https://api.github.com/repos/JeffSackmann/{repo}/git/blobs/{sha}"))
                if blob.get("encoding") != "base64":
                    continue
                text = _b64.b64decode(blob["content"]).decode("utf-8", "replace")
                rows = list(_csv.DictReader(_io.StringIO(text)))
                n = _bsr.aggregate(rows, store)
                total += n
                picked.append(f"{p}:+{n}")
            report["repos"][repo] = {"files": len(picked), "matches": total,
                                     "tree_truncated": tree.get("truncated", False),
                                     "picked": picked}
        except _ue.HTTPError as e:
            report["errors"].append(f"{repo}: HTTPError {e.code}")
        except Exception as e:
            report["errors"].append(f"{repo}: {type(e).__name__}: {e}")

    report["players"] = len(store)
    probe = {nm: bool(_resolve_surface_rec(nm)) or any(
                 nm.split()[-1].lower() in k for k in store)
             for nm in ("Aryna Sabalenka", "Coco Gauff", "Iga Swiatek")}
    report["wta_present"] = probe

    if len(store) >= 1500 and any(probe.values()):
        save_path = _srf if str(_srf).startswith("/data") else "/data/surface_records.json"
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
        except Exception:
            pass
        try:
            with open(save_path, "w") as f:
                json.dump(store, f, separators=(",", ":"))
            SURFACE_RECORDS = store
            _rebuild_surface_abbrev()
            report["saved_to"] = save_path
            report["status"] = "SAVED to volume + loaded into memory (Surface tab live now)"
        except Exception as e:
            SURFACE_RECORDS = store
            _rebuild_surface_abbrev()
            report["status"] = (f"loaded into memory but volume write failed ({e}); "
                                "will rebuild on next restart")
    else:
        hint = ("" if tok else " No token was set, so reads ran anonymously and Railway's "
                "IP is filtered \u2014 add a CLASSIC GitHub token as the DATA_TOKEN env var on "
                "Railway and rerun.")
        report["status"] = ("NOT saved \u2014 guard failed (need \u22651500 players AND a WTA "
                             "name present)." + hint)
    return JSONResponse(report, headers={"Cache-Control": "no-store"})


@app.post("/api/surface/upload")
def _surface_upload(payload: dict, confirm: str = ""):
    """Receive a surface_records store built in the user's browser (which can
    reach raw.githubusercontent / jsDelivr from a residential IP) and save it to
    the /data volume + hot-reload memory. Guarded so a bad payload can't wipe a
    good cache."""
    global SURFACE_RECORDS
    if confirm != "yes":
        return JSONResponse({"error": "append ?confirm=yes"}, status_code=400)
    store = payload or {}
    n = len(store)
    probe = {nm: any(nm in k for k in store)
             for nm in ("sabalenka", "gauff", "swiatek")}
    if n < 1500 or not any(probe.values()):
        return JSONResponse({"saved": False, "players": n, "wta_probe": probe,
                             "error": "guard failed: need >=1500 players AND a WTA name"},
                            status_code=400)
    # shape sanity-check on a sample
    bad = 0
    for k in list(store.keys())[:50]:
        v = store[k]
        if not isinstance(v, dict) or "surfaces" not in v:
            bad += 1
    if bad:
        return JSONResponse({"saved": False, "error": f"payload shape invalid ({bad}/50 bad)"},
                            status_code=400)
    save_path = _srf if str(_srf).startswith("/data") else "/data/surface_records.json"
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
    except Exception:
        pass
    try:
        with open(save_path, "w") as f:
            json.dump(store, f, separators=(",", ":"))
    except Exception as e:
        SURFACE_RECORDS = store
        _rebuild_surface_abbrev()
        return JSONResponse({"saved": False, "players": n,
                             "note": f"loaded into memory but volume write failed: {e}"})
    SURFACE_RECORDS = store
    _rebuild_surface_abbrev()
    return JSONResponse({"saved": True, "players": n, "saved_to": save_path,
                         "wta_probe": probe})


_SURFACE_BUILDER_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Surface records builder</title>
<style>
 body{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:18px;background:#0f1115;color:#e7e9ee}
 h1{font-size:19px;margin:0 0 4px} p{font-size:14px;color:#aab;margin:6px 0 14px;line-height:1.4}
 button{font-size:16px;font-weight:600;padding:13px 18px;border:0;border-radius:11px;background:#3b82f6;color:#fff;width:100%}
 button:disabled{background:#334}
 #log{margin-top:16px;font-size:12.5px;font-family:ui-monospace,monospace;white-space:pre-wrap;
   background:#161922;border:1px solid #232838;border-radius:10px;padding:11px;max-height:62vh;overflow:auto}
 .ok{color:#4ade80}.err{color:#f87171}.mut{color:#8b93a7}
</style></head><body>
<h1>Build surface records</h1>
<p>This runs in <b>your browser</b>, so it pulls the tennis data from your normal connection (the server can't, but your phone can). It builds the file and sends it to the app. Takes ~30&ndash;60s. Leave this open while it runs.</p>
<button id="go" onclick="run()">Build &amp; upload</button>
<div id="log"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/PapaParse/5.4.1/papaparse.min.js"></script>
<script>
const L=document.getElementById('log'), B=document.getElementById('go');
function log(m,c){const s=document.createElement('div');if(c)s.className=c;s.textContent=m;L.appendChild(s);L.scrollTop=L.scrollHeight;}
function normName(n){if(!n)return"";const d=n.normalize("NFKD");let o="";for(let i=0;i<d.length;i++){const c=d.charCodeAt(i);if(c>=768&&c<=879)continue;o+=d[i];}return o.toLowerCase().split(/\\s+/).filter(Boolean).join(" ");}
const SURF=new Set(["Hard","Clay","Grass","Carpet"]);
function titleSurf(s){s=(s||"").trim();if(!s)return"";return s.charAt(0).toUpperCase()+s.slice(1).toLowerCase();}
function bump(store,name,surface,year,won){
  const k=normName(name);if(!k)return;
  let r=store[k];if(!r){r={name:name,surfaces:{}};store[k]=r;}
  let sf=r.surfaces[surface];if(!sf){sf={career:[0,0],by_year:{}};r.surfaces[surface]=sf;}
  let y=sf.by_year[year];if(!y){y=[0,0];sf.by_year[year]=y;}
  sf.career[won?0:1]++;y[won?0:1]++;
}
async function fetchCsv(repo,fname,verbose){
  const raw="https://raw.githubusercontent.com/JeffSackmann/"+repo+"/master/"+fname;
  const urls=[
    raw,
    "https://cdn.jsdelivr.net/gh/JeffSackmann/"+repo+"@master/"+fname,
    "https://api.codetabs.com/v1/proxy?quest="+encodeURIComponent(raw),
    "https://api.allorigins.win/raw?url="+encodeURIComponent(raw),
    "https://api.allorigins.win/get?url="+encodeURIComponent(raw)
  ];
  const errs=[];
  for(const u of urls){
    const host=u.split("/")[2];
    try{
      const r=await fetch(u);
      if(r.ok){
        let t=await r.text();
        // allorigins /get wraps the body in JSON {contents: "..."}
        if(u.indexOf("/get?")>=0){try{t=JSON.parse(t).contents;}catch(e){}}
        if(t&&t.indexOf("tourney_")>=0)return t;
        errs.push(host+" badbody");
      } else errs.push(host+" HTTP "+r.status);
    }catch(e){errs.push(host+" "+(e&&e.message?e.message:e));}
  }
  if(verbose)log("    tried: "+errs.join(" | "),"mut");
  return null;
}
function aggregate(text,store){
  let added=0;
  const out=Papa.parse(text,{header:true,skipEmptyLines:true});
  for(const row of out.data){
    const surface=titleSurf(row.surface);
    if(!SURF.has(surface))continue;
    const date=(row.tourney_date||"").toString().trim();
    const year=date.slice(0,4);
    if(year.length!==4||isNaN(year))continue;
    const w=(row.winner_name||"").trim(), l=(row.loser_name||"").trim();
    if(!w||!l)continue;
    bump(store,w,surface,year,true);
    bump(store,l,surface,year,false);
    added++;
  }
  return added;
}
async function run(){
  B.disabled=true;L.innerHTML="";
  const store={};const y1=new Date().getFullYear();const start=2015;
  let total=0, tries=0;
  for(const [repo,pre] of [["tennis_atp","atp_matches_"],["tennis_wta","wta_matches_"]]){
    for(let y=start;y<=y1;y++){
      const fname=pre+y+".csv";
      const text=await fetchCsv(repo,fname,tries<2);tries++;
      if(!text){log("  skip "+fname+" (not found)","mut");continue;}
      const n=aggregate(text,store);total+=n;
      log("  "+fname+": +"+n.toLocaleString()+"  (players "+Object.keys(store).length.toLocaleString()+")");
    }
  }
  const players=Object.keys(store).length;
  const wta=["sabalenka","gauff","swiatek"].filter(nm=>Object.keys(store).some(k=>k.includes(nm)));
  log("");
  log("Built "+players.toLocaleString()+" players from "+total.toLocaleString()+" matches.");
  log("WTA check: "+(wta.length?wta.join(", "):"NONE FOUND"),wta.length?"ok":"err");
  if(players<1500||!wta.length){log("Aborting upload \\u2014 looks incomplete.","err");B.disabled=false;return;}
  log("Uploading to the app \\u2026");
  try{
    const res=await fetch("/api/surface/upload?confirm=yes",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(store)});
    const j=await res.json();
    if(j.saved){log("\\u2705 SAVED "+j.players.toLocaleString()+" players to the server. Surface tab is live now.","ok");}
    else{log("Server rejected it: "+(j.error||JSON.stringify(j)),"err");}
  }catch(e){log("Upload failed: "+e,"err");}
  B.disabled=false;
}
</script></body></html>"""


@app.get("/surface-builder")
def _surface_builder_page():
    return Response(content=_SURFACE_BUILDER_HTML, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


_FEED_BUILD = {"running": False, "report": None}


def _run_feed_build(start: int, chunk_days: int = 7):
    """Wrapper: guarantees the running flag is cleared no matter what, so a build
    can never again get stuck 'running' with no report."""
    try:
        _run_feed_build_inner(start, chunk_days)
    except Exception as e:
        import traceback
        cur = _FEED_BUILD.get("report") or {}
        cur["fatal"] = f"{type(e).__name__}: {e}"
        cur["trace"] = traceback.format_exc()[-1200:]
        _FEED_BUILD["report"] = cur
    finally:
        _FEED_BUILD["running"] = False


def _run_feed_build_inner(start: int, chunk_days: int = 7):
    global SURFACE_RECORDS
    import calendar as _cal
    report = {"start": start, "chunk_days": chunk_days, "by_year": {}, "errors": [], "calls": 0, "matches": 0}
    try:
        import apitennis as _at
        prov = _at.APITennisProvider()
    except Exception as e:
        report["status"] = f"api-tennis init failed: {e}"
        _FEED_BUILD["report"] = report
        _FEED_BUILD["running"] = False
        return
    SURF = {"Hard", "Clay", "Grass", "Carpet"}
    store: dict = {}

    def bump(nm, surface, year, won):
        k = _norm_surface_name(nm)
        if not k:
            return
        r = store.setdefault(k, {"name": nm, "surfaces": {}})
        sf = r["surfaces"].setdefault(surface, {"career": [0, 0], "by_year": {}})
        yr = sf["by_year"].setdefault(year, [0, 0])
        sf["career"][0 if won else 1] += 1
        yr[0 if won else 1] += 1

    # Deep base: start from the committed Sackmann ATP file (full career history),
    # then overlay the feed (which adds WTA + any players the base lacks). Read the
    # repo/app copy explicitly, NOT /data (that's the previous feed output).
    base: dict = {}
    _basedir = os.path.dirname(os.path.abspath(__file__))
    for p in ("surface_records.json", os.path.join(_basedir, "surface_records.json"),
              "/app/surface_records.json"):
        try:
            with open(p) as bf:
                cand = json.load(bf)
            if isinstance(cand, dict) and len(cand) > len(base):
                base, report["base_file"] = cand, p
        except Exception:
            continue
    report["base_players"] = len(base)

    def _grab(d0, d1):
        return prov._call("get_fixtures", date_start=d0.isoformat(), date_stop=d1.isoformat())

    today = dt.date.today()
    if start < 2010:
        start = 2010
    span = max(1, min(31, chunk_days))
    cur = dt.date(start, 1, 1)
    empty_streak = 0
    try:
        while cur <= today:
            cend = min(cur + dt.timedelta(days=span - 1), today)
            try:
                rows = _grab(cur, cend)
                report["calls"] += 1
            except Exception as ex:
                rows = []
                # A timeout means the whole range is unreachable (e.g. a year not
                # in the plan) — don't waste 7x20s on day-by-day. A 500 is usually
                # a size issue, so a day-by-day retry is worth it there.
                if "timeout" in (type(ex).__name__ + str(ex)).lower():
                    report["errors"].append(f"{cur:%Y-%m-%d}: timeout (range skipped)")
                else:
                    d = cur
                    while d <= cend:
                        try:
                            rows += _grab(d, d) or []
                            report["calls"] += 1
                        except Exception as e2:
                            report["errors"].append(f"{d:%Y-%m-%d}: {type(e2).__name__}")
                        d += dt.timedelta(days=1)
            n = 0
            for fix in rows or []:
                if not fix.get("event_winner"):
                    continue
                win = _at._winner(fix.get("event_winner"))
                if not win:
                    continue
                pa = (fix.get("event_first_player") or "").strip()
                pb = (fix.get("event_second_player") or "").strip()
                if not pa or not pb or "/" in pa or "/" in pb:
                    continue
                tier = _at._classify_tier(fix)
                if tier not in ("ATP", "WTA"):
                    continue
                ds = (fix.get("event_date") or "").strip()
                year = ds[:4] if (len(ds) >= 4 and ds[:4].isdigit()) else str(cur.year)
                try:
                    when = dt.date.fromisoformat(ds)
                except Exception:
                    when = cur
                surface = _at._infer_surface(fix.get("tournament_name") or "", tier, when)
                if surface not in SURF:
                    continue
                w = pa if win == "a" else pb
                l = pb if win == "a" else pa
                bump(w, surface, year, True)
                bump(l, surface, year, False)
                n += 1
            if n:
                yk = f"{cur:%Y}"
                report.setdefault("by_year", {})[yk] = report["by_year"].get(yk, 0) + n
                empty_streak = 0
            else:
                empty_streak += 1
            report["matches"] += n
            report["players_so_far"] = len(store)
            _FEED_BUILD["report"] = dict(report)  # live progress for polling
            # If we're deep into a range with zero matches found, the data isn't
            # there (e.g. a start year before your plan's history) — stop grinding.
            if report["matches"] == 0 and empty_streak >= 8:
                report["aborted"] = (f"no matches in first {empty_streak} chunks from "
                                     f"{start} \u2014 that history isn't in your api-tennis plan; "
                                     f"try a later &start=")
                break
            cur = cend + dt.timedelta(days=1)
    except Exception as e:
        report["errors"].append(f"loop: {type(e).__name__}: {e}")

    # Overlay: keep every deep base (Sackmann ATP) record; add feed players the
    # base doesn't have (all WTA + any new entrants). No double-counting of ATP.
    report["feed_players"] = len(store)
    if base:
        added = 0
        for k, v in store.items():
            if k not in base:
                base[k] = v
                added += 1
        report["feed_added_to_base"] = added
        store = base
    report["players"] = len(store)
    probe = {nm: any(nm in k for k in store) for nm in ("sabalenka", "gauff", "swiatek")}
    report["wta_present"] = probe
    if len(store) >= 200 and any(probe.values()):
        save_path = _srf if str(_srf).startswith("/data") else "/data/surface_records.json"
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
        except Exception:
            pass
        try:
            with open(save_path, "w") as f:
                json.dump(store, f, separators=(",", ":"))
            SURFACE_RECORDS = store
            _rebuild_surface_abbrev()
            report["saved_to"] = save_path
            report["status"] = "DONE \u2014 saved to volume + loaded into memory (Surface tab live now)"
        except Exception as e:
            SURFACE_RECORDS = store
            _rebuild_surface_abbrev()
            report["status"] = f"DONE in memory; volume write failed ({e})"
    else:
        le = getattr(prov, "last_error", None)
        report["status"] = ("DONE but NOT saved \u2014 too few players / no WTA found."
                            + (f" api-tennis last_error: {le}" if le else ""))
    _FEED_BUILD["report"] = report
    _FEED_BUILD["running"] = False


@app.get("/api/surface/build-from-feed")
def _surface_from_feed(confirm: str = "", start: int = 2024, chunk: int = 7, force: str = ""):
    """Plan B: build surface_records.json from the api-tennis feed (which Railway
    reaches) instead of GitHub. Pulls finished ATP+WTA singles from ?start=YYYY
    (default 2024) to today in small date-chunks, infers surface from the
    tournament name the same way the board does, aggregates per-player W/L, saves
    to /data and hot-reloads. Background; poll /api/surface/feed-status.
    &force=yes clears a stuck/hung previous run."""
    if confirm != "yes":
        yrs = max(1, dt.date.today().year - start + 1)
        return JSONResponse({"note": "append ?confirm=yes to run",
                             "start": start, "approx_calls": yrs * 12,
                             "effect": "background build from api-tennis finished singles -> /data"})
    if _FEED_BUILD["running"] and force != "yes":
        return JSONResponse({"status": "already running",
                             "tip": "if it's stuck, add &force=yes to clear and restart",
                             "poll": "/api/surface/feed-status"})
    _FEED_BUILD["running"] = True
    _FEED_BUILD["report"] = None
    import threading
    threading.Thread(target=_run_feed_build, args=(start, chunk), daemon=True).start()
    return JSONResponse({"status": "build started in background"
                                   + (" (forced over a stuck run)" if force == "yes" else ""),
                         "poll": "/api/surface/feed-status",
                         "note": "refresh feed-status until running=false"})


@app.get("/api/surface/feed-probe")
def _feed_probe(date: str = ""):
    """Test ONE api-tennis get_fixtures call in isolation, wrapped so it can't hang
    the request. Tells us if the calls work, error, or hang — which is what's
    been stalling the background build."""
    import time as _t, threading
    d = date or (dt.date.today() - dt.timedelta(days=2)).isoformat()
    out = {}

    def _do():
        try:
            import apitennis as _at
            prov = _at.APITennisProvider()
            out["req_count"] = getattr(prov, "_req_count", None)
            out["daily_max"] = getattr(_at, "_DAILY_MAX", None)
            t0 = _t.time()
            rows = prov._call("get_fixtures", date_start=d, date_stop=d)
            out["seconds"] = round(_t.time() - t0, 1)
            out["rows"] = len(rows or [])
            out["finished"] = sum(1 for f in (rows or []) if f.get("event_winner"))
            out["sample"] = [{"t": (f.get("tournament_name") or "")[:28],
                              "p1": f.get("event_first_player"),
                              "p2": f.get("event_second_player"),
                              "w": f.get("event_winner")} for f in (rows or [])[:3]]
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
            out["last_error"] = getattr(locals().get("prov", None), "last_error", None)

    th = threading.Thread(target=_do, daemon=True)
    th.start()
    th.join(25)
    if th.is_alive():
        return JSONResponse({"date": d, "result": "HUNG \u2014 the call did not return in 25s; "
                             "the deployed apitennis._call has no working timeout",
                             "partial": out}, headers={"Cache-Control": "no-store"})
    return JSONResponse({"date": d, **out}, headers={"Cache-Control": "no-store"})


@app.get("/api/whoami")
def _whoami():
    import threading
    return JSONResponse({"pid": os.getpid(),
                         "threads": threading.active_count(),
                         "feed_running": _FEED_BUILD["running"],
                         "feed_report_null": _FEED_BUILD["report"] is None},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/surface/feed-status")
def _feed_status():
    return JSONResponse({"running": _FEED_BUILD["running"], "report": _FEED_BUILD["report"]},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/tennis/settle-stale")
def _tennis_settle_stale(confirm: str = "", hours: int = 0):
    """Force-settle canceled/stuck tennis (start time past the staleness window,
    not finished) as PUSHES right now, so hanging single bets and parlay legs
    clear immediately. ?confirm=yes to run; optional &hours=N overrides the
    window for this run."""
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes to run",
                             "window_hours": STALE_TENNIS_HOURS,
                             "tip": "use &hours=24 to be more aggressive for old stuck bets"})
    pushed = _settle_stale_tennis(hours if hours and hours > 0 else None)
    try:
        _settle_parlays()   # re-grade slips now that legs settled
    except Exception as e:
        return JSONResponse({"pushed": pushed, "parlay_regrade_error": str(e)})
    return JSONResponse({"pushed_as_canceled": pushed, "parlays": "re-graded",
                         "window_hours": (hours or STALE_TENNIS_HOURS)},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/surface/reset-volume")
def _surface_reset_volume(confirm: str = ""):
    """Undo the last feed build: delete /data/surface_records.json so the app falls
    back to the committed surface_records.json (your deep Sackmann ATP file, which
    has real grass). Reports how many players load from the committed file so you
    can see what the base actually contains."""
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes to wipe the volume file and "
                             "revert to the committed surface_records.json"})
    path = "/data/surface_records.json"
    existed = os.path.exists(path)
    try:
        if existed:
            os.remove(path)
    except Exception as e:
        return JSONResponse({"error": f"could not delete {path}: {e}"})
    src = _load_surface_records()      # reloads from committed file now that /data is gone
    return JSONResponse({"deleted_volume_file": existed,
                         "now_loaded_from": src,
                         "players_loaded": len(SURFACE_RECORDS),
                         "note": "this is your committed base; rebuild WTA on top with "
                                 "/api/surface/build-from-feed"},
                        headers={"Cache-Control": "no-store"})


# ---- favicon / app icons (embedded so they survive a forgotten git add) ----
import base64 as _b64
_FAVICON_ICO = _b64.b64decode("AAABAAMAEBAAAAAAIADhAgAANgAAACAgAAAAACAAjwcAABcDAAAwMAAAAAAgAJgLAACmCgAAiVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACqElEQVR4nH2TTUhcVxiG33PujzPpzB1CkiEbx2gLdjFpframriwkOBuhCQhu7ET3uhKXOgEp4q6LbkIMLly5SKBINy2lhARjzEKTgNEYpyWRQhjGBqKee58uxjEpJPngLD7O9/4s3ldJkhDHMUmSALC8vMzw8DAdHR14nofnebS3tzM0NMTS0hIAH2IUxzFxHOOcY3x8nHQ6jaSPvlQqxdjYGAcHBzRxcodLf3//0WEQBFhrkYQx5shJ8//a1as453DOIYCpqakj4KfUm2RB2Li5UbkBgDY3N4miCGstxhjCMGRhYYFyuYwkrly5zOLiIvl8HhlhjcVaS3Q8y/r6c+zc3Jzq9bqstQLU1tamvr4++YEnY6VSqaSenh7t7tYlJJuSvvsxr939Xc3evC11f9vdsHZov7e3Fxc7ip3fIIn79x5w7/6fSKIl8vj6+4jT59JIhktdXfjb1W0BkiQZqburW/FbtP/Va315LiXn7+nx709kM1JHKafqb3X9+8pJkrarVfk6nNjFEtLOux1d/3lAr3feKJ86qbk/ftLzf57oTOkLbfxS034tkfWMkrghaguFgowxsikr/2ygV+m/deLNadUfOrWaomauz+pd1dNfv76VqxkZaySMjDEqFApSpVJBEsfOZ2jtagUHAI9WHnFr9hYAL9erbG2+5M7dO1hrjzIyMTGB2XqxxYWLF1Sr1XQqd0oD5QG1hC3a29vTxsaGOjs7FYS+wrBFz54+0/z8vKy1ymQyeryyIgHMzMwgCevZzwbpw7BNT083guRcw3O5/EODxFrCMMT3fTzPw/d9giAgDEOMMUhicHCQJEk4cO59mQAqlQq5XO6T6lEUMTk5CfC+TM1qNknW1tYYHR2lWCySzWbJZrMUi0VGRkZYXV39HzhJEv4DnLzkQLLwKnQAAAAASUVORK5CYIKJUE5HDQoaCgAAAA1JSERSAAAAIAAAACAIBgAAAHN6evQAAAdWSURBVHictZdbbFTXFYa/vc+ZwbdiMOACjoNxbGxoIciXAHYDQi6y5RKRSkSoKhFPtXgiSER5qVqjpCqFlLZPUVOEGqL2Ic0FVXUJtBKXKICh+JII22CD7T6Y2GB7fBszM2f2Xn0Yz9RjjyltlS0tjc7Zc9a/rnv/S1lrhRRLRLDW4rouAAMDA1y5coWLFy9y69Yt+vv7mZqaQkTIysqiYM0ayisqqKmpYfv27eTn5wMQjUbRWqOUSgUD1lqZK8YYiZqoiIj09/fL60del5KSEgGeSoqLi+Xw4cPS29srIiLGGDHGzMOx1so8AzzPExGRaDQqJ0+elBUrViQUO44jjuOIUmoeqFIqsR9/l5OTIydOnJBIJCIiIp7nPdmAaDTm9fDwsOzduzehyHVd0Vo/dQS01uK6buJ5z549MjQ0lHAspQFRzxOxIkODg7Jt2zYBxOfzpfT2aUUpJT6fTwCprKyUgYGBeZEgnnNrjExMTEh1dXUC/H8FnitxXVte2CKBQCBRZ9ZawVgrXiQixhhpaGhYENxxHHFdNynHc0Puuu6CEYvrPHDggESjUfE8T4wxgjdTIGfPnk2A/D9hXzAdxNKhtZYPPvggkQpljZFQOExlZSUdHR1orbHWJrWqAva+8gp5eXk8Dj3m448/YfjRIxxHY4ylvr6edevWYYyhqamJvr4+lFKI/PuIUQ4oNNZYSktKuNXSQkZGBoiInDlzJhHmuUUESFZWloyMjIiIyMOHDyU/Pz/RHY7W8o+bNyW+ysrKkr6dK/70WHecOnVKRETwPE9qa2tFKZXUOvHcArJx40aZmpwUz/PkwoULokDcGWPz8vKkq6tLPM+Tnu4eSU9PTzJAaQSFFNdnyt4/rpKV384QpZR8d1eNRCIRcbu7u2lvb0dEMMYkh37m+Fy/fj3paWlo16Wvtw8BXNclagx5q/PIz8/HdV3u994nFAolwq80iAWloOqN5SzKUjzqCSEitLd/wZ07d3Db2toYGhrCcZz5BmgNxrBhwwb0zJ3Q2dmBUqCcmHEFBQVkZmbO7HUiIjiOg8UgBtKWOFT/ZBnXfz3Kg5uPESM4WjP8aJi21lbc1rZWFlwzRVS6vhQAz/PovNOJCISmoygN60qKY94qRVdnJ0CsOCOGjFyH6p+uoO/8FPeaphJqHUcD0NLaiu7v65/BSr4UlVIYY0hPT2ftmrUYawiGg9xu7SJ9ucMz30lDLGwpr0IpRcSL0N3Tg9IQiRi+8azL1sZc7nw4wb2mKbQbS8VsrL6+PtzR0dGUziulsNaycuUqclfl4miH+239TOgRXjy5krXbs2j+5SC3uq+y46sawnqaB4MPEAvLn0/jWw2L+fLdAMNfhlCuwkZlHsZYYAx3oehrpbHK8lzxWgqeLeDy3y5z/LOjFL+Wzb2Pgtw4Osyy0jT+4P851373Zyp1PQ/++RW5W9Mo/eES2n8zwnhPGOWApACPeQnu0pylKfdECxjIVks4erqRt6++RdbjTMYvhAkHPACK0jfT2PBjfnv9Z3wY+RV1Tc8wPWK4cmiQ6aEo2lFYswA4kJOTgy4sKEyEPG4VCoxn8Of6ubD0PG9/9gve3HGcF4ZqCAc80jPSUArKX9zES3te4pM3mqkJ/4h7fw/Q+c4k00MGx6cRmxo4jlVQUIBbVlaWFBIk9ptTt4xFm/1sts/z5vfeoqK6gj+9+1EsOkYQgZLnSjDW4MuBqbEpvnhnIvaxEoy3sOfx7iorK0N1dXXJzp07GRwcBA3+XD8rfvBNvHGPyb+M0/ZpKyXlpQTGRrl+rZmpqSkyMjJ4//0zvPzy99m3bx9exOPqtasExgOx2olaHNfh9u3bNDY2zmBKwnsRITc3l0uXLkE0GpX6+voYpdJaMtZnSnbVUgGkqLBIjBhJtQ4dOiRXP/885V58vff79+bdMfHbtra2VjzPE9dxHF7d/yrnzp0DrZnuCgJBlFaszl9N1+0u7EwyrbUopQgGg9y9e5fg9DQdHR2JEzTupTEGv9/PuU/PpYi+ICLs378f13VRxhjxPI+qqipaW1txXAdjDVhIS0sjMzMzcdLFQ2iMIRgMkpWVtSDd1lozNjaG53lJ76y1bNq0iRs3brDI74c4Cz5//rz4fD7xuT5RfA2EZIYfuj5X/trUFCMkkUiME8aNOHLkSOzO9vsTHy0k/2l/Lh+IU7LDrx1OsCEzm5QaYyQcDsvu3bu/NlJaV1cnoVAoaVBh9kxgjJGxsTGpq6tLVOx/Mw/MFa21uL4Yydm1a5cEAoHY1DVrNkgaTIyJtVwoFJKDBw8mFD1pIkqVa611Uus1NDRIcHr6yYPJ7NHMWitxrlhUVJTkkeM4orWel+/Ze/H/FxYWyunTp2OHwizdTzQgHol4YY6Ojsrx48eloqJiHmdMJY7jSHl5uRw7dixBZOMzQCostdB4DrHR2ufzATA5OUlzczOXL1+mpaWF3t5exsfHAVi8eDGFhYWUl5ezY8cOtm7dSnZ2dkKH4zgLQfAv6Bd3U2jdz9EAAAAASUVORK5CYIKJUE5HDQoaCgAAAA1JSERSAAAAMAAAADAIBgAAAFcC+YcAAAtfSURBVHiczVprbFTHFf5m5q6Nd21skB0Dxk+WLODaxsFYBCtFqYAIlFJoSFRo0qA2JSFS05eUiASJSEQqRQn5A6hKTWJanAoUogRCHhUpJIBMiHGIvcamGLO2axsb8/C6u168d+b0x33Ya68fSx70SEfavXfuzHe+OWfumTOXKaUIMQgRQUoJh8NhX/P5fDh16hSqqqpQW1uLFp8PN27eRH9/P4gICQkJmDp1KrKzslBQWIjFixejrKwMeXl5dh/hcBhCCDDGYoEDKKVoohoOh8mS3t5e2lexj1asWEHJyckEICadPHkyLV++nPbu3Us3btyw+w2HwxPGo5SiCRkgpbTB+3v9tH37dsrKyooAxDknIQRxzokxNgIwYyyizdB7GRkZtG3bNrp58yYREem6TlLKb8cAXddtdg4ePEhut9seWAhBQoiogMdTxpj9vHUtJyeH9u/fb4+n6/o3M8Bm3e+nJ5980h5I07Q7Aj2WMZqm2f/XrVtnu9V4LjWqAeEBA/ylS5eoqKjIZnz49H+byjm3DZk7dy7V19cbRgwMxGaAxXxNTQ1Nnz7dZv27Aj5crbFSU1PpzJkzYxoRYQApRXrY8Hmv10tpqanfO/ih8QWAUlJSqOZczajuFGGAruukpKKuri7KycmJ6OhuqDX2jBkzqK2tbRBjVAPMpVIpRUuXLr1rzI/mTmVlZRQOhykcDkcssRju9zt27CAA5HA4xu2ccx6hY61MsbQdrhaWrVu3jnAlDJ2WS5cuUUJCwh2v7d+VWu8Mh8NBXq/XcKWwgVkDABCBMYbNmzejv78fmqaBiDCaMMZARMjKzERySgqUUuCco6/PD5+vxb4/tL3H44HD4QCZY3V2dqKnp2dE22hiPGPkSy88/zw+OHoUsFIm3XSd6upq+3U/EUYA0OnTp4mI6Pbt20REtHv37gi/5ZzZQRgMBu23KxHRo48+On6cMRAThgIgIQxsp0+dtl2JW7bv3LkTRATO+ZhsWIwlJSXB7XZbFAEAvvrqq2Ftjb7cs2YhISEBUkpwzqGUQn19PQBAKTUG9QBJQ7nG7P5e2/laxPRQZ2cnuVwuYkPYHU2tGSooKCApJUkpbVbvX7TIZEpEsPvMM8+QUopCoRAREXW0d5DL5YyYzRGzLECuaYLyljrpJ+XTKOdBsz1nNGnSJGptbSUiIg4AH374IQKBAIQQ4/qjNUNz5swB5xxS1yGEgN/vR9PlywYpw1gtKiqK8PWmpiYEAkFwzqOOxwQD1xgW/zEVjx/ORO6DTnTX3wYAaEIgFArhyJEjBh4AOHr0qLGRiGEzUVBQAGDQBXxXfCOC0ro3b968iGe99d4IMiLAc4AkQd4miAQGf7fE5RNBBLulcU8Zi8BHH31kGBQMBFFdXQ0iGtsfTbHAWQZY/DU0NoCIIISAlBKMMSil4HQ64Z5lxAo3Caqrq4vatwEQ4HEMqyuno69N4a0lrUjOEXYbpRSICDU1NfD7/dAaLzaivb3dADOOAYwxOxA9Ho99DQDqamsH/7PBYM/Ozkb6tHTDOM1Yteu93ggyAIAJI1jjJ3OsPpiBq9UhfLblGgCgt8UkTwFkUna1sxMXLlwAb2hosEGN7f2DS296ejqysrIMVk03qDNBGSMBXBjXPR4PhBDQzVgJBAK41NQUYQATDCQB1zQNPz2ciSv/DOKzLdfAhDErwz1bcA5FZBjQ3NwcweSYBphg3W43XC4XpJTQNA26rqOxsRFMAFJXcCRyyDCBCYb8/HmQUkJJCQBoaWlBV1eXyagC1xhIEpJnxeHHh2airqIXZ3deN6+brA9n1sR6pbkZ3OpsImIZmZ+fDwDQdR1KKXR0dqCtvRUkgfv+MBVPVOUi7yEXSBJKikshhICCUc24ePEilFJGBUJjUDohdX48Hto/A1/uuAFvxS1w8/p40tXdDS0QCEzYAEsKCgqglIKUOuLj49F0oRn9gRDcj03BD56eijgnw+JX0jDtgUnYVfka9P8yPPLzVWACqPPWgQkGEccx0C8xY4kLi7ffg1MvdKHj8+CEwQNAMBCEFgtwa5Xy3OsB5xxOpwvv/f0wtr29BYvKM5DodODY0x0AEfp8Ydy7LhFi3Tm8UvME3vxZKX7/+Es4X10LkoSB/jByViVhwQtpOLGpAz3nQzGBBwAwQHO5XBNray2LLifKHliM5ror2LL9JZxM+Rjpv3Si+/0QzlS2RzzTsnsSipcV4cbss+h7rAqbT69FS8Nt5K5MRFJeHHLXTMaxX/wHvZcHYgcPIDExEVp6evqEDQAHMu7JwK4/78Ff6nYh8EAP4jvi0fibmwh0BY3Y4oAmNOhhHUtX/RBvV/wDf91VgYqKXVALm/Cjv6VhSk4c+m9JvPOgD8GrOpiIHTxgrIbcKu+NmUJw8wUiCVdFJ17Fn3Cr+DpSj2Xh+amvIIVNARPGm3zoyuGZPQfQgF//bgOOHTiN5T3P4V+/bUevT+H87hsIXtXBhbEKxSIW1tzcXKDmXI2dfLGo6ayZbHHQjI3TKWvvTLrnsTR69lfP0rW2Hurq6aK4uDjSNI0cDgdpmkZxcXEEgA69c4iIiPr6+oiI6MgHh4lBkDMpgQTTyOHQSNMGNZb9N+ecvvjiC0IgEKDs7OyITNMCb/1OLEmiOW/OocytmXT/0vvp86Of29WzioqKUQeo93qJiCgU6ielFD311FPfeHfGzex15syZ5Pf7SXM6nSgtLUVra6udq4MZzbUUgWmbMpDgceLqW1249dlNvH/2PRQvvA+h/n7ExcejsKAAr7/+up0DCSHQ0uJDZeXbyM7JAWDEBAPD6tWrkZ+fb7wXhqQtSikITaD261rs3bt3EEc0bxYCJCVKSkqQlJRk7Af27ds3mMczg32RpJH7jbk047mZxOOMmUmekkxd3V32PkApRdHkgyNHaH5RERERySFlkPFkz5494+7SNNPNysvLiYiMPfHKlSuRnJyM3t5ecM6gFIEJoP3VVvT/OwDOOBhnyJqZhSkpUyDNtMAKKOu/rutwOBz4+JNPkJuXB13XoYfDdhKnlIrKrHXecOL4iaisW8IYg1QKiYmJePjhh43Z1cM6UlNTsXbtWnP6BJTSod8ylHEGLjj0sI6SkpKIg43RpKGhAatWrYKmadC0ib8rGy822oZGEyspXLNmDdLT06HrOpge1kkIjvr6C5hfPN/OtwlkxIKC7ZOFhYVYsGABpFTgPHryxxjDu+++i+LiYuTm5kIpNW6iyBjDwMAADh06hFAoFL0NBpPJc+fOYf78+ZC6bpzQWEWtDRs2jF8puEtqYVq/fr1R3TCLW8wMMLtWk5+fj76+PgAENez4jHM+btUCgL0jm0jboaLretTrjDEwxuB0OlHv9SIzMxPKqqAMLy2Wl5cTMLHS4velFpY9e/bY9aCo1WnLiPXr1//fGOEwXWftI2tHgB9hgFXjCQQCVFJSctfjwRq7qKiI+vx+4/Bv1PL6sEO9zs5O8ng8ESzcDfDuWW5qa2szAjfKoV/UIybrlKatrY0KCwpsd/q+KtaW686bO498Pl9U1xn/kM+Mh+vXr9OKFSsIGCxzf1fAhRB2srZs2TLq7u4eE/yYBgx1JyKil19+2Qb/bZ9WWgfgFkkvvvgiSSkj1vs7MkApRVKXdmdVVVW0ZMmSwYHN8907McY6Uh36bFlZGZ08edJIAqUcEbB3ZIDtUgOD30kcOHCAFpmV6OEuYM2OdYxkqcVyNBdcuHAhVVZW2v3H8r1ETB97WN8wWHL8+HHatGkTzZ49O+YAd7vdtHHjRvr000/t/qwxYsHE1PB8YQIipYTgAsxM6ILBIM6fP4+zZ79Ebe3XaG5uxrVr12DVnJxOJ9LS0pCbm4vCwkKUlpaiuLgYVkXESsmFEKOOOZr8D1YLs7eIw1oFAAAAAElFTkSuQmCC")
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
