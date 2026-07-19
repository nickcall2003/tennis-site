import os
import asyncio
import logging
from datetime import datetime, timezone, time as dtime

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("linelogic-bot")

# ---------- Config ----------

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

# TODO: replace with your real FastAPI base URL and swap the paths in each
# fetch_json(...) call below once you send over your actual route list.
LINE_LOGIC_API_BASE = os.environ.get("LINE_LOGIC_API_BASE", "https://api.thelinelogic.com")
LINE_LOGIC_API_KEY = os.environ.get("LINE_LOGIC_API_KEY", "")

WEBHOOK_SHARED_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET", "")
PORT = int(os.environ.get("PORT", "8080"))

# v1 channels only — free tier
CHANNEL_IDS = {
    "daily_model": int(os.environ.get("CHANNEL_DAILY_MODEL", "0")),
    "official_results": int(os.environ.get("CHANNEL_OFFICIAL_RESULTS", "0")),
}

WELCOME_CHANNEL_ID = int(os.environ.get("CHANNEL_WELCOME", "0"))
BOT_LOGS_CHANNEL_ID = int(os.environ.get("CHANNEL_BOT_LOGS", "0"))
VERIFIED_ROLE_ID = int(os.environ.get("VERIFIED_ROLE_ID", "0"))
TICKETS_CHANNEL_ID = int(os.environ.get("CHANNEL_TICKETS", "0"))
EDGE_ALERTS_CHANNEL_ID = int(os.environ.get("CHANNEL_EDGE_ALERTS", "0"))
WEBSITE_UPDATES_CHANNEL_ID = int(os.environ.get("CHANNEL_WEBSITE_UPDATES", "0"))
OWNER_USER_ID = int(os.environ.get("OWNER_USER_ID", "0"))

# AI explain layer. Dormant until your backend has the /api/explain route live
# (which is where the LLM key lives — the bot never calls an LLM directly, it just
# asks your backend). Flip to "1" once that endpoint is deployed.
AI_EXPLAIN_ENABLED = os.environ.get("AI_EXPLAIN_ENABLED", "0").strip() == "1"

# Notify role IDs — bot @-mentions these when posting, instead of pinging everyone.
# Right-click each role in Server Settings -> Roles -> Copy Role ID.
NOTIFY_ROLE_IDS = {
    "mlb": int(os.environ.get("ROLE_MLB_ALERTS", "0")),
    "nba": int(os.environ.get("ROLE_NBA_ALERTS", "0")),
    "nfl": int(os.environ.get("ROLE_NFL_ALERTS", "0")),
    "nhl": int(os.environ.get("ROLE_NHL_ALERTS", "0")),
    "ncaaf": int(os.environ.get("ROLE_NCAAF_ALERTS", "0")),  # College Football
    "ncaab": int(os.environ.get("ROLE_NCAAB_ALERTS", "0")),  # College Basketball
    "wnba": int(os.environ.get("ROLE_WNBA_ALERTS", "0")),
    "tennis": int(os.environ.get("ROLE_TENNIS_ALERTS", "0")),
    "soccer": int(os.environ.get("ROLE_SOCCER_ALERTS", "0")),
    "ufc": int(os.environ.get("ROLE_UFC_ALERTS", "0")),
    "stocks": int(os.environ.get("ROLE_STOCK_ALERTS", "0")),
    "line_movement": int(os.environ.get("ROLE_LINE_MOVEMENT_ALERTS", "0")),
    "ladder": int(os.environ.get("ROLE_LADDER_ALERTS", "0")),
}

BRAND_COLOR = int(os.environ.get("BRAND_COLOR_HEX", "2B6CB0"), 16)

# --- Premium / Whop — NOT active in v1. Leave these unset until you're ready. ---
# When you do activate: set PREMIUM_ROLE_ID, uncomment the /premium/webhook route
# near the bottom, and enable Whop's native Discord role sync (no code needed
# on your end for the actual billing — Whop handles grant/revoke itself; the
# webhook below is only a fallback if you ever want custom control).
PREMIUM_ROLE_ID = int(os.environ.get("PREMIUM_ROLE_ID", "0"))

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Tappable dropdown of the sports your backend supports (from SPORT_SEASON in
# your main.py). Discord shows these as buttons/menu on mobile — no typing, no
# typos, and no invalid sports reaching the API. Add/remove a line here if your
# supported sports change. (Max 25 choices; we're well under.)
SPORT_CHOICES = [
    app_commands.Choice(name="MLB", value="mlb"),
    app_commands.Choice(name="NBA", value="nba"),
    app_commands.Choice(name="NFL", value="nfl"),
    app_commands.Choice(name="NHL", value="nhl"),
    app_commands.Choice(name="WNBA", value="wnba"),
    app_commands.Choice(name="Tennis", value="tennis"),
    app_commands.Choice(name="Soccer", value="soccer"),
    app_commands.Choice(name="UFC", value="ufc"),
    app_commands.Choice(name="NCAA Football", value="ncaaf"),
    app_commands.Choice(name="NCAA Basketball", value="ncaab"),
    app_commands.Choice(name="NCAA Baseball", value="ncaabb"),
]

# Props only exist for these sports in your backend (/api/{sport}/props/... and
# /api/mlb/props/...). Keep this list in sync with team_props()/mlb_props().
PROP_SPORT_CHOICES = [
    app_commands.Choice(name="MLB", value="mlb"),
    app_commands.Choice(name="NBA", value="nba"),
    app_commands.Choice(name="WNBA", value="wnba"),
    app_commands.Choice(name="NFL", value="nfl"),
]


# ---------- Helpers ----------

async def fetch_json(session: aiohttp.ClientSession, path: str, params: dict | None = None, timeout: int = 10):
    url = f"{LINE_LOGIC_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {LINE_LOGIC_API_KEY}"} if LINE_LOGIC_API_KEY else {}
    async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        resp.raise_for_status()
        return await resp.json()


def now_utc():
    return datetime.now(timezone.utc)


def role_mention(sport_key: str) -> str | None:
    role_id = NOTIFY_ROLE_IDS.get(sport_key)
    return f"<@&{role_id}>" if role_id else None


# ---------- Daily auto-post ----------
# Posts the day's card to #daily-model on a schedule. Runs entirely from the bot
# (no backend webhook needed) so there's one place to look when something breaks.

DAILY_POST_HOUR_UTC = int(os.environ.get("DAILY_POST_HOUR_UTC", "15"))   # 15:00 UTC ≈ 10am CT
DAILY_MIN_EDGE = float(os.environ.get("DAILY_MIN_EDGE", "3"))           # % edge to qualify
DAILY_MAX_PLAYS = int(os.environ.get("DAILY_MAX_PLAYS", "5"))
DAILY_POST_ENABLED = os.environ.get("DAILY_POST_ENABLED", "1") == "1"

SPORT_LABEL_MAP = {
    "mlb": ("⚾", "Baseball", "games"), "nba": ("🏀", "Basketball", "games"),
    "wnba": ("👟", "WNBA", "games"), "nfl": ("🏈", "Football", "games"),
    "nhl": ("🏒", "Hockey", "games"), "ncaaf": ("🏟️", "College Football", "games"),
    "ncaab": ("🎓", "College Basketball", "games"), "soccer": ("⚽", "Soccer", "matches"),
    "tennis": ("🎾", "Tennis", "matches"), "ufc": ("🥊", "UFC", "fights"),
    "golf": ("⛳", "Golf", "events"),
}


async def build_daily_card():
    """Returns (embed, mention_string). Always returns an embed — if the model
    found no qualifying edges we say so plainly rather than posting nothing."""
    async with aiohttp.ClientSession() as session:
        slate = {}
        try:
            slate = await fetch_json(session, "/api/slate", timeout=25)
        except Exception:
            log.warning("daily card: slate fetch failed")
        try:
            data = await fetch_json(session, "/api/picks/quick", timeout=25)
        except Exception:
            log.exception("daily card: picks fetch failed")
            return None, None

    picks = data.get("picks", []) or []
    edges = [p for p in picks
             if isinstance(p.get("edge_pct"), (int, float))
             and p["edge_pct"] >= DAILY_MIN_EDGE
             and p.get("market_odds") is not None]
    edges.sort(key=lambda p: p["edge_pct"], reverse=True)
    edges = edges[:DAILY_MAX_PLAYS]

    counts = (slate.get("counts") or {})
    slate_bits = []
    for key, (emoji, label, unit) in SPORT_LABEL_MAP.items():
        n = counts.get(key, 0)
        if n:
            slate_bits.append(f"{emoji} {label}: **{n}** {unit}")

    if edges:
        embed = discord.Embed(
            title="📈 Today's Line Logic Model",
            description=f"Top {len(edges)} value play{'s' if len(edges) != 1 else ''} the model found today.",
            color=BRAND_COLOR,
            timestamp=now_utc(),
        )
        for p in edges:
            prob_pct = round((p.get("prob") or 0) * 100)
            edge = p.get("edge_pct")
            embed.add_field(
                name=f"{p.get('pick','—')}  ({str(p.get('sport','')).upper()})",
                value=(f"{p.get('match','')}\n"
                       f"Model: **{prob_pct}%** • Market: {_fmt_odds(p.get('market_odds'))} "
                       f"• Edge: **+{edge:.1f}%**"),
                inline=False,
            )
    else:
        embed = discord.Embed(
            title="📈 Today's Line Logic Model",
            description=("No qualifying edges today — the model didn't find value "
                         "worth posting at current prices. No play is better than a bad play."),
            color=BRAND_COLOR,
            timestamp=now_utc(),
        )

    if slate_bits:
        embed.add_field(name="On the board today", value="\n".join(slate_bits), inline=False)
    embed.add_field(
        name="Want the reasoning?",
        value="`/why [team]` for the model's logic • `/model [team]` for any team's read",
        inline=False,
    )
    embed.set_footer(text="Value plays, not guarantees • thelinelogic.com")

    mentions = []
    seen = set()
    for p in edges:
        sp = str(p.get("sport", "")).lower()
        if sp and sp not in seen:
            m = role_mention(sp)
            if m:
                mentions.append(m)
            seen.add(sp)
    return embed, (" ".join(mentions) if mentions else None)


