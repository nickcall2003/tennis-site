"""
golf_model.py — live Monte Carlo tournament projections from ESPN leaderboard data.

A player's scoring rate so far (to-par per hole) is their best in-tournament skill
estimate. Regress it toward the field mean (weighted by holes played), then simulate
the remaining golf as iid normal rounds to produce:
  - project():  win / top-5/10/20, make-cut (cut events, pre-cut), round-leader (live)
  - matchup():  2-ball / 3-ball — P(each selected player is low of the group)

It is purely SCORE-DRIVEN: it learns each player's form from THIS week's scores. It
adapts to the event automatically in three ways:
  * event length: 54 holes for LIV, else 72 (so finish math is correct)
  * cut structure: no-cut limited-field events (Sentry/signature/LIV) skip make-cut
  * scoring spread: the per-round std is CALIBRATED from the week's actual round
    dispersion (a windy US Open setup widens it; a benign desert week tightens it),
    instead of a fixed constant.

What it does NOT model (would need strokes-gained / DataGolf, a later phase):
course-fit, weather, specific course history, or any pre-tournament prior before
scores exist. Pure-python, fast enough for a cached endpoint.
"""
from __future__ import annotations

import math
import os
import random
import statistics

ROUND_STD_DEFAULT = float(os.environ.get("GOLF_ROUND_STD", "2.9"))
ROUND_STD_MIN = float(os.environ.get("GOLF_ROUND_STD_MIN", "2.2"))
ROUND_STD_MAX = float(os.environ.get("GOLF_ROUND_STD_MAX", "3.8"))
REG_K = float(os.environ.get("GOLF_REG_K", "36"))
CUT_RULE = int(os.environ.get("GOLF_CUT_N", "65"))
CUT_FIELD_MIN = int(os.environ.get("GOLF_CUT_FIELD_MIN", "80"))  # below this => treat as no-cut
N_SIMS = int(os.environ.get("GOLF_SIMS", "2000"))


def _event_holes(board):
    return 54 if (board.get("tour") == "liv") else 72


def _holes_played(p, hpr=18):
    nr = p.get("n_rounds") or 0
    if nr <= 0:
        return 0
    h = p.get("holes")
    if h is None or h == 0:
        h = 18
    return (nr - 1) * 18 + h


def _sigma(holes_remaining, round_std):
    if holes_remaining <= 0:
        return 0.0001
    return round_std * math.sqrt(holes_remaining / 18.0)


def _calibrate_round_std(players):
    """Pooled within-player round-to-round dispersion (strokes) ~ scoring spread.
    Excludes partial/in-progress rounds by keeping only plausible full-round totals."""
    resid = []
    for p in players:
        rs = [r for r in (p.get("rounds") or [])
              if isinstance(r, (int, float)) and 58 <= r <= 92]
        if len(rs) >= 2:
            m = statistics.fmean(rs)
            resid.extend(r - m for r in rs)
    if len(resid) >= 40:
        s = statistics.pstdev(resid)
        # pooled residual slightly understates a single round's spread; nudge up
        s *= 1.15
        return max(ROUND_STD_MIN, min(ROUND_STD_MAX, s))
    return ROUND_STD_DEFAULT


def _build_params(board):
    ev = board.get("event") or {}
    players = [p for p in (board.get("players") or []) if p.get("total_num") is not None]
    if not players or ev.get("is_complete"):
        return None

    holes_total = _event_holes(board)
    rates = []
    for p in players:
        hp = _holes_played(p)
        if hp > 0:
            rates.append(p["total_num"] / hp)
    if not rates:
        return None
    field_rate = statistics.fmean(rates)
    round_std = _calibrate_round_std(players)

    active = [p for p in players if p.get("made_cut") is not False]
    if len(active) < 2:
        return None

    field_size = len(players)
    has_cut = field_size >= CUT_FIELD_MIN and board.get("tour") != "liv"
    max_hp = max(_holes_played(p) for p in active)
    pre_cut = has_cut and max_hp < 36
    is_live = bool(ev.get("is_live"))

    out = []
    for p in active:
        hp = _holes_played(p)
        if hp > 0:
            w = hp / (hp + REG_K)
            mh = w * (p["total_num"] / hp) + (1 - w) * field_rate
        else:
            mh = field_rate
        base = p["total_num"]
        hr = holes_total - hp
        holes_in_round = min(18, p.get("holes") or 0)
        rem_r = max(0, 18 - holes_in_round)
        today = p.get("today_num") or 0
        out.append({
            "id": p["id"], "name": p["name"], "pos": p.get("pos"),
            "total": p.get("total"), "total_num": base,
            "mu_f": base + mh * hr, "sig_f": _sigma(hr, round_std),
            "mu_c": base + mh * max(0, 36 - hp), "sig_c": _sigma(max(0, 36 - hp), round_std),
            "mu_r": today + mh * rem_r, "sig_r": _sigma(rem_r, round_std),
        })
    return {"event": ev, "players": out, "pre_cut": pre_cut, "is_live": is_live,
            "has_cut": has_cut, "holes_total": holes_total, "round_std": round(round_std, 2),
            "field_size": field_size}


