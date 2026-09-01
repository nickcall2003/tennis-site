"""
ncaaf_provider.py
-----------------
File-backed SP+ lookup + win-probability model for NCAA football, fed by
ncaaf_sp.json (built by refresh_cfbd_sp.py). Mirrors the other file-backed
providers: lazy JSON load, no network on the request path.

Model: SP+ overall ratings are points relative to an average team, so
    expected margin (home - away) = (sp_home - sp_away) + HOME_FIELD
A normal CDF on that margin (college FB scoring SD ~ 16.5 pts) gives the home
win probability; offense/defense ratings vs the national baseline give a total.

If the file is missing or either team can't be matched, predict() returns None
so the caller falls back to its existing win%-Elo prediction.
"""
from __future__ import annotations
import os
import json
import math
import unicodedata

_PATH = (os.environ.get("NCAAF_SP_PATH")
         or ("/data/ncaaf_sp.json" if os.path.exists("/data/ncaaf_sp.json") else "ncaaf_sp.json"))
HOME_FIELD = float(os.environ.get("NCAAF_HOME_FIELD", "2.5"))
MARGIN_SD = float(os.environ.get("NCAAF_MARGIN_SD", "16.5"))
# --- advanced-stats blend -------------------------------------------------
# SP+ is the backbone (it already bakes in transfers + returning production).
# On top of it we add a points adjustment from CFBD advanced stats (EPA/PPA,
# success rate, explosiveness) — the per-play efficiency SP+ compresses into one
# number. Two guards keep this honest:
#   1) It's OFF unless an advanced-stats file is present (built alongside SP+).
#   2) Its weight RAMPS with games played. In Week 1 the season's advanced stats
#      are one-game noise, so the blend is ~0 and we trust SP+; by mid-season the
#      efficiency data is real and gets full weight. Controlled by NCAAF_ADV_*.
_ADV_PATH = (os.environ.get("NCAAF_ADV_PATH")
             or ("/data/ncaaf_adv.json" if os.path.exists("/data/ncaaf_adv.json")
                 else "ncaaf_adv.json"))
ADV_MAX_WEIGHT = float(os.environ.get("NCAAF_ADV_WEIGHT", "0.45"))   # cap on blend
ADV_FULL_GAMES = float(os.environ.get("NCAAF_ADV_FULL_GAMES", "6"))  # games to full weight
# Points-per-unit scalings: PPA (EPA/play) differences are small numbers, so a
# whole-game edge of ~0.15 PPA is worth a lot of points. These convert a
# per-play efficiency edge into an expected-points margin contribution.
ADV_PPA_TO_PTS = float(os.environ.get("NCAAF_ADV_PPA_PTS", "55.0"))
ADV_SUCC_TO_PTS = float(os.environ.get("NCAAF_ADV_SUCC_PTS", "35.0"))
ADV_EXPL_TO_PTS = float(os.environ.get("NCAAF_ADV_EXPL_PTS", "8.0"))
_adv = None
# An FBS team playing an opponent NOT in the SP+ file is almost always facing an
# FCS team. CFBD's SP+ covers only FBS, so an unrated opponent is, in reality,
# weaker than the weakest FBS team (which sits near -33). We assign unrated teams
# this baseline SP+ so FBS-vs-FCS games predict sensibly (big FBS favorite)
# instead of bailing to a win%-only fallback that can favor the FCS side.
# Tunable via NCAAF_FCS_SP.
FCS_SP = float(os.environ.get("NCAAF_FCS_SP", "-38.0"))
# Rough offense/defense split for an unrated FCS team, for the total calc.
FCS_OFF = float(os.environ.get("NCAAF_FCS_OFF", "13.0"))
FCS_DEF = float(os.environ.get("NCAAF_FCS_DEF", "42.0"))
_data = None
_logged_miss = set()