async def post_daily_card() -> tuple[bool, str]:
    """Returns (ok, reason). Reason is surfaced to staff so failures are obvious."""
    channel_id = CHANNEL_IDS.get("daily_model")
    if not channel_id:
        return False, "CHANNEL_DAILY_MODEL isn't set in the bot's environment."
    channel = bot.get_channel(channel_id)
    if channel is None:
        # not in cache — fetch it directly (also surfaces permission problems)
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.Forbidden:
            return False, f"No access to channel `{channel_id}` — check LineBot's View Channel / Send Messages permission there."
        except discord.NotFound:
            return False, f"Channel `{channel_id}` doesn't exist — is CHANNEL_DAILY_MODEL the right ID?"
        except Exception as e:
            return False, f"Couldn't load channel `{channel_id}`: {e}"
    embed, mentions = await build_daily_card()
    if embed is None:
        return False, "Couldn't reach the model (the /api/picks/quick call failed)."
    try:
        await channel.send(content=mentions or None, embed=embed)
    except discord.Forbidden:
        return False, f"Can't post in <#{channel_id}> — LineBot needs Send Messages + Embed Links there."
    except Exception as e:
        return False, f"Send failed: {e}"
    await bot_log("Posted the daily model card.")
    return True, "posted"


@tasks.loop(time=dtime(hour=DAILY_POST_HOUR_UTC, minute=0, tzinfo=timezone.utc))
async def daily_post_loop():
    if not DAILY_POST_ENABLED:
        return
    try:
        ok, reason = await post_daily_card()
        if not ok:
            log.warning("daily post skipped: %s", reason)
            await bot_log(f"Daily post skipped — {reason}")
    except Exception:
        log.exception("daily post loop failed")


@daily_post_loop.before_loop
async def _before_daily_post():
    await bot.wait_until_ready()


# ---------- Weekly capper leaderboard + Top Capper role ----------

WEEKLY_POST_HOUR_UTC = int(os.environ.get("WEEKLY_POST_HOUR_UTC", "16"))  # ~11am CT
WEEKLY_POST_WEEKDAY = int(os.environ.get("WEEKLY_POST_WEEKDAY", "0"))     # 0=Monday
TOP_CAPPER_ROLE_ID = int(os.environ.get("TOP_CAPPER_ROLE_ID", "0"))
CAPPER_CHANNEL_ID = int(os.environ.get("CHANNEL_CAPPERS", "0"))
WEEKLY_POST_ENABLED = os.environ.get("WEEKLY_POST_ENABLED", "1") == "1"


async def _sync_top_capper_role(top_user_id: str | None):
    """Give the Top Capper role to the current leader, remove it from everyone
    else. Silently does nothing if TOP_CAPPER_ROLE_ID isn't configured."""
    if not TOP_CAPPER_ROLE_ID or not top_user_id:
        return
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    role = guild.get_role(TOP_CAPPER_ROLE_ID)
    if role is None:
        log.warning("top capper: role %s not found", TOP_CAPPER_ROLE_ID)
        return
    try:
        for member in list(role.members):
            if str(member.id) != str(top_user_id):
                await member.remove_roles(role, reason="No longer top capper")
        winner = guild.get_member(int(top_user_id))
        if winner and role not in winner.roles:
            await winner.add_roles(role, reason="Top capper this week")
    except discord.Forbidden:
        log.warning("top capper: missing Manage Roles or role is above LineBot")
    except Exception:
        log.exception("top capper role sync failed")


async def post_capper_leaderboard() -> tuple[bool, str]:
    channel_id = CAPPER_CHANNEL_ID
    if not channel_id:
        return False, ("CHANNEL_CAPPERS isn't set — the leaderboard posts to its own "
                       "channel, so set that variable to the capper channel ID.")
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            return False, f"Couldn't load channel `{channel_id}`: {e}"

    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/capper/leaderboard",
                                    params={"sort": "units"}, timeout=25)
        except Exception:
            log.exception("weekly leaderboard fetch failed")
            return False, "Couldn't reach the leaderboard."

    cappers = data.get("cappers", []) or []
    if not cappers:
        building = data.get("building", 0)
        embed = discord.Embed(
            title="🏆 Capper Leaderboard",
            description=("No graded records yet this week. Track your plays with "
                         "`/track [team]` and you'll show up here once they settle."
                         + (f"\n\n{building} capper{'s' if building != 1 else ''} building a record."
                            if building else "")),
            color=BRAND_COLOR, timestamp=now_utc(),
        )
        await channel.send(embed=embed)
        return True, "posted (empty)"

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, c in enumerate(cappers[:10]):
        rank = medals[i] if i < 3 else f"**{i+1}.**"
        u = c.get("units_pl", 0)
        roi = c.get("roi_pct")
        roi_s = f" · {roi:+.1f}% ROI" if isinstance(roi, (int, float)) else ""
        lines.append(f"{rank} **{c.get('username','capper')}** — {u:+.2f}u ({c.get('record','0-0')}){roi_s}")

    embed = discord.Embed(
        title="🏆 Capper Leaderboard",
        description="\n".join(lines),
        color=BRAND_COLOR, timestamp=now_utc(),
    )
    leader = cappers[0]
    embed.add_field(
        name="Leader",
        value=f"**{leader.get('username','capper')}** — {leader.get('units_pl',0):+.2f}u",
        inline=False,
    )
    embed.add_field(name="Track your own", value="`/track [team]` • `/mystats`", inline=False)
    embed.set_footer(text="Ranked by units won • Line Logic")
    await channel.send(embed=embed)
    # the room's combined record posts alongside the individual leaderboard
    room = await build_room_embed(7)
    if room is not None:
        try:
            await channel.send(embed=room)
        except Exception:
            log.warning("room embed send failed")
    await _sync_top_capper_role(leader.get("user_id"))
    return True, "posted"


@tasks.loop(time=dtime(hour=WEEKLY_POST_HOUR_UTC, minute=0, tzinfo=timezone.utc))
async def weekly_capper_loop():
    if not WEEKLY_POST_ENABLED:
        return
    if datetime.now(timezone.utc).weekday() != WEEKLY_POST_WEEKDAY:
        return
    try:
        ok, reason = await post_capper_leaderboard()
        if not ok:
            log.warning("weekly leaderboard skipped: %s", reason)
    except Exception:
        log.exception("weekly leaderboard loop failed")


@weekly_capper_loop.before_loop
async def _before_weekly_capper():
    await bot.wait_until_ready()


# ---------- Ladder Challenge daily post ----------

LADDER_POST_ENABLED = os.environ.get("LADDER_POST_ENABLED", "1") == "1"
LADDER_POST_HOUR_UTC = int(os.environ.get("LADDER_POST_HOUR_UTC", "15"))
LADDER_CHANNEL_ID = int(os.environ.get("CHANNEL_LADDER", "0"))


async def build_ladder_embed():
    """Returns (embed, mention) for the Ladder Challenge, or (None, None)."""
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/ladder/status",
                                    params={"history": 5}, timeout=25)
        except Exception:
            log.exception("ladder status fetch failed")
            return None, None

    st = data.get("state") or {}
    leg = data.get("current_leg") or {}
    hist = data.get("history") or []

    rung = st.get("rung", 1)
    bankroll = st.get("bankroll", 0)
    attempt = st.get("attempt", 1)

    embed = discord.Embed(
        title="🪜 The Ladder Challenge",
        description=(f"**Rung {rung} of 10** · Bankroll **${bankroll:,.2f}** · Run #{attempt}\n"
                     "Roll a $10 bankroll through 10 straight winners. One loss resets to rung 1."),
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )

    if leg:
        odds_s = _fmt_odds(leg.get("odds"))
        edge = leg.get("edge_pct")
        edge_s = f" • Edge: **+{edge:.1f}%**" if isinstance(edge, (int, float)) else ""
        embed.add_field(
            name=f"Today's Leg — Rung {leg.get('rung', rung)}",
            value=(f"**{leg.get('pick','—')}** ({str(leg.get('sport','')).upper()})\n"
                   f"Risk **${leg.get('stake',0):,.2f}** at {odds_s} "
                   f"to return **${leg.get('to_return',0):,.2f}**{edge_s}"),
            inline=False,
        )
    else:
        embed.add_field(
            name="Today's Leg",
            value="Not posted yet — check back once the model locks today's pick.",
            inline=False,
        )

    if hist:
        marks = []
        for h in hist[:5]:
            r = (h.get("result") or "").lower()
            marks.append("🟢" if r == "win" else "🔴" if r == "loss" else "⚪")
        embed.add_field(name="Last 5 legs", value=" ".join(marks), inline=True)

    best_r = st.get("best_rung_ever")
    if best_r:
        embed.add_field(
            name="Best run",
            value=f"Rung {best_r} · ${st.get('best_bankroll_ever', 0):,.2f}",
            inline=True,
        )
    if st.get("completed_runs"):
        embed.add_field(name="Completed runs", value=str(st["completed_runs"]), inline=True)

    embed.set_footer(text="One bet at a time • not a guarantee • thelinelogic.com")

    # The ladder has its own opt-in role so it doesn't double-ping sport followers
    mention = role_mention("ladder")
    return embed, mention


async def post_ladder_card() -> tuple[bool, str]:
    channel_id = LADDER_CHANNEL_ID
    if not channel_id:
        return False, ("CHANNEL_LADDER isn't set — the ladder posts to its own "
                       "channel, so set that variable to the Ladder Challenge channel ID.")
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            return False, f"Couldn't load channel `{channel_id}`: {e}"
    embed, mention = await build_ladder_embed()
    if embed is None:
        return False, "Couldn't reach the ladder status endpoint."
    try:
        await channel.send(content=mention or None, embed=embed)
    except discord.Forbidden:
        return False, f"Can't post in <#{channel_id}> — needs Send Messages + Embed Links."
    except Exception as e:
        return False, f"Send failed: {e}"
    return True, "posted"


@tasks.loop(time=dtime(hour=LADDER_POST_HOUR_UTC, minute=10, tzinfo=timezone.utc))
async def ladder_post_loop():
    if not LADDER_POST_ENABLED:
        return
    try:
        ok, reason = await post_ladder_card()
        if not ok:
            log.warning("ladder post skipped: %s", reason)
    except Exception:
        log.exception("ladder post loop failed")


