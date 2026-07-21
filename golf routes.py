"""
golf_routes.py — all /api/golf/* endpoints, extracted from main.py so the main
file stays small enough to edit on a phone.

Wiring (already done in main.py):
    from golf_routes import router as _golf_router
    app.include_router(_golf_router)

Everything here is self-contained: the projection/edge caches and the
decimal->American odds helper moved over with the routes. Provider modules
(golf_provider, datagolf_api, golf_model, golf_tracker, golf_weather) are
imported lazily inside each handler, same as before.
"""
import re
import time
import datetime as dt

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


_golf_proj_cache = {}

_golf_edge_cache = {}

def _dec_to_american(d):
    try:
        d = float(d)
    except (TypeError, ValueError):
        return None
    if d <= 1.0:
        return None
    return int(round((d - 1.0) * 100)) if d >= 2.0 else int(round(-100.0 / (d - 1.0)))


@router.get("/api/golf/edge")
def golf_edge(tour: str = "pga"):
    c = _golf_edge_cache.get(tour)
    if c and time.time() - c[0] < 60:
        return JSONResponse(c[1], headers={"Cache-Control": "no-store"})
    import datagolf_api, golf_provider
    out = {"ready": False, "tour": tour}
    try:
        if not datagolf_api.enabled():
            out["reason"] = "no_datagolf_key"
        else:
            o = datagolf_api.outrights(tour, "win")
            players = (o or {}).get("players") or {}
            if not players:
                out["reason"] = "no_market"
            else:
                # map to the board (if any) for a clickable id + score
                board = golf_provider.get_board(tour)
                bmap = {datagolf_api._norm(p.get("name") or ""): p
                        for p in (board.get("players") or [])}
                rows = []
                for nm, m in players.items():
                    md, bd = m.get("model_dec"), m.get("book_dec")
                    if not md or not bd:
                        continue
                    model_p = round(100.0 / md, 1)
                    mkt_p = round(100.0 / bd, 1)
                    bp = bmap.get(nm) or {}
                    rows.append({"id": bp.get("id", ""), "name": m["name"],
                                 "total": bp.get("total", ""), "model": model_p,
                                 "market": mkt_p, "edge": round(model_p - mkt_p, 1),
                                 "american": _dec_to_american(bd), "book": m.get("book")})
                rows.sort(key=lambda x: -x["edge"])
                ev = board.get("event") or {}
                out = {"ready": True, "tour": tour, "source": "datagolf",
                       "event": (o or {}).get("event") or ev.get("name"),
                       "market": "win", "matched": len(rows),
                       "pre": not ev.get("is_live"), "rows": rows[:50]}
    except Exception as e:
        out = {"ready": False, "tour": tour, "reason": "error", "error": str(e)}
    _golf_edge_cache[tour] = (time.time(), out)
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


def _golf_pretourney_matchup(board, id_list, tour="pga"):
    """Pre-tournament 3-ball when there's no live scoring to simulate: price each
    selected player's chance to finish best of the group from DataGolf's model
    win probabilities (Harville normalization). Returns None if DataGolf isn't
    configured or doesn't have all the picked players."""
    import datagolf_api
    if not datagolf_api.enabled():
        return None
    by_id = {p["id"]: p for p in (board.get("players") or [])}
    sel = [by_id[i] for i in id_list if i in by_id][:3]
    if len(sel) < 2:
        return None
    pred = datagolf_api.pre_tournament(tour, "fit")  # course history & fit, not neutral
    mkt = (pred or {}).get("players") or {}
    if not mkt:
        return None
    ws = []
    for p in sel:
        m = mkt.get(datagolf_api._norm(p["name"]))
        if not m or m.get("win") is None:
            return None          # need every picked player in the DataGolf field
        ws.append(max(float(m["win"]), 0.0001))
    tot = sum(ws) or 1.0
    ev = board.get("event") or {}
    out = [{"id": p["id"], "name": p["name"], "pos": p.get("pos"),
            "total": p.get("total"), "prob": round(100 * w / tot, 1)}
           for p, w in zip(sel, ws)]
    out.sort(key=lambda x: -x["prob"])
    return {"ready": True, "scope": "pretourney", "source": "datagolf",
            "event": ev.get("name"), "round": ev.get("round"), "players": out}


