"""
ufc_provider.py — UFC / MMA fight cards from ESPN's public MMA feed.

A "game" here is a single BOUT (Fighter A vs Fighter B), shaped like the other
providers' game dicts (id, sport, status, home, away, prob_home/away, odds,
winner, ...) so the existing board/detail/odds/settlement plumbing reuses it.
"home" = red corner (first competitor), "away" = blue corner (second).

Win probability is market-derived when odds are attached upstream; with no
odds we fall back to a record-based estimate. UFC's schedule is sparse (~weekly,
usually Saturdays), so when a requested day has no card we surface the NEXT
upcoming card instead of an empty board.
"""
from __future__ import annotations

import datetime as dt
import time

import espn_provider as E

_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/summary"

_DAY_TTL = 300
_LIVE_TTL = 8
_cache = {}          # key -> (ts, [bouts])


def _winpct(comp):
    """(win_pct, record_str) for a competitor from its records array."""
    recs = comp.get("records") or (comp.get("athlete") or {}).get("records") or []
    for rec in recs:
        summ = rec.get("summary") or ""
        if "-" in summ:
            try:
                parts = [int(x) for x in summ.split("-")[:3]]
                w = parts[0]
                l = parts[1] if len(parts) > 1 else 0
                d = parts[2] if len(parts) > 2 else 0
                tot = w + l + d
                if tot > 0:
                    return (w + 0.5 * d) / tot, summ
            except (ValueError, IndexError):
                continue
    return None, ""


def _fighter(comp):
    ath = comp.get("athlete") or {}
    wp, rec = _winpct(comp)
    name = (ath.get("displayName") or ath.get("fullName")
            or ath.get("shortName") or "TBD")
    img = ((ath.get("headshot") or {}).get("href")
           or (ath.get("flag") or {}).get("href") or "")
    return {
        "name": name,
        "short": ath.get("shortName") or name,
        "img": img,
        "record": rec,
        "win_pct": wp if wp is not None else 0.5,
        "id": str(ath.get("id") or ""),
    }


def _bout_winprob(h, a, odds):
    """2-way win probabilities. Market first (de-vigged), else record-based."""
    if odds and odds.get("ml_home") is not None and odds.get("ml_away") is not None:
        ih, ia = _imp(odds["ml_home"]), _imp(odds["ml_away"])
        if ih and ia and (ih + ia) > 0:
            return ih / (ih + ia), ia / (ih + ia)
    sh = max(0.05, min(0.95, h["win_pct"]))
    sa = max(0.05, min(0.95, a["win_pct"]))
    # widen the gap a touch so favorites read sensibly, then bound it
    import math
    ph = 1.0 / (1.0 + math.exp(-3.2 * (sh - sa)))
    ph = max(0.2, min(0.8, ph))
    return ph, 1.0 - ph


def _imp(o):
    try:
        o = float(o)
    except (TypeError, ValueError):
        return None
    return 100.0 / (o + 100) if o > 0 else (-o) / ((-o) + 100)


def _weight_class(comp):
    t = comp.get("type") or {}
    return (t.get("text") or t.get("abbreviation")
            or (comp.get("note") or "") or "")


def _is_main(comp, idx, total):
    for n in comp.get("notes") or []:
        if "main event" in (n.get("headline") or "").lower():
            return True
    return idx == 0           # ESPN lists the main event first on the card


def _method(comp):
    """KO/TKO, Submission, Decision … from a finished bout, if present."""
    st = ((comp.get("status") or {}).get("type") or {})
    detail = st.get("detail") or st.get("description") or ""
    return detail


def _build_bout(comp, event, idx, total):
    cs = comp.get("competitors") or []
    if len(cs) < 2:
        return None
    cs = sorted(cs, key=lambda c: c.get("order", 99))
    h, a = _fighter(cs[0]), _fighter(cs[1])
    status = E._status(comp)
    finished = status == "finished"
    win = None
    if finished:
        if cs[0].get("winner"):
            win = "home"
        elif cs[1].get("winner"):
            win = "away"
    ph, pa = _bout_winprob(h, a, None)
    top = max(ph, pa)
    conf = "high" if top >= 0.66 else "medium" if top >= 0.56 else "low"
    return {
        "id": str(comp.get("id") or ""),
        "sport": "ufc",
        "event_name": event.get("shortName") or event.get("name") or "UFC",
        "event_label": event.get("shortName") or event.get("name") or "UFC",
        "event_id": str(event.get("id") or ""),
        "weight_class": _weight_class(comp),
        "is_main": _is_main(comp, idx, total),
        "status": status,
        "event_time": E._ct_time(event.get("date", "")),
        "kickoff_iso": event.get("date", ""),
        "home": h, "away": a,
        "prob_home": round(ph, 4), "prob_away": round(pa, 4),
        "confidence": conf,
        "odds": None,
        "method": _method(comp) if finished else "",
        "winner": win,
        "score": {"detail": (((comp.get("status") or {}).get("type") or {})
                             .get("shortDetail", ""))},
        "prominence": (2.0 if _is_main(comp, idx, total) else 1.0)
                      + (total - idx) * 0.01,
    }


