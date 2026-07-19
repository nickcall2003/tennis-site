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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, Request
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
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
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
    if not loaded:
        # Phone-friendly fallback: a re-downloaded build is often saved as
        # ratings_8.json / ratings_9.json (the browser's de-dup suffix), and
        # renaming a 20MB file on a phone is painful. Load the NEWEST ratings*.json
        # committed to the repo so the exact filename never has to be fixed.
        try:
            import glob
            for _c in sorted(glob.glob("ratings*.json"), key=os.path.getmtime, reverse=True):
                loaded = engine.load_ratings(_c)
                if loaded:
                    print(f"[predictions] loaded ratings from {_c}")
                    break
        except Exception:
            pass
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
        try:
            _reinfer_tennis_surfaces()   # correct stale/wrong stored surfaces + re-predict
        except Exception as _se:
            print(f"[surface] startup reinfer skipped: {_se}")
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

    # Loud check: are accounts/bets on durable storage? Surfaces the #1 footgun
    # (SQLite on Railway's ephemeral disk) in the deploy logs immediately.
    try:
        import os as _os
        from db import DATABASE_URL as _DBU
        if _DBU.startswith("postgresql"):
            print("[storage] OK: external Postgres \u2014 data persists across redeploys.")
        else:
            _p = _DBU.replace("sqlite:///", "")
            _mounted = _os.path.ismount(_os.path.dirname(_p) or ".") or _os.path.ismount("/data")
            if _mounted:
                print(f"[storage] OK: SQLite on a mounted volume ({_p}) \u2014 survives redeploys.")
            else:
                print("=" * 70)
                print("[storage] !!! WARNING: SQLite is on EPHEMERAL disk at "
                      f"{_p}. Accounts and bets will be ERASED on every redeploy.")
                print("[storage] Fix: attach a Railway Volume mounted at /data, OR set "
                      "DATABASE_URL to a managed Postgres (Neon/Supabase/Railway).")
                print("=" * 70)
    except Exception as e:
        print(f"[storage] durability check failed: {e}")

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
                    # ONE odds request for the entire day's slate (ITF included) —
                    # not one-per-match. Keeps tennis odds at ~1 call per cycle no
                    # matter how many matches, so ITF adds no odds cost.
                    day_book = {}
                    try:
                        prov = globals().get("provider")
                        if prov is not None and hasattr(prov, "get_odds"):
                            day_book = prov.get_odds(day=dt.date.today()) or {}
                    except Exception:
                        day_book = {}
                    for p in tennis:
                        if str(p["id"]) in recent:
                            continue
                        try:
                            _attach_tennis_market(p, book=day_book)
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

    # Umpire-tendency builder — folds new MLB finals into the per-home-plate-ump
    # run/strikeout profile, computed from the free statsapi. Incremental, so
    # after the first backfill each daily cycle is cheap. Disable: UMP_STATS=0.
    if USE_REAL and run_bg and os.environ.get("UMP_STATS", "1") == "1":
        def _umpire_bg():
            import time as _t
            _t.sleep(200)   # let startup settle + pass healthcheck
            while True:
                try:
                    import umpire_stats
                    for _ in range(6):          # chip through any backlog
                        r = umpire_stats.refresh(days=75, max_games=120)
                        if not r.get("more_pending"):
                            break
                        _t.sleep(20)
                    print(f"[umpire] {umpire_stats.summary()}")
                except Exception as e:
                    print(f"[umpire] bg error: {e}")
                _t.sleep(int(os.environ.get("UMP_STATS_SECS", "86400") or 86400))
        import threading as _thr_um
        _thr_um.Thread(target=_umpire_bg, daemon=True).start()

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
    if run_bg and os.environ.get("GOLF_TRACKER", "0") == "1":
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

    # ---- prop auto-logger -------------------------------------------------
    # Sweeps the whole slate on a loop so EVERY game's props get logged (not just
    # the ones someone opens), then grades them once games finish. This is what
    # makes the prop model-quality record a complete, unbiased sample.
    if run_bg and os.environ.get("PROPS_AUTOLOG", "1").strip().lower() not in ("0", "false", "no"):
        def _props_sweeper():
            import time as _t
            _t.sleep(120)          # let startup settle first
            while True:
                try:
                    res = _autolog_props()
                    if res.get("logged"):
                        print(f"[props] autolog {res['logged']}")
                    # grade anything that has finished (yesterday included)
                    g = _settle_props(days=3)
                    if g.get("graded"):
                        print(f"[props] graded {g['graded']}")
                except Exception as e:
                    print(f"[props] autolog sweep failed: {e}")
                _t.sleep(int(os.environ.get("PROPS_AUTOLOG_INTERVAL", "14400")))
        import threading as _thr_pa
        _thr_pa.Thread(target=_props_sweeper, daemon=True).start()
        print("[props] auto-logger started")

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

# User accounts, sessions, synced bet log, storage health — see accounts.py.
try:
    from accounts import router as accounts_router
    app.include_router(accounts_router)
except Exception as _acct_err:
    print(f"[startup] accounts router not loaded: {_acct_err}")

# Performance & accuracy reporting endpoints — see reports.py.
try:
    from reports import router as reports_router
    app.include_router(reports_router)
except Exception as _rep_err:
    print(f"[startup] reports router not loaded: {_rep_err}")

# Favicon, icons, PWA manifest + service worker — see static_routes.py.
try:
    from static_routes import router as static_router
    app.include_router(static_router)
except Exception as _static_err:
    print(f"[startup] static router not loaded: {_static_err}")

# Per-game market lines for the bet-from-a-game picker — see markets_routes.py.
try:
    from markets_routes import router as markets_router
    app.include_router(markets_router)
except Exception as _mkt_err:
    print(f"[startup] markets router not loaded: {_mkt_err}")

# Surface / ratings build + ops endpoints — split out to keep main.py phone-sized.
try:
    from ops_routes import router as ops_router
    app.include_router(ops_router)
except Exception as _ops_err:
    print(f"[startup] ops router not loaded: {_ops_err}")

try:
    from chat_routes import router as chat_router
    app.include_router(chat_router)
except Exception as _chat_err:
    print(f"[startup] chat router not loaded: {_chat_err}")

try:
    from promo_routes import router as promo_router
    app.include_router(promo_router)
except Exception as _promo_err:
    print(f"[startup] promo router not loaded: {_promo_err}")

try:
    from stocks_routes import router as stocks_router
    app.include_router(stocks_router)
except Exception as _stocks_err:
    print(f"[startup] stocks router not loaded: {_stocks_err}")


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
                             "sports": ["nba", "wnba", "nfl", "ncaaf", "ncaab", "wncaab"]})
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


def _extract_ppg(athlete_row, ppg_idx):
    """Pull points-per-game from an ESPN byathlete row using the offensive
    avgPoints column index resolved from the page schema. Defensive: returns
    None if the shape isn't what we expect."""
    try:
        for cat in (athlete_row.get("categories") or []):
            if (cat.get("name") or "").lower().startswith("off"):
                totals = cat.get("totals") or cat.get("values") or []
                if ppg_idx is not None and ppg_idx < len(totals):
                    return float(totals[ppg_idx])
        # fallback: a flat stats list
        st = athlete_row.get("stats") or []
        if ppg_idx is not None and ppg_idx < len(st):
            return float(st[ppg_idx])
    except Exception:
        return None
    return None


@app.get("/api/injuries/usage/build")
def injuries_usage_build(sport: str = "nba", confirm: str = "", season: int = 0):
    """Best-effort: weight injured players by real scoring so a star out costs
    more than a bench player. Pulls ESPN per-athlete season scoring, writes
    /data/{sport}_usage.json. Off-season the feed is sparse -> reports few
    players and the model keeps using position value. In-season this makes NBA
    injuries individual rather than positional."""
    sport = (sport or "").lower()
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes (sport=nba)", "supported": ["nba"]})
    league = {"nba": ("basketball", "nba")}.get(sport)
    if not league:
        return JSONResponse({"error": f"usage build only supports nba right now"}, status_code=400)
    import espn_provider as ep, injuries, json as _json
    yr = season or dt.date.today().year
    url = (f"https://site.web.api.espn.com/apis/common/v3/sports/"
           f"{league[0]}/{league[1]}/statistics/byathlete")
    usage = {}
    got = 0
    try:
        for page in range(1, 8):
            data = ep._get(url, {"region": "us", "lang": "en", "season": yr, "seasontype": 2,
                                 "limit": 50, "page": page, "sort": "offensive.avgPoints:desc"})
            # resolve the avgPoints column index from the page schema once
            ppg_idx = None
            for cat in (data.get("categories") or []):
                if (cat.get("name") or "").lower().startswith("off"):
                    names = cat.get("names") or cat.get("labels") or []
                    for i, nm in enumerate(names):
                        if str(nm).lower() in ("avgpoints", "pts", "ppg", "points"):
                            ppg_idx = i
                            break
            aths = data.get("athletes") or []
            if not aths:
                break
            for row in aths:
                name = ((row.get("athlete") or {}).get("displayName"))
                ppg = _extract_ppg(row, ppg_idx)
                if name and ppg is not None and ppg >= 0:
                    usage[injuries._norm(name)] = round(max(0.3, min(1.8, ppg / 14.0)), 3)
                    got += 1
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-700:]},
                            status_code=500)
    if got < 20:
        return JSONResponse({"status": "too few players parsed (likely off-season or schema change)",
                             "parsed": got, "season": yr,
                             "note": "model keeps using position value until this populates in-season"})
    path = f"/data/{sport}_usage.json"
    try:
        with open(path, "w") as f:
            _json.dump({"sport": sport, "season": yr, "usage": usage}, f)
    except Exception as e:
        return JSONResponse({"error": f"write failed: {e}"}, status_code=500)
    n = injuries.load_usage(sport)
    injuries.reload(sport)
    return JSONResponse({"status": "done", "players": got, "loaded": n, "path": path, "season": yr})


@app.get("/api/rest/diag")
def rest_diag(sport: str = "nba", date: str = ""):
    """Show computed rest days per team for a date (scanned from ESPN)."""
    sport = (sport or "").lower()
    try:
        import schedule
        if not schedule.enabled(sport):
            return JSONResponse({"sport": sport, "enabled": False,
                                 "supported": [s for s in schedule._CFG]})
        target = dt.date.fromisoformat(date) if date else dt.date.today()
        tbl = schedule.rest_table(sport, target)
        return JSONResponse({"sport": sport, "date": target.isoformat(),
                             "teams_found": len(tbl),
                             "rest_days": dict(sorted(tbl.items(), key=lambda x: x[1]))},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-700:]},
                            status_code=500)


