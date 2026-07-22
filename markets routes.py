"""
markets_routes.py — /api/markets, the per-game line board behind the
"log a bet from a game" picker. Kept off main.py to hold its size down.

Also serves /api/markets/game — the Moneyline / Spread / Total model-vs-market
view for one game, used by the three-button panel on every matchup. That view
reaches into main.py (deferred, to avoid a circular import) for the model's
probability + score projection, and into odds_api / sgo_api for the live market
lines and prices.
"""
import time as _t
import datetime as dt

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import espn_markets

router = APIRouter()

_cache = {}          # (sport, ymd) -> (ts, games)
_TTL = 120

_mv_cache = {}       # (sport, ref) -> (ts, view)
_MV_TTL = 90


@router.get("/api/markets")
def markets(sport: str, date: str | None = None):
    sport = (sport or "").lower().strip()
    if sport not in espn_markets.LEAGUES:
        return JSONResponse(
            {"error": "unsupported sport", "supported": sorted(espn_markets.LEAGUES)},
            status_code=400)
    ymd = date.replace("-", "") if date else dt.date.today().strftime("%Y%m%d")
    key = (sport, ymd)
    hit = _cache.get(key)
    if hit and _t.time() - hit[0] < _TTL:
        games = hit[1]
    else:
        games = espn_markets.game_markets(sport, ymd)
        _cache[key] = (_t.time(), games)
    # hide finished games; keep upcoming + live ones that still have a line
    games = [g for g in games if g.get("state") != "post"]
    return {"sport": sport, "date": ymd, "count": len(games), "games": games}


def _M():
    import main
    return main


def _unified_market(sport, home, away):
    """Best available market record for one game: The Odds API first (already
    carries spread/total prices after the odds_api patch), then the free SGO
    full-market read. Returns {} when neither has it."""
    import main as _main
    rec = {}
    # The Odds API (team-sport book), matched by normalized team names
    try:
        import odds_api
        if odds_api.enabled() and sport in getattr(odds_api, "SPORT_KEY", {}):
            book = odds_api.get_odds(sport) or {}
            o = book.get(_main._norm_team(home) + "|" + _main._norm_team(away))
            if o:
                mlh, mla = _main._odds_rec_sides(home, o)
                rec = {"ml_home": mlh, "ml_away": mla,
                       "spread_home": o.get("spread_home"), "spread_away": o.get("spread_away"),
                       "spread_home_price": o.get("spread_home_price"),
                       "spread_away_price": o.get("spread_away_price"),
                       "total": o.get("total"),
                       "total_over_price": o.get("total_over_price"),
                       "total_under_price": o.get("total_under_price"),
                       "ml_home_best": o.get("ml_home_best"), "ml_home_book": o.get("ml_home_book"),
                       "ml_away_best": o.get("ml_away_best"), "ml_away_book": o.get("ml_away_book")}
    except Exception as e:
        print(f"[markets] odds-api {sport} skipped: {e}")
    # Fill any gaps from SGO's free full-market read
    try:
        import sgo_api
        if sgo_api.available():
            need = (not rec or rec.get("spread_home") is None or rec.get("total") is None
                    or rec.get("ml_home") is None)
            if need:
                sm = sgo_api.get_game_markets(sport, home, away)
                if sm:
                    for k, v in sm.items():
                        if rec.get(k) is None:
                            rec[k] = v
    except Exception as e:
        print(f"[markets] sgo {sport} skipped: {e}")
    return rec


@router.get("/api/markets/game")
def market_game(sport: str, ref: str, best_of: int = 3):
    """Moneyline / Spread / Total, model vs market, for one game.

    `ref` is the play id used across the app (same id /track resolves). We pull
    the model's probability + any score projection from the live board, the
    market lines/prices from the odds sources, and hand both to
    model_markets.market_view."""
    sport = (sport or "").lower().strip()
    hit = _mv_cache.get((sport, ref))
    if hit and _t.time() - hit[0] < _MV_TTL:
        return hit[1]

    main = _M()
    try:
        import model_markets
    except Exception as e:
        return JSONResponse({"error": f"engine unavailable: {e}"}, status_code=500)

    # locate the play on the board by id (searches today..+3 like model_lookup)
    play = None
    try:
        import datetime as _dt
        today = _dt.date.today()
        for off in range(4):
            d = today + _dt.timedelta(days=off)
            try:
                main._ensure_day(d)
                plays = main._gather_plays(d)
            except Exception:
                plays = []
            for p in plays:
                if str(p.get("id")) == str(ref) and (not sport or p.get("sport") == sport):
                    play = dict(p)
                    break
            if play:
                break
    except Exception as e:
        return JSONResponse({"error": f"lookup failed: {e}"}, status_code=500)

    if not play:
        return JSONResponse({"error": "not_found", "ref": ref}, status_code=404)

    sport = play.get("sport") or sport
    match = play.get("match") or ""
    import re as _re
    # "Away @ Home" (team sports) vs "A vs B" (tennis/UFC, where the board's
    # prob_home is built from the FIRST name = player A). Order differs by
    # delimiter, so detect which one is present.
    if _re.search(r"\s+@\s+", match):
        parts = _re.split(r"\s+@\s+", match, maxsplit=1)
        away_nm, home_nm = (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (match, match)
    else:
        parts = _re.split(r"\s+vs\.?\s+", match, maxsplit=1)
        home_nm, away_nm = (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (match, match)

    prob_home = play.get("prob_home")
    if prob_home is None:
        # Board plays carry the FAVORITE's prob (always >= .5) plus the pick
        # label "<Team> to win" — not a home/away prob. Reconstruct the HOME
        # prob by matching the pick's team name to home vs away. The old code
        # matched pick.split()[-1] which is always "win", so it silently took
        # the else-branch and inverted every game. Match on real names instead.
        pk = _re.sub(r"\s+to win\s*$", "", (play.get("pick") or ""), flags=_re.I).strip().lower()
        pr = play.get("prob")

        def _tok(s):
            return set(w for w in _re.sub(r"[^a-z ]", " ", (s or "").lower()).split()
                       if len(w) > 2)
        if pr is not None and pk:
            hset, aset, pset = _tok(home_nm), _tok(away_nm), _tok(pk)
            home_hit = len(pset & hset)
            away_hit = len(pset & aset)
            if home_hit > away_hit:
                prob_home = pr                    # pick IS the home team
            elif away_hit > home_hit:
                prob_home = 1 - pr                # pick is the away team
            else:
                # fall back to substring test, then to prob as-is
                if home_nm and pk and (pk in home_nm.lower() or home_nm.lower() in pk):
                    prob_home = pr
                elif away_nm and pk and (pk in away_nm.lower() or away_nm.lower() in pk):
                    prob_home = 1 - pr
                else:
                    prob_home = pr
        else:
            prob_home = pr
    ctx = play.get("ctx") or {}
    exp_home = ctx.get("exp_runs_home", ctx.get("exp_goals_home"))
    exp_away = ctx.get("exp_runs_away", ctx.get("exp_goals_away"))
    if sport == "soccer" and exp_home is None:
        exp_home, exp_away = ctx.get("exp_goals_home"), ctx.get("exp_goals_away")

    market = _unified_market(sport, home_nm, away_nm)
    view = model_markets.market_view(
        sport, prob_home, market=market,
        exp_home=exp_home, exp_away=exp_away, best_of=best_of,
        home=home_nm, away=away_nm)
    view["ref"] = ref
    _mv_cache[(sport, ref)] = (_t.time(), view)
    return view
