"""
livetennisapi.py
----------------
OPTIONAL extra data-feed adapter, for livetennisapi.com.

This does NOT replace anything. `TENNIS_PROVIDER` still defaults to `mock`,
and `TENNIS_PROVIDER=apitennis` behaves exactly as before. Set

    TENNIS_PROVIDER=livetennisapi
    LIVETENNISAPI_KEY=<key>

to use this feed instead. With neither variable set, nothing here ever runs.

Disclosure: I maintain livetennisapi.com.

WHAT THIS ADAPTER IMPLEMENTS
    get_schedule / get_live_score / get_match_stats
        The whole contract documented in base.py.
    final_results     -> live.py's reconcile pass
    fixture_meta      -> seed.py (hasattr-guarded there)
    match_statistics  -> /api/tennis/match-detail

WHAT IT DELIBERATELY DOES NOT IMPLEMENT
    get_odds, get_h2h, get_match_context, get_rankings, player_serve_averages,
    raw_fixture, _call

    This feed has no bookmaker odds, no head-to-head endpoint, no rank-ordered
    player listing and no career serve/return splits, so rather than return
    plausible-looking empties the methods are simply ABSENT. The app already
    guards every one of them (`hasattr(provider, "get_odds")`,
    `hasattr(provider, "get_match_context")`, the try/except around
    `get_rankings`, and `if not hasattr(provider, "_call")` in the api-tennis
    diagnostics), so each of those features turns itself off cleanly instead of
    being fed a lie. Predictions fall back to ratings.json / ranking-only, which
    is the same path the app already takes when api-tennis rankings fail.

    One consequence worth stating plainly: this feed is a good fit if you want
    schedule + live scores + in-match statistics. If you rely on the odds,
    H2H-context or ranking-fallback features, keep using api-tennis.

TIERS
    A match's tier comes from the API's own `tour` FILTER (one request per
    tour), never from the player records -- the API documents the player-level
    `tour` field as an opaque string that must not be parsed into the filter
    vocabulary, so guessing from it would silently mislabel Challengers.

PLANS
    /matches live+upcoming is FREE. Completed matches and /history/matches need
    BASIC; /matches/{id}/statistics needs ULTRA. Anything the key isn't
    entitled to answers 403; we record it in .last_error, stop re-asking for
    that resource, and degrade -- never crash, never invent.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from base import LiveScore, MatchInfo, MatchStats, PlayerStats, TennisProvider

BASE_URL = "https://api.livetennisapi.com/api/public/v1"

# One request per tour, because the tour filter is the only trustworthy source
# of a match's tier. `juniors` exists upstream but has no slot in base.TIERS,
# so it is not requested at all.
_TOUR_TIERS = (("atp", "ATP"), ("wta", "WTA"), ("challenger", "CHALLENGER"), ("itf", "ITF"))

# Same kill-switch name api-tennis uses here, so the two providers behave alike.
_INCLUDE_ITF = os.environ.get("INCLUDE_ITF", "1").strip().lower() not in ("0", "false", "no", "off")

_PAGE = 100          # rows per page we ask for
_MAX_PAGES = 20      # hard stop, so a bad `has_more` can never loop forever
_LIVE_TTL = float(os.environ.get("TENNIS_LIVE_TTL", "15"))

_STATUS = {"upcoming": "scheduled", "live": "live",
           "completed": "finished", "cancelled": "finished"}


def _pct01(v):
    """API percentages are integers 0-100; base.PlayerStats wants 0-1."""
    return None if v is None else v / 100.0


def _surface(match):
    """`hard` / `clay` / `grass` -> `Hard` / `Clay` / `Grass`. The field is
    nullable, so fall back to the app's own tournament-name guesser (shared with
    the api-tennis provider) and finally to Hard, the modal surface."""
    s = match.get("surface")
    if s:
        return str(s).capitalize()
    try:
        from apitennis import _infer_surface
        return _infer_surface(match.get("tournament") or "")
    except Exception:
        return "Hard"


def _when(match, fallback):
    ts = match.get("scheduled_time")
    if not ts:
        return fallback
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return fallback


def _side(match, key):
    return ((match.get("players") or {}).get(key) or {})


def _winner(match):
    """'a' / 'b' / None. `winner` is a PLAYER id on completed matches only."""
    w = match.get("winner")
    if w is None:
        return None
    if w == _side(match, "p1").get("id"):
        return "a"
    if w == _side(match, "p2").get("id"):
        return "b"
    return None


class LiveTennisAPIProvider(TennisProvider):
    name = "livetennisapi"

    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get("LIVETENNISAPI_KEY")
        if not self.api_key:
            raise RuntimeError("Set LIVETENNISAPI_KEY to use the live livetennisapi.com feed.")
        self._matches = {}        # match id -> latest match row we've seen
        self._live_fetched_at = 0.0
        self._denied = set()      # paths the key isn't entitled to; asked once
        self.last_error = None

    # ---- HTTP ------------------------------------------------------------
    # NOTE: deliberately not named `_call`. main.py uses `hasattr(provider,
    # "_call")` to detect the api-tennis provider for its raw diagnostics, and
    # those routes would misread this feed's shapes.

    def _get(self, path, _kind=None, **params):
        """GET one page. Returns the decoded body, or None when the request was
        refused (auth/plan/rate-limit) so every caller can degrade instead of
        raising into the poll loop.

        `_kind` is the entitlement bucket to remember a 403 against. It matters
        for the per-match routes: keying on the literal path would remember one
        match id instead of 'this key has no statistics', and grow forever."""
        import httpx

        kind = _kind or path
        if kind in self._denied:
            return None
        params = {k: v for k, v in params.items() if v is not None}
        try:
            r = httpx.get(BASE_URL + path, params=params, timeout=20.0,
                          headers={"Authorization": f"Bearer {self.api_key}",
                                   "Accept": "application/json"})
        except Exception as e:
            self.last_error = f"{path}: {type(e).__name__}: {e}"
            print(f"[livetennisapi] request failed ({self.last_error})")
            return None

        if r.status_code in (401, 403):
            # 401 bad key, 403 plan doesn't cover it. Either way re-asking every
            # poll just burns quota, so remember it.
            self._denied.add(kind)
            self.last_error = f"{path}: HTTP {r.status_code} — key not entitled to this endpoint"
            print(f"[livetennisapi] {self.last_error}")
            return None
        if r.status_code == 429:
            self.last_error = f"{path}: rate limited"
            return None
        if r.status_code >= 400:
            self.last_error = f"{path}: HTTP {r.status_code}"
            return None

        self.last_error = None
        return r.json()

    def _pages(self, path, _kind=None, **params):
        """Follow `meta.has_more`, capped. Returns the accumulated rows.
        List routes are the only ones with a {data, meta} envelope."""
        out = []
        for page in range(_MAX_PAGES):
            body = self._get(path, _kind=_kind, limit=_PAGE, offset=page * _PAGE, **params)
            if not body:
                break
            out.extend(body.get("data") or [])
            if not ((body.get("meta") or {}).get("has_more")):
                break
        return out

    def _tours(self):
        for tour, tier in _TOUR_TIERS:
            if tier == "ITF" and not _INCLUDE_ITF:
                continue
            yield tour, tier

    # ---- schedule --------------------------------------------------------

    def get_schedule(self, day: datetime):
        """The given day's matches. Built from /matches (live + upcoming +
        completed) because that is the only route carrying the `tour` filter we
        need for a trustworthy tier.

        Honest limitation: /matches is not date-filtered, so this is accurate
        for today and the near term and will return little for a day far in the
        past. Reconciling an older day is what final_results() is for."""
        want = day.date()
        found = {}
        for tour, tier in self._tours():
            for status in ("live", "upcoming", "completed"):
                # Bucket the entitlement per STATUS, not per path: `completed`
                # needs BASIC while live/upcoming are FREE, so a 403 on
                # completed must not switch off the other two.
                for m in self._pages("/matches", _kind=f"matches:{status}",
                                     status=status, tour=tour):
                    mid = m.get("id")
                    if mid is None or m.get("status") == "cancelled":
                        continue
                    when = _when(m, None)
                    if when is None or when.date() != want:
                        continue
                    self._matches[str(mid)] = m
                    p1, p2 = _side(m, "p1"), _side(m, "p2")
                    found[str(mid)] = MatchInfo(
                        provider_match_id=str(mid),
                        tier=tier,
                        tournament=m.get("tournament") or "Tennis",
                        surface=_surface(m),
                        player_a=p1.get("name") or "Player A",
                        player_b=p2.get("name") or "Player B",
                        scheduled=when,
                        best_of=5 if m.get("format") == "BO5" else 3,
                        status=_STATUS.get(m.get("status"), "scheduled"),
                    )
        return list(found.values())

    def fixture_meta(self, provider_match_id):
        """Extra fields the neutral MatchInfo doesn't carry. `tournament_key`
        stays empty: this feed has no tournament id, `tournament` is free text.
        Player logos aren't served either, so both are left as None."""
        m = self._matches.get(str(provider_match_id), {})
        return {
            "event_time": (m.get("scheduled_time") or "")[11:16],
            "tournament_key": "",
            "round": m.get("round") or "",
            "player_a_key": str(_side(m, "p1").get("id") or ""),
            "player_b_key": str(_side(m, "p2").get("id") or ""),
            "player_a_logo": None,
            "player_b_logo": None,
        }

    # ---- live ------------------------------------------------------------

    def _refresh_live_cache(self):
        """ONE request pulls every live match with its latest score, so the
        per-match poll costs nothing. Same trick the api-tennis provider uses.

        Not named `_refresh_live`/`_fixtures`: main.py reaches into those on the
        api-tennis provider expecting that feed's raw shapes."""
        if time.time() - self._live_fetched_at < _LIVE_TTL:
            return
        self._live_fetched_at = time.time()
        for tour, _tier in self._tours():
            for m in self._pages("/matches", _kind="matches:live", status="live", tour=tour):
                if m.get("id") is not None:
                    self._matches[str(m["id"])] = m

    def _match(self, provider_match_id, refresh=True):
        key = str(provider_match_id)
        if refresh:
            self._refresh_live_cache()
        m = self._matches.get(key)
        if m and m.get("status") == "live":
            return m
        # Not in the live snapshot (scheduled, or already finished and dropped
        # out of it) -- ask for the one match directly. Single-match routes
        # return the object itself, with no {data, meta} envelope.
        one = self._get(f"/matches/{key}", _kind="match")
        if isinstance(one, dict) and one.get("id") is not None:
            self._matches[key] = one
            return one
        return m

    def get_live_score(self, provider_match_id):
        m = self._match(provider_match_id)
        if not m:
            return LiveScore(status="scheduled")
        score = m.get("score") or {}

        # `games` is [games_p1, games_p2], each a per-set list. A COMPLETED match
        # can legitimately carry an empty games array -- we pass the emptiness
        # through rather than inventing a scoreline.
        games = score.get("games") or []
        sets_a = [g for g in (games[0] if len(games) > 0 and games[0] else [])]
        sets_b = [g for g in (games[1] if len(games) > 1 and games[1] else [])]

        # In-game points are tennis strings and entries CAN be null.
        pts = score.get("points") or []
        game_a = pts[0] if len(pts) > 0 and pts[0] else "0"
        game_b = pts[1] if len(pts) > 1 and pts[1] else "0"

        srv = score.get("server")
        status = _STATUS.get(m.get("status"), "scheduled")
        return LiveScore(
            sets_a=sets_a, sets_b=sets_b,
            game_a=str(game_a), game_b=str(game_b),
            server="b" if srv == 2 else "a",
            status=status,
            winner=_winner(m) if status == "finished" else None,
        )

    def final_results(self, day):
        """{match_id: (status, winner)} for one day, used by live.py to
        reconcile matches the live feed dropped before they were marked
        finished. /history/matches is date-exact, which is exactly what this
        needs (it carries no tour filter, but no tier is needed here)."""
        d = day.strftime("%Y-%m-%d") if hasattr(day, "strftime") else str(day)
        out = {}
        for m in self._pages("/history/matches", **{"from": d, "to": d}):
            mid = m.get("id")
            if mid is None:
                continue
            status = _STATUS.get(m.get("status"), "scheduled")
            out[str(mid)] = (status, _winner(m) if status == "finished" else None)
        return out

    # ---- stats -----------------------------------------------------------

    def _statistics(self, provider_match_id):
        """Single-match route: the MatchStatistics object itself, unenveloped.
        `statistics` is the entitlement bucket (ULTRA), so one 403 is enough to
        stop asking for every subsequent match."""
        body = self._get(f"/matches/{provider_match_id}/statistics", _kind="statistics")
        return body if isinstance(body, dict) else {}

    def get_match_stats(self, provider_match_id):
        """In-match serve/return stats. Needs ULTRA; below that the request is
        refused and this returns an empty MatchStats, which base.py documents as
        'not available' and the UI already renders gracefully."""
        stats = self._statistics(provider_match_id)
        players = (stats or {}).get("players") or {}
        if not players or (stats or {}).get("coverage") == "none":
            return MatchStats()
        return MatchStats(player_a=_player_stats(players.get("p1")),
                          player_b=_player_stats(players.get("p2")))

    def match_statistics(self, match_id):
        """Serve/return detail sheet for ONE match, for /api/tennis/match-detail.
        Mirrors the api-tennis provider's envelope so the route's consumers see
        a familiar shape."""
        m = self._match(match_id, refresh=False) or {}
        stats = self._statistics(match_id)
        players = (stats or {}).get("players") or {}
        p1, p2 = _side(m, "p1"), _side(m, "p2")
        srv = ((m.get("score") or {}).get("server"))
        out = {
            "match_key": str(match_id),
            "live": m.get("status") == "live",
            "status": m.get("event_status") or m.get("status"),
            "score": _scoreline(m),
            "serving": None if srv not in (1, 2) else ("First Player" if srv == 1 else "Second Player"),
            "tournament": m.get("tournament"),
            "round": m.get("round"),
            "p1": {"name": p1.get("name"), "key": str(p1.get("id") or ""),
                   "stats": _flat_stats(players.get("p1"))},
            "p2": {"name": p2.get("name"), "key": str(p2.get("id") or ""),
                   "stats": _flat_stats(players.get("p2"))},
            # Only this feed can say how complete an in-play stat line is; pass
            # it through so the sheet can caveat a partially-covered match.
            "coverage": (stats or {}).get("coverage"),
            "games_counted": (stats or {}).get("games_counted"),
        }
        out["has_stats"] = bool(out["p1"]["stats"] or out["p2"]["stats"])
        return out