@app.get("/api/injuries/diag")
def injuries_diag(sport: str = "nba"):
    """Show the per-team injury penalties (Elo pts) and the weighted players the
    model is docking for. Confirms the ESPN injuries feed is parsing."""
    sport = (sport or "").lower()
    try:
        import injuries
        if not injuries.enabled(sport):
            supported = [s for s in injuries._POS if injuries._inj_url(s)]
            return JSONResponse({"sport": sport, "enabled": False, "supported": supported})
        injuries.reload(sport)
        tbl = injuries._table(sport)
        by_id = tbl.get("by_id") or {}
        seen, teams = set(), []
        for src in (by_id, tbl.get("by_name") or {}):
            for v in src.values():
                key = v.get("team") or id(v)
                if key in seen:
                    continue
                seen.add(key)
                teams.append(v)
        teams.sort(key=lambda x: -x.get("penalty", 0))
        return JSONResponse({"sport": sport, "enabled": True, "teams_with_injuries": len(teams),
                             "teams": teams[:40]}, headers={"Cache-Control": "no-store"})
    except Exception as e:
        import traceback
        return JSONResponse({"sport": sport, "error": f"{type(e).__name__}: {e}",
                             "trace": traceback.format_exc()[-800:]}, status_code=500)


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


def _tennis_set_complete(g1, g2):
    hi, lo = max(g1, g2), min(g1, g2)
    return (hi >= 6 and hi - lo >= 2) or hi == 7


@app.get("/api/tennis/winprob")
def tennis_winprob_ep(match_id: str):
    """Live in-match win probability from the current score. Uses a neutral
    serve model so the number reflects the MATCH STATE (who's ahead and how
    safe the lead is), updating as games and sets are won. Honest: it's a
    score-based read, not a player-strength forecast."""
    if not USE_REAL:
        return JSONResponse({"error": "tennis provider inactive"}, status_code=503)
    import tennis_winprob as twp
    from apitennis import _sets, _server
    fix = {}
    try:
        fix = provider.raw_fixture(match_id) or {}
    except Exception:
        fix = {}
    if not fix:
        try:
            provider._refresh_live()
            fix = getattr(provider, "_fixtures", {}).get(str(match_id)) or {}
        except Exception:
            pass
    if not fix:
        return JSONResponse({"error": "no live fixture for that match"}, status_code=404)
    a_list, b_list = _sets(fix.get("scores"))
    status = (fix.get("event_status") or "").strip()
    pa = fix.get("event_first_player") or "Player A"
    pb = fix.get("event_second_player") or "Player B"
    a_serving = (_server(fix.get("event_serve")) != "b")
    sa = sb = cur_ga = cur_gb = 0
    for i in range(len(a_list)):
        g1, g2 = a_list[i], b_list[i]
        if _tennis_set_complete(g1, g2):
            if g1 > g2:
                sa += 1
            else:
                sb += 1
        else:
            cur_ga, cur_gb = g1, g2
    best_of = 5 if max(sa, sb) >= 2 else 3
    P = 0.62  # neutral serve -> score-driven

    def wp(_sa, _sb, _ga, _gb, _srv):
        return twp.match_prob(_sa, _sb, _ga, _gb, _srv, best_of, P, P)

    cur = wp(sa, sb, cur_ga, cur_gb, a_serving)
    momentum = [{"label": "Start", "prob_a": round(wp(0, 0, 0, 0, True) * 100, 1)}]
    ra = rb = 0
    for i in range(len(a_list)):
        g1, g2 = a_list[i], b_list[i]
        if _tennis_set_complete(g1, g2):
            if g1 > g2:
                ra += 1
            else:
                rb += 1
            momentum.append({"label": "Set %d" % (i + 1),
                             "prob_a": round(wp(ra, rb, 0, 0, True) * 100, 1)})
    finished = status.lower() in ("finished", "ended")
    if not finished:
        momentum.append({"label": "Now", "prob_a": round(cur * 100, 1)})
    return {"match_id": str(match_id), "player_a": pa, "player_b": pb,
            "sets": [sa, sb], "current_games": [cur_ga, cur_gb],
            "server": "a" if a_serving else "b", "best_of": best_of,
            "status": status, "finished": finished,
            "prob_a": round(cur * 100, 1), "prob_b": round((1 - cur) * 100, 1),
            "momentum": momentum}


@app.get("/api/tennis/season-diag")
def tennis_season_diag(match_key: str = ""):
    """Trace why the pre-match season form is or isn't populating for a match:
    shows the H2H shape, whether recent results carry a match id, whether an old
    match still serves statistics, and the aggregated averages."""
    if not USE_REAL:
        return JSONResponse({"error": "tennis provider not active (mock mode)"})
    if not match_key:
        try:
            provider.get_schedule(dt.datetime.combine(dt.date.today(), dt.time()))
            provider._refresh_live()
        except Exception:
            pass
        fx = getattr(provider, "_fixtures", {}) or {}
        if not fx:
            return JSONResponse({"error": "no matches loaded right now; try again when matches are scheduled"})
        match_key = next(iter(fx.keys()))
    try:
        fix = provider.raw_fixture(match_key) or {}
        pa = str(fix.get("first_player_key") or "")
        pb = str(fix.get("second_player_key") or "")
        h2h = provider.get_h2h(pa, pb) or {}
        fpr = h2h.get("firstPlayerResults") or []
        spr = h2h.get("secondPlayerResults") or []
        sample = fpr[0] if fpr else {}
        ka = [str(r.get("event_key")) for r in fpr if isinstance(r, dict) and r.get("event_key")]
        kb = [str(r.get("event_key")) for r in spr if isinstance(r, dict) and r.get("event_key")]
        hist = None
        if ka:
            tf = provider.raw_fixture(ka[0]) or {}
            hist = {"match_key": ka[0], "top_level_keys": sorted(tf.keys()),
                    "has_statistics": bool(tf.get("statistics")),
                    "stat_count": len(tf.get("statistics") or [])}
        from apitennis import _infer_surface as _surf
        msurf = _surf(fix.get("tournament_name"), when=fix.get("event_date"))
        sa = provider.player_serve_averages(pa, ka, surface=msurf)
        sb = provider.player_serve_averages(pb, kb, surface=msurf)
        return JSONResponse({
            "players": {"a": pa, "b": pb,
                        "names": [fix.get("event_first_player"), fix.get("event_second_player")]},
            "h2h_top_keys": sorted(h2h.keys()),
            "firstPlayerResults_count": len(fpr),
            "secondPlayerResults_count": len(spr),
            "sample_result_keys": sorted(sample.keys()) if isinstance(sample, dict) else None,
            "match_keys_found_a": len(ka), "match_keys_found_b": len(kb),
            "historical_fixture_test": hist,
            "season_avg_a": sa, "season_avg_b": sb,
        }, headers={"Cache-Control": "no-store"})
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1000:]},
                            status_code=500)


@app.get("/api/tennis/match-detail")
def tennis_match_detail(match_key: str = ""):
    """Serve/return detail sheet for one match. While the match is live (or
    finished) this returns the in-match stats that take over the sheet."""
    if not USE_REAL:
        return JSONResponse({"error": "tennis provider not active (mock mode)"})
    if not match_key:
        return JSONResponse({"error": "pass ?match_key=<id>"})
    try:
        return JSONResponse(provider.match_statistics(match_key),
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]},
                            status_code=500)


@app.get("/api/tennis/stats-probe")
def tennis_stats_probe(match_key: str = "", date: str = ""):
    """Confirm what a tennis fixture actually carries (statistics / point-by-point)
    so the serve-return detail sheet is built against real data. Best run during
    or just after a live match. Call with no match_key first to list loaded IDs."""
    if not USE_REAL:
        return JSONResponse({"error": "tennis provider not active (mock mode)"})
    try:
        d = dt.date.fromisoformat(date) if date else dt.date.today()
        try:
            provider.get_schedule(dt.datetime.combine(d, dt.time()))
            provider._refresh_live()
        except Exception:
            pass
        if not match_key:
            fx = getattr(provider, "_fixtures", {}) or {}
            # prefer an in-play match so the probe actually shows live stats
            live_ids = [k for k, v in fx.items()
                        if str((v or {}).get("event_live") or "") == "1"]
            if live_ids:
                probe = provider.raw_fixture_probe(live_ids[0])
                probe["auto_picked_live_match"] = live_ids[0]
                return JSONResponse(probe, headers={"Cache-Control": "no-store"})
            return JSONResponse({"hint": "no live match right now — run this while a match is in play, "
                                         "or pass ?match_key=<id>",
                                 "loaded_match_ids": list(fx.keys())[:60]},
                                headers={"Cache-Control": "no-store"})
        return JSONResponse(provider.raw_fixture_probe(match_key),
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]},
                            status_code=500)


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
            ml = _ml_pair(m)
            tier = (m.tier or "").upper()
            row = _match_row(db, m)
            if ml:
                row["odds"] = ml
                # authoritative edge% = model prob(picked side) - market implied%.
                # Can be negative (model likes a favorite the market likes even more).
                try:
                    pr = (row.get("prediction") or {}).get("prob_a")
                    if pr is not None:
                        favA = pr >= 0.5
                        mkt = ml["ml_home"] if favA else ml["ml_away"]
                        if mkt is not None:
                            mimp = 100.0 / (mkt + 100) if mkt > 0 else abs(mkt) / (abs(mkt) + 100.0)
                            mp = pr if favA else (1 - pr)
                            row["edge_pct"] = round((mp - mimp) * 100, 1)
                except Exception:
                    pass
            # ITF/futures floods the board. We still show these tournaments, but
            # only when there's something useful to show — a model pick OR a price
            # — so we don't render empty "Awaiting market" shells with no
            # prediction at all. Tour/challenger, anything priced, and anything
            # live or finished always show.
            if (tier == "ITF" and m.status not in ("live", "finished")
                    and not ml and not row.get("prediction")):
                continue
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
        vals = list(groups.values())
        # When the same tournament name exists for both tours (e.g. an ATP and a
        # WTA event share a name), prefix each with its tour to tell them apart.
        from collections import defaultdict
        tiers_by_name = defaultdict(set)
        for g in vals:
            tiers_by_name[g["name"]].add(g.get("tier"))
        for g in vals:
            if len(tiers_by_name[g["name"]]) > 1 and g.get("tier") in ("ATP", "WTA"):
                g["name"] = g["tier"] + " " + g["name"]
        # alphabetical by (display) name
        return sorted(vals, key=lambda g: (g["name"] or "").lower())