@ladder_post_loop.before_loop
async def _before_ladder_post():
    await bot.wait_until_ready()


# ---------- Official results recap ----------
# PickResult stores game refs, not team names, so the recap is an honest
# aggregate (record / units / ROI per sport) rather than a game-by-game list.

RESULTS_POST_ENABLED = os.environ.get("RESULTS_POST_ENABLED", "1") == "1"
RESULTS_POST_HOUR_UTC = int(os.environ.get("RESULTS_POST_HOUR_UTC", "14"))  # ~9am CT


async def build_results_embed(days: int = 1):
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/accuracy",
                                    params={"days": days}, timeout=25)
        except Exception:
            log.exception("results recap fetch failed")
            return None

    ov = data.get("overall", {}) or {}
    by_sport = data.get("by_sport", {}) or {}
    units = ov.get("units_30d")
    roi = ov.get("roi_30d")
    priced = ov.get("priced_30d", 0)

    window = "Yesterday" if days == 1 else f"Last {days} days"
    embed = discord.Embed(
        title=f"📊 Official Results — {window}",
        description=("Every graded pick, win or lose. Units count only recommended "
                     "+EV wagers.\n\n"
                     + (f"**{units:+.2f}u** on {priced} graded wager(s)"
                        + (f" · **{roi:+.1f}% ROI**" if isinstance(roi, (int, float)) else "")
                        if isinstance(units, (int, float)) and priced
                        else "No priced wagers settled in this window.")),
        color=BRAND_COLOR, timestamp=now_utc(),
    )

    lines = []
    for sp, s in sorted(by_sport.items(),
                        key=lambda kv: (kv[1].get("units_30d") or 0), reverse=True):
        emoji, label, _ = SPORT_LABEL_MAP.get(sp, ("", sp.upper(), ""))
        u = s.get("units_30d")
        n = s.get("priced_30d", 0)
        if n and isinstance(u, (int, float)):
            lines.append(f"{emoji} **{label}** — {u:+.2f}u on {n} wager(s)")
    if lines:
        embed.add_field(name="By sport", value="\n".join(lines[:8]), inline=False)

    at_w, at_l = ov.get("alltime_wins", 0), ov.get("alltime_losses", 0)
    if at_w or at_l:
        embed.add_field(
            name="All-time",
            value=f"{at_w}-{at_l} ({ov.get('alltime_pct','—')}%)",
            inline=False,
        )
    embed.set_footer(text="Full track record • thelinelogic.com")
    return embed


async def post_results_recap() -> tuple[bool, str]:
    channel_id = CHANNEL_IDS.get("official_results")
    if not channel_id:
        return False, "CHANNEL_OFFICIAL_RESULTS isn't set."
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            return False, f"Couldn't load channel `{channel_id}`: {e}"
    embed = await build_results_embed(1)
    if embed is None:
        return False, "Couldn't reach the results endpoint."
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        return False, f"Can't post in <#{channel_id}> — needs Send Messages + Embed Links."
    return True, "posted"


@tasks.loop(time=dtime(hour=RESULTS_POST_HOUR_UTC, minute=0, tzinfo=timezone.utc))
async def results_post_loop():
    if not RESULTS_POST_ENABLED:
        return
    try:
        ok, reason = await post_results_recap()
        if not ok:
            log.warning("results recap skipped: %s", reason)
    except Exception:
        log.exception("results recap loop failed")


@results_post_loop.before_loop
async def _before_results_post():
    await bot.wait_until_ready()


# ---------- Edge alerts ----------
# Scans the board periodically and posts NEW plays above a threshold, pinging
# that sport's role. Dedupes in memory so the same play isn't posted twice a day.

EDGE_ALERTS_ENABLED = os.environ.get("EDGE_ALERTS_ENABLED", "1") == "1"
EDGE_ALERT_MIN = float(os.environ.get("EDGE_ALERT_MIN", "6"))       # % edge to fire
EDGE_ALERT_INTERVAL_MIN = int(os.environ.get("EDGE_ALERT_INTERVAL_MIN", "90"))
# Low-tier tennis (ITF futures, Challengers) has thin, stale lines — big "edges"
# there are usually data noise rather than real value, and most books barely
# offer them. Excluded from alerts by default; override with EDGE_EXCLUDE_TIERS.
EDGE_EXCLUDE_TIERS = {
    t.strip().upper()
    for t in os.environ.get("EDGE_EXCLUDE_TIERS", "ITF,CHALLENGER").split(",")
    if t.strip()
}
_edge_seen: dict[str, set] = {}


def _edge_key(p) -> str:
    return f"{p.get('sport')}|{p.get('match')}|{p.get('pick')}"


