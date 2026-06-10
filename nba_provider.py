"""
nba_provider.py
---------------
File-backed efficiency lookup + win-probability model for the NBA, fed by
nba_stats.json (refresh_nba_stats.py). Same Pythagorean engine as the NCAAB
provider, tuned for the NBA and using each matchup's expected pace.

    home_eff = OffRtg_home * DefRtg_away / avg_eff   (expected pts/100)
    away_eff = OffRtg_away * DefRtg_home / avg_eff
    game_pace = mean(home pace, away pace)            (falls back to league pace)
    margin   = (home_eff - away_eff) * game_pace/100 + HOME_COURT
    total    = (home_eff + away_eff) * game_pace/100
    P(home)  = normal CDF of margin / MARGIN_SD        (NBA margin SD ~ 12 pts)

Returns None (caller falls back to win%-Elo) when the file is missing or a team
can't be matched. NBA team names are full ("Boston Celtics"), so we match on the
ESPN displayName, not the city.
"""
from __future__ import annotations
import os
import json
import math
import unicodedata

_PATH = os.environ.get("NBA_STATS_PATH", "nba_stats.json")
HOME_COURT = float(os.environ.get("NBA_HOME_COURT", "2.8"))
MARGIN_SD = float(os.environ.get("NBA_MARGIN_SD", "12.0"))
_data = None
_logged_miss = set()

# norm(espn displayName) -> norm(nba_api TEAM_NAME), for the few that differ.
_ALIASES = {
    "laclippers": "losangelesclippers",
    "losangelesclippers": "laclippers",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def _load():
    global _data
    if _data is None:
        try:
            with open(_PATH) as f:
                _data = json.load(f)
        except Exception:
            _data = {"teams": {}, "avg_eff": 114.0, "pace": 99.5}
    return _data


def reload():
    global _data
    _data = None
    return _load()


def _lookup(name):
    d = _load()
    teams = d.get("teams") or {}
    if not teams:
        return None
    n = _norm(name)
    if n in teams:
        return teams[n]
    for ak, av in _ALIASES.items():
        if (n == ak or n.startswith(ak)) and av in teams:
            return teams[av]
    best = None
    for k, v in teams.items():
        if n.startswith(k) or k.startswith(n):
            if best is None or len(k) > len(best[0]):
                best = (k, v)
    return best[1] if best else None


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def predict(home_name, away_name):
    d = _load()
    h = _lookup(home_name)
    a = _lookup(away_name)
    if bool(h) != bool(a):
        miss = away_name if h else home_name
        if miss not in _logged_miss:
            _logged_miss.add(miss)
            print(f"[nba] efficiency name unmatched: {miss!r}")
    if not h or not a:
        return None
    avg = d.get("avg_eff") or 114.0
    lg_pace = d.get("pace") or 99.5
    try:
        home_eff = h["off"] * a["def"] / avg
        away_eff = a["off"] * h["def"] / avg
    except Exception:
        return None
    hp, ap = h.get("pace") or lg_pace, a.get("pace") or lg_pace
    game_pace = (hp + ap) / 2.0
    margin = (home_eff - away_eff) * game_pace / 100.0 + HOME_COURT
    total = (home_eff + away_eff) * game_pace / 100.0
    prob_home = max(0.02, min(0.98, _norm_cdf(margin / MARGIN_SD)))
    return {
        "prob_home": round(prob_home, 4),
        "exp_margin": round(margin, 1),
        "home_rating": round(h.get("net", 0.0), 1),
        "away_rating": round(a.get("net", 0.0), 1),
        "confidence": "high",
        "avg_total": round(total, 1),
        "model": "nba-eff",
    }
