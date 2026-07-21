"""
capper_routes.py — all /api/capper/* endpoints, extracted from main.py so the
main file stays small enough to edit on a phone.

Wiring (already done in main.py, AFTER model_lookup is defined):
    from capper_routes import router as _capper_router
    app.include_router(_capper_router)

Split of responsibilities:
  * This file: the HTTP surface (track / mine / stats / leaderboard / set /
    community / diag / regrade) and _ensure_capper_table.
  * main.py keeps the settlement-side graders (_grade_capper_picks,
    _capper_apply_result, _catchup_grade_cappers) because game settling calls
    them, plus model_lookup because the AI write-up path also uses it.

The two helpers this file needs from main (_catchup_grade_cappers and
model_lookup) are reached through _M(), a deferred import of main. Deferred
because main imports this file at load time — importing main back at the top
would be circular. By the time any request handler runs, main is fully loaded.
"""
import datetime as dt
import re
import time

from fastapi import APIRouter

from db import SessionLocal

router = APIRouter()


def _M():
    """Deferred handle on main.py (avoids a circular import at load time)."""
    import main
    return main


@router.get("/api/capper/diag")
def capper_diag():
    """Diagnostic: show pending capper picks next to the settled results for the
    same sport, so a ref-format mismatch is obvious."""
    from models import CapperPick, PickResult
    _ensure_capper_table()
    out = {"pending": [], "recent_results": {}, "match_test": []}
    try:
        with SessionLocal() as db:
            pend = db.query(CapperPick).filter(CapperPick.status == "pending").all()
            for r in pend:
                out["pending"].append({
                    "id": r.id, "sport": r.sport, "ref": r.ref,
                    "ref_type": type(r.ref).__name__,
                    "pick": r.pick, "match": r.match, "event_date": r.event_date,
                })
            # recent settled results per sport involved
            for sp in {p["sport"] for p in out["pending"] if p["sport"]}:
                rows = (db.query(PickResult)
                          .filter_by(sport=sp)
                          .order_by(PickResult.id.desc()).limit(15).all())
                out["recent_results"][sp] = [
                    {"ref": r.ref, "ref_type": type(r.ref).__name__,
                     "predicted": r.predicted, "actual": r.actual,
                     "settled": str(r.settled_date)[:19]}
                    for r in rows
                ]
            # direct lookup test for each pending pick
            for r in pend:
                if not r.ref:
                    out["match_test"].append({"id": r.id, "result": "no ref stored"})
                    continue
                hit = (db.query(PickResult)
                         .filter_by(sport=r.sport, ref=str(r.ref)).first())
                out["match_test"].append({
                    "id": r.id, "sport": r.sport, "ref": str(r.ref),
                    "found_result": bool(hit),
                    "actual": (hit.actual if hit else None),
                })
    except Exception as e:
        out["error"] = str(e)
    return out


@router.get("/api/capper/regrade")
def capper_regrade():
    """Manually run the catch-up grader (also runs automatically on stats reads)."""
    _ensure_capper_table()
    n = _M()._catchup_grade_cappers()
    return {"graded": n}


def _ensure_capper_table():
    """Ensure capper_picks exists AND has the current columns. create_all only
    creates missing tables — it won't add columns to an out-of-date table. Since
    this table holds only tracked picks (safe to rebuild while empty of real
    data), if the schema is stale we drop and recreate it with the full columns."""
    try:
        from models import CapperPick
        from sqlalchemy import inspect as _sa_inspect
        with SessionLocal() as _s:
            bind = _s.get_bind()
        insp = _sa_inspect(bind)
        if insp.has_table("capper_picks"):
            cols = {c["name"] for c in insp.get_columns("capper_picks")}
            expected = set(CapperPick.__table__.columns.keys())
            if not expected.issubset(cols):
                # stale schema (missing a column like 'ref') -> rebuild clean
                print(f"[capper] schema stale (have {cols}, need {expected}); rebuilding table")
                CapperPick.__table__.drop(bind=bind, checkfirst=True)
                CapperPick.__table__.create(bind=bind, checkfirst=True)
        else:
            CapperPick.__table__.create(bind=bind, checkfirst=True)
    except Exception as e:
        print(f"[capper] ensure table failed: {e}")