@router.get("/api/golf/matchup")
def golf_matchup(tour: str = "pga", ids: str = "", scope: str = "tournament"):
    import golf_provider, golf_model
    id_list = [x for x in ids.split(",") if x]
    if len(id_list) < 2:
        return JSONResponse({"ready": False, "reason": "need_2"})
    board = golf_provider.get_board(tour)
    res = golf_model.matchup(board, id_list, scope=scope)
    # Pre-tournament (no live scoring): fall back to DataGolf model probabilities.
    if (not res.get("ready")) and res.get("reason") == "no_field":
        alt = _golf_pretourney_matchup(board, id_list, tour=tour)
        if alt:
            res = alt
    return JSONResponse(res, headers={"Cache-Control": "no-store"})


@router.get("/api/golf/projections")
def golf_projections(tour: str = "pga"):
    import golf_provider
    c = _golf_proj_cache.get(tour)
    if c and time.time() - c[0] < 60:
        return JSONResponse(c[1], headers={"Cache-Control": "no-store"})
    try:
        import golf_model
        board = golf_provider.get_board(tour)
        data = golf_model.project(board)
        # Pre-tournament (no live scoring): fill projections from DataGolf's model.
        if (not data.get("ready")) and data.get("reason") == "no_field":
            import datagolf_api
            if datagolf_api.enabled():
                pb = datagolf_api.pre_tournament(tour, "baseline")
                pf = datagolf_api.pre_tournament(tour, "fit")
                mb = (pb or {}).get("players") or {}
                mf = (pf or {}).get("players") or {}
                # is the course-fit model actually different from baseline?
                has_fit = bool(mf) and any(
                    (mf.get(k, {}).get("win") != mb.get(k, {}).get("win"))
                    for k in list(mb.keys())[:60])
                if mb:
                    rows = []
                    for p in (board.get("players") or []):
                        key = datagolf_api._norm(p["name"])
                        b = mb.get(key)
                        if not b or b.get("win") is None:
                            continue
                        f = mf.get(key) or {}
                        base = {"win": b.get("win"), "top5": b.get("top5"),
                                "top10": b.get("top10"), "top20": b.get("top20"),
                                "make_cut": b.get("make_cut")}
                        row = {"id": p["id"], "name": p["name"],
                               "pos": p.get("pos"), "total": p.get("total"),
                               "win": base["win"], "top5": base["top5"],
                               "top10": base["top10"], "top20": base["top20"],
                               "make_cut": base["make_cut"], "base": base}
                        fsrc = f if (has_fit and f and f.get("win") is not None) else base
                        fnum, flab = golf_model.estimate_finish(
                            fsrc.get("win"), fsrc.get("top5"), fsrc.get("top10"), fsrc.get("top20"),
                            fsrc.get("make_cut"), len(board.get("players") or []))
                        if flab:
                            row["proj_finish"] = flab
                            row["proj_finish_num"] = fnum
                        if has_fit and f:
                            row["fit"] = {"win": f.get("win"), "top5": f.get("top5"),
                                          "top10": f.get("top10"), "top20": f.get("top20"),
                                          "make_cut": f.get("make_cut")}
                        rows.append(row)
                    if rows:
                        rows.sort(key=lambda r: (-(r.get("win") or 0),
                                                 -(r.get("top5") or 0)))
                        ev = board.get("event") or {}
                        data = {"ready": True, "scope": "pretourney",
                                "source": "datagolf", "pre_cut": True,
                                "has_fit": has_fit,
                                "event": ev.get("name") or (pb or {}).get("event"),
                                "field": len(rows), "projections": rows}
        data["tour"] = tour
        _golf_proj_cache[tour] = (time.time(), data)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"ready": False, "error": str(e)})


