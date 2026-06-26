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

_edge_col_ready = False


def _ensure_edge_column():
    """Make sure golf_matchup_picks.edge exists before we query the table. The
    model defines the column, so if a deploy shipped the model without the db.py
    migration, every ORM query on this table would error ('no such column: edge')
    and silently break grading. Adding it here (idempotent) decouples grading from
    deploy order so a missing migration can't stop win/loss backfilling."""
    global _edge_col_ready
    if _edge_col_ready:
        return
    try:
        from db import engine
        from sqlalchemy import text
        with engine.begin() as conn:
            for col in ("edge REAL", "s1 REAL", "s2 REAL", "s3 REAL"):
                try:
                    conn.execute(text(f"ALTER TABLE golf_matchup_picks ADD COLUMN {col}"))
                except Exception:
                    pass  # already there
        _edge_col_ready = True
    except Exception:
        pass

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
        # Edge = model% - market% on the favorite, computed exactly like the live
        # board (implied prob off the DataGolf decimal vs the best book decimal),
        # so the high-edge subset record lines up with what users see on the board.
        edge_val = None
        try:
            md = float((odds_block.get("datagolf") or {}).get(fav))
            bd = _best_book_decimal(odds_block, fav)
            if md and md > 0 and bd and bd > 0:
                edge_val = round(100.0 / md - 100.0 / bd, 1)
        except (TypeError, ValueError):
            edge_val = None
        idtag = "_".join(ids[s] for s in slots)
        ref = f"{evtag}-r{rnd}-{mk_tag}-{idtag}"[:64]
        if db.query(GolfMatchupPick).filter_by(ref=ref).first():
            continue
        db.add(GolfMatchupPick(
            ref=ref, tour=tour, event=event[:80], round_num=rnd,
            p1=names.get("p1", "")[:48], p2=names.get("p2", "")[:48],
            p3=names.get("p3", "")[:48],
            fav_id=ids[fav][:12], fav_name=names[fav][:48], fav_slot=fav,
            taken_odds=am, edge=edge_val,
            recorded_date=dt.datetime.utcnow(), settled=False))
        added += 1
    return added


def record(tour="pga"):
    """Fetch current 3-balls AND 2-ball round matchups; store any not already
    tracked. Returns # added across both markets."""
    _ensure_edge_column()
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


def record_summary(edge_threshold=3.0, days=30):
    """Full settled-matchup record over the last `days`, PLUS the high-edge subset
    (favorite's edge >= threshold pts at record time). The subset is reported
    ALONGSIDE the full record, never in place of it.

    The full record deliberately selects ONLY taken_odds + result (never `edge`),
    so it still returns the true record on a database where the `edge` column
    hasn't been migrated in yet. The high-edge subset, which does need `edge`, is
    computed in a separate guarded query that simply yields an empty subset if the
    column isn't there. Edge is only stored going forward, so the subset starts
    empty and fills in as new matchups settle."""
    _ensure_edge_column()
    from models import GolfMatchupPick
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=days)
    full = {"w": 0, "l": 0, "units": 0.0}
    hi = {"w": 0, "l": 0, "units": 0.0, "threshold": edge_threshold}

    def _profit(odds, won):
        prof = ((odds / 100.0) if odds and odds > 0
                else (100.0 / (-odds)) if odds else 0.0)
        return prof if won else -1.0

    try:
        with SessionLocal() as db:
            rows = db.query(GolfMatchupPick.taken_odds, GolfMatchupPick.result).filter(
                GolfMatchupPick.settled == True,            # noqa: E712
                GolfMatchupPick.recorded_date >= cutoff).all()
        for odds, result in rows:
            if result not in ("win", "loss"):
                continue
            won = result == "win"
            full["w" if won else "l"] += 1
            full["units"] += _profit(odds, won)
    except Exception:
        pass

    try:
        with SessionLocal() as db:
            erows = db.query(GolfMatchupPick.taken_odds, GolfMatchupPick.result,
                             GolfMatchupPick.edge).filter(
                GolfMatchupPick.settled == True,            # noqa: E712
                GolfMatchupPick.recorded_date >= cutoff,
                GolfMatchupPick.edge != None,               # noqa: E711
                GolfMatchupPick.edge >= edge_threshold).all()
        for odds, result, _edge in erows:
            if result not in ("win", "loss"):
                continue
            won = result == "win"
            hi["w" if won else "l"] += 1
            hi["units"] += _profit(odds, won)
    except Exception:
        pass  # `edge` column not migrated yet -> subset just stays empty

    for d in (full, hi):
        d["units"] = round(d["units"], 2)
        tot = d["w"] + d["l"]
        d["record"] = f"{d['w']}-{d['l']}"
        d["win_pct"] = round(100 * d["w"] / tot, 1) if tot else None
    return {"full": full, "high_edge": hi}


