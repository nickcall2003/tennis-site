"""
warrennolan.py — College baseball RPI enrichment, FILE-BACKED.

Why this version exists:
- The previous version scraped warrennolan.com on the server and parsed a
  ~672KB HTML page with a regex. Running that parse in-process pegged the CPU
  and took the whole container down during the healthcheck/serving window.
- This version reads RPI from a small local JSON file (rpi_data.json). There is
  NO network call and NO HTML parse on the server, so it can never stall or
  crash the box. It works whether RUN_BACKGROUND is 0 or 1, because the lookup
  functions load the file on first use.

How to refresh the numbers:
- RPI only moves after games finish, so updating once a day is plenty.
- Regenerate rpi_data.json (paste a fresh table to your assistant, or edit it by
  hand) and redeploy. Or point RPI_DATA_PATH at a file on your /data volume and
  update that file without a redeploy.

Attribution: RPI is Warren Nolan's own computed rating (warrennolan.com).
Data is keyed by a normalized team name so we can match ESPN's team names.
"""
from __future__ import annotations

import os
import re
import json
import time

# rpi_data.json ships next to this file in the image. Override with RPI_DATA_PATH
# (e.g. "/data/rpi_data.json") to update RPI without redeploying.
_HERE = os.path.dirname(os.path.abspath(__file__))
RPI_DATA_PATH = os.environ.get("RPI_DATA_PATH", os.path.join(_HERE, "rpi_data.json"))

# Kept for backward-compat in case anything imports these names. UNUSED now —
# this module never makes network calls.
RPI_URL = "https://www.warrennolan.com/baseball/2026/rpi-live"
ELO_URL = "https://www.warrennolan.com/baseball/2026/elo-live"

_cache = {"ts": 0.0, "rpi": {}}
_loaded = {"done": False}


def _norm(name):
    """Normalize a team name for matching across ESPN and Warren Nolan."""
    n = (name or "").lower()
    n = n.replace("&", "and")
    n = re.sub(r"[^a-z0-9 ]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _load_file():
    """Load rpi_data.json into the cache. Cheap (a few hundred small rows), no
    network, no HTML parse. Safe to call on any path including the request path.

    Accepts any of:
      {"teams": [ {"name": "...", "rpi_rank": 1, "rpi": 0.6, "record": "50-10"}, ... ]}
      {"Team Name": 1, ...}                      # bare rank
      {"Team Name": {"rpi_rank": 1, ...}, ...}
    """
    try:
        with open(RPI_DATA_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print(f"[warrennolan] rpi_data.json not found at {RPI_DATA_PATH} — RPI disabled, records-only")
        _loaded["done"] = True
        return
    except Exception as e:
        print(f"[warrennolan] failed to read rpi_data.json: {e} — RPI disabled, records-only")
        _loaded["done"] = True
        return

    # Figure out the list/dict of teams, ignoring any "_comment" metadata key.
    if isinstance(raw, dict) and "teams" in raw:
        items = raw["teams"]
    else:
        items = raw

    if isinstance(items, dict):
        iterable = [(k, v) for k, v in items.items() if k != "_comment"]
    else:
        iterable = [(d.get("name"), d) for d in (items or []) if isinstance(d, dict)]

    out = {}
    for name, val in iterable:
        if not name:
            continue
        if isinstance(val, dict):
            rank = val.get("rpi_rank", val.get("rank"))
            rpi = val.get("rpi")
            record = val.get("record", "")
        else:
            rank = val          # bare number -> treat as rank
            rpi = None
            record = ""
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            continue
        entry = {"rpi_rank": rank, "record": record or ""}
        if rpi is not None:
            try:
                entry["rpi"] = float(rpi)
            except (TypeError, ValueError):
                pass
        out[_norm(name)] = entry

    _cache["rpi"] = out
    _cache["ts"] = time.time()
    _loaded["done"] = True
    print(f"[warrennolan] loaded {len(out)} RPI rows from {RPI_DATA_PATH}")


def _ensure_loaded():
    if not _loaded["done"]:
        _load_file()


def _lookup(team_name):
    if not _cache["rpi"]:
        return {}
    key = _norm(team_name)
    if key in _cache["rpi"]:
        return _cache["rpi"][key]
    # Fuzzy fallback: match on mascot-trimmed names (e.g. "tennessee" vs
    # "tennessee volunteers"). Exact keys above always win.
    for k, v in _cache["rpi"].items():
        if k and (k in key or key in k):
            return v
    return {}


# ---- Public interface (unchanged signatures the model already calls) --------

def cached_ready():
    """True if RPI is loaded. Loads the local file on first call (cheap)."""
    _ensure_loaded()
    return bool(_cache["rpi"])


def get_rating_cached(team_name):
    """RPI dict {'rpi_rank', 'record', ['rpi']} for a team, or {}.
    No network, no parse — safe in the request hot path."""
    _ensure_loaded()
    return _lookup(team_name)


def get_rating(team_name):
    """Back-compat alias — same as get_rating_cached (file-backed, no network)."""
    return get_rating_cached(team_name)


def available():
    """Back-compat alias — same as cached_ready()."""
    return cached_ready()


def warm():
    """(Re)load the RPI file into memory. Cheap, no network. Kept so existing
    startup code that calls warrennolan.warm() still works — and it's now safe
    to call anywhere, even during the healthcheck window."""
    _load_file()