async def scan_edge_alerts() -> int:
    """Post any qualifying plays we haven't already alerted on today."""
    if not EDGE_ALERTS_CHANNEL_ID:
        return 0
    channel = bot.get_channel(EDGE_ALERTS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(EDGE_ALERTS_CHANNEL_ID)
        except Exception:
            return 0

    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/picks/quick", timeout=25)
        except Exception:
            log.warning("edge alert scan: board fetch failed")
            return 0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    seen = _edge_seen.setdefault(today, set())
    for k in list(_edge_seen.keys()):   # drop previous days so the dict stays small
        if k != today:
            _edge_seen.pop(k, None)

    posted = 0
    skipped_tier = 0
    for p in (data.get("picks") or []):
        edge = p.get("edge_pct")
        if not isinstance(edge, (int, float)) or edge < EDGE_ALERT_MIN:
            continue
        if p.get("market_odds") is None:
            continue
        tier = str(p.get("tier") or "").strip().upper()
        if tier and tier in EDGE_EXCLUDE_TIERS:
            skipped_tier += 1
            continue
        key = _edge_key(p)
        if key in seen:
            continue
        prob_pct = round((p.get("prob") or 0) * 100)
        embed = discord.Embed(
            title=f"⚡ Edge Alert — {p.get('pick','—')}",
            description=p.get("match", ""),
            color=0x2ECC71, timestamp=now_utc(),
        )
        embed.add_field(name="Sport", value=str(p.get("sport", "")).upper(), inline=True)
        embed.add_field(name="Model", value=f"{prob_pct}%", inline=True)
        embed.add_field(name="Market", value=_fmt_odds(p.get("market_odds")), inline=True)
        embed.add_field(name="Fair", value=_fmt_odds(p.get("fair_odds")), inline=True)
        embed.add_field(name="Edge", value=f"**+{edge:.1f}%**", inline=True)
        if p.get("event_time"):
            embed.add_field(name="Start", value=str(p["event_time"]), inline=True)
        embed.set_footer(text="Value play, not a guarantee • thelinelogic.com")
        mention = role_mention(str(p.get("sport", "")).lower())
        try:
            await channel.send(content=mention or None, embed=embed)
            seen.add(key)
            posted += 1
            await asyncio.sleep(1)
        except Exception:
            log.warning("edge alert send failed", exc_info=True)
    if posted or skipped_tier:
        note = f"Posted {posted} edge alert(s)."
        if skipped_tier:
            note += f" Skipped {skipped_tier} low-tier tennis play(s)."
        await bot_log(note)
    return posted


@tasks.loop(minutes=EDGE_ALERT_INTERVAL_MIN)
async def edge_alert_loop():
    if not EDGE_ALERTS_ENABLED:
        return
    try:
        await scan_edge_alerts()
    except Exception:
        log.exception("edge alert loop failed")


@edge_alert_loop.before_loop
async def _before_edge_alerts():
    await bot.wait_until_ready()


# ---------- Slash commands ----------

def _fmt_odds(v):
    """American odds as a display string (+150 / -164 / —)."""
    if v is None:
        return "—"
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return str(v)
    return f"+{v}" if v > 0 else str(v)


def _pick_embed(p: dict) -> discord.Embed:
    """Build an embed from a pick object as returned by /api/picks/best and
    /api/picks/free (keys: sport, match, pick, prob 0-1, confidence,
    market_odds, fair_odds, edge_pct)."""
    prob_pct = round((p.get("prob") or 0) * 100)
    edge = p.get("edge_pct")
    edge_str = f"+{edge:.1f}%" if isinstance(edge, (int, float)) else "—"

    embed = discord.Embed(
        title=f"📊 {p.get('pick', 'Pick')}",
        description=p.get("match", ""),
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.add_field(name="Sport", value=str(p.get("sport", "—")).upper(), inline=True)
    embed.add_field(name="Model Win %", value=f"{prob_pct}%", inline=True)
    embed.add_field(name="Confidence", value=str(p.get("confidence", "—")), inline=True)
    embed.add_field(name="Market", value=_fmt_odds(p.get("market_odds")), inline=True)
    embed.add_field(name="Fair Odds", value=_fmt_odds(p.get("fair_odds")), inline=True)
    embed.add_field(name="Edge", value=edge_str, inline=True)
    embed.set_footer(text="Line Logic Model • thelinelogic.com")
    return embed


@bot.tree.command(name="model", description="The Line Logic model's read on any team — edge or not")
@app_commands.describe(
    name="Team or player, e.g. Braves",
    sport="Optional: pick a sport to narrow the search",
)
@app_commands.choices(sport=SPORT_CHOICES)
async def model_command(interaction: discord.Interaction, name: str,
                        sport: app_commands.Choice[str] | None = None):
    await interaction.response.defer()
    params = {"team": name}
    if sport:
        params["sport"] = sport.value
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/model", params=params)
        except Exception:
            log.exception("model lookup failed for %s", name)
            await interaction.followup.send("Couldn't reach the model right now — try again shortly.")
            return

    if not data.get("found"):
        await interaction.followup.send(
            f"No upcoming game found for **{name}** in the next few days. "
            f"Check the spelling, or see the full board at thelinelogic.com"
        )
        return

    edge = data.get("edge_pct")
    has_edge = data.get("has_edge")
    win_pct = data.get("win_pct", round((data.get("prob") or 0) * 100))

    if has_edge:
        color = 0x2ECC71  # green — there's an edge
        headline = f"✅ Edge found: **+{edge:.1f}%**"
    elif edge is not None:
        color = 0xE67E22  # amber — priced, no edge
        headline = "⚖️ No edge — the market price is fair or against this side."
    else:
        color = BRAND_COLOR  # no market line captured yet
        headline = "ℹ️ No market line captured yet, so edge can't be computed."

    embed = discord.Embed(
        title=f"📊 {data.get('pick', name)}",
        description=f"{data.get('match', '')}\n{headline}",
        color=color,
        timestamp=now_utc(),
    )
    embed.add_field(name="Sport", value=str(data.get("sport", "—")).upper(), inline=True)
    embed.add_field(name="Model Win %", value=f"{win_pct}%", inline=True)
    embed.add_field(name="Confidence", value=str(data.get("confidence", "—")), inline=True)
    embed.add_field(name="Market", value=_fmt_odds(data.get("market_odds")), inline=True)
    embed.add_field(name="Fair Odds", value=_fmt_odds(data.get("fair_odds")), inline=True)
    embed.add_field(
        name="Edge",
        value=(f"+{edge:.1f}%" if isinstance(edge, (int, float)) else "—"),
        inline=True,
    )
    if data.get("date"):
        embed.set_footer(text=f"Game {data['date']} • Line Logic Model • thelinelogic.com")
    else:
        embed.set_footer(text="Line Logic Model • thelinelogic.com")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="today", description="Today's slate — how many games/matches are on the board per sport")
async def today_command(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/slate", timeout=25)
        except Exception:
            log.exception("today slate lookup failed")
            await interaction.followup.send("Couldn't reach the model right now — try again shortly.")
            return

    counts = data.get("counts", {}) or {}
    if not counts:
        await interaction.followup.send("Nothing on the board today yet.")
        return

    # Display names + the unit each sport is counted in, in a sensible order
    SPORT_DISPLAY = [
        ("mlb", "⚾ Baseball", "games"),
        ("nba", "🏀 Basketball", "games"),
        ("wnba", "👟 WNBA", "games"),
        ("nfl", "🏈 Football", "games"),
        ("ncaaf", "🏟️ College Football", "games"),
        ("ncaab", "🎓 College Basketball", "games"),
        ("nhl", "🏒 Hockey", "games"),
        ("soccer", "⚽ Soccer", "matches"),
        ("tennis", "🎾 Tennis", "matches"),
        ("ufc", "🥊 UFC", "fights"),
        ("golf", "⛳ Golf", "events"),
    ]

    lines = []
    listed = set()
    for key, label, unit in SPORT_DISPLAY:
        n = counts.get(key, 0)
        if n:
            lines.append(f"{label}: **{n}** {unit}")
            listed.add(key)
    # catch any sport not in the display list
    for key, n in counts.items():
        if key not in listed and n:
            lines.append(f"{key.upper()}: **{n}**")

    total = data.get("total", sum(counts.values()))

    embed = discord.Embed(
        title="📅 Today's Line Logic Slate",
        description="\n".join(lines),
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.add_field(name="Total on the board", value=f"**{total}**", inline=False)
    embed.add_field(
        name="Want the plays?",
        value="Use `/model [team]` for a specific read, or check the daily model feed for the top edges.",
        inline=False,
    )
    embed.set_footer(text="Line Logic • thelinelogic.com")
    await interaction.followup.send(embed=embed)


def _prop_stat(p: dict) -> str:
    return str(p.get("stat") or p.get("label") or "").strip()


def _prop_edge(p: dict):
    """Signed gap between the model's projection and the book line. Positive =
    model projects OVER the line, negative = UNDER. None if either is missing."""
    proj, line = p.get("projection"), p.get("line")
    try:
        return float(proj) - float(line)
    except (TypeError, ValueError):
        return None


async def _games_for_sport(session, sport: str, ids_only: bool = False):
    path = "/api/mlb/games" if sport == "mlb" else f"/api/{sport}/games"
    data = await fetch_json(session, path, timeout=25)
    games = data if isinstance(data, list) else (data.get("games") or [])
    return games


@bot.tree.command(name="prop", description="Today's biggest player-prop edges from the Line Logic model")
@app_commands.describe(
    sport="Pick a sport",
    player="Optional: filter to one player, e.g. Judge",
)
@app_commands.choices(sport=PROP_SPORT_CHOICES)
async def prop_command(interaction: discord.Interaction,
                       sport: app_commands.Choice[str],
                       player: str = ""):
    await interaction.response.defer()
    sport_val = sport.value
    needle = player.lower().strip()

    async with aiohttp.ClientSession() as session:
        try:
            games = await _games_for_sport(session, sport_val)
        except Exception:
            log.exception("prop: games fetch failed for %s", sport_val)
            await interaction.followup.send(f"Couldn't load today's {sport_val.upper()} games.")
            return

        # Only pull props for games that haven't finished (live/upcoming), and
        # cap how many games we hit so one command doesn't hammer the backend.
        active = [g for g in games if str(g.get("status", "")).lower() not in
                  ("final", "finished", "post", "completed", "closed")]
        active = active[:8]
        if not active:
            await interaction.followup.send(f"No upcoming {sport_val.upper()} games with props right now.")
            return

        collected = []
        for g in active:
            gid = g.get("id") or g.get("game_id")
            if gid is None:
                continue
            try:
                pdata = await fetch_json(session, f"/api/{sport_val}/props/{gid}")
            except Exception:
                continue
            matchup = ""
            try:
                matchup = f"{(g.get('away') or {}).get('name','')} @ {(g.get('home') or {}).get('name','')}".strip(" @")
            except Exception:
                pass
            for pr in (pdata.get("props") or []):
                if pr.get("projection") is None or pr.get("line") is None:
                    continue
                if needle and needle not in str(pr.get("player", "")).lower():
                    continue
                edge = _prop_edge(pr)
                if edge is None:
                    continue
                pr["_edge"] = edge
                pr["_matchup"] = matchup
                collected.append(pr)

    if not collected:
        msg = (f"No props with a model projection for **{player}** today."
               if needle else f"No {sport_val.upper()} props with a model edge on the board yet.")
        await interaction.followup.send(msg)
        return

    # Rank by absolute projection-vs-line gap — the model's strongest leans
    collected.sort(key=lambda p: abs(p["_edge"]), reverse=True)
    top = collected[:6]

    title = (f"🎯 {sport_val.upper()} Prop Edges — {player}"
             if needle else f"🎯 Today's Top {sport_val.upper()} Prop Edges")
    embed = discord.Embed(title=title, color=BRAND_COLOR, timestamp=now_utc())
    for pr in top:
        edge = pr["_edge"]
        lean = "OVER" if edge > 0 else "UNDER"
        stat = _prop_stat(pr)
        proj = pr.get("projection")
        line = pr.get("line")
        try:
            proj_s = f"{float(proj):g}"
            line_s = f"{float(line):g}"
        except (TypeError, ValueError):
            proj_s, line_s = str(proj), str(line)
        embed.add_field(
            name=f"{pr.get('player','—')} — {stat}",
            value=(f"Model **{lean} {line_s}** {stat}\n"
                   f"Projection {proj_s} vs line {line_s} • gap {abs(edge):.1f}"
                   + (f"\n{pr['_matchup']}" if pr.get("_matchup") else "")),
            inline=False,
        )
    embed.set_footer(text="Model projections — not a guarantee. Line Logic • thelinelogic.com")
    await interaction.followup.send(embed=embed)


# ---------- AI explain layer ----------
# These call ONE backend route, /api/explain, which is where the LLM lives. The
# bot never talks to an LLM directly. Your backend feeds the LLM only your own
# model + context data, so the AI explains the pick — it never invents it. All
# three stay hidden until AI_EXPLAIN_ENABLED=1 and the backend route exists.

async def _explain_disabled_notice(interaction: discord.Interaction):
    await interaction.followup.send(
        "The AI explain feature isn't switched on yet. (It goes live once the "
        "`/api/explain` route is deployed on the backend and `AI_EXPLAIN_ENABLED=1`.)"
    )


@bot.tree.command(name="explain", description="AI breakdown of why the model likes a play — built on Line Logic's own data")
@app_commands.describe(query="Team, player, or matchup, e.g. Braves ML")
async def explain_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "full"})
        except Exception:
            log.exception("explain failed for %s", query)
            await interaction.followup.send(f"Couldn't generate a breakdown for **{query}** right now.")
            return

    if not data.get("found") or not data.get("content"):
        await interaction.followup.send(
            f"No model play found for **{query}** to explain. Try a team that's on today's board."
        )
        return

    edge = data.get("edge_pct")
    edge_str = f"+{edge:.1f}%" if isinstance(edge, (int, float)) else "—"
    embed = discord.Embed(
        title=f"🧠 {data.get('pick', query)}",
        description=str(data["content"])[:4000],  # embed description hard limit
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    if data.get("match"):
        embed.insert_field_at(0, name="Matchup", value=data["match"], inline=False)
    embed.add_field(name="Model Win %", value=f"{data.get('win_pct', '—')}%", inline=True)
    embed.add_field(name="Market", value=_fmt_odds(data.get("market_odds")), inline=True)
    embed.add_field(name="Edge", value=edge_str, inline=True)
    embed.set_footer(text="AI explains the model's read — not a guarantee, and lines move. Line Logic")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="why", description="Quick bulleted reasons behind a model play")
@app_commands.describe(query="Team or player, e.g. Braves")
async def why_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "why"})
        except Exception:
            log.exception("why failed for %s", query)
            await interaction.followup.send(f"Couldn't pull the quick view for **{query}** right now.")
            return

    bullets = data.get("bullets") or []
    if not data.get("found") or not bullets:
        await interaction.followup.send(f"No model play found for **{query}**.")
        return

    edge = data.get("edge_pct")
    edge_str = f"+{edge:.1f}%" if isinstance(edge, (int, float)) else "—"
    body = "\n".join(f"• {b}" for b in bullets[:6])
    embed = discord.Embed(
        title=f"⚡ Why: {data.get('pick', query)}",
        description=f"**Model edge: {edge_str}**\n\n{body}",
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.set_footer(text="Run /explain for the full breakdown • Line Logic")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="sources", description="What data fed a model play — honest provenance, no filler")
@app_commands.describe(query="Team or player")
async def sources_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "sources"})
        except Exception:
            log.exception("sources failed for %s", query)
            await interaction.followup.send(f"Couldn't pull the data trail for **{query}** right now.")
            return

    sources = data.get("sources") or {}
    if not data.get("found") or not sources:
        await interaction.followup.send(f"No data trail available for **{query}**.")
        return

    embed = discord.Embed(
        title=f"🔍 Data behind: {data.get('pick', query)}",
        description="Only what actually fed this projection is listed here.",
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    for label, src in list(sources.items())[:10]:
        embed.add_field(name=str(label), value=f"✓ {src}", inline=True)
    embed.set_footer(text="Line Logic • thelinelogic.com")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="confidence", description="What the model's confidence grade means for a play — not certainty of outcome")
