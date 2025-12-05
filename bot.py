import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from difflib import get_close_matches
from dotenv import load_dotenv

# ------------------------------------------------------------
# Configuration & Setup
# ------------------------------------------------------------

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# EQ2 canonical time zone (DST-aware)
TIMEZONE = ZoneInfo("America/New_York")

INTENTS = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=INTENTS)

DATA_FILE = "mobs_data.json"

# ------------------------------------------------------------
# Core Helpers (Time & Data)
# ------------------------------------------------------------

def now_local() -> datetime:
    """Return current aware datetime in Eastern time."""
    return datetime.now(TIMEZONE)

def load_data():
    """Load entire mobs_data.json into a Python dict."""
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_data(data):
    """Persist full mob data to JSON storage."""
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_guild_data(guild_id):
    """Return or create guild-level block."""
    data = load_data()
    gid = str(guild_id)

    if gid not in data:
        data[gid] = {
            "status_channel_id": None,
            "status_message_id": None,
            "mobs": {}
        }
        save_data(data)
        return data[gid]

    # Guarantee keys
    if "status_channel_id" not in data[gid]:
        data[gid]["status_channel_id"] = None
    if "status_message_id" not in data[gid]:
        data[gid]["status_message_id"] = None
    if "mobs" not in data[gid]:
        data[gid]["mobs"] = {}

    save_data(data)
    return data[gid]

def update_guild_data(guild_id, gdata):
    """Write back updated guild block."""
    data = load_data()
    data[str(guild_id)] = gdata
    save_data(data)

def normalize_mob_name(name: str) -> str:
    """Canonical key format."""
    return name.strip().lower()

def format_timedelta(delta: timedelta) -> str:
    """Format as Xm or Xh Ym."""
    total = int(delta.total_seconds())
    if total < 0:
        total = -total
    hours, rest = divmod(total, 3600)
    minutes, _ = divmod(rest, 60)

    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)

# ------------------------------------------------------------
# Time & Date Parsing
# ------------------------------------------------------------

def looks_like_time(tok: str) -> bool:
    """Check if token is HMM or HHMM."""
    return tok.isdigit() and len(tok) in (3, 4)