@router.get("/api/golf/dg-diag")
def golf_dg_diag(tour: str = "pga"):
    """Confirms the DataGolf key works and shows a few parsed players so the
    field mapping can be verified against a real response."""
    try:
        import datagolf_api
        return JSONResponse(datagolf_api.diag(tour),
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"enabled": False, "error": str(e)})


@router.get("/api/golf/dg-matchups-diag")
def golf_dg_matchups_diag(tour: str = "pga", market: str = "3_balls"):
    """Shows the raw structure of DataGolf's offered matchups (top-level keys +
    the first match) so the matchup-tracker parser can be built against the real
    field names. Try market=3_balls, then tournament_matchups if empty."""
    try:
        import datagolf_api
        data = datagolf_api.matchups(tour, market)
        if data is None:
            return JSONResponse({"enabled": datagolf_api.enabled(),
                                 "note": "no data (not enabled, or feed empty)"})
        out = {"market_requested": market}
        if isinstance(data, dict):
            out["top_keys"] = list(data.keys())
            out["event"] = data.get("event_name")
            out["round"] = data.get("round_num")
            out["market"] = data.get("market")
            ml = data.get("match_list")
            out["match_list_type"] = type(ml).__name__
            if isinstance(ml, list):
                out["count"] = len(ml)
                out["first"] = ml[0] if ml else None
            elif isinstance(ml, str):
                out["match_list_note"] = ml      # DataGolf returns a note when none offered
        elif isinstance(data, list):
            out["top_keys"] = "list"
            out["count"] = len(data)
            out["first"] = data[0] if data else None
        return JSONResponse(out, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@router.get("/api/golf/weather")
def golf_weather(tour: str = "pga"):
    """Tournament weather (free, via Open-Meteo). Geocodes the venue city, then
    returns a per-day forecast across the event dates. Wind is the golf driver."""
    try:
        import golf_provider, golf_weather
        board = golf_provider.get_board(tour)
        ev = (board or {}).get("event") or {}
        if not ev:
            return JSONResponse({"ready": False, "reason": "no_event"})
        start = (ev.get("start") or "")[:10] or None
        end = (ev.get("end") or "")[:10] or None
        # Open-Meteo's forecast endpoint only covers today forward; clamp a start
        # that's already in the past so we still get the remaining rounds.
        today = dt.date.today().isoformat()
        if start and start < today:
            start = today
        if end and end < today:
            end = today
        w = golf_weather.tournament_weather(ev.get("city"), ev.get("state"),
                                       start, end, ev.get("venue"))
        w["event"] = ev.get("name")
        w["tour"] = tour
        return JSONResponse(w, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"ready": False, "reason": "error", "error": str(e)})


