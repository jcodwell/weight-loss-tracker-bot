"""
Accountability bot for a fitness Discord.

What it does:
  - Watches a designated channel for posted screenshots (food-log app + Apple Fitness).
  - Uses Claude's vision API to extract structured data from each image.
  - Asks the poster to confirm the parse with a ✅ reaction (vision can misread).
  - Stores one row per user per day.
  - Posts a weekly leaderboard ranked on CONSISTENCY + ACTIVITY (see scoring.py rationale).

Set up via environment variables (see .env.example / README):
  DISCORD_TOKEN, ANTHROPIC_API_KEY, TRACKING_CHANNEL_ID, (optional) LEADERBOARD_CHANNEL_ID
"""

import os
import json
import base64
import sqlite3
import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands, tasks
from anthropic import Anthropic
from dotenv import load_dotenv

from zoneinfo import ZoneInfo
from scoring import total_scores, entry_points, LATE_FOOD_PENALTY

EST = ZoneInfo("America/New_York")

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TRACKING_CHANNEL_ID = int(os.environ["TRACKING_CHANNEL_ID"])

# Haiku is cheap and plenty for reading clear screenshots. Switch to a Sonnet
# model if your members post low-quality / cluttered images. Check the current
# model list + pricing at https://docs.claude.com/en/docs/about-claude/models
VISION_MODEL = "claude-haiku-4-5-latest"

DB_PATH = "fitness.db"
client = Anthropic(api_key=ANTHROPIC_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                user_id        TEXT NOT NULL,
                username       TEXT NOT NULL,
                day            TEXT NOT NULL,         -- YYYY-MM-DD
                logged_food    INTEGER DEFAULT 0,     -- posted a readable food-log screenshot
                active_minutes INTEGER DEFAULT 0,     -- Apple Fitness exercise minutes
                move_goal_met  INTEGER DEFAULT 0,     -- Apple Fitness move ring closed
                late_penalty   INTEGER DEFAULT 0,     -- 1 if food log posted after 7 PM EST
                PRIMARY KEY (user_id, day)
            )
            """
        )
        # Migrate older databases missing the late_penalty column
        try:
            conn.execute("ALTER TABLE entries ADD COLUMN late_penalty INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists


def upsert_entry(user_id, username, day, *, logged_food=None,
                 active_minutes=None, move_goal_met=None, late_penalty=None):
    """Merge new fields into a user's row for the day without clobbering the rest."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM entries WHERE user_id=? AND day=?", (user_id, day)
        ).fetchone()
        base = dict(row) if row else {
            "user_id": user_id, "username": username, "day": day,
            "logged_food": 0, "active_minutes": 0, "move_goal_met": 0,
            "late_penalty": 0,
        }
        if logged_food is not None:
            base["logged_food"] = int(logged_food)
        if active_minutes is not None:
            base["active_minutes"] = max(int(base["active_minutes"]), int(active_minutes))
        if move_goal_met is not None:
            base["move_goal_met"] = int(move_goal_met)
        if late_penalty is not None:
            base["late_penalty"] = int(late_penalty)
        base["username"] = username
        conn.execute(
            """
            INSERT INTO entries (user_id, username, day, logged_food, active_minutes, move_goal_met, late_penalty)
            VALUES (:user_id, :username, :day, :logged_food, :active_minutes, :move_goal_met, :late_penalty)
            ON CONFLICT(user_id, day) DO UPDATE SET
                username=excluded.username,
                logged_food=excluded.logged_food,
                active_minutes=excluded.active_minutes,
                move_goal_met=excluded.move_goal_met,
                late_penalty=excluded.late_penalty
            """,
            base,
        )


def all_entries():
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM entries"
        ).fetchall()]


def clear_all_entries():
    with db() as conn:
        conn.execute("DELETE FROM entries")


def all_tracked_users():
    """Return a list of distinct (user_id, username) that have ever had an entry."""
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id, username FROM entries"
        ).fetchall()
        return [(r["user_id"], r["username"]) for r in rows]


# --------------------------------------------------------------------------- #
# Vision: read a screenshot -> structured data
# --------------------------------------------------------------------------- #
VISION_PROMPT = """You are reading a screenshot from a fitness app. It is either a
food-logging app (e.g. MyFitnessPal, Lose It!, Cronometer) or Apple Fitness/activity rings.

Return ONLY a JSON object, no prose, no markdown fences, with these keys:
{
  "kind": "food_log" | "apple_fitness" | "unknown",
  "logged_food": true | false,        // true if this is a readable food-log screen showing entries were logged
  "active_minutes": integer | null,   // Apple Fitness "Exercise" minutes if visible, else null
  "move_goal_met": true | false | null, // true if the red Move ring is fully closed, else false/null
  "readable": true | false            // false if the image is too blurry/cropped to interpret
}
If a value isn't present in the image, use null. Do not guess."""


