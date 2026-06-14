"""
The Odds API integration (the-odds-api.com) for real sportsbook lines.

Honest scope notes:
- FREE tier gives live/upcoming odds. We capture the line when a pick is first
  seen ("opening" we record) and refresh toward game time. The latest pre-game
  line is used as a CLOSING proxy.
- TRUE historical closing odds is a PAID endpoint. Without it, CLV is computed
  against our best available near-close line, and we label it as such rather
  than implying a verified official close.
- Quota is limited (free ~500 req/month), so every call is cached hard and we
  fetch one combined request (h2h) per league per refresh.

Set ODDS_API_KEY in the environment to enable. With no key, everything degrades
to the model's own fair odds and the betting metrics show as "needs odds key".
"""
import os
import time
import datetime as dt

API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
BASE = "https://api.the-odds-api.com/v4"

# --- quota governor -------------------------------------------------------
# Free tier is ~500 requests/MONTH. We enforce a hard DAILY ceiling so traffic
# spikes can never drain the month, and we also obey the live remaining-count
# header the API returns. Tennis is split per-tournament (each its own sport
# key), so it is the heaviest consumer; the ceiling keeps it safe.
_DAILY_MAX = int(os.environ.get("ODDS_DAILY_MAX", "30"))   # requests/day, all sports
_MIN_REMAINING = 20    # stop calling if the API says this few are left this month
_spend = {"day": None, "count": 0}


def _quota_ok():
    today = dt.date.today().isoformat()
    if _spend["day"] != today:
        _spend["day"] = today
        _spend["count"] = 0
    if _spend["count"] >= _DAILY_MAX:
        return False
    rem = _quota.get("remaining")
    try:
        if rem is not None and int(rem) <= _MIN_REMAINING:
            return False
    except (ValueError, TypeError):
        pass
    return True


def _spend_one():
    _spend["count"] = _spend.get("count", 0) + 1

SPORT_KEY = {
    "mlb": "baseball_mlb",
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "nhl": "icehockey_nhl",
    "ncaaf": "americanfootball_ncaaf",
    "ncaab": "basketball_ncaab",
    "ncaabb": "baseball_ncaa",
    # Note: women's college basketball (wncaab) has no market on The Odds API,
    # so it is intentionally omitted and stays model-only. The active-season
    # gate below means a missing/inactive key never costs a quota call anyway.
}

# Tennis is split into per-tournament sport keys (e.g. tennis_atp_french_open).
# We discover the currently-active ones from /sports, cache that list for a day,
# then pull h2h for each. Heavily throttled to protect the monthly quota.
_tennis_keys_cache = {"ts": 0.0, "keys": []}
_TENNIS_KEYS_TTL = 12 * 3600     # refresh active-tournament list twice a day
_TENNIS_ODDS_TTL = 6 * 3600      # odds per tournament refreshed every 6h
_tennis_odds_cache = {}          # sport_key -> (ts, {match_key: odds})

_cache = {}        # sport -> (ts, {match_key: {...odds...}})
_TTL = 900         # 15 min; protects the monthly quota
_quota = {"remaining": None, "used": None}

# In-season ("active") sport keys, from the free /sports list. Used to avoid
# spending a paid odds call on an off-season league (e.g. college football in
# June). Refreshed twice a day; /sports does not count against the quota.
_active_cache = {"ts": 0.0, "keys": set()}
_ACTIVE_TTL = 12 * 3600


def enabled():
    return bool(API_KEY)


def _norm(name):
    return "".join(c for c in (name or "").lower() if c.isalnum())


def american_from_decimal(dec):
    try:
        dec = float(dec)
    except (TypeError, ValueError):
        return None
    if dec <= 1.0:
        return None
    if dec >= 2.0:
        return round((dec - 1) * 100)
    return round(-100 / (dec - 1))


def _active_sport_keys():
    """Set of in-season sport keys from /sports (all=false). This endpoint is
    free (does NOT count against the quota), so we use it to skip a paid odds
    call on a league that isn't currently offered. If the list can't be
    fetched we return whatever we last had (and get_odds will simply proceed)."""
    if not API_KEY:
        return set()
    if time.time() - _active_cache["ts"] < _ACTIVE_TTL and _active_cache["keys"]:
        return _active_cache["keys"]
    try:
        import httpx
        r = httpx.get(f"{BASE}/sports", params={"apiKey": API_KEY, "all": "false"}, timeout=12)
        r.raise_for_status()
        keys = {s.get("key") for s in r.json() if s.get("active")}
    except Exception as e:
        print(f"[odds] active-sport list failed: {e}")
        return _active_cache["keys"]
    if keys:
        _active_cache["ts"] = time.time()
        _active_cache["keys"] = keys
    return keys