def _tennis_stats_from_array(fix):
    """Build the match-stats structure the detail sheet expects directly from the
    feed's clean `statistics` array (aces, serve %, break points, return points),
    which is far more reliable than reconstructing it from point-by-point."""
    arr = (fix or {}).get("statistics") or []
    if not arr:
        return None
    p1key = str(fix.get("first_player_key") or "")
    p2key = str(fix.get("second_player_key") or "")
    sides = {p1key: {}, p2key: {}}
    for s in arr:
        if not isinstance(s, dict) or s.get("stat_period") != "match":
            continue
        pk = str(s.get("player_key") or "")
        if pk in sides:
            sides[pk][s.get("stat_name")] = s

    def _pct(v):
        try:
            return round(float(str(v).replace("%", "").strip()))
        except (TypeError, ValueError):
            return None

    def _build(d):
        def g(name):
            return d.get(name) or {}
        sp, rp = g("Service Points Won"), g("Return Points Won")
        bps, bpc = g("Break Points Saved"), g("Break Points Converted")
        tp = g("Total Points Won")
        return {
            "service_points_won": sp.get("stat_won"), "service_points_total": sp.get("stat_total"),
            "service_points_pct": _pct(sp.get("stat_value")),
            "break_points_saved": bps.get("stat_won") or 0, "break_points_faced": bps.get("stat_total") or 0,
            "return_points_won": rp.get("stat_won"), "return_points_total": rp.get("stat_total"),
            "return_points_pct": _pct(rp.get("stat_value")),
            "break_points_won": bpc.get("stat_won") or 0, "break_points_chances": bpc.get("stat_total") or 0,
            "service_games_won": 0, "service_games_total": 0, "service_games_pct": None,
            "games_won": 0,
            "total_points_won": (tp.get("stat_won") if tp.get("stat_won") is not None else _pct(tp.get("stat_value"))),
            "aces": g("Aces").get("stat_value"),
            "double_faults": g("Double Faults").get("stat_value"),
            "first_serve_pct": _pct(g("1st serve percentage").get("stat_value")),
            "first_serve_won_pct": _pct(g("1st serve points won").get("stat_value")),
            "second_serve_won_pct": _pct(g("2nd serve points won").get("stat_value")),
        }
    a, b = _build(sides.get(p1key, {})), _build(sides.get(p2key, {}))
    if a.get("service_points_total") is None and b.get("service_points_total") is None:
        return None
    return {"has_data": True, "a": a, "b": b}


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

        raw_h2h = {}
        if USE_REAL and hasattr(provider, "raw_fixture"):
            try:
                raw_h2h = provider.get_h2h(m.player_a_key, m.player_b_key) or {}
                h2h, fa, fb, ra_list, rb_list = _parse_h2h(raw_h2h, m.player_a, m.player_b)
            except Exception as e:
                print(f"[detail] h2h failed: {e}")
            try:
                fix = provider.raw_fixture(m.provider_match_id)
                stats = _tennis_stats_from_array(fix) or compute_stats(fix.get("pointbypoint"))
                # pre-match (no live stats yet): show each player's SEASON serve/
                # return form, aggregated from their recent matches.
                if not (stats and stats.get("has_data")) and hasattr(provider, "player_serve_averages"):
                    # use the FIXTURE's player keys (what the stats are keyed by),
                    # and pull H2H with those same keys so recent matches line up.
                    p1k = str((fix or {}).get("first_player_key") or m.player_a_key or "")
                    p2k = str((fix or {}).get("second_player_key") or m.player_b_key or "")
                    h2hs = (provider.get_h2h(p1k, p2k) or raw_h2h or {}) if (p1k and p2k) else (raw_h2h or {})
                    ka = [str(r.get("event_key")) for r in (h2hs.get("firstPlayerResults") or []) if isinstance(r, dict) and r.get("event_key")]
                    kb = [str(r.get("event_key")) for r in (h2hs.get("secondPlayerResults") or []) if isinstance(r, dict) and r.get("event_key")]
                    sa = provider.player_serve_averages(p1k, ka, surface=getattr(m, "surface", None))
                    sb = provider.player_serve_averages(p2k, kb, surface=getattr(m, "surface", None))
                    if sa or sb:
                        stats = {"has_data": True, "season": True,
                                 "matches_a": (sa or {}).get("_matches"),
                                 "matches_b": (sb or {}).get("_matches"),
                                 "a": sa or {}, "b": sb or {}}
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


@app.get("/api/admin/regrade")
@app.post("/api/admin/regrade")
def admin_regrade(result: str, ref: str = "", player: str = "", opponent: str = "",
                  sport: str = "tennis", token: str = "", authorization: str | None = Header(None)):
    """Owner-only: correct a settled pick (e.g. a tennis retirement that wrongly
    voided). Pass ?player=<name> (easiest) or ?ref=<match id>. result = win | loss
    | void from the MODEL PICK's point of view (opponent retired = win). Clears any
    prior result and re-records with real odds so units/ROI update."""
    admin_tok = os.environ.get("PROMO_CRON_TOKEN", "").strip()
    ok = bool(admin_tok) and (token or "").strip().strip('"') == admin_tok
    if not ok:
        try:
            import accounts
            _adm = os.environ.get("ADMIN_USERNAME", "").strip().lower()
            with SessionLocal() as _db:
                _u = accounts._user_from_token(_db, accounts._bearer(authorization))
            ok = bool(_u and _adm and (getattr(_u, "username", "") or "").strip().lower() == _adm)
        except Exception:
            ok = False
    if not ok:
        return {"error": "forbidden"}
    result = (result or "").strip().lower()
    if result not in ("win", "loss", "void", "reset", "clear"):
        return {"error": "result must be win, loss, void, or reset"}
    from models import PickResult
    with SessionLocal() as db:
        m = None
        if str(ref).isdigit():
            m = db.query(Match).filter(Match.id == int(ref)).one_or_none()
        if m is None and player:
            pl = player.strip().lower()
            op = opponent.strip().lower()
            lo = dt.datetime.now() - dt.timedelta(days=28)
            cands = []
            for x in db.query(Match).filter(Match.scheduled >= lo).all():
                a = (getattr(x, "player_a", "") or "").lower()
                b = (getattr(x, "player_b", "") or "").lower()
                if pl in a or pl in b:
                    if op and not (op in a or op in b):
                        continue                    # opponent given but not in this match
                    cands.append(x)
            cands.sort(key=lambda x: x.scheduled, reverse=True)
            if cands:
                m = cands[0]
                ref = str(m.id)
        if m is None:
            return {"error": "no tennis match found \u2014 pass ?player=<name>&opponent=<name> or ?ref=<id>"}
        matchup = (getattr(m, "player_a", "") or "?") + " vs " + (getattr(m, "player_b", "") or "?")
        # RESET: remove any existing result (un-grade). Use for a wrongly graded or
        # not-yet-played match.
        if result in ("reset", "clear"):
            n = db.query(PickResult).filter(PickResult.sport == sport, PickResult.ref == str(ref)).delete()
            db.commit()
            _recorded_refs.discard((sport, str(ref)))
            return {"ok": True, "match_id": str(ref), "matchup": matchup, "graded": "reset", "removed": n}
        pw = (_match_row(db, m) or {}).get("predicted_winner")
        if not pw:
            prev = db.query(PickResult).filter(PickResult.sport == sport, PickResult.ref == str(ref)).first()
            pw = getattr(prev, "predicted", None) if prev else None
        if pw not in ("a", "b"):
            return {"error": "found match (" + matchup + ") but couldn't resolve the model's pick side",
                    "match_id": str(ref)}
        if result == "win":
            actual = pw
        elif result == "loss":
            actual = "b" if pw == "a" else "a"
        else:
            actual = "canceled"
        db.query(PickResult).filter(PickResult.sport == sport, PickResult.ref == str(ref)).delete()
        db.commit()
    _recorded_refs.discard((sport, str(ref)))
    with SessionLocal() as db:
        _record_result(db, sport, str(ref), pw, actual)
        db.commit()
    return {"ok": True, "sport": sport, "match_id": str(ref), "matchup": matchup,
            "picked_side": pw, "graded": result, "actual": actual}


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
                    _snapshot_odds(sport, str(g["id"]), side, int(round(taken)),
                                   prob=(g["prob_home"] if side == "home" else g.get("prob_away", 1 - g["prob_home"])))
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


def _snapshot_odds(sport, ref, side, odds, prob=None, subcat=None):
    """Record/refresh the market line for a pick (open = first seen, last = now).
    Also captures the model's probability and sub-league tag (tennis tour) for the
    picked side so every settled game carries them for edge/wager/tour tracking."""
    from models import OddsSnapshot
    now = dt.datetime.utcnow()
    try:
        with SessionLocal() as db:
            row = db.query(OddsSnapshot).filter_by(sport=sport, ref=ref).first()
            if row is None:
                db.add(OddsSnapshot(sport=sport, ref=ref, side=side,
                                    open_odds=odds, last_odds=odds, prob=prob,
                                    subcat=subcat, first_seen=now, last_seen=now))
            else:
                row.last_odds = odds
                row.last_seen = now
                row.side = side
                if prob is not None:
                    row.prob = prob
                if subcat is not None:
                    row.subcat = subcat
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