def _bouts_from_events(events):
    out = []
    for ev in events or []:
        comps = ev.get("competitions") or []
        n = len(comps)
        for i, comp in enumerate(comps):
            try:
                b = _build_bout(comp, ev, i, n)
                if b:
                    out.append(b)
            except Exception as ex:
                print(f"[ufc] bout build failed: {ex}")
    return out


def _fetch(dates):
    try:
        return E._get(_SCOREBOARD, {"dates": dates}).get("events", [])
    except Exception as ex:
        print(f"[ufc] fetch {dates} failed: {ex}")
        return []


def _has_live(bouts):
    return any(b.get("status") == "live" for b in bouts)


def get_games(date: dt.date, force_live=False):
    """Bouts for `date`. If that day has no card and the date is today/future,
    fall back to the NEXT upcoming card so the board is never empty."""
    key = date.isoformat()
    c = _cache.get(key)
    ttl = _LIVE_TTL if (c and _has_live(c[1])) else _DAY_TTL
    if c and not force_live and time.time() - c[0] < ttl:
        return c[1]

    events = _fetch(date.strftime("%Y%m%d"))
    bouts = _bouts_from_events(events)

    if not bouts and date >= dt.date.today():
        # look ahead ~60 days and surface the soonest card
        rng = f"{date.strftime('%Y%m%d')}-{(date + dt.timedelta(days=60)).strftime('%Y%m%d')}"
        future = _fetch(rng)
        future = [e for e in future if (e.get("date") or "") >= date.isoformat()[:10]]
        future.sort(key=lambda e: e.get("date", ""))
        if future:
            first_id = future[0].get("id")
            same = [e for e in future if e.get("id") == first_id]
            bouts = _bouts_from_events(same)

    bouts.sort(key=lambda b: (b["status"] != "live", -b["prominence"]))
    _cache[key] = (time.time(), bouts)
    return bouts


def get_game(date: dt.date, game_id: str, force_live=False):
    for b in get_games(date, force_live=force_live):
        if str(b["id"]) == str(game_id):
            return b
    # the bout might belong to the next card surfaced for a nearby date
    for off in (0, 1, 2, 3, 7):
        for b in get_games(date + dt.timedelta(days=off)):
            if str(b["id"]) == str(game_id):
                return b
    return None


def next_event_label(date: dt.date):
    bouts = get_games(date)
    return bouts[0]["event_label"] if bouts else "UFC"


# ===================== API-Sports MMA enrichment (MMA only) =====================
# ESPN powers the board + live (free, real-time). API-Sports is spent ONLY here,
# on the detail page, to attach a fighter "tale of the tape" + a why-they-win read.
# All calls are cached hard (card 15m, fighters 24h) and capped at 90/day.

def _last_name(n):
    toks = [t for t in (n or "").replace(".", " ").split() if t]
    return "".join(c for c in (toks[-1].lower() if toks else "") if c.isalnum())


def _as_name(f):
    if isinstance(f, dict):
        return (f.get("name") or f.get("full_name")
                or (f.get("fighter") or {}).get("name") if isinstance(f.get("fighter"), dict)
                else f.get("name")) or ""
    return str(f or "")


def _as_id(f):
    if isinstance(f, dict):
        return f.get("id") or (f.get("fighter") or {}).get("id")
    return None


def _card_fighters(fight):
    """Pull the two fighters from a fight object across the shapes API-Sports
    might use (fighters.first/second, teams.home/away, or a 2-item list)."""
    for k in ("fighters", "teams", "competitors"):
        v = fight.get(k)
        if isinstance(v, dict):
            a = v.get("first") or v.get("home") or v.get("fighter_1") or v.get("a")
            b = v.get("second") or v.get("away") or v.get("fighter_2") or v.get("b")
            if a or b:
                return a, b
        if isinstance(v, list) and len(v) >= 2:
            return v[0], v[1]
    return None, None


