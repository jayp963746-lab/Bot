
import os
import re
import json
import random
import asyncio
import time
from collections import defaultdict
from datetime import timedelta, timezone, datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
from dotenv import load_dotenv

load_dotenv()
TOKEN   = os.getenv("DISCORD_TOKEN")
DB_PATH = "bot.db"

intents = discord.Intents.default()
intents.message_content = True
intents.members          = True

bot      = commands.Bot(command_prefix="!", intents=intents)
bot.db   = None  # assigned in setup_hook

INVITE_RE = re.compile(
    r"(discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)\S+",
    re.IGNORECASE,
)

# ── Anti-nuke in-memory tracking ──────────────────────────────────────────────
# { guild_id: { user_id: { action: [epoch_ts, ...] } } }
nuke_tracker: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
NUKE_WINDOW              = 10   # seconds to look back
NUKE_BAN_THRESH          = 3
NUKE_CHANNEL_DEL_THRESH  = 3
NUKE_ROLE_DEL_THRESH     = 3

# ── Anti-raid in-memory tracking ──────────────────────────────────────────────
# { guild_id: [epoch_ts, ...] }
raid_tracker: dict = defaultdict(list)

# ── RPG constants ─────────────────────────────────────────────────────────────
RPG_CLASSES = {
    "warrior": {"emoji": "⚔️",  "hp": 120, "attack": 12, "defense": 10, "crit": 5},
    "mage":    {"emoji": "🔮",  "hp":  80, "attack": 20, "defense":  5, "crit": 10},
    "archer":  {"emoji": "🏹",  "hp": 100, "attack": 15, "defense":  7, "crit": 15},
    "rogue":   {"emoji": "🗡️", "hp":  90, "attack": 18, "defense":  6, "crit": 20},
}

MONSTERS = [
    {"name": "Slime 🟢",    "hp":  20, "atk":  3, "def":  0, "xp":  15, "gmin":   5, "gmax":  15},
    {"name": "Goblin 👺",   "hp":  45, "atk":  8, "def":  2, "xp":  30, "gmin":  10, "gmax":  30},
    {"name": "Skeleton 💀", "hp":  60, "atk": 12, "def":  3, "xp":  50, "gmin":  20, "gmax":  50},
    {"name": "Orc ⚔️",     "hp":  90, "atk": 18, "def":  6, "xp":  80, "gmin":  40, "gmax":  80},
    {"name": "Troll 🧌",    "hp": 140, "atk": 25, "def":  8, "xp": 130, "gmin":  60, "gmax": 120},
    {"name": "Dragon 🐉",   "hp": 250, "atk": 40, "def": 15, "xp": 300, "gmin": 150, "gmax": 300},
]

SHOP_ITEMS = {
    "iron_sword":   {"name": "Iron Sword",   "type": "weapon", "atk":  5, "def":  0, "cost":  50},
    "steel_sword":  {"name": "Steel Sword",  "type": "weapon", "atk": 12, "def":  0, "cost": 150},
    "magic_staff":  {"name": "Magic Staff",  "type": "weapon", "atk": 18, "def":  0, "cost": 200},
    "elven_bow":    {"name": "Elven Bow",    "type": "weapon", "atk": 14, "def":  0, "cost": 175},
    "leather_armor":{"name": "Leather Armor","type": "armor",  "atk":  0, "def":  3, "cost":  30},
    "iron_shield":  {"name": "Iron Shield",  "type": "armor",  "atk":  0, "def":  5, "cost":  40},
    "steel_shield": {"name": "Steel Shield", "type": "armor",  "atk":  0, "def": 10, "cost": 120},
    "dragon_plate": {"name": "Dragon Plate", "type": "armor",  "atk":  0, "def": 18, "cost": 350},
}

HUNT_COOLDOWNS: dict[int, float] = {}   # user_id → last hunt timestamp
HUNT_COOLDOWN_SECS = 30

def xp_for_level(level: int) -> int:
    """XP needed to reach the next level."""
    return level * 150

