import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# -----------------------
# Config & setup
# -----------------------

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Change this if you want a different canonical timezone
# Examples:
#   "America/Los_Angeles" (Pacific)
#   "America/Chicago"
#   "UTC"
TIMEZONE = ZoneInfo("America/New_York")

INTENTS = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=INTENTS)

DATA_FILE = "mobs_data.json"


# -----------------------
# Helpers: time & data
# -----------------------

def now_local() -> datetime:
    """Current time in the bot's canonical timezone (aware datetime)."""
    return datetime.now(TIMEZONE)


def load_data():
    """Load the full JSON data file."""
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_data(data):
    """Save the full JSON data file."""
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_guild_data(guild_id):
    """
    Return the data dict for a single guild, always with a 'mobs' dict.

    Overall structure in mobs_data.json is:
    {
        "guild_id_str": {
            "status_channel_id": int | null,
            "status_message_id": int | null,
            "mobs": {
                "mob_key": { ... }
            }
        },
        ...
    }
    """
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

    guild_data = data[gid]
    if "status_channel_id" not in guild_data:
        guild_data["status_channel_id"] = None
    if "status_message_id" not in guild_data:
        guild_data["status_message_id"] = None
    if "mobs" not in guild_data:
        guild_data["mobs"] = {}

    save_data(data)
    return guild_data


def update_guild_data(guild_id, new_guild_data):
    """Write back a single guild's data into the JSON file."""
    data = load_data()
    data[str(guild_id)] = new_guild_data
    save_data(data)


def normalize_mob_name(name: str) -> str:
    """Canonical key for a mob name."""
    return name.strip().lower()