def _apisports_card_index(date):
    """{frozenset(surnames): {fight_id, a, b}} for the date's card (1 cached call)."""
    try:
        import apisports_mma
        if not apisports_mma.enabled():
            return {}
        fights = apisports_mma.get_card(date) or []
    except Exception:
        return {}
    idx = {}
    for ft in fights:
        a, b = _card_fighters(ft)
        na, nb = _as_name(a), _as_name(b)
        if not na or not nb:
            continue
        idx[frozenset([_last_name(na), _last_name(nb)])] = {
            "fight_id": ft.get("id"),
            "a": {"id": _as_id(a), "name": na},
            "b": {"id": _as_id(b), "name": nb},
        }
    return idx


def _prof_record(prof):
    rec = prof.get("record") or prof.get("records")
    if isinstance(rec, dict):
        w, l, d = rec.get("win") or rec.get("wins"), rec.get("loss") or rec.get("losses"), rec.get("draw") or rec.get("draws")
        if w is not None and l is not None:
            return f"{w}-{l}" + (f"-{d}" if d not in (None, 0, "0") else "")
    if isinstance(rec, str):
        return rec
    return ""


def _clean_height(v):
    import re
    m = re.match(r"\s*(\d+)'\s*(\d+)", str(v or ""))
    return f"{m.group(1)}'{m.group(2)}\"" if m else (str(v).strip() if v else "")


def _clean_reach(v):
    import re
    m = re.match(r"\s*(\d+(?:\.\d+)?)", str(v or ""))
    return f"{m.group(1)}\"" if m else (str(v).strip() if v else "")


def _tale_row(corner, prof):
    # record comes from ESPN (API-Sports profile is bio-only); bio from API-Sports
    row = {"name": corner.get("name"), "record": corner.get("record") or "",
           "img": corner.get("img", ""), "stats": {}}
    if isinstance(prof, dict):
        row["record"] = _prof_record(prof) or row["record"]
        if prof.get("photo"):
            row["img"] = prof["photo"]
        if prof.get("nickname"):
            row["nickname"] = prof["nickname"]
        if prof.get("height"):
            row["height"] = _clean_height(prof["height"])
        if prof.get("reach"):
            row["reach"] = _clean_reach(prof["reach"])
        if prof.get("stance"):
            row["stance"] = prof["stance"]
        if prof.get("weight"):
            row["weight"] = str(prof["weight"]).strip()
        if prof.get("age") not in (None, "", 0):
            row["age"] = prof["age"]
        team = prof.get("team") or {}
        if isinstance(team, dict) and team.get("name"):
            row["team"] = team["name"]
    return row


_STAT_LABELS = [
    ("slpm",    "Sig. strikes landed /min"),
    ("str_acc", "Striking accuracy"),
    ("sapm",    "Sig. strikes absorbed /min"),
    ("str_def", "Striking defense"),
    ("td_avg",  "Takedowns /15 min"),
    ("td_acc",  "Takedown accuracy"),
    ("td_def",  "Takedown defense"),
    ("sub_avg", "Submissions /15 min"),
]


def _num(v):
    try:
        return float("".join(c for c in str(v) if c.isdigit() or c == "."))
    except Exception:
        return None