def _reinfer_tennis_surfaces():
    """Correct stored tennis surfaces for upcoming matches (and re-run their
    predictions on the corrected surface). Fixes stale/wrong values \u2014 e.g. clay
    ITF events that were mislabeled grass before the surface-logic fix."""
    try:
        from apitennis import _infer_surface
    except Exception:
        return
    try:
        with SessionLocal() as db:
            now = dt.datetime.now()
            rows = (db.query(Match, Prediction)
                      .outerjoin(Prediction, Prediction.match_id == Match.id)
                      .filter(Match.scheduled >= now - dt.timedelta(hours=18))
                      .all())
            changed = 0
            for m, pred in rows:
                if not m.tournament:
                    continue
                new = _infer_surface(m.tournament, m.tier, m.scheduled)
                if not new or new == m.surface:
                    continue
                m.surface = new
                changed += 1
                if pred is not None and engine is not None:
                    try:
                        res = engine.predict_feed(m.player_a, m.player_b, new)
                        if res and res[0] is not None:
                            pred.prob_a = res[0]
                            if len(res) > 1 and res[1]:
                                pred.confidence = res[1]
                    except Exception:
                        pass
            if changed:
                db.commit()
                print(f"[surface] corrected {changed} tennis surfaces + re-predicted")
    except Exception as e:
        print(f"[surface] reinfer failed: {e}")


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
            _dayres = {}

            def _final(mm):
                """Definitive (status, winner) for a match, re-fetched per date and
                cached. Catches retirements the live feed dropped before recording."""
                try:
                    day = mm.scheduled.date() if hasattr(mm.scheduled, "date") else mm.scheduled
                    k = str(day)
                    if k not in _dayres:
                        _dayres[k] = provider.final_results(day) or {}
                    return _dayres[k].get(str(mm.provider_match_id))
                except Exception:
                    return None

            # 1) settle newly-stale tracked picks \u2014 re-fetch the real result before voiding
            for m in stale:
                if str(m.id) in have:
                    continue
                row = _match_row(db, m)
                pw = row.get("predicted_winner")
                if not pw:
                    continue                       # not a tracked pick
                sc = row.get("score") or {}
                winner = sc.get("winner") if row.get("status") == "finished" else None
                if winner not in ("a", "b"):
                    fr = _final(m)                 # definitive result (catches retirements)
                    if fr and fr[0] == "finished" and fr[1] in ("a", "b"):
                        winner = fr[1]
                if winner in ("a", "b"):
                    _record_result(db, "tennis", m.id, pw, winner)
                else:
                    _record_result(db, "tennis", m.id, pw, "canceled")
                    pushed += 1
                wrote = True

            # 2) fix ALREADY-voided tennis picks: a retirement the feed reported late
            #    should flip from void to a real win/loss, restoring units/ROI.
            try:
                voids = (db.query(PickResult)
                           .filter(PickResult.sport == "tennis",
                                   PickResult.actual.in_(["canceled", "cancelled", "void"]))
                           .all())
                recent = {str(mm.id): mm for mm in
                          db.query(Match).filter(Match.scheduled >= lo).all()}
                for pr in voids:
                    m = recent.get(str(pr.ref))
                    if not m:
                        continue
                    fr = _final(m)
                    if fr and fr[0] == "finished" and fr[1] in ("a", "b"):
                        pw = getattr(pr, "predicted", None) or (_match_row(db, m) or {}).get("predicted_winner")
                        if pw not in ("a", "b"):
                            continue
                        db.delete(pr)
                        db.commit()
                        _recorded_refs.discard(("tennis", str(pr.ref)))
                        _record_result(db, "tennis", m.id, pw, fr[1])
                        wrote = True
            except Exception as _ve:
                print(f"[stale-tennis] void re-check failed: {_ve}")

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
    snap = None
    try:
        snap = db.query(OddsSnapshot).filter_by(sport=sport, ref=ref).first()
        if snap:
            taken = snap.open_odds
            close = snap.last_odds
    except Exception:
        pass
    prob = (snap.prob if snap is not None and getattr(snap, "prob", None) is not None else None)
    if prob is None:
        prob = _lookup_locked_prob(db, sport, ref)
    subcat = snap.subcat if snap is not None and getattr(snap, "subcat", None) is not None else None
    db.add(PickResult(sport=sport, ref=ref, settled_date=dt.datetime.now(),
                      predicted=str(predicted), actual=str(actual),
                      correct=(str(predicted) == str(actual)),
                      taken_odds=taken, close_odds=close, prob=prob, subcat=subcat))
    _recorded_refs.add(memo)


def _lookup_locked_prob(db, sport, ref):
    """Find the model probability this pick was shown with, from the locked daily
    sets. Lets the settled record carry the prob so we can tell which picks were
    real +edge wagers vs no-edge chalk. Bounded to recent sets for speed."""
    import json
    from models import LockedPickSet
    try:
        cutoff = dt.datetime.now() - dt.timedelta(days=21)
        for row in db.query(LockedPickSet).filter(LockedPickSet.pick_date >= cutoff).all():
            for p in json.loads(row.payload):
                if (p.get("sport") == sport and str(p.get("id")) == str(ref)
                        and p.get("prob") is not None):
                    return float(p["prob"])
    except Exception:
        pass
    return None


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
        od = (book or {}).get(str(pmid))
        if not od:
            # Fallback to the whole-day feed, which reliably carries ITF / lower-tier
            # events that the per-match endpoint often omits. Cached -> one call.
            import datetime as _dt
            day = prov.get_odds(day=_dt.date.today()) or {}
            od = day.get(str(pmid))
        if not od:
            return None, None
        f, s = od.get("first"), od.get("second")
        if f and s and _norm(f) == _norm(player_b) and _norm(s) == _norm(player_a):
            return od["b"], od["a"]            # feed listed them flipped
        return od["a"], od["b"]
    except Exception:
        return None, None


def _attach_tennis_market(p, book=None):
    """Attach api-tennis market odds + de-vigged edge to a tennis pick. The price
    is aligned by matching the pick's NAME against the odds feed's own player
    names (not by list position, which can differ from the board order and was
    handing the pick the OPPONENT's price — a favorite showing +1176). Settlement
    side stays in board order so grading is unaffected.

    Pass `book` (the whole day's get_odds(day=...) result) to look the match up
    locally instead of making a per-match API call — this is how the background
    warmer prices the entire slate (ITF included) in ONE request."""
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
        if book is not None:
            od = book.get(str(pmid))
        else:
            od = (prov.get_odds(match_key=pmid) or {}).get(str(pmid))
            if not od:
                # ITF / lower-tier events are often absent from the per-match
                # endpoint but present in the day feed (cached, one call).
                import datetime as _dt
                od = (prov.get_odds(day=_dt.date.today()) or {}).get(str(pmid))
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
    # Honest edge: calibrated model prob vs the DE-VIGGED market (both sides known
    # here, so we can strip the overround instead of comparing to a vig-inflated
    # single-side price).
    try:
        from calibrate import calibrate as _calib
        cprob = _calib("tennis", p.get("prob"))
        ia, ib = 1.0 / dec, 1.0 / other
        devig = ia / (ia + ib) if (ia + ib) else None
        if cprob is not None and devig is not None:
            p["cal_prob"] = round(cprob, 3)
            p["edge_pct"] = round((cprob - devig) * 100, 1)
    except Exception:
        pass
    bside = "a" if _same(pick, names[0]) else "b"   # settlement stays in board order
    _snapshot_odds("tennis", str(p["id"]), bside, am, prob=p.get("prob"), subcat=p.get("tier"))


@app.get("/api/mlb/games")
def mlb_games(date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    try:
        from mlb_provider import get_games
        games = get_games(target)
    except Exception as e:
        print(f"[mlb] games failed: {e}")
        return []
    try:
        import injuries
        for g in games:
            if g.get("status") != "finished":
                injuries.game_adjust("mlb", g)
    except Exception as e:
        print(f"[mlb] injuries skipped: {e}")
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
                "pmid": m.provider_match_id, "tier": m.tier,
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
                    "h2h": g.get("h2h"),
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
                    "h2h": g.get("h2h"),
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
                "h2h": g.get("h2h"),
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
    from calibrate import calibrate as _calib
    prob = p.get("prob")
    cprob = _calib(p["sport"], prob) if prob else None   # honest, overconfidence-corrected
    if cprob is not None:
        p["cal_prob"] = round(cprob, 3)
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
            if tot and side_imp is not None and cprob is not None:
                p["edge_pct"] = round((cprob - side_imp / tot) * 100, 1)
        # market odds: team sports via snapshot, tennis via per-tournament feed
    try:
        import odds_api
        if odds_api.enabled() and p["sport"] in ("mlb", "nba", "nfl", "wnba"):
            from models import OddsSnapshot
            with SessionLocal() as db:
                snap = db.query(OddsSnapshot).filter_by(sport=p["sport"], ref=str(p["id"])).first()
            if snap and snap.last_odds is not None:
                p["market_odds"] = snap.last_odds
                mp = american_to_prob(snap.last_odds)
                if cprob is not None and mp is not None:
                    p["edge_pct"] = round((cprob - mp) * 100, 1)
    except Exception:
        pass
    # tennis market odds come from api-tennis (included in the plan), not the
    # Odds API — keeps Odds API credits for the team sports + soccer.
    if p["sport"] == "tennis":
        try:
            _attach_tennis_market(p)
        except Exception:
            pass
    # Line movement (opening -> current) relative to OUR pick, from the saved
    # snapshot. Honest: this is real movement vs the side we're on, not a public
    # bet-% reverse-line-movement signal (no free feed carries bet %).
    try:
        from models import OddsSnapshot
        with SessionLocal() as db:
            snap = db.query(OddsSnapshot).filter_by(sport=p["sport"], ref=str(p["id"])).first()
        if (snap and snap.open_odds is not None and snap.last_odds is not None
                and snap.open_odds != snap.last_odds):
            op = american_to_prob(snap.open_odds)
            lp = american_to_prob(snap.last_odds)
            if op is not None and lp is not None:
                d = round((lp - op) * 100, 1)   # +: implied prob rose on our side
                if abs(d) >= 0.5:
                    p["line_move"] = {"open": snap.open_odds, "now": snap.last_odds,
                                      "delta_pct": d, "toward": d > 0}
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
        _settle_props()      # grade player props from final boxscores (model-quality only)
    except Exception as _pe:
        print(f"[props] periodic settle skipped: {_pe}")
    try:
        _settle_ladder()     # roll/reset the daily ladder challenge
    except Exception as _le:
        print(f"[ladder] periodic settle skipped: {_le}")
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
                                   shown_date=dt.datetime.combine(target, dt.time.min),
                                   prob=p.get("prob")))
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


def _kelly_units(prob, american):
    """Quarter-Kelly stake in units (1u = 1% bankroll), capped at 4u. None when
    there's no edge. Display-only — never used to grade results."""
    try:
        prob = float(prob); american = float(american)
    except (TypeError, ValueError):
        return None
    b = american / 100.0 if american > 0 else 100.0 / abs(american)
    if b <= 0:
        return None
    f = prob - (1 - prob) / b
    if f <= 0:
        return None
    u = min(f * 0.25 * 100, 4)
    return round(u, 1) if u >= 0.1 else None


_HOT_PATH = "/data/hot_pick.json" if os.path.isdir("/data") else "hot_pick.json"