def get_odds(sport: str):
    """
    Return {match_key: {home, away, h2h:{home_team, away_team, prices...},
    spread, total, books, fetched}} for a league. match_key is
    norm(home)+'|'+norm(away). Cached; quota-aware. Empty dict if no key.
    """
    if not API_KEY or sport not in SPORT_KEY:
        return {}
    c = _cache.get(sport)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    if not _quota_ok():
        return _cache.get(sport, (0, {}))[1]   # serve last cache; protect quota
    # Don't spend a paid call on an off-season / unoffered league. The /sports
    # check is free; if it says this league isn't active right now, cache empty
    # briefly and bail. (When the season starts it reappears and we resume.)
    active = _active_sport_keys()
    if active and SPORT_KEY[sport] not in active:
        _cache[sport] = (time.time(), {})
        return {}
    url = f"{BASE}/sports/{SPORT_KEY[sport]}/odds"
    params = {"regions": "us", "markets": "h2h,spreads,totals",
              "oddsFormat": "american", "apiKey": API_KEY}
    try:
        import httpx
        r = httpx.get(url, params=params, timeout=12)
        _spend_one()
        _quota["remaining"] = r.headers.get("x-requests-remaining")
        _quota["used"] = r.headers.get("x-requests-used")
        r.raise_for_status()
        games = r.json()
    except Exception as e:
        print(f"[odds] {sport} fetch failed: {e}")
        return _cache.get(sport, (0, {}))[1]

    out = {}
    for g in games:
        home, away = g.get("home_team"), g.get("away_team")
        if not home or not away:
            continue
        key = _norm(home) + "|" + _norm(away)
        ml = {}          # team -> american price (consensus = median later)
        spreads = {}
        totals = {}
        books = 0
        for bk in g.get("bookmakers", []) or []:
            books += 1
            for mkt in bk.get("markets", []) or []:
                mkey = mkt.get("key")
                for oc in mkt.get("outcomes", []) or []:
                    nm = oc.get("name")
                    price = oc.get("price")
                    point = oc.get("point")
                    if mkey == "h2h" and nm and price is not None:
                        ml.setdefault(nm, []).append(price)
                    elif mkey == "spreads" and nm and point is not None:
                        spreads.setdefault(nm, []).append(point)
                    elif mkey == "totals" and nm and point is not None:
                        totals.setdefault(nm.lower(), []).append(point)
        def med(xs):
            if not xs:
                return None
            xs = sorted(xs)
            n = len(xs)
            return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
        rec = {
            "home_team": home, "away_team": away,
            "ml_home": med(ml.get(home, [])), "ml_away": med(ml.get(away, [])),
            "spread_home": med(spreads.get(home, [])),
            "spread_away": med(spreads.get(away, [])),
            "total": med(totals.get("over", [])),
            "books": books,
            "fetched": dt.datetime.utcnow().isoformat(),
        }
        # store under BOTH orderings; The Odds API's home/away can differ from
        # ESPN's, and the consumer remaps prices by team name.
        out[_norm(home) + "|" + _norm(away)] = rec
        out[_norm(away) + "|" + _norm(home)] = rec
    _cache[sport] = (time.time(), out)
    return out


SOCCER_SPORTKEYS = {
    "epl": "soccer_epl",
    "ucl": "soccer_uefa_champs_league",
    "uel": "soccer_uefa_europa_league",
    "uecl": "soccer_uefa_europa_conference_league",
    "laliga": "soccer_spain_la_liga",
    "seriea": "soccer_italy_serie_a",
    "bundesliga": "soccer_germany_bundesliga",
    "ligue1": "soccer_france_ligue_one",
    "mls": "soccer_usa_mls",
    "ligamx": "soccer_mexico_ligamx",
    "championship": "soccer_england_efl_champ",
    "eredivisie": "soccer_netherlands_eredivisie",
    "ligaportugal": "soccer_portugal_primeira_liga",
    "saudi": "soccer_spl",
    "worldcup": "soccer_fifa_world_cup",
}
_soccer_cache = {}        # league slug -> (ts, {match_key: {...}})


