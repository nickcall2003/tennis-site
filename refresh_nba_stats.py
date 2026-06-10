"""
refresh_nba_stats.py
--------------------
Pulls team advanced stats (Offensive/Defensive Rating, Net, Pace) from the NBA
stats API via the nba_api library and writes nba_stats.json for the NBA model.

IMPORTANT: nba_api hits stats.nba.com directly, which rate-limits / IP-bans
aggressive callers. This script makes ONE request, so it's safe -- but run it
OFFLINE on a schedule (it must never be on the web request path). Commit the
resulting nba_stats.json; the live server only reads that file.

Setup:  pip install nba_api pandas
Run:    python refresh_nba_stats.py
Season: NBA seasons are "YYYY-YY"; they end in June, so before October we use
        the season that started the previous year. Override with NBA_SEASON.
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


def _season() -> str:
    today = dt.date.today()
    start = today.year if today.month >= 10 else today.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def fetch(season):
    from nba_api.stats.endpoints import leaguedashteamstats
    res = leaguedashteamstats.LeagueDashTeamStats(
        measure_type_detailed_defense="Advanced",
        per_mode_detailed="PerGame",
        season=season,
        timeout=60,
    )
    return res.get_data_frames()[0]


def build(season=None) -> dict:
    season = season or os.environ.get("NBA_SEASON") or _season()
    df = fetch(season)
    rows = df.to_dict("records")
    teams = {}
    offs, paces = [], []
    for r in rows:
        name = r.get("TEAM_NAME")
        off = r.get("OFF_RATING")
        deff = r.get("DEF_RATING")
        net = r.get("NET_RATING")
        pace = r.get("PACE")
        if not name or off is None or deff is None:
            continue
        if net is None:
            net = off - deff
        teams[_norm(name)] = {"name": name, "off": off, "def": deff,
                              "net": net, "pace": pace}
        offs.append(off)
        if pace:
            paces.append(pace)
    avg_eff = round(sum(offs) / len(offs), 2) if offs else 114.0
    avg_pace = round(sum(paces) / len(paces), 2) if paces else 99.5
    data = {"season": season, "updated": dt.datetime.utcnow().isoformat() + "Z",
            "avg_eff": avg_eff, "pace": avg_pace, "teams": teams}
    with open(os.environ.get("NBA_STATS_PATH", "nba_stats.json"), "w") as f:
        json.dump(data, f, indent=2)
    print(f"[nba] wrote {len(teams)} teams for {season} "
          f"(avg_eff={avg_eff}, pace={avg_pace})")
    # sample for verification
    for k in list(teams)[:5]:
        t = teams[k]
        print(f"   {t['name']:24} OFF={t['off']:.1f} DEF={t['def']:.1f} "
              f"NET={t['net']:+.1f} PACE={t['pace']}")
    return data


if __name__ == "__main__":
    build()