@router.get("/api/golf/weather-diag")
def golf_weather_diag(tour: str = "pga"):
    try:
        import golf_provider
        ev = (golf_provider.get_board(tour) or {}).get("event") or {}
        return JSONResponse({"event": ev.get("name"), "venue": ev.get("venue"),
                             "city": ev.get("city"), "state": ev.get("state"),
                             "start": ev.get("start"), "end": ev.get("end")},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@router.get("/api/golf/matchup-board")
def golf_matchup_board(tour: str = "pga"):
    """Full value board of every offered matchup (model price vs best book +
    edge per player), for the rebuilt Matchups tab."""
    try:
        import datagolf_api
        if not datagolf_api.enabled():
            return JSONResponse({"ready": False, "reason": "no_datagolf_key"})
        b = datagolf_api.matchup_board(tour)
        if not b or not b.get("groups"):
            return JSONResponse({"ready": False, "reason": "no_market"})
        # attach each player's live score + decide a completed-matchup result
        import golf_provider
        from golf_tracker import _round_score as _gscore, _board_index as _gidx, _lookup as _glookup
        board = golf_provider.get_board(tour)
        try:
            import golf_tracker
            golf_tracker.settle(tour, board=board)   # snapshot/grade while board is live
            # self-heal: if matchups are still pending (board may have rolled to
            # the next event), grade them against the last few finished days too.
            with SessionLocal() as _db:
                from models import GolfMatchupPick
                _pending = _db.query(GolfMatchupPick).filter_by(tour=tour, settled=False).count()
            if _pending:
                _today = dt.date.today()
                for _i in (1, 2, 3):
                    _d = _today - dt.timedelta(days=_i)
                    try:
                        _pb = golf_provider.get_board(tour, dates=_d.strftime("%Y%m%d"))
                        golf_tracker.settle(tour, board=_pb, stale_days=999)
                    except Exception:
                        pass
        except Exception as _e:
            print(f"[golf] inline settle skipped: {_e}")
        gidx = _gidx(board.get("players") or [])
        for g in b["groups"]:
            rnum = g.get("round")
            scored = {}
            for pl in g["players"]:
                bp = _glookup(gidx, datagolf_api._norm(pl["name"]))
                if bp:
                    pl["total"] = bp.get("total")
                    pl["thru"] = bp.get("thru")
                    pl["pos"] = bp.get("pos")
                sc = _gscore(bp, rnum) if bp else None
                if sc is not None:
                    scored[pl["name"]] = sc
            # A group is "complete" only once every player has a finished round
            # score (the >=55 guard). Then grade the model favorite: strict low =
            # win, tie for low = push, otherwise loss.
            fav = next((p for p in g["players"] if p.get("fav")), None)
            if rnum and g["players"] and len(scored) == len(g["players"]):
                low = min(scored.values())
                winners = [n for n, v in scored.items() if v == low]
                g["complete"] = True
                if fav is None:
                    g["result"] = None
                elif len(winners) != 1:
                    g["result"] = "push"
                else:
                    g["result"] = "win" if winners[0] == fav["name"] else "loss"
            else:
                g["complete"] = False
                g["result"] = None
        ev = board.get("event") or {}
        b["live"] = bool(ev.get("is_live"))
        # Enrich each player with tournament skill (baseline win%) + course-fit
        # delta (fit model win% minus baseline) so the detail page can explain the
        # edge: quality of player vs whether the venue suits them.
        try:
            base = datagolf_api.pre_tournament(tour, "baseline") or {}
            fitm = datagolf_api.pre_tournament(tour, "fit") or {}
            bpl, fpl = base.get("players") or {}, fitm.get("players") or {}
            for g in b["groups"]:
                for pl in g["players"]:
                    nm = datagolf_api._norm(pl["name"])
                    bm, fm = bpl.get(nm), fpl.get(nm)
                    if bm:
                        pl["win_pct"] = bm.get("win")
                        pl["top20_pct"] = bm.get("top20")
                    if bm and fm and bm.get("win") is not None and fm.get("win") is not None:
                        pl["fit_delta"] = round(fm["win"] - bm["win"], 1)
        except Exception:
            pass
        try:
            from golf_tracker import record_summary
            b["record"] = record_summary()
        except Exception:
            b["record"] = None
        b["ready"] = True
        b["tour"] = tour
        return JSONResponse(b, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"ready": False, "reason": "error", "error": str(e)})


@router.get("/api/golf/dg-outrights-diag")
def golf_dg_outrights_diag(tour: str = "pga", market: str = "win"):
    """Shows the parsed outrights (model + best book per player) so the Edge
    tab's source can be verified against a real response."""
    try:
        import datagolf_api
        o = datagolf_api.outrights(tour, market)
        if not o:
            return JSONResponse({"enabled": datagolf_api.enabled(),
                                 "note": "no data (not enabled or feed empty)"})
        pl = o.get("players") or {}
        sample = list(pl.values())[:5]
        return JSONResponse({"event": o.get("event"), "market": o.get("market"),
                             "players_loaded": len(pl), "sample": sample},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@router.get("/api/golf/tracker-settle")
def golf_tracker_settle(confirm: str = "", tour: str = "pga"):
    """Force a settle pass right now and report how many graded — or the exact
    error + traceback if it throws. The hourly background settle swallows its
    errors, so this surfaces why gradeable matchups aren't settling."""
    if confirm != "yes":
        return JSONResponse({"note": "add ?confirm=yes to force a settle pass", "tour": tour})
    try:
        import golf_tracker
        n = golf_tracker.settle(tour)
        return JSONResponse({"settled_this_pass": n, "tour": tour},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()[-1800:]},
                            headers={"Cache-Control": "no-store"})