def _imp_amer(o):
    try:
        o = float(o)
    except (TypeError, ValueError):
        return None
    return 100.0 / (o + 100) if o > 0 else (-o) / ((-o) + 100)


def _amer_from_prob(p):
    if not p or p <= 0 or p >= 1:
        return None
    return -round(100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def diag():
    """Snapshot of odds availability so /api/odds/diag can explain a blank board:
    key present?, monthly remaining, today's spend vs cap, in-season keys, and
    how many games are cached per sport."""
    try:
        active = sorted(_active_sport_keys())
    except Exception:
        active = []
    return {
        "key_present": bool(API_KEY),
        "monthly_remaining": _quota.get("remaining"),
        "monthly_used": _quota.get("used"),
        "daily_spent": _spend.get("count"),
        "daily_max": _DAILY_MAX,
        "min_remaining_floor": _MIN_REMAINING,
        "quota_ok": _quota_ok(),
        "active_sport_keys": active,
        "cached": {k: len(v[1]) for k, v in _cache.items()},
        "cached_soccer": {k: len(v[1]) for k, v in _soccer_cache.items()},
    }


def get_soccer_odds(slug):
    """Real 3-way (home / draw / away) soccer odds via The Odds API, keyed by
    norm(home)+'|'+norm(away). Each value: {ml_home, ml_draw, ml_away, books,
    home_team, away_team} as consensus American odds across books. Quota-aware
    and cached. Returns {} with no key or when the league isn't currently
    offered. Reuses the existing ODDS_API_KEY (no new account needed)."""
    if not API_KEY or slug not in SOCCER_SPORTKEYS:
        return {}
    sk = SOCCER_SPORTKEYS[slug]
    c = _soccer_cache.get(slug)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    if not _quota_ok():
        return _soccer_cache.get(slug, (0, {}))[1]
    active = _active_sport_keys()
    if active and sk not in active:
        _soccer_cache[slug] = (time.time(), {})   # not offered right now; cheap bail
        return {}
    url = f"{BASE}/sports/{sk}/odds"
    params = {"regions": "us,uk,eu", "markets": "h2h",
              "oddsFormat": "american", "apiKey": API_KEY}
    try:
        import httpx
        r = httpx.get(url, params=params, timeout=12)
        _spend_one()
        _quota["remaining"] = r.headers.get("x-requests-remaining")
        _quota["used"] = r.headers.get("x-requests-used")
        r.raise_for_status()
        games = r.json()
    except Exception as e:
        print(f"[odds] soccer {slug} fetch failed: {e}")
        return _soccer_cache.get(slug, (0, {}))[1]
    out = {}
    for g in games:
        home, away = g.get("home_team"), g.get("away_team")
        if not home or not away:
            continue
        hp, dp, ap = [], [], []         # implied probs per outcome across books
        nbk = 0
        for bk in g.get("bookmakers", []) or []:
            seen = False
            for mkt in bk.get("markets", []) or []:
                if mkt.get("key") != "h2h":
                    continue
                for oc in mkt.get("outcomes", []) or []:
                    nm, price = (oc.get("name") or ""), oc.get("price")
                    ip = _imp_amer(price)
                    if ip is None:
                        continue
                    if nm == home:
                        hp.append(ip); seen = True
                    elif nm == away:
                        ap.append(ip); seen = True
                    elif nm.lower() == "draw":
                        dp.append(ip)
            if seen:
                nbk += 1
        if not hp or not ap:
            continue
        avg = lambda xs: (sum(xs) / len(xs)) if xs else None
        out[_norm(home) + "|" + _norm(away)] = {
            "ml_home": _amer_from_prob(avg(hp)),
            "ml_draw": _amer_from_prob(avg(dp)) if dp else None,
            "ml_away": _amer_from_prob(avg(ap)),
            "books": nbk, "home_team": home, "away_team": away,
        }
    _soccer_cache[slug] = (time.time(), out)
    return out


def quota():
    return dict(_quota)


def _active_tennis_keys():
    """Discover currently-active tennis tournament sport keys (cached 12h)."""
    if time.time() - _tennis_keys_cache["ts"] < _TENNIS_KEYS_TTL and _tennis_keys_cache["keys"]:
        return _tennis_keys_cache["keys"]
    if not _quota_ok():
        return _tennis_keys_cache["keys"]
    try:
        import httpx
        # /sports does NOT count against quota (per docs it's free to list)
        r = httpx.get(f"{BASE}/sports", params={"apiKey": API_KEY, "all": "false"}, timeout=12)
        r.raise_for_status()
        keys = [s["key"] for s in r.json()
                if s.get("key", "").startswith("tennis_") and s.get("active")]
    except Exception as e:
        print(f"[odds] tennis key discovery failed: {e}")
        return _tennis_keys_cache["keys"]
    _tennis_keys_cache["ts"] = time.time()
    _tennis_keys_cache["keys"] = keys
    return keys


def get_tennis_odds():
    """
    Combined {match_key: odds} across active ATP/WTA tournaments. h2h only,
    each tournament cached 6h and gated by the daily quota ceiling. match_key is
    norm(playerA)+'|'+norm(playerB) for BOTH orderings so lookups are robust.
    """
    if not API_KEY:
        return {}
    combined = {}
    for key in _active_tennis_keys():
        c = _tennis_odds_cache.get(key)
        if c and time.time() - c[0] < _TENNIS_ODDS_TTL:
            combined.update(c[1])
            continue
        if not _quota_ok():
            # out of budget for now; use whatever we have cached
            if c:
                combined.update(c[1])
            continue
        try:
            import httpx
            r = httpx.get(f"{BASE}/sports/{key}/odds",
                          params={"regions": "us", "markets": "h2h",
                                  "oddsFormat": "american", "apiKey": API_KEY},
                          timeout=12)
            _spend_one()
            _quota["remaining"] = r.headers.get("x-requests-remaining")
            _quota["used"] = r.headers.get("x-requests-used")
            r.raise_for_status()
            games = r.json()
        except Exception as e:
            print(f"[odds] tennis {key} failed: {e}")
            if c:
                combined.update(c[1])
            continue
        book = {}
        for g in games:
            a, b = g.get("home_team"), g.get("away_team")
            if not a or not b:
                continue
            prices = {}
            for bk in g.get("bookmakers", []) or []:
                for mkt in bk.get("markets", []) or []:
                    if mkt.get("key") != "h2h":
                        continue
                    for oc in mkt.get("outcomes", []) or []:
                        if oc.get("name") and oc.get("price") is not None:
                            prices.setdefault(oc["name"], []).append(oc["price"])
            def med(xs):
                xs = sorted(xs); n = len(xs)
                return None if not xs else (xs[n//2] if n % 2 else (xs[n//2-1]+xs[n//2])/2)
            pa, pb = med(prices.get(a, [])), med(prices.get(b, []))
            rec = {"a": a, "b": b, "odds_a": pa, "odds_b": pb}
            # store under both name orderings for robust matching
            book[_norm(a) + "|" + _norm(b)] = rec
            book[_norm(b) + "|" + _norm(a)] = rec
        _tennis_odds_cache[key] = (time.time(), book)
        combined.update(book)
    return combined


def spend_today():
    """How many requests we've used today (for diagnostics)."""
    return {"day": _spend.get("day"), "count": _spend.get("count"), "cap": _DAILY_MAX}


# ===== real sportsbook PLAYER PROPS (per-event endpoint) ====================
# Player props live on The Odds API's per-event odds endpoint and cost
# [markets x regions] credits each, so we keep the market list focused, cache
# aggressively (15 min) and gate every paid call through the quota governor.
# The /events lookup used to resolve the event id is FREE (no quota cost).

PROP_MARKETS = {
    "mlb":   {"pitcher_strikeouts": "Strikeouts", "batter_hits": "Hits",
              "batter_total_bases": "Total Bases", "batter_home_runs": "Home Runs"},
    "nba":   {"player_points": "Points", "player_rebounds": "Rebounds",
              "player_assists": "Assists", "player_threes": "3-Pointers"},
    "nfl":   {"player_pass_yds": "Pass Yds", "player_rush_yds": "Rush Yds",
              "player_reception_yds": "Rec Yds", "player_receptions": "Receptions"},
    "ncaab": {"player_points": "Points", "player_rebounds": "Rebounds",
              "player_assists": "Assists"},
}

_events_cache = {}   # sport -> (ts, {match_key: event_id})
_props_cache = {}    # (sport, event_id) -> (ts, [props])


def _event_id(sport, home, away):
    """Resolve The Odds API event id for a game by matching team names.
    Uses the free /events endpoint (does not count against the quota)."""
    if not API_KEY or sport not in SPORT_KEY:
        return None
    c = _events_cache.get(sport)
    book = c[1] if (c and time.time() - c[0] < 1800) else None
    if book is None:
        try:
            import httpx
            r = httpx.get(f"{BASE}/sports/{SPORT_KEY[sport]}/events",
                          params={"apiKey": API_KEY, "dateFormat": "iso"}, timeout=12)
            r.raise_for_status()
            book = {}
            for e in r.json() or []:
                h, a, eid = e.get("home_team"), e.get("away_team"), e.get("id")
                if h and a and eid:
                    book[_norm(h) + "|" + _norm(a)] = eid
                    book[_norm(a) + "|" + _norm(h)] = eid
            _events_cache[sport] = (time.time(), book)
        except Exception as ex:
            print(f"[odds] events {sport} failed: {ex}")
            book = c[1] if c else {}
    return (book or {}).get(_norm(home) + "|" + _norm(away))


def _imp(o):
    if o is None:
        return None
    return 100.0 / (o + 100) if o > 0 else (-o) / ((-o) + 100)


def _amer(p):
    """American odds from an implied probability (vig included)."""
    if p is None or p <= 0 or p >= 1:
        return None
    return -round(100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def _median_num(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def get_player_props(sport, home, away):
    """Real sportsbook player props for one game, aggregated across books
    (median line, median over/under price, de-vigged over probability).
    Returns [] on: no key, sport without prop markets, no event match, a plan
    that doesn't include these markets (422), or exhausted quota."""
    if not API_KEY or sport not in PROP_MARKETS or sport not in SPORT_KEY:
        return []
    eid = _event_id(sport, home, away)
    if not eid:
        return []
    ck = (sport, eid)
    c = _props_cache.get(ck)
    if c and time.time() - c[0] < 900:
        return c[1]
    if not _quota_ok():
        return c[1] if c else []
    labels = PROP_MARKETS[sport]
    try:
        import httpx
        r = httpx.get(f"{BASE}/sports/{SPORT_KEY[sport]}/events/{eid}/odds",
                      params={"apiKey": API_KEY, "regions": "us",
                              "markets": ",".join(labels.keys()),
                              "oddsFormat": "american"}, timeout=15)
        if r.status_code == 422:
            print(f"[odds] props {sport}: markets not on plan / unavailable")
            _props_cache[ck] = (time.time(), [])
            return []
        r.raise_for_status()
        _quota["remaining"] = r.headers.get("x-requests-remaining")
        _quota["used"] = r.headers.get("x-requests-used")
        data = r.json() or {}
    except Exception as ex:
        print(f"[odds] props {sport} failed: {ex}")
        return c[1] if c else []
    acc = {}
    for bk in data.get("bookmakers", []) or []:
        for mkt in bk.get("markets", []) or []:
            mkey = mkt.get("key")
            if mkey not in labels:
                continue
            for o in mkt.get("outcomes", []) or []:
                player, point, price = o.get("description"), o.get("point"), o.get("price")
                side = (o.get("name") or "").lower()
                if not player or point is None:
                    continue
                d = acc.setdefault((mkey, player), {"lines": [], "over_imp": [], "under_imp": []})
                d["lines"].append(point)
                ip = _imp(price)
                if ip is not None:
                    if side == "over":
                        d["over_imp"].append(ip)
                    elif side == "under":
                        d["under_imp"].append(ip)
    out = []
    for (mkey, player), d in acc.items():
        line = _median_num(d["lines"])
        if line is None:
            continue
        oimp, uimp = _median_num(d["over_imp"]), _median_num(d["under_imp"])
        if oimp is not None and uimp is not None and (oimp + uimp) > 0:
            over_prob = oimp / (oimp + uimp)   # de-vig
        elif oimp is not None:
            over_prob = oimp
        else:
            over_prob = 0.5
        oo, uo = _amer(oimp), _amer(uimp)
        out.append({
            "player": player, "stat": labels[mkey], "label": labels[mkey],
            "line": round(line, 1),
            "over_odds": oo,
            "under_odds": uo,
            "over_prob": round(over_prob, 3),
            "lean": "over" if over_prob >= 0.5 else "under",
            "source": "book",
        })
    out.sort(key=lambda p: (p["stat"], p["player"]))
    _props_cache[ck] = (time.time(), out)
    return out
