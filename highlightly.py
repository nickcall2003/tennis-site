"""
highlightly.py — College baseball team-stats provider, FILE-BACKED.

Replaces the old quota-limited Highlightly API client. The old client made live
API calls that (a) hit a DAILY request cap (the 429s) and (b) added per-game
network calls to the request path, which could hang the board. This version
reads team stats from a small local JSON file (ncaa_stats.json) — runs/game and
ERA per team. No network, no quota, no crash risk.

Keeps the EXACT interface ncaa_model._runexp_baseball calls:
    enabled(), get_team_stats(name), get_team_stats_cached(name)
Any OTHER attribute older code may have referenced resolves to a harmless no-op
(returns {}), so swapping this file in can't raise AttributeError anywhere.

Refresh: regenerate ncaa_stats.json (e.g. from the free NCAA API, henrygd) and
redeploy, or point NCAA_STATS_PATH at a file on /data and update it in place.
Stats only move after games finish, so once a day is plenty.
"""
from __future__ import annotations

import os
import re
import json
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
NCAA_STATS_PATH = os.environ.get("NCAA_STATS_PATH", os.path.join(_HERE, "ncaa_stats.json"))

_cache = {"ts": 0.0, "stats": {}}
_loaded = {"done": False}


def _norm(name):
    n = (name or "").lower().replace("&", "and")
    n = re.sub(r"[^a-z0-9 ]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _load_file():
    try:
        with open(NCAA_STATS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print(f"[ncaa_stats] {NCAA_STATS_PATH} not found — run model off, strength fallback")
        _loaded["done"] = True
        return
    except Exception as e:
        print(f"[ncaa_stats] failed to read {NCAA_STATS_PATH}: {e} — run model off")
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
        rpg = val.get("rpg", val.get("runs_per_game"))
        era = val.get("era")
        try:
            if rpg is not None:
                entry["rpg"] = float(rpg)
        except (TypeError, ValueError):
            pass
        try:
            if era is not None:
                entry["era"] = float(era)
        except (TypeError, ValueError):
            pass
        # pass through optional extras if you add them later
        for k in ("oppg", "runs_allowed_per_game", "obp", "slg", "record"):
            if k in val:
                entry[k] = val[k]
        if "rpg" in entry:   # need at least offense for the run model to be useful
            out[_norm(name)] = entry

    _cache["stats"] = out
    _cache["ts"] = time.time()
    _loaded["done"] = True
    print(f"[ncaa_stats] loaded {len(out)} team stat rows from {NCAA_STATS_PATH}")


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


# ---- Interface ncaa_model._runexp_baseball expects ---------------------------

def enabled():
    """True if team stats are loaded — this gates the run-expectancy model on."""
    _ensure_loaded()
    return bool(_cache["stats"])


def get_team_stats_cached(name):
    """Cache-only team stats {'rpg','era',...} or {}. No network — safe in the
    request hot path (this is what ncaa_model calls on a normal request)."""
    _ensure_loaded()
    return _lookup(name)


def get_team_stats(name):
    """Same as cached (file-backed, no network)."""
    return get_team_stats_cached(name)


def warm():
    """(Re)load the stats file. Cheap, no network."""
    _load_file()


def available():
    return enabled()


def _noop(*a, **k):
    return {}


def __getattr__(name):
    # Safety net: any other attribute older code referenced (a prefetch helper,
    # a client object, etc.) resolves to a harmless no-op returning {}, so
    # dropping this file in over the old client can't break other imports.
    return _noop