@router.get("/api/golf/tracker-recover")
def golf_tracker_recover(confirm: str = "", tour: str = "pga", date: str = "", days: int = 3):
    """Recover ungraded matchups from finished events: re-fetch the leaderboard
    for recent past dates (default the last few days) and grade pending matchups
    against it. Use when matchups washed out because the board rolled to the next
    event before they settled. Add ?confirm=yes (optional &date=YYYY-MM-DD)."""
    if confirm != "yes":
        return JSONResponse({"note": "add ?confirm=yes (optional &date=YYYY-MM-DD or &days=3)", "tour": tour})
    try:
        import golf_provider, golf_tracker
        if date:
            dates_list = [dt.date.fromisoformat(date)]
        else:
            today = dt.date.today()
            dates_list = [today - dt.timedelta(days=i) for i in range(1, max(1, days) + 1)]
        passes, total = [], 0
        for d in dates_list:
            board = golf_provider.get_board(tour, dates=d.strftime("%Y%m%d"))
            ev = (board or {}).get("event") or {}
            n = golf_tracker.settle(tour, board=board, stale_days=999)  # don't void during recovery
            passes.append({"date": d.isoformat(),
                           "event": ev.get("name") if isinstance(ev, dict) else None,
                           "complete": ev.get("is_complete") if isinstance(ev, dict) else None,
                           "players_on_board": len((board or {}).get("players") or []),
                           "settled": n})
            total += n
        return JSONResponse({"tour": tour, "recovered_total": total, "passes": passes},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()[-1500:]},
                            headers={"Cache-Control": "no-store"})