@router.post("/api/capper/track")
async def capper_track(payload: dict):
    """Store a tracked pick. Bot posts: {user_id, username, team, sport?, stake_units?}
    The line is captured from the model board via model_lookup."""
    from models import CapperPick
    _ensure_capper_table()
    user_id = str(payload.get("user_id") or "").strip()
    username = str(payload.get("username") or "").strip()[:64]
    team = str(payload.get("team") or "").strip()
    stake = payload.get("stake_units")
    try:
        stake = float(stake) if stake is not None else 1.0
    except (TypeError, ValueError):
        stake = 1.0
    stake = max(0.1, min(stake, 100.0))
    if not user_id or not team:
        return {"ok": False, "error": "missing user or team"}

    look = _M().model_lookup(team=team, sport=payload.get("sport") or None)
    if not look.get("found"):
        return {"ok": False, "error": "not_on_board",
                "message": f"'{team}' isn't on the model board right now."}

    # Record the side the USER named. `look["pick"]` is the model's side, which is
    # often the opponent — using it booked people onto the wrong team whenever the
    # model disagreed with them. If the side can't be resolved from the matchup,
    # refuse rather than guess.
    side = look.get("matched_side")
    if not side:
        return {"ok": False, "error": "side_unresolved",
                "message": (f"Found {look.get('match')} but couldn't tell which side "
                            f"'{team}' means. Try the full team name.")}

    is_model_pick = bool(look.get("side_is_model_pick"))
    prob = look.get("prob")
    if not is_model_pick and prob is not None:
        try:
            prob = round(1.0 - float(prob), 4)      # user took the other side
        except (TypeError, ValueError):
            prob = None
    # market_odds on the board is priced for the MODEL's side only. Attaching it to
    # the opposite side would book a real bet at a price that was never offered.
    odds = look.get("market_odds") if is_model_pick else None

    row = CapperPick(
        discord_user_id=user_id, discord_username=username,
        sport=look.get("sport"), ref=look.get("ref"),
        match=look.get("match"), pick=side,
        market_odds=odds, stake_units=stake,
        prob=prob, status="pending", event_date=look.get("date"),
    )
    try:
        with SessionLocal() as db:
            db.add(row)
            db.commit()
            db.refresh(row)
            pid = row.id
    except Exception as e:
        print(f"[capper] track insert failed: {e}")
        return {"ok": False, "error": "db_error"}

    return {"ok": True, "id": pid, "pick": side, "match": look.get("match"),
            "market_odds": odds, "stake_units": stake,
            "sport": look.get("sport"), "event_date": look.get("date"),
            "model_pick": look.get("pick"), "agrees_with_model": is_model_pick,
            "note": (None if odds is not None else
                     "No market price on file for this side — graded at even money "
                     "unless a price is set later.")}


@router.get("/api/capper/mine")
def capper_mine(user_id: str = ""):
    """Tracked picks. Pass user_id for one member, or omit it to list all picks
    (handy for verifying tracking/grading without knowing a Discord ID)."""
    from models import CapperPick
    _ensure_capper_table()
    try:
        with SessionLocal() as db:
            q = db.query(CapperPick)
            if user_id:
                q = q.filter(CapperPick.discord_user_id == str(user_id))
            rows = q.order_by(CapperPick.id.desc()).all()
            out = [{
                "id": r.id, "user": r.discord_username, "pick": r.pick,
                "match": r.match, "sport": r.sport, "ref": r.ref,
                "market_odds": r.market_odds, "stake_units": r.stake_units,
                "status": r.status, "units_pl": r.units_pl,
                "event_date": r.event_date,
            } for r in rows]
    except Exception as e:
        print(f"[capper] mine query failed: {e}")
        return {"picks": []}
    return {"count": len(out), "picks": out}


# ---- Capper stats: per-user record + leaderboard (Stage 2) --------------------

def _capper_summarize(rows):
    """Aggregate a list of CapperPick rows into a record. units_pl is only set on
    graded picks, so pending picks contribute to counts but not W/L/units."""
    wins = losses = pushes = pending = 0
    units_pl = units_staked = 0.0
    for r in rows:
        st = (r.status or "pending").lower()
        if st == "win":
            wins += 1
        elif st == "loss":
            losses += 1
        elif st == "push":
            pushes += 1
        else:
            pending += 1
            continue
        units_staked += (r.stake_units or 0)
        if r.units_pl is not None:
            units_pl += r.units_pl
    decided = wins + losses
    return {
        "total": len(rows), "wins": wins, "losses": losses,
        "pushes": pushes, "pending": pending,
        "record": f"{wins}-{losses}" + (f"-{pushes}" if pushes else ""),
        "win_pct": round(100 * wins / decided, 1) if decided else None,
        "units_pl": round(units_pl, 2),
        "roi_pct": round(100 * units_pl / units_staked, 1) if units_staked else None,
    }