def image_block_from_bytes(data: bytes, content_type: str):
    media_type = content_type if content_type in (
        "image/png", "image/jpeg", "image/gif", "image/webp"
    ) else "image/png"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("utf-8"),
        },
    }


def parse_screenshot(data: bytes, content_type: str) -> dict:
    resp = client.messages.create(
        model=VISION_MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                image_block_from_bytes(data, content_type),
                {"type": "text", "text": VISION_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"kind": "unknown", "readable": False}


# Pending confirmations: message_id -> dict(parsed payload + author)
pending = {}


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
@bot.event
async def on_ready():
    init_db()
    if not daily_food_penalty.is_running():
        daily_food_penalty.start()
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Slash sync failed:", e)
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.channel.id != TRACKING_CHANNEL_ID:
        await bot.process_commands(message)
        return

    # "oorah" joins the leaderboard with a score of zero
    if message.content.strip().lower() == "oorah":
        user_id = str(message.author.id)
        username = message.author.display_name
        day = dt.date.today().isoformat()
        with db() as conn:
            existing = conn.execute(
                "SELECT 1 FROM entries WHERE user_id=?", (user_id,)
            ).fetchone()
        if existing:
            await message.reply("You're already on the board!", mention_author=False)
        else:
            upsert_entry(user_id, username, day, logged_food=0, active_minutes=0, move_goal_met=0)
            # Determine their rank with ties
            ranked = total_scores(all_entries())
            rank = 1
            for i, r in enumerate(ranked):
                if i > 0 and r["score"] < ranked[i - 1]["score"]:
                    rank = i + 1
                if r["username"] == username:
                    break
            # Find everyone at the same rank
            user_score = next(r["score"] for r in ranked if r["username"] == username)
            tied_with = [r["username"] for r in ranked if r["score"] == user_score and r["username"] != username]
            rank_label = f"{rank}{'st' if rank == 1 else 'nd' if rank == 2 else 'rd' if rank == 3 else 'th'} place"
            if tied_with:
                tied_names = ", ".join(tied_with)
                rank_text = f"**{rank_label}** (tied with {tied_names})"
            else:
                rank_text = f"**{rank_label}**"
            await message.reply(
                f"Welcome **{username}**! You're on the leaderboard at {rank_text} with **{user_score} pts**.\n"
                f"Post screenshots to earn points!",
                mention_author=False,
            )
        await bot.process_commands(message)
        return

    images = [a for a in message.attachments
              if (a.content_type or "").startswith("image/")]
    if not images:
        await bot.process_commands(message)
        return

    day = dt.date.today().isoformat()
    results = []
    for att in images:
        raw = await att.read()
        try:
            parsed = parse_screenshot(raw, att.content_type or "image/png")
        except Exception as e:
            print(f"Vision API error for {att.filename}: {type(e).__name__}: {e}")
            await message.reply(
                "Something went wrong reading that image — try again in a moment.",
                mention_author=False,
            )
            return
        results.append(parsed)

    # Merge what we read across however many images they posted.
    payload = {"logged_food": None, "active_minutes": None, "move_goal_met": None}
    readable_any = False
    for p in results:
        if not p.get("readable", False):
            continue
        readable_any = True
        if p.get("kind") == "food_log" and p.get("logged_food"):
            payload["logged_food"] = True
        if p.get("active_minutes") is not None:
            payload["active_minutes"] = max(
                payload["active_minutes"] or 0, int(p["active_minutes"])
            )
        if p.get("move_goal_met") is not None:
            payload["move_goal_met"] = bool(p["move_goal_met"])

    if not readable_any:
        await message.reply(
            "I couldn't read that clearly — try a fuller, less cropped screenshot.",
            mention_author=False,
        )
        return

    summary = []
    if payload["logged_food"]:
        summary.append("food logged ✅")
    if payload["active_minutes"]:
        summary.append(f"{payload['active_minutes']} active min")
    if payload["move_goal_met"]:
        summary.append("move goal closed 🔴")
    summary_text = ", ".join(summary) if summary else "nothing trackable found"

    confirm = await message.reply(
        f"Read: **{summary_text}** for {day}.\nReact ✅ to confirm, ❌ to discard.",
        mention_author=False,
    )
    await confirm.add_reaction("✅")
    await confirm.add_reaction("❌")
    pending[confirm.id] = {
        "user_id": str(message.author.id),
        "username": message.author.display_name,
        "day": day,
        "payload": payload,
    }
    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.message_id not in pending:
        return
    item = pending[payload.message_id]
    if str(payload.user_id) != item["user_id"]:
        return  # only the poster confirms their own entry

    if str(payload.emoji) == "✅":
        p = item["payload"]
        pts_added = entry_points(
            logged_food=p["logged_food"],
            active_minutes=p["active_minutes"],
            move_goal_met=p["move_goal_met"],
        )
        upsert_entry(
            item["user_id"], item["username"], item["day"],
            logged_food=p["logged_food"],
            active_minutes=p["active_minutes"],
            move_goal_met=p["move_goal_met"],
        )
        # Calculate new running total
        rows = all_entries()
        ranked = total_scores(rows)
        user_total = next(
            (r["score"] for r in ranked if r["username"] == item["username"]), 0
        )
        channel = bot.get_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        await msg.edit(
            content=f"✅ Saved for {item['day']}. **+{pts_added} pts** → total: **{user_total} pts**"
        )
        pending.pop(payload.message_id, None)
    elif str(payload.emoji) == "❌":
        channel = bot.get_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        await msg.edit(content="❌ Discarded. Repost when ready.")
        pending.pop(payload.message_id, None)


# --------------------------------------------------------------------------- #
# Daily food-log penalty — runs at 7:00 PM EST every day
# --------------------------------------------------------------------------- #
@tasks.loop(time=dt.time(hour=19, minute=0, tzinfo=EST))
async def daily_food_penalty():
    """Penalize tracked users who didn't log food today."""
    today = dt.datetime.now(EST).date().isoformat()
    channel = bot.get_channel(TRACKING_CHANNEL_ID)
    penalised = []
    for user_id, username in all_tracked_users():
        with db() as conn:
            row = conn.execute(
                "SELECT logged_food FROM entries WHERE user_id=? AND day=?",
                (user_id, today),
            ).fetchone()
        already_logged = row and row["logged_food"]
        if not already_logged:
            upsert_entry(user_id, username, today, late_penalty=1)
            penalised.append(username)
    if penalised and channel:
        names = ", ".join(f"**{n}**" for n in penalised)
        await channel.send(
            f"⏰ No food log posted by 7 PM EST — **{LATE_FOOD_PENALTY} pts** penalty for: {names}"
        )


# --------------------------------------------------------------------------- #
# Leaderboard
# --------------------------------------------------------------------------- #
def build_leaderboard_embed():
    rows = all_entries()
    ranked = total_scores(rows)
    embed = discord.Embed(
        title="🏆 Standings",
        description="Ranked on consistency + activity.",
        color=0x00B894,
    )
    if not ranked:
        embed.add_field(name="No entries yet", value="Post your screenshots!", inline=False)
        return embed
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(ranked):
        prefix = medals[i] if i < 3 else f"#{i + 1}"
        embed.add_field(
            name=f"{prefix}  {r['username']} — {r['score']} pts",
            value=f"logged {r['days_logged']} days · "
                  f"{r['active_minutes']} active min · "
                  f"{r['move_goals']} move goals · streak {r['streak']}",
            inline=False,
        )
    return embed


@bot.tree.command(name="leaderboard", description="Show current standings")
async def leaderboard_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_leaderboard_embed())


@bot.tree.command(name="reset", description="Announce the winner and reset all scores (moderators/owner only)")
async def reset_cmd(interaction: discord.Interaction):
    is_owner = interaction.guild and interaction.guild.owner_id == interaction.user.id
    is_mod = interaction.permissions.manage_guild or interaction.permissions.administrator
    if not (is_owner or is_mod):
        await interaction.response.send_message("Only moderators or the server owner can reset scores.", ephemeral=True)
        return
    rows = all_entries()
    ranked = total_scores(rows)
    if not ranked:
        await interaction.response.send_message("No scores to reset.")
        return
    winner = ranked[0]
    clear_all_entries()
    embed = discord.Embed(
        title="🎉 Competition Reset!",
        description=f"**{winner['username']}** wins with **{winner['score']} pts**!\n\n"
                    f"All scores have been reset. Post screenshots to start earning again!",
        color=0xFDCB6E,
    )
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(ranked[:3]):
        prefix = medals[i]
        embed.add_field(
            name=f"{prefix}  {r['username']} — {r['score']} pts",
            value=f"logged {r['days_logged']} days · "
                  f"{r['active_minutes']} active min · "
                  f"{r['move_goals']} move goals",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
