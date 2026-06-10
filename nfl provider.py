"""
nfl_provider.py
---------------
File-backed EPA model for the NFL, fed by nfl_stats.json (refresh_nfl_stats.py).
No network on the request path.

Model (EPA/play -> points):
    net = off_epa - def_epa          (team strength in points per play)
    margin = (net_home - net_away) * PLAYS + HOME_FIELD
    P(home) = normal CDF of margin / MARGIN_SD     (NFL margin SD ~ 13.5 pts)
    total estimated from each offense vs the opposing defense, around league PPG.

nflfastR uses team ABBREVIATIONS (KC, SF, ...), so we match on ESPN's abbr, with
a small alias map for the few that differ (LAR<->LA, WSH<->WAS). Returns None
(caller falls back to win%-Elo) if the file is missing or a team is unmatched.
"""
from __future__ import annotations
import os
import json
import math

_PATH = os.environ.get("NFL_STATS_PATH", "nfl_stats.json")
PLAYS = float(os.environ.get("NFL_PLAYS", "63"))           # offensive plays/team/game
HOME_FIELD = float(os.environ.get("NFL_HOME_FIELD", "2.0"))
MARGIN_SD = float(os.environ.get("NFL_MARGIN_SD", "13.5"))
LG_PPG = float(os.environ.get("NFL_LG_PPG", "22.0"))       # ~half of a ~44 total
_data = None
_logged_miss = set()

# ESPN abbr -> nflfastR abbr (normalized, lowercase) for the few that differ.
_ALIASES = {"lar": "la", "la": "lar", "wsh": "was", "was": "wsh",
            "jac": "jax", "jax": "jac", "oak": "lv"}


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _load():
    global _data
    if _data is None:
        try:
            with open(_PATH) as f:
                _data = json.load(f)
        except Exception:
            _data = {"teams": {}, "lg_off": 0.0, "lg_def": 0.0}
    return _data


def reload():
    global _data
    _data = None
    return _load()


def _lookup(abbr):
    d = _load()
    teams = d.get("teams") or {}
    if not teams:
        return None
    n = _norm(abbr)
    if n in teams:
        return teams[n]
    if n in _ALIASES and _ALIASES[n] in teams:
        return teams[_ALIASES[n]]
    return None   # abbreviations are exact tokens; no fuzzy prefix matching


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def predict(home_abbr, away_abbr):
    d = _load()
    h = _lookup(home_abbr)
    a = _lookup(away_abbr)
    if bool(h) != bool(a):
        miss = away_abbr if h else home_abbr
        if miss not in _logged_miss:
            _logged_miss.add(miss)
            print(f"[nfl] EPA abbr unmatched: {miss!r}")
    if not h or not a:
        return None
    net_h = h["off_epa"] - h["def_epa"]
    net_a = a["off_epa"] - a["def_epa"]
    margin = (net_h - net_a) * PLAYS + HOME_FIELD
    prob_home = max(0.02, min(0.98, _norm_cdf(margin / MARGIN_SD)))

    lg_off = d.get("lg_off", 0.0)
    lg_def = d.get("lg_def", 0.0)
    home_pts = LG_PPG + ((h["off_epa"] - lg_off) + (a["def_epa"] - lg_def)) * PLAYS
    away_pts = LG_PPG + ((a["off_epa"] - lg_off) + (h["def_epa"] - lg_def)) * PLAYS
    total = round(max(20.0, home_pts + away_pts), 1)

    return {
        "prob_home": round(prob_home, 4),
        "exp_margin": round(margin, 1),
        "home_rating": round(net_h, 3),
        "away_rating": round(net_a, 3),
        "confidence": "high",
        "avg_total": total,
        "model": "nfl-epa",
    }
