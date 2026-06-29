"""
reports.py — performance & accuracy reporting endpoints.

Pulled out of main.py to keep that file small enough to open on mobile. These
are pure read/compute endpoints over the settled-results log (PickResult) and
the shown-pick log (PickLog); they share no mutable state with the rest of the
app beyond the database, so they live cleanly on their own router. Public URLs
are unchanged.
"""
import datetime as dt
import time as _t

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from db import SessionLocal

router = APIRouter()


def _is_push(r):
    """Delegate to the canonical implementation in main (imported lazily to
    avoid a circular import at load time)."""
    from main import _is_push as _impl
    return _impl(r)


def _to_american(o):
    """Normalize a stored odd to American. American odds always have magnitude
    >= 100; a stored value between 1 and 100 is decimal (e.g. 1.83) and gets
    converted. This repairs older rows that captured decimal lines, which would
    otherwise read as ~+0u wins and wreck units/ROI."""
    try:
        a = float(o)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    if abs(a) >= 100:
        return a                                   # already American
    if a > 1.0:                                    # decimal -> American
        return (a - 1.0) * 100.0 if a >= 2.0 else -100.0 / (a - 1.0)
    return None


# ---- accuracy ----
_acc_cache = {"ts": 0.0, "data": None}


@router.get("/api/accuracy")
def accuracy(days: int = 30):
    """Per-sport rolling accuracy from the settled-results log (cached 2 min)."""
    import time as _t
    from models import PickResult
    if _acc_cache["data"] and _t.time() - _acc_cache["ts"] < 30 and _acc_cache["data"]["days"] == days:
        return _acc_cache["data"]
    since = dt.datetime.now() - dt.timedelta(days=days)
    _today = dt.date.today()
    by_sport = {}
    tot_p = tot_c = 0
    tot_tp = tot_tc = 0
    tot_units = 0.0
    tot_priced = 0
    alltime = {}
    at_p = at_c = 0
    with SessionLocal() as db:
        rows = db.query(PickResult).filter(PickResult.settled_date >= since).all()
        for r in rows:
            if _is_push(r):
                continue                       # draw/canceled = push, off the record
            s = by_sport.setdefault(r.sport, {"picks": 0, "correct": 0, "today_picks": 0,
                                              "today_correct": 0, "units": 0.0, "priced": 0})
            s["picks"] += 1
            tot_p += 1
            is_today = bool(r.settled_date) and r.settled_date.date() == _today
            if is_today:
                s["today_picks"] += 1
                tot_tp += 1
            if r.correct:
                s["correct"] += 1
                tot_c += 1
                if is_today:
                    s["today_correct"] += 1
                    tot_tc += 1
            if r.taken_odds is not None and abs(r.taken_odds) >= 100:  # valid line -> ROI
                prof = (r.taken_odds / 100.0) if r.taken_odds > 0 else (100.0 / (-r.taken_odds))
                pl = prof if r.correct else -1.0
                s["priced"] += 1
                s["units"] += pl
                tot_priced += 1
                tot_units += pl
        # all-time record (no date filter), per sport and overall
        allrows = db.query(PickResult).all()
        for r in allrows:
            if _is_push(r):
                continue                       # draw/canceled = push, off the record
            a = alltime.setdefault(r.sport, {"wins": 0, "losses": 0})
            at_p += 1
            if r.correct:
                a["wins"] += 1
                at_c += 1
            else:
                a["losses"] += 1
    for s, v in by_sport.items():
        v["accuracy"] = round(100 * v["correct"] / v["picks"]) if v["picks"] else None
        v["wins_30d"] = v["correct"]
        v["losses_30d"] = v["picks"] - v["correct"]
        v["today_wins"] = v.get("today_correct", 0)
        v["today_losses"] = v.get("today_picks", 0) - v.get("today_correct", 0)
        at = alltime.get(s, {"wins": 0, "losses": 0})
        v["alltime_wins"] = at["wins"]
        v["alltime_losses"] = at["losses"]
        tot = at["wins"] + at["losses"]
        v["alltime_pct"] = round(100 * at["wins"] / tot) if tot else None
        v["units_30d"] = round(v.get("units", 0.0), 2)
        v["priced_30d"] = v.get("priced", 0)
        v["roi_30d"] = round(100 * v["units"] / v["priced"], 1) if v.get("priced") else None
    data = {
        "days": days,
        "overall": {"picks": tot_p, "correct": tot_c,
                    "accuracy": round(100 * tot_c / tot_p) if tot_p else None,
                    "wins_30d": tot_c, "losses_30d": tot_p - tot_c,
                    "today_wins": tot_tc, "today_losses": tot_tp - tot_tc,
                    "units_30d": round(tot_units, 2), "priced_30d": tot_priced,
                    "roi_30d": round(100 * tot_units / tot_priced, 1) if tot_priced else None,
                    "alltime_wins": at_c, "alltime_losses": at_p - at_c,
                    "alltime_pct": round(100 * at_c / at_p) if at_p else None},
        "by_sport": by_sport,
    }
    _acc_cache["ts"] = _t.time()
    _acc_cache["data"] = data
    return data