def _hot_load_all():
    try:
        import json
        with open(_HOT_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _hot_save_all(store):
    try:
        import json
        with open(_HOT_PATH, "w") as f:
            json.dump(store, f)
    except Exception:
        pass


@app.get("/api/picks/hot")
def hot_pick(date: str | None = None):
    """Single best edge across all sports — the 'Hot Pick of the Day'. LOCKED:
    the first qualifying pick found for a given day is saved and served for the
    rest of that day, so it never swaps to a new game when an earlier one ends.
    Persisted on the data volume, so a redeploy can't change it either."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    iso = target.isoformat()
    store = _hot_load_all()
    if iso in store:
        pick = dict(store[iso])
        _hot_attach_result(pick)
        return {"date": iso, "pick": pick, "locked": True}
    _ensure_day(target)
    plays = _gather_plays(target)
    best = None
    for p in plays:
        try:
            _enrich_odds(p)
        except Exception:
            continue
        edge = p.get("edge_pct")
        prob = p.get("prob") or 0
        if edge is None or edge <= 0 or prob < 0.5:
            continue
        mo = p.get("market_odds")
        cand = {
            "sport": p["sport"], "pick": p.get("pick"), "match": p.get("match"),
            "prob": prob, "edge_pct": edge, "market_odds": mo,
            "fair_odds": p.get("fair_odds"), "event_time": p.get("event_time"),
            "kelly_units": _kelly_units(prob, mo) if mo is not None else None,
            "line_move": p.get("line_move"),
            "id": p.get("id"), "confidence": p.get("confidence"),
        }
        if best is None or edge > best["edge_pct"]:
            best = cand
    # Lock the first real pick of the day so it stops moving.
    if best is not None:
        store[iso] = best
        for old in sorted(store)[:-10]:        # keep ~10 days of history
            store.pop(old, None)
        _hot_save_all(store)
    pick = best
    _hot_attach_result(pick)
    return {"date": iso, "pick": pick, "locked": bool(best)}


def _hot_attach_result(pick):
    """Look up the locked pick's settled outcome (if its game has finished) and
    tag it win/loss so the card can show the result and fire confetti."""
    if not pick or pick.get("id") is None:
        return
    try:
        from models import PickResult
        with SessionLocal() as db:
            rr = (db.query(PickResult)
                    .filter_by(sport=pick["sport"], ref=str(pick["id"])).first())
        if rr is not None:
            pick["result"] = "win" if rr.correct else "loss"
            pick["settled"] = True
    except Exception:
        pass


@app.get("/api/officials/{sport}/{game_id}")
def officials(sport: str, game_id: str):
    """Officiating crew for a game — ASSIGNMENT plus, for MLB, the home-plate
    umpire's run/strikeout tendency computed from the free statsapi. NFL/NBA/NCAA
    referees are best-effort from ESPN (names only — no tendency feed exists)."""
    sp = (sport or "").lower()
    try:
        if sp == "mlb":
            from mlb_provider import get_officials
            data = get_officials(int(game_id))
            try:
                import umpire_stats
                hp = data.get("home_plate")
                if hp:
                    data["hp_tendency"] = umpire_stats.get_tendency(hp)
                    data["hp_runs_factor"] = umpire_stats.runs_factor(hp)
            except Exception:
                pass
            return data
        if sp in ("nba", "wnba", "nfl", "ncaaf", "ncaab", "wncaab"):
            from espn_provider import get_officials
            return get_officials(sp, game_id)
    except Exception as e:
        return {"sport": sp, "officials": [], "error": str(e)}
    return {"sport": sp, "officials": [], "unsupported": True}


@app.get("/api/umpires/refresh")
def umpires_refresh(days: int = 70, max_games: int = 120):
    """Fold recent FINAL games into the per-umpire tendency table (incremental).
    First backfill is API-heavy, so it's capped per call — hit it again while
    'more_pending' is true to continue."""
    import umpire_stats
    return umpire_stats.refresh(days=days, max_games=max_games)


@app.get("/api/umpires/diag")
def umpires_diag(name: str | None = None):
    import umpire_stats
    out = umpire_stats.summary()
    if name:
        out["tendency"] = umpire_stats.get_tendency(name)
        out["runs_factor"] = umpire_stats.runs_factor(name)
    return out


def _clv_proven_sports(min_n: int = 30, min_clv: float = 50.0):
    """Sports that have actually beaten the closing line (CLV >= min_clv%) over at
    least min_n graded picks. CLV is the honest proof a model beats the market —
    until a sport clears this bar we PREDICT it but must not RECOMMEND wagers on
    it, because its 'edges' haven't shown they're real. Protects users from the
    model's own overconfidence (e.g. tennis edges currently run ~23% CLV)."""
    from collections import defaultdict
    from models import PickResult
    from clv import american_to_prob
    n = defaultdict(int); beat = defaultdict(int)
    try:
        with SessionLocal() as db:
            for r in db.query(PickResult).all():
                if r.taken_odds is None or abs(r.taken_odds) < 100:
                    continue
                if r.close_odds is None or abs(r.close_odds) < 100:
                    continue
                n[r.sport] += 1
                if american_to_prob(r.taken_odds) < american_to_prob(r.close_odds):
                    beat[r.sport] += 1
    except Exception:
        return set()
    return {s for s in n if n[s] >= min_n and 100.0 * beat[s] / n[s] >= min_clv}


@app.get("/api/calib")
def calib_params_all():
    """Per-sport calibration coefficients so the client can show the same honest,
    overconfidence-corrected probabilities and edges the backend uses."""
    from calibrate import calib_params
    sports = ("tennis", "mlb", "nba", "nfl", "nhl", "ncaabb", "ncaaf",
              "ncaab", "wncaab", "soccer", "ufc", "golf")
    out = {}
    for s in sports:
        try:
            a, b, n = calib_params(s)
            out[s] = {"a": round(a, 4), "b": round(b, 4), "n": n}
        except Exception:
            pass
    return out


@app.get("/api/value")
def value_board(date: str | None = None, min_edge: float = 2.0):
    """Cross-sport VALUE board: today's recommended +EV plays only, ranked by the
    model's edge over the live market. The 'what should I actually bet today' view.
    Honest by construction — a play appears solely when the model beats a real
    market price AND its sport has proven it beats the closing line, so no-edge
    chalk and unproven 'edges' never show up here."""
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_day(target)
    proven = _clv_proven_sports()
    out = []
    for p in _gather_plays(target):
        if p.get("sport") not in proven:
            continue
        p = dict(p)
        try:
            _enrich_odds(p)
        except Exception:
            continue
        e, mo, prob = p.get("edge_pct"), p.get("market_odds"), p.get("prob")
        cprob = p.get("cal_prob", prob)
        if e is None or mo is None or cprob is None or e < min_edge:
            continue
        stake = None
        try:
            b = (mo / 100.0) if mo > 0 else (100.0 / abs(mo))
            f = (b * cprob - (1 - cprob)) / b
            u = max(0.0, f * 0.25 * 100)
            stake = round(u, 1) if u >= 0.5 else 0
        except Exception:
            pass
        out.append({
            "sport": p["sport"], "id": p.get("id"),
            "match": p.get("match"), "pick": p.get("pick"),
            "prob": cprob, "raw_prob": prob,
            "market_odds": mo, "fair_odds": p.get("fair_odds"),
            "edge_pct": e, "stake": stake,
            "confidence": p.get("confidence"), "event_time": p.get("event_time"),
        })
    out.sort(key=lambda x: x["edge_pct"], reverse=True)
    return {"date": target.isoformat(), "count": len(out),
            "min_edge": min_edge, "proven_sports": sorted(proven), "plays": out}


@app.get("/api/picks/best")
def best_bets(date: str | None = None, sport: str | None = None,
              min_prob: float = 0.0, min_edge: float = 0.0):
    """Larger, filterable board with in-depth rationale (premium-style).
    Filterable by sport, model win% (min_prob) and market edge% (min_edge) \u2014 all
    of which work for every sport including tennis."""
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
        try:
            _enrich_odds(p)                 # market_odds + edge_pct (every sport)
        except Exception:
            pass
        if min_edge > 0 and (p.get("edge_pct") is None or p.get("edge_pct", 0) < min_edge):
            continue                        # edge filter (needs a captured market line)
        try:
            p["reason"] = narrate.prose(_long_reason(p), kind="reason",
                                        sport=p["sport"], llm=LLM_COMPLETE, budget=budget)
            # premium "why it's a best bet" layer (paywall-ready)
            pf = premium.premium_facts(p, slate, SessionLocal)
            pf["text"] = narrate.prose(pf["text"], kind="premium",
                                       sport=p["sport"], llm=LLM_COMPLETE, budget=budget)
            p["premium"] = pf
        except Exception as _pe:
            print(f"[best] enrich failed for {p.get('sport')}/{p.get('id')}: {_pe}")
        p.pop("score_key", None)
        p.pop("ctx", None)
        out.append(p)
    # log the unfiltered top board (so the record reflects the view's real picks)
    if not sport and min_prob == 0.0 and min_edge == 0.0:
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
    "wnba":   {5, 6, 7, 8, 9, 10},
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
    # so it isn't in sports.py SPORTS. Surface it here so the home grid shows it \u2014
    # but let HIDDEN_SPORTS hide it like any other sport (set HIDDEN_SPORTS to
    # include "golf" to remove it, no code change needed).
    if "golf" not in hidden and not any(e.get("key") == "golf" for e in meta):
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
    pred = datagolf_api.pre_tournament(tour, "fit")  # course history & fit, not neutral
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
                        fsrc = f if (has_fit and f and f.get("win") is not None) else base
                        fnum, flab = golf_model.estimate_finish(
                            fsrc.get("win"), fsrc.get("top5"), fsrc.get("top10"), fsrc.get("top20"),
                            fsrc.get("make_cut"), len(board.get("players") or []))
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
        try:
            import golf_tracker
            golf_tracker.settle(tour, board=board)   # snapshot/grade while board is live
            # self-heal: if matchups are still pending (board may have rolled to
            # the next event), grade them against the last few finished days too.
            with SessionLocal() as _db:
                from models import GolfMatchupPick
                _pending = _db.query(GolfMatchupPick).filter_by(tour=tour, settled=False).count()
            if _pending:
                _today = dt.date.today()
                for _i in (1, 2, 3):
                    _d = _today - dt.timedelta(days=_i)
                    try:
                        _pb = golf_provider.get_board(tour, dates=_d.strftime("%Y%m%d"))
                        golf_tracker.settle(tour, board=_pb, stale_days=999)
                    except Exception:
                        pass
        except Exception as _e:
            print(f"[golf] inline settle skipped: {_e}")
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


@app.get("/api/golf/tracker-recover")
def golf_tracker_recover(confirm: str = "", tour: str = "pga", date: str = "", days: int = 3):
    """Recover ungraded matchups from finished events: re-fetch the leaderboard
    for recent past dates (default the last few days) and grade pending matchups
    against it. Use when matchups washed out because the board rolled to the next
    event before they settled. Add ?confirm=yes (optional &date=YYYY-MM-DD)."""
    if confirm != "yes":
        return JSONResponse({"note": "add ?confirm=yes (optional &date=YYYY-MM-DD or &days=3)", "tour": tour})
    try:
        import golf_provider, golf_tracker
        if date:
            dates_list = [dt.date.fromisoformat(date)]
        else:
            today = dt.date.today()
            dates_list = [today - dt.timedelta(days=i) for i in range(1, max(1, days) + 1)]
        passes, total = [], 0
        for d in dates_list:
            board = golf_provider.get_board(tour, dates=d.strftime("%Y%m%d"))
            ev = (board or {}).get("event") or {}
            n = golf_tracker.settle(tour, board=board, stale_days=999)  # don't void during recovery
            passes.append({"date": d.isoformat(),
                           "event": ev.get("name") if isinstance(ev, dict) else None,
                           "complete": ev.get("is_complete") if isinstance(ev, dict) else None,
                           "players_on_board": len((board or {}).get("players") or []),
                           "settled": n})
            total += n
        return JSONResponse({"tour": tour, "recovered_total": total, "passes": passes},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()[-1500:]},
                            headers={"Cache-Control": "no-store"})


