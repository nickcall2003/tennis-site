"""
refresh_nfl_stats.py
--------------------
Computes team offensive / defensive EPA-per-play from nflfastR play-by-play
(via the nfl_data_py library) and writes nfl_stats.json for the NFL model.

EPA/play is the single most predictive public team-strength signal in the NFL.
We aggregate scrimmage plays: a team's offense EPA/play, and the EPA/play its
defense ALLOWS. net = off - def_allowed.

Run OFFLINE (pulls a full season of play-by-play; heavy) and commit the JSON.
Setup:  pip install nfl_data_py pandas
Run:    python refresh_nfl_stats.py
Season: NFL seasons run Sep-Feb and are labeled by the start year; before August
        the latest completed season is last year. Override with NFL_SEASON.
"""
from __future__ import annotations
import os
import json
import datetime as dt
import unicodedata


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def _season() -> int:
    today = dt.date.today()
    return today.year if today.month >= 8 else today.year - 1


def build(year=None) -> dict:
    import nfl_data_py as nfl
    year = int(year or os.environ.get("NFL_SEASON") or _season())
    pbp = nfl.import_pbp_data([year], columns=["posteam", "defteam", "epa", "play_type"])
    pbp = pbp[pbp["epa"].notna()]
    pbp = pbp[pbp["play_type"].isin(["pass", "run"])]

    off = pbp.groupby("posteam")["epa"].mean()
    deff = pbp.groupby("defteam")["epa"].mean()   # EPA the defense ALLOWS
    lg_off = float(off.mean())
    lg_def = float(deff.mean())

    teams = {}
    for team in off.index:
        if not team or str(team) == "nan":
            continue
        o = float(off[team])
        d = float(deff.get(team, lg_def))
        teams[_norm(team)] = {"name": str(team), "off_epa": round(o, 4),
                              "def_epa": round(d, 4), "net_epa": round(o - d, 4)}

    data = {"season": year, "updated": dt.datetime.utcnow().isoformat() + "Z",
            "lg_off": round(lg_off, 4), "lg_def": round(lg_def, 4), "teams": teams}
    with open(os.environ.get("NFL_STATS_PATH", "nfl_stats.json"), "w") as f:
        json.dump(data, f, indent=2)
    print(f"[nfl] wrote {len(teams)} teams for {year} "
          f"(lg_off={lg_off:.3f}, lg_def={lg_def:.3f})")
    ranked = sorted(teams.values(), key=lambda t: t["net_epa"], reverse=True)
    for t in ranked[:5]:
        print(f"   {t['name']:5} off={t['off_epa']:+.3f} def={t['def_epa']:+.3f} "
              f"net={t['net_epa']:+.3f}")
    return data


if __name__ == "__main__":
    build()
