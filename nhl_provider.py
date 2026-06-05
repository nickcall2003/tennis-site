"""
nhl_provider.py — NHL team-stats provider, FILE-BACKED.

Same pattern as the NCAABB providers: reads team stats from a local JSON file
(nhl_stats.json) — goals-for/game (gf) and goals-against/game (ga) per team.
NO network calls and NO parsing on the server, so it can't stall or crash the
box and has no rate limit. A refresher script pulls the numbers from the NHL
public API and writes the file; the server only ever reads it.

Interface used by nhl_model:
    enabled(), get_team_stats(name), get_team_stats_cached(name)

Refresh: regenerate nhl_stats.json (refresh_nhl_stats.py) and redeploy, or point
NHL_STATS_PATH at a file on /data and update it in place.
"""
from __future__ import annotations

import os
import re
import json
import time
import unicodedata

_HERE = os.path.dirname(os.path.abspath(__file__))
NHL_STATS_PATH = os.environ.get("NHL_STATS_PATH", os.path.join(_HERE, "nhl_stats.json"))

_cache = {"ts": 0.0, "stats": {}}
_loaded = {"done": False}


def _norm(name):
    n = (name or "").lower().replace("&", "and")
    n = unicodedata.normalize("NFKD", n)          # fold accents: Montréal -> Montreal
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"[^a-z0-9 ]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _load_file():
    try:
        with open(NHL_STATS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print(f"[nhl_stats] {NHL_STATS_PATH} not found — model off, fallback")
        _loaded["done"] = True
        return
    except Exception as e:
        print(f"[nhl_stats] failed to read {NHL_STATS_PATH}: {e} — model off")
        _loaded["done"] = True
        return

    items = raw["teams"] if isinstance(raw, dict) and "teams" in raw else raw
    if isinstance(items, dict):
        iterable = [(k, v) for k, v in items.items() if k != "_comment"]
    else:
        iterable = [(d.get("name"), d) for d in (items or []) if isinstance(d, dict)]

    out = {}
    for name, val in iterable:
        if not name or not isinstance(val, dict):
            continue
        entry = {}
        gf = val.get("gf", val.get("goals_for_pg"))
        ga = val.get("ga", val.get("goals_against_pg"))
        try:
            if gf is not None:
                entry["gf"] = float(gf)
        except (TypeError, ValueError):
            pass
        try:
            if ga is not None:
                entry["ga"] = float(ga)
        except (TypeError, ValueError):
            pass
        for k in ("record", "pp_pct", "pk_pct", "save_pct", "points_pct"):
            if k in val:
                entry[k] = val[k]
        if "gf" in entry and "ga" in entry:   # need both for the xG model
            out[_norm(name)] = entry

    _cache["stats"] = out
    _cache["ts"] = time.time()
    _loaded["done"] = True
    print(f"[nhl_stats] loaded {len(out)} team stat rows from {NHL_STATS_PATH}")


def _ensure_loaded():
    if not _loaded["done"]:
        _load_file()


def _lookup(name):
    if not _cache["stats"]:
        return {}
    key = _norm(name)
    if key in _cache["stats"]:
        return _cache["stats"][key]
    for k, v in _cache["stats"].items():
        if k and (k in key or key in k):
            return v
    return {}


def enabled():
    _ensure_loaded()
    return bool(_cache["stats"])


def get_team_stats_cached(name):
    _ensure_loaded()
    return _lookup(name)


def get_team_stats(name):
    return get_team_stats_cached(name)


def warm():
    _load_file()


def available():
    return enabled()


def _noop(*a, **k):
    return {}


def __getattr__(name):
    return _noop