@app.get("/api/golf/tracker-regrade")
def golf_tracker_regrade(confirm: str = "", tour: str = "pga", days: int = 30, event: str = ""):
    """Clean re-grade: wipe stored scores + results for tracked matchups, then
    re-settle each against ITS OWN event's finished leaderboard (fetched by date,
    oldest->newest). The event guard in settle() keeps a player who is in this
    week's field too from contaminating an older matchup. Optionally limit to one
    event with &event=<substring> (e.g. event=open for the U.S. Open).
    Add ?confirm=yes (optional &days=30 &event= &tour=pga)."""
    if confirm != "yes":
        return JSONResponse({"note": "add ?confirm=yes; optional &days=30 &event=<substr> &tour=pga",
                             "example": "?confirm=yes&tour=pga&event=open"})
    try:
        import golf_provider, golf_tracker
        from models import GolfMatchupPick, PickResult

        def _evnorm(s):
            return "".join(ch for ch in (s or "").lower() if ch.isalnum())
        want = _evnorm(event)

        # 1) wipe results + stored scores (only the targeted event if given)
        wiped, refs = 0, []
        with SessionLocal() as db:
            for mp in db.query(GolfMatchupPick).filter_by(tour=tour).all():
                if want and want not in _evnorm(mp.event):
                    continue
                mp.settled, mp.result, mp.settled_date = False, None, None
                mp.s1 = mp.s2 = mp.s3 = None
                refs.append(mp.ref)
                wiped += 1
            if want:
                for i in range(0, len(refs), 400):
                    db.query(PickResult).filter(
                        PickResult.sport == "golf",
                        PickResult.ref.in_(refs[i:i + 400])).delete(synchronize_session=False)
            else:
                db.query(PickResult).filter(PickResult.sport == "golf").delete(synchronize_session=False)
            db.commit()

        # 2) fetch ONLY the dates these matchups actually came from (plus today),
        #    deduped — fetching any date during an event returns that event's full
        #    final leaderboard, so this is a few calls, not a 31-day sweep that
        #    times out and leaves everything wiped.
        dates_needed = set()
        with SessionLocal() as db:
            for mp in db.query(GolfMatchupPick).filter_by(tour=tour).all():
                if want and want not in _evnorm(mp.event):
                    continue
                if mp.recorded_date:
                    base = mp.recorded_date.date()
                    for off in range(0, 5):       # round may be played a few days after offer
                        dates_needed.add(base + dt.timedelta(days=off))
        today = dt.date.today()
        dates_needed.add(today)
        dates_list = sorted(d for d in dates_needed if d <= today)

        passes = []
        for d in dates_list:
            try:
                board = golf_provider.get_board(tour, dates=d.strftime("%Y%m%d"))
                ev = (board or {}).get("event") or {}
                if want and want not in _evnorm(ev.get("name")):
                    continue
                n = golf_tracker.settle(tour, board=board, stale_days=10 ** 9)
                if n:
                    passes.append({"date": d.isoformat(), "event": ev.get("name"), "settled": n})
            except Exception:
                continue
        # final pass against the live current board for the in-progress event
        try:
            cur = golf_provider.get_board(tour)
            n = golf_tracker.settle(tour, board=cur, stale_days=10 ** 9)
            if n:
                ev = (cur or {}).get("event") or {}
                passes.append({"date": today.isoformat(), "event": ev.get("name"),
                               "settled": n, "live": True})
        except Exception:
            pass

        # 3) report the clean record
        with SessionLocal() as db:
            rows = db.query(GolfMatchupPick).filter_by(tour=tour).all()
            rec = {"wins": sum(1 for r in rows if r.result == "win"),
                   "losses": sum(1 for r in rows if r.result == "loss"),
                   "pushes": sum(1 for r in rows if r.result == "push"),
                   "still_pending": sum(1 for r in rows if not r.settled)}
        return JSONResponse({"tour": tour, "wiped": wiped, "passes": passes, "record": rec},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]},
                            status_code=500)


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
    try:
        import injuries, schedule
        for g in games:
            if g.get("status") != "finished":
                injuries.game_adjust("nhl", g)
                schedule.game_adjust("nhl", g, target)
    except Exception as e:
        print(f"[nhl] injuries skipped: {e}")
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
        if sport in ("nba", "wnba", "nfl"):
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
        elif sport in ("nba", "wnba", "nfl", "ncaaf", "ncaab", "wncaab"):
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
    if sport not in ("mlb", "nba", "wnba", "nfl", "soccer"):
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
    if missing and sport in ("mlb", "nba", "wnba", "nfl"):
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
    if sport in ("mlb", "nba", "wnba", "nfl", "soccer") and status and str(status).lower() in _LIVE_OR_FINAL:
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
    # Persist for model-quality tracking (separate from the betting record).
    try:
        import prop_tracking as PT
        with SessionLocal() as _db:
            PT.log_props(_db, sport, game_id, props)
            done = {}
            for p in props:
                a = p.get("actual")
                if a is None:
                    continue
                st = (p.get("stat") or p.get("label") or "").strip().lower()
                nm = (p.get("player") or "").strip().lower()
                if nm and st:
                    done[(nm, st)] = a
            if done:
                PT.grade_props(_db, sport, game_id, done)
    except Exception as _pe:
        print(f"[{sport}] prop tracking failed: {_pe}")
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
                # Book props carry the sportsbook's line but none of the model's
                # reasoning. Attach it from a per-game CACHED context map so we
                # don't rerun the whole projection pass on every page load.
                try:
                    from mlb_provider import prop_context_map
                    _ctx = prop_context_map(target, game_id)
                    for _bp in b:
                        _k = ((_bp.get("player") or "").strip().lower(),
                              (_bp.get("stat") or _bp.get("label") or "").strip().lower())
                        _hit = _ctx.get(_k)
                        if _hit:
                            if _hit[0] and not _bp.get("context"):
                                _bp["context"] = _hit[0]
                            if _hit[1] and not _bp.get("why"):
                                _bp["why"] = _hit[1]
                except Exception as _ce:
                    print(f"[mlb] prop context merge failed: {_ce}")
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
    if sport not in ("nba", "wnba", "nfl"):
        return {"props": []}
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    status = None
    # 1) real sportsbook lines: SportsGameOdds (free props) then The Odds API
    try:
        from espn_provider import get_game
        g = get_game(sport, target, game_id)
        if not g:
            # get_game can miss (cache/date edge). Rebuild the teams from the slate
            # so book props still resolve — the books have the lines either way.
            try:
                slate = team_games(sport, target.isoformat())
                for row in ((slate.get("games") if isinstance(slate, dict) else slate) or []):
                    if str(row.get("id") or row.get("game_id")) == str(game_id):
                        g = {"home": row.get("home") or {}, "away": row.get("away") or {},
                             "status": row.get("status")}
                        break
            except Exception:
                g = None
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
        res = _enrich_props(sport, game_id, target,
                            get_props(sport, target, game_id), status)
        if (res or {}).get("props"):
            return res
    except Exception as e:
        print(f"[{sport}] props failed: {e}")
    # 3) STORED props: once a game ends the books pull their lines, so serve the
    #    props we already logged (with their graded actuals). This is what makes a
    #    finished game keep showing its props instead of going blank.
    try:
        import prop_tracking as PT
        with SessionLocal() as db:
            stored = PT.stored_props(db, sport, game_id)
        if stored:
            return {"game_id": game_id, "props": stored, "source": "stored",
                    "graded": any(p.get("actual") is not None for p in stored)}
    except Exception as e:
        print(f"[{sport}] stored props failed: {e}")
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


@app.post("/api/admin/hoops-upload")
def hoops_upload(payload: dict, token: str = ""):
    """Owner-only: receive the advanced hoops stats fetched from a NON-datacenter
    IP (see fetch_hoops.py, run on your PC). Saved to the Railway volume and used
    by the prop projections. stats.nba.com blocks this server, so this is how the
    real defensive ratings / usage / defense-vs-position get in."""
    admin_tok = os.environ.get("PROMO_CRON_TOKEN", "").strip()
    if not admin_tok or (token or "").strip() != admin_tok:
        return {"error": "forbidden"}
    try:
        import hoops_advanced as HA
        return HA.save_uploaded(payload)
    except Exception as e:
        return {"error": str(e)[:300]}


def _autolog_props(day=None, sports=("nba", "wnba", "nfl", "mlb")):
    """Load + log props for EVERY game on the slate, so prop tracking is a complete,
    unbiased sample instead of only the games someone happened to open. Idempotent:
    re-running refreshes ungraded props and grades any that have finished.
    Best-effort per game; one bad game never stops the sweep."""
    day = day or dt.date.today()
    out = {"date": day.isoformat(), "logged": {}, "errors": 0}
    for sport in sports:
        # only sweep sports that are actually in season today
        try:
            if day.month not in SPORT_SEASON.get(sport, set(range(1, 13))):
                continue
        except Exception:
            pass
        n = 0
        try:
            games = team_games(sport, day.isoformat())
            rows = games.get("games") if isinstance(games, dict) else games
            for g in (rows or []):
                gid = g.get("id") or g.get("game_id")
                if not gid:
                    continue
                try:
                    # going through team_props means props get logged + graded by the
                    # same path the UI uses (no duplicate logic to drift)
                    res = team_props(sport, str(gid), date=day.isoformat())
                    n += len((res or {}).get("props") or [])
                except Exception as e:
                    out["errors"] += 1
                    print(f"[autolog] {sport}/{gid}: {e}")
        except Exception as e:
            out["errors"] += 1
            print(f"[autolog] {sport} slate failed: {e}")
        if n:
            out["logged"][sport] = n
    return out