@app_commands.describe(query="Team or player, e.g. Braves")
async def confidence_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "confidence"})
        except Exception:
            log.exception("confidence failed for %s", query)
            await interaction.followup.send(f"Couldn't pull confidence for **{query}** right now.")
            return

    if not data.get("found") or not data.get("content"):
        await interaction.followup.send(f"No model play found for **{query}**.")
        return

    embed = discord.Embed(
        title=f"🎯 Confidence: {data.get('confidence', '—')} — {data.get('pick', query)}",
        description=str(data["content"])[:2000],
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.set_footer(text="Confidence = quality of the opportunity, not certainty. Line Logic")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="tweet", description="A ready-to-post X blurb for a model play (copy-paste)")
@app_commands.describe(query="Team or player, e.g. Braves ML")
async def tweet_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "tweet"})
        except Exception:
            log.exception("tweet failed for %s", query)
            await interaction.followup.send(f"Couldn't draft a post for **{query}** right now.")
            return

    if not data.get("found") or not data.get("content"):
        await interaction.followup.send(f"No model play found for **{query}**.")
        return

    # Send as plain text in a code-style block so it's easy to copy on mobile
    await interaction.followup.send(f"**Draft post — copy below:**\n>>> {data['content']}")


@bot.tree.command(name="writeup", description="Long-form model breakdown (Overview, Model, Matchup, Market, Risks)")
@app_commands.describe(query="Team or player, e.g. Braves")
async def writeup_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "writeup"}, timeout=40)
        except Exception:
            log.exception("writeup failed for %s", query)
            await interaction.followup.send(f"Couldn't generate a write-up for **{query}** right now.")
            return

    if not data.get("found") or not data.get("content"):
        await interaction.followup.send(f"No model play found for **{query}**.")
        return

    # Write-ups can exceed Discord's 2000-char message limit — chunk safely.
    text = str(data["content"]).strip()
    header = f"🧠 **{data.get('pick', query)}** — {data.get('match', '')}\n\n"
    text = header + text
    # split into <=1900 char chunks, skipping any empty ones
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    chunks = [c for c in chunks if c.strip()]
    if not chunks:
        await interaction.followup.send(f"No write-up available for **{query}** right now.")
        return
    await interaction.followup.send(chunks[0])
    for extra in chunks[1:]:
        await interaction.followup.send(extra)


@bot.tree.command(name="ask", description="Ask about a model play — grounded in Line Logic's data, not a general chatbot")
@app_commands.describe(query="Team or player the question is about", question="Your question")
async def ask_command(interaction: discord.Interaction, query: str, question: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(
                session, "/api/generate",
                params={"q": query, "mode": "ask", "question": question},
            )
        except Exception:
            log.exception("ask failed for %s", query)
            await interaction.followup.send("Couldn't answer that right now — try again shortly.")
            return

    if not data.get("found"):
        await interaction.followup.send(
            f"No model play found for **{query}**, so there's nothing grounded to answer from."
        )
        return

    answer = data.get("answer")
    if not answer:
        await interaction.followup.send("The model doesn't include enough to answer that confidently.")
        return

    embed = discord.Embed(
        title=f"💬 {query}",
        description=f"**Q:** {question}\n\n{str(answer)[:3500]}",
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.set_footer(text="Grounded in Line Logic's model data • not betting advice")
    await interaction.followup.send(embed=embed)


# Static betting glossary — no LLM, no backend call, so it works even with AI off
# and costs nothing. Add terms freely. Keys are matched case-insensitively.
GLOSSARY = {
    "edge": "The gap between the model's fair price and the sportsbook's price. A positive edge means the book is offering more than the true odds — that's the value the model targets.",
    "clv": "Closing Line Value — whether you bet at a better price than the line closed at. Consistently beating the closing line is the strongest sign a model has real, repeatable value.",
    "ev": "Expected Value — the average profit or loss a bet would return if you could make it many times. Positive EV (+EV) means it's mathematically worth making, even if it loses on any given night.",
    "expected value": "Expected Value — the average profit or loss a bet would return if you could make it many times. Positive EV (+EV) means it's mathematically worth making, even if it loses on any given night.",
    "vig": "Vig (or juice) — the sportsbook's built-in commission. It's why a coin-flip bet pays -110 on both sides instead of +100; the book keeps the difference.",
    "juice": "Juice (or vig) — the sportsbook's built-in commission. It's why a coin-flip bet pays -110 on both sides instead of +100; the book keeps the difference.",
    "implied probability": "The win probability baked into a set of odds. -150 implies about 60%. Comparing implied probability to the model's probability is how an edge is found.",
    "unit": "A standard bet size, usually 1% of your bankroll. Sizing in units instead of dollars keeps your risk consistent and protects you through losing streaks.",
    "steam": "Steam — a fast, coordinated line move across many sportsbooks at once, usually from sharp money hitting the same side hard.",
    "sharp money": "Money from professional, winning bettors. Books move lines quickly when they detect it because it signals where the true price is heading.",
    "fair odds": "The price that matches the model's true win probability, with no sportsbook vig added. The edge is the distance between fair odds and the market price.",
}


@bot.tree.command(name="learn", description="Plain-English definition of a betting term")
@app_commands.describe(term="e.g. edge, CLV, EV, vig, implied probability, unit")
async def learn_command(interaction: discord.Interaction, term: str):
    key = term.lower().strip()
    definition = GLOSSARY.get(key)
    if not definition:
        # light fuzzy match so "closing line value" finds "clv", etc.
        for k, v in GLOSSARY.items():
            if key in k or k in key:
                definition = v
                break
    if not definition:
        available = ", ".join(sorted({k for k in GLOSSARY if " " not in k}))
        await interaction.response.send_message(
            f"I don't have a definition for **{term}** yet. Try one of: {available}",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title=f"📚 {term.strip().title()}",
        description=definition,
        color=BRAND_COLOR,
    )
    embed.set_footer(text="Line Logic • learn the why, not just the pick")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="track", description="Track a pick to your capper record (model-board picks only)")
@app_commands.describe(
    pick="Team or player, e.g. Braves",
    units="How many units (default 1)",
    sport="Optional: narrow the sport",
)
@app_commands.choices(sport=SPORT_CHOICES)
async def track_command(interaction: discord.Interaction, pick: str,
                        units: float = 1.0,
                        sport: app_commands.Choice[str] | None = None):
    await interaction.response.defer()
    body = {
        "user_id": str(interaction.user.id),
        "username": interaction.user.display_name,
        "team": pick,
        "stake_units": units,
    }
    if sport:
        body["sport"] = sport.value

    async with aiohttp.ClientSession() as session:
        try:
            url = f"{LINE_LOGIC_API_BASE}/api/capper/track"
            headers = {"Authorization": f"Bearer {LINE_LOGIC_API_KEY}"} if LINE_LOGIC_API_KEY else {}
            async with session.post(url, json=body, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=25)) as resp:
                data = await resp.json()
        except Exception:
            log.exception("track failed for %s", pick)
            await interaction.followup.send("Couldn't track that right now — try again shortly.")
            return

    if not data.get("ok"):
        if data.get("error") == "not_on_board":
            await interaction.followup.send(
                f"**{pick}** isn't on the model board right now, so it can't be tracked yet. "
                f"You can only track picks the model has on the current slate."
            )
        else:
            await interaction.followup.send("Couldn't track that pick — check the name and try again.")
        return

    odds = data.get("market_odds")
    odds_str = _fmt_odds(odds)
    embed = discord.Embed(
        title="✅ Pick Tracked",
        description=f"**{data.get('pick')}**\n{data.get('match','')}",
        color=0x2ECC71,
        timestamp=now_utc(),
    )
    embed.add_field(name="Sport", value=str(data.get("sport", "—")).upper(), inline=True)
    embed.add_field(name="Odds", value=odds_str, inline=True)
    embed.add_field(name="Stake", value=f"{data.get('stake_units', 1)}u", inline=True)
    embed.set_footer(text=f"Tracked by {interaction.user.display_name} • graded after the game")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="mystats", description="Your capper record — tracked picks, W/L, units, ROI")
async def mystats_command(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/capper/stats",
                                    params={"user_id": str(interaction.user.id)}, timeout=20)
        except Exception:
            log.exception("mystats failed")
            await interaction.followup.send("Couldn't pull your stats right now — try again shortly.")
            return

    if not data.get("total"):
        await interaction.followup.send(
            "You haven't tracked any picks yet. Use `/track [team]` to start your record."
        )
        return

    units = data.get("units_pl")
    roi = data.get("roi_pct")
    win_pct = data.get("win_pct")
    embed = discord.Embed(
        title=f"📊 {interaction.user.display_name}'s Capper Record",
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.add_field(name="Record", value=data.get("record", "0-0"), inline=True)
    embed.add_field(name="Win %", value=f"{win_pct}%" if win_pct is not None else "—", inline=True)
    embed.add_field(name="Pending", value=str(data.get("pending", 0)), inline=True)
    embed.add_field(name="Units", value=f"{units:+.2f}u" if isinstance(units, (int, float)) else "—", inline=True)
    embed.add_field(name="ROI", value=f"{roi:+.1f}%" if isinstance(roi, (int, float)) else "—", inline=True)
    embed.add_field(name="Total Tracked", value=str(data.get("total", 0)), inline=True)

    form = data.get("recent_form") or []
    if form:
        embed.add_field(
            name="Recent Form",
            value=" ".join("🟢" if x == "W" else "🔴" for x in form),
            inline=False,
        )

    by_sport = data.get("by_sport") or {}
    ranked = sorted(by_sport.items(), key=lambda kv: (kv[1].get("units_pl") or 0), reverse=True)
    lines = []
    for sp, s in ranked[:6]:
        emoji, label, _ = SPORT_LABEL_MAP.get(sp, ("", sp.upper(), ""))
        u = s.get("units_pl")
        rec = s.get("record", "0-0")
        pend = s.get("pending", 0)
        if (s.get("wins", 0) + s.get("losses", 0)) > 0:
            lines.append(f"{emoji} **{label}** — {rec} · {u:+.2f}u")
        elif pend:
            lines.append(f"{emoji} **{label}** — {pend} pending")
    if lines:
        embed.add_field(name="By Sport", value="\n".join(lines), inline=False)
    embed.set_footer(text="Line Logic • track picks with /track")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="cappers", description="The capper leaderboard — top tracked records in the server")