# ---- calibration ----
@router.get("/api/calibration")
def calibration(days: int = 365, sport: str | None = None):
    """Reliability of the model's probabilities: for picks we said had an X%
    chance, how often did they actually win? Built from the probability stored
    when each pick was shown (PickLog) joined to its settled outcome. Honest:
    only picks logged since this feature shipped have a stored probability, so
    the curve fills in over time."""
    from models import LockedPickSet, PickLog, PickResult
    import json
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    probs = {}
    with SessionLocal() as db:
        # Primary source: the locked daily pick sets. They've stored each pick's
        # model probability as JSON since long before the PickLog.prob column,
        # so this is where the history actually lives.
        try:
            lrows = db.query(LockedPickSet).filter(LockedPickSet.pick_date >= cutoff).all()
        except Exception:
            lrows = []
        for row in lrows:
            try:
                picks = json.loads(row.payload)
            except Exception:
                continue
            for p in picks:
                pr, pid, sp = p.get("prob"), p.get("id"), p.get("sport")
                if pr is None or pid is None or sp is None:
                    continue
                if sport and sp != sport:
                    continue
                probs[(sp, str(pid))] = pr
        # Supplement with any PickLog rows that carry a probability.
        try:
            for l in db.query(PickLog).filter(PickLog.prob.isnot(None)).all():
                if sport and l.sport != sport:
                    continue
                probs.setdefault((l.sport, str(l.ref)), l.prob)
        except Exception:
            pass
        outcomes = {(r.sport, str(r.ref)): r.correct for r in db.query(PickResult).all()}
    data = [(p, 1 if outcomes[k] else 0) for k, p in probs.items() if k in outcomes]
    edges = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.90, 1.01]
    buckets = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        pts = [(p, o) for (p, o) in data if lo <= p < hi]
        if pts:
            n = len(pts)
            buckets.append({
                "lo": round(lo * 100), "hi": round(min(hi, 1.0) * 100), "n": n,
                "predicted": round(100 * sum(p for p, _ in pts) / n, 1),
                "actual": round(100 * sum(o for _, o in pts) / n, 1)})
    total = len(data)
    brier = round(sum((p - o) ** 2 for p, o in data) / total, 4) if total else None
    return {"buckets": buckets, "n": total, "brier": brier,
            "settled_with_prob": total, "logged_with_prob": len(probs)}



# ---- edges ----
def _implied(o):
    """American odds -> implied probability."""
    return (-o) / ((-o) + 100.0) if o < 0 else 100.0 / (o + 100.0)