def _round_score(bp, rnum):
    """Gross strokes for round `rnum`, or None if that round isn't completed yet.
    ESPN reports 0 for a round not played (and can briefly show a partial running
    total mid-round). A real 18-hole score is ~58-92, so anything under 55 is
    treated as 'not scored yet'. This is the fix for the false-push epidemic:
    grading used to run while every player's round value was still 0, tying them
    all into a push."""
    rounds = (bp or {}).get("rounds") or []
    idx = rnum - 1
    if 0 <= idx < len(rounds):
        try:
            v = float(rounds[idx])
        except (TypeError, ValueError):
            return None
        if v >= 55:
            return v
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


def settle(tour="pga", board=None, stale_days=6):
    """Grade tracked matchups. As soon as a player's round goes final on the
    board, snapshot that player's score onto the pick (s1/s2/s3) — so grading no
    longer depends on the board still showing them next time. Once all of a
    matchup's players have a stored score, grade it (lowest wins; dead heat =
    push). Matchups that can't be completed days after the event are voided so
    nothing hangs pending forever. Returns # newly settled."""
    _ensure_edge_column()
    if board is None:
        import golf_provider
        board = golf_provider.get_board(tour)
    players = (board or {}).get("players") or []
    by_name = _board_index(players)
    slots_attr = ("p1", "p2", "p3")
    score_attr = {"p1": "s1", "p2": "s2", "p3": "s3"}
    settled = 0
    with SessionLocal() as db:
        from models import GolfMatchupPick, PickResult
        pend = db.query(GolfMatchupPick).filter_by(tour=tour, settled=False).all()
        for mp in pend:
            try:
                present = [s for s in slots_attr if getattr(mp, s)]
                # 1) snapshot any newly-final scores from the current board
                for slot in present:
                    if getattr(mp, score_attr[slot]) is not None:
                        continue                       # already captured
                    bp = _lookup(by_name, getattr(mp, slot))
                    if bp is not None and _round_final(board, bp, mp.round_num):
                        sc = _round_score(bp, mp.round_num)
                        if sc is not None:
                            setattr(mp, score_attr[slot], sc)
                # 2) grade when every slot has a stored score
                stored = {slot: getattr(mp, score_attr[slot]) for slot in present}
                if all(v is not None for v in stored.values()):
                    low = min(stored.values())
                    winners = [s for s, v in stored.items() if v == low]
                    if len(winners) != 1:
                        mp.settled, mp.result = True, "push"
                        mp.settled_date = dt.datetime.utcnow()
                        settled += 1
                        continue
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
                    continue
                # 3) void truly-stale matchups (event long over, never completable)
                age_days = (dt.datetime.utcnow() - (mp.recorded_date or dt.datetime.utcnow())).days
                if age_days >= stale_days:
                    mp.settled, mp.result = True, "push"
                    mp.settled_date = dt.datetime.utcnow()
                    settled += 1
            except Exception:
                continue                               # one bad row never aborts the batch
        if settled:
            db.commit()
    return settled


def diag(tour="pga"):
    _ensure_edge_column()
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
            # P&L by market — does a 51% blended rate actually profit? Each market
            # has a different breakeven (a -120 two-ball needs 54.5%, a +140
            # three-ball only 41.7%), so the blended rate alone can mislead.
            pnl = {}
            for r in rows:
                if not r.settled or r.result == "push":
                    continue
                mkt = "3_ball" if r.p3 else "2_ball"
                bk = pnl.setdefault(mkt, {"w": 0, "l": 0, "units": 0.0, "odds_sum": 0, "n": 0})
                if r.result == "win":
                    bk["w"] += 1
                    if r.taken_odds:
                        bk["units"] += (r.taken_odds / 100.0) if r.taken_odds > 0 else (100.0 / (-r.taken_odds))
                elif r.result == "loss":
                    bk["l"] += 1
                    bk["units"] -= 1.0
                if r.taken_odds:
                    bk["odds_sum"] += r.taken_odds
                    bk["n"] += 1
            for mkt, bk in pnl.items():
                tot = bk["w"] + bk["l"]
                ao = round(bk["odds_sum"] / bk["n"]) if bk["n"] else None
                bk["record"] = f"{bk['w']}-{bk['l']}"
                bk["win_pct"] = round(100 * bk["w"] / tot, 1) if tot else None
                bk["avg_odds"] = ao
                bk["breakeven_pct"] = (round(100 * 100 / (ao + 100), 1) if ao and ao > 0
                                       else round(100 * (-ao) / (-ao + 100), 1) if ao else None)
                bk["units"] = round(bk["units"], 2)
                del bk["odds_sum"], bk["n"]
            out["pnl_by_market"] = pnl
            out["recent"] = [
                {"event": r.event, "round": r.round_num, "fav": r.fav_name,
                 "odds": r.taken_odds, "result": r.result or "pending"}
                for r in sorted(rows, key=lambda r: r.id, reverse=True)[:8]]
    except Exception as e:
        import traceback
        out["error"] = str(e)
        out["trace"] = traceback.format_exc()[-400:]
    return out
