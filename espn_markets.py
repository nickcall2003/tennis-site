"""
espn_markets.py — per-game market lines (moneyline, spread/run-line, total)
straight from ESPN's public scoreboard, for the "log a bet from a game" flow.

ESPN's scoreboard puts the consensus line on each event at
  competitions[0].odds[0] = {
     details: "LAL -5.5", overUnder: 220.5, spread: -5.5,
     homeTeamOdds: {moneyLine: -210, favorite: true},
     awayTeamOdds: {moneyLine: +175},
  }
We surface the real moneyline prices, and the spread/total LINES (their price is
the standard -110 unless ESPN says otherwise — the UI lets the user edit it).

Covers every major league plus men's & women's college hoops, college football,
and college baseball. Tennis is handled separately via the api-tennis provider.
"""

LEAGUES = {
    "nfl":    "football/nfl",
    "nba":    "basketball/nba",
    "mlb":    "baseball/mlb",
    "nhl":    "hockey/nhl",
    "ncaaf":  "football/college-football",
    "ncaab":  "basketball/mens-college-basketball",
    "wncaab": "basketball/womens-college-basketball",
    "ncaabb": "baseball/college-baseball",
}

# Spread/total carry standard juice on the scoreboard feed; ML prices are real.
STD_JUICE = -110


def _scoreboard_url(sport):
    lg = LEAGUES.get(sport)
    return ("https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard" % lg) if lg else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def game_markets(sport, yyyymmdd=None):
    """Return a list of games with market lines for the sport/date. Empty list
    on any failure (never raises)."""
    url = _scoreboard_url(sport)
    if not url:
        return []
    params = {}
    if yyyymmdd:
        params["dates"] = str(yyyymmdd)
    if sport in ("ncaab", "wncaab", "ncaaf", "ncaabb"):
        params["groups"] = "50"      # all of D-I, not just ranked
        params["limit"] = "400"
    try:
        import httpx
        r = httpx.get(url, params=params, timeout=20.0)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    out = []
    for ev in data.get("events", []) or []:
        comp = (ev.get("competitions") or [{}])[0]
        home = away = None
        for c in comp.get("competitors") or []:
            t = c.get("team") or {}
            info = {"name": t.get("displayName") or t.get("name"),
                    "abbr": t.get("abbreviation"),
                    "short": t.get("shortDisplayName") or t.get("abbreviation")}
            if c.get("homeAway") == "home":
                home = info
            else:
                away = info
        if not home or not away:
            continue

        odds = (comp.get("odds") or [{}])[0]
        ho = odds.get("homeTeamOdds") or {}
        ao = odds.get("awayTeamOdds") or {}
        ml_home = _num(ho.get("moneyLine"))
        ml_away = _num(ao.get("moneyLine"))
        total = _num(odds.get("overUnder"))
        spread = _num(odds.get("spread"))

        # Resolve the home spread sign. ESPN's `spread` sign isn't consistent,
        # so anchor it to the favorite (from moneylines, else the details text).
        mag = abs(spread) if spread is not None else None
        home_fav = None
        if ml_home is not None and ml_away is not None:
            home_fav = ml_home < ml_away
        elif odds.get("details") and home.get("abbr"):
            home_fav = str(odds.get("details", "")).strip().startswith(home["abbr"])
        home_spread = away_spread = None
        if mag is not None and home_fav is not None:
            home_spread = -mag if home_fav else mag
            away_spread = -home_spread

        status = ((comp.get("status") or {}).get("type") or {})
        out.append({
            "id": str(ev.get("id") or ""),
            "start": ev.get("date"),
            "state": status.get("state"),          # pre | in | post
            "detail": status.get("shortDetail"),
            "home": home, "away": away,
            "ml_home": ml_home, "ml_away": ml_away,
            "total": total,
            "home_spread": home_spread, "away_spread": away_spread,
            "spread_details": odds.get("details"),
            "spread_juice": STD_JUICE, "total_juice": STD_JUICE,
            "has_odds": any(v is not None for v in (ml_home, ml_away, total, home_spread)),
        })
    return out