def _tale_why(rows, raws):
    if len(rows) != 2:
        return ""
    a, b = rows[0], rows[1]
    ra_, rb_ = raws[0] or {}, raws[1] or {}
    last = lambda r: (r.get("name") or "").split()[-1] if r.get("name") else ""
    bits = []

    def wins(r):
        try:
            return int(str(r.get("record", "0-0")).split("-")[0])
        except Exception:
            return None
    wa, wb = wins(a), wins(b)
    if wa is not None and wb is not None and wa != wb:
        more = a if wa > wb else b
        bits.append(f"{last(more)} brings the deeper record ({more.get('record')})")

    sa, sb = _num(ra_.get("slpm")), _num(rb_.get("slpm"))
    if sa is not None and sb is not None and abs(sa - sb) >= 1.0:
        hi, hv = (a, sa) if sa > sb else (b, sb)
        lo = sb if sa > sb else sa
        bits.append(f"{last(hi)} is the higher-volume striker ({hv:.1f} vs {lo:.1f} sig strikes/min)")

    da, db = _num(ra_.get("str_def")), _num(rb_.get("str_def"))
    if da is not None and db is not None and abs(da - db) >= 8:
        bits.append(f"{last(a if da > db else b)} is harder to hit ({int(max(da, db))}% striking defense)")

    ta, tb = _num(ra_.get("td_def")), _num(rb_.get("td_def"))
    if ta is not None and tb is not None and abs(ta - tb) >= 10:
        bits.append(f"{last(a if ta > tb else b)} stuffs takedowns ({int(max(ta, tb))}% TD defense)")

    ga, gb = _num(ra_.get("td_avg")), _num(rb_.get("td_avg"))
    if ga is not None and gb is not None and abs(ga - gb) >= 1.5:
        hi, hv = (a, ga) if ga > gb else (b, gb)
        bits.append(f"{last(hi)} has the takedown edge ({hv:.1f}/15 min)")

    if len(bits) < 3:
        rea, reb = _num(a.get("reach")), _num(b.get("reach"))
        if rea and reb and abs(rea - reb) >= 2:
            longer = a if rea > reb else b
            bits.append(f"{last(longer)} owns a {abs(int(rea - reb))}\" reach edge")

    return ("Tale of the tape: " + "; ".join(bits[:3]) + ".") if bits else ""


def fighter_tale(date, bout):
    """Tale-of-the-tape + why. Primary deep stats from ufcstats.com; nickname/
    team/photo supplemented from API-Sports when available. Lazy + cached; only
    called from the detail route. Returns None if neither source yields data."""
    bdate = date
    iso = bout.get("kickoff_iso") or ""
    try:
        bdate = dt.date.fromisoformat(iso[:10])
    except Exception:
        bdate = date

    as_match = None
    try:
        import apisports_mma
        if apisports_mma.enabled():
            idx = _apisports_card_index(bdate)
            if idx:
                as_match = idx.get(frozenset([_last_name(bout["home"]["name"]),
                                              _last_name(bout["away"]["name"])]))
    except Exception:
        as_match = None

    got_any = False
    used_apisports = False
    rows, raws = [], []
    for corner in (bout["away"], bout["home"]):
        row = {"name": corner.get("name"), "record": corner.get("record") or "",
               "img": corner.get("img", ""), "stats": {}}
        raw = {}

        try:
            import ufcstats
            us = ufcstats.get_stats(corner.get("name"))
        except Exception:
            us = None
        if us:
            got_any = True
            raw = us
            if us.get("record"):
                row["record"] = us["record"]
            for k in ("height", "reach", "stance", "weight"):
                if us.get(k):
                    row[k] = us[k]
            for key, label in _STAT_LABELS:
                if us.get(key):
                    row["stats"][label] = us[key]

        if as_match:
            ln = _last_name(corner.get("name"))
            as_f = as_match["a"] if _last_name(as_match["a"]["name"]) == ln else as_match["b"]
            prof = None
            try:
                import apisports_mma
                prof = apisports_mma.get_fighter(as_f.get("id")) if as_f.get("id") else None
            except Exception:
                prof = None
            if isinstance(prof, dict):
                got_any = True
                used_apisports = True
                if prof.get("nickname"):
                    row["nickname"] = prof["nickname"]
                if isinstance(prof.get("team"), dict) and prof["team"].get("name"):
                    row["team"] = prof["team"]["name"]
                if prof.get("photo") and not row.get("img"):
                    row["img"] = prof["photo"]
                if prof.get("height") and not row.get("height"):
                    row["height"] = _clean_height(prof["height"])
                if prof.get("reach") and not row.get("reach"):
                    row["reach"] = _clean_reach(prof["reach"])
                if prof.get("stance") and not row.get("stance"):
                    row["stance"] = prof["stance"]
                if prof.get("weight") and not row.get("weight"):
                    row["weight"] = str(prof["weight"]).strip()

        rows.append(row)
        raws.append(raw)

    if not got_any:
        return None
    src = "ufcstats" if any(raws) else ""
    if used_apisports:
        src = (src + " + API-Sports").strip(" +")
    return {"fighters": rows, "why": _tale_why(rows, raws),
            "fight_id": as_match.get("fight_id") if as_match else None,
            "source": src or "ufcstats"}