@app.get("/api/props-source-diag")
def props_source_diag(sport: str = "wnba", date: str | None = None, token: str = ""):
    """Which prop source actually works for this sport today? Shows, per game,
    what SportsGameOdds, The Odds API, and the model projection each return."""
    admin_tok = os.environ.get("PROMO_CRON_TOKEN", "").strip()
    if admin_tok and (token or "").strip() != admin_tok:
        return {"error": "forbidden"}
    day = dt.date.fromisoformat(date) if date else dt.date.today()
    out = {"sport": sport, "date": day.isoformat(), "games": []}
    try:
        games = team_games(sport, day.isoformat())
        rows = (games.get("games") if isinstance(games, dict) else games) or []
    except Exception as e:
        return {"error": f"slate failed: {str(e)[:150]}"}
    for g in rows[:3]:
        gid = g.get("id") or g.get("game_id")
        entry = {"game_id": gid,
                 "matchup": f"{(g.get('away') or {}).get('name')} @ {(g.get('home') or {}).get('name')}",
                 "status": g.get("status")}
        try:
            from espn_provider import get_game
            gg = get_game(sport, day, str(gid))
            home = (gg or {}).get("home", {}).get("name")
            away = (gg or {}).get("away", {}).get("name")
            entry["espn_teams"] = [away, home]
            try:
                import sgo_api
                entry["sgo_enabled"] = sgo_api.enabled()
                sp = sgo_api.get_player_props(sport, home, away) if sgo_api.enabled() else None
                entry["sgo_props"] = len(sp or [])
            except Exception as e:
                entry["sgo_error"] = str(e)[:120]
            try:
                import odds_api
                entry["oddsapi_enabled"] = odds_api.enabled()
                op = odds_api.get_player_props(sport, home, away) if odds_api.enabled() else None
                entry["oddsapi_props"] = len(op or [])
            except Exception as e:
                entry["oddsapi_error"] = str(e)[:120]
            try:
                from espn_provider import get_props as _mp
                mp = _mp(sport, day, str(gid))
                entry["model_props"] = len((mp or {}).get("props") or [])
            except Exception as e:
                entry["model_error"] = str(e)[:120]
            # what the ACTUAL endpoint the UI/chat calls returns (the real test)
            try:
                tp = team_props(sport, str(gid), date=day.isoformat())
                entry["team_props_count"] = len((tp or {}).get("props") or [])
                entry["team_props_source"] = (tp or {}).get("source")
            except Exception as e:
                entry["team_props_error"] = str(e)[:160]
            try:
                entry["get_game_ok"] = bool(get_game(sport, day, str(gid)))
            except Exception as e:
                entry["get_game_error"] = str(e)[:120]
        except Exception as e:
            entry["error"] = str(e)[:150]
        out["games"].append(entry)
    return out


@app.get("/api/props/autolog")
def props_autolog(date: str | None = None, token: str = ""):
    """Manually trigger the slate-wide prop logging sweep (also runs on a schedule)."""
    admin_tok = os.environ.get("PROMO_CRON_TOKEN", "").strip()
    if admin_tok and (token or "").strip() != admin_tok:
        return {"error": "forbidden"}
    day = dt.date.fromisoformat(date) if date else None
    return _autolog_props(day)


@app.get("/api/props/record")
def props_record(sport: str | None = None, days: int = 30):
    """How the PROJECTION model is performing on player props. Model-quality only —
    these results never touch the site's win/loss record, units, or ROI."""
    try:
        import prop_tracking as PT
        with SessionLocal() as db:
            return PT.prop_record(db, sport, days)
    except Exception as e:
        return {"error": str(e)[:200], "graded": 0}


@app.get("/api/props/recent")
def props_recent(sport: str | None = None, limit: int = 60):
    """Recently graded props: projection vs. line vs. what the player actually did."""
    try:
        import prop_tracking as PT
        with SessionLocal() as db:
            return {"props": PT.recent_props(db, sport, limit)}
    except Exception as e:
        return {"error": str(e)[:200], "props": []}


@app.get("/api/props/settle")
def props_settle(days: int = 3, token: str = ""):
    """Grade finished games' props with the players' ACTUAL stats. Runs on a
    schedule; can be triggered manually with the owner token."""
    admin_tok = os.environ.get("PROMO_CRON_TOKEN", "").strip()
    if admin_tok and (token or "").strip() != admin_tok:
        return {"error": "forbidden"}
    return _settle_props(days)


def _settle_props(days=3):
    """For each recent game with logged, ungraded props: pull the final boxscore and
    grade the model's leans. Best-effort; never raises."""
    graded = 0
    try:
        import prop_tracking as PT
        from espn_provider import final_player_stats
        from models import PropResult
        with SessionLocal() as db:
            since = dt.datetime.now() - dt.timedelta(days=days)
            rows = db.query(PropResult).filter(
                PropResult.settled_date >= since,
                PropResult.actual.is_(None)).all()
            pending = {}
            for r in rows:
                pending.setdefault((r.sport, r.game_ref), []).append(r)
            for (sport, ref), items in pending.items():
                if sport not in ("nba", "wnba", "nfl"):
                    continue          # MLB grading uses its own boxscore shape
                day = items[0].settled_date.date() if items[0].settled_date else dt.date.today()
                actuals = final_player_stats(sport, day, ref)
                if not actuals:
                    continue
                graded += PT.grade_props(db, sport, ref, actuals)
    except Exception as e:
        print(f"[props] settle failed: {e}")
    return {"graded": graded}


@app.get("/api/hoops-adv-diag")
def hoops_adv_diag(sport: str = "wnba"):
    """Is stats.nba.com/stats.wnba.com reachable from this server? (Datacenter IPs
    are often blocked.) Shows whether advanced stats can be used at all."""
    import time as _t
    t0 = _t.time()
    try:
        import hoops_advanced as HA
        up = HA.uploaded_status()
        ta = HA.team_advanced(sport)          # uploaded snapshot first, then live probe
        elapsed = round(_t.time() - t0, 1)
        if not ta:
            return {"uploaded": up, "reachable": False, "elapsed_sec": elapsed,
                    "health": dict(HA._health), "season": HA._season(sport),
                    "meaning": ("stats.nba.com is NOT reachable from this server "
                                "(datacenter IP likely blocked). Props still work \u2014 "
                                "they fall back to the ESPN proxy.")}
        pu = HA.player_usage(sport)
        return {"uploaded": up, "reachable": True, "elapsed_sec": elapsed, "teams": len(ta),
                "sample_team": list(ta.items())[:2],
                "usage_ok": bool(pu),
                "sample_player": (list(pu.items())[:2] if pu else None),
                "season": HA._season(sport)}
    except Exception as e:
        return {"reachable": False, "error": str(e)[:300],
                "elapsed_sec": round(_t.time() - t0, 1)}


@app.get("/api/prop-diag")
def prop_diag(player: str, sport: str = "wnba", stat: str = "points"):
    """Diagnostic: shows exactly where WNBA prop projection resolution succeeds or
    fails (search hits, resolved athlete id, gamelog labels + event count)."""
    out = {"player": player, "sport": sport, "stat": stat}
    try:
        from espn_provider import _athlete_id_by_name, _get, GAMELOG, _LOG_LABEL
        sdata = _get("https://site.web.api.espn.com/apis/search/v2",
                     {"query": player, "limit": 8})
        hits = []
        for grp in (sdata.get("results") or []):
            for it in (grp.get("contents") or []):
                if it.get("type") == "player":
                    hits.append({"name": it.get("displayName"),
                                 "subtitle": it.get("subtitle"), "uid": it.get("uid")})
        out["search_player_hits"] = hits[:6]
        pid = _athlete_id_by_name(sport, player)
        out["resolved_pid"] = pid
        out["stat_col_target"] = _LOG_LABEL.get((stat or "").lower())
        if pid and sport in GAMELOG:
            gdata = _get(GAMELOG[sport].format(pid=pid))
            out["gamelog_names"] = gdata.get("names")
            out["gamelog_labels"] = gdata.get("labels")
            st = gdata.get("seasonTypes") or []
            out["gamelog_event_count"] = sum(
                len(cat.get("events", [])) for s in st for cat in (s.get("categories") or []))
    except Exception as e:
        out["error"] = str(e)[:400]
    return out


@app.get("/api/{sport}/prop-history/{game_id}")
def team_prop_history(sport: str, game_id: str, player: str, stat: str,
                      line: float, date: str | None = None):
    if sport not in ("nba", "wnba", "nfl"):
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
    if sport not in ("nba", "wnba", "nfl", "mlb"):
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


