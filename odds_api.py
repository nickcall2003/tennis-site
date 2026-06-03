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

SPORT_KEY = {
    "mlb": "baseball_mlb",
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
}

_cache = {}        # sport -> (ts, {match_key: {...odds...}})
_TTL = 900         # 15 min; protects the monthly quota
_quota = {"remaining": None, "used": None}


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
    url = f"{BASE}/sports/{SPORT_KEY[sport]}/odds"
    params = {"regions": "us", "markets": "h2h,spreads,totals",
              "oddsFormat": "american", "apiKey": API_KEY}
    try:
        import httpx
        r = httpx.get(url, params=params, timeout=12)
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
        out[key] = {
            "home_team": home, "away_team": away,
            "ml_home": med(ml.get(home, [])), "ml_away": med(ml.get(away, [])),
            "spread_home": med(spreads.get(home, [])),
            "spread_away": med(spreads.get(away, [])),
            "total": med(totals.get("over", [])),
            "books": books,
            "fetched": dt.datetime.utcnow().isoformat(),
        }
    _cache[sport] = (time.time(), out)
    return out


def quota():
    return dict(_quota)
