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


def _classify_tier(fix):
    """
    Decide the tier from an API fixture, robustly. The feed's exact
    'event_type_type' strings vary (case, spacing, naming), and WTA events were
    being dropped by exact-string matching. We normalize and match by keyword,
    and also fall back to the tournament name (e.g. 'WTA 1000 Rome').
    Singles only; doubles/ITF excluded.
    """
    et = (fix.get("event_type_type") or "").strip().lower()
    name = (fix.get("tournament_name") or "").lower()
    hay = et + " " + name

    # exclude doubles explicitly (singles product only)
    if "doubles" in hay or "/" in (fix.get("event_first_player") or ""):
        return None

    is_chall = "challenger" in hay or "atp challenger" in hay
    # WTA: event type or tournament name mentions wta (covers 'Wta Singles',
    # 'WTA', 'WTA 1000', etc.)
    if "wta" in hay:
        return "CHALLENGER" if is_chall and "challenger" in et else "WTA"
    if "atp" in hay:
        return "CHALLENGER" if is_chall else "ATP"
    if is_chall:
        return "CHALLENGER"
    # fall back to the original exact map if present; otherwise exclude rather
    # than guess gender/tour wrong.
    return _TIER_MAP.get(fix.get("event_type_type"))

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
            tier = _classify_tier(fix)
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
            if _classify_tier(fix) is None:
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

    def get_match_context(self, key_a, key_b, match_dt=None):
        """
        Derive form / fatigue / H2H from one get_H2H call (it returns both the
        head-to-head and each player's recent results). Returns a dict ready for
        PredictionEngine.predict_feed_ctx, or {} if unavailable.

          form_x    : win rate over the player's last ~10 results (0..1)
          fatigue_x : share of those recent matches played in the last 7 days
          h2h_x     : meetings won by each player
        """
        import datetime as _dt
        data = self.get_h2h(key_a, key_b)
        if not data:
            return {}
        ctx = {}

        # --- head to head ---
        h2h = data.get("H2H") or data.get("firstPlayer_VS_secondPlayer") or []
        if isinstance(h2h, list) and h2h:
            wa = wb = 0
            for m in h2h:
                w = (m.get("event_winner") or "").lower()
                # event_winner is "First Player" / "Second Player"
                if "first" in w:
                    wa += 1
                elif "second" in w:
                    wb += 1
            if wa or wb:
                ctx["h2h_a"], ctx["h2h_b"] = wa, wb

        # --- recent form + fatigue, per player ---
        def _player_stats(results_key):
            results = data.get(results_key) or []
            if not isinstance(results, list) or not results:
                return None, None
            recent = results[:10]
            wins = 0
            recent_count = 0
            now = match_dt or _dt.datetime.now()
            for m in recent:
                ew = (m.get("event_winner") or "").lower()
                # the player whose list this is appears as "First Player" in their own list
                if "first" in ew:
                    wins += 1
                # fatigue: did this match happen within 7 days before the match?
                ds = m.get("event_date") or ""
                try:
                    d = _dt.datetime.strptime(ds, "%Y-%m-%d")
                    if 0 <= (now - d).days <= 7:
                        recent_count += 1
                except (ValueError, TypeError):
                    pass
            form = wins / len(recent) if recent else None
            fatigue = min(1.0, recent_count / 3.0)   # 3+ matches in a week = maxed
            return form, fatigue

        fa, fata = _player_stats("firstPlayerResults")
        fb, fatb = _player_stats("secondPlayerResults")
        if fa is not None and fb is not None:
            ctx["form_a"], ctx["form_b"] = fa, fb
        if fata is not None and fatb is not None:
            ctx["fatigue_a"], ctx["fatigue_b"] = fata, fatb
        return ctx