def parse_date_str(date_str: str):
    """Parse several common date formats."""
    formats = ["%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            pass
    raise ValueError("Date must be YYYY-MM-DD or MM/DD/YYYY")

def parse_time_str(time_str: str, for_date=None) -> datetime:
    """
    Interpret HHMM as either an explicit date's time or the most recent such time.
    """
    s = time_str.strip()
    if not s.isdigit() or len(s) not in (3, 4):
        raise ValueError("Time must be HMM or HHMM")

    if len(s) == 3:
        hour = int(s[0])
        minute = int(s[1:])
    else:
        hour = int(s[:2])
        minute = int(s[2:])

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Invalid numeric time")

    if for_date:
        return datetime(for_date.year, for_date.month, for_date.day,
                        hour, minute, tzinfo=TIMEZONE)

    # No explicit date → fallback to latest matching time (today or yesterday)
    now = now_local()
    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if dt > now:
        dt -= timedelta(days=1)
    return dt

# ------------------------------------------------------------
# Fuzzy Matching (Option A)
# ------------------------------------------------------------

def fuzzy_find_mob(name: str, mobs: dict):
    """
    Option A:
      - Exact match → return key
      - One close fuzzy match → return that key
      - Multiple close matches → return list of keys
      - No match → return None
    """
    name = name.lower().strip()

    if name in mobs:
        return name

    keys = list(mobs.keys())
    close = get_close_matches(name, keys, n=3, cutoff=0.6)

    if len(close) == 1:
        return close[0]

    if len(close) > 1:
        return close  # ambiguous

    return None  # no match

# ------------------------------------------------------------
# Auto-Learning Spawn Window
# ------------------------------------------------------------

def update_window_from_tod_history(mob_data: dict):
    """
    Given list of TOD strings, compute:
    - min_respawn_hours
    - max_respawn_hours
    - learned_confidence
    """
    history = mob_data.get("tod_history", [])
    if len(history) < 2:
        mob_data["learned_confidence"] = "LOW"
        return None, None, "LOW"

    times = sorted(datetime.fromisoformat(t) for t in history)
    intervals = []

    for a, b in zip(times, times[1:]):
        hours = (b - a).total_seconds() / 3600.0
        if hours > 0.25:  # prevent tiny intervals
            intervals.append(hours)

    if not intervals:
        mob_data["learned_confidence"] = "LOW"
        return None, None, "LOW"

    intervals.sort()

    # Trim outliers if enough data
    trimmed = intervals[1:-1] if len(intervals) >= 4 else intervals

    min_h = min(trimmed) * 0.95
    max_h = max(trimmed) * 1.05

    # Confidence
    n = len(intervals)
    if n < 3:
        conf = "LOW"
    elif n < 6:
        conf = "MEDIUM"
    else:
        conf = "HIGH"

    mob_data["min_respawn_hours"] = round(min_h, 2)
    mob_data["max_respawn_hours"] = round(max_h, 2)
    mob_data["learned_confidence"] = conf

    return mob_data["min_respawn_hours"], mob_data["max_respawn_hours"], conf

# ------------------------------------------------------------
# Status Line Builder
# ------------------------------------------------------------

def mob_status_line(mob_key: str, mob_data: dict, now: datetime) -> str:
    """
    Generate text line for !status and auto-updater.
    """
    name = mob_data.get("display_name", mob_key)

    if not mob_data.get("tracking", False):
        return f"❌ {name} — tracking OFF"

    min_h = mob_data.get("min_respawn_hours")
    max_h = mob_data.get("max_respawn_hours")
    last_death = mob_data.get("last_death")
    last_spawn = mob_data.get("last_spawn")
    conf = mob_data.get("learned_confidence", "LOW")

    if min_h is None or max_h is None:
        return f"⚠️ {name} — no spawn window (`!setwindow {name} min max`)"

    base_time = None
    if last_death:
        base_time = datetime.fromisoformat(last_death)
    elif last_spawn:
        base_time = datetime.fromisoformat(last_spawn)

    if not base_time:
        return f"ℹ️ {name} — no TOD or spawn recorded yet. (confidence: {conf})"

    earliest = base_time + timedelta(hours=min_h)
    latest = base_time + timedelta(hours=max_h)

    if now < earliest:
        return (f"⏳ {name} — window CLOSED, opens in "
                f"**{format_timedelta(earliest - now)}** (confidence: {conf})")

    if earliest <= now <= latest:
        return (f"✅ {name} — **WINDOW OPEN**, ~"
                f"{format_timedelta(latest - now)} left (confidence: {conf})")

    return (f"🔥 {name} — window OVERDUE by "
            f"**{format_timedelta(now - latest)}** (confidence: {conf})")

# ------------------------------------------------------------
# Bot Events / Background Loop
# ------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if not update_status_messages.is_running():
        update_status_messages.start()

@bot.event
async def on_command_error(ctx, error):
    """User-friendly error printing."""
    try:
        await ctx.send(f"Error: `{error}`")
    except Exception:
        pass
    print(f"[ERROR] {error}")

@tasks.loop(seconds=60)
async def update_status_messages():
    """Auto-refresh status board."""
    now = now_local()
    all_data = load_data()

    for guild in bot.guilds:
        gid = str(guild.id)
        gdata = all_data.get(gid)
        if not gdata:
            continue

        chan_id = gdata.get("status_channel_id")
        msg_id = gdata.get("status_message_id")
        mobs = gdata.get("mobs", {})

        if not chan_id:
            continue

        channel = guild.get_channel(chan_id)
        if not channel:
            continue

        if not mobs:
            content = "No mobs tracked. Use `!track MobName`."
        else:
            lines = [
                mob_status_line(key, mob, now)
                for key, mob in mobs.items()
                if mob.get("tracking", False)
            ]
            content = "__**Contested Mob Spawn Windows**__\n" + "\n".join(lines) if lines else "No mobs with tracking ON."

        try:
            if msg_id:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(content=content)
            else:
                msg = await channel.send(content)
                gdata["status_message_id"] = msg.id
                update_guild_data(guild.id, gdata)

        except discord.NotFound:
            msg = await channel.send(content)
            gdata["status_message_id"] = msg.id
            update_guild_data(guild.id, gdata)

        except discord.Forbidden:
            continue
# ------------------------------------------------------------
# TOD Command (explicit date + fuzzy)
# ------------------------------------------------------------

@bot.command(
    help=(
        "Record a Time of Death.\n"
        "Usage:\n"
        "  !tod MobName HHMM\n"
        "  !tod MobName YYYY-MM-DD HHMM\n"
        "  !tod MobName MM/DD/YYYY HHMM\n"
        "Examples:\n"
        "  !tod Pumpkinhead 0200\n"
        "  !tod Haraghur 2025-12-05 0210\n"
        "  !tod Vraksakin 12/05/2025 0630"
    )
)
async def tod(ctx, *, mob_and_time: str):
    now = now_local()
    parts = mob_and_time.split()

    time_token = None
    date_obj = None

    # Identify time token:
    if parts and looks_like_time(parts[-1]):
        time_token = parts[-1]
        parts = parts[:-1]

    # Identify date:
    if parts:
        possible_date = parts[-1]
        try:
            date_obj = parse_date_str(possible_date)
            parts = parts[:-1]
        except ValueError:
            date_obj = None

    mob_name = " ".join(parts).strip()
    if not mob_name:
        await ctx.send("Error: You must specify a mob name.")
        return

    # If date provided but no time:
    if date_obj and not time_token:
        await ctx.send("You must provide both date AND time. Example: `!tod Pumpkinhead 2025-12-05 0200`")
        return

    # Determine TOD timestamp:
    if time_token:
        try:
            tod_time = parse_time_str(time_token, for_date=date_obj)
        except ValueError as e:
            await ctx.send(f"Invalid time: {e}")
            return
    else:
        tod_time = now

    # Load DB:
    gdata = get_guild_data(ctx.guild.id)
    mobs = gdata["mobs"]

    # Fuzzy match:
    raw_key = normalize_mob_name(mob_name)
    fuzzy = fuzzy_find_mob(raw_key, mobs)

    if isinstance(fuzzy, list):
        await ctx.send("Mob name ambiguous. Did you mean:\n" +
                       "\n".join(f" • {mobs[k]['display_name']}" for k in fuzzy))
        return

    key = fuzzy if isinstance(fuzzy, str) else raw_key

    # Create mob entry if needed:
    if key not in mobs:
        mobs[key] = {
            "display_name": mob_name,
            "tracking": True,
            "min_respawn_hours": None,
            "max_respawn_hours": None,
            "last_death": None,
            "last_spawn": None,
            "tod_history": [],
            "learned_confidence": "LOW",
        }

    mob = mobs[key]
    mob["display_name"] = mob_name
    mob["last_death"] = tod_time.isoformat()

    # TOD history update:
    history = mob.get("tod_history", [])
    history.append(tod_time.isoformat())
    mob["tod_history"] = sorted(history)[-10:]  # keep last 10

    # Auto-learn:
    min_h, max_h, conf = update_window_from_tod_history(mob)

    update_guild_data(ctx.guild.id, gdata)

    when = tod_time.strftime("%Y-%m-%d %H:%M")
    msg = f"☠️ Recorded TOD for **{mob_name}** at `{when}`."
    if min_h is not None:
        msg += f"\n🧠 Auto-learned window: **{min_h}–{max_h} hours** (confidence: {conf})."
    await ctx.send(msg)


# ------------------------------------------------------------
# SPAWN Command
# ------------------------------------------------------------

@bot.command(help="Record a mob's spawn time. Usage: !spawn MobName [HHMM]")
async def spawn(ctx, *, mob_and_time: str):
    now = now_local()
    parts = mob_and_time.split()

    time_token = None
    if parts and looks_like_time(parts[-1]):
        time_token = parts[-1]
        mob_name = " ".join(parts[:-1])
    else:
        mob_name = mob_and_time

    mob_name = mob_name.strip()
    if not mob_name:
        await ctx.send("Usage: `!spawn MobName [HHMM]`")
        return

    if time_token:
        try:
            spawn_time = parse_time_str(time_token)
        except ValueError as e:
            await ctx.send(f"Invalid time: {e}")
            return
    else:
        spawn_time = now

    # Load DB:
    gdata = get_guild_data(ctx.guild.id)
    mobs = gdata["mobs"]

    # Fuzzy:
    raw_key = normalize_mob_name(mob_name)
    fuzzy = fuzzy_find_mob(raw_key, mobs)
    if isinstance(fuzzy, list):
        await ctx.send("Ambiguous mob name. Did you mean:\n" +
                       "\n".join(f" • {mobs[k]['display_name']}" for k in fuzzy))
        return
    key = fuzzy if isinstance(fuzzy, str) else raw_key

    # Create if new:
    if key not in mobs:
        mobs[key] = {
            "display_name": mob_name,
            "tracking": True,
            "min_respawn_hours": None,
            "max_respawn_hours": None,
            "last_death": None,
            "last_spawn": None,
            "tod_history": [],
            "learned_confidence": "LOW",
        }

    mob = mobs[key]
    mob["display_name"] = mob_name
    mob["last_spawn"] = spawn_time.isoformat()

    update_guild_data(ctx.guild.id, gdata)

    when = spawn_time.strftime("%Y-%m-%d %H:%M")
    await ctx.send(f"🌱 Recorded spawn for **{mob_name}** at `{when}`.")


# ------------------------------------------------------------
# TRACK / UNTRACK
# ------------------------------------------------------------

@bot.command(help="Start tracking a mob. Usage: !track MobName")
async def track(ctx, *, mob_name: str):
    gdata = get_guild_data(ctx.guild.id)
    mobs = gdata["mobs"]

    raw_key = normalize_mob_name(mob_name)
    fuzzy = fuzzy_find_mob(raw_key, mobs)

    if isinstance(fuzzy, list):
        await ctx.send("Ambiguous mob name. Did you mean:\n" +
                       "\n".join(f" • {mobs[k]['display_name']}" for k in fuzzy))
        return

    key = fuzzy if isinstance(fuzzy, str) else raw_key

    if key not in mobs:
        mobs[key] = {
            "display_name": mob_name,
            "tracking": True,
            "min_respawn_hours": None,
            "max_respawn_hours": None,
            "last_death": None,
            "last_spawn": None,
            "tod_history": [],
            "learned_confidence": "LOW",
        }
    else:
        mobs[key]["display_name"] = mob_name
        mobs[key]["tracking"] = True

    update_guild_data(ctx.guild.id, gdata)
    await ctx.send(f"🟢 Tracking **{mob_name}** enabled.")

@bot.command(help="Stop tracking a mob (keeps its data). Usage: !untrack MobName")
async def untrack(ctx, *, mob_name: str):
    gdata = get_guild_data(ctx.guild.id)
    mobs = gdata["mobs"]

    raw_key = normalize_mob_name(mob_name)
    fuzzy = fuzzy_find_mob(raw_key, mobs)
    if isinstance(fuzzy, list):
        await ctx.send("Ambiguous mob name. Did you mean:\n" +
                       "\n".join(f" • {mobs[k]['display_name']}" for k in fuzzy))
        return
    key = fuzzy if isinstance(fuzzy, str) else raw_key

    if key not in mobs:
        await ctx.send(f"Mob **{mob_name}** not found.")
        return

    mobs[key]["tracking"] = False
    update_guild_data(ctx.guild.id, gdata)
    await ctx.send(f"🔴 Tracking **{mobs[key]['display_name']}** disabled.")


# ------------------------------------------------------------
# DELETE / RENAME / UNDO
# ------------------------------------------------------------

@bot.command(help="Delete a mob entirely. Usage: !deletemob MobName")
async def deletemob(ctx, *, mob_name: str):
    gdata = get_guild_data(ctx.guild.id)
    mobs = gdata["mobs"]

    raw_key = normalize_mob_name(mob_name)
    fuzzy = fuzzy_find_mob(raw_key, mobs)
    if isinstance(fuzzy, list):
        await ctx.send("Ambiguous mob name:\n" +
                       "\n".join(f" • {mobs[k]['display_name']}" for k in fuzzy))
        return

    key = fuzzy if isinstance(fuzzy, str) else raw_key
    if key not in mobs:
        await ctx.send(f"Mob **{mob_name}** not found.")
        return

    del mobs[key]
    update_guild_data(ctx.guild.id, gdata)
    await ctx.send(f"🗑️ Deleted mob **{mob_name}**.")

@bot.command(help="Rename a mob. Usage: !renamemob Old Name | New Name")
async def renamemob(ctx, *, args: str):
    if "|" not in args:
        await ctx.send("Usage: `!renamemob Old Name | New Name`")
        return

    old_name, new_name = [s.strip() for s in args.split("|", 1)]

    gdata = get_guild_data(ctx.guild.id)
    mobs = gdata["mobs"]

    # Fuzzy match old name
    raw_key = normalize_mob_name(old_name)
    fuzzy = fuzzy_find_mob(raw_key, mobs)
    if isinstance(fuzzy, list):
        await ctx.send("Ambiguous original name:\n" +
                       "\n".join(f" • {mobs[k]['display_name']}" for k in fuzzy))
        return
    old_key = fuzzy if isinstance(fuzzy, str) else raw_key

    if old_key not in mobs:
        await ctx.send(f"Mob **{old_name}** not found.")
        return

    new_key = normalize_mob_name(new_name)
    if new_key in mobs and new_key != old_key:
        await ctx.send(f"A mob named **{new_name}** already exists.")
        return

    mob = mobs.pop(old_key)
    mob["display_name"] = new_name
    mobs[new_key] = mob

    update_guild_data(ctx.guild.id, gdata)
    await ctx.send(f"✏️ Renamed **{old_name}** → **{new_name}**.")

@bot.command(help="Undo the last TOD entry. Usage: !undo MobName")
async def undo(ctx, *, mob_name: str):
    gdata = get_guild_data(ctx.guild.id)
    mobs = gdata["mobs"]

    raw_key = normalize_mob_name(mob_name)
    fuzzy = fuzzy_find_mob(raw_key, mobs)
    if isinstance(fuzzy, list):
        await ctx.send("Ambiguous name:\n" +
                       "\n".join(f" • {mobs[k]['display_name']}" for k in fuzzy))
        return

    key = fuzzy if isinstance(fuzzy, str) else raw_key

    if key not in mobs:
        await ctx.send(f"Mob **{mob_name}** not tracked.")
        return

    mob = mobs[key]
    history = mob.get("tod_history", [])

    if not history:
        await ctx.send(f"No TOD history for **{mob['display_name']}**.")
        return

    removed = history.pop()
    mob["tod_history"] = history

    if len(history) >= 2:
        min_h, max_h, conf = update_window_from_tod_history(mob)
        msg = (f"↩️ Removed last TOD (`{removed}`).\n"
               f"New window: **{min_h}–{max_h} hours** (confidence: {conf}).")
    else:
        mob["min_respawn_hours"] = None
        mob["max_respawn_hours"] = None
        mob["learned_confidence"] = "LOW"
        msg = (
            f"↩️ Removed last TOD (`{removed}`).\n"
            f"Not enough TOD data to compute a window."
        )

    update_guild_data(ctx.guild.id, gdata)
    await ctx.send(msg)


# ------------------------------------------------------------
# SET WINDOW
# ------------------------------------------------------------

@bot.command(help="Set spawn window manually. Usage: !setwindow MobName min max")
async def setwindow(ctx, *, args: str):
    parts = args.split()
    if len(parts) < 3:
        await ctx.send("Usage: `!setwindow MobName min max`")
        return

    try:
        min_h = float(parts[-2])
        max_h = float(parts[-1])
    except ValueError:
        await ctx.send("min and max must be numbers.")
        return

    mob_name = " ".join(parts[:-2])
    gdata = get_guild_data(ctx.guild.id)
    mobs = gdata["mobs"]

    raw_key = normalize_mob_name(mob_name)
    fuzzy = fuzzy_find_mob(raw_key, mobs)
    if isinstance(fuzzy, list):
        await ctx.send("Ambiguous mob name:\n" +
                       "\n".join(f" • {mobs[k]['display_name']}" for k in fuzzy))
        return
    key = fuzzy if isinstance(fuzzy, str) else raw_key

    if key not in mobs:
        mobs[key] = {
            "display_name": mob_name,
            "tracking": True,
            "min_respawn_hours": min_h,
            "max_respawn_hours": max_h,
            "last_death": None,
            "last_spawn": None,
            "tod_history": [],
            "learned_confidence": "LOW",
        }
    else:
        mobs[key]["min_respawn_hours"] = min_h
        mobs[key]["max_respawn_hours"] = max_h

    update_guild_data(ctx.guild.id, gdata)
    await ctx.send(f"⏱️ Window for **{mob_name}** set to **{min_h}-{max_h} hours**.")


# ------------------------------------------------------------
# STATUS CHANNEL
# ------------------------------------------------------------

@bot.command(help="Set the channel used for auto-updating status board.")
@commands.has_permissions(manage_channels=True)
async def setstatuschannel(ctx, channel: discord.TextChannel = None):
    if channel is None:
        channel = ctx.channel

    gdata = get_guild_data(ctx.guild.id)
    gdata["status_channel_id"] = channel.id
    gdata["status_message_id"] = None

    update_guild_data(ctx.guild.id, gdata)

    await ctx.send(f"📡 Status updates will now appear in {channel.mention}.")


# ------------------------------------------------------------
# STATUS (manual)
# ------------------------------------------------------------

@bot.command(help="Show current spawn windows.")
async def status(ctx):
    now = now_local()
    gdata = get_guild_data(ctx.guild.id)
    mobs = gdata.get("mobs", {})

    if not mobs:
        await ctx.send("No mobs tracked.")
        return

    lines = [
        mob_status_line(key, mob, now)
        for key, mob in mobs.items()
        if mob.get("tracking", False)
    ]

    if not lines:
        await ctx.send("No mobs have tracking enabled.")
        return

    await ctx.send("__**Contested Mob Spawn Windows**__\n" + "\n".join(lines))


# ------------------------------------------------------------
# RUN BOT
# ------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN not set.")
    else:
        bot.run(TOKEN)
