"""
tennis_conditions.py — automatic venue conditions for tennis tournaments.

The ONLY thing looked up here is ELEVATION, which is a hard geographic fact we can
verify (Open-Meteo's elevation API: free, no key, server-friendly). From elevation
we state a well-established physical consequence: thinner air means less drag, so
the ball moves faster, serves win more free points, and rallies shorten.

WHAT THIS FILE WILL NOT DO:
  * It will not invent a "court speed" rating. Court pace depends on surface
    composition and ball type, which we do not have. Elevation is not court speed.
  * It will not guess a venue's elevation. If the lookup fails, the conditions
    note is simply omitted — no filler.
  * It will not claim a style/matchup effect on its own. The caller ties the
    altitude note to a player's REAL serve numbers, or says nothing.

Cached to disk so we hit the geocoder once per venue, ever.
"""
import json
import os
import re

_CACHE_PATH = os.environ.get("TENNIS_ELEV_PATH", "/data/tennis_elevation.json")
_mem = None

# Venues whose elevation is well known — seeds the cache so common events never
# depend on a network call. (Metres above sea level.)
_SEED = {
    "gstaad": 1050, "kitzbuhel": 760, "bogota": 2640, "quito": 2850,
    "mexico city": 2240, "la paz": 3640, "denver": 1610, "madrid": 660,
    "umag": 5, "bastad": 10, "hamburg": 6, "rome": 21, "paris": 35,
    "london": 11, "wimbledon": 45, "melbourne": 31, "new york": 10,
    "athens": 70, "iasi": 95, "monastir": 5, "cairo": 23, "antalya": 30,
    "geneva": 375, "munich": 520, "stuttgart": 245, "halle": 100,
    "eastbourne": 5, "newport": 10, "washington": 7, "toronto": 76,
    "montreal": 36, "cincinnati": 150, "winston-salem": 275, "acapulco": 5,
    "santiago": 570, "cordoba": 390, "buenos aires": 25, "rio de janeiro": 2,
    "marrakech": 466, "estoril": 20, "barcelona": 12, "munich": 520,
}

_ELEV_HIGH = 800        # metres: above this, altitude meaningfully speeds play


def _load():
    global _mem
    if _mem is not None:
        return _mem
    _mem = dict(_SEED)
    try:
        with open(_CACHE_PATH) as f:
            _mem.update({k: v for k, v in json.load(f).items() if isinstance(v, (int, float))})
    except Exception:
        pass
    return _mem


def _save():
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH) or ".", exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump(_mem or {}, f)
    except Exception:
        pass


def _city_of(tournament):
    """Strip tier prefixes/round suffixes to get the host city."""
    n = (tournament or "").strip()
    n = re.sub(r"^[MW]\d{2,3}\s+", "", n)              # ITF "M25 Skopje"
    n = re.sub(r"\s*\d+$", "", n)                       # trailing event number
    n = re.split(r"\s+[-\u2013]\s+", n)[0]              # drop " - 1/16-finals"
    n = re.sub(r"\((.*?)\)", "", n)                     # drop "(Country)"
    n = re.sub(r"\b(ATP|WTA|Challenger|Open|Cup|Masters|Classic|International)\b",
               "", n, flags=re.I)
    return n.strip().lower()


def elevation_m(tournament):
    """Metres above sea level for a tournament's host city, or None. Cached forever;
    looks the city up once via Open-Meteo's free elevation/geocoding API."""
    city = _city_of(tournament)
    if not city:
        return None
    cache = _load()
    if city in cache:
        v = cache[city]
        return v if isinstance(v, (int, float)) else None
    elev = None
    try:
        import httpx
        r = httpx.get("https://geocoding-api.open-meteo.com/v1/search",
                      params={"name": city, "count": 1}, timeout=8.0)
        res = (r.json().get("results") or [])
        if res and res[0].get("elevation") is not None:
            elev = float(res[0]["elevation"])
    except Exception:
        elev = None
    cache[city] = elev if elev is not None else None
    _save()
    return elev


def conditions_note(tournament):
    """A verified conditions line, or None. States the elevation and its known
    physical effect — nothing about court speed, which we can't verify."""
    e = elevation_m(tournament)
    if e is None or e < _ELEV_HIGH:
        return None                      # say nothing rather than invent
    city = _city_of(tournament).title()
    return (f"{city} sits at ~{int(round(e))}m. Thinner air means less drag: the ball "
            f"travels faster, serves earn more free points, and rallies tend to shorten.")


def altitude_edge(tournament, serve_pct=None):
    """Tie the (real) altitude to a player's (real) serve number — or return None.
    Never asserts a style effect without an actual stat to hang it on."""
    e = elevation_m(tournament)
    if e is None or e < _ELEV_HIGH or serve_pct is None:
        return None
    return (f"At ~{int(round(e))}m, big serving is rewarded \u2014 and he wins "
            f"{serve_pct}% of first-serve points.")
