"""Golf matchup ROI tracker.

Records DataGolf matchups at tee-off and grades them on that round's scores so
golf shows up in /api/accuracy units/ROI alongside every other sport. Two
markets are tracked, both graded the same way (lowest round score wins, a tie
for best is a push):
  - 3_balls        : threesomes, mostly Rounds 1-2 (full field)
  - round_matchups : 2-ball head-to-heads, the weekend pairings after the cut

The favorite is the player with the lowest DataGolf odds, staked at the best
available book price. Forward-only: a matchup is tracked from the moment
record() first sees it; odds are locked at first sighting.
"""
import datetime as dt

import datagolf_api
from db import SessionLocal

_BOOKS_SKIP = {"datagolf"}   # DataGolf's own line is the model fair price, not a book


def _dec_to_american(d):
    try:
        d = float(d)
    except (TypeError, ValueError):
        return None
    if d <= 1.0:
        return None
    return int(round((d - 1.0) * 100)) if d >= 2.0 else int(round(-100.0 / (d - 1.0)))


def _best_book_decimal(odds_block, slot):
    """Best (highest) decimal price on `slot` across real books, or None."""
    best = None
    for book, line in (odds_block or {}).items():
        if book in _BOOKS_SKIP or not isinstance(line, dict):
            continue
        try:
            v = float(line.get(slot))
        except (TypeError, ValueError):
            continue
        if v and (best is None or v > best):
            best = v
    return best


def _model_fav_slot(odds_block, slots=("p1", "p2", "p3")):
    """Slot the model likes most = lowest DataGolf odds (across the slots present)."""
    dg = (odds_block or {}).get("datagolf") or {}
    best_slot, best_val = None, None
    for slot in slots:
        try:
            v = float(dg.get(slot))
        except (TypeError, ValueError):
            continue
        if best_val is None or v < best_val:
            best_slot, best_val = slot, v
    return best_slot


# market key, short tag for the ref, and the player slots it carries
_MARKETS = (("3_balls", "3b", ("p1", "p2", "p3")),
            ("round_matchups", "2b", ("p1", "p2")))


def _record_market(db, tour, market, mk_tag, slots):
    data = datagolf_api.matchups(tour, market)
    if not isinstance(data, dict):
        return 0
    ml = data.get("match_list")
    if not isinstance(ml, list) or not ml:
        return 0
    from models import GolfMatchupPick
    event = data.get("event_name") or ""
    rnd = int(data.get("round_num") or 0)
    evtag = "".join(ch for ch in event.lower() if ch.isalnum())[:8]
    added = 0
    for m in ml:
        if not isinstance(m, dict):
            continue
        names, ids, bad = {}, {}, False
        for s in slots:
            nm = datagolf_api._norm(m.get(f"{s}_player_name") or "")
            pid = str(m.get(f"{s}_dg_id") or "")
            if not nm or not pid:
                bad = True
                break
            names[s], ids[s] = nm, pid
        if bad:
            continue
        odds_block = m.get("odds") or {}
        fav = _model_fav_slot(odds_block, slots)
        if not fav:
            continue
        am = _dec_to_american(_best_book_decimal(odds_block, fav))
        if am is None:
            continue
        idtag = "_".join(ids[s] for s in slots)
        ref = f"{evtag}-r{rnd}-{mk_tag}-{idtag}"[:64]
        if db.query(GolfMatchupPick).filter_by(ref=ref).first():
            continue
        db.add(GolfMatchupPick(
            ref=ref, tour=tour, event=event[:80], round_num=rnd,
            p1=names.get("p1", "")[:48], p2=names.get("p2", "")[:48],
            p3=names.get("p3", "")[:48],
            fav_id=ids[fav][:12], fav_name=names[fav][:48], fav_slot=fav,
            taken_odds=am, recorded_date=dt.datetime.utcnow(), settled=False))
        added += 1
    return added


