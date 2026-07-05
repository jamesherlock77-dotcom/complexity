import io
import json
import os
import time
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: set for instant slash-command sync during testing
JOIN_LOG_CHANNEL_ID = int(os.getenv("JOIN_LOG_CHANNEL_ID", "1522303537306927317"))
DB_CHANNEL_ID = int(os.getenv("DB_CHANNEL_ID", "1521201722930757649"))
FAKE_ACCOUNT_AGE_DAYS = int(os.getenv("FAKE_ACCOUNT_AGE_DAYS", "7"))

intents = discord.Intents.default()
intents.members = True  # required: enable "Server Members Intent" in the Discord Developer Portal

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------------------------------------------------------------------
# "Database": a JSON blob kept as a file attachment on one message in
# DB_CHANNEL_ID. Discord stores it permanently, so it survives Railway
# redeploys with no paid volume needed.
# ---------------------------------------------------------------------------

DB_FILENAME = "database.json"

state = {
    "stats": {},    # {guild_id: {user_id: {regular, left_count, fake, bonus}}}
    "records": {},  # {guild_id: {member_id: {inviter_id, code, is_fake, is_vanity}}}
}

db_message: discord.Message | None = None
save_lock = asyncio.Lock()


async def load_state():
    """Load the database file from the DB channel, if one already exists."""
    global db_message, state

    channel = bot.get_channel(DB_CHANNEL_ID)
    if channel is None:
        print(f"WARNING: could not find DB_CHANNEL_ID={DB_CHANNEL_ID}. Starting with empty data.")
        return

    async for message in channel.history(limit=50):
        if message.author.id == bot.user.id:
            for attachment in message.attachments:
                if attachment.filename == DB_FILENAME:
                    raw = await attachment.read()
                    try:
                        loaded = json.loads(raw.decode("utf-8"))
                        loaded.setdefault("stats", {})
                        loaded.setdefault("records", {})
                        state = loaded
                    except json.JSONDecodeError:
                        print("WARNING: database.json was unreadable, starting fresh.")
                    db_message = message
                    print(f"Loaded database from message {message.id} in #{channel.name}.")
                    return

    print("No existing database found in the DB channel — starting fresh.")


async def save_state():
    """Persist the current state to the DB channel as a file attachment."""
    global db_message

    channel = bot.get_channel(DB_CHANNEL_ID)
    if channel is None:
        return

    buffer = io.BytesIO(json.dumps(state, indent=2).encode("utf-8"))
    file = discord.File(buffer, filename=DB_FILENAME)
    content = f"\U0001f5c4\ufe0f Invite database — last updated <t:{int(time.time())}:f>"

    async with save_lock:
        if db_message is not None:
            try:
                db_message = await db_message.edit(content=content, attachments=[file])
                return
            except discord.NotFound:
                db_message = None  # message was deleted, fall through to re-create it

        db_message = await channel.send(content=content, file=file)


def get_stats(guild_id: int, user_id: int):
    guild_stats = state["stats"].setdefault(str(guild_id), {})
    return guild_stats.setdefault(str(user_id), {"regular": 0, "left_count": 0, "fake": 0, "bonus": 0})


def total_invites(guild_id: int, user_id: int) -> int:
    s = get_stats(guild_id, user_id)
    return max(s["regular"] - s["left_count"] + s["bonus"], 0)


def set_join_record(guild_id, member_id, inviter_id, code, is_fake, is_vanity):
    guild_records = state["records"].setdefault(str(guild_id), {})
    guild_records[str(member_id)] = {
        "inviter_id": inviter_id,
        "code": code,
        "is_fake": is_fake,
        "is_vanity": is_vanity,
        "joined_at": datetime.now(timezone.utc).isoformat(),
    }


def pop_join_record(guild_id, member_id):
    guild_records = state["records"].setdefault(str(guild_id), {})
    return guild_records.pop(str(member_id), None)


# ---------------------------------------------------------------------------
# Invite cache: guild_id -> {code: uses}. "__vanity__" is a special key.
# This is rebuilt live from Discord each session — it's not persisted.
# ---------------------------------------------------------------------------

invite_cache: dict[int, dict[str, int]] = {}