@router.get("/api/capper/stats")
def capper_stats(user_id: str):
    """One user's tracked-pick record."""
    from models import CapperPick
    _ensure_capper_table()
    _M()._catchup_grade_cappers()   # settle anything whose game already finished
    try:
        with SessionLocal() as db:
            rows = (db.query(CapperPick)
                      .filter(CapperPick.discord_user_id == str(user_id)).all())
            summ = _capper_summarize(rows)
            # per-sport breakdown so members can see where they're actually good
            from collections import defaultdict
            bysport = defaultdict(list)
            for r in rows:
                if r.sport:
                    bysport[r.sport].append(r)
            summ["by_sport"] = {sp: _capper_summarize(rs) for sp, rs in bysport.items()}
            # recent form: last 10 graded picks, newest first
            graded = [r for r in rows if (r.status or "") in ("win", "loss")]
            graded.sort(key=lambda r: (r.graded_at or r.created_at), reverse=True)
            summ["recent_form"] = [("W" if r.status == "win" else "L") for r in graded[:10]]
    except Exception as e:
        print(f"[capper] stats failed: {e}")
        return {"total": 0}
    summ["user_id"] = str(user_id)
    return summ


@router.get("/api/capper/leaderboard")
def capper_leaderboard(sort: str = "units", min_decided: int = 1):
    """Leaderboard across all cappers. sort: 'units' (default) or 'winpct'.
    min_decided filters out people with too few graded picks to rank fairly."""
    from models import CapperPick
    from collections import defaultdict
    _ensure_capper_table()
    _M()._catchup_grade_cappers()   # settle anything whose game already finished
    try:
        with SessionLocal() as db:
            rows = db.query(CapperPick).all()
    except Exception as e:
        print(f"[capper] leaderboard failed: {e}")
        return {"cappers": []}

    by_user = defaultdict(list)
    names = {}
    for r in rows:
        by_user[r.discord_user_id].append(r)
        if r.discord_username:
            names[r.discord_user_id] = r.discord_username

    board = []
    for uid, urows in by_user.items():
        s = _capper_summarize(urows)
        s["user_id"] = uid
        s["username"] = names.get(uid, "capper")
        board.append(s)

    decided = [c for c in board if (c["wins"] + c["losses"]) >= min_decided]
    if sort == "winpct":
        decided.sort(key=lambda c: (c["win_pct"] or -1, c["units_pl"]), reverse=True)
    else:
        decided.sort(key=lambda c: (c["units_pl"], c["win_pct"] or -1), reverse=True)

    # people with only pending picks (not yet rankable) shown separately as count
    building = [c for c in board if (c["wins"] + c["losses"]) < min_decided]
    return {"cappers": decided[:15], "building": len(building), "total_cappers": len(board)}


@router.get("/api/capper/set")
def capper_set(id: int, result: str):
    """Manually settle a capper pick that can't auto-grade (e.g. tracked without
    a ref), or delete it. result: win | loss | push | pending | delete"""
    from models import CapperPick
    _ensure_capper_table()
    res = (result or "").strip().lower()
    if res not in ("win", "loss", "push", "delete", "pending"):
        return {"ok": False, "error": "result must be win|loss|push|pending|delete"}
    try:
        with SessionLocal() as db:
            r = db.query(CapperPick).filter_by(id=int(id)).first()
            if not r:
                return {"ok": False, "error": "not found"}
            if res == "delete":
                db.delete(r)
                db.commit()
                return {"ok": True, "deleted": int(id)}
            stake = r.stake_units or 1.0
            if res == "pending":
                r.status, r.units_pl, r.graded_at = "pending", None, None
            elif res == "push":
                r.status, r.units_pl = "push", 0.0
                r.graded_at = dt.datetime.now()
            elif res == "win":
                odds = r.market_odds
                r.units_pl = (stake if odds is None else
                              stake * (odds / 100.0) if odds > 0 else
                              stake * (100.0 / abs(odds)))
                r.status = "win"
                r.graded_at = dt.datetime.now()
            else:
                r.status, r.units_pl = "loss", -stake
                r.graded_at = dt.datetime.now()
            db.commit()
            return {"ok": True, "id": r.id, "status": r.status, "units_pl": r.units_pl}
    except Exception as e:
        print(f"[capper] manual set failed: {e}")
        return {"ok": False, "error": str(e)}