def record(tour="pga"):
    """Fetch current 3-balls AND 2-ball round matchups; store any not already
    tracked. Returns # added across both markets."""
    if not datagolf_api.enabled():
        return 0
    total = 0
    with SessionLocal() as db:
        for market, mk_tag, slots in _MARKETS:
            try:
                total += _record_market(db, tour, market, mk_tag, slots)
            except Exception as e:
                print(f"[golf-tracker] record {market} error: {e}")
        if total:
            db.commit()
    return total


def _round_score(bp, rnum):
    rounds = (bp or {}).get("rounds") or []
    idx = rnum - 1
    if 0 <= idx < len(rounds):
        try:
            return float(rounds[idx])
        except (TypeError, ValueError):
            return None
    return None


def _round_final(board, bp, rnum):
    """True only when round `rnum` is genuinely COMPLETE for this player. The
    board's per-round value appears mid-round, so we gate on holes played / the
    event round to avoid grading a 3-ball before the round is over."""
    if not bp:
        return False
    ev = (board or {}).get("event") or {}
    if ev.get("is_complete"):
        return True
    evr = ev.get("round") or 0
    if evr > rnum:                      # tournament has moved past this round
        return True
    if evr == rnum and (bp.get("holes") or 0) >= 18:   # finished current round
        return True
    return False


def settle(tour="pga", board=None):
    """Grade tracked matchups whose round is fully scored. Returns # settled.
    Win/loss become a PickResult(sport="golf"); a dead-heat for best is a push
    (marked settled, but not logged as a pick so it doesn't skew accuracy)."""
    if board is None:
        import golf_provider
        board = golf_provider.get_board(tour)
    players = (board or {}).get("players") or []
    by_name = {datagolf_api._norm(p.get("name") or ""): p for p in players}
    settled = 0
    with SessionLocal() as db:
        from models import GolfMatchupPick, PickResult
        pend = db.query(GolfMatchupPick).filter_by(tour=tour, settled=False).all()
        for mp in pend:
            present = [(s, getattr(mp, s)) for s in ("p1", "p2", "p3") if getattr(mp, s)]
            scores, ok = {}, True
            for slot, nm in present:
                bp = by_name.get(nm)
                if not _round_final(board, bp, mp.round_num):
                    ok = False
                    break                 # round not finished for everyone -> wait
                sc = _round_score(bp, mp.round_num)
                if sc is None:
                    ok = False
                    break
                scores[slot] = sc
            if not ok:
                continue                      # round not fully in yet -> stay pending
            low = min(scores.values())
            winners = [s for s, v in scores.items() if v == low]
            if len(winners) != 1:
                mp.settled, mp.result = True, "push"
                mp.settled_date = dt.datetime.utcnow()
                settled += 1
                continue                      # dead heat -> push, no PickResult
            wslot = winners[0]
            correct = (wslot == mp.fav_slot)
            if not db.query(PickResult).filter_by(sport="golf", ref=mp.ref).first():
                db.add(PickResult(
                    sport="golf", ref=mp.ref, settled_date=dt.datetime.utcnow(),
                    predicted=mp.fav_slot, actual=wslot, correct=correct,
                    taken_odds=mp.taken_odds, close_odds=None))
            mp.settled = True
            mp.result = "win" if correct else "loss"
            mp.settled_date = dt.datetime.utcnow()
            settled += 1
        if settled:
            db.commit()
    return settled


def diag(tour="pga"):
    out = {"enabled": datagolf_api.enabled(), "tour": tour}
    try:
        with SessionLocal() as db:
            from models import GolfMatchupPick
            q = db.query(GolfMatchupPick)
            rows = q.all()
            out["tracked_total"] = len(rows)
            out["pending"] = sum(1 for r in rows if not r.settled)
            out["settled"] = sum(1 for r in rows if r.settled)
            out["wins"] = sum(1 for r in rows if r.result == "win")
            out["losses"] = sum(1 for r in rows if r.result == "loss")
            out["pushes"] = sum(1 for r in rows if r.result == "push")
            out["recent"] = [
                {"event": r.event, "round": r.round_num, "fav": r.fav_name,
                 "odds": r.taken_odds, "result": r.result or "pending"}
                for r in sorted(rows, key=lambda r: r.id, reverse=True)[:8]]
    except Exception as e:
        out["error"] = str(e)
    return out
