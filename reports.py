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
from calibrate import calibrate as _calibrate

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
            if r.sport == "golf":
                continue                       # golf is view-only: never tracked
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
                # Headline Tennis units/ROI count ONLY the three main tours
                # (ATP, WTA, Challenger). ITF/futures and untagged historical picks
                # ("EARLIER") are excluded from the money line — they still appear
                # in win/loss counts and the per-tour breakdown, just not the total.
                skip_units = (r.sport == "tennis" and
                              (r.subcat or "").upper() not in ("ATP", "WTA", "CHALLENGER"))
                if not skip_units:
                    s["priced"] += 1
                    s["units"] += pl
                    tot_priced += 1
                    tot_units += pl
        # all-time record (no date filter), per sport and overall
        allrows = db.query(PickResult).all()
        for r in allrows:
            if r.sport == "golf":
                continue                       # golf is view-only: never tracked
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
@router.get("/api/results/recent")
def recent_results(days: int = 5):
    """Public: recent graded picks grouped by day \u2014 the honest receipts."""
    from models import LockedPickSet, PickResult
    import json
    import datetime as _dt
    cutoff = _dt.datetime.now() - _dt.timedelta(days=days + 2)
    by_day = {}
    with SessionLocal() as db:
        try:
            rows = db.query(LockedPickSet).filter(
                LockedPickSet.view == "free", LockedPickSet.pick_date >= cutoff).all()
        except Exception:
            rows = []
        outcomes = {(r.sport, str(r.ref)): r for r in db.query(PickResult).all()}

    def _pick_units(r):
        """Profit in units for one settled pick, using the SAME rule as
        /api/accuracy: a valid taken line only, flat 1u risk, and tennis counted
        only on the three main tours. Keeping the rule identical is the point —
        a chart drawn from a different rule would quietly contradict the
        headline record on the same page."""
        if r.taken_odds is None or abs(r.taken_odds) < 100:
            return None
        if r.sport == "tennis" and (r.subcat or "").upper() not in ("ATP", "WTA", "CHALLENGER"):
            return None
        prof = (r.taken_odds / 100.0) if r.taken_odds > 0 else (100.0 / (-r.taken_odds))
        return round(prof if r.correct else -1.0, 4)

    def _pick_beat_close(r):
        """True/False if we can compare the taken line to the close, else None.
        Mirrors the CLV test in /api/edges/diag."""
        if r.taken_odds is None or r.close_odds is None:
            return None
        if abs(r.taken_odds) < 100 or abs(r.close_odds) < 100:
            return None
        def _imp(o):
            o = float(o)
            return (-o) / ((-o) + 100.0) if o < 0 else 100.0 / (o + 100.0)
        return _imp(float(r.taken_odds)) < _imp(float(r.close_odds))

    for row in rows:
        try:
            plist = json.loads(row.payload)
        except Exception:
            continue
        day = str(row.pick_date)[:10]
        for p in plist:
            r = outcomes.get((p.get("sport"), str(p.get("id"))))
            if r is None or r.correct is None:
                continue
            by_day.setdefault(day, []).append({
                "pick": (p.get("pick") or "").replace(" to win", ""),
                "prob": round(float(p.get("prob", 0)) * 100),
                "sport": p.get("sport"), "won": bool(r.correct), "match": p.get("match"),
                # new: lets the site chart units and CLV over time without
                # inventing numbers from aggregates
                "odds": r.taken_odds,
                "units": _pick_units(r),
                "beat_close": _pick_beat_close(r)})
    out = []
    for day in sorted(by_day.keys(), reverse=True)[:days]:
        picks = sorted(by_day[day], key=lambda x: x["prob"], reverse=True)
        w = sum(1 for x in picks if x["won"])
        priced = [x for x in picks if x["units"] is not None]
        day_u = round(sum(x["units"] for x in priced), 2) if priced else None
        clv_rows = [x for x in picks if x["beat_close"] is not None]
        day_clv = (round(100.0 * sum(1 for x in clv_rows if x["beat_close"]) / len(clv_rows), 1)
                   if clv_rows else None)
        out.append({"date": day, "w": w, "l": len(picks) - w,
                    "record": f"{w}-{len(picks)-w}", "picks": picks,
                    "units": day_u, "priced": len(priced),
                    "clv_beat_pct": day_clv, "clv_n": len(clv_rows)})
    tw = sum(d["w"] for d in out)
    tl = sum(d["l"] for d in out)
    return {"days": out, "summary": {"w": tw, "l": tl, "record": f"{tw}-{tl}"}}


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