@app_commands.describe(sort="Rank by units won (default) or win %")
@app_commands.choices(sort=[
    app_commands.Choice(name="Units", value="units"),
    app_commands.Choice(name="Win %", value="winpct"),
])
async def cappers_command(interaction: discord.Interaction,
                          sort: app_commands.Choice[str] | None = None):
    await interaction.response.defer()
    sort_val = sort.value if sort else "units"
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/capper/leaderboard",
                                    params={"sort": sort_val}, timeout=20)
        except Exception:
            log.exception("cappers failed")
            await interaction.followup.send("Couldn't load the leaderboard right now — try again shortly.")
            return

    cappers = data.get("cappers", [])
    if not cappers:
        building = data.get("building", 0)
        msg = "No ranked cappers yet — records show up here once picks are graded."
        if building:
            msg += f" ({building} capper{'s' if building != 1 else ''} building a record.)"
        await interaction.followup.send(msg)
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, c in enumerate(cappers):
        rank = medals[i] if i < 3 else f"**{i+1}.**"
        units = c.get("units_pl", 0)
        roi = c.get("roi_pct")
        rec = c.get("record", "0-0")
        roi_str = f" · {roi:+.1f}% ROI" if isinstance(roi, (int, float)) else ""
        lines.append(f"{rank} **{c.get('username','capper')}** — {units:+.2f}u ({rec}){roi_str}")

    embed = discord.Embed(
        title="🏆 Capper Leaderboard",
        description="\n".join(lines),
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    label = "units won" if sort_val == "units" else "win %"
    footer = f"Ranked by {label}"
    if data.get("building"):
        footer += f" • {data['building']} still building a record"
    embed.set_footer(text=footer)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="postdaily", description="(Staff) Post the daily model card now")
async def postdaily_command(interaction: discord.Interaction):
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms or not (perms.manage_guild or perms.administrator):
        await interaction.response.send_message(
            "That's a staff-only command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok, reason = await post_daily_card()
    await interaction.followup.send(
        "Posted the daily card." if ok else f"Couldn't post — {reason}",
        ephemeral=True,
    )


async def build_room_embed(days: int = 7):
    """The community's combined record — every tracked pick as one capper."""
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/capper/community",
                                    params={"days": days}, timeout=25)
        except Exception:
            log.exception("room record fetch failed")
            return None

    if not data.get("total"):
        return discord.Embed(
            title="🏟️ The Room",
            description=("No tracked picks in this window yet. Use `/track [team]` "
                         "and the room's record starts building."),
            color=BRAND_COLOR, timestamp=now_utc(),
        )

    units = data.get("units_pl")
    roi = data.get("roi_pct")
    win_pct = data.get("win_pct")
    window = f"last {days} days" if days else "all time"

    embed = discord.Embed(
        title="🏟️ The Room — Community Record",
        description=(f"Every pick tracked in this server, combined ({window}).\n"
                     f"**{data.get('cappers', 0)}** capper(s) · "
                     f"**{data.get('total', 0)}** picks tracked"),
        color=BRAND_COLOR, timestamp=now_utc(),
    )
    embed.add_field(name="Record", value=data.get("record", "0-0"), inline=True)
    embed.add_field(name="Win %", value=f"{win_pct}%" if win_pct is not None else "—", inline=True)
    embed.add_field(name="Pending", value=str(data.get("pending", 0)), inline=True)
    embed.add_field(name="Units", value=f"{units:+.2f}u" if isinstance(units, (int, float)) else "—", inline=True)
    embed.add_field(name="ROI", value=f"{roi:+.1f}%" if isinstance(roi, (int, float)) else "—", inline=True)

    by_sport = data.get("by_sport") or {}
    ranked = sorted(by_sport.items(), key=lambda kv: (kv[1].get("units_pl") or 0), reverse=True)
    lines = []
    for sp, s in ranked[:5]:
        emoji, label, _ = SPORT_LABEL_MAP.get(sp, ("", sp.upper(), ""))
        if (s.get("wins", 0) + s.get("losses", 0)) > 0:
            lines.append(f"{emoji} **{label}** — {s.get('record','0-0')} · {s.get('units_pl',0):+.2f}u")
    if lines:
        embed.add_field(name="Where the room is winning", value="\n".join(lines), inline=False)

    hot = data.get("hot_picks") or []
    if hot:
        embed.add_field(
            name="🔥 Most tracked right now",
            value="\n".join(f"**{h['pick']}** — {h['count']} cappers" for h in hot[:3]),
            inline=False,
        )
    embed.set_footer(text="Track your plays with /track • Line Logic")
    return embed


TICKET_KINDS = {
    "suggestion": ("💡", "Suggestion"),
    "bug": ("🐛", "Bug / something broken"),
    "report": ("🚨", "Report a member"),
    "other": ("📩", "Other"),
}