# ---- /api/ladder/status : current challenge state + today's leg + history -----
# Powers the Discord ladder post and the /ladder command.

@router.get("/api/ladder/status")
def ladder_status(history: int = 5):
    from models import LadderState, LadderLeg
    out = {"state": None, "current_leg": None, "history": []}
    try:
        with SessionLocal() as db:
            st = db.query(LadderState).order_by(LadderState.id.asc()).first()
            if st:
                out["state"] = {
                    "rung": st.rung, "bankroll": round(st.bankroll, 2),
                    "start_bankroll": round(st.start_bankroll, 2),
                    "attempt": st.attempt,
                    "best_rung_ever": st.best_rung_ever,
                    "best_bankroll_ever": round(st.best_bankroll_ever, 2),
                    "completed_runs": st.completed_runs,
                    "updated": str(st.updated)[:19],
                }
            # the live (unsettled) leg — OLDEST first, so a leg still awaiting its
            # result is shown rather than skipped over by a newer one. If more than
            # one is pending, that means settlement is behind; surface the count
            # instead of hiding it.
            pending = (db.query(LadderLeg)
                         .filter(LadderLeg.settled == False)  # noqa: E712
                         .order_by(LadderLeg.pick_date.asc(), LadderLeg.id.asc()).all())
            leg = pending[0] if pending else None
            out["pending_legs"] = len(pending)
            if leg:
                out["current_leg"] = {
                    "rung": leg.rung, "attempt": leg.attempt, "sport": leg.sport,
                    "pick": leg.pick, "odds": leg.odds, "edge_pct": leg.edge_pct,
                    "stake": round(leg.stake, 2), "to_return": round(leg.to_return, 2),
                    "pick_date": str(leg.pick_date)[:10], "game_ref": leg.game_ref,
                }
            hist = (db.query(LadderLeg)
                      .filter(LadderLeg.settled == True)  # noqa: E712
                      .order_by(LadderLeg.pick_date.desc())
                      .limit(max(1, min(history, 20))).all())
            out["history"] = [{
                "date": str(l.pick_date)[:10], "rung": l.rung, "attempt": l.attempt,
                "sport": l.sport, "pick": l.pick, "odds": l.odds,
                "stake": round(l.stake, 2), "to_return": round(l.to_return, 2),
                "result": l.result,
            } for l in hist]
    except Exception as e:
        print(f"[ladder] status failed: {e}")
        out["error"] = str(e)[:200]
    return out


# ---- /api/capper/community : the room's combined record ----------------------
# Treats every tracked pick in the server as one collective capper, so the
# community has its own W/L, units and ROI alongside individual records.

@router.get("/api/capper/community")
def capper_community(days: int = 0):
    """days=0 -> all time. Otherwise limit to picks created in the last N days."""
    from models import CapperPick
    from collections import defaultdict
    _ensure_capper_table()
    _M()._catchup_grade_cappers()
    try:
        with SessionLocal() as db:
            q = db.query(CapperPick)
            if days and days > 0:
                since = dt.datetime.now() - dt.timedelta(days=int(days))
                q = q.filter(CapperPick.created_at >= since)
            rows = q.all()
            summ = _capper_summarize(rows)
            bysport = defaultdict(list)
            for r in rows:
                if r.sport:
                    bysport[r.sport].append(r)
            summ["by_sport"] = {sp: _capper_summarize(rs) for sp, rs in bysport.items()}
            summ["cappers"] = len({r.discord_user_id for r in rows})
            # most-tracked picks (where the room is piling in)
            counts = defaultdict(int)
            for r in rows:
                if r.status == "pending" and r.pick:
                    counts[r.pick] += 1
            summ["hot_picks"] = sorted(
                [{"pick": k, "count": v} for k, v in counts.items() if v > 1],
                key=lambda x: x["count"], reverse=True)[:5]
    except Exception as e:
        print(f"[capper] community failed: {e}")
        return {"total": 0}
    summ["days"] = days or None
    return summ