@router.get("/api/ext/status")
def _ext_status():
    """Which external-API keys are configured (never returns the keys)."""
    import apifootball, balldontlie
    return {"apifootball": apifootball.available(),
            "thesportsdb": True,
            "balldontlie": balldontlie.available()}


@router.get("/api/ext/apifootball")
def _ext_apifootball(name: str, league: int = 39, season: int = 2024):
    """Test: rich soccer team season stats for an ESPN team name."""
    import apifootball
    if not apifootball.available():
        return {"error": "APIFOOTBALL_KEY not set"}
    return apifootball.team_stats(name, league, season) or {"result": None}


@router.get("/api/ext/apifootball-fixture")
def _ext_af_fixture(id: int):
    """Test raw per-fixture stats (incl xG) by api-football fixture id directly,
    bypassing team matching — isolates whether your plan returns xG at all."""
    import apifootball
    if not apifootball.available():
        return {"error": "APIFOOTBALL_KEY not set"}
    return apifootball.fixture_stats(id) or {"result": None}


@router.get("/api/ext/apifootball-xg")
def _ext_af_xg(home: str, away: str, league: int = 39, season: int = 2024, date: str | None = None):
    """Test the full chain: ESPN match (teams + date) -> api-football fixture -> xG."""
    import apifootball
    import datetime as dt
    if not apifootball.available():
        return {"error": "APIFOOTBALL_KEY not set"}
    return apifootball.match_xg(league, season, date or dt.date.today().isoformat(), home, away) or {"result": None}


@router.get("/api/ext/thesportsdb")
def _ext_thesportsdb(name: str):
    """Test: badge / stadium / jersey media for a team name."""
    import thesportsdb
    return thesportsdb.team_media(name) or {"result": None}


@router.get("/api/ext/balldontlie")
def _ext_balldontlie(sport: str = "nba", resource: str = "standings", season: int | None = None):
    """Test: raw balldontlie payload for a sport + resource."""
    import balldontlie
    if not balldontlie.available():
        return {"error": "BALLDONTLIE_KEY not set"}
    return balldontlie.get(sport, resource, {"season": season} if season else None) or {"result": None}


@router.get("/api/team-profile")
def team_profile(sport: str, team_id: str, name: str | None = None, league: str | None = None):
    """Honest team profile — power rating (where we have Elo), record, recent
    form, home/away splits, scoring for/against, current streak — computed from
    the team's real schedule. No fabricated pace/ATS/clutch numbers."""
    try:
        prof = None
        if sport in ("nba", "nfl", "ncaaf", "ncaab", "wncaab"):
            import espn_provider
            prof = espn_provider.team_profile(sport, team_id, name)
        elif sport == "nhl":
            import nhl_games
            prof = nhl_games.team_profile(team_id, name)
        elif sport == "mlb":
            import mlb_provider
            prof = mlb_provider.team_profile(team_id, name)
        elif sport == "soccer":
            import soccer_provider
            prof = soccer_provider.team_profile(team_id, name, league)
        else:
            return {"sport": sport, "team_id": team_id, "name": name, "unsupported": True}
        # Enrich with a crest/badge (best-effort; a miss just omits it).
        if isinstance(prof, dict) and prof.get("name") and not prof.get("badge"):
            try:
                import thesportsdb
                b = thesportsdb.badge(prof["name"])
                if b:
                    prof["badge"] = b
            except Exception:
                pass
        return prof
    except Exception as e:
        return {"sport": sport, "team_id": team_id, "name": name, "error": str(e)}



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
        with_prob = pr_prob_n = 0
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
            # edge buckets (need model prob) — prefer the pick's own durable prob,
            # which the snapshot now stamps on every settled game, over the older
            # locked-set lookup.
            has_pr_prob = getattr(r, "prob", None) is not None
            if has_pr_prob:
                pr_prob_n += 1
            pr = r.prob if has_pr_prob else probs.get((sport, str(r.ref)))
            if pr is not None:
                with_prob += 1
                edge = _calibrate(sport, pr) - _implied(o)   # calibrated edge — honest
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
            "have_model_prob_for": with_prob,
            "pick_results_with_prob": pr_prob_n,
            "samples": samples}


