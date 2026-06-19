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
import unicodedata

import datagolf_api
from db import SessionLocal

_BOOKS_SKIP = {"datagolf"}   # DataGolf's own line is the model fair price, not a book

# DataGolf anglicizes names (Hojgaard, Norgaard) while ESPN keeps diacritics
# (Højgaard, Nørgaard). Fold both sides so the grader can line players up.
_TRANS = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "å": "a", "Å": "a",
                        "ß": "ss", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ð": "d",
                        "ı": "i", "þ": "th"})


def _fold(s):
    s = (s or "").translate(_TRANS)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).strip().lower()


def _board_index(players):
    """name -> player row, with accent/special-letter-folded aliases so matchup
    names line up with the scoreboard even when diacritics or spellings differ."""
    idx = {}
    for p in players:
        nm = datagolf_api._norm(p.get("name") or "")
        if nm:
            idx.setdefault(nm, p)
            idx.setdefault(_fold(nm), p)
    return idx


def _lookup(idx, nm):
    return idx.get(nm) or idx.get(_fold(nm))


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
    by_name = _board_index(players)
    settled = 0
    with SessionLocal() as db:
        from models import GolfMatchupPick, PickResult
        pend = db.query(GolfMatchupPick).filter_by(tour=tour, settled=False).all()
        for mp in pend:
            present = [(s, getattr(mp, s)) for s in ("p1", "p2", "p3") if getattr(mp, s)]
            scores, ok = {}, True
            for slot, nm in present:
                bp = _lookup(by_name, nm)
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
        # 1) What DataGolf is offering right now, and how many carry a real book
        #    price (record() skips any matchup with no book line -> am is None).
        offered = {}
        for market, mk_tag, slots in _MARKETS:
            data = datagolf_api.matchups(tour, market)
            ml = (data or {}).get("match_list") if isinstance(data, dict) else None
            tot = priced = 0
            if isinstance(ml, list):
                for m in ml:
                    if not isinstance(m, dict):
                        continue
                    tot += 1
                    ob = m.get("odds") or {}
                    fav = _model_fav_slot(ob, slots)
                    if fav and _dec_to_american(_best_book_decimal(ob, fav)) is not None:
                        priced += 1
            offered[market] = {"offered": tot, "with_book_price": priced,
                               "round_num": (data or {}).get("round_num") if isinstance(data, dict) else None,
                               "event_name": (data or {}).get("event_name") if isinstance(data, dict) else None}
        out["offered_now"] = offered

        # 2) The board we grade against
        import golf_provider
        board = golf_provider.get_board(tour)
        ev = (board or {}).get("event") or {}
        players = (board or {}).get("players") or []
        by_name = _board_index(players)
        out["board"] = {"event_round": ev.get("round"), "is_complete": ev.get("is_complete"),
                        "players": len(players)}

        # 3) Tracked rows + WHY each pending one hasn't settled
        with SessionLocal() as db:
            from models import GolfMatchupPick
            rows = db.query(GolfMatchupPick).all()
            out["tracked_total"] = len(rows)
            out["settled"] = sum(1 for r in rows if r.settled)
            out["pending"] = sum(1 for r in rows if not r.settled)
            out["wins"] = sum(1 for r in rows if r.result == "win")
            out["losses"] = sum(1 for r in rows if r.result == "loss")
            out["pushes"] = sum(1 for r in rows if r.result == "push")
            reasons = {"name_not_on_board": 0, "round_not_final": 0,
                       "score_missing": 0, "gradeable_now": 0}
            stuck = []
            for mp in rows:
                if mp.settled:
                    continue
                present = [getattr(mp, s) for s in ("p1", "p2", "p3") if getattr(mp, s)]
                why = None
                for nm in present:
                    bp = _lookup(by_name, nm)
                    if bp is None:
                        why = "name_not_on_board"
                        break
                    if not _round_final(board, bp, mp.round_num):
                        why = "round_not_final"
                        break
                    if _round_score(bp, mp.round_num) is None:
                        why = "score_missing"
                        break
                why = why or "gradeable_now"
                reasons[why] += 1
                if len(stuck) < 6 and why != "gradeable_now":
                    miss = [nm for nm in present if _lookup(by_name, nm) is None]
                    stuck.append({"event": mp.event, "round": mp.round_num,
                                  "players": present, "missing_on_board": miss,
                                  "why": why})
            out["pending_reasons"] = reasons
            out["pending_samples"] = stuck
            # Audit settled matchups: show the scores ACTUALLY compared, to catch
            # a degenerate scorer (everyone reading the same value -> false push).
            audit = []
            for mp in [r for r in rows if r.settled][:8]:
                present = [(s, getattr(mp, s)) for s in ("p1", "p2", "p3") if getattr(mp, s)]
                detail = []
                for slot, nm in present:
                    bp = _lookup(by_name, nm)
                    detail.append({"name": nm, "found": bp is not None,
                                   "rounds": (bp or {}).get("rounds"),
                                   "holes": (bp or {}).get("holes"),
                                   "score": _round_score(bp, mp.round_num) if bp else None})
                audit.append({"round": mp.round_num, "result": mp.result,
                              "fav": mp.fav_slot, "scores": detail})
            out["settle_audit"] = audit
            out["recent"] = [
                {"event": r.event, "round": r.round_num, "fav": r.fav_name,
                 "odds": r.taken_odds, "result": r.result or "pending"}
                for r in sorted(rows, key=lambda r: r.id, reverse=True)[:8]]
    except Exception as e:
        import traceback
        out["error"] = str(e)
        out["trace"] = traceback.format_exc()[-400:]
    return out