# ---- favicon / app icons (embedded so they survive a forgotten git add) ----
import base64 as _b64
@app.get("/")
@app.head("/")
def index():
    # No-cache so every deploy reaches the browser immediately. index.html is
    # small; the cost is negligible and it prevents stale-frontend confusion.
    # HEAD is accepted too so platform health checks don't get a 405.
    return FileResponse("index.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})


def _settle_ladder():
    """Grade the current ladder leg once its game is final, using the SAME win/loss
    the pick record uses (PickResult). Rolls or resets the challenge. Best-effort."""
    try:
        import ladder as L
        from models import LadderLeg, PickResult
        with SessionLocal() as db:
            leg = (db.query(LadderLeg)
                     .filter(LadderLeg.settled == False)  # noqa: E712
                     .order_by(LadderLeg.pick_date.asc()).first())
            if not leg or not leg.game_ref:
                return {"settled": 0}
            pr = (db.query(PickResult)
                    .filter(PickResult.sport == leg.sport,
                            PickResult.ref == leg.game_ref).first())
            if not pr or pr.correct is None:
                return {"settled": 0}          # game not graded yet
            L.settle_leg(db, leg, bool(pr.correct))
            return {"settled": 1, "result": leg.result}
    except Exception as e:
        print(f"[ladder] settle failed: {e}")
        return {"settled": 0, "error": str(e)[:150]}


@app.get("/api/ladder/settle")
def ladder_settle(token: str = ""):
    admin_tok = os.environ.get("PROMO_CRON_TOKEN", "").strip()
    if admin_tok and (token or "").strip() != admin_tok:
        return {"error": "forbidden"}
    return _settle_ladder()


# ═══════════════════════════════════════════════════════════════════════════════
# LINE LOGIC DISCORD BOT INTEGRATION
#   /api/model     — single-team read for the bot's /model
#   /api/generate  — unified AI engine (/explain, /why, /tweet, /ask, ...)
#   notify_discord — backend -> bot webhook push
# Reuses _ensure_day, _gather_plays, _enrich_odds. Safe to delete this block.
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/model")
def model_lookup(team: str, sport: str | None = None, days: int = 3):
    """On-demand model read for one team/player across today..today+days."""
    needle = (team or "").strip().lower()
    if not needle:
        return {"found": False, "error": "no team provided"}
    days = max(1, min(int(days or 1), 7))
    today = dt.date.today()
    for offset in range(days):
        target = today + dt.timedelta(days=offset)
        try:
            _ensure_day(target)
            plays = _gather_plays(target)
        except Exception as e:
            print(f"[model_lookup] gather failed for {target}: {e}")
            continue
        for p in plays:
            if sport and p.get("sport") != sport:
                continue
            hay = (str(p.get("match", "")) + " " + str(p.get("pick", ""))).lower()
            if needle not in hay:
                continue
            p = dict(p)
            try:
                _enrich_odds(p)
            except Exception:
                pass
            prob = p.get("prob") or 0
            edge = p.get("edge_pct")
            return {
                "found": True, "date": target.isoformat(), "sport": p.get("sport"),
                "match": p.get("match"), "pick": p.get("pick"), "prob": prob,
                "win_pct": round(prob * 100), "confidence": p.get("confidence"),
                "market_odds": p.get("market_odds"), "fair_odds": p.get("fair_odds"),
                "edge_pct": edge, "has_edge": bool(edge is not None and edge > 0),
                "event_time": p.get("event_time"),
            }
    return {"found": False, "team": team,
            "note": f"No game found for '{team}' in the next {days} day(s)."}


# ---- /api/generate : unified AI engine (Claude Haiku -> Gemini -> OpenAI) ----
_LLM_ANTHROPIC = (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY") or "").strip()
_LLM_GEMINI = os.environ.get("GEMINI_API_KEY", "").strip()
_LLM_OPENAI = os.environ.get("OPENAI_API_KEY", "").strip()
_ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

_LL_SYSTEM_PROMPT = (
    "You are a sports analytics writer for Line Logic. You explain WHY a "
    "mathematical model reached its projection, in plain, professional language. "
    "HARD RULES: Never invent stats, names, records, or reasons. Use ONLY the "
    "facts provided; if a fact isn't provided, don't mention it. Never use hype "
    "words (lock, guaranteed, insane, free money, max bet). Outcomes are "
    "high-variance; no single bet is guaranteed. When useful, explain how the "
    "model's win probability compares to the market price to create positive "
    "expected value."
)
_LL_ASK_SYSTEM_PROMPT = _LL_SYSTEM_PROMPT + (
    " You are answering a user's question about this play. Answer ONLY from the "
    "facts provided. If the facts don't contain the answer, say: 'The current "
    "Line Logic model doesn't include enough information to answer that "
    "confidently.' If the user asks how much to bet, whether to parlay, or any "
    "staking/bankroll-sizing question, DO NOT advise — reply that bet sizing is "
    "personal and point them to /learn unit and /learn bankroll."
)
_LL_MODE_PROMPTS = {
    "why": ("Give 3-5 short bullet reasons the model favors this side, each under "
            "12 words. Output plain lines starting with '- '. No preamble.", 300),
    "writeup": ("Write a 500-700 word analysis with these sections, each a short "
                "header: Overview, Model, Matchup, Market, Risks, Final Thoughts. "
                "Professional, no hype.", 1200),
    "full": ("Write a clean 3-paragraph breakdown of how the model's projection "
             "lines up against the market price. No hype.", 600),
    "confidence": ("Explain what the model's confidence grade means for THIS play "
                   "in 2-3 sentences: what makes the opportunity above or below "
                   "average. End by conveying, in your own words: confidence "
                   "reflects the quality of the betting opportunity, not the "
                   "certainty of the outcome — even high-confidence plays lose "
                   "sometimes.", 400),
    "tweet": ("Write a single X/Twitter post under 240 characters. One line on the "
              "play, then model %, market price, and edge as short stat lines. "
              "Calm and factual, no hashtag spam, no hype words.", 200),
    "discord": ("Write a short Discord-friendly blurb: one sentence of context, then "
                "model %, market, and edge as bold stat lines.", 300),
    "newsletter": ("Write a 150-250 word newsletter blurb: a friendly lead, the "
                   "model's read, and one line on the main risk.", 500),
    "article": ("Write a 1200-1500 word article-style breakdown for the website. "
                "Clear sections, informative, no hype.", 2200),
}
_LL_DISC_SHORT = "\n\nValue play, not a guaranteed outcome."
_LL_DISC_TWEET = "\n\nValue play — not a guarantee."
_LL_DISC_LONG = ("\n\n—\nLine Logic explanations describe the model's reasoning. "
                 "Betting involves risk and variance; no outcome is guaranteed. "
                 "Bet responsibly.")


def _ll_disclaimer_for(mode: str) -> str:
    if mode == "tweet":
        return _LL_DISC_TWEET
    if mode in ("writeup", "article", "newsletter"):
        return _LL_DISC_LONG
    return _LL_DISC_SHORT


def _ll_facts_from_play(play: dict) -> dict:
    facts = {}
    for k in ("match", "pick", "win_pct", "market_odds", "fair_odds",
              "edge_pct", "confidence", "sport"):
        v = play.get(k)
        if v is not None:
            facts[k] = v
    return facts


def _ll_sources_from_play(play: dict) -> dict:
    src = {"Model projection": "Line Logic model"}
    if play.get("market_odds") is not None:
        src["Market odds"] = "Sportsbook consensus (your odds provider)"
    return src


async def _ll_post_anthropic(system, user, max_tokens):
    import httpx
    async with httpx.AsyncClient(timeout=14.0) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": _LLM_ANTHROPIC, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": _ANTHROPIC_MODEL, "max_tokens": max_tokens,
                  "system": system, "messages": [{"role": "user", "content": user}]})
        if r.status_code >= 400:
            print(f"[anthropic] HTTP {r.status_code} using model='{_ANTHROPIC_MODEL}': {r.text[:400]}")
        r.raise_for_status()
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


async def _ll_post_gemini(system, user, max_tokens):
    import httpx
    async with httpx.AsyncClient(timeout=14.0) as c:
        r = await c.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
            params={"key": _LLM_GEMINI},
            json={"contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
                  "generationConfig": {"maxOutputTokens": max_tokens}})
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


async def _ll_post_openai(system, user, max_tokens):
    import httpx
    async with httpx.AsyncClient(timeout=14.0) as c:
        r = await c.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {_LLM_OPENAI}"},
            json={"model": "gpt-4o-mini", "max_tokens": max_tokens, "temperature": 0.4,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


async def _ll_run_llm(system, user, max_tokens):
    for has_key, fn, name in (
        (_LLM_ANTHROPIC, _ll_post_anthropic, "anthropic"),
        (_LLM_GEMINI, _ll_post_gemini, "gemini"),
        (_LLM_OPENAI, _ll_post_openai, "openai"),
    ):
        if not has_key:
            continue
        try:
            out = await fn(system, user, max_tokens)
            if out:
                return out
        except Exception as e:
            print(f"[generate] {name} failed, falling through: {e}")
    return None


async def _ll_generate_impl(q: str, mode: str, question: str = ""):
    play = model_lookup(team=q)
    if not play or not play.get("found"):
        return {"found": False, "q": q}
    base = {"found": True, "mode": mode, "pick": play.get("pick"),
            "match": play.get("match"), "win_pct": play.get("win_pct"),
            "market_odds": play.get("market_odds"), "edge_pct": play.get("edge_pct"),
            "confidence": play.get("confidence")}
    if mode == "sources":
        return {**base, "sources": _ll_sources_from_play(play)}
    facts = _ll_facts_from_play(play)
    if mode == "ask":
        user = f"Facts (use only these):\n{facts}\n\nUser question: {question}"
        text = await _ll_run_llm(_LL_ASK_SYSTEM_PROMPT, user, 500)
        return {**base, "answer": (text or "") + (_LL_DISC_SHORT if text else "")}
    instruction, max_tokens = _LL_MODE_PROMPTS.get(mode, _LL_MODE_PROMPTS["full"])
    user = f"Facts (use only these):\n{facts}\n\nTask: {instruction}"
    text = await _ll_run_llm(_LL_SYSTEM_PROMPT, user, max_tokens)
    if not text:
        return {**base, "content": None}
    text += _ll_disclaimer_for(mode)
    if mode == "why":
        bullets = [ln.lstrip("-• ").strip() for ln in text.splitlines()
                   if ln.strip() and not ln.strip().startswith("Value play")]
        return {**base, "bullets": bullets[:6]}
    return {**base, "content": text}


@app.get("/api/generate")
async def generate_get(q: str, mode: str = "full", question: str = ""):
    return await _ll_generate_impl(q, mode, question)


@app.post("/api/generate")
async def generate_post(payload: dict):
    return await _ll_generate_impl(
        payload.get("q") or payload.get("team") or "",
        payload.get("mode", "full"), payload.get("question", ""))


# ---- notify_discord : push a post to the bot's webhook ----
_DISCORD_BOT_SERVICE_URL = os.environ.get("DISCORD_BOT_SERVICE_URL", "").strip()
_LL_WEBHOOK_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET", "").strip()


async def notify_discord(channel_key: str, title: str, description: str = "",
                         fields: list | None = None, notify_sport: str | None = None):
    if not _DISCORD_BOT_SERVICE_URL:
        print("[notify_discord] DISCORD_BOT_SERVICE_URL not set; skipping.")
        return False
    import httpx
    url = f"{_DISCORD_BOT_SERVICE_URL}/post/{channel_key}"
    payload = {"title": title, "description": description, "fields": fields or []}
    if notify_sport:
        payload["notify_sport"] = notify_sport
    headers = {"X-Webhook-Secret": _LL_WEBHOOK_SECRET}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return True
    except Exception as e:
        print(f"[notify_discord] failed: {e}")
        return False


# ---- /api/picks/quick : board with odds, NO LLM narration (fast) -------------
@app.get("/api/picks/quick")
def picks_quick(date: str | None = None, sport: str | None = None,
                min_prob: float = 0.0, min_edge: float = 0.0):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_day(target)
    plays = _gather_plays(target)
    out = []
    for p in plays:
        if sport and p.get("sport") != sport:
            continue
        if (p.get("prob") or 0) < min_prob:
            continue
        p = dict(p)
        try:
            _enrich_odds(p)
        except Exception:
            pass
        if min_edge > 0 and (p.get("edge_pct") is None or p.get("edge_pct", 0) < min_edge):
            continue
        out.append({
            "sport": p.get("sport"), "match": p.get("match"), "pick": p.get("pick"),
            "prob": p.get("prob"), "confidence": p.get("confidence"),
            "market_odds": p.get("market_odds"), "fair_odds": p.get("fair_odds"),
            "edge_pct": p.get("edge_pct"), "event_time": p.get("event_time"),
        })
    return {"date": target.isoformat(), "count": len(out), "picks": out}


# ---- /api/slate : counts per sport only, NO odds enrichment (instant) --------
@app.get("/api/slate")
def slate_counts(date: str | None = None):
    target = dt.date.fromisoformat(date) if date else dt.date.today()
    _ensure_day(target)
    plays = _gather_plays(target)
    from collections import Counter
    counts = Counter(str(p.get("sport", "")).lower() for p in plays if p.get("sport"))
    return {"date": target.isoformat(), "total": sum(counts.values()),
            "counts": dict(counts)}
