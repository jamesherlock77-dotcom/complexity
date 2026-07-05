import os
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: set for instant slash-command sync during testing
JOIN_LOG_CHANNEL_ID = int(os.getenv("JOIN_LOG_CHANNEL_ID", "1522303537306927317"))
FAKE_ACCOUNT_AGE_DAYS = int(os.getenv("FAKE_ACCOUNT_AGE_DAYS", "7"))
DB_PATH = os.getenv("DB_PATH", "invites.db")

intents = discord.Intents.default()
intents.members = True  # required: enable "Server Members Intent" in the Discord Developer Portal

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

db = sqlite3.connect(DB_PATH)
db.execute(
    """
    CREATE TABLE IF NOT EXISTS invite_stats (
        guild_id INTEGER,
        user_id INTEGER,
        regular INTEGER DEFAULT 0,
        left_count INTEGER DEFAULT 0,
        fake INTEGER DEFAULT 0,
        bonus INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )
    """
)
db.execute(
    """
    CREATE TABLE IF NOT EXISTS join_records (
        guild_id INTEGER,
        member_id INTEGER,
        inviter_id INTEGER,
        invite_code TEXT,
        is_fake INTEGER,
        is_vanity INTEGER,
        joined_at TEXT,
        PRIMARY KEY (guild_id, member_id)
    )
    """
)
db.commit()


def get_stats(guild_id: int, user_id: int):
    row = db.execute(
        "SELECT regular, left_count, fake, bonus FROM invite_stats WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        return {"regular": 0, "left_count": 0, "fake": 0, "bonus": 0}
    return {"regular": row[0], "left_count": row[1], "fake": row[2], "bonus": row[3]}


def adjust_stat(guild_id: int, user_id: int, field: str, delta: int):
    db.execute(
        f"""
        INSERT INTO invite_stats (guild_id, user_id, {field})
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET {field} = {field} + excluded.{field}
        """,
        (guild_id, user_id, delta),
    )
    db.commit()


def total_invites(guild_id: int, user_id: int) -> int:
    s = get_stats(guild_id, user_id)
    return max(s["regular"] - s["left_count"] + s["bonus"], 0)


def save_join_record(guild_id, member_id, inviter_id, code, is_fake, is_vanity):
    db.execute(
        """
        INSERT INTO join_records (guild_id, member_id, inviter_id, invite_code, is_fake, is_vanity, joined_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, member_id) DO UPDATE SET
            inviter_id=excluded.inviter_id,
            invite_code=excluded.invite_code,
            is_fake=excluded.is_fake,
            is_vanity=excluded.is_vanity,
            joined_at=excluded.joined_at
        """,
        (guild_id, member_id, inviter_id, code, int(is_fake), int(is_vanity), datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def pop_join_record(guild_id, member_id):
    row = db.execute(
        "SELECT inviter_id, is_fake, is_vanity FROM join_records WHERE guild_id=? AND member_id=?",
        (guild_id, member_id),
    ).fetchone()
    db.execute("DELETE FROM join_records WHERE guild_id=? AND member_id=?", (guild_id, member_id))
    db.commit()
    return row


# ---------------------------------------------------------------------------
# Invite cache: guild_id -> {code: uses}. "__vanity__" is a special key.
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

    save_join_record(guild.id, member.id, inviter.id if inviter else None, code, is_fake, vanity_used)

    log_channel = bot.get_channel(JOIN_LOG_CHANNEL_ID)
    if log_channel is None:
        return

    if vanity_used:
        msg = f"{member.mention} joined using a vanity invite."
    elif inviter and is_fake:
        adjust_stat(guild.id, inviter.id, "fake", 1)
        msg = (
            f"{member.mention} has been invited by {inviter.mention}, "
            f"but this invite is **fake** (account created {account_age_days} day(s) ago)."
        )
    elif inviter:
        adjust_stat(guild.id, inviter.id, "regular", 1)
        total = total_invites(guild.id, inviter.id)
        msg = f"{member.mention} has been invited by {inviter.mention} and has now {total} invites."
    else:
        msg = f"{member.mention} joined, but I couldn't determine which invite was used."

    await log_channel.send(msg)


@bot.event
async def on_member_remove(member: discord.Member):
    record = pop_join_record(member.guild.id, member.id)
    if record is None:
        return
    inviter_id, is_fake, is_vanity = record
    if inviter_id and not is_fake and not is_vanity:
        adjust_stat(member.guild.id, inviter_id, "left_count", 1)


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
