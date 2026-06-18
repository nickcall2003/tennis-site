"""DataGolf integration — pre-tournament win / top-N predictions for golf.

Golf is served strictly by DataGolf (the Odds API no longer touches golf). This
fills the pre-tournament gap that the live scoring model can't cover: before a
tournament starts there are no scores to simulate, so projections and 3-ball
matchups are priced from DataGolf's model instead.

Enable by setting DATAGOLF_KEY in the environment. With no key, enabled() is
False and every call returns None, so callers fall back to their normal message.

Docs: https://datagolf.com/api-access  (requires a Scratch Plus membership w/ API)
"""
import os
import time
import unicodedata

BASE = "https://feeds.datagolf.com"
_KEY = os.environ.get("DATAGOLF_KEY", "").strip()
_TTL = int(os.environ.get("DATAGOLF_TTL", "1800"))      # cache 30 min
_cache = {}                                             # dg_tour -> (ts, data)

# app / ESPN tour key  ->  DataGolf tour code
TOUR_MAP = {
    "pga": "pga", "dpworld": "euro", "euro": "euro", "european": "euro",
    "kft": "kft", "kornferry": "kft", "liv": "liv", "opp": "opp", "alt": "alt",
}


def enabled():
    return bool(_KEY)


def _strip(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c))


def _to_first_last(name):
    """DataGolf returns 'Last, First'; flip to 'First Last' for display + match."""
    s = _strip(name).strip()
    if "," in s:
        last, first = s.split(",", 1)
        s = first.strip() + " " + last.strip()
    return " ".join(s.split())


def _norm(name):
    """Key that lines up with the ESPN board names ('first last', lowercased)."""
    return _to_first_last(name).lower()


def _pct(v):
    """DataGolf percent format may arrive as a 0-1 fraction or a 0-100 number;
    normalize to a percentage with one decimal."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 1.0:
        v *= 100.0
    return round(v, 1)


def pre_tournament(tour="pga"):
    """Returns:
      {"event","updated","players":{norm_name:{name,dg_id,win,top5,top10,top20,make_cut}}}
    or None when disabled/unavailable. Cached per tour for _TTL seconds."""
    if not _KEY:
        return None
    dgt = TOUR_MAP.get((tour or "pga").lower(), "pga")
    c = _cache.get(dgt)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    url = (f"{BASE}/preds/pre-tournament?tour={dgt}"
           f"&odds_format=percent&file_format=json&key={_KEY}")
    try:
        import httpx
        r = httpx.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[datagolf] pre-tournament {dgt} failed: {e}")
        return c[1] if c else None

    rows = []
    if isinstance(data, dict):
        rows = data.get("baseline") or data.get("baseline_history_fit") or []
    elif isinstance(data, list):
        rows = data
    players = {}
    for p in rows:
        if not isinstance(p, dict):
            continue
        nm = p.get("player_name") or p.get("name") or ""
        if not nm:
            continue
        players[_norm(nm)] = {
            "name": _to_first_last(nm), "dg_id": p.get("dg_id"),
            "win": _pct(p.get("win")), "top5": _pct(p.get("top_5")),
            "top10": _pct(p.get("top_10")), "top20": _pct(p.get("top_20")),
            "make_cut": _pct(p.get("make_cut")),
        }
    out = {"event": (data.get("event_name") if isinstance(data, dict) else None),
           "updated": (data.get("last_updated") if isinstance(data, dict) else None),
           "players": players}
    _cache[dgt] = (time.time(), out)
    return out


def win_prob(tour, name):
    """Convenience: model win% for one player by name, or None."""
    pred = pre_tournament(tour)
    if not pred:
        return None
    m = (pred.get("players") or {}).get(_norm(name))
    return m.get("win") if m else None


def matchups(tour="pga", market="3_balls"):
    """Book-offered matchups with odds, for ROI tracking. market is one of
    '3_balls', 'tournament_matchups', 'round_matchups'. Returns the raw DataGolf
    payload (parser finalized once the shape is confirmed via the diag), or None."""
    if not _KEY:
        return None
    dgt = TOUR_MAP.get((tour or "pga").lower(), "pga")
    url = (f"{BASE}/betting-tools/matchups?tour={dgt}&market={market}"
           f"&odds_format=decimal&file_format=json&key={_KEY}")
    try:
        import httpx
        r = httpx.get(url, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[datagolf] matchups {dgt}/{market} failed: {e}")
        return None


def outrights(tour="pga", market="win"):
    """Per-player model + book odds for an outright market (win, top_5, top_10,
    top_20, mc, frl, ...). Returns:
      {"event","updated","market","players":{norm:{name,dg_id,model_dec,book_dec,book}}}
    or None. model_dec is DataGolf's model decimal price; book_dec is the best
    (highest) decimal across real sportsbooks. Cached per (tour,market)."""
    if not _KEY:
        return None
    dgt = TOUR_MAP.get((tour or "pga").lower(), "pga")
    ck = f"out:{dgt}:{market}"
    c = _cache.get(ck)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    url = (f"{BASE}/betting-tools/outrights?tour={dgt}&market={market}"
           f"&odds_format=decimal&file_format=json&key={_KEY}")
    try:
        import httpx
        r = httpx.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[datagolf] outrights {dgt}/{market} failed: {e}")
        return c[1] if c else None

    rows = data.get("odds") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    _NONBOOK = {"player_name", "dg_id", "datagolf", "data_golf"}
    players = {}
    for p in rows:
        if not isinstance(p, dict):
            continue
        nm = p.get("player_name") or ""
        if not nm:
            continue
        dgv = p.get("datagolf")
        if isinstance(dgv, dict):
            dgv = dgv.get("baseline") or dgv.get("baseline_history_fit")
        try:
            model_dec = float(dgv)
        except (TypeError, ValueError):
            model_dec = None
        best, bestbk = None, None
        for k, v in p.items():
            if k in _NONBOOK:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v and (best is None or v > best):
                best, bestbk = v, k
        players[_norm(nm)] = {"name": _to_first_last(nm), "dg_id": p.get("dg_id"),
                              "model_dec": model_dec, "book_dec": best, "book": bestbk}
    out = {"event": data.get("event_name"), "updated": data.get("last_updated"),
           "market": data.get("market") or market, "players": players}
    _cache[ck] = (time.time(), out)
    return out


def diag(tour="pga"):
    """Snapshot so /api/golf/dg-diag can confirm the key works + the field mapping
    is right against a real response."""
    out = {"enabled": enabled(), "tour": tour}
    if not _KEY:
        out["note"] = "DATAGOLF_KEY not set"
        return out
    pred = pre_tournament(tour)
    if not pred:
        out["error"] = "no data returned (check server logs for [datagolf])"
        return out
    pl = pred.get("players") or {}
    out.update(event=pred.get("event"), updated=pred.get("updated"),
               players_loaded=len(pl), sample=list(pl.values())[:3])
    return out
