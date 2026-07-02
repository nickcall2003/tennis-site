"""
team_profile_util.py — shared stat builder for team profile pages.

Given a normalized list of a team's completed games, compute the honest,
results-only profile: record, home/away splits, last-10, current streak,
recent-form string, scoring for/against, and the last few games. Handles
draws (soccer) via a per-game res in {"w","d","l"}; for W/L sports pass
games with a "won" bool instead.

Each game dict may carry: won (bool) OR res ("w"/"d"/"l"), home (bool),
opp (str), ms/os (ints, this team's score / opponent's), date (str).
"""


def _res(g):
    if "res" in g:
        return g["res"]
    return "w" if g.get("won") else "l"


def build(out, games, draws=False):
    games = sorted(games, key=lambda x: x.get("date", "") or "")
    n = len(games)
    if not n:
        return out

    def rec(gs):
        w = sum(1 for g in gs if _res(g) == "w")
        d = sum(1 for g in gs if _res(g) == "d")
        l = sum(1 for g in gs if _res(g) == "l")
        return f"{w}-{d}-{l}" if draws else f"{w}-{l}"

    home = [g for g in games if g.get("home")]
    away = [g for g in games if not g.get("home")]
    last10 = games[-10:]
    pf = [g["ms"] for g in games if g.get("ms") is not None]
    pa = [g["os"] for g in games if g.get("os") is not None]

    last = _res(games[-1])
    streak = 0
    for g in reversed(games):
        if _res(g) == last:
            streak += 1
        else:
            break

    out.update({
        "record": rec(games), "games": n,
        "home_record": rec(home), "away_record": rec(away),
        "last10": rec(last10),
        "form": "".join(_res(g).upper() for g in last10),
        "streak": (last.upper() + str(streak)) if streak else None,
        "ppg": round(sum(pf) / len(pf), 1) if pf else None,
        "opp_ppg": round(sum(pa) / len(pa), 1) if pa else None,
        "recent": [{"opp": g.get("opp"), "won": (_res(g) == "w"), "res": _res(g),
                    "home": g.get("home"),
                    "score": (f"{g['ms']}-{g['os']}" if g.get("ms") is not None else None),
                    "date": g.get("date")} for g in reversed(games[-8:])],
    })
    return out
