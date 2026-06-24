"""
ncaaf_provider.py
-----------------
File-backed SP+ lookup + win-probability model for NCAA football, fed by
ncaaf_sp.json (built by refresh_cfbd_sp.py). Mirrors the other file-backed
providers: lazy JSON load, no network on the request path.

Model: SP+ overall ratings are points relative to an average team, so
    expected margin (home - away) = (sp_home - sp_away) + HOME_FIELD
A normal CDF on that margin (college FB scoring SD ~ 16.5 pts) gives the home
win probability; offense/defense ratings vs the national baseline give a total.

If the file is missing or either team can't be matched, predict() returns None
so the caller falls back to its existing win%-Elo prediction.
"""
from __future__ import annotations
import os
import json
import math
import unicodedata

_PATH = (os.environ.get("NCAAF_SP_PATH")
         or ("/data/ncaaf_sp.json" if os.path.exists("/data/ncaaf_sp.json") else "ncaaf_sp.json"))
HOME_FIELD = float(os.environ.get("NCAAF_HOME_FIELD", "2.5"))
MARGIN_SD = float(os.environ.get("NCAAF_MARGIN_SD", "16.5"))
_data = None
_logged_miss = set()

# Known name mismatches between ESPN displayName and CFBD's `team` string.
# Maps norm(espn-side) -> norm(cfbd). Seeded as misses surface in season.
_ALIASES = {
    "appalachianstate": "appstate",
    "appstate": "appalachianstate",
    "olemiss": "mississippi",
    "mississippi": "olemiss",
    "connecticut": "uconn",
    "uconn": "connecticut",
    "louisianamonroe": "ulmonroe",
    "louisianalafayette": "louisiana",
    "sanjosestate": "sanjosestate",
    "hawaii": "hawaii",
    "miamioh": "miamioh",
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
            _data = {"teams": {}, "national": {}}
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
    # 1) exact
    if n in teams:
        return teams[n]
    # 2) alias (exact, then as a leading match in case a mascot slipped through)
    for ak, av in _ALIASES.items():
        if (n == ak or n.startswith(ak)) and av in teams:
            return teams[av]
    # 3) school-name prefix: CFBD's name is usually the leading part of ESPN's
    # ("Alabama" vs "Alabama Crimson Tide"); take the longest such match.
    best = None
    for k, v in teams.items():
        if n.startswith(k) or k.startswith(n):
            if best is None or len(k) > len(best[0]):
                best = (k, v)
    return best[1] if best else None


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def predict(home_name, away_name):
    """SP+ home win prob + margin + total, or None to fall back."""
    d = _load()
    h = _lookup(home_name)
    a = _lookup(away_name)
    # If exactly one side matches, the other is likely an FBS team we failed to
    # map -> log once so we can add an alias. (Both missing is usually a non-FBS
    # opponent and not worth logging.)
    if bool(h) != bool(a):
        miss = away_name if h else home_name
        if miss not in _logged_miss:
            _logged_miss.add(miss)
            print(f"[ncaaf] SP+ name unmatched: {miss!r}")
    if not h or not a or h.get("sp") is None or a.get("sp") is None:
        return None
    margin = (h["sp"] - a["sp"]) + HOME_FIELD
    prob_home = max(0.02, min(0.98, _norm_cdf(margin / MARGIN_SD)))

    total = None
    nat = d.get("national") or {}
    dbase = nat.get("def")
    try:
        if None not in (h.get("off"), a.get("off"), h.get("def"),
                        a.get("def"), dbase):
            hp = h["off"] + (a["def"] - dbase)   # home offense vs away defense
            ap = a["off"] + (h["def"] - dbase)   # away offense vs home defense
            total = round(hp + ap, 1)
    except Exception:
        total = None

    return {
        "prob_home": round(prob_home, 4),
        "exp_margin": round(margin, 1),
        "home_rating": round(h["sp"], 1),
        "away_rating": round(a["sp"], 1),
        "confidence": "high",
        "avg_total": total,
        "model": "cfbd-sp+",
    }