def equipment_bonuses(equip: dict) -> tuple[int, int]:
    atk = def_ = 0
    for slot_item in equip.values():
        item = SHOP_ITEMS.get(slot_item, {})
        atk  += item.get("atk", 0)
        def_ += item.get("def", 0)
    return atk, def_


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════
async def init_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)

    await db.executescript("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id            INTEGER PRIMARY KEY,
            welcome_channel_id  INTEGER,
            welcome_message     TEXT,
            leave_channel_id    INTEGER,
            leave_message       TEXT,
            log_channel_id      INTEGER,
            automod_enabled     INTEGER DEFAULT 0,
            block_invites       INTEGER DEFAULT 0,
            autorole_id         INTEGER
        );

        CREATE TABLE IF NOT EXISTS banned_words (
            guild_id INTEGER,
            word     TEXT,
            PRIMARY KEY (guild_id, word)
        );

        CREATE TABLE IF NOT EXISTS tags (
            guild_id   INTEGER,
            name       TEXT,
            content    TEXT,
            creator_id INTEGER,
            PRIMARY KEY (guild_id, name)
        );

        CREATE TABLE IF NOT EXISTS reaction_roles (
            message_id INTEGER,
            emoji      TEXT,
            role_id    INTEGER,
            guild_id   INTEGER,
            PRIMARY KEY (message_id, emoji)
        );

        CREATE TABLE IF NOT EXISTS warnings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            reason     TEXT    NOT NULL,
            created_at TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS antinuke_config (
            guild_id                  INTEGER PRIMARY KEY,
            enabled                   INTEGER DEFAULT 0,
            log_channel_id            INTEGER,
            action                    TEXT    DEFAULT 'kick',
            ban_threshold             INTEGER DEFAULT 3,
            channel_delete_threshold  INTEGER DEFAULT 3,
            role_delete_threshold     INTEGER DEFAULT 3
        );

        CREATE TABLE IF NOT EXISTS antinuke_whitelist (
            guild_id INTEGER,
            user_id  INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS antiraid_config (
            guild_id            INTEGER PRIMARY KEY,
            enabled             INTEGER DEFAULT 0,
            log_channel_id      INTEGER,
            join_threshold      INTEGER DEFAULT 10,
            join_window         INTEGER DEFAULT 10,
            action              TEXT    DEFAULT 'kick',
            min_account_age_days INTEGER DEFAULT 7
        );

        CREATE TABLE IF NOT EXISTS rpg_characters (
            user_id   INTEGER,
            guild_id  INTEGER,
            class     TEXT,
            level     INTEGER DEFAULT 1,
            xp        INTEGER DEFAULT 0,
            hp        INTEGER,
            max_hp    INTEGER,
            attack    INTEGER,
            defense   INTEGER,
            gold      INTEGER DEFAULT 100,
            equipment TEXT    DEFAULT '{}',
            PRIMARY KEY (user_id, guild_id)
        );

        CREATE TABLE IF NOT EXISTS whitelist (
            guild_id    INTEGER,
            user_id     INTEGER,
            expires_at  TEXT    NOT NULL,
            granted_by  INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS afk (
            guild_id    INTEGER,
            user_id     INTEGER,
            reason      TEXT    NOT NULL DEFAULT 'AFK',
            old_nick    TEXT,
            set_at      TEXT    DEFAULT (datetime('now')),
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS giveaways (
            message_id  INTEGER PRIMARY KEY,
            channel_id  INTEGER NOT NULL,
            guild_id    INTEGER NOT NULL,
            host_id     INTEGER NOT NULL,
            prize       TEXT    NOT NULL,
            description TEXT    DEFAULT '',
            winners     INTEGER DEFAULT 1,
            ends_at     TEXT    NOT NULL,
            ended       INTEGER DEFAULT 0,
            entries     TEXT    DEFAULT '[]'
        );
    """)
    await db.commit()
    return db


# ── Migration: add columns added after initial schema ─────────────────────────
async def run_migrations(db: aiosqlite.Connection):
    migrations = [
        "ALTER TABLE guild_config ADD COLUMN block_invites INTEGER DEFAULT 0",
        "ALTER TABLE guild_config ADD COLUMN autorole_id INTEGER",
        "ALTER TABLE tags         ADD COLUMN creator_id INTEGER",
    ]
    for sql in migrations:
        try:
            await db.execute(sql)
        except Exception:
            pass
    await db.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────
async def ensure_guild_config(guild_id: int):
    await bot.db.execute(
        "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,)
    )
    await bot.db.commit()


async def get_guild_config(guild_id: int):
    await ensure_guild_config(guild_id)
    async with bot.db.execute(
        "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
    ) as cur:
        return await cur.fetchone()


def has_perm(interaction: discord.Interaction, perm: str) -> bool:
    return getattr(interaction.user.guild_permissions, perm, False)


def is_mod(interaction: discord.Interaction) -> bool:
    p = interaction.user.guild_permissions
    return p.manage_messages or p.manage_guild or p.administrator


async def is_whitelisted(guild_id: int, user_id: int) -> bool:
    async with bot.db.execute(
        "SELECT 1 FROM whitelist WHERE guild_id=? AND user_id=? AND expires_at > datetime('now')",
        (guild_id, user_id),
    ) as cur:
        return await cur.fetchone() is not None


# ── RPG helpers ───────────────────────────────────────────────────────────────
async def get_character(user_id: int, guild_id: int):
    async with bot.db.execute(
        "SELECT * FROM rpg_characters WHERE user_id=? AND guild_id=?",
        (user_id, guild_id),
    ) as cur:
        return await cur.fetchone()


async def save_character(user_id: int, guild_id: int, data: dict):
    await bot.db.execute(
        """UPDATE rpg_characters
           SET level=?, xp=?, hp=?, max_hp=?, attack=?, defense=?, gold=?, equipment=?
           WHERE user_id=? AND guild_id=?""",
        (
            data["level"], data["xp"], data["hp"], data["max_hp"],
            data["attack"], data["defense"], data["gold"],
            json.dumps(data["equipment"]),
            user_id, guild_id,
        ),
    )
    await bot.db.commit()


def row_to_char(row) -> dict:
    return {
        "user_id":  row[0], "guild_id": row[1], "class":   row[2],
        "level":    row[3], "xp":       row[4], "hp":      row[5],
        "max_hp":   row[6], "attack":   row[7], "defense": row[8],
        "gold":     row[9], "equipment": json.loads(row[10] or "{}"),
    }


async def check_level_up(user_id: int, guild_id: int, char: dict) -> list[str]:
    msgs = []
    while char["xp"] >= xp_for_level(char["level"]):
        char["xp"]     -= xp_for_level(char["level"])
        char["level"]  += 1
        char["max_hp"] += 10
        char["hp"]      = char["max_hp"]
        char["attack"]  += 2
        char["defense"] += 1
        msgs.append(f"🎉 Level up! Now **Level {char['level']}**!")
    return msgs


# ═══════════════════════════════════════════════════════════════════════════════
# LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════
@bot.event
async def setup_hook():
    bot.db = await init_db()
    await run_migrations(bot.db)
    cleanup_whitelist.start()
    # Register persistent views so buttons survive bot restarts
    bot.add_view(GiveawayView())
    bot.add_view(GiveawayEndedView())
    giveaway_ticker.start()


BOT_NAME        = "HEAVENLY"
BOT_AVATAR_PATH = "avatar.jpg"


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Sync error: {e}")

    try:
        if bot.user.name != BOT_NAME:
            await bot.user.edit(username=BOT_NAME)
        if os.path.exists(BOT_AVATAR_PATH):
            with open(BOT_AVATAR_PATH, "rb") as f:
                await bot.user.edit(avatar=f.read())
    except discord.HTTPException as e:
        print(f"Profile update skipped (rate-limited?): {e}")

    print(f"Online as {bot.user} ({bot.user.id})")


@tasks.loop(minutes=5)
async def cleanup_whitelist():
    """Purge expired whitelist entries every 5 minutes."""
    await bot.db.execute("DELETE FROM whitelist WHERE expires_at <= datetime('now')")
    await bot.db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# HELP
# ═══════════════════════════════════════════════════════════════════════════════
@bot.tree.command(name="help", description="List all available commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Command List", color=discord.Color.blurple())
    embed.add_field(name="🛡️ Moderation",
        value="`/kick` `/ban` `/unban` `/mute` `/unmute`\n`/warn` `/warnings` `/warnings-clear` `/clear`",
        inline=False)
    embed.add_field(name="🔐 Whitelist (temp mod access)",
        value="`/whitelist add` `/whitelist remove` `/whitelist list`",
        inline=False)
    embed.add_field(name="🛡 Anti-Nuke",
        value="`/antinuke setup` `/antinuke toggle` `/antinuke action`\n`/antinuke thresholds` `/antinuke whitelist-add` `/antinuke whitelist-remove`",
        inline=False)
    embed.add_field(name="🚨 Anti-Raid",
        value="`/antiraid setup` `/antiraid toggle` `/antiraid action` `/antiraid thresholds`",
        inline=False)
    embed.add_field(name="🤖 Auto-Mod",
        value="`/automod toggle` `/automod block-invites`\n`/automod addword` `/automod removeword` `/automod listwords`",
        inline=False)
    embed.add_field(name="⚔️ RPG",
        value="`/rpg start` `/rpg profile` `/rpg hunt`\n`/rpg shop` `/rpg buy` `/rpg inventory` `/rpg duel`",
        inline=False)
    embed.add_field(name="🏷️ Tags",
        value="`/tag create` `/tag delete` `/tag get` `/tag list`  · prefix: `!tagname`",
        inline=False)
    embed.add_field(name="🎭 Reaction Roles",
        value="`/reactionrole add` `/reactionrole remove`",
        inline=False)
    embed.add_field(name="👋 Welcome / Leave",
        value="`/welcome set` `/leave set`",
        inline=False)
    embed.add_field(name="📋 Logging",
        value="`/setlogchannel`",
        inline=False)
    embed.set_footer(text="Tag/whitelist management requires Manage Messages or higher.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# WELCOME / LEAVE
# ═══════════════════════════════════════════════════════════════════════════════
@bot.event
async def on_member_join(member: discord.Member):
    cfg = await get_guild_config(member.guild.id)
    if cfg[1]:
        ch = member.guild.get_channel(cfg[1])
        if ch:
            text = (cfg[2] or "Welcome {member} to {server}!").format(
                member=member.mention, server=member.guild.name)
            await ch.send(text)
    # Autorole
    if cfg[8]:
        role = member.guild.get_role(cfg[8])
        if role:
            try:
                await member.add_roles(role, reason="Autorole")
            except discord.Forbidden:
                pass
    # Anti-raid join tracking handled separately in on_member_join (see below)
    await antiraid_check(member)


@bot.event
async def on_member_remove(member: discord.Member):
    cfg = await get_guild_config(member.guild.id)
    if cfg[3]:
        ch = member.guild.get_channel(cfg[3])
        if ch:
            text = (cfg[4] or "{member} has left {server}.").format(
                member=str(member), server=member.guild.name)
            await ch.send(text)


welcome_group = app_commands.Group(name="welcome", description="Configure welcome messages")
leave_group   = app_commands.Group(name="leave",   description="Configure leave messages")


@welcome_group.command(name="set", description="Set the welcome channel and message")
@app_commands.describe(channel="Channel", message="Use {member} and {server}")
async def welcome_set(interaction: discord.Interaction, channel: discord.TextChannel,
                      message: str = "Welcome {member} to {server}!"):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET welcome_channel_id=?, welcome_message=? WHERE guild_id=?",
        (channel.id, message, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Welcome → {channel.mention}")


@leave_group.command(name="set", description="Set the leave channel and message")
@app_commands.describe(channel="Channel", message="Use {member} and {server}")
async def leave_set(interaction: discord.Interaction, channel: discord.TextChannel,
                    message: str = "{member} has left {server}."):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET leave_channel_id=?, leave_message=? WHERE guild_id=?",
        (channel.id, message, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Leave → {channel.mention}")


bot.tree.add_command(welcome_group)
bot.tree.add_command(leave_group)


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
@bot.tree.command(name="setlogchannel", description="Set channel for message edit/delete logs")
@app_commands.describe(channel="Log channel")
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET log_channel_id=? WHERE guild_id=?",
        (channel.id, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Logs → {channel.mention}")


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    cfg = await get_guild_config(message.guild.id)
    if cfg[5]:
        ch = message.guild.get_channel(cfg[5])
        if ch:
            e = discord.Embed(title="🗑️ Message Deleted", color=discord.Color.red())
            e.add_field(name="Author",  value=str(message.author),       inline=True)
            e.add_field(name="Channel", value=message.channel.mention,   inline=True)
            e.add_field(name="Content", value=message.content or "*[no text]*", inline=False)
            await ch.send(embed=e)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild or before.content == after.content:
        return
    cfg = await get_guild_config(before.guild.id)
    if cfg[5]:
        ch = before.guild.get_channel(cfg[5])
        if ch:
            e = discord.Embed(title="✏️ Message Edited", color=discord.Color.orange())
            e.add_field(name="Author",  value=str(before.author),      inline=True)
            e.add_field(name="Channel", value=before.channel.mention,  inline=True)
            e.add_field(name="Before",  value=before.content or "*[empty]*", inline=False)
            e.add_field(name="After",   value=after.content  or "*[empty]*", inline=False)
            await ch.send(embed=e)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-MODERATION
# ═══════════════════════════════════════════════════════════════════════════════
automod_group = app_commands.Group(name="automod", description="Configure auto-moderation")

MASS_MENTION_THRESHOLD = 5


@automod_group.command(name="toggle", description="Enable or disable auto-moderation")
async def automod_toggle(interaction: discord.Interaction, enabled: bool):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET automod_enabled=? WHERE guild_id=?",
        (int(enabled), interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Auto-mod {'**ON**' if enabled else '**OFF**'}.")


@automod_group.command(name="block-invites", description="Block Discord invite links")
async def automod_block_invites(interaction: discord.Interaction, enabled: bool):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET block_invites=? WHERE guild_id=?",
        (int(enabled), interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Invite blocking {'**ON**' if enabled else '**OFF**'}.")


@automod_group.command(name="addword", description="Add a banned word")
async def automod_addword(interaction: discord.Interaction, word: str):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT OR IGNORE INTO banned_words (guild_id, word) VALUES (?, ?)",
        (interaction.guild.id, word.lower()))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Banned `{word}`.", ephemeral=True)


@automod_group.command(name="removeword", description="Remove a banned word")
async def automod_removeword(interaction: discord.Interaction, word: str):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM banned_words WHERE guild_id=? AND word=?",
        (interaction.guild.id, word.lower()))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Unbanned `{word}`.", ephemeral=True)


@automod_group.command(name="listwords", description="List all banned words")
async def automod_listwords(interaction: discord.Interaction):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    async with bot.db.execute(
        "SELECT word FROM banned_words WHERE guild_id=? ORDER BY word",
        (interaction.guild.id,)
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message("No banned words yet.", ephemeral=True)
    await interaction.response.send_message(
        "🚫 **Banned words:** " + ", ".join(f"`{r[0]}`" for r in rows), ephemeral=True)


bot.tree.add_command(automod_group)


async def run_automod(message: discord.Message):
    cfg = await get_guild_config(message.guild.id)
    automod_on    = cfg[6]
    block_invites = cfg[7] if len(cfg) > 7 else 0

    if not automod_on and not block_invites:
        return

    async def delete_warn(text: str):
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention}, {text}", delete_after=6)
        except discord.Forbidden:
            pass

    if automod_on and len(message.mentions) >= MASS_MENTION_THRESHOLD:
        return await delete_warn("please don't mass-mention members.")

    if block_invites and INVITE_RE.search(message.content):
        return await delete_warn("posting invite links is not allowed here.")

    if automod_on:
        async with bot.db.execute(
            "SELECT word FROM banned_words WHERE guild_id=?", (message.guild.id,)
        ) as cur:
            rows = await cur.fetchall()
        content_lower = message.content.lower()
        for (word,) in rows:
            if re.search(rf"\b{re.escape(word)}\b", content_lower):
                return await delete_warn("that message contained a banned word.")


# ═══════════════════════════════════════════════════════════════════════════════
# ANTI-NUKE
# ═══════════════════════════════════════════════════════════════════════════════
antinuke_group = app_commands.Group(name="antinuke", description="Anti-nuke protection")


@antinuke_group.command(name="setup", description="Set the anti-nuke log channel")
@app_commands.describe(channel="Where to send nuke alerts")
async def antinuke_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antinuke_config (guild_id, log_channel_id) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
        (interaction.guild.id, channel.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Anti-nuke alerts → {channel.mention}")


@antinuke_group.command(name="toggle", description="Enable or disable anti-nuke")
async def antinuke_toggle(interaction: discord.Interaction, enabled: bool):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antinuke_config (guild_id, enabled) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled",
        (interaction.guild.id, int(enabled)))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Anti-nuke {'**ON**' if enabled else '**OFF**'}.")


@antinuke_group.command(name="action",
    description="Action when nuke detected: kick or ban")
@app_commands.choices(action=[
    app_commands.Choice(name="Kick the nuker", value="kick"),
    app_commands.Choice(name="Ban the nuker",  value="ban"),
])
async def antinuke_action(interaction: discord.Interaction,
                          action: app_commands.Choice[str]):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antinuke_config (guild_id, action) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET action=excluded.action",
        (interaction.guild.id, action.value))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Nuke action set to **{action.value}**.")


@antinuke_group.command(name="thresholds",
    description="Set how many actions in 10s trigger anti-nuke")
@app_commands.describe(bans="Ban threshold", channel_deletes="Channel-delete threshold",
                       role_deletes="Role-delete threshold")
async def antinuke_thresholds(interaction: discord.Interaction,
                              bans: int = 3, channel_deletes: int = 3,
                              role_deletes: int = 3):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        """INSERT INTO antinuke_config
               (guild_id, ban_threshold, channel_delete_threshold, role_delete_threshold)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(guild_id) DO UPDATE SET
               ban_threshold=excluded.ban_threshold,
               channel_delete_threshold=excluded.channel_delete_threshold,
               role_delete_threshold=excluded.role_delete_threshold""",
        (interaction.guild.id, bans, channel_deletes, role_deletes))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Thresholds — bans: {bans} · channel deletes: {channel_deletes} · role deletes: {role_deletes}")


@antinuke_group.command(name="whitelist-add",
    description="Exempt a user/bot from anti-nuke checks")
@app_commands.describe(user="User to whitelist")
async def antinuke_wl_add(interaction: discord.Interaction, user: discord.User):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT OR IGNORE INTO antinuke_whitelist (guild_id, user_id) VALUES (?, ?)",
        (interaction.guild.id, user.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ {user.mention} whitelisted from anti-nuke.")


@antinuke_group.command(name="whitelist-remove",
    description="Remove a user from the anti-nuke whitelist")
@app_commands.describe(user="User to remove")
async def antinuke_wl_remove(interaction: discord.Interaction, user: discord.User):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM antinuke_whitelist WHERE guild_id=? AND user_id=?",
        (interaction.guild.id, user.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ {user.mention} removed from anti-nuke whitelist.")


bot.tree.add_command(antinuke_group)


async def _get_antinuke_cfg(guild_id: int):
    async with bot.db.execute(
        "SELECT * FROM antinuke_config WHERE guild_id=?", (guild_id,)
    ) as cur:
        return await cur.fetchone()


async def _is_antinuke_whitelisted(guild_id: int, user_id: int) -> bool:
    async with bot.db.execute(
        "SELECT 1 FROM antinuke_whitelist WHERE guild_id=? AND user_id=?",
        (guild_id, user_id)
    ) as cur:
        return await cur.fetchone() is not None


def _track_nuke_action(guild_id: int, user_id: int, action: str, threshold: int) -> bool:
    """Add an event timestamp and return True if the threshold is exceeded."""
    now = time.time()
    ts_list = nuke_tracker[guild_id][user_id][action]
    ts_list.append(now)
    # Keep only events within the window
    nuke_tracker[guild_id][user_id][action] = [t for t in ts_list if now - t <= NUKE_WINDOW]
    return len(nuke_tracker[guild_id][user_id][action]) >= threshold


async def _antinuke_punish(guild: discord.Guild, offender: discord.Member,
                           reason: str, cfg):
    """Strip roles, then kick or ban the offender, and log the event."""
    action      = cfg[3] if cfg else "kick"
    log_chan_id = cfg[2] if cfg else None

    # Strip roles first (neutralises the threat immediately)
    try:
        removable = [r for r in offender.roles if r != guild.default_role and r.is_assignable()]
        if removable:
            await offender.remove_roles(*removable, reason="Anti-Nuke: threat neutralised")
    except discord.Forbidden:
        pass

    # Kick or ban
    try:
        if action == "ban":
            await guild.ban(offender, reason=f"Anti-Nuke: {reason}", delete_message_days=0)
        else:
            await guild.kick(offender, reason=f"Anti-Nuke: {reason}")
    except discord.Forbidden:
        pass

    # Log
    if log_chan_id:
        ch = guild.get_channel(log_chan_id)
        if ch:
            e = discord.Embed(
                title="🚨 Anti-Nuke Triggered",
                description=f"**Offender:** {offender.mention} (`{offender}`)\n"
                            f"**Reason:** {reason}\n"
                            f"**Action:** {action.upper()}",
                color=discord.Color.red(),
            )
            e.set_footer(text=f"User ID: {offender.id}")
            await ch.send(embed=e)


async def _resolve_audit_actor(guild: discord.Guild,
                               action: discord.AuditLogAction) -> discord.Member | None:
    """Fetch the most recent audit log entry for the given action and return the responsible member."""
    try:
        async for entry in guild.audit_logs(limit=1, action=action):
            if entry.user and not entry.user.bot:
                return guild.get_member(entry.user.id)
            return None
    except discord.Forbidden:
        return None


# Anti-nuke events
@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    cfg = await _get_antinuke_cfg(guild.id)
    if not cfg or not cfg[1]:
        return
    threshold = cfg[4]
    actor = await _resolve_audit_actor(guild, discord.AuditLogAction.ban)
    if not actor or actor.id == bot.user.id:
        return
    if await _is_antinuke_whitelisted(guild.id, actor.id):
        return
    if actor.id == guild.owner_id:
        return
    if _track_nuke_action(guild.id, actor.id, "ban", threshold):
        await _antinuke_punish(guild, actor, f"Mass ban detected ({threshold}+ bans in 10s)", cfg)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild = channel.guild
    cfg = await _get_antinuke_cfg(guild.id)
    if not cfg or not cfg[1]:
        return
    threshold = cfg[5]
    actor = await _resolve_audit_actor(guild, discord.AuditLogAction.channel_delete)
    if not actor or actor.id == bot.user.id:
        return
    if await _is_antinuke_whitelisted(guild.id, actor.id):
        return
    if actor.id == guild.owner_id:
        return
    if _track_nuke_action(guild.id, actor.id, "channel_delete", threshold):
        await _antinuke_punish(
            guild, actor, f"Mass channel deletion detected ({threshold}+ in 10s)", cfg)


@bot.event
async def on_guild_role_delete(role: discord.Role):
    guild = role.guild
    cfg = await _get_antinuke_cfg(guild.id)
    if not cfg or not cfg[1]:
        return
    threshold = cfg[6]
    actor = await _resolve_audit_actor(guild, discord.AuditLogAction.role_delete)
    if not actor or actor.id == bot.user.id:
        return
    if await _is_antinuke_whitelisted(guild.id, actor.id):
        return
    if actor.id == guild.owner_id:
        return
    if _track_nuke_action(guild.id, actor.id, "role_delete", threshold):
        await _antinuke_punish(
            guild, actor, f"Mass role deletion detected ({threshold}+ in 10s)", cfg)


# ═══════════════════════════════════════════════════════════════════════════════
# ANTI-RAID
# ═══════════════════════════════════════════════════════════════════════════════
antiraid_group = app_commands.Group(name="antiraid", description="Anti-raid protection")


@antiraid_group.command(name="setup", description="Set the anti-raid log channel")
@app_commands.describe(channel="Where to send raid alerts")
async def antiraid_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antiraid_config (guild_id, log_channel_id) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
        (interaction.guild.id, channel.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Anti-raid alerts → {channel.mention}")


@antiraid_group.command(name="toggle", description="Enable or disable anti-raid")
async def antiraid_toggle(interaction: discord.Interaction, enabled: bool):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antiraid_config (guild_id, enabled) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled",
        (interaction.guild.id, int(enabled)))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Anti-raid {'**ON**' if enabled else '**OFF**'}.")


@antiraid_group.command(name="action",
    description="Action taken on raiders: kick, ban, or lockdown")
@app_commands.choices(action=[
    app_commands.Choice(name="Kick raiders",     value="kick"),
    app_commands.Choice(name="Ban raiders",      value="ban"),
    app_commands.Choice(name="Lockdown server",  value="lockdown"),
])
async def antiraid_action(interaction: discord.Interaction,
                          action: app_commands.Choice[str]):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antiraid_config (guild_id, action) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET action=excluded.action",
        (interaction.guild.id, action.value))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Raid action set to **{action.value}**.")


@antiraid_group.command(name="thresholds",
    description="Configure join flood and account age settings")
@app_commands.describe(
    joins="Joins in the time window to trigger (default 10)",
    window="Time window in seconds (default 10)",
    min_age_days="Minimum account age in days (default 7)")
async def antiraid_thresholds(interaction: discord.Interaction,
                              joins: int = 10, window: int = 10,
                              min_age_days: int = 7):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        """INSERT INTO antiraid_config (guild_id, join_threshold, join_window, min_account_age_days)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(guild_id) DO UPDATE SET
               join_threshold=excluded.join_threshold,
               join_window=excluded.join_window,
               min_account_age_days=excluded.min_account_age_days""",
        (interaction.guild.id, joins, window, min_age_days))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Raid thresholds — {joins} joins / {window}s · min account age {min_age_days}d")


bot.tree.add_command(antiraid_group)


async def antiraid_check(member: discord.Member):
    """Called on every member join to check for a raid."""
    guild = member.guild
    async with bot.db.execute(
        "SELECT * FROM antiraid_config WHERE guild_id=?", (guild.id,)
    ) as cur:
        cfg = await cur.fetchone()
    if not cfg or not cfg[1]:
        return

    join_threshold  = cfg[3]
    join_window     = cfg[4]
    action          = cfg[5]
    min_age_days    = cfg[6]
    log_chan_id     = cfg[2]

    now = time.time()

    # --- New-account check (independent of flood) ---
    account_age_days = (datetime.now(timezone.utc) - member.created_at).days
    if account_age_days < min_age_days:
        await _raid_act(guild, member, action, log_chan_id,
                        f"New account (age {account_age_days}d < {min_age_days}d minimum)")
        return

    # --- Join flood check ---
    ts_list = raid_tracker[guild.id]
    ts_list.append(now)
    raid_tracker[guild.id] = [t for t in ts_list if now - t <= join_window]

    if len(raid_tracker[guild.id]) >= join_threshold:
        raid_tracker[guild.id].clear()  # reset to avoid acting on every subsequent join
        await _raid_act(guild, member, action, log_chan_id,
                        f"Join flood ({join_threshold}+ joins in {join_window}s)")


async def _raid_act(guild: discord.Guild, member: discord.Member,
                    action: str, log_chan_id: int | None, reason: str):
    try:
        if action == "ban":
            await guild.ban(member, reason=f"Anti-Raid: {reason}", delete_message_days=0)
        elif action == "lockdown":
            # Remove Send Messages from @everyone in all text channels
            overwrite = discord.PermissionOverwrite(send_messages=False)
            for ch in guild.text_channels:
                try:
                    await ch.set_permissions(guild.default_role, overwrite=overwrite,
                                             reason="Anti-Raid lockdown")
                except discord.Forbidden:
                    pass
            await guild.kick(member, reason=f"Anti-Raid: {reason}")
        else:
            await guild.kick(member, reason=f"Anti-Raid: {reason}")
    except discord.Forbidden:
        pass

    if log_chan_id:
        ch = guild.get_channel(log_chan_id)
        if ch:
            e = discord.Embed(
                title="🚨 Anti-Raid Triggered",
                description=f"**Member:** {member.mention} (`{member}`)\n"
                            f"**Reason:** {reason}\n"
                            f"**Action:** {action.upper()}",
                color=discord.Color.dark_red(),
            )
            e.set_footer(text=f"Account created: {member.created_at.strftime('%Y-%m-%d')}")
            await ch.send(embed=e)


# ═══════════════════════════════════════════════════════════════════════════════
# CUSTOM TAGS
# ═══════════════════════════════════════════════════════════════════════════════
tag_group = app_commands.Group(name="tag", description="Custom tags / canned responses")


@tag_group.command(name="create", description="Create a custom tag (Manage Messages required)")
@app_commands.describe(name="Tag name", content="Reply content")
async def tag_create(interaction: discord.Interaction, name: str, content: str):
    if not is_mod(interaction):
        return await interaction.response.send_message(
            "Need Manage Messages permission.", ephemeral=True)
    try:
        await bot.db.execute(
            "INSERT INTO tags (guild_id, name, content, creator_id) VALUES (?, ?, ?, ?)",
            (interaction.guild.id, name.lower(), content, interaction.user.id))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ Tag `{name}` created.")
    except aiosqlite.IntegrityError:
        await interaction.response.send_message(f"Tag `{name}` already exists.", ephemeral=True)


@tag_group.command(name="delete", description="Delete a custom tag (Manage Messages required)")
@app_commands.describe(name="Tag name")
async def tag_delete(interaction: discord.Interaction, name: str):
    if not is_mod(interaction):
        return await interaction.response.send_message(
            "Need Manage Messages permission.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM tags WHERE guild_id=? AND name=?", (interaction.guild.id, name.lower()))
    await bot.db.commit()
    await interaction.response.send_message(f"🗑️ Tag `{name}` deleted.")


@tag_group.command(name="get", description="Show a tag's content")
@app_commands.describe(name="Tag name")
async def tag_get(interaction: discord.Interaction, name: str):
    async with bot.db.execute(
        "SELECT content FROM tags WHERE guild_id=? AND name=?",
        (interaction.guild.id, name.lower())
    ) as cur:
        row = await cur.fetchone()
    if row:
        await interaction.response.send_message(row[0])
    else:
        await interaction.response.send_message(f"No tag `{name}` found.", ephemeral=True)


@tag_group.command(name="list", description="List all tags in this server")
async def tag_list(interaction: discord.Interaction):
    async with bot.db.execute(
        "SELECT name FROM tags WHERE guild_id=? ORDER BY name", (interaction.guild.id,)
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message("No tags yet.")
    await interaction.response.send_message(
        "**Tags:** " + ", ".join(f"`{r[0]}`" for r in rows))


bot.tree.add_command(tag_group)


# ═══════════════════════════════════════════════════════════════════════════════
# ON MESSAGE  (automod + tag prefix)
# ═══════════════════════════════════════════════════════════════════════════════
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    await run_automod(message)

    # ── AFK: remove AFK status when the AFK user speaks ──────────────────────
    async with bot.db.execute(
        "SELECT reason, old_nick FROM afk WHERE guild_id=? AND user_id=?",
        (message.guild.id, message.author.id)
    ) as cur:
        afk_self = await cur.fetchone()

    if afk_self:
        await bot.db.execute(
            "DELETE FROM afk WHERE guild_id=? AND user_id=?",
            (message.guild.id, message.author.id))
        await bot.db.commit()
        # Restore original nickname
        try:
            await message.author.edit(nick=afk_self[1], reason="AFK removed")
        except discord.Forbidden:
            pass
        await message.channel.send(
            f"👋 Welcome back, {message.author.mention}! Your AFK has been removed.",
            delete_after=8)

    # ── AFK: notify when someone mentions an AFK user ────────────────────────
    if message.mentions:
        alerts = []
        for mentioned in message.mentions:
            if mentioned.id == message.author.id:
                continue
            async with bot.db.execute(
                "SELECT reason, set_at FROM afk WHERE guild_id=? AND user_id=?",
                (message.guild.id, mentioned.id)
            ) as cur:
                afk_row = await cur.fetchone()
            if afk_row:
                alerts.append(
                    f"💤 **{mentioned.display_name}** is AFK: *{afk_row[0]}* (since {afk_row[1]} UTC)")
        if alerts:
            await message.channel.send("\n".join(alerts), delete_after=10)

    # ── Tag prefix trigger ────────────────────────────────────────────────────
    if message.content.startswith("!") and len(message.content) > 1:
        name = message.content[1:].split()[0].lower()
        async with bot.db.execute(
            "SELECT content FROM tags WHERE guild_id=? AND name=?",
            (message.guild.id, name)
        ) as cur:
            row = await cur.fetchone()
        if row:
            await message.channel.send(row[0])

    await bot.process_commands(message)


# ═══════════════════════════════════════════════════════════════════════════════
# REACTION ROLES
# ═══════════════════════════════════════════════════════════════════════════════
rr_group = app_commands.Group(name="reactionrole", description="Manage reaction roles")


@rr_group.command(name="add", description="Link a reaction emoji on a message to a role")
@app_commands.describe(channel="Channel with the message", message_id="Message ID",
                       emoji="Emoji to use", role="Role to assign")
async def rr_add(interaction: discord.Interaction, channel: discord.TextChannel,
                 message_id: str, emoji: str, role: discord.Role):
    if not has_perm(interaction, "manage_roles"):
        return await interaction.response.send_message("Need Manage Roles.", ephemeral=True)
    try:
        msg_id = int(message_id)
    except ValueError:
        return await interaction.response.send_message("Message ID must be numeric.", ephemeral=True)
    try:
        target = await channel.fetch_message(msg_id)
    except (discord.NotFound, discord.Forbidden):
        return await interaction.response.send_message(
            f"Couldn't find that message in {channel.mention}.", ephemeral=True)
    try:
        await target.add_reaction(emoji)
    except discord.HTTPException:
        return await interaction.response.send_message(
            "Invalid emoji.", ephemeral=True)
    await bot.db.execute(
        "INSERT OR REPLACE INTO reaction_roles (message_id, emoji, role_id, guild_id) VALUES (?,?,?,?)",
        (msg_id, emoji, role.id, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Reacting {emoji} → {role.mention}")


@rr_group.command(name="remove", description="Remove a reaction role link")
@app_commands.describe(message_id="Message ID", emoji="Emoji to unlink")
async def rr_remove(interaction: discord.Interaction, message_id: str, emoji: str):
    if not has_perm(interaction, "manage_roles"):
        return await interaction.response.send_message("Need Manage Roles.", ephemeral=True)
    try:
        msg_id = int(message_id)
    except ValueError:
        return await interaction.response.send_message("Message ID must be numeric.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM reaction_roles WHERE message_id=? AND emoji=?", (msg_id, emoji))
    await bot.db.commit()
    await interaction.response.send_message(f"🗑️ Removed reaction role for {emoji}.")


bot.tree.add_command(rr_group)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.member is None or payload.member.bot:
        return
    async with bot.db.execute(
        "SELECT role_id FROM reaction_roles WHERE message_id=? AND emoji=?",
        (payload.message_id, str(payload.emoji))
    ) as cur:
        row = await cur.fetchone()
    if row:
        guild = bot.get_guild(payload.guild_id)
        role  = guild.get_role(row[0])
        if role:
            try:
                await payload.member.add_roles(role, reason="Reaction role")
            except discord.Forbidden:
                pass


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    guild  = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    member = guild.get_member(payload.user_id)
    if member is None or member.bot:
        return
    async with bot.db.execute(
        "SELECT role_id FROM reaction_roles WHERE message_id=? AND emoji=?",
        (payload.message_id, str(payload.emoji))
    ) as cur:
        row = await cur.fetchone()
    if row:
        role = guild.get_role(row[0])
        if role:
            try:
                await member.remove_roles(role, reason="Reaction role removed")
            except discord.Forbidden:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# WHITELIST  (temporary /warn + /mute access)
# ═══════════════════════════════════════════════════════════════════════════════
whitelist_group = app_commands.Group(name="whitelist",
    description="Grant users temporary /warn + /mute access")


@whitelist_group.command(name="add",
    description="Give a user temporary mod powers (warn + mute)")
@app_commands.describe(user="User to grant powers to",
                       minutes="How long (in minutes) the access lasts")
async def wl_add(interaction: discord.Interaction, user: discord.Member, minutes: int):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    if minutes < 1:
        return await interaction.response.send_message(
            "Duration must be at least 1 minute.", ephemeral=True)

    expires_sql = f"datetime('now', '+{minutes} minutes')"
    await bot.db.execute(
        f"INSERT INTO whitelist (guild_id, user_id, expires_at, granted_by)"
        f" VALUES (?, ?, {expires_sql}, ?)"
        f" ON CONFLICT(guild_id, user_id) DO UPDATE SET expires_at={expires_sql}, granted_by=excluded.granted_by",
        (interaction.guild.id, user.id, interaction.user.id))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ {user.mention} can now use `/warn` and `/mute` for **{minutes} minute(s)**.")


@whitelist_group.command(name="remove",
    description="Revoke a user's temporary mod access early")
@app_commands.describe(user="User to remove from whitelist")
async def wl_remove(interaction: discord.Interaction, user: discord.Member):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM whitelist WHERE guild_id=? AND user_id=?",
        (interaction.guild.id, user.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Removed {user.mention}'s temporary access.")


@whitelist_group.command(name="list", description="Show all active temporary mod grants")
async def wl_list(interaction: discord.Interaction):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    async with bot.db.execute(
        """SELECT user_id, expires_at, granted_by
           FROM whitelist
           WHERE guild_id=? AND expires_at > datetime('now')
           ORDER BY expires_at""",
        (interaction.guild.id,)
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message("No active whitelist entries.")
    lines = []
    for user_id, expires_at, granted_by in rows:
        member = interaction.guild.get_member(user_id)
        name   = member.mention if member else f"`{user_id}`"
        lines.append(f"{name} — expires `{expires_at}` (granted by <@{granted_by}>)")
    embed = discord.Embed(title="🔐 Active Whitelist", description="\n".join(lines),
                          color=discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(whitelist_group)


# ═══════════════════════════════════════════════════════════════════════════════
# MODERATION  (warn / mute check whitelist; all others require real permission)
# ═══════════════════════════════════════════════════════════════════════════════
@bot.tree.command(name="kick", description="Kick a member")
@app_commands.describe(member="Member to kick", reason="Reason")
async def kick(interaction: discord.Interaction, member: discord.Member,
               reason: str = "No reason provided"):
    if not has_perm(interaction, "kick_members"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if member.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "Can't kick someone with equal or higher role.", ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 **{member}** kicked. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@bot.tree.command(name="ban", description="Ban a member")
@app_commands.describe(member="Member to ban", reason="Reason")
async def ban(interaction: discord.Interaction, member: discord.Member,
              reason: str = "No reason provided"):
    if not has_perm(interaction, "ban_members"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if member.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "Can't ban someone with equal or higher role.", ephemeral=True)
    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(f"🔨 **{member}** banned. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@bot.tree.command(name="unban", description="Unban a user by ID")
@app_commands.describe(user_id="User ID to unban")
async def unban(interaction: discord.Interaction, user_id: str):
    if not has_perm(interaction, "ban_members"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        await interaction.response.send_message(f"✅ **{user}** unbanned.")
    except ValueError:
        await interaction.response.send_message("Invalid user ID.", ephemeral=True)
    except discord.NotFound:
        await interaction.response.send_message("User isn't banned.", ephemeral=True)


@bot.tree.command(name="mute", description="Timeout a member (mods or whitelisted users)")
@app_commands.describe(member="Member to mute", minutes="Duration in minutes", reason="Reason")
async def mute(interaction: discord.Interaction, member: discord.Member,
               minutes: int, reason: str = "No reason provided"):
    can_act = (has_perm(interaction, "moderate_members") or
               await is_whitelisted(interaction.guild.id, interaction.user.id))
    if not can_act:
        return await interaction.response.send_message(
            "You don't have permission to mute members.", ephemeral=True)
    if member.top_role >= interaction.user.top_role and not has_perm(interaction, "administrator"):
        return await interaction.response.send_message(
            "Can't mute someone with equal or higher role.", ephemeral=True)
    try:
        await member.timeout(timedelta(minutes=minutes), reason=reason)
        await interaction.response.send_message(
            f"🔇 **{member}** muted for {minutes} min. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@bot.tree.command(name="unmute", description="Remove a timeout from a member")
@app_commands.describe(member="Member to unmute")
async def unmute(interaction: discord.Interaction, member: discord.Member):
    if not has_perm(interaction, "moderate_members"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        await member.timeout(None)
        await interaction.response.send_message(f"🔊 **{member}** unmuted.")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@bot.tree.command(name="warn", description="Warn a member (mods or whitelisted users)")
@app_commands.describe(member="Member to warn", reason="Reason")
async def warn(interaction: discord.Interaction, member: discord.Member,
               reason: str = "No reason provided"):
    can_act = (has_perm(interaction, "kick_members") or
               await is_whitelisted(interaction.guild.id, interaction.user.id))
    if not can_act:
        return await interaction.response.send_message(
            "You don't have permission to warn members.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO warnings (guild_id, user_id, reason) VALUES (?, ?, ?)",
        (interaction.guild.id, member.id, reason))
    await bot.db.commit()
    async with bot.db.execute(
        "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?",
        (interaction.guild.id, member.id)
    ) as cur:
        (count,) = await cur.fetchone()
    await interaction.response.send_message(
        f"⚠️ **{member}** warned ({count} total). Reason: {reason}")


@bot.tree.command(name="warnings", description="View a member's warning history")
@app_commands.describe(member="Member to check")
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    async with bot.db.execute(
        "SELECT reason, created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY created_at",
        (interaction.guild.id, member.id)
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message(f"**{member}** has no warnings.")
    lines = "\n".join(
        f"{i+1}. {reason}  *(at {ts})*" for i, (reason, ts) in enumerate(rows))
    embed = discord.Embed(title=f"⚠️ Warnings for {member.display_name}",
                          description=lines, color=discord.Color.yellow())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="warnings-clear", description="Clear all warnings for a member")
@app_commands.describe(member="Member to clear")
async def warnings_clear(interaction: discord.Interaction, member: discord.Member):
    if not has_perm(interaction, "kick_members"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM warnings WHERE guild_id=? AND user_id=?",
        (interaction.guild.id, member.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Cleared all warnings for **{member}**.")


@bot.tree.command(name="clear", description="Delete recent messages in this channel")
@app_commands.describe(amount="Number of messages to delete (max 100)")
async def clear(interaction: discord.Interaction, amount: int):
    if not has_perm(interaction, "manage_messages"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    amount = max(1, min(amount, 100))
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s).", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# RPG SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════
rpg_group = app_commands.Group(name="rpg", description="RPG adventure game")


# ── /rpg start ────────────────────────────────────────────────────────────────
@rpg_group.command(name="start", description="Create your RPG character")
@app_commands.describe(cls="Your character class")
@app_commands.choices(cls=[
    app_commands.Choice(name="⚔️ Warrior — high HP & defense",           value="warrior"),
    app_commands.Choice(name="🔮 Mage — highest attack, glass cannon",    value="mage"),
    app_commands.Choice(name="🏹 Archer — balanced with high crit",       value="archer"),
    app_commands.Choice(name="🗡️ Rogue — high attack & crit chance",     value="rogue"),
])
async def rpg_start(interaction: discord.Interaction, cls: app_commands.Choice[str]):
    if await get_character(interaction.user.id, interaction.guild.id):
        return await interaction.response.send_message(
            "You already have a character! Use `/rpg profile` to view it.", ephemeral=True)
    base = RPG_CLASSES[cls.value]
    await bot.db.execute(
        """INSERT INTO rpg_characters
               (user_id, guild_id, class, level, xp, hp, max_hp, attack, defense, gold, equipment)
           VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?, 100, '{}')""",
        (interaction.user.id, interaction.guild.id, cls.value,
         base["hp"], base["hp"], base["attack"], base["defense"]))
    await bot.db.commit()
    embed = discord.Embed(
        title=f"{base['emoji']} Character Created!",
        description=f"Welcome, **{interaction.user.display_name}** the **{cls.value.title()}**!",
        color=discord.Color.green())
    embed.add_field(name="❤️ HP",      value=str(base["hp"]),     inline=True)
    embed.add_field(name="⚔️ Attack",  value=str(base["attack"]), inline=True)
    embed.add_field(name="🛡️ Defense", value=str(base["defense"]),inline=True)
    embed.add_field(name="✨ Crit",    value=f"{base['crit']}%",  inline=True)
    embed.add_field(name="💰 Gold",    value="100",               inline=True)
    embed.set_footer(text="Use /rpg hunt to start your adventure!")
    await interaction.response.send_message(embed=embed)


# ── /rpg profile ──────────────────────────────────────────────────────────────
@rpg_group.command(name="profile", description="View your (or another user's) character")
@app_commands.describe(user="User to inspect (leave blank for yourself)")
async def rpg_profile(interaction: discord.Interaction,
                      user: discord.Member = None):
    target = user or interaction.user
    row = await get_character(target.id, interaction.guild.id)
    if not row:
        who = "You don't" if target == interaction.user else f"{target.display_name} doesn't"
        return await interaction.response.send_message(
            f"{who} have a character yet. Use `/rpg start`.", ephemeral=True)
    c    = row_to_char(row)
    base = RPG_CLASSES[c["class"]]
    atk_b, def_b = equipment_bonuses(c["equipment"])
    xp_need = xp_for_level(c["level"])

    embed = discord.Embed(
        title=f"{base['emoji']} {target.display_name}'s Character",
        color=discord.Color.blurple())
    embed.add_field(name="Class",    value=c["class"].title(), inline=True)
    embed.add_field(name="Level",    value=str(c["level"]),    inline=True)
    embed.add_field(name="XP",       value=f"{c['xp']}/{xp_need}", inline=True)
    embed.add_field(name="❤️ HP",    value=f"{c['hp']}/{c['max_hp']}", inline=True)
    embed.add_field(name="⚔️ ATK",   value=f"{c['attack']} (+{atk_b})", inline=True)
    embed.add_field(name="🛡️ DEF",   value=f"{c['defense']} (+{def_b})", inline=True)
    embed.add_field(name="💰 Gold",  value=str(c["gold"]),    inline=True)
    if c["equipment"]:
        items = ", ".join(
            SHOP_ITEMS[k]["name"] for k in c["equipment"].values() if k in SHOP_ITEMS
        ) or "None"
        embed.add_field(name="🎒 Equipment", value=items, inline=False)
    await interaction.response.send_message(embed=embed)


# ── /rpg hunt ─────────────────────────────────────────────────────────────────
@rpg_group.command(name="hunt", description="Go hunt a monster for XP and gold")
async def rpg_hunt(interaction: discord.Interaction):
    row = await get_character(interaction.user.id, interaction.guild.id)
    if not row:
        return await interaction.response.send_message(
            "You don't have a character yet. Use `/rpg start`.", ephemeral=True)

    # Cooldown check
    last = HUNT_COOLDOWNS.get(interaction.user.id, 0)
    remaining = HUNT_COOLDOWN_SECS - (time.time() - last)
    if remaining > 0:
        return await interaction.response.send_message(
            f"⏳ Hunt cooldown: **{remaining:.0f}s** remaining.", ephemeral=True)
    HUNT_COOLDOWNS[interaction.user.id] = time.time()

    c = row_to_char(row)
    if c["hp"] <= 0:
        return await interaction.response.send_message(
            "❤️ You're too injured to hunt! Use `/rpg rest` to recover.", ephemeral=True)

    atk_b, def_b = equipment_bonuses(c["equipment"])
    p_atk = c["attack"] + atk_b
    p_def = c["defense"] + def_b
    crit_pct = RPG_CLASSES[c["class"]]["crit"]

    # Pick monster (harder monsters appear as player levels up)
    max_idx = min(len(MONSTERS) - 1, c["level"] // 2 + 2)
    monster = dict(MONSTERS[random.randint(0, max_idx)])
    m_hp = monster["hp"]

    log: list[str] = []
    turns = 0
    player_hp = c["hp"]

    while player_hp > 0 and m_hp > 0 and turns < 30:
        turns += 1
        # Player attacks
        dmg = max(1, p_atk - monster["def"])
        if random.randint(1, 100) <= crit_pct:
            dmg = int(dmg * 1.5)
            log.append(f"💥 CRIT! You hit {monster['name']} for **{dmg}**")
        else:
            log.append(f"You hit {monster['name']} for **{dmg}**")
        m_hp -= dmg
        if m_hp <= 0:
            break
        # Monster attacks
        m_dmg = max(1, monster["atk"] - p_def)
        player_hp -= m_dmg
        log.append(f"{monster['name']} hits you for **{m_dmg}**")

    won  = m_hp <= 0
    gold = random.randint(monster["gmin"], monster["gmax"]) if won else 0
    xp   = monster["xp"] if won else monster["xp"] // 4

    c["hp"]   = max(0, player_hp)
    c["xp"]  += xp
    c["gold"] = c["gold"] + gold if won else max(0, c["gold"] - gold // 2)

    lvl_msgs = await check_level_up(interaction.user.id, interaction.guild.id, c)
    await save_character(interaction.user.id, interaction.guild.id, c)

    embed = discord.Embed(
        title=f"{'🏆 Victory!' if won else '💀 Defeated!'} vs {monster['name']}",
        color=discord.Color.green() if won else discord.Color.red())
    embed.add_field(name="Combat Log", value="\n".join(log[-6:]) or "—", inline=False)
    embed.add_field(name="Result",
        value=(f"+{xp} XP · +{gold} 💰" if won else f"+{xp} XP · -{gold // 2} 💰"),
        inline=True)
    embed.add_field(name="❤️ HP", value=f"{c['hp']}/{c['max_hp']}", inline=True)
    if lvl_msgs:
        embed.add_field(name="⬆️ Level Up!", value="\n".join(lvl_msgs), inline=False)
    embed.set_footer(text=f"Hunt again in {HUNT_COOLDOWN_SECS}s · Use /rpg rest to heal")
    await interaction.response.send_message(embed=embed)


# ── /rpg rest ─────────────────────────────────────────────────────────────────
@rpg_group.command(name="rest", description="Restore HP by spending gold (10 gold = 20 HP)")
async def rpg_rest(interaction: discord.Interaction):
    row = await get_character(interaction.user.id, interaction.guild.id)
    if not row:
        return await interaction.response.send_message(
            "No character yet. Use `/rpg start`.", ephemeral=True)
    c = row_to_char(row)
    if c["hp"] >= c["max_hp"]:
        return await interaction.response.send_message("❤️ You're already at full HP!")
    heal_cost = 10
    if c["gold"] < heal_cost:
        return await interaction.response.send_message(
            f"💰 You need at least {heal_cost} gold to rest.", ephemeral=True)
    c["gold"] -= heal_cost
    healed     = min(20, c["max_hp"] - c["hp"])
    c["hp"]   += healed
    await save_character(interaction.user.id, interaction.guild.id, c)
    await interaction.response.send_message(
        f"🛌 Rested! Restored **{healed} HP** for **{heal_cost} gold**.\n"
        f"HP: {c['hp']}/{c['max_hp']} · Gold: {c['gold']}")


# ── /rpg shop ─────────────────────────────────────────────────────────────────
@rpg_group.command(name="shop", description="View the item shop")
async def rpg_shop(interaction: discord.Interaction):
    embed = discord.Embed(title="🏪 Item Shop", color=discord.Color.gold())
    weapons = [(k, v) for k, v in SHOP_ITEMS.items() if v["type"] == "weapon"]
    armor   = [(k, v) for k, v in SHOP_ITEMS.items() if v["type"] == "armor"]
    embed.add_field(
        name="⚔️ Weapons",
        value="\n".join(
            f"`{k}` — **{v['name']}** | +{v['atk']} ATK | {v['cost']} 💰"
            for k, v in weapons),
        inline=False)
    embed.add_field(
        name="🛡️ Armor",
        value="\n".join(
            f"`{k}` — **{v['name']}** | +{v['def']} DEF | {v['cost']} 💰"
            for k, v in armor),
        inline=False)
    embed.set_footer(text="Use /rpg buy <item_id> to purchase")
    await interaction.response.send_message(embed=embed)


# ── /rpg buy ──────────────────────────────────────────────────────────────────
@rpg_group.command(name="buy", description="Buy an item from the shop")
@app_commands.describe(item_id="Item ID shown in /rpg shop")
async def rpg_buy(interaction: discord.Interaction, item_id: str):
    row = await get_character(interaction.user.id, interaction.guild.id)
    if not row:
        return await interaction.response.send_message(
            "No character yet. Use `/rpg start`.", ephemeral=True)
    item = SHOP_ITEMS.get(item_id.lower())
    if not item:
        return await interaction.response.send_message(
            "Unknown item. Check `/rpg shop` for valid item IDs.", ephemeral=True)
    c = row_to_char(row)
    if c["gold"] < item["cost"]:
        return await interaction.response.send_message(
            f"💰 Need **{item['cost']}** gold, you have **{c['gold']}**.", ephemeral=True)
    slot = item["type"]  # "weapon" or "armor"
    old  = c["equipment"].get(slot)
    c["equipment"][slot] = item_id.lower()
    c["gold"] -= item["cost"]
    await save_character(interaction.user.id, interaction.guild.id, c)
    msg = f"✅ Bought **{item['name']}** for {item['cost']} 💰 and equipped it!"
    if old:
        msg += f"\n*(Replaced **{SHOP_ITEMS[old]['name']}**)*"
    await interaction.response.send_message(msg)


# ── /rpg inventory ────────────────────────────────────────────────────────────
@rpg_group.command(name="inventory", description="View your equipped items")
async def rpg_inventory(interaction: discord.Interaction):
    row = await get_character(interaction.user.id, interaction.guild.id)
    if not row:
        return await interaction.response.send_message(
            "No character yet. Use `/rpg start`.", ephemeral=True)
    c = row_to_char(row)
    equip = c["equipment"]
    if not equip:
        return await interaction.response.send_message("🎒 Your inventory is empty.")
    lines = []
    for slot, item_id in equip.items():
        item = SHOP_ITEMS.get(item_id, {})
        bonus = f"+{item.get('atk',0)} ATK" if slot == "weapon" else f"+{item.get('def',0)} DEF"
        lines.append(f"**{slot.title()}:** {item.get('name', item_id)} ({bonus})")
    await interaction.response.send_message("🎒 **Inventory**\n" + "\n".join(lines))


# ── /rpg duel ─────────────────────────────────────────────────────────────────
class DuelView(discord.ui.View):
    def __init__(self, challenger: discord.Member, target: discord.Member):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.target     = target

    @discord.ui.button(label="⚔️ Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message(
                "Only the challenged player can accept!", ephemeral=True)
        self.stop()
        await self._run_duel(interaction)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message(
                "Only the challenged player can decline!", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(
            content=f"❌ {self.target.display_name} declined the duel.", view=None)

    async def on_timeout(self):
        try:
            await self._message.edit(content="⏰ Duel challenge expired.", view=None)
        except Exception:
            pass

    async def _run_duel(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        c_row    = await get_character(self.challenger.id, guild_id)
        t_row    = await get_character(self.target.id,     guild_id)

        if not c_row or not t_row:
            return await interaction.response.edit_message(
                content="One of the players doesn't have a character.", view=None)

        c_char = row_to_char(c_row)
        t_char = row_to_char(t_row)

        if c_char["hp"] <= 0 or t_char["hp"] <= 0:
            return await interaction.response.edit_message(
                content="One of the players is too injured to duel.", view=None)

        c_atk_b, c_def_b = equipment_bonuses(c_char["equipment"])
        t_atk_b, t_def_b = equipment_bonuses(t_char["equipment"])

        c_atk = c_char["attack"]  + c_atk_b
        c_def = c_char["defense"] + c_def_b
        t_atk = t_char["attack"]  + t_atk_b
        t_def = t_char["defense"] + t_def_b
        c_crit = RPG_CLASSES[c_char["class"]]["crit"]
        t_crit = RPG_CLASSES[t_char["class"]]["crit"]

        c_hp = c_char["hp"]
        t_hp = t_char["hp"]
        log  = []
        turn = 0

        while c_hp > 0 and t_hp > 0 and turn < 30:
            turn += 1
            # Challenger attacks
            dmg = max(1, c_atk - t_def)
            if random.randint(1, 100) <= c_crit:
                dmg = int(dmg * 1.5)
                log.append(f"💥 {self.challenger.display_name} CRITS for **{dmg}**!")
            else:
                log.append(f"{self.challenger.display_name} hits for **{dmg}**")
            t_hp -= dmg
            if t_hp <= 0:
                break
            # Target attacks back
            dmg2 = max(1, t_atk - c_def)
            if random.randint(1, 100) <= t_crit:
                dmg2 = int(dmg2 * 1.5)
                log.append(f"💥 {self.target.display_name} CRITS for **{dmg2}**!")
            else:
                log.append(f"{self.target.display_name} hits for **{dmg2}**")
            c_hp -= dmg2

        winner = self.challenger if t_hp <= 0 else self.target
        loser  = self.target     if t_hp <= 0 else self.challenger
        w_char = c_char if winner == self.challenger else t_char
        l_char = t_char if winner == self.challenger else c_char
        w_uid  = self.challenger.id if winner == self.challenger else self.target.id
        l_uid  = self.target.id     if winner == self.challenger else self.challenger.id

        gold_loot = max(1, l_char["gold"] // 10)
        xp_gain   = 50 + w_char["level"] * 10

        w_char["gold"] += gold_loot
        w_char["xp"]   += xp_gain
        w_char["hp"]    = max(1, int(w_char["hp"] * 0.8))  # winner loses a bit of HP
        lvl_msgs = await check_level_up(w_uid, guild_id, w_char)
        await save_character(w_uid, guild_id, w_char)

        l_char["hp"]   = max(1, int(l_char["hp"] * 0.6))
        l_char["gold"] = max(0, l_char["gold"] - gold_loot)
        await save_character(l_uid, guild_id, l_char)

        embed = discord.Embed(
            title=f"⚔️ Duel: {self.challenger.display_name} vs {self.target.display_name}",
            color=discord.Color.gold())
        embed.add_field(name="Combat Log", value="\n".join(log[-8:]) or "—", inline=False)
        embed.add_field(name="🏆 Winner",  value=winner.mention,                  inline=True)
        embed.add_field(name="💰 Spoils",  value=f"+{gold_loot} gold · +{xp_gain} XP", inline=True)
        if lvl_msgs:
            embed.add_field(name="⬆️ Level Up!", value="\n".join(lvl_msgs), inline=False)
        embed.set_footer(text=f"Duel ended in {turn} turn(s)")
        await interaction.response.edit_message(content=None, embed=embed, view=None)


@rpg_group.command(name="duel", description="Challenge another player to a duel")
@app_commands.describe(user="Player to challenge")
async def rpg_duel(interaction: discord.Interaction, user: discord.Member):
    if user.id == interaction.user.id:
        return await interaction.response.send_message("You can't duel yourself!", ephemeral=True)
    if user.bot:
        return await interaction.response.send_message("You can't duel a bot!", ephemeral=True)

    c_row = await get_character(interaction.user.id, interaction.guild.id)
    t_row = await get_character(user.id, interaction.guild.id)
    if not c_row:
        return await interaction.response.send_message(
            "You don't have a character. Use `/rpg start`.", ephemeral=True)
    if not t_row:
        return await interaction.response.send_message(
            f"{user.display_name} doesn't have a character yet.", ephemeral=True)

    view = DuelView(interaction.user, user)
    msg  = await interaction.response.send_message(
        f"⚔️ {interaction.user.mention} challenges {user.mention} to a duel!\n"
        f"{user.mention}, do you accept?",
        view=view)
    view._message = await interaction.original_response()


bot.tree.add_command(rpg_group)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTOROLE
# ═══════════════════════════════════════════════════════════════════════════════
autorole_group = app_commands.Group(name="autorole",
    description="Automatically assign a role to every new member")


@autorole_group.command(name="set", description="Set the role new members receive on join")
@app_commands.describe(role="Role to auto-assign")
async def autorole_set(interaction: discord.Interaction, role: discord.Role):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    if role.managed or role >= interaction.guild.me.top_role:
        return await interaction.response.send_message(
            "I can't assign that role — it's either managed by an integration "
            "or above my highest role.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET autorole_id=? WHERE guild_id=?",
        (role.id, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Autorole set to {role.mention} — new members will receive it on join.")


@autorole_group.command(name="disable", description="Disable autorole")
async def autorole_disable(interaction: discord.Interaction):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET autorole_id=NULL WHERE guild_id=?",
        (interaction.guild.id,))
    await bot.db.commit()
    await interaction.response.send_message("✅ Autorole disabled.")


@autorole_group.command(name="view", description="Show the current autorole")
async def autorole_view(interaction: discord.Interaction):
    cfg = await get_guild_config(interaction.guild.id)
    autorole_id = cfg[8] if len(cfg) > 8 else None
    if not autorole_id:
        return await interaction.response.send_message("No autorole is set.", ephemeral=True)
    role = interaction.guild.get_role(autorole_id)
    if role:
        await interaction.response.send_message(f"🎭 Autorole: {role.mention}", ephemeral=True)
    else:
        await interaction.response.send_message(
            "⚠️ The saved autorole no longer exists. Use `/autorole set` to update it.",
            ephemeral=True)


bot.tree.add_command(autorole_group)


# ═══════════════════════════════════════════════════════════════════════════════
# ROLE MANAGEMENT  (give / take / removeall)
# ═══════════════════════════════════════════════════════════════════════════════
role_group = app_commands.Group(name="role", description="Manage member roles")


@role_group.command(name="give", description="Give a role to a member")
@app_commands.describe(member="Member to give the role to", role="Role to assign")
async def role_give(interaction: discord.Interaction, member: discord.Member,
                    role: discord.Role):
    if not has_perm(interaction, "manage_roles"):
        return await interaction.response.send_message("Need Manage Roles.", ephemeral=True)
    if role.managed:
        return await interaction.response.send_message(
            "That role is managed by an integration and can't be assigned manually.",
            ephemeral=True)
    if role >= interaction.guild.me.top_role:
        return await interaction.response.send_message(
            "That role is above my highest role — I can't assign it.", ephemeral=True)
    if role >= interaction.user.top_role and not has_perm(interaction, "administrator"):
        return await interaction.response.send_message(
            "You can't assign a role equal to or above your own.", ephemeral=True)
    if role in member.roles:
        return await interaction.response.send_message(
            f"{member.mention} already has {role.mention}.", ephemeral=True)
    try:
        await member.add_roles(role, reason=f"Role given by {interaction.user}")
        await interaction.response.send_message(
            f"✅ Gave {role.mention} to {member.mention}.")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@role_group.command(name="take", description="Remove a role from a member")
@app_commands.describe(member="Member to remove the role from", role="Role to remove")
async def role_take(interaction: discord.Interaction, member: discord.Member,
                    role: discord.Role):
    if not has_perm(interaction, "manage_roles"):
        return await interaction.response.send_message("Need Manage Roles.", ephemeral=True)
    if role.managed:
        return await interaction.response.send_message(
            "That role is managed by an integration and can't be removed manually.",
            ephemeral=True)
    if role >= interaction.guild.me.top_role:
        return await interaction.response.send_message(
            "That role is above my highest role — I can't remove it.", ephemeral=True)
    if role >= interaction.user.top_role and not has_perm(interaction, "administrator"):
        return await interaction.response.send_message(
            "You can't remove a role equal to or above your own.", ephemeral=True)
    if role not in member.roles:
        return await interaction.response.send_message(
            f"{member.mention} doesn't have {role.mention}.", ephemeral=True)
    try:
        await member.remove_roles(role, reason=f"Role taken by {interaction.user}")
        await interaction.response.send_message(
            f"✅ Removed {role.mention} from {member.mention}.")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@role_group.command(name="removeall", description="Remove all assignable roles from a member")
@app_commands.describe(member="Member to strip roles from")
async def role_removeall(interaction: discord.Interaction, member: discord.Member):
    if not has_perm(interaction, "manage_roles"):
        return await interaction.response.send_message("Need Manage Roles.", ephemeral=True)
    if member.top_role >= interaction.user.top_role and not has_perm(interaction, "administrator"):
        return await interaction.response.send_message(
            "You can't remove roles from someone with an equal or higher role.", ephemeral=True)

    removable = [
        r for r in member.roles
        if r != interaction.guild.default_role
        and not r.managed
        and r < interaction.guild.me.top_role
        and r < interaction.user.top_role
    ]

    if not removable:
        return await interaction.response.send_message(
            f"{member.mention} has no roles I can remove.", ephemeral=True)

    await interaction.response.defer()
    try:
        await member.remove_roles(*removable, reason=f"All roles removed by {interaction.user}")
        names = ", ".join(r.mention for r in removable)
        await interaction.followup.send(
            f"✅ Removed **{len(removable)}** role(s) from {member.mention}: {names}")
    except discord.Forbidden:
        await interaction.followup.send("Missing permissions to remove some roles.", ephemeral=True)


bot.tree.add_command(role_group)


# ═══════════════════════════════════════════════════════════════════════════════
# GIVEAWAY
# ═══════════════════════════════════════════════════════════════════════════════
def _parse_duration(s: str) -> timedelta | None:
    """Parse a duration string like '1d', '2h30m', '90s' into a timedelta."""
    total = timedelta()
    found = False
    for amount, unit in re.findall(r'(\d+)\s*([dhms])', s.lower()):
        found = True
        amount = int(amount)
        if unit == 'd':   total += timedelta(days=amount)
        elif unit == 'h': total += timedelta(hours=amount)
        elif unit == 'm': total += timedelta(minutes=amount)
        elif unit == 's': total += timedelta(seconds=amount)
    return total if found else None


def _giveaway_embed(prize: str, description: str, host_id: int, winners_count: int,
                    ends_at: datetime, entries: list, ended: bool = False,
                    winner_ids: list | None = None) -> discord.Embed:
    unix = int(ends_at.timestamp())
    color = discord.Color.from_rgb(88, 101, 242)  # Discord blurple, matching the image
    if ended:
        color = discord.Color.greyple()

    embed = discord.Embed(
        title=prize,
        description=(description + "\n\u200b") if description else "\u200b",
        color=color,
    )
    if ended:
        embed.add_field(name="Ended",      value=f"<t:{unix}:F>",                  inline=False)
    else:
        embed.add_field(name="Ends",       value=f"<t:{unix}:R>  (<t:{unix}:F>)",  inline=False)
    embed.add_field(name="Hosted by",      value=f"<@{host_id}>",                  inline=True)
    embed.add_field(name="Entries",        value=str(len(entries)),                 inline=True)
    embed.add_field(name="Winners",        value=str(winners_count),               inline=True)

    if ended:
        if winner_ids:
            embed.add_field(
                name="🏆 Winner(s)",
                value=" ".join(f"<@{w}>" for w in winner_ids),
                inline=False,
            )
        else:
            embed.add_field(name="🏆 Winner(s)", value="No valid entries.", inline=False)

    embed.set_footer(text=ends_at.strftime("%d/%m/%Y"))
    return embed


class GiveawayEndedView(discord.ui.View):
    """Shown on the embed after a giveaway ends — has a Reroll button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎲 Reroll", style=discord.ButtonStyle.secondary,
                       custom_id="giveaway_reroll")
    async def reroll(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "You need Manage Server permission to reroll.", ephemeral=True)

        msg_id = interaction.message.id
        async with bot.db.execute(
            "SELECT channel_id, guild_id, ended FROM giveaways WHERE message_id=?",
            (msg_id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                "Giveaway not found.", ephemeral=True)
        if not row[2]:
            return await interaction.response.send_message(
                "This giveaway hasn't ended yet.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        # Temporarily unflag so _end_giveaway can re-pick
        await bot.db.execute(
            "UPDATE giveaways SET ended=0 WHERE message_id=?", (msg_id,))
        await bot.db.commit()
        await _end_giveaway(row[1], row[0], msg_id, reroll=True)
        await interaction.followup.send("🎲 Winner rerolled!", ephemeral=True)


class GiveawayView(discord.ui.View):
    """Persistent view — survives bot restarts because custom_id is fixed."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎉", style=discord.ButtonStyle.primary,
                       custom_id="giveaway_enter")
    async def enter(self, interaction: discord.Interaction,
                    button: discord.ui.Button):
        msg_id = interaction.message.id
        async with bot.db.execute(
            "SELECT entries, ended, ends_at FROM giveaways WHERE message_id=?", (msg_id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                "This giveaway no longer exists.", ephemeral=True)

        entries_list, ended, ends_at_str = row
        if ended:
            return await interaction.response.send_message(
                "This giveaway has already ended!", ephemeral=True)

        # Check expiry
        ends_at = datetime.fromisoformat(ends_at_str)
        if datetime.utcnow() > ends_at:
            return await interaction.response.send_message(
                "This giveaway has already ended!", ephemeral=True)

        entries = json.loads(entries_list or "[]")
        uid = interaction.user.id

        if uid in entries:
            # Toggle — let them leave the giveaway
            entries.remove(uid)
            await bot.db.execute(
                "UPDATE giveaways SET entries=? WHERE message_id=?",
                (json.dumps(entries), msg_id))
            await bot.db.commit()
            await _refresh_giveaway_embed(interaction.message, msg_id)
            return await interaction.response.send_message(
                "You have left the giveaway.", ephemeral=True)

        entries.append(uid)
        await bot.db.execute(
            "UPDATE giveaways SET entries=? WHERE message_id=?",
            (json.dumps(entries), msg_id))
        await bot.db.commit()
        await _refresh_giveaway_embed(interaction.message, msg_id)
        await interaction.response.send_message(
            "🎉 You've entered the giveaway! Good luck!", ephemeral=True)


async def _refresh_giveaway_embed(message: discord.Message, msg_id: int):
    """Re-draw the giveaway embed with the current entry count."""
    async with bot.db.execute(
        "SELECT prize, description, host_id, winners, ends_at, entries, ended "
        "FROM giveaways WHERE message_id=?", (msg_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return
    prize, desc, host_id, winners, ends_at_str, entries_json, ended = row
    entries  = json.loads(entries_json or "[]")
    ends_at  = datetime.fromisoformat(ends_at_str)
    embed    = _giveaway_embed(prize, desc, host_id, winners, ends_at, entries, bool(ended))
    try:
        await message.edit(embed=embed)
    except discord.HTTPException:
        pass


async def _end_giveaway(guild_id: int, channel_id: int, message_id: int,
                        reroll: bool = False):
    """Pick winner(s), update the embed, announce in channel."""
    async with bot.db.execute(
        "SELECT prize, description, host_id, winners, ends_at, entries "
        "FROM giveaways WHERE message_id=?", (message_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return

    prize, desc, host_id, winners_count, ends_at_str, entries_json = row
    entries  = json.loads(entries_json or "[]")
    ends_at  = datetime.fromisoformat(ends_at_str)
    guild    = bot.get_guild(guild_id)
    channel  = guild.get_channel(channel_id) if guild else None

    # Pick winners
    pool         = [e for e in entries if guild and guild.get_member(e)]
    winner_ids   = random.sample(pool, min(winners_count, len(pool))) if pool else []

    # Mark ended
    await bot.db.execute(
        "UPDATE giveaways SET ended=1 WHERE message_id=?", (message_id,))
    await bot.db.commit()

    # Edit original message
    if channel:
        try:
            msg   = await channel.fetch_message(message_id)
            embed = _giveaway_embed(prize, desc, host_id, winners_count,
                                    ends_at, entries, ended=True, winner_ids=winner_ids)
            await msg.edit(embed=embed, view=GiveawayEndedView())
        except (discord.NotFound, discord.HTTPException):
            pass

        # Announce
        if winner_ids:
            mentions = " ".join(f"<@{w}>" for w in winner_ids)
            action   = "rerolled" if reroll else "ended"
            await channel.send(
                f"🎉 **Giveaway {action}!** Congratulations {mentions}! "
                f"You won **{prize}**!")
        else:
            await channel.send(
                f"😔 The **{prize}** giveaway ended but there were no valid entries.")


@tasks.loop(seconds=30)
async def giveaway_ticker():
    """Auto-end giveaways whose timer has expired."""
    await bot.wait_until_ready()
    now = datetime.utcnow().isoformat()
    async with bot.db.execute(
        "SELECT message_id, channel_id, guild_id FROM giveaways "
        "WHERE ended=0 AND ends_at <= ?", (now,)
    ) as cur:
        rows = await cur.fetchall()
    for msg_id, ch_id, g_id in rows:
        await _end_giveaway(g_id, ch_id, msg_id)


# ── Commands ──────────────────────────────────────────────────────────────────
giveaway_group = app_commands.Group(name="giveaway", description="Manage giveaways")


@giveaway_group.command(name="create", description="Start a new giveaway")
@app_commands.describe(
    prize="What are you giving away?",
    duration="Duration, e.g. 1d / 2h30m / 90m / 7d",
    winners="Number of winners (default 1)",
    channel="Channel to post in (defaults to current channel)",
    description="Optional extra description shown in the embed",
)
async def gw_create(
    interaction: discord.Interaction,
    prize: str,
    duration: str,
    winners: int = 1,
    channel: discord.TextChannel = None,
    description: str = "",
):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)

    delta = _parse_duration(duration)
    if delta is None or delta.total_seconds() < 10:
        return await interaction.response.send_message(
            "Invalid duration. Use values like `10m`, `2h`, `1d`, `7d12h`.",
            ephemeral=True)

    winners = max(1, winners)
    dest    = channel or interaction.channel
    ends_at = datetime.utcnow() + delta

    embed = _giveaway_embed(
        prize, description, interaction.user.id, winners, ends_at, [])

    await interaction.response.defer(ephemeral=True)
    msg = await dest.send(embed=embed, view=GiveawayView())

    await bot.db.execute(
        """INSERT INTO giveaways
               (message_id, channel_id, guild_id, host_id, prize, description,
                winners, ends_at, ended, entries)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '[]')""",
        (msg.id, dest.id, interaction.guild.id, interaction.user.id,
         prize, description, winners, ends_at.isoformat()))
    await bot.db.commit()

    await interaction.followup.send(
        f"✅ Giveaway successfully created! **ID:** `{msg.id}`\n"
        f"Posted in {dest.mention}.",
        ephemeral=True)


@giveaway_group.command(name="end", description="End a giveaway early and pick a winner now")
@app_commands.describe(message_id="ID of the giveaway message")
async def gw_end(interaction: discord.Interaction, message_id: str):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("Invalid message ID.", ephemeral=True)

    async with bot.db.execute(
        "SELECT channel_id, guild_id, ended FROM giveaways WHERE message_id=?", (mid,)
    ) as cur:
        row = await cur.fetchone()

    if not row:
        return await interaction.response.send_message(
            "No giveaway found with that ID.", ephemeral=True)
    if row[2]:
        return await interaction.response.send_message(
            "That giveaway has already ended.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    await _end_giveaway(row[1], row[0], mid)
    await interaction.followup.send("✅ Giveaway ended and winner(s) selected!", ephemeral=True)


@giveaway_group.command(name="reroll", description="Reroll the winner of an ended giveaway")
@app_commands.describe(message_id="ID of the giveaway message")
async def gw_reroll(interaction: discord.Interaction, message_id: str):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("Invalid message ID.", ephemeral=True)

    async with bot.db.execute(
        "SELECT channel_id, guild_id, ended FROM giveaways WHERE message_id=?", (mid,)
    ) as cur:
        row = await cur.fetchone()

    if not row:
        return await interaction.response.send_message(
            "No giveaway found with that ID.", ephemeral=True)
    if not row[2]:
        return await interaction.response.send_message(
            "That giveaway hasn't ended yet. Use `/giveaway end` first.", ephemeral=True)

    # Allow reroll — temporarily unflag ended so _end_giveaway can re-pick
    await bot.db.execute("UPDATE giveaways SET ended=0 WHERE message_id=?", (mid,))
    await bot.db.commit()

    await interaction.response.defer(ephemeral=True)
    await _end_giveaway(row[1], row[0], mid, reroll=True)
    await interaction.followup.send("🎲 Winner rerolled!", ephemeral=True)


bot.tree.add_command(giveaway_group)


# ═══════════════════════════════════════════════════════════════════════════════
# AFK
# ═══════════════════════════════════════════════════════════════════════════════
AFK_PREFIX = "[AFK] "


@bot.tree.command(name="afk", description="Set yourself as AFK — adds [AFK] in front of your name")
@app_commands.describe(reason="Why you're going AFK (shown when someone mentions you)")
async def afk_cmd(interaction: discord.Interaction, reason: str = "AFK"):
    member = interaction.user
    guild  = interaction.guild

    # Check not already AFK
    async with bot.db.execute(
        "SELECT 1 FROM afk WHERE guild_id=? AND user_id=?",
        (guild.id, member.id)
    ) as cur:
        if await cur.fetchone():
            return await interaction.response.send_message(
                "You're already AFK! Send any message to remove it.", ephemeral=True)

    # Save current nick before we touch it
    old_nick = member.nick  # None means using username

    # Build new nickname — cap at 32 chars (Discord limit)
    base_name = member.nick or member.name
    new_nick   = (AFK_PREFIX + base_name)[:32]

    # Save to DB
    await bot.db.execute(
        "INSERT OR REPLACE INTO afk (guild_id, user_id, reason, old_nick) VALUES (?, ?, ?, ?)",
        (guild.id, member.id, reason, old_nick))
    await bot.db.commit()

    # Try to update nickname
    nick_updated = False
    try:
        await member.edit(nick=new_nick, reason="User went AFK")
        nick_updated = True
    except discord.Forbidden:
        pass  # Can't rename server owner or someone above bot

    msg = f"💤 You're now AFK: *{reason}*"
    if not nick_updated:
        msg += "\n*(I couldn't update your nickname — you may be above my role or the server owner)*"
    await interaction.response.send_message(msg, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════════════════
bot.run(TOKEN)