@router.get("/api/edges/simulate")
def edges_simulate(days: int = 3650, sport: str | None = None):
    """Backtest: 'what would my record/ROI have been if I only bet picks at or
    above each edge threshold?' Sweeps several thresholds over graded picks that
    carry a recorded model probability. Honest — same real results, just filtered.
    The pool is limited to picks with a stored prob, so it grows over time."""
    import json
    from models import PickResult, LockedPickSet
    since = dt.datetime.now() - dt.timedelta(days=days)
    locked = {}
    with SessionLocal() as db:
        try:
            for row in db.query(LockedPickSet).all():
                for p in json.loads(row.payload):
                    if p.get("prob") is not None and p.get("id") is not None and p.get("sport"):
                        locked[(p["sport"], str(p["id"]))] = float(p["prob"])
        except Exception:
            pass
        q = db.query(PickResult).filter(PickResult.settled_date >= since)
        if sport:
            q = q.filter(PickResult.sport == sport)
        rows = q.all()

    pool = []   # (edge, won, pl, beat_close|None)
    for r in rows:
        if _is_push(r) or r.taken_odds is None or abs(r.taken_odds) < 100:
            continue
        prob = r.prob if getattr(r, "prob", None) is not None else locked.get((r.sport, str(r.ref)))
        if prob is None:
            continue
        imp = _implied(float(r.taken_odds))
        won = bool(r.correct)
        pl = (1.0 / imp - 1.0) if won else -1.0
        beat = None
        if r.close_odds is not None and abs(r.close_odds) >= 100:
            beat = imp < _implied(float(r.close_odds))
        pool.append((_calibrate(r.sport, prob) - imp, won, pl, beat))

    out = []
    for thr in (0.0, 0.02, 0.03, 0.05, 0.07, 0.10):
        sel = [x for x in pool if x[0] >= thr]
        n = len(sel)
        wins = sum(1 for x in sel if x[1])
        units = sum(x[2] for x in sel)
        ct = sum(1 for x in sel if x[3] is not None)
        cb = sum(1 for x in sel if x[3])
        out.append({"threshold_pct": round(thr * 100, 1), "wagers": n,
                    "wins": wins, "losses": n - wins,
                    "units": round(units, 2),
                    "roi_pct": round(100.0 * units / n, 1) if n else None,
                    "win_pct": round(100.0 * wins / n, 1) if n else None,
                    "clv_beat_pct": round(100.0 * cb / ct, 1) if ct else None})
    return {"sport": sport, "pool": len(pool), "rows": out}


@router.get("/api/tennis/tours")
def tennis_tours():
    """Tennis Track Record split by tour. The headline Tennis total EXCLUDES
    ITF/futures (tracked as its own sub-category), so the lowest tier never drags
    the main tennis number. Untagged historical picks (before tour tagging existed)
    stay in the total under 'EARLIER' since they predate ITF ingestion."""
    from models import PickResult
    with SessionLocal() as db:
        rows = db.query(PickResult).filter(PickResult.sport == "tennis").all()

    def blank():
        return {"n": 0, "wins": 0, "priced": 0, "units": 0.0, "clv_beat": 0, "clv_total": 0}

    def add(bucket, r, won):
        bucket["n"] += 1
        bucket["wins"] += 1 if won else 0
        if r.taken_odds is not None and abs(r.taken_odds) >= 100 and not _is_push(r):
            imp = _implied(float(r.taken_odds))
            bucket["priced"] += 1
            bucket["units"] += (1.0 / imp - 1.0) if won else -1.0
            if r.close_odds is not None and abs(r.close_odds) >= 100:
                bucket["clv_total"] += 1
                bucket["clv_beat"] += 1 if imp < _implied(float(r.close_odds)) else 0

    tours, total = {}, blank()
    for r in rows:
        sub = (r.subcat or "EARLIER").upper()
        won = bool(r.correct)
        add(tours.setdefault(sub, blank()), r, won)
        if sub in ("ATP", "WTA", "CHALLENGER"):     # headline = the three main tours only
            add(total, r, won)

    def finish(b):
        n, pr = b["n"], b["priced"]
        return {"wins": b["wins"], "losses": n - b["wins"],
                "win_pct": round(100.0 * b["wins"] / n, 1) if n else None,
                "units": round(b["units"], 2) if pr else None,
                "roi_pct": round(100.0 * b["units"] / pr, 1) if pr else None,
                "priced": pr,
                "clv_beat_pct": round(100.0 * b["clv_beat"] / b["clv_total"], 1) if b["clv_total"] else None}

    order = ["ATP", "WTA", "CHALLENGER", "ITF", "EARLIER"]
    out = {k: finish(tours[k]) for k in order if k in tours}
    return {"total_excl_itf": finish(total), "tours": out,
            "itf_included": "ITF" in tours}