@router.get("/api/golf/tracker-regrade")
def golf_tracker_regrade(confirm: str = "", tour: str = "pga", days: int = 30, event: str = ""):
    """Clean re-grade: wipe stored scores + results for tracked matchups, then
    re-settle each against ITS OWN event's finished leaderboard (fetched by date,
    oldest->newest). The event guard in settle() keeps a player who is in this
    week's field too from contaminating an older matchup. Optionally limit to one
    event with &event=<substring> (e.g. event=open for the U.S. Open).
    Add ?confirm=yes (optional &days=30 &event= &tour=pga)."""
    if confirm != "yes":
        return JSONResponse({"note": "add ?confirm=yes; optional &days=30 &event=<substr> &tour=pga",
                             "example": "?confirm=yes&tour=pga&event=open"})
    try:
        import golf_provider, golf_tracker
        from models import GolfMatchupPick, PickResult

        def _evnorm(s):
            return "".join(ch for ch in (s or "").lower() if ch.isalnum())
        want = _evnorm(event)

        # 1) wipe results + stored scores (only the targeted event if given)
        wiped, refs = 0, []
        with SessionLocal() as db:
            for mp in db.query(GolfMatchupPick).filter_by(tour=tour).all():
                if want and want not in _evnorm(mp.event):
                    continue
                mp.settled, mp.result, mp.settled_date = False, None, None
                mp.s1 = mp.s2 = mp.s3 = None
                refs.append(mp.ref)
                wiped += 1
            if want:
                for i in range(0, len(refs), 400):
                    db.query(PickResult).filter(
                        PickResult.sport == "golf",
                        PickResult.ref.in_(refs[i:i + 400])).delete(synchronize_session=False)
            else:
                db.query(PickResult).filter(PickResult.sport == "golf").delete(synchronize_session=False)
            db.commit()

        # 2) fetch ONLY the dates these matchups actually came from (plus today),
        #    deduped — fetching any date during an event returns that event's full
        #    final leaderboard, so this is a few calls, not a 31-day sweep that
        #    times out and leaves everything wiped.
        dates_needed = set()
        with SessionLocal() as db:
            for mp in db.query(GolfMatchupPick).filter_by(tour=tour).all():
                if want and want not in _evnorm(mp.event):
                    continue
                if mp.recorded_date:
                    base = mp.recorded_date.date()
                    for off in range(0, 5):       # round may be played a few days after offer
                        dates_needed.add(base + dt.timedelta(days=off))
        today = dt.date.today()
        dates_needed.add(today)
        dates_list = sorted(d for d in dates_needed if d <= today)

        passes = []
        for d in dates_list:
            try:
                board = golf_provider.get_board(tour, dates=d.strftime("%Y%m%d"))
                ev = (board or {}).get("event") or {}
                if want and want not in _evnorm(ev.get("name")):
                    continue
                n = golf_tracker.settle(tour, board=board, stale_days=10 ** 9)
                if n:
                    passes.append({"date": d.isoformat(), "event": ev.get("name"), "settled": n})
            except Exception:
                continue
        # final pass against the live current board for the in-progress event
        try:
            cur = golf_provider.get_board(tour)
            n = golf_tracker.settle(tour, board=cur, stale_days=10 ** 9)
            if n:
                ev = (cur or {}).get("event") or {}
                passes.append({"date": today.isoformat(), "event": ev.get("name"),
                               "settled": n, "live": True})
        except Exception:
            pass

        # 3) report the clean record
        with SessionLocal() as db:
            rows = db.query(GolfMatchupPick).filter_by(tour=tour).all()
            rec = {"wins": sum(1 for r in rows if r.result == "win"),
                   "losses": sum(1 for r in rows if r.result == "loss"),
                   "pushes": sum(1 for r in rows if r.result == "push"),
                   "still_pending": sum(1 for r in rows if not r.settled)}
        return JSONResponse({"tour": tour, "wiped": wiped, "passes": passes, "record": rec},
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]},
                            status_code=500)


@router.get("/api/golf/tracker-reset")
def golf_tracker_reset(confirm: str = ""):
    """Wipes golf tracking: deletes golf PickResults and re-arms every tracked
    matchup so the corrected grader re-settles only completed rounds. Use once
    to clear records that settled early. Add ?confirm=yes."""
    if confirm != "yes":
        return JSONResponse({"ok": False, "note": "add ?confirm=yes to reset golf tracking"})
    try:
        from db import SessionLocal
        from models import GolfMatchupPick, PickResult
        with SessionLocal() as db:
            n_pr = db.query(PickResult).filter_by(sport="golf").delete()
            n_mp = 0
            for mp in db.query(GolfMatchupPick).all():
                mp.settled = False
                mp.result = None
                mp.settled_date = None
                n_mp += 1
            db.commit()
        return JSONResponse({"ok": True, "deleted_golf_pickresults": n_pr,
                             "re_armed_matchups": n_mp})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/api/golf/tracker-diag")
def golf_tracker_diag(tour: str = "pga"):
    """Tracked/pending/settled counts + record so the matchup tracker can be
    verified."""
    try:
        import golf_tracker
        return JSONResponse(golf_tracker.diag(tour),
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@router.get("/api/golf/board")
def golf_board(tour: str = "pga"):
    import golf_provider
    b = golf_provider.get_board(tour)
    return JSONResponse(b, headers={"Cache-Control": "no-store"})


@router.get("/api/golf/schedule")
def golf_schedule(tour: str = "pga"):
    import golf_provider
    return golf_provider.get_schedule(tour)


@router.get("/api/golf/raw")
def golf_raw(tour: str = "pga"):
    import golf_provider
    return JSONResponse(golf_provider.raw(tour), headers={"Cache-Control": "no-store"})