def _player_stats(side):
    """MatchStatisticsSide -> base.PlayerStats. Measured fields are OMITTED when
    the upstream match doesn't carry them (never zero-filled), so a missing key
    stays None, i.e. 'not available' -- exactly what base.PlayerStats means."""
    if not side:
        return None
    m = side.get("measured") or {}
    return PlayerStats(
        aces=m.get("aces"),
        double_faults=m.get("double_faults"),
        first_serve_pct=_pct01(m.get("first_serves_in_pct")),
        first_serve_won_pct=_pct01(m.get("first_serve_points_won_pct")),
        second_serve_won_pct=_pct01(m.get("second_serve_points_won_pct")),
        break_points_won=side.get("break_points_converted"),
        break_points_faced=side.get("break_points_faced"),
        total_points_won=side.get("points_won"),
    )


def _flat_stats(side):
    """{stat_name: {value, won, total}} for the detail sheet."""
    if not side:
        return {}
    out = {}
    for name, value in (side.get("measured") or {}).items():
        if value is None or name.endswith(("_of", "_pct")):
            continue
        entry = {"type": "measured", "value": value, "won": value, "total": None}
        of = (side.get("measured") or {}).get(name + "_of")
        if of is not None:
            entry["total"] = of
        out[name] = entry
    for name in ("hold_pct", "break_pct", "break_points_saved_pct",
                 "break_points_converted_pct", "service_points_won_pct",
                 "return_points_won_pct"):
        if side.get(name) is not None:
            out[name] = {"type": "derived", "value": side[name], "won": None, "total": None}
    return out


def _scoreline(match):
    """'6-4 3-6 2-1' from the per-set games, or None when the match carries no
    games (which a completed match legitimately can). Never synthesised."""
    games = (match.get("score") or {}).get("games") or []
    if len(games) < 2 or not games[0]:
        return None
    return " ".join(f"{a}-{b}" for a, b in zip(games[0], games[1]))
