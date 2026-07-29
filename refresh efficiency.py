#!/usr/bin/env python3
"""
refresh_efficiency.py — rebuild every sport's efficiency ratings file in one run.

WHY THIS EXISTS
Win totals and the live board are only as good as the ratings files behind them
(ncaab_ratings.json, ncaaf_sp.json, nfl_stats.json). Each sport has its own
builder; running three scripts by hand every week is exactly the kind of chore
that gets skipped until a projection quietly falls back to Elo. This runs them
all, reports what succeeded, and — crucially — NEVER lets one sport's failure
stop the others.

WHAT IT BUILDS (and what each needs)
  NCAAB  -> ncaab_ratings.json   Bart Torvik, FREE, no key
  NCAAF  -> ncaaf_sp.json        SP+ via CFBD, needs CFBD_KEY or CFBD_API_KEY
  NFL    -> nfl_stats.json       EPA via nfl_data_py, FREE (heavy: pulls a full
                                 season of play-by-play; run offline)

PREFERENCE ORDER
For NCAAB and NCAAF, a no-key/data-module builder is tried first; the API-key
builder is the fallback. So it does as much as possible with zero setup and only
needs keys for the parts that truly require them.

USAGE
    python refresh_efficiency.py                 # all sports, current season
    python refresh_efficiency.py --only ncaaf    # one sport
    python refresh_efficiency.py --season 2026    # force a season
    python refresh_efficiency.py --quiet          # only print failures

SCHEDULING
Run weekly during each sport's season (they don't overlap much). Out of season a
sport simply returns "no data yet" and is skipped — safe to run year-round.
Commit the resulting *.json (or point *_PATH env vars at /data on Railway).
"""
from __future__ import annotations

import argparse
import sys
import traceback


def _try(label, fn):
    """Run one builder, capture the outcome, never raise."""
    try:
        result = fn()
        # builders return either a dict summary or write-and-print; normalise
        if isinstance(result, dict):
            ok = result.get("ok", True)
            teams = result.get("teams") or len((result.get("teams_map") or {})) or "?"
            if ok:
                return {"sport": label, "ok": True,
                        "teams": result.get("teams", teams),
                        "detail": result.get("path") or result.get("out") or ""}
            return {"sport": label, "ok": False,
                    "error": result.get("error", "builder reported failure")}
        # non-dict (older builders that just write + print) = assume success
        return {"sport": label, "ok": True, "teams": "?", "detail": "built"}
    except SystemExit as e:
        # builders raise SystemExit when a required key is missing
        return {"sport": label, "ok": False, "error": str(e) or "missing API key"}
    except ImportError as e:
        return {"sport": label, "ok": False,
                "error": f"missing dependency: {e} (this builder runs offline only)"}
    except Exception as e:
        return {"sport": label, "ok": False,
                "error": f"{type(e).__name__}: {str(e)[:160]}",
                "trace": traceback.format_exc()[-400:]}


def build_ncaab(season=None):
    """Torvik first (free, no key); CBBD API as fallback."""
    try:
        import ncaab_stats
        r = ncaab_stats.refresh_ratings_file(year=season)
        if r.get("ok"):
            return r
        # Torvik parsed nothing (often just out of season) — try CBBD if keyed
    except Exception:
        pass
    import refresh_cbbd_ratings
    data = refresh_cbbd_ratings.build(season)
    return {"ok": True, "teams": len(data.get("teams", {})),
            "path": refresh_cbbd_ratings.OUT}


def build_ncaaf(season=None):
    """The self-maintaining CFBD SP+ refresher first; plain builder as fallback."""
    try:
        import ncaaf_stats
        r = ncaaf_stats.refresh_sp_file(year=season)
        if r.get("ok"):
            return r
    except Exception:
        pass
    import refresh_cfbd_sp
    data = refresh_cfbd_sp.build(season)
    return {"ok": True, "teams": len(data.get("teams", {})),
            "path": refresh_cfbd_sp.OUT}


def build_nfl(season=None):
    """EPA/play from nfl_data_py. Heavy; offline only."""
    import refresh_nfl_stats
    data = refresh_nfl_stats.build(season)
    return {"ok": True, "teams": len(data.get("teams", {})),
            "path": "nfl_stats.json"}


_BUILDERS = {"ncaab": build_ncaab, "ncaaf": build_ncaaf, "nfl": build_nfl}


def main():
    ap = argparse.ArgumentParser(description="Rebuild all efficiency ratings files.")
    ap.add_argument("--only", choices=list(_BUILDERS), help="build just one sport")
    ap.add_argument("--season", type=int, default=None, help="force a season/year")
    ap.add_argument("--quiet", action="store_true", help="print only failures")
    args = ap.parse_args()

    targets = [args.only] if args.only else list(_BUILDERS)
    results = []
    for sport in targets:
        r = _try(sport, lambda s=sport: _BUILDERS[s](args.season))
        results.append(r)
        if r["ok"]:
            if not args.quiet:
                print(f"  \u2713 {sport:6} {r.get('teams','?')} teams "
                      f"\u2192 {r.get('detail','')}")
        else:
            print(f"  \u2717 {sport:6} FAILED: {r['error']}")

    ok = sum(1 for r in results if r["ok"])
    print(f"\n{ok}/{len(results)} efficiency files rebuilt.")
    failed = [r for r in results if not r["ok"]]
    if failed:
        print("Failures (safe to ignore if that sport is out of season or "
              "you haven't set its API key):")
        for r in failed:
            print(f"  \u2022 {r['sport']}: {r['error']}")
    # exit non-zero only if EVERYTHING failed, so a scheduled job flags a real
    # outage but not a routine out-of-season skip
    return 1 if ok == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
