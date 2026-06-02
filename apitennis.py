"""
apitennis.py
------------
Real data-feed adapter for API-Tennis (https://api-tennis.com).

Covers ATP / WTA / Challenger (ITF/doubles filtered out). Your key is read
from TENNIS_API_KEY and only ever used server-side.

Endpoints used (from API-Tennis docs):
  get_fixtures   -> a day's matches (with event_time, tournament, player keys)
  get_livescore  -> all currently-live matches at once
  get_standings  -> ATP / WTA rankings  (used to rate otherwise-unknown players)
  get_H2H        -> head-to-head + each player's recent results
  (point-by-point arrives inside get_fixtures for a specific match key)
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from base import LiveScore, MatchInfo, MatchStats, TennisProvider

BASE_URL = "https://api.api-tennis.com/tennis/"

_TIER_MAP = {
    "Atp Singles": "ATP",
    "Wta Singles": "WTA",
    "Challenger Men Singles": "CHALLENGER",
    "Challenger Women Singles": "CHALLENGER",
}

_LIVE_TTL = 8.0          # seconds between live-score pulls
_FIXTURE_TTL = 20.0      # seconds to cache a single match's detail pull


def _server(flag) -> str:
    return "a" if flag == "First Player" else "b" if flag == "Second Player" else "a"


def _winner(flag) -> str | None:
    return "a" if flag == "First Player" else "b" if flag == "Second Player" else None


def _sets(scores):
    a, b = [], []
    for s in scores or []:
        try:
            a.append(int(s.get("score_first", 0)))
            b.append(int(s.get("score_second", 0)))
        except (ValueError, TypeError):
            pass
    return a, b


def _game(result):
    if not result or str(result).strip() in ("-", ""):
        return "0", "0"
    parts = [p.strip() for p in str(result).split("-")]
    return (parts[0], parts[1]) if len(parts) == 2 else ("0", "0")


def _status(fix):
    st = (fix.get("event_status") or "").strip()
    if st == "Finished" or fix.get("event_winner"):
        return "finished"
    if fix.get("event_live") == "1" or st.startswith("Set"):
        return "live"
    return "scheduled"


class APITennisProvider(TennisProvider):
    name = "apitennis"

    def __init__(self, api_key=None, timezone=None):
        self.api_key = api_key or os.environ.get("TENNIS_API_KEY")
        if not self.api_key:
            raise RuntimeError("Set TENNIS_API_KEY to use the live API-Tennis feed.")
        self.timezone = timezone or os.environ.get("TENNIS_TZ", "America/Chicago")
        self._fixtures = {}            # event_key -> raw fixture (latest seen)
        self._live_cache = {}
        self._live_fetched_at = 0.0
        self._detail_cache = {}        # event_key -> (ts, raw fixture with pbp)

    # ---- HTTP ------------------------------------------------------------

    def _call(self, method, **params):
        import httpx
        params = {"method": method, "APIkey": self.api_key, "timezone": self.timezone, **params}
        r = httpx.get(BASE_URL, params=params, timeout=20.0)
        r.raise_for_status()
        data = r.json()
        if not data or data.get("success") != 1:
            return []
        return data.get("result", []) or []

    # ---- schedule --------------------------------------------------------

    def get_schedule(self, day: datetime):
        d = day.strftime("%Y-%m-%d")
        rows = self._call("get_fixtures", date_start=d, date_stop=d)
        out = []
        for fix in rows:
            tier = _TIER_MAP.get(fix.get("event_type_type"))
            if tier is None:
                continue
            key = str(fix.get("event_key"))
            self._fixtures[key] = fix
            try:
                when = datetime.strptime(
                    f"{fix.get('event_date')} {fix.get('event_time','00:00')}", "%Y-%m-%d %H:%M")
            except ValueError:
                when = day
            out.append(MatchInfo(
                provider_match_id=key, tier=tier,
                tournament=fix.get("tournament_name", "Tennis"),
                surface="Unknown",
                player_a=fix.get("event_first_player", "Player A"),
                player_b=fix.get("event_second_player", "Player B"),
                scheduled=when, best_of=3, status=_status(fix),
            ))
        return out

    def fixture_meta(self, provider_match_id):
        """Extra fields we keep but the neutral MatchInfo doesn't carry."""
        fix = self._fixtures.get(str(provider_match_id), {})
        return {
            "event_time": fix.get("event_time"),
            "tournament_key": str(fix.get("tournament_key") or ""),
            "round": fix.get("tournament_round") or "",
            "player_a_key": str(fix.get("event_first_player_key") or ""),
            "player_b_key": str(fix.get("event_second_player_key") or ""),
            "player_a_logo": fix.get("event_first_player_logo"),
            "player_b_logo": fix.get("event_second_player_logo"),
        }

    # ---- live ------------------------------------------------------------

    def _refresh_live(self):
        if time.time() - self._live_fetched_at < _LIVE_TTL:
            return
        self._live_fetched_at = time.time()
        try:
            rows = self._call("get_livescore")
        except Exception:
            return
        cache = {}
        for fix in rows:
            if _TIER_MAP.get(fix.get("event_type_type")) is None:
                continue
            key = str(fix.get("event_key"))
            self._fixtures[key] = fix
            sa, sb = _sets(fix.get("scores"))
            ga, gb = _game(fix.get("event_game_result"))
            cache[key] = LiveScore(sets_a=sa, sets_b=sb, game_a=ga, game_b=gb,
                                   server=_server(fix.get("event_serve")),
                                   status="live", winner=None)
        self._live_cache = cache

    def get_live_score(self, provider_match_id):
        self._refresh_live()
        key = str(provider_match_id)
        if key in self._live_cache:
            return self._live_cache[key]
        fix = self._fixtures.get(key)
        if not fix:
            return LiveScore(status="scheduled")
        sa, sb = _sets(fix.get("scores"))
        st = _status(fix)
        return LiveScore(sets_a=sa, sets_b=sb, game_a="", game_b="",
                         server=_server(fix.get("event_serve")), status=st,
                         winner=_winner(fix.get("event_winner")) if st == "finished" else None)

    def get_match_stats(self, provider_match_id):
        # Kept for the narrow interface; the rich detail path uses raw_fixture().
        return MatchStats()

    # ---- detail (point-by-point, for the match page) ---------------------

    def raw_fixture(self, provider_match_id):
        """
        Full fixture for ONE match, including point-by-point. Cached briefly so
        a live detail page polling every few seconds doesn't hammer the feed.
        """
        key = str(provider_match_id)
        cached = self._detail_cache.get(key)
        if cached and time.time() - cached[0] < _FIXTURE_TTL:
            return cached[1]
        rows = []
        try:
            rows = self._call("get_fixtures", match_key=key)
        except Exception:
            pass
        fix = rows[0] if rows else self._fixtures.get(key, {})
        if fix:
            self._fixtures[key] = fix
            self._detail_cache[key] = (time.time(), fix)
        return fix

    # ---- rankings (fills the rating model for unknown players) -----------

    def get_rankings(self):
        """Return {normalized_name: rank_int} for ATP + WTA."""
        out = {}
        for ev in ("ATP", "WTA"):
            try:
                rows = self._call("get_standings", event_type=ev)
            except Exception:
                continue
            for r in rows or []:
                name = r.get("player")
                place = r.get("place") or r.get("rank")
                if not name or place in (None, ""):
                    continue
                try:
                    out[name] = int(str(place).strip())
                except ValueError:
                    pass
        return out

    # ---- head to head ----------------------------------------------------

    def get_h2h(self, key_a, key_b):
        """Raw get_H2H result dict, or {} on failure."""
        if not key_a or not key_b:
            return {}
        try:
            res = self._call("get_H2H", first_player_key=key_a, second_player_key=key_b)
        except Exception:
            return {}
        # get_H2H returns a dict (not a list) under result; _call already unwrapped.
        return res if isinstance(res, dict) else {}
