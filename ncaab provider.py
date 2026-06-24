"""
ncaab_provider.py
-----------------
File-backed adjusted-efficiency lookup + win-probability model for men's NCAA
basketball, fed by ncaab_ratings.json (refresh_cbbd_ratings.py). No network on
the request path.

Model (standard Pythagorean efficiency):
    league avg efficiency = avg_eff (points per 100 possessions)
    home_eff = AdjO_home * AdjD_away / avg_eff   (expected pts/100 for home)
    away_eff = AdjO_away * AdjD_home / avg_eff
    margin   = (home_eff - away_eff) * tempo/100 + HOME_COURT
    total    = (home_eff + away_eff) * tempo/100
    P(home)  = normal CDF of margin / MARGIN_SD   (CBB margin SD ~ 11 pts)

Returns None (caller falls back to win%-Elo) if the file is missing or a team
can't be matched.
"""
from __future__ import annotations
import os
import json
import math
import unicodedata

_PATH = (os.environ.get("NCAAB_RATINGS_PATH")
         or ("/data/ncaab_ratings.json" if os.path.exists("/data/ncaab_ratings.json") else "ncaab_ratings.json"))
HOME_COURT = float(os.environ.get("NCAAB_HOME_COURT", "3.5"))
MARGIN_SD = float(os.environ.get("NCAAB_MARGIN_SD", "11.0"))
_data = None
_logged_miss = set()

# norm(espn location) -> norm(cbbd team), seeded with common men's-CBB oddities.
_ALIASES = {
    "connecticut": "uconn", "uconn": "connecticut",
    "saintmarys": "stmarys", "stmarys": "saintmarys",
    "stjohns": "saintjohns", "saintjohns": "stjohns",
    "ole miss".replace(" ", ""): "mississippi", "mississippi": "olemiss",
    "nccentral": "northcarolinacentral",
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
            _data = {"teams": {}, "avg_eff": 104.0, "tempo": 68.0}
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
            print(f"[ncaab] adj-rating name unmatched: {miss!r}")
    if not h or not a:
        return None
    avg = d.get("avg_eff") or 104.0
    tempo = d.get("tempo") or 68.0
    try:
        home_eff = h["off"] * a["def"] / avg
        away_eff = a["off"] * h["def"] / avg
    except Exception:
        return None
    margin = (home_eff - away_eff) * tempo / 100.0 + HOME_COURT
    total = (home_eff + away_eff) * tempo / 100.0
    prob_home = max(0.02, min(0.98, _norm_cdf(margin / MARGIN_SD)))
    return {
        "prob_home": round(prob_home, 4),
        "exp_margin": round(margin, 1),
        "home_rating": round(h.get("net", 0.0), 1),
        "away_rating": round(a.get("net", 0.0), 1),
        "confidence": "high",
        "avg_total": round(total, 1),
        "model": "cbbd-adj",
    }