# Known name mismatches between ESPN displayName and CFBD's `team` string.
# Maps norm(espn-side) -> norm(cfbd). Seeded as misses surface in season.
_ALIASES = {
    "appalachianstate": "appstate",
    "appstate": "appalachianstate",
    "olemiss": "mississippi",
    "mississippi": "olemiss",
    "connecticut": "uconn",
    "uconn": "connecticut",
    "louisianamonroe": "ulmonroe",
    "louisianalafayette": "louisiana",
    "sanjosestate": "sanjosestate",
    "hawaii": "hawaii",
    "miamioh": "miamioh",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def _load():
    global _data
    if _data is None:
        try:
            with open(_PATH) as f:
                _data = json.load(f)
        except Exception:
            _data = {"teams": {}, "national": {}}
    return _data


def _load_adv():
    """Load the advanced-stats file: {teams:{norm:{off_ppa,def_ppa,off_success,
    def_success,off_explosive,def_explosive,games}}, national:{...}}. Empty when
    no file — in which case the blend is simply skipped."""
    global _adv
    if _adv is None:
        try:
            with open(_ADV_PATH) as f:
                _adv = json.load(f)
        except Exception:
            _adv = {"teams": {}, "national": {}}
    return _adv


def reload():
    global _data, _adv
    _data = None
    _adv = None
    return _load()


def _adv_lookup(name):
    d = _load_adv()
    teams = d.get("teams") or {}
    if not teams:
        return None
    n = _norm(name)
    if n in teams:
        return teams[n]
    for ak, av in _ALIASES.items():
        if (n == ak or n.startswith(ak)) and av in teams:
            return teams[av]
    best = None
    for k, v in teams.items():
        if n.startswith(k) or k.startswith(n):
            if best is None or len(k) > len(best[0]):
                best = (k, v)
    return best[1] if best else None


def _season_weight(games):
    """0..1 ramp: how much to trust this season's advanced stats given how many
    games have been played. Week 1 (0-1 games) ~ 0; ADV_FULL_GAMES+ -> 1."""
    try:
        g = float(games or 0)
    except Exception:
        g = 0.0
    if g <= 1:
        return 0.0
    return max(0.0, min(1.0, (g - 1.0) / max(1.0, ADV_FULL_GAMES - 1.0)))


def _adv_margin_adjustment(h_name, a_name):
    """Extra expected-margin points (home minus away) from the advanced-stats
    efficiency edge, weighted by season progress. Returns (points, weight, info)
    or (0.0, 0.0, None) when advanced data is unavailable for either side."""
    ha = _adv_lookup(h_name)
    aa = _adv_lookup(a_name)
    if not ha or not aa:
        return 0.0, 0.0, None

    def num(x):
        try:
            return float(x)
        except Exception:
            return None

    # Season-progress weight = the smaller of the two teams' game counts (we only
    # trust the matchup as much as the thinner sample allows).
    w_games = min(_season_weight(ha.get("games")), _season_weight(aa.get("games")))
    if w_games <= 0.0:
        return 0.0, 0.0, {"reason": "insufficient games (early season)"}

    pts = 0.0
    # PPA (EPA/play): home offense vs away defense, and vice versa. For defense,
    # LOWER ppa allowed is better, so a positive (off - def_allowed) helps.
    ho, hd = num(ha.get("off_ppa")), num(ha.get("def_ppa"))
    ao, ad = num(aa.get("off_ppa")), num(aa.get("def_ppa"))
    if None not in (ho, ad):
        pts += (ho - ad) * ADV_PPA_TO_PTS
    if None not in (ao, hd):
        pts -= (ao - hd) * ADV_PPA_TO_PTS
    # Success rate: same matchup logic, offense vs opposing defense.
    hos, hds = num(ha.get("off_success")), num(ha.get("def_success"))
    aos, ads = num(aa.get("off_success")), num(aa.get("def_success"))
    if None not in (hos, ads):
        pts += (hos - ads) * ADV_SUCC_TO_PTS
    if None not in (aos, hds):
        pts -= (aos - hds) * ADV_SUCC_TO_PTS
    # Explosiveness: offense vs opposing defense.
    hoe, hde = num(ha.get("off_explosive")), num(ha.get("def_explosive"))
    aoe, ade = num(aa.get("off_explosive")), num(aa.get("def_explosive"))
    if None not in (hoe, ade):
        pts += (hoe - ade) * ADV_EXPL_TO_PTS
    if None not in (aoe, hde):
        pts -= (aoe - hde) * ADV_EXPL_TO_PTS

    weight = w_games * ADV_MAX_WEIGHT
    return pts * weight, weight, {
        "raw_adv_pts": round(pts, 2), "season_weight": round(w_games, 2),
        "applied_weight": round(weight, 3),
        "home_games": ha.get("games"), "away_games": aa.get("games"),
    }


def _lookup(name):
    d = _load()
    teams = d.get("teams") or {}
    if not teams:
        return None
    n = _norm(name)
    # 1) exact
    if n in teams:
        return teams[n]
    # 2) alias (exact, then as a leading match in case a mascot slipped through)
    for ak, av in _ALIASES.items():
        if (n == ak or n.startswith(ak)) and av in teams:
            return teams[av]
    # 3) school-name prefix: CFBD's name is usually the leading part of ESPN's
    # ("Alabama" vs "Alabama Crimson Tide"); take the longest such match.
    best = None
    for k, v in teams.items():
        if n.startswith(k) or k.startswith(n):
            if best is None or len(k) > len(best[0]):
                best = (k, v)
    return best[1] if best else None


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _fcs_rating(name):
    """A synthetic SP+ record for a team not in the FBS SP+ file (i.e. an FCS
    opponent). Rated below the weakest FBS team so the FBS side is a big, but not
    absurd, favorite."""
    return {"name": name, "sp": FCS_SP, "off": FCS_OFF, "def": FCS_DEF,
            "_fcs_baseline": True}


def predict(home_name, away_name):
    """SP+ home win prob + margin + total.

    Both FBS (both in file)        -> SP+ vs SP+.
    One FBS, one unrated (FCS)     -> the unrated side gets an FCS baseline
                                      rating, so the FBS team is a large favorite
                                      (this is the common early-season buy game).
    Neither in file                -> None (let the caller fall back; two unrated
                                      teams is a non-FBS matchup we don't model).
    """
    d = _load()
    if not (d.get("teams") or {}):
        return None                              # no SP+ data at all -> fall back
    h = _lookup(home_name)
    a = _lookup(away_name)

    # Both unmatched: not an FBS game we model — fall back.
    if not h and not a:
        return None

    # Exactly one matched -> the other is an unrated (FCS) opponent. Use the
    # baseline instead of bailing, and log the unmatched name once in case it's
    # actually an FBS team we failed to alias (so we can add it later).
    fcs_side = None
    if h and not a:
        miss = away_name
        a = _fcs_rating(away_name)
        fcs_side = "away"
    elif a and not h:
        miss = home_name
        h = _fcs_rating(home_name)
        fcs_side = "home"
    else:
        miss = None

    if miss and miss not in _logged_miss:
        _logged_miss.add(miss)
        print(f"[ncaaf] SP+ unmatched (treated as FCS baseline): {miss!r}")

    if h.get("sp") is None or a.get("sp") is None:
        return None

    sp_margin = (h["sp"] - a["sp"]) + HOME_FIELD
    # Blend in the advanced-stats efficiency edge (EPA/success/explosiveness),
    # weighted by season progress — ~0 in Week 1, ramping to full by mid-season.
    # Skipped entirely for FCS-baseline games (no advanced data for the FCS side).
    adv_pts, adv_w, adv_info = (0.0, 0.0, None)
    if not fcs_side:
        try:
            adv_pts, adv_w, adv_info = _adv_margin_adjustment(home_name, away_name)
        except Exception:
            adv_pts, adv_w, adv_info = 0.0, 0.0, None
    margin = sp_margin + adv_pts
    prob_home = max(0.02, min(0.98, _norm_cdf(margin / MARGIN_SD)))

    total = None
    nat = d.get("national") or {}
    dbase = nat.get("def")
    try:
        if None not in (h.get("off"), a.get("off"), h.get("def"),
                        a.get("def"), dbase):
            hp = h["off"] + (a["def"] - dbase)   # home offense vs away defense
            ap = a["off"] + (h["def"] - dbase)   # away offense vs home defense
            total = round(hp + ap, 1)
    except Exception:
        total = None

    # An FBS-vs-FCS game is inherently lower-confidence than FBS-vs-FBS (the FCS
    # side is a baseline estimate, not a measured rating), so label it as such.
    confidence = "medium" if fcs_side else "high"

    out = {
        "prob_home": round(prob_home, 4),
        "exp_margin": round(margin, 1),
        "sp_margin": round(sp_margin, 1),
        "home_rating": round(h["sp"], 1),
        "away_rating": round(a["sp"], 1),
        "confidence": confidence,
        "avg_total": total,
        "model": "cfbd-sp+" + ("/fcs-baseline" if fcs_side
                               else ("/adv-blend" if adv_w > 0 else "")),
    }
    if adv_w > 0:
        out["adv_adjustment"] = round(adv_pts, 1)
        out["adv_detail"] = adv_info
    return out
