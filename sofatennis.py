"""
sofatennis.py
-------------
SofaScore-backed tennis provider (Stage 1).

Why this exists: API-Tennis was cancelled, so the paid feed is gone. SofaScore
exposes an undocumented-but-stable JSON API (the same class of thing as the
ESPN hidden API already used for the team sports) that carries ATP, WTA,
Challenger AND ITF — which matters because most of the tennis track record is
Challenger/ITF volume. This provider pulls:

  * the day's fixtures            -> get_schedule()
  * live score snapshots          -> get_live_score()
  * finished results              -> final_results()
  * player rankings (for the model line)

and derives a MODEL-ONLY line (no market odds) from ATP/WTA ranking + surface +
a small home/first-listed edge, converted to American odds. People can compare
to the market themselves — which fits the "receipts, not hype" ethos.

STAGE 1 SCOPE (deliberate):
  * The rich serve/return per-player statistical model that API-Tennis fed is
    NOT reproduced here yet (that's Stage 2). player_serve_averages() and
    match_statistics() return empty-but-valid shapes so the app never breaks;
    the UI already degrades gracefully when stats are absent (ITF has always
    been statless).
  * Everything is best-effort and FAIL-SOFT: any network/parse error yields an
    empty list or a neutral object, never an exception that could blank the
    whole site. Tennis going quiet is acceptable; tennis crashing pages is not.

ENV VARS:
  SOFA_BASE          default https://api.sofascore.com/api/v1
  SOFA_TIMEOUT       per-request seconds (default 8)
  SOFA_LIVE_TTL      live-poll cache seconds (default 15)
  SOFA_DAY_TTL       schedule cache seconds (default 120)
  SOFA_INCLUDE_ITF   "1" (default) to include ITF, "0" to exclude
  SOFA_USER_AGENT    override the request UA if needed
  TENNIS_TZ          IANA tz for day bucketing (default America/Chicago)
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from base import (LiveScore, MatchInfo, MatchStats, PlayerStats,
                  TennisProvider)

BASE_URL = os.environ.get("SOFA_BASE", "https://api.sofascore.com/api/v1").rstrip("/")
_TIMEOUT = float(os.environ.get("SOFA_TIMEOUT", "8"))
_LIVE_TTL = float(os.environ.get("SOFA_LIVE_TTL", "15"))
_DAY_TTL = float(os.environ.get("SOFA_DAY_TTL", "120"))
_INCLUDE_ITF = os.environ.get("SOFA_INCLUDE_ITF", "1").strip().lower() not in ("0", "false", "no", "off")
_UA = os.environ.get(
    "SOFA_USER_AGENT",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
)

# Central-time (or configured) day bucketing, mirroring the rest of the app.
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(os.environ.get("TENNIS_TZ", "America/Chicago"))
except Exception:                                       # pragma: no cover
    _TZ = timezone.utc


# ---------------------------------------------------------------------------
# helpers (kept local so this file has no hard dependency on apitennis)
# ---------------------------------------------------------------------------

def _infer_surface(name: str, category: str = "", tier: str | None = None) -> str:
    """Best-effort surface from tournament / category text. SofaScore doesn't
    always label surface on the scheduled-events payload, so we keyword-match
    and fall back to Hard (the most common)."""
    hay = f"{name or ''} {category or ''}".lower()
    if "grass" in hay:
        return "Grass"
    if "clay" in hay or "roland" in hay or "garros" in hay:
        return "Clay"
    if "hard" in hay:
        return "Hard"
    return "Hard"


def _classify_tier(tournament: dict) -> str | None:
    """Map a SofaScore tennis tournament object to one of our tiers, or None to
    skip (doubles, team events, unknowns).

    SofaScore shapes this as:
      tournament.name / .slug
      tournament.category.name  (e.g. "ATP", "WTA", "Challenger Men", "ITF Men")
      tournament.uniqueTournament.name
    We classify by keyword + gender, mirroring the apitennis logic."""
    t = tournament or {}
    name = (t.get("name") or "").lower()
    uniq = ((t.get("uniqueTournament") or {}).get("name") or "").lower()
    cat = ((t.get("category") or {}).get("name") or "").lower()
    catslug = ((t.get("category") or {}).get("slug") or "").lower()
    hay = " ".join([name, uniq, cat, catslug])

    # Exclude doubles + national-team competitions (not bettable singles).
    if "doubles" in hay:
        return None
    if "teams" in hay or any(k in hay for k in (
            "davis cup", "billie jean king", "united cup",
            "laver cup", "atp cup", "fed cup", "hopman")):
        return None

    is_women = ("women" in hay or "wta" in hay or "ladies" in hay or "girls" in hay)
    is_men = ("men" in hay or "atp" in hay or "boys" in hay) and not is_women
    is_chall = "challenger" in hay
    is_itf = ("itf" in hay or "futures" in hay
              or any(c in hay for c in ("m15", "m25", "w15", "w25", "w35",
                                        "w50", "w75", "w100")))

    if is_women:
        if is_chall:            # WTA 125 "challenger"-style events are WTA
            return "WTA"
        if is_itf:
            return "ITF" if _INCLUDE_ITF else None
        return "WTA"
    if is_chall and not is_itf:
        return "CHALLENGER"
    if is_men:
        if is_itf:
            return "ITF" if _INCLUDE_ITF else None
        return "ATP"
    # explicit tour words without a clear gender
    if "atp" in hay:
        return "CHALLENGER" if is_chall else "ATP"
    if "wta" in hay:
        return "WTA"
    # ITF-only signal, gender unknown
    if is_itf:
        return "ITF" if _INCLUDE_ITF else None
    return None


# SofaScore status.type -> our status
def _status_from(ev: dict) -> str:
    st = ((ev.get("status") or {}).get("type") or "").lower()
    if st == "finished":
        return "finished"
    if st == "inprogress":
        return "live"
    return "scheduled"


def _winner_side(ev: dict) -> str | None:
    wc = ev.get("winnerCode")
    if wc == 1:
        return "a"
    if wc == 2:
        return "b"
    return None


def _american_from_prob(p: float) -> int | None:
    """Fair American line from a win probability."""
    if p is None or p <= 0.0 or p >= 1.0:
        return None
    if p >= 0.5:
        return -round(100 * p / (1 - p))
    return round(100 * (1 - p) / p)


# Elo-ish ranking curve: convert an ATP/WTA rank into a rating, then two ratings
# into a win probability. Deliberately simple and transparent for Stage 1.
def _rank_rating(rank: int | None) -> float:
    if not rank or rank <= 0:
        return 1500.0          # unranked/unknown -> neutral
    import math
    # rank 1 ~ 2000, rank 100 ~ 1500, rank 1000 ~ 1150; smooth log decay
    return 2000.0 - 217.0 * math.log10(max(1, rank))


def _prob_from_ratings(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


class SofaTennisProvider(TennisProvider):
    name = "sofascore"

    def __init__(self, timezone_name: str | None = None):
        self.last_error: str | None = None
        self._events: dict[str, dict] = {}        # event_id -> raw event
        self._day_cache: dict[str, tuple[float, list]] = {}   # date -> (ts, MatchInfo[])
        self._live_cache: dict[str, tuple[float, dict]] = {}  # event_id -> (ts, event)
        self._rank_cache: dict[str, tuple[float, dict]] = {}  # tour -> (ts, {name_lower: rank})
        self._client = httpx.Client(
            timeout=_TIMEOUT,
            headers={"User-Agent": _UA, "Accept": "application/json",
                     "Referer": "https://www.sofascore.com/"},
            follow_redirects=True,
        )

    # ---- low-level ------------------------------------------------------
    def _get(self, path: str):
        """GET {BASE_URL}{path} -> parsed JSON or None. Never raises."""
        url = f"{BASE_URL}{path}"
        try:
            r = self._client.get(url)
            if r.status_code != 200:
                self.last_error = f"{r.status_code} on {path}"
                return None
            return r.json()
        except Exception as e:                              # network/parse
            self.last_error = f"{type(e).__name__}: {e}"
            return None

    # compat shim: some diagnostics call provider._call(...)
    def _call(self, method, **params):
        return {"note": "sofascore provider has no _call; use REST paths", "method": method}

    def _refresh_live(self):
        """No-op kept for API compatibility; live is pulled per-match on demand."""
        return None

    # ---- schedule -------------------------------------------------------
    def _day_key(self, day: datetime) -> str:
        return day.strftime("%Y-%m-%d")

    def get_schedule(self, day: datetime) -> list[MatchInfo]:
        dk = self._day_key(day)
        hit = self._day_cache.get(dk)
        if hit and (time.time() - hit[0] < _DAY_TTL):
            return hit[1]

        data = self._get(f"/sport/tennis/scheduled-events/{dk}")
        out: list[MatchInfo] = []
        if not data:
            # cache the empty result briefly so a dead call doesn't hammer
            self._day_cache[dk] = (time.time(), out)
            return out

        for ev in (data.get("events") or []):
            try:
                tier = _classify_tier(ev.get("tournament") or {})
                if tier is None:
                    continue
                eid = str(ev.get("id"))
                self._events[eid] = ev
                home = (ev.get("homeTeam") or {}).get("name") or "Player A"
                away = (ev.get("awayTeam") or {}).get("name") or "Player B"
                # singles only: SofaScore doubles names contain "/"
                if "/" in home or "/" in away:
                    continue
                ts = ev.get("startTimestamp")
                when = (datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_TZ)
                        if ts else day)
                # STRICT date filter: keep only events whose local date == dk.
                if ts and when.strftime("%Y-%m-%d") != dk:
                    continue
                tname = (ev.get("tournament") or {}).get("name") or "Tennis"
                cat = ((ev.get("tournament") or {}).get("category") or {}).get("name") or ""
                out.append(MatchInfo(
                    provider_match_id=eid,
                    tier=tier,
                    tournament=tname,
                    surface=_infer_surface(tname, cat, tier),
                    player_a=home,
                    player_b=away,
                    scheduled=when.replace(tzinfo=None),
                    best_of=3,
                    status=_status_from(ev),
                ))
            except Exception as e:
                self.last_error = f"schedule row: {type(e).__name__}: {e}"
                continue

        self._day_cache[dk] = (time.time(), out)
        return out

    def fixture_meta(self, provider_match_id: str) -> dict:
        """Extra fields the neutral MatchInfo doesn't carry."""
        ev = self._events.get(str(provider_match_id), {})
        home = ev.get("homeTeam") or {}
        away = ev.get("awayTeam") or {}
        ts = ev.get("startTimestamp")
        et = ""
        if ts:
            try:
                et = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_TZ).strftime("%H:%M")
            except Exception:
                et = ""
        return {
            "event_time": et,
            "tournament_key": str(((ev.get("tournament") or {}).get("uniqueTournament") or {}).get("id") or ""),
            "round": ((ev.get("roundInfo") or {}).get("name") or ""),
            "player_a_key": str(home.get("id") or ""),
            "player_b_key": str(away.get("id") or ""),
            "player_a_logo": None,
            "player_b_logo": None,
        }

    # ---- live -----------------------------------------------------------
    def _event(self, provider_match_id: str) -> dict:
        """Fetch the freshest single-event payload (cached _LIVE_TTL)."""
        eid = str(provider_match_id)
        hit = self._live_cache.get(eid)
        if hit and (time.time() - hit[0] < _LIVE_TTL):
            return hit[1]
        data = self._get(f"/event/{eid}") or {}
        ev = data.get("event") or self._events.get(eid) or {}
        if ev:
            self._live_cache[eid] = (time.time(), ev)
        return ev

    def get_live_score(self, provider_match_id: str) -> LiveScore:
        ev = self._event(provider_match_id)
        if not ev:
            return LiveScore(status="scheduled")
        hs = ev.get("homeScore") or {}
        as_ = ev.get("awayScore") or {}
        # SofaScore per-set fields: period1..period5 (games won in each set).
        sets_a, sets_b = [], []
        for i in range(1, 6):
            pa, pb = hs.get(f"period{i}"), as_.get(f"period{i}")
            if pa is None and pb is None:
                continue
            sets_a.append(int(pa or 0))
            sets_b.append(int(pb or 0))
        status = _status_from(ev)
        # current game points (SofaScore uses .point on score objs when live)
        ga = str(hs.get("point", "0")) if status == "live" else "0"
        gb = str(as_.get("point", "0")) if status == "live" else "0"
        return LiveScore(
            sets_a=sets_a, sets_b=sets_b,
            game_a=ga, game_b=gb,
            server="a",                       # SofaScore server flag is inconsistent; default a
            status=status,
            winner=_winner_side(ev) if status == "finished" else None,
        )

    def get_match_stats(self, provider_match_id: str) -> MatchStats:
        """Stage 1: return empty-but-valid stats. The serve/return statistical
        model is Stage 2; the UI already handles absent stats (ITF always was)."""
        return MatchStats(player_a=None, player_b=None)

    # ---- results --------------------------------------------------------
    def final_results(self, day: datetime) -> list[dict]:
        """Finished matches for the day, as simple dicts the grader can read:
        {provider_match_id, winner ('a'/'b'), status}."""
        dk = self._day_key(day)
        data = self._get(f"/sport/tennis/scheduled-events/{dk}")
        out = []
        for ev in ((data or {}).get("events") or []):
            if _status_from(ev) != "finished":
                continue
            if _classify_tier(ev.get("tournament") or {}) is None:
                continue
            out.append({
                "provider_match_id": str(ev.get("id")),
                "winner": _winner_side(ev),
                "status": "finished",
            })
        return out

    # ---- rankings + model line -----------------------------------------
    def get_rankings(self) -> dict:
        """Return {player_name: rank_int} across ATP + WTA, matching the shape
        apitennis returned (raw display name as key — the PredictionEngine does
        its own name normalization). Cached 12h. Best-effort: an empty dict just
        means the model treats players as neutral until ranks load."""
        out: dict[str, int] = {}
        for tour, rid in (("atp", 1), ("wta", 2)):
            hit = self._rank_cache.get(tour)
            if hit and (time.time() - hit[0] < 43200):
                out.update(hit[1])
                continue
            # SofaScore rankings endpoint for tennis tours.
            data = self._get(f"/rankings/type/{rid}")
            m: dict[str, int] = {}
            for row in ((data or {}).get("rankings") or []):
                try:
                    nm = ((row.get("team") or {}).get("name") or "").strip()
                    rk = int(row.get("ranking") or row.get("position") or 0)
                    if nm and rk:
                        m[nm] = rk
                except Exception:
                    continue
            if m:
                self._rank_cache[tour] = (time.time(), m)
                out.update(m)
        return out

    def _ranks_lower(self) -> dict:
        """Lowercased view for the internal model_line helper only."""
        return {k.strip().lower(): v for k, v in self.get_rankings().items()}

    def model_line(self, info: MatchInfo) -> dict:
        """MODEL-ONLY line for a match: win prob for player_a and the fair
        American odds. Uses ATP/WTA ranking + a tiny first-listed edge. Returns
        neutral (50/50) when ranks are unknown (common at ITF)."""
        ranks = self._ranks_lower()
        ra = _rank_rating(ranks.get((info.player_a or "").strip().lower()))
        rb = _rank_rating(ranks.get((info.player_b or "").strip().lower()))
        # small edge to the first-listed player (SofaScore home = usually higher seed)
        ra += 15.0
        p = _prob_from_ratings(ra, rb)
        p = max(0.02, min(0.98, p))
        return {
            "prob_a": round(p, 4),
            "prob_b": round(1 - p, 4),
            "odds_a": _american_from_prob(p),
            "odds_b": _american_from_prob(1 - p),
            "model_only": True,
        }

    # ---- compatibility no-ops (apitennis had these; keep app happy) ------
    def player_serve_averages(self, name, key, surface=None, **kw) -> dict:
        """Stage 2 feature. Return an empty dict so callers degrade gracefully."""
        return {}

    def match_statistics(self, match_key: str) -> dict:
        return {"match_key": str(match_key), "statistics": [], "available": False,
                "note": "serve/return stats are Stage 2 for the SofaScore provider"}

    def raw_fixture(self, provider_match_id: str) -> dict:
        return self._event(provider_match_id) or {}

    def raw_fixture_probe(self, match_id: str) -> dict:
        ev = self._event(match_id) or {}
        return {
            "found": bool(ev),
            "top_level_keys": sorted(ev.keys()),
            "has_statistics": False,
            "status": (ev.get("status") or {}).get("type"),
            "players": [(ev.get("homeTeam") or {}).get("name"),
                        (ev.get("awayTeam") or {}).get("name")],
        }

    def get_h2h(self, key_a, key_b):
        return {}

    def get_match_context(self, key_a, key_b, match_dt=None):
        return {}

    def get_odds(self, day=None, match_key=None):
        return {}