async def cache_guild_invites(guild: discord.Guild):
    data = {}
    try:
        for inv in await guild.invites():
            data[inv.code] = inv.uses
    except discord.Forbidden:
        pass

    if "VANITY_URL" in guild.features:
        try:
            vanity = await guild.vanity_invite()
            if vanity:
                data["__vanity__"] = vanity.uses
        except (discord.Forbidden, discord.HTTPException):
            pass

    invite_cache[guild.id] = data


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@bot.event
async def on_ready():
    await load_state()

    for guild in bot.guilds:
        await cache_guild_invites(guild)

    if GUILD_ID:
        guild_obj = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild_obj)
        await tree.sync(guild=guild_obj)
    else:
        await tree.sync()

    print(f"Logged in as {bot.user} — tracking invites in {len(bot.guilds)} server(s).")


@bot.event
async def on_guild_join(guild: discord.Guild):
    await cache_guild_invites(guild)


@bot.event
async def on_invite_create(invite: discord.Invite):
    invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses


@bot.event
async def on_invite_delete(invite: discord.Invite):
    invite_cache.get(invite.guild.id, {}).pop(invite.code, None)


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    old_cache = invite_cache.get(guild.id, {})

    try:
        current_invites = await guild.invites()
    except discord.Forbidden:
        current_invites = []

    used_invite = None
    for inv in current_invites:
        if inv.uses > old_cache.get(inv.code, 0):
            used_invite = inv
            break

    new_cache = {inv.code: inv.uses for inv in current_invites}

    vanity_used = False
    inviter = None
    code = None

    if used_invite:
        inviter = used_invite.inviter
        code = used_invite.code
    elif "VANITY_URL" in guild.features:
        try:
            vanity = await guild.vanity_invite()
            if vanity and vanity.uses > old_cache.get("__vanity__", 0):
                vanity_used = True
                code = vanity.code
            if vanity:
                new_cache["__vanity__"] = vanity.uses
        except (discord.Forbidden, discord.HTTPException):
            pass

    invite_cache[guild.id] = new_cache

    account_age_days = (datetime.now(timezone.utc) - member.created_at).days
    is_fake = inviter is not None and account_age_days < FAKE_ACCOUNT_AGE_DAYS

    set_join_record(guild.id, member.id, inviter.id if inviter else None, code, is_fake, vanity_used)

    if vanity_used:
        msg = f"{member.mention} joined using a vanity invite."
    elif inviter and is_fake:
        stats = get_stats(guild.id, inviter.id)
        stats["fake"] += 1
        msg = (
            f"{member.mention} has been invited by {inviter.mention}, "
            f"but this invite is **fake** (account created {account_age_days} day(s) ago)."
        )
    elif inviter:
        stats = get_stats(guild.id, inviter.id)
        stats["regular"] += 1
        total = total_invites(guild.id, inviter.id)
        msg = f"{member.mention} has been invited by {inviter.mention} and has now {total} invites."
    else:
        msg = f"{member.mention} joined, but I couldn't determine which invite was used."

    await save_state()

    log_channel = bot.get_channel(JOIN_LOG_CHANNEL_ID)
    if log_channel is not None:
        await log_channel.send(msg)


@bot.event
async def on_member_remove(member: discord.Member):
    record = pop_join_record(member.guild.id, member.id)
    if record is None:
        return

    inviter_id = record["inviter_id"]
    if inviter_id and not record["is_fake"] and not record["is_vanity"]:
        stats = get_stats(member.guild.id, inviter_id)
        stats["left_count"] += 1

    await save_state()


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


@tree.command(name="invites", description="Check invite stats for yourself or another member")
@app_commands.describe(member="The member to check (defaults to you)")
async def invites_cmd(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    stats = get_stats(interaction.guild.id, target.id)
    total = total_invites(interaction.guild.id, target.id)

    embed = discord.Embed(
        title=target.display_name,
        description=(
            f"You currently have **{total}** invites. "
            f"({stats['regular']} regular, {stats['left_count']} left, "
            f"{stats['fake']} fake, {stats['bonus']} bonus)"
        ),
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Add it to your .env file or Railway environment variables.")
    bot.run(TOKEN)