@router.get("/api/edges/diag")
def edges_diag(sport: str = "tennis", days: int = 3650):
    """Inspect a sport's stored odds AND split performance by whether the model
    actually had an edge. 'Bet every predicted winner' vs 'bet only +EV picks'
    are very different strategies; this shows both."""
    import json
    from models import PickResult, LockedPickSet
    since = dt.datetime.now() - dt.timedelta(days=days)
    # model probability per (sport, ref) from the locked daily sets
    probs = {}
    with SessionLocal() as db:
        try:
            for row in db.query(LockedPickSet).all():
                for p in json.loads(row.payload):
                    if p.get("prob") is not None and p.get("id") is not None and p.get("sport"):
                        probs[(p["sport"], str(p["id"]))] = float(p["prob"])
        except Exception:
            pass

        def newbucket():
            return {"n": 0, "wins": 0, "units": 0.0}

        allp, edge_pos, edge_3, edge_5 = newbucket(), newbucket(), newbucket(), newbucket()
        clv_beat = clv_total = 0
        n = wins = 0
        vals = []
        samples = []
        q = db.query(PickResult).filter(PickResult.settled_date >= since,
                                        PickResult.sport == sport)
        for r in q.all():
            if _is_push(r) or r.taken_odds is None or abs(r.taken_odds) < 100:
                continue
            o = float(r.taken_odds)
            n += 1
            vals.append(o)
            won = bool(r.correct)
            if won:
                wins += 1
                pl = 1.0 / _implied(o) - 1.0      # decimal payout - 1
            else:
                pl = -1.0
            # accumulate "all"
            for b in (allp,):
                b["n"] += 1; b["wins"] += won; b["units"] += pl
            # edge buckets (need model prob)
            pr = probs.get((sport, str(r.ref)))
            if pr is not None:
                edge = pr - _implied(o)
                if edge > 0:
                    edge_pos["n"] += 1; edge_pos["wins"] += won; edge_pos["units"] += pl
                if edge >= 0.03:
                    edge_3["n"] += 1; edge_3["wins"] += won; edge_3["units"] += pl
                if edge >= 0.05:
                    edge_5["n"] += 1; edge_5["wins"] += won; edge_5["units"] += pl
            # CLV: did we get a better number than the close?
            if r.close_odds is not None and abs(r.close_odds) >= 100:
                clv_total += 1
                if _implied(o) < _implied(float(r.close_odds)):
                    clv_beat += 1
            if len(samples) < 12:
                samples.append({"odds": o, "won": won, "close": r.close_odds,
                                "model_prob": round(pr, 3) if pr is not None else None})
    for b in (allp, edge_pos, edge_3, edge_5):
        b["units"] = round(b["units"], 2)
        b["win_pct"] = round(100.0 * b["wins"] / b["n"], 1) if b["n"] else None
        b["roi_pct"] = round(100.0 * b["units"] / b["n"], 1) if b["n"] else None
    vals.sort()
    return {"sport": sport, "priced": n, "record": f"{wins}-{n - wins}",
            "all_picks": allp, "edge_positive": edge_pos,
            "edge_3pct_plus": edge_3, "edge_5pct_plus": edge_5,
            "clv_beat_pct": round(100.0 * clv_beat / clv_total, 1) if clv_total else None,
            "median_odds": vals[len(vals) // 2] if vals else None,
            "have_model_prob_for": len(probs), "samples": samples}


@router.get("/api/edges")
def edges_report(days: int = 90, sport: str | None = None):
    """Profitability x-ray: settled priced picks sliced by odds range and by
    sport, each with hit rate, units (flat 1u), ROI, and CLV (did we beat the
    close). This is the bet-selection tool — it shows WHERE the edge actually
    is, so you can lean into the profitable buckets and skip the rest."""
    from models import PickResult

    def _dec(a):
        return 1.0 + (a / 100.0 if a > 0 else 100.0 / (-a))

    def _imp(a):
        return (100.0 / (a + 100.0)) if a > 0 else ((-a) / ((-a) + 100.0))

    def _bucket(o):
        if o <= -250: return "1 heavy_fav (<=-250)"
        if o <= -120: return "2 fav (-250..-120)"
        if o < 120:   return "3 pickem (-120..+120)"
        if o < 250:   return "4 dog (+120..+250)"
        return "5 big_dog (>=+250)"

    def _summarize(bets):
        n = len(bets)
        if not n:
            return None
        wins = sum(1 for b in bets if b["won"])
        units = sum((_dec(b["odds"]) - 1.0) if b["won"] else -1.0 for b in bets)
        clv = [b for b in bets if b["close"] is not None]
        beat = sum(1 for b in clv if _imp(b["odds"]) < _imp(b["close"]))
        avg_clv = (sum(_imp(b["close"]) - _imp(b["odds"]) for b in clv) / len(clv) * 100.0) if clv else None
        return {
            "n": n, "wins": wins, "losses": n - wins,
            "hit_rate": round(100.0 * wins / n, 1),
            "units": round(units, 2), "roi_pct": round(100.0 * units / n, 1),
            "clv_beat_pct": round(100.0 * beat / len(clv), 1) if clv else None,
            "avg_clv_pts": round(avg_clv, 2) if avg_clv is not None else None,
            "priced_for_clv": len(clv),
        }

    since = dt.datetime.now() - dt.timedelta(days=days)
    by_bucket, by_sport = {}, {}
    with SessionLocal() as db:
        q = db.query(PickResult).filter(PickResult.settled_date >= since)
        if sport:
            q = q.filter(PickResult.sport == sport)
        for r in q.all():
            if _is_push(r) or r.taken_odds is None or abs(r.taken_odds) < 100:
                continue                       # abs<100 = invalid American = junk row
            bet = {"odds": r.taken_odds, "won": bool(r.correct), "close": r.close_odds}
            by_bucket.setdefault(_bucket(r.taken_odds), []).append(bet)
            by_sport.setdefault(r.sport, []).append(bet)

    out = {"days": days, "sport": sport or "all",
           "by_odds_bucket": {k: _summarize(v) for k, v in sorted(by_bucket.items())},
           "by_sport": {k: _summarize(v) for k, v in sorted(by_sport.items())},
           "note": ("CLV beat% > 50 and positive avg_clv_pts means you're systematically "
                    "getting better prices than the close \u2014 the strongest signal you're +EV. "
                    "Chase ROI/units, not hit rate.")}
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


# ---- performance ----
@router.get("/api/performance")
def performance(days: int = 30, sport: str | None = None):
    """
    Units won/lost, ROI, and CLV over the last N days, from settled picks that
    have captured odds. Honest about coverage: only picks with a recorded line
    count toward units/ROI/CLV; everything else still counts toward W/L.
    """
    from models import PickResult
    from clv import summarize
    import odds_api
    since = dt.datetime.now() - dt.timedelta(days=days)
    bets = []
    wins = losses = 0
    with SessionLocal() as db:
        q = db.query(PickResult).filter(PickResult.settled_date >= since)
        if sport:
            q = q.filter(PickResult.sport == sport)
        for r in q.all():
            if _is_push(r):
                continue                       # draw/canceled = push, off the record
            if r.correct:
                wins += 1
            else:
                losses += 1
            if r.taken_odds is not None and abs(r.taken_odds) >= 100:
                bets.append({"odds": r.taken_odds, "won": bool(r.correct),
                             "close_odds": r.close_odds})
    s = summarize(bets)
    # overall record includes picks without odds; betting metrics only the priced ones
    s["record_wins"] = wins
    s["record_losses"] = losses
    s["record_total"] = wins + losses
    s["odds_enabled"] = odds_api.enabled()
    s["odds_quota"] = odds_api.quota() if odds_api.enabled() else None
    s["odds_spend_today"] = odds_api.spend_today() if odds_api.enabled() else None
    s["days"] = days
    return s


# ---- picks record (per-view W/L) ----
@router.get("/api/picks/record")
def picks_record(view: str = "free", date: str | None = None):
    """
    W/L for a specific view (free|best): today's settled picks AND a rolling
    30-day figure, counting only games that view actually surfaced.
    """
    from models import PickResult, PickLog
    target = dt.date.fromisoformat(date) if date else dt.date.today()

    def tally(since_dt, until_dt):
        wins = losses = 0
        items = []
        with SessionLocal() as db:
            logged = db.query(PickLog).filter(PickLog.view == view,
                                              PickLog.shown_date >= since_dt,
                                              PickLog.shown_date <= until_dt).all()
            keys = {(l.sport, l.ref) for l in logged}
            if not keys:
                return 0, 0, []
            results = {(r.sport, r.ref): r for r in
                       db.query(PickResult).filter(PickResult.settled_date >= since_dt,
                                                   PickResult.settled_date <= until_dt + dt.timedelta(days=2)).all()}
            for k in keys:
                r = results.get(k)
                if not r:
                    continue
                if r.correct:
                    wins += 1
                else:
                    losses += 1
                items.append({"sport": k[0], "ref": k[1], "won": bool(r.correct)})
        return wins, losses, items

    # today (the picks shown for `target`)
    d0 = dt.datetime.combine(target, dt.time.min)
    d1 = dt.datetime.combine(target, dt.time.max)
    tw, tl, items = tally(d0, d1)
    tt = tw + tl

    # rolling 30 days
    m0 = dt.datetime.combine(target - dt.timedelta(days=30), dt.time.min)
    mw, ml, _ = tally(m0, d1)
    mt = mw + ml

    return {
        "view": view, "date": target.isoformat(),
        "today": {"wins": tw, "losses": tl, "total": tt,
                  "hit_rate": round(100 * tw / tt) if tt else None, "items": items},
        "month": {"wins": mw, "losses": ml, "total": mt,
                  "hit_rate": round(100 * mw / mt) if mt else None},
    }