@router.get("/api/edges/wagers")
def edges_wagers(days: int = 3650, min_edge: float = 0.03, min_sample: int = 25):
    """Per-sport performance on RECOMMENDED WAGERS only: picks where the model's
    probability beat the market's implied probability by >= min_edge. This is the
    honest 'if you followed the flagged plays' record — not 'bet every favorite'.
    A sport is only 'mature' (safe to showcase units) once it has min_sample
    graded wagers; until then the UI shows a building-sample state."""
    import json
    from models import PickResult, LockedPickSet
    since = dt.datetime.now() - dt.timedelta(days=days)
    locked = {}
    with SessionLocal() as db:
        try:    # prob fallback for rows settled before PickResult.prob existed
            for row in db.query(LockedPickSet).all():
                for p in json.loads(row.payload):
                    if p.get("prob") is not None and p.get("id") is not None and p.get("sport"):
                        locked[(p["sport"], str(p["id"]))] = float(p["prob"])
        except Exception:
            pass
        rows = db.query(PickResult).filter(PickResult.settled_date >= since).all()

    def blank():
        return {"n": 0, "wins": 0, "units": 0.0, "clv_beat": 0, "clv_total": 0}

    by, ov = {}, blank()
    for r in rows:
        if _is_push(r) or r.taken_odds is None or abs(r.taken_odds) < 100:
            continue
        prob = r.prob if getattr(r, "prob", None) is not None else locked.get((r.sport, str(r.ref)))
        if prob is None:
            continue
        imp = _implied(float(r.taken_odds))
        if _calibrate(r.sport, prob) - imp < min_edge:
            continue
        won = bool(r.correct)
        pl = (1.0 / imp - 1.0) if won else -1.0
        s = by.setdefault(r.sport, blank())
        for b in (s, ov):
            b["n"] += 1; b["wins"] += won; b["units"] += pl
        if r.close_odds is not None and abs(r.close_odds) >= 100:
            beat = imp < _implied(float(r.close_odds))
            for b in (s, ov):
                b["clv_total"] += 1; b["clv_beat"] += 1 if beat else 0

    def finish(s):
        n = s["n"]
        return {"wagers": n, "wins": s["wins"], "losses": n - s["wins"],
                "units": round(s["units"], 2),
                "roi_pct": round(100.0 * s["units"] / n, 1) if n else None,
                "win_pct": round(100.0 * s["wins"] / n, 1) if n else None,
                "clv_beat_pct": round(100.0 * s["clv_beat"] / s["clv_total"], 1) if s["clv_total"] else None,
                "mature": n >= min_sample}

    return {"min_edge": min_edge, "min_sample": min_sample,
            "overall": finish(ov),
            "by_sport": {k: finish(v) for k, v in by.items()}}


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


@router.get("/api/ratings/reset-volume")
def _ratings_reset_volume(confirm: str = ""):
    """Delete the stale /data/ratings.json (an old, small feed-built file) so the app
    loads your committed ratings file instead, then hot-reload the live engine from
    it — no redeploy needed. Mirrors /api/surface/reset-volume but for ratings.

    Why this exists: the startup loader reads /data/ratings.json FIRST, so an old
    2,360-player volume file was shadowing a freshly committed 23k-player file. This
    removes the shadow and loads the committed file in one shot."""
    import os, glob
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes to delete /data/ratings.json "
                             "and load your committed ratings file instead"})
    path = "/data/ratings.json"
    existed = os.path.exists(path)
    try:
        if existed:
            os.remove(path)
    except Exception as e:
        return JSONResponse({"error": f"could not delete {path}: {e}"})

    result = {"deleted_volume_file": existed}
    try:
        import main
        rfile = os.environ.get("RATINGS_FILE", "ratings.json")
        # try the configured file first, then the newest committed ratings*.json,
        # skipping anything on /data (that's the volume we just cleared)
        cands = [rfile] + sorted(glob.glob("ratings*.json"),
                                 key=os.path.getmtime, reverse=True)
        loaded, src = 0, None
        for c in cands:
            if c and not str(c).startswith("/data") and os.path.exists(c):
                n = main.engine.load_ratings(c)
                if n:
                    loaded, src = n, c
                    break
        if loaded:
            main._MODEL_STATUS["ratings_loaded"] = loaded
            main._MODEL_STATUS["mode"] = "surface-elo"
        result["loaded_from"] = src
        result["ratings_loaded"] = loaded
        result["note"] = ("now on committed ratings" if loaded
                          else "no committed ratings*.json found in repo root")
    except Exception as e:
        result["reload_error"] = str(e)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