def format_timedelta(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = -total_seconds
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def parse_time_str(time_str: str) -> datetime:
    """
    Interpret HHMM or HMM as a time in the bot's canonical timezone.

    Example:
      "2000" -> most recent 20:00 in TIMEZONE.
      If that time hasn't happened yet today, assume it was yesterday.
    """
    now = now_local()
    time_str = time_str.strip()
    if not time_str.isdigit() or len(time_str) not in (3, 4):
        raise ValueError("Time must be HMM or HHMM, e.g. 215 or 0215.")

    if len(time_str) == 3:
        hour = int(time_str[0])
        minute = int(time_str[1:])
    else:
        hour = int(time_str[:2])
        minute = int(time_str[2:])

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Invalid hour/minute values.")

    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If that clock time is still in our future, assume it was yesterday
    if dt > now:
        dt -= timedelta(days=1)

    return dt


def update_window_from_tod_history(mob_data: dict):
    """
    Use the mob's TOD history to auto-learn a spawn window.
    Stores:
      mob_data["min_respawn_hours"]
      mob_data["max_respawn_hours"]
      mob_data["learned_confidence"]

    Returns: (min_h, max_h, confidence) or (None, None, "LOW") if not enough data.
    """
    history = mob_data.get("tod_history", [])
    if len(history) < 2:
        mob_data["learned_confidence"] = "LOW"
        return None, None, "LOW"

    # Parse and sort TODs (aware datetimes in TIMEZONE)
    times = sorted(datetime.fromisoformat(t) for t in history)
    intervals = []
    for a, b in zip(times, times[1:]):
        delta_hours = (b - a).total_seconds() / 3600.0
        # Ignore obviously bogus tiny values (<15 minutes)
        if delta_hours > 0.25:
            intervals.append(delta_hours)

    if not intervals:
        mob_data["learned_confidence"] = "LOW"
        return None, None, "LOW"

    intervals.sort()
    trimmed = intervals

    # If we have 4+ samples, drop one smallest and one largest as outliers
    if len(intervals) >= 4:
        trimmed = intervals[1:-1]

    min_h = min(trimmed) * 0.95  # small safety margin
    max_h = max(trimmed) * 1.05

    # Confidence based on count of intervals
    n = len(intervals)
    if n < 3:
        confidence = "LOW"
    elif n < 6:
        confidence = "MEDIUM"
    else:
        confidence = "HIGH"

    mob_data["min_respawn_hours"] = round(min_h, 2)
    mob_data["max_respawn_hours"] = round(max_h, 2)
    mob_data["learned_confidence"] = confidence

    return mob_data["min_respawn_hours"], mob_data["max_respawn_hours"], confidence


def mob_status_line(mob_name: str, mob_data: dict, now_time: datetime) -> str:
    """Build a single status line for a mob, including confidence info."""
    if not mob_data.get("tracking", False):
        return f"❌ {mob_data.get('display_name', mob_name)} — tracking OFF"

    min_h = mob_data.get("min_respawn_hours")
    max_h = mob_data.get("max_respawn_hours")
    last_death = mob_data.get("last_death")
    last_spawn = mob_data.get("last_spawn")
    confidence = mob_data.get("learned_confidence")
    conf_suffix = f" (confidence: {confidence})" if confidence else ""

    if min_h is None or max_h is None:
        return (f"⚠️ {mob_data.get('display_name', mob_name)} — spawn window not set "
                f"(`!setwindow {mob_name} min max`)")

    base_time = None
    if last_death:
        base_time = datetime.fromisoformat(last_death)
    elif last_spawn:
        base_time = datetime.fromisoformat(last_spawn)

    if base_time is None:
        return f"ℹ️ {mob_data.get('display_name', mob_name)} — no TOD or spawn recorded yet." + conf_suffix

    earliest = base_time + timedelta(hours=min_h)
    latest = base_time + timedelta(hours=max_h)

    if now_time < earliest:
        until_open = earliest - now_time
        return (f"⏳ {mob_data.get('display_name', mob_name)} — window CLOSED, "
                f"opens in **{format_timedelta(until_open)}**" + conf_suffix)
    elif earliest <= now_time <= latest:
        until_close = latest - now_time
        return (f"✅ {mob_data.get('display_name', mob_name)} — **WINDOW OPEN**, "
                f"~{format_timedelta(until_close)} left until late limit" + conf_suffix)
    else:
        overdue = now_time - latest
        return (f"🔥 {mob_data.get('display_name', mob_name)} — window OVERDUE by "
                f"**{format_timedelta(overdue)}** (likely up / killed unseen)" + conf_suffix)


# -----------------------
# Bot events & tasks
# -----------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if not update_status_messages.is_running():
        update_status_messages.start()


@bot.event
async def on_command_error(ctx, error):
    # Simple error handler so you see issues in Discord and console
    print(f"Command error: {error}")
    try:
        await ctx.send(f"Error: `{error}`")
    except Exception:
        pass


@tasks.loop(seconds=60)
async def update_status_messages():
    now_time = now_local()
    all_data = load_data()

    for guild in bot.guilds:
        gid = str(guild.id)
        guild_data = all_data.get(gid)
        if not guild_data:
            continue

        channel_id = guild_data.get("status_channel_id")
        message_id = guild_data.get("status_message_id")
        mobs = guild_data.get("mobs", {})

        if not channel_id:
            continue

        channel = guild.get_channel(channel_id)
        if not channel:
            continue

        if not mobs:
            content = "No mobs are being tracked yet. Use `!track MobName` to start."
        else:
            lines = []
            for key, mob_data in mobs.items():
                if mob_data.get("tracking", False):
                    lines.append(mob_status_line(key, mob_data, now_time))
            if not lines:
                content = "No mobs are currently set to tracking ON."
            else:
                content = "__**Contested Mob Spawn Windows**__\n" + "\n".join(lines)

        try:
            if message_id:
                msg = await channel.fetch_message(message_id)
                await msg.edit(content=content)
            else:
                msg = await channel.send(content)
                guild_data["status_message_id"] = msg.id
                update_guild_data(guild.id, guild_data)
        except discord.NotFound:
            msg = await channel.send(content)
            guild_data["status_message_id"] = msg.id
            update_guild_data(guild.id, guild_data)
        except discord.Forbidden:
            continue


# -----------------------
# Commands
# -----------------------

@bot.command(help="Mark Time Of Death. Usage: !tod Mob Name [HHMM]")
async def tod(ctx, *, mob_and_time: str):
    """Record TOD and auto-learn window from TOD history (canonical timezone)."""
    current = now_local()

    parts = mob_and_time.split()
    time_str = None

    if parts and parts[-1].isdigit() and len(parts[-1]) in (3, 4):
        time_str = parts[-1]
        mob_name = " ".join(parts[:-1])
    else:
        mob_name = mob_and_time

    mob_name = mob_name.strip()
    if not mob_name:
        await ctx.send("Please specify a mob name, e.g. `!tod Pumpkinhead`.")
        return

    if time_str:
        try:
            tod_time = parse_time_str(time_str)
        except ValueError as e:
            await ctx.send(f"Invalid time: {e}")
            return
    else:
        tod_time = current

    guild_data = get_guild_data(ctx.guild.id)
    mobs = guild_data["mobs"]
    key = normalize_mob_name(mob_name)

    if key not in mobs:
        mobs[key] = {
            "display_name": mob_name,
            "tracking": True,
            "min_respawn_hours": None,
            "max_respawn_hours": None,
            "last_death": None,
            "last_spawn": None,
            "tod_history": []
        }

    mob = mobs[key]
    mob["display_name"] = mob_name
    mob["last_death"] = tod_time.isoformat()

    # Record TOD history & auto-learn
    history = mob.get("tod_history", [])
    history.append(tod_time.isoformat())
    history = sorted(history)[-10:]  # keep last 10
    mob["tod_history"] = history

    min_h, max_h, confidence = update_window_from_tod_history(mob)

    guild_data["mobs"] = mobs
    update_guild_data(ctx.guild.id, guild_data)

    tod_local_str = tod_time.strftime("%Y-%m-%d %H:%M")
    msg = f"☠️ Recorded TOD for **{mob_name}** at `{tod_local_str}`."

    if min_h is not None and max_h is not None:
        msg += f"\n🧠 Auto-learned window: **{min_h}–{max_h} hours** (confidence: {confidence})."

    await ctx.send(msg)


@bot.command(help="Mark a spawn time. Usage: !spawn Mob Name [HHMM]")
async def spawn(ctx, *, mob_and_time: str):
    """Record when you actually saw the mob spawn (canonical timezone)."""
    current = now_local()

    parts = mob_and_time.split()
    time_str = None

    if parts and parts[-1].isdigit() and len(parts[-1]) in (3, 4):
        time_str = parts[-1]
        mob_name = " ".join(parts[:-1])
    else:
        mob_name = mob_and_time

    mob_name = mob_name.strip()
    if not mob_name:
        await ctx.send("Usage: `!spawn MobName [HHMM]`")
        return

    if time_str:
        try:
            spawn_time = parse_time_str(time_str)
        except ValueError as e:
            await ctx.send(f"Invalid time: {e}")
            return
    else:
        spawn_time = current

    guild_data = get_guild_data(ctx.guild.id)
    mobs = guild_data["mobs"]
    key = normalize_mob_name(mob_name)

    if key not in mobs:
        mobs[key] = {
            "display_name": mob_name,
            "tracking": True,
            "min_respawn_hours": None,
            "max_respawn_hours": None,
            "last_death": None,
            "last_spawn": None,
            "tod_history": []
        }

    mob = mobs[key]
    mob["display_name"] = mob_name
    mob["last_spawn"] = spawn_time.isoformat()

    guild_data["mobs"] = mobs
    update_guild_data(ctx.guild.id, guild_data)

    spawn_local_str = spawn_time.strftime("%Y-%m-%d %H:%M")
    await ctx.send(f"🌱 Recorded spawn for **{mob_name}** at `{spawn_local_str}`.")


@bot.command(help="Toggle tracking for a mob on/off. Usage: !track MobName")
async def track(ctx, *, mob_name: str):
    """Turn tracking on/off for a mob, creating it if needed."""
    mob_name = mob_name.strip()
    if not mob_name:
        await ctx.send("Usage: `!track Mob Name`")
        return

    guild_data = get_guild_data(ctx.guild.id)
    mobs = guild_data["mobs"]
    key = normalize_mob_name(mob_name)

    if key not in mobs:
        mobs[key] = {
            "display_name": mob_name,
            "tracking": True,
            "min_respawn_hours": None,
            "max_respawn_hours": None,
            "last_death": None,
            "last_spawn": None,
            "tod_history": []
        }
        state = "ON"
    else:
        mob = mobs[key]
        mob["display_name"] = mob_name
        mob["tracking"] = not mob.get("tracking", False)
        state = "ON" if mob["tracking"] else "OFF"

    guild_data["mobs"] = mobs
    update_guild_data(ctx.guild.id, guild_data)

    await ctx.send(f"Tracking for **{mob_name}** is now **{state}**.")


@bot.command(help="Set a mob's spawn window manually. Usage: !setwindow MobName min max")
async def setwindow(ctx, *, args: str):
    """
    Example:
      !setwindow Pumpkinhead 8 12
    """
    parts = args.split()
    if len(parts) < 3:
        await ctx.send("Usage: `!setwindow MobName min max` (hours). Example: `!setwindow Pumpkinhead 8 12`")
        return

    try:
        min_h = float(parts[-2])
        max_h = float(parts[-1])
    except ValueError:
        await ctx.send("The last two values must be numbers (hours). Example: `!setwindow Pumpkinhead 8 12`")
        return

    if min_h <= 0 or max_h <= 0 or min_h > max_h:
        await ctx.send("Invalid window. Require: min > 0, max > 0, and min <= max.")
        return

    mob_name = " ".join(parts[:-2]).strip()
    if not mob_name:
        await ctx.send("Please include the mob name before the min/max hours.")
        return

    guild_data = get_guild_data(ctx.guild.id)
    mobs = guild_data["mobs"]
    key = normalize_mob_name(mob_name)

    if key not in mobs:
        mobs[key] = {
            "display_name": mob_name,
            "tracking": True,
            "min_respawn_hours": min_h,
            "max_respawn_hours": max_h,
            "last_death": None,
            "last_spawn": None,
            "tod_history": []
        }
    else:
        mob = mobs[key]
        mob["display_name"] = mob_name
        mob["min_respawn_hours"] = min_h
        mob["max_respawn_hours"] = max_h

    guild_data["mobs"] = mobs
    update_guild_data(ctx.guild.id, guild_data)

    await ctx.send(f"⏱️ Window for **{mob_name}** set to **{min_h}–{max_h} hours**.")


@bot.command(help="Set the channel where the auto-updating spawn list will appear.")
@commands.has_permissions(manage_channels=True)
async def setstatuschannel(ctx, channel: discord.TextChannel = None):
    if channel is None:
        channel = ctx.channel

    guild_data = get_guild_data(ctx.guild.id)
    guild_data["status_channel_id"] = channel.id
    guild_data["status_message_id"] = None
    update_guild_data(ctx.guild.id, guild_data)

    await ctx.send(f"✅ Status updates will now appear in {channel.mention}.")


@bot.command(help="Show immediate spawn status.")
async def status(ctx):
    now_time = now_local()
    guild_data = get_guild_data(ctx.guild.id)
    mobs = guild_data.get("mobs", {})

    if not mobs:
        await ctx.send("No mobs are being tracked. Use `!track MobName` to start.")
        return

    lines = []
    for key, mob_data in mobs.items():
        if mob_data.get("tracking", False):
            lines.append(mob_status_line(key, mob_data, now_time))

    if not lines:
        await ctx.send("No mobs currently have tracking enabled.")
        return

    content = "__**Contested Mob Spawn Windows**__\n" + "\n".join(lines)
    await ctx.send(content)


# -----------------------
# Run bot
# -----------------------

if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN not found in environment.")
    else:
        bot.run(TOKEN)