async def _create_ticket(interaction: discord.Interaction, kind: str,
                         subject: str, details: str):
    """Shared ticket creation — used by both the button panel and /ticket."""
    emoji, label = TICKET_KINDS.get(kind, ("📩", "Ticket"))
    kind_label = f"{emoji} {label}"

    if not TICKETS_CHANNEL_ID:
        await interaction.followup.send(
            "Tickets aren't set up yet — staff need to set CHANNEL_TICKETS.", ephemeral=True)
        return
    channel = bot.get_channel(TICKETS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(TICKETS_CHANNEL_ID)
        except Exception as e:
            await interaction.followup.send(f"Couldn't open the ticket channel: {e}", ephemeral=True)
            return

    embed = discord.Embed(
        title=f"{kind_label} — {subject[:200]}",
        description=details[:3500],
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.set_author(name=str(interaction.user),
                     icon_url=getattr(interaction.user.display_avatar, "url", None))
    embed.set_footer(text=f"User ID: {interaction.user.id}")

    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        await interaction.followup.send(
            "I can't post in the ticket channel — staff need to give LineBot access there.",
            ephemeral=True)
        return

    thread = None
    try:
        thread = await msg.create_thread(name=f"{kind}-{interaction.user.name}"[:90],
                                         auto_archive_duration=10080)
        await thread.add_user(interaction.user)
        await thread.send(
            f"{interaction.user.mention} thanks — staff will pick this up here. "
            "Add anything else you think helps.")
    except Exception:
        log.warning("ticket thread creation failed", exc_info=True)

    if OWNER_USER_ID:
        try:
            owner = bot.get_user(OWNER_USER_ID) or await bot.fetch_user(OWNER_USER_ID)
            link = (thread.jump_url if thread else msg.jump_url)
            dm = discord.Embed(
                title=f"{kind_label} from {interaction.user}",
                description=f"**{subject[:200]}**\n\n{details[:1500]}",
                color=BRAND_COLOR, timestamp=now_utc(),
            )
            dm.add_field(name="Open it", value=link, inline=False)
            await owner.send(embed=dm)
        except discord.Forbidden:
            log.warning("ticket DM blocked — owner has DMs closed")
        except Exception:
            log.warning("ticket DM failed", exc_info=True)

    await interaction.followup.send(
        f"Ticket opened{' — ' + thread.mention if thread else ''}. Staff will follow up there.",
        ephemeral=True)


class TicketModal(discord.ui.Modal):
    """The popup form the member fills in after clicking a button."""
    def __init__(self, kind: str):
        emoji, label = TICKET_KINDS.get(kind, ("📩", "Ticket"))
        super().__init__(title=f"{label}"[:45])
        self.kind = kind
        self.subject = discord.ui.TextInput(
            label="Subject", placeholder="One line — what's this about?", max_length=100)
        self.details = discord.ui.TextInput(
            label="Details", style=discord.TextStyle.paragraph,
            placeholder="Tell us what's going on. Include anything that helps.",
            max_length=1500)
        self.add_item(self.subject)
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await _create_ticket(interaction, self.kind,
                             self.subject.value, self.details.value)


class TicketPanel(discord.ui.View):
    """Persistent button panel — survives bot restarts via fixed custom_ids."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Suggestion", emoji="💡",
                       style=discord.ButtonStyle.primary, custom_id="ll_ticket:suggestion")
    async def _suggestion(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal("suggestion"))

    @discord.ui.button(label="Bug", emoji="🐛",
                       style=discord.ButtonStyle.secondary, custom_id="ll_ticket:bug")
    async def _bug(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal("bug"))

    @discord.ui.button(label="Report", emoji="🚨",
                       style=discord.ButtonStyle.danger, custom_id="ll_ticket:report")
    async def _report(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal("report"))

    @discord.ui.button(label="Other", emoji="📩",
                       style=discord.ButtonStyle.secondary, custom_id="ll_ticket:other")
    async def _other(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal("other"))


@bot.tree.command(name="announce", description="(Staff) Post a website/product update to #website-updates")
@app_commands.describe(title="Headline for the update", body="What changed and why it matters",
                       ping="Ping @everyone? Use sparingly — big releases only")
async def announce_command(interaction: discord.Interaction, title: str, body: str,
                           ping: bool = False):
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms or not (perms.manage_guild or perms.administrator):
        await interaction.response.send_message("That's a staff-only command.", ephemeral=True)
        return
    if not WEBSITE_UPDATES_CHANNEL_ID:
        await interaction.response.send_message(
            "CHANNEL_WEBSITE_UPDATES isn't set.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    channel = bot.get_channel(WEBSITE_UPDATES_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(WEBSITE_UPDATES_CHANNEL_ID)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load the channel: {e}", ephemeral=True)
            return
    embed = discord.Embed(
        title=f"🚀 {title[:240]}",
        description=body[:3800].replace("\\n", "\n"),
        color=BRAND_COLOR, timestamp=now_utc(),
    )
    embed.set_footer(text="Line Logic • thelinelogic.com")
    try:
        await channel.send(content="@everyone" if ping else None, embed=embed)
    except discord.Forbidden:
        await interaction.followup.send("Can\'t post there — check LineBot\'s permissions.", ephemeral=True)
        return
    await interaction.followup.send("Update posted.", ephemeral=True)


@bot.tree.command(name="postresults", description="(Staff) Post the results recap now")
async def postresults_command(interaction: discord.Interaction):
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms or not (perms.manage_guild or perms.administrator):
        await interaction.response.send_message("That's a staff-only command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok, reason = await post_results_recap()
    await interaction.followup.send(
        "Posted the results recap." if ok else f"Couldn't post — {reason}", ephemeral=True)


@bot.tree.command(name="scanedges", description="(Staff) Scan the board and post any new edge alerts")
async def scanedges_command(interaction: discord.Interaction):
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms or not (perms.manage_guild or perms.administrator):
        await interaction.response.send_message("That's a staff-only command.", ephemeral=True)
        return
    if not EDGE_ALERTS_CHANNEL_ID:
        await interaction.response.send_message("CHANNEL_EDGE_ALERTS isn't set.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    n = await scan_edge_alerts()
    await interaction.followup.send(
        f"Posted {n} edge alert(s)." if n else
        f"No new plays at or above +{EDGE_ALERT_MIN:.0f}% edge right now.", ephemeral=True)


@bot.tree.command(name="ticketpanel", description="(Staff) Post the ticket button panel in this channel")
async def ticketpanel_command(interaction: discord.Interaction):
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms or not (perms.manage_guild or perms.administrator):
        await interaction.response.send_message("That's a staff-only command.", ephemeral=True)
        return
    embed = discord.Embed(
        title="📩 Open a Ticket",
        description=("Need something? Pick the option that fits and a short form will "
                     "pop up. Your ticket is **private** — only you and staff can see it, "
                     "and we'll reply in your own thread.\n\n"
                     "💡 **Suggestion** — an idea to make the server or model better\n"
                     "🐛 **Bug** — something broken on the site, bot, or Discord\n"
                     "🚨 **Report** — a member issue that needs staff attention\n"
                     "📩 **Other** — anything else"),
        color=BRAND_COLOR,
    )
    embed.set_footer(text="Line Logic • we read every one")
    await interaction.response.send_message(embed=embed, view=TicketPanel())


@bot.tree.command(name="ticket", description="Open a private ticket with staff — suggestions, bugs, or issues")
@app_commands.describe(subject="Short summary", details="What's going on?")
@app_commands.choices(kind=[
    app_commands.Choice(name="Suggestion", value="suggestion"),
    app_commands.Choice(name="Bug / something broken", value="bug"),
    app_commands.Choice(name="Report a member", value="report"),
    app_commands.Choice(name="Other", value="other"),
])
async def ticket_command(interaction: discord.Interaction, subject: str, details: str,
                         kind: app_commands.Choice[str] | None = None):
    await interaction.response.defer(ephemeral=True)
    await _create_ticket(interaction, kind.value if kind else "other", subject, details)


@bot.tree.command(name="room", description="The community's combined record — every tracked pick together")
@app_commands.describe(days="Window in days (0 = all time, default 7)")
async def room_command(interaction: discord.Interaction, days: int = 7):
    await interaction.response.defer()
    embed = await build_room_embed(max(0, min(days, 365)))
    if embed is None:
        await interaction.followup.send("Couldn't pull the room record right now — try again shortly.")
        return
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="permcheck", description="(Staff) Show what LineBot can actually do in a channel")
@app_commands.describe(channel="Channel to check (defaults to this one)")
async def permcheck_command(interaction: discord.Interaction,
                            channel: discord.TextChannel | None = None):
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms or not (perms.manage_guild or perms.administrator):
        await interaction.response.send_message("That's a staff-only command.", ephemeral=True)
        return
    ch = channel or interaction.channel
    me = interaction.guild.me
    p = ch.permissions_for(me)
    checks = [
        ("View Channel", p.view_channel),
        ("Send Messages", p.send_messages),
        ("Embed Links", p.embed_links),
        ("Read Message History", p.read_message_history),
        ("Mention Everyone (role pings)", p.mention_everyone),
        ("Manage Roles (server-wide)", me.guild_permissions.manage_roles),
    ]
    lines = [f"{'✅' if ok else '❌'} {name}" for name, ok in checks]
    cat = ch.category.name if ch.category else "— none —"
    synced = "yes" if (ch.category and ch.permissions_synced) else "no"
    body = (f"**#{ch.name}**\nCategory: {cat} · Synced to category: {synced}\n\n"
            + "\n".join(lines))
    await interaction.response.send_message(body, ephemeral=True)


@bot.tree.command(name="verifyall", description="(Staff) Give the Verified Member role to everyone who doesn't have it")
async def verifyall_command(interaction: discord.Interaction):
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms or not (perms.manage_guild or perms.administrator):
        await interaction.response.send_message("That's a staff-only command.", ephemeral=True)
        return
    if not VERIFIED_ROLE_ID:
        await interaction.response.send_message(
            "VERIFIED_ROLE_ID isn't set in the bot's environment.", ephemeral=True)
        return

    guild = interaction.guild
    role = guild.get_role(VERIFIED_ROLE_ID) if guild else None
    if role is None:
        await interaction.response.send_message(
            f"Couldn't find the Verified Member role (`{VERIFIED_ROLE_ID}`).", ephemeral=True)
        return
    if guild.me.top_role <= role:
        await interaction.response.send_message(
            "LineBot's role sits below Verified Member, so it can't assign it. "
            "Drag LineBot above it in Server Settings → Roles, then run this again.",
            ephemeral=True)
        return

    await interaction.response.send_message(
        "Starting the backfill — this runs in the background and can take a few "
        "minutes on a large server. I'll report back here when it's done.",
        ephemeral=True)

    async def _run_backfill():
        added = skipped = failed = 0
        try:
            # Load the member list from the gateway in one pass (fast, cached),
            # rather than streaming the HTTP endpoint while also writing roles —
            # interleaving those two makes Discord throttle aggressively.
            if not guild.chunked:
                await guild.chunk(cache=True)
            members = list(guild.members)
            todo = [m for m in members if not m.bot and role not in m.roles]
            total = len(todo)
            await bot_log(f"Verified backfill starting — {total} member(s) need the role.")

            for i, member in enumerate(todo, 1):
                try:
                    await member.add_roles(role, reason="Verified Member backfill")
                    added += 1
                except discord.Forbidden:
                    failed += 1
                except discord.HTTPException as e:
                    failed += 1
                    if e.status == 429:          # rate limited — ease off
                        await asyncio.sleep(5)
                except Exception:
                    failed += 1
                await asyncio.sleep(0.25)
                if i % 50 == 0:
                    await bot_log(f"Verified backfill: {i}/{total} processed ({added} added).")
            skipped = len(members) - total
        except Exception:
            log.exception("verifyall backfill failed")

        summary = (f"✅ Verified Member backfill complete — **{added}** added, "
                   f"{skipped} already had it or were bots"
                   + (f", {failed} failed" if failed else "") + ".")
        try:
            await interaction.followup.send(summary, ephemeral=True)
        except Exception:
            pass
        await bot_log(summary)

    asyncio.create_task(_run_backfill())


@bot.tree.command(name="ladder", description="The Ladder Challenge — current rung, bankroll, and today's leg")
async def ladder_command(interaction: discord.Interaction):
    await interaction.response.defer()
    embed, _ = await build_ladder_embed()
    if embed is None:
        await interaction.followup.send("Couldn't reach the ladder right now — try again shortly.")
        return
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="postladder", description="(Staff) Post the Ladder Challenge card now")
async def postladder_command(interaction: discord.Interaction):
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms or not (perms.manage_guild or perms.administrator):
        await interaction.response.send_message("That's a staff-only command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok, reason = await post_ladder_card()
    await interaction.followup.send(
        "Posted the ladder card." if ok else f"Couldn't post — {reason}", ephemeral=True)


@bot.tree.command(name="postcappers", description="(Staff) Post the capper leaderboard now")
async def postcappers_command(interaction: discord.Interaction):
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms or not (perms.manage_guild or perms.administrator):
        await interaction.response.send_message("That's a staff-only command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok, reason = await post_capper_leaderboard()
    await interaction.followup.send(
        "Posted the capper leaderboard." if ok else f"Couldn't post — {reason}",
        ephemeral=True,
    )


@bot.tree.command(name="record", description="Line Logic's verified track record, units, and ROI")
async def record_command(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        try:
            # Same endpoint that powers the website Track Record page
            data = await fetch_json(session, "/api/accuracy", params={"days": 30}, timeout=25)
        except Exception:
            log.exception("accuracy lookup failed")
            await interaction.followup.send("Performance data isn't available right now.")
            return

    ov = data.get("overall", {})
    by_sport = data.get("by_sport", {})
    if not ov:
        await interaction.followup.send("Performance data isn't available right now.")
        return

    at_w = ov.get("alltime_wins", 0)
    at_l = ov.get("alltime_losses", 0)
    at_pct = ov.get("alltime_pct")
    units = ov.get("units_30d")
    roi = ov.get("roi_30d")
    n_sports = len(by_sport)

    desc = f"**{at_pct}%** all-time · {at_w}-{at_l} across {n_sports} sports"
    if isinstance(units, (int, float)):
        desc += f"\n**{units:+.2f}u** on graded wagers (30d)"

    embed = discord.Embed(
        title="📈 Line Logic — Verified Track Record",
        description=desc,
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )

    SPORT_LABEL = {
        "mlb": "⚾ MLB", "nba": "🏀 NBA", "wnba": "👟 WNBA", "nfl": "🏈 NFL",
        "nhl": "🏒 NHL", "ncaaf": "🏟️ NCAA FB", "ncaab": "🎓 NCAA BB",
        "ncaabb": "⚾ NCAA Baseball", "soccer": "⚽ Soccer", "tennis": "🎾 Tennis",
        "ufc": "🥊 UFC", "golf": "⛳ Golf",
    }
    # rank sports by all-time wins so the biggest bodies of work show first
    ranked = sorted(by_sport.items(),
                    key=lambda kv: (kv[1].get("alltime_wins") or 0), reverse=True)
    shown = 0
    for sp, s in ranked:
        if shown >= 8:
            break
        label = SPORT_LABEL.get(sp, sp.upper())
        aw = s.get("alltime_wins", 0)
        al = s.get("alltime_losses", 0)
        apct = s.get("alltime_pct")
        u = s.get("units_30d")
        roi_s = s.get("roi_30d")
        priced = s.get("priced_30d", 0)
        if priced and isinstance(u, (int, float)):
            val = f"{aw}-{al} ({apct}%) · **{u:+.2f}u** · {roi_s:+.1f}% ROI on {priced} wagers (30d)"
        else:
            val = f"{aw}-{al} ({apct}%) · +EV wagers building"
        embed.add_field(name=label, value=val, inline=False)
        shown += 1

    embed.set_footer(text="Verified across every graded pick • thelinelogic.com")
    await interaction.followup.send(embed=embed)


_STOCK_DISCLAIMER = ("Educational, paper-traded model signals — not financial advice. "
                     "Consult a licensed advisor before investing.")


@bot.tree.command(name="stock", description="Market quote, or the day's hot pick if no ticker given")
@app_commands.describe(symbol="Optional ticker, e.g. NVDA. Leave blank for the Hot Pick of the Day.")
async def stock_command(interaction: discord.Interaction, symbol: str = ""):
    await interaction.response.defer()
    symbol = symbol.upper().strip()

    async with aiohttp.ClientSession() as session:
        # No ticker -> the paper-model's "Hot Pick of the Day" (/api/stocks/hotpick)
        if not symbol:
            try:
                data = await fetch_json(session, "/api/stocks/hotpick", timeout=30)
            except Exception:
                log.exception("hotpick failed")
                await interaction.followup.send("Market data isn't available right now.")
                return
            hot = data.get("hot")
            if not hot:
                await interaction.followup.send(
                    "Nothing trending up steadily today — the model is holding its paper positions.\n"
                    f"_{_STOCK_DISCLAIMER}_"
                )
                return
            embed = discord.Embed(
                title=f"🔥 Hot Pick of the Day — {hot.get('ticker', '')}",
                description=hot.get("name", ""),
                color=0x2ECC71,
                timestamp=now_utc(),
            )
            embed.add_field(name="Price", value=f"${hot.get('price', '—')}", inline=True)
            embed.add_field(name=f"Last {hot.get('days', '?')} sessions", value=f"+{hot.get('pct', '—')}%", inline=True)
            embed.add_field(name="Up days", value=f"{hot.get('up_days', '—')}/{hot.get('days', '—')}", inline=True)
            vr = hot.get("vol_ratio")
            if isinstance(vr, (int, float)) and vr >= 1.2:
                embed.add_field(name="Volume", value=f"{round((vr - 1) * 100)}% above usual", inline=True)
            embed.set_footer(text=_STOCK_DISCLAIMER)
            await interaction.followup.send(embed=embed)
            return

        # Ticker given -> on-demand quote (/api/stocks/quote)
        try:
            data = await fetch_json(session, "/api/stocks/quote", params={"symbol": symbol, "range": "1D"}, timeout=25)
        except Exception:
            log.exception("stock quote failed for %s", symbol)
            await interaction.followup.send(f"Couldn't pull a quote for **{symbol}** right now.")
            return

    if data.get("error"):
        await interaction.followup.send(f"No price data for **{symbol}**.")
        return

    chg = data.get("change_pct")
    up = isinstance(chg, (int, float)) and chg >= 0
    chg_str = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "—"
    embed = discord.Embed(
        title=f"📈 {data.get('symbol', symbol)} — {data.get('name', '')}",
        color=0x2ECC71 if up else 0xE74C3C,
        timestamp=now_utc(),
    )
    embed.add_field(name="Price", value=f"${data.get('price', '—')}", inline=True)
    embed.add_field(name="Change (1D)", value=f"{'▲' if up else '▼'} {chg_str}", inline=True)
    embed.set_footer(text=_STOCK_DISCLAIMER)
    await interaction.followup.send(embed=embed)


@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    # Commands are defined globally; copy them into this guild so they register
    # instantly (guild syncs are immediate; pure-global syncs can take ~1 hour).
    # Persistent ticket buttons keep working across restarts
    try:
        bot.add_view(TicketPanel())
    except Exception:
        log.warning("ticket panel view registration failed", exc_info=True)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    log.info("Logged in as %s. Synced %d commands to guild %s", bot.user, len(synced), GUILD_ID)
    if DAILY_POST_ENABLED and not daily_post_loop.is_running():
        daily_post_loop.start()
        log.info("Daily post scheduled for %02d:00 UTC", DAILY_POST_HOUR_UTC)
    if RESULTS_POST_ENABLED and not results_post_loop.is_running():
        results_post_loop.start()
        log.info("Results recap scheduled for %02d:00 UTC", RESULTS_POST_HOUR_UTC)
    if EDGE_ALERTS_ENABLED and EDGE_ALERTS_CHANNEL_ID and not edge_alert_loop.is_running():
        edge_alert_loop.start()
        log.info("Edge alerts every %d min (min edge %.1f%%)", EDGE_ALERT_INTERVAL_MIN, EDGE_ALERT_MIN)
    if LADDER_POST_ENABLED and not ladder_post_loop.is_running():
        ladder_post_loop.start()
        log.info("Ladder post scheduled for %02d:10 UTC", LADDER_POST_HOUR_UTC)
    if WEEKLY_POST_ENABLED and not weekly_capper_loop.is_running():
        weekly_capper_loop.start()
        log.info("Weekly capper leaderboard scheduled for %02d:00 UTC (weekday %d)",
                 WEEKLY_POST_HOUR_UTC, WEEKLY_POST_WEEKDAY)


async def bot_log(message: str):
    if not BOT_LOGS_CHANNEL_ID:
        return
    channel = bot.get_channel(BOT_LOGS_CHANNEL_ID)
    if channel:
        await channel.send(f"`{now_utc().strftime('%Y-%m-%d %H:%M UTC')}` {message}")


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    member_count = guild.member_count

    if VERIFIED_ROLE_ID:
        role = guild.get_role(VERIFIED_ROLE_ID)
        if role:
            try:
                await member.add_roles(role, reason="Auto-verified on join")
            except discord.Forbidden:
                log.warning("Missing permission to assign Verified role — check bot role position")

    if WELCOME_CHANNEL_ID:
        channel = bot.get_channel(WELCOME_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title=f"👋 Welcome, {member.display_name}!",
                description=(
                    "Welcome to Line Logic 📊\n\n"
                    "Before you get started:\n"
                    "1️⃣ Read the rules\n"
                    "2️⃣ Pick your sports in the roles channel\n"
                    "3️⃣ Check the daily model\n\n"
                    f"You're member **#{member_count}**. Glad to have you."
                ),
                color=BRAND_COLOR,
                timestamp=now_utc(),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(embed=embed)

    await bot_log(f"👤 {member} joined — now {member_count} members")


@bot.event
async def on_member_remove(member: discord.Member):
    await bot_log(f"👋 {member} left — now {member.guild.member_count} members")


# ---------- Inbound webhook server ----------
# Your backend calls these to auto-post the daily model card and results.
# Include "notify_sport" in the payload (e.g. "mlb") to auto-ping the right
# notify role instead of pinging everyone.

routes = web.RouteTableDef()


def check_secret(request: web.Request) -> bool:
    if not WEBHOOK_SHARED_SECRET:
        return True
    return request.headers.get("X-Webhook-Secret") == WEBHOOK_SHARED_SECRET


@routes.post("/post/{channel_key}")
async def post_to_channel(request: web.Request):
    """
    curl -X POST https://your-bot.up.railway.app/post/daily_model \
      -H "X-Webhook-Secret: $WEBHOOK_SHARED_SECRET" \
      -H "Content-Type: application/json" \
      -d '{"title": "MLB — Braves ML", "description": "Model Probability: 61% | Market: +105 | Edge: +8.7% | Confidence: B+",
           "notify_sport": "mlb"}'
    """
    if not check_secret(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    channel_key = request.match_info["channel_key"]
    channel_id = CHANNEL_IDS.get(channel_key)
    if not channel_id:
        return web.json_response({"error": f"unknown channel_key '{channel_key}'"}, status=400)

    payload = await request.json()
    channel = bot.get_channel(channel_id)
    if channel is None:
        return web.json_response({"error": "bot cannot see that channel"}, status=500)

    embed = discord.Embed(
        title=payload.get("title", ""),
        description=payload.get("description", ""),
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    for f in payload.get("fields", []):
        embed.add_field(name=f.get("name", ""), value=f.get("value", ""), inline=f.get("inline", False))
    embed.set_footer(text="Line Logic • thelinelogic.com")

    content = role_mention(payload.get("notify_sport", "")) if payload.get("notify_sport") else None
    await channel.send(content=content, embed=embed)
    return web.json_response({"ok": True})


# --- Premium role sync — DISABLED in v1. Uncomment when you activate Whop/premium. ---
#
# @routes.post("/premium/webhook")
# async def premium_webhook(request: web.Request):
#     if not check_secret(request):
#         return web.json_response({"error": "unauthorized"}, status=401)
#     payload = await request.json()
#     user_id = int(payload.get("discord_user_id", 0))
#     action = payload.get("action")
#     guild = bot.get_guild(GUILD_ID)
#     if guild is None or not user_id or not PREMIUM_ROLE_ID:
#         return web.json_response({"error": "misconfigured"}, status=500)
#     member = guild.get_member(user_id) or await guild.fetch_member(user_id)
#     role = guild.get_role(PREMIUM_ROLE_ID)
#     if action == "grant":
#         await member.add_roles(role, reason="Premium subscription active")
#     elif action == "revoke":
#         await member.remove_roles(role, reason="Premium subscription ended")
#     else:
#         return web.json_response({"error": "unknown action"}, status=400)
#     return web.json_response({"ok": True})


@routes.get("/health")
async def health(request: web.Request):
    return web.json_response({"ok": True, "bot_ready": bot.is_ready()})


async def start_web_server():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Webhook server listening on port %d", PORT)


async def main():
    async with bot:
        await start_web_server()
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