def project(board, n_sims=N_SIMS):
    P = _build_params(board)
    if not P:
        ev = board.get("event") or {}
        return {"ready": False, "reason": "complete" if ev.get("is_complete") else "no_field"}
    pl = P["players"]
    n = len(pl)
    pre_cut = P["pre_cut"]
    live = P["is_live"]
    g = random.gauss

    win = [0] * n; t5 = [0] * n; t10 = [0] * n; t20 = [0] * n
    made = [0] * n; rlead = [0] * n

    for _ in range(n_sims):
        if pre_cut:
            s36 = [p["mu_c"] + g(0, p["sig_c"]) for p in pl]
            order36 = sorted(range(n), key=lambda i: s36[i])
            cut_val = s36[order36[min(CUT_RULE - 1, n - 1)]]
            in_cut = [s36[i] <= cut_val + 1e-9 for i in range(n)]
            finals = []
            for i, p in enumerate(pl):
                if in_cut[i]:
                    made[i] += 1
                    finals.append(p["mu_f"] + g(0, p["sig_f"]))
                else:
                    finals.append(s36[i] + 99)
        else:
            finals = [p["mu_f"] + g(0, p["sig_f"]) for p in pl]
        order = sorted(range(n), key=lambda i: finals[i])
        win[order[0]] += 1
        for rank, i in enumerate(order[:20]):
            if rank < 5: t5[i] += 1
            if rank < 10: t10[i] += 1
            t20[i] += 1
        if live:
            rs = [p["mu_r"] + g(0, p["sig_r"]) for p in pl]
            rlead[min(range(n), key=lambda i: rs[i])] += 1

    out = []
    for i, p in enumerate(pl):
        row = {"id": p["id"], "name": p["name"], "pos": p["pos"],
               "total": p["total"], "total_num": p["total_num"],
               "win": round(100 * win[i] / n_sims, 1),
               "top5": round(100 * t5[i] / n_sims, 1),
               "top10": round(100 * t10[i] / n_sims, 1),
               "top20": round(100 * t20[i] / n_sims, 1)}
        if pre_cut:
            row["make_cut"] = round(100 * made[i] / n_sims, 1)
        if live:
            row["round_leader"] = round(100 * rlead[i] / n_sims, 1)
        out.append(row)
    out.sort(key=lambda x: (-x["win"], -x["top5"], x["total_num"]))
    ev = P["event"]
    return {"ready": True, "event": ev.get("name"), "round": ev.get("round"),
            "pre_cut": pre_cut, "has_cut": P["has_cut"], "live": live,
            "holes": P["holes_total"], "round_std": P["round_std"],
            "n_sims": n_sims, "field": n, "projections": out}


def matchup(board, ids, scope="tournament", n_sims=4000):
    P = _build_params(board)
    if not P:
        return {"ready": False, "reason": "no_field"}
    idset = [str(i) for i in ids]
    sel = [p for p in P["players"] if p["id"] in idset][:3]
    if len(sel) < 2:
        return {"ready": False, "reason": "need_2_in_field"}
    rnd = scope == "round"
    if rnd and not P["is_live"]:
        scope, rnd = "tournament", False
    g = random.gauss
    k = len(sel)
    win = [0] * k
    for _ in range(n_sims):
        sc = [(p["mu_r"] + g(0, p["sig_r"])) if rnd else (p["mu_f"] + g(0, p["sig_f"])) for p in sel]
        win[min(range(k), key=lambda i: sc[i])] += 1
    out = [{"id": p["id"], "name": p["name"], "pos": p["pos"], "total": p["total"],
            "prob": round(100 * win[i] / n_sims, 1)} for i, p in enumerate(sel)]
    out.sort(key=lambda x: -x["prob"])
    ev = P["event"]
    return {"ready": True, "scope": scope, "event": ev.get("name"),
            "round": ev.get("round"), "n_sims": n_sims, "players": out}
