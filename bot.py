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
import math
import base64
import logging
import sqlite3
import asyncio
import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands, tasks
from anthropic import Anthropic
from dotenv import load_dotenv

from zoneinfo import ZoneInfo
import scoring
from scoring import (
    total_scores, compute_met, oura_calorie_baseline,
    MET_THRESHOLD,
    POINTS_CALORIE_CLOSE, POINTS_CALORIE_ON_GOAL, POINTS_PROTEIN_GOAL,
    CALORIE_CLOSE_MIN, CALORIE_CLOSE_MAX, CALORIE_GRACE_OVER,
    POINTS_MOVE_GOAL, POINTS_OURA_GOAL,
    POINTS_PER_ACTIVE_MIN, POINTS_PER_OURA_ACTIVE_MIN, POINTS_PER_STEP,
    OURA_MIN_ACTIVE_MINUTES, POINTS_PER_OURA_OVERTIME_MIN,
    POINTS_FOOD_LOGGED, POINTS_FITNESS_LOGGED, POINTS_WEIGHT_LOGGED, POINTS_MISSED_ALL,
    POINTS_TREND_7D, POINTS_TREND_30D, POINTS_TREND_90D, POINTS_TREND_ALLTIME,
)

EST = ZoneInfo("America/New_York")
CST = ZoneInfo("America/Chicago")

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fitness-bot")

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TRACKING_CHANNEL_ID = int(os.environ["TRACKING_CHANNEL_ID"])

# Haiku is cheap and plenty for reading clear screenshots. Switch to a Sonnet
# model if your members post low-quality / cluttered images. Check the current
# model list + pricing at https://docs.claude.com/en/docs/about-claude/models
VISION_MODEL = "claude-haiku-4-5-20251001"

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
                user_id          TEXT NOT NULL,
                username         TEXT NOT NULL,
                day              TEXT NOT NULL,         -- YYYY-MM-DD
                logged_food      INTEGER DEFAULT 0,     -- posted a readable food-log screenshot
                calories_over    INTEGER DEFAULT 0,     -- how many calories over goal (0 if at/under)
                calories_under   INTEGER DEFAULT NULL,  -- how many calories under goal (0 if exactly at, NULL if over)
                protein_goal_met INTEGER DEFAULT 0,     -- 1 if daily protein goal was met
                active_minutes   INTEGER DEFAULT 0,     -- exercise minutes (Apple Fitness or Oura)
                active_calories  INTEGER DEFAULT 0,     -- active calories burned (used to compute MET)
                workout_calories INTEGER DEFAULT NULL,  -- calories burned in workout session (for MET calc)
                workout_minutes  INTEGER DEFAULT NULL,  -- duration of workout session (for MET calc)
                steps            INTEGER DEFAULT 0,     -- step count (0.001 pt per step, no cap)
                weight_kg        REAL    DEFAULT NULL,  -- user's weight at time of entry (for MET calc)
                fitness_logged   INTEGER DEFAULT 0,     -- 1 if a fitness screenshot (Apple/Oura) was confirmed
                weight_logged    INTEGER DEFAULT 0,     -- 1 if a Happy Scale screenshot was confirmed
                move_goal_met    INTEGER DEFAULT 0,     -- Apple Fitness move ring closed
                all_rings_closed INTEGER DEFAULT 0,     -- 1 if all 3 Apple rings closed
                oura_goal_met    INTEGER DEFAULT 0,     -- 1 if Oura daily activity goal met
                trend_7d         INTEGER DEFAULT 0,     -- 1 if Happy Scale shows loss in last 7 days
                trend_30d        INTEGER DEFAULT 0,     -- 1 if Happy Scale shows loss in last 30 days
                trend_90d        INTEGER DEFAULT 0,     -- 1 if Happy Scale shows loss in last 90 days
                trend_alltime    INTEGER DEFAULT 0,     -- 1 if Happy Scale shows loss since start
                late_penalty     INTEGER DEFAULT 0,     -- 1 if no food log by midnight EST
                PRIMARY KEY (user_id, day)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS point_adjustments (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  TEXT NOT NULL,
                username TEXT NOT NULL,
                points   INTEGER NOT NULL,
                reason   TEXT NOT NULL,
                created  TEXT NOT NULL          -- ISO timestamp
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id      TEXT PRIMARY KEY,
                username     TEXT NOT NULL,
                tracker_type TEXT DEFAULT 'unknown',  -- 'apple_fitness', 'oura', 'unknown'
                weight_kg    REAL DEFAULT NULL         -- current weight in kg (set via /setweight or Happy Scale)
            )
            """
        )
        # Migrate older databases missing new columns
        for col, typedef in [
            ("late_penalty", "INTEGER DEFAULT 0"),
            ("over_calories", "INTEGER DEFAULT 0"),
            ("all_rings_closed", "INTEGER DEFAULT 0"),
            ("calories_over", "INTEGER DEFAULT 0"),
            ("oura_goal_met", "INTEGER DEFAULT 0"),
            ("step_goal_met", "INTEGER DEFAULT 0"),
            ("calories_under", "INTEGER DEFAULT NULL"),
            ("protein_goal_met", "INTEGER DEFAULT 0"),
            ("active_calories", "INTEGER DEFAULT 0"),
            ("workout_calories", "INTEGER DEFAULT NULL"),
            ("workout_minutes",  "INTEGER DEFAULT NULL"),
            ("steps", "INTEGER DEFAULT 0"),
            ("weight_kg", "REAL DEFAULT NULL"),
            ("fitness_logged", "INTEGER DEFAULT 0"),
            ("weight_logged", "INTEGER DEFAULT 0"),
            ("trend_7d", "INTEGER DEFAULT 0"),
            ("trend_30d", "INTEGER DEFAULT 0"),
            ("trend_90d", "INTEGER DEFAULT 0"),
            ("trend_alltime", "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE entries ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass


def upsert_entry(user_id, username, day, *, logged_food=None, calories_over=None,
                 calories_under=None, protein_goal_met=None,
                 active_minutes=None, active_calories=None, steps=None, weight_kg=None,
                 fitness_logged=None, weight_logged=None,
                 move_goal_met=None, oura_goal_met=None,
                 workout_calories=None, workout_minutes=None,
                 trend_7d=None, trend_30d=None, trend_90d=None, trend_alltime=None,
                 late_penalty=None):
    """Merge new fields into a user's row for the day without clobbering the rest."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM entries WHERE user_id=? AND day=?", (user_id, day)
        ).fetchone()
        base = dict(row) if row else {
            "user_id": user_id, "username": username, "day": day,
            "logged_food": 0, "calories_over": 0, "calories_under": None,
            "protein_goal_met": 0, "active_minutes": 0,
            "active_calories": 0, "workout_calories": None, "workout_minutes": None,
            "steps": 0, "weight_kg": None,
            "fitness_logged": 0, "weight_logged": 0,
            "move_goal_met": 0, "oura_goal_met": 0,
            "trend_7d": 0, "trend_30d": 0, "trend_90d": 0, "trend_alltime": 0,
            "late_penalty": 0,
        }
        if logged_food is not None:
            base["logged_food"] = int(logged_food)
        if calories_over is not None:
            base["calories_over"] = max(int(calories_over), 0)
        if calories_under is not None:
            base["calories_under"] = max(int(calories_under), 0)
        if protein_goal_met is not None:
            base["protein_goal_met"] = int(protein_goal_met)
        if active_minutes is not None:
            base["active_minutes"] = max(int(base.get("active_minutes", 0)), int(active_minutes))
        if active_calories is not None:
            base["active_calories"] = max(int(base.get("active_calories", 0)), int(active_calories))
        if workout_calories is not None:
            base["workout_calories"] = max(int(base.get("workout_calories") or 0), int(workout_calories))
        if workout_minutes is not None:
            base["workout_minutes"] = max(int(base.get("workout_minutes") or 0), int(workout_minutes))
        if steps is not None:
            base["steps"] = max(int(base.get("steps", 0)), int(steps))
        if weight_kg is not None:
            base["weight_kg"] = float(weight_kg)
        if fitness_logged is not None:
            base["fitness_logged"] = int(fitness_logged)
        if weight_logged is not None:
            base["weight_logged"] = int(weight_logged)
        if move_goal_met is not None:
            base["move_goal_met"] = int(move_goal_met)
        if oura_goal_met is not None:
            base["oura_goal_met"] = int(oura_goal_met)
        if trend_7d is not None:
            base["trend_7d"] = int(trend_7d)
        if trend_30d is not None:
            base["trend_30d"] = int(trend_30d)
        if trend_90d is not None:
            base["trend_90d"] = int(trend_90d)
        if trend_alltime is not None:
            base["trend_alltime"] = int(trend_alltime)
        if late_penalty is not None:
            base["late_penalty"] = int(late_penalty)
        base["username"] = username
        conn.execute(
            """
            INSERT INTO entries (user_id, username, day, logged_food, calories_over,
                calories_under, protein_goal_met,
                active_minutes, active_calories, workout_calories, workout_minutes,
                steps, weight_kg,
                fitness_logged, weight_logged,
                move_goal_met, oura_goal_met,
                trend_7d, trend_30d, trend_90d, trend_alltime,
                late_penalty)
            VALUES (:user_id, :username, :day, :logged_food, :calories_over,
                :calories_under, :protein_goal_met,
                :active_minutes, :active_calories, :workout_calories, :workout_minutes,
                :steps, :weight_kg,
                :fitness_logged, :weight_logged,
                :move_goal_met, :oura_goal_met,
                :trend_7d, :trend_30d, :trend_90d, :trend_alltime,
                :late_penalty)
            ON CONFLICT(user_id, day) DO UPDATE SET
                username=excluded.username,
                logged_food=excluded.logged_food,
                calories_over=excluded.calories_over,
                calories_under=excluded.calories_under,
                protein_goal_met=excluded.protein_goal_met,
                active_minutes=excluded.active_minutes,
                active_calories=excluded.active_calories,
                workout_calories=excluded.workout_calories,
                workout_minutes=excluded.workout_minutes,
                steps=excluded.steps,
                weight_kg=excluded.weight_kg,
                fitness_logged=excluded.fitness_logged,
                weight_logged=excluded.weight_logged,
                move_goal_met=excluded.move_goal_met,
                oura_goal_met=excluded.oura_goal_met,
                trend_7d=excluded.trend_7d,
                trend_30d=excluded.trend_30d,
                trend_90d=excluded.trend_90d,
                trend_alltime=excluded.trend_alltime,
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
        conn.execute("DELETE FROM point_adjustments")
        conn.execute("UPDATE user_prefs SET weight_kg = NULL")


def clear_user_entries(user_id: str):
    with db() as conn:
        conn.execute("DELETE FROM entries WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM point_adjustments WHERE user_id=?", (user_id,))
        conn.execute("UPDATE user_prefs SET weight_kg = NULL WHERE user_id=?", (user_id,))


def add_point_adjustment(user_id, username, points, reason):
    with db() as conn:
        conn.execute(
            "INSERT INTO point_adjustments (user_id, username, points, reason, created) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, points, reason, dt.datetime.now(EST).isoformat()),
        )


def get_adjustments_total(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(points), 0) AS total FROM point_adjustments WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return row["total"]


def all_adjustments():
    """Return dict of user_id -> total manual adjustment points."""
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, SUM(points) AS total FROM point_adjustments GROUP BY user_id"
        ).fetchall()
        return {r["user_id"]: r["total"] for r in rows}


def get_all_tracker_types():
    """Return dict of user_id -> tracker_type from user_prefs."""
    with db() as conn:
        rows = conn.execute("SELECT user_id, tracker_type FROM user_prefs").fetchall()
        return {r["user_id"]: r["tracker_type"] for r in rows}


def all_tracked_users():
    """Return a list of distinct (user_id, username) that have ever had an entry."""
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id, username FROM entries"
        ).fetchall()
        return [(r["user_id"], r["username"]) for r in rows]


def save_tracker_pref(user_id, username, tracker_type):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_prefs (user_id, username, tracker_type)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                tracker_type=excluded.tracker_type
            """,
            (user_id, username, tracker_type),
        )


def get_tracker_pref(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT tracker_type FROM user_prefs WHERE user_id=?", (user_id,)
        ).fetchone()
    return row["tracker_type"] if row else "unknown"


def save_weight(user_id, username, weight_kg):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_prefs (user_id, username, weight_kg)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                weight_kg=excluded.weight_kg
            """,
            (user_id, username, weight_kg),
        )


def get_weight(user_id):
    """Returns weight_kg float or None if not set."""
    with db() as conn:
        row = conn.execute(
            "SELECT weight_kg FROM user_prefs WHERE user_id=?", (user_id,)
        ).fetchone()
    return float(row["weight_kg"]) if row and row["weight_kg"] else None


# Pending tracker selections: message_id -> {user_id, username}
TRACKER_EMOJI_APPLE = "🍎"
TRACKER_EMOJI_OURA  = "💍"
pending_tracker = {}


async def ask_tracker(channel, user_id: str, username: str):
    """Post a tracker-selection message and pre-add the reaction options."""
    msg = await channel.send(
        f"Hey **{username}**! Which fitness tracker do you use?\n"
        f"React with {TRACKER_EMOJI_APPLE} for **Apple Fitness** or "
        f"{TRACKER_EMOJI_OURA} for **Oura Ring**.\n\n"
        f"Also, run **/setweight** with your current weight so I can calculate your activity points accurately!"
    )
    await msg.add_reaction(TRACKER_EMOJI_APPLE)
    await msg.add_reaction(TRACKER_EMOJI_OURA)
    pending_tracker[msg.id] = {"user_id": user_id, "username": username}


# --------------------------------------------------------------------------- #
# Vision: read a screenshot -> structured data
# --------------------------------------------------------------------------- #
VISION_PROMPT = """You are reading a screenshot from a fitness or food-logging app.
Supported apps: food loggers (MyFitnessPal, Lose It!, Cronometer, etc.),
Apple Fitness / Activity rings, Oura Ring, and Happy Scale (weight trend app).

Return ONLY a JSON object, no prose, no markdown fences, with these keys:
{
  "kind": "food_log" | "apple_fitness" | "oura" | "happy_scale" | "unknown",
  "logged_food": true | false,             // true if this is a readable food-log screen showing entries were logged
  "calories_over": integer | null,         // calories OVER daily goal (e.g. goal 2000, eaten 2350 -> 350). 0 if at or under goal. null if not visible
  "calories_under": integer | null,        // calories UNDER daily goal (e.g. goal 2000, eaten 1960 -> 40). 0 if exactly at goal. null if over goal or not visible
  "protein_goal_met": true | false | null, // true if the daily protein goal is shown as met/reached in the food log
  "active_minutes": integer | null,        // ACTUAL minutes exercised today — from Apple Fitness or Oura ONLY. Apple Fitness: the number shown ON or below the green Exercise ring (e.g. "11 MIN" means 11, not the goal "30 MIN"). Oura: the "Active Minutes" field. Do NOT read this from food logging apps (Lose It!, MyFitnessPal, Cronometer, etc.) — ignore any Exercise or activity fields in those apps. Do NOT use Move calories, Stand hours, or any goal value. null if not visible or if this is a food log screenshot.
  "active_calories": integer | null,       // active calories BURNED today (actual, not goal) — from Apple Fitness or Oura ONLY. Apple Fitness: the red Move ring number (e.g. "350 CAL"). Oura: active calories burned. Do NOT read from food logging apps. Do NOT use the goal. null if not visible or if this is a food log screenshot.
  "workout_calories": integer | null,  // Apple Fitness: calories burned in a specific workout session (from the workout summary detail screen, NOT the daily Move ring total). Oura: calories for a specific activity (from the Activities section). null if this is not a workout detail screen or not visible.
  "workout_minutes": integer | null,   // Apple Fitness: duration of a specific workout session in minutes (from workout summary). Oura: duration of a specific activity in minutes. null if not a workout detail screen or not visible.
  "steps": integer | null,                 // total step count for today if visible (from any app), else null
  "move_goal_met": true | false | null,    // Apple Fitness only: true if the red Move ring is fully closed
  "oura_goal_met": true | false | null,    // Oura only: true if the daily activity goal is fully reached (100% or more)
  "trend_7d": true | false | null,         // Happy Scale only: true if "Lost in 7 days" is green (showing a loss)
  "trend_30d": true | false | null,        // Happy Scale only: true if "Lost in 30 days" is green
  "trend_90d": true | false | null,        // Happy Scale only: true if "Lost in 90 days" is green
  "trend_alltime": true | false | null,    // Happy Scale only: true if "Lost all time" is green
  "weight_lbs": number | null,             // Happy Scale only: the TODAY'S trend weight (the number labeled "today" or shown as the current day's point on the graph), in lbs. Do NOT use any Milestone weight, goal weight, or target weight — only the current day's actual weight reading.
  "readable": true | false                 // false if the image is too blurry/cropped to interpret
}
If a value isn't present in the image, use null. Do not guess."""


def _detect_media_type(data: bytes) -> str:
    """Detect actual image type from file header bytes."""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if data[:2] == b'\xff\xd8':
        return "image/jpeg"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return "image/gif"
    return "image/png"


def image_block_from_bytes(data: bytes, content_type: str):
    media_type = _detect_media_type(data)
    log.info("Encoding image: %d bytes, detected=%s, discord=%s", len(data), media_type, content_type)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("utf-8"),
        },
    }


def parse_screenshot(data: bytes, content_type: str, tracker_hint: str = "unknown") -> dict:
    if tracker_hint == "apple_fitness":
        hint = "This user tracks fitness with Apple Fitness. Expect Activity ring screenshots.\n"
    elif tracker_hint == "oura":
        hint = "This user tracks fitness with Oura Ring. Expect Oura app screenshots.\n"
    else:
        hint = ""
    resp = client.messages.create(
        model=VISION_MODEL,
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": [
                image_block_from_bytes(data, content_type),
                {"type": "text", "text": hint + VISION_PROMPT},
            ],
        }],
    )
    logging.info("Token usage — input: %d, output: %d", resp.usage.input_tokens, resp.usage.output_tokens)
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"kind": "unknown", "readable": False}


# Pending confirmations: message_id -> dict(parsed payload + author)
pending = {}

# Deduplication: track message IDs already being processed to prevent double-parse
_processing = set()


def _fmt_val(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _format_parsed(p, day, weight_kg=None):
    fields = [
        ("logged_food",      p.get("logged_food")),
        ("calories_over",    p.get("calories_over")),
        ("calories_under",   p.get("calories_under")),
        ("protein_goal_met", p.get("protein_goal_met")),
        ("active_minutes",   p.get("active_minutes")),
        ("active_calories",  p.get("active_calories")),
        ("workout_calories", p.get("workout_calories")),
        ("workout_minutes",  p.get("workout_minutes")),
        ("steps",            p.get("steps")),
        ("move_goal_met",    p.get("move_goal_met")),
        ("oura_goal_met",    p.get("oura_goal_met")),
        ("fitness_logged",   p.get("fitness_logged") or None),
        ("weight_logged",    p.get("weight_logged") or None),
        ("weight_lbs",       p.get("weight_lbs")),
        ("trend_7d",         p.get("trend_7d")),
        ("trend_30d",        p.get("trend_30d")),
        ("trend_90d",        p.get("trend_90d")),
        ("trend_alltime",    p.get("trend_alltime")),
    ]
    body = "\n".join(f"{k}: {_fmt_val(v)}" for k, v in fields if v is not None)

    # Calculated section — tracker-specific
    cal_lines = []

    if p.get("oura_goal_met") is not None and p.get("oura_goal_met"):
        # Oura Ring scoring path
        active_mins = int(p.get("active_minutes") or 0)
        active_cal  = p.get("active_calories")
        if weight_kg:
            baseline = oura_calorie_baseline(active_mins, weight_kg)
            if baseline is not None:
                cal_lines.append(f"oura_cal_baseline: {baseline:.0f} active cal in {active_mins} min")
            if active_cal is not None and baseline is not None:
                cal_lines.append(
                    f"active_cal_vs_baseline: {active_cal} vs {baseline:.0f} "
                    f"({'qualifies ✅' if int(active_cal) > baseline else 'below baseline ❌'})"
                )
            cal_lines.append(
                f"active_minutes_requirement: {active_mins} / {OURA_MIN_ACTIVE_MINUTES} min "
                f"({'met ✅' if active_mins >= OURA_MIN_ACTIVE_MINUTES else f'need >= {OURA_MIN_ACTIVE_MINUTES} ❌'})"
            )
            if active_cal is not None and baseline is not None:
                cal_ok = int(active_cal) > baseline
                min_ok = active_mins >= OURA_MIN_ACTIVE_MINUTES
                if cal_ok and min_ok:
                    overtime     = active_mins - OURA_MIN_ACTIVE_MINUTES
                    overtime_pts = round(overtime * POINTS_PER_OURA_OVERTIME_MIN, 3)
                    if overtime > 0:
                        cal_lines.append(
                            f"oura_goal_pts: {POINTS_OURA_GOAL} base "
                            f"+ {overtime_pts} overtime ({overtime} min × {POINTS_PER_OURA_OVERTIME_MIN}) ✅"
                        )
                    else:
                        cal_lines.append(f"oura_goal_pts: {POINTS_OURA_GOAL} (qualified — exactly 30 min) ✅")
                else:
                    reasons = []
                    if not cal_ok and baseline is not None:
                        reasons.append(f"{active_cal} cal ≤ {baseline:.0f} baseline")
                    if not min_ok:
                        reasons.append(f"{active_mins} min < {OURA_MIN_ACTIVE_MINUTES}")
                    cal_lines.append(f"oura_goal_pts: 0 (didn't qualify — {'; '.join(reasons)}) ❌")
        else:
            cal_lines.append("oura_scoring: weight not set — run /setweight (fallback: flat 10 pts)")

    elif p.get("move_goal_met"):
        # Apple Fitness scoring path
        workout_cal  = p.get("workout_calories")
        workout_mins = int(p.get("workout_minutes") or 0)
        if weight_kg:
            ref_mins = workout_mins if workout_mins > 0 else (int(p.get("active_minutes") or 0) or 60)
            baseline_cal = round(MET_THRESHOLD * ref_mins * MET_THRESHOLD * weight_kg / 200)
            cal_lines.append(f"cal_goal_to_qualify: {baseline_cal} active cal in {ref_mins} min")
        if workout_cal and workout_mins:
            if not weight_kg:
                cal_lines.append("met: unknown (run /setweight first)")
            else:
                met = compute_met(workout_cal, workout_mins, weight_kg)
                qualifies = met > MET_THRESHOLD
                cal_lines.append(f"met: {met:.2f} ({'qualifies ✅' if qualifies else f'need > {MET_THRESHOLD} ❌'})")
                if qualifies:
                    goal_pts = round(POINTS_MOVE_GOAL * (1 + met / 100), 3)
                    cal_lines.append(f"move_goal_pts: {goal_pts} (base {POINTS_MOVE_GOAL} + MET bonus)")
                else:
                    cal_lines.append(f"move_goal_pts: {POINTS_MOVE_GOAL} (flat, MET too low for bonus)")
        else:
            cal_lines.append(f"move_goal_pts: {POINTS_MOVE_GOAL} (flat — post workout details for MET bonus)")
            cal_lines.append("⚠️ Post your workout summary to earn the MET intensity bonus!")

    calc_block = ("\n\n── calculated (not editable) ──\n" + "\n".join(cal_lines)) if cal_lines else ""

    return (
        f"📋 **Parsed for {day}** — React ✅ to confirm, ❌ to discard, "
        f"or **reply** with any corrections (e.g. `active_minutes: 30`).\n"
        f"```\n{body}{calc_block}\n```"
    )


def _parse_corrections(text: str) -> dict:
    """Parse 'key: value' lines from a reply into a payload patch."""
    valid_keys = {
        "logged_food", "calories_over", "calories_under", "protein_goal_met",
        "active_minutes", "active_calories", "steps", "move_goal_met",
        "oura_goal_met", "fitness_logged", "weight_logged", "weight_lbs",
        "workout_calories", "workout_minutes",
        "trend_7d", "trend_30d", "trend_90d", "trend_alltime",
    }
    patch = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()
        if key not in valid_keys:
            continue
        if raw.lower() == "null":
            patch[key] = None
        elif raw.lower() == "true":
            patch[key] = True
        elif raw.lower() == "false":
            patch[key] = False
        else:
            try:
                patch[key] = int(raw)
            except ValueError:
                try:
                    patch[key] = float(raw)
                except ValueError:
                    pass
    return patch


async def _save_confirmed(item: dict, msg: discord.Message):
    """Save a confirmed entry and edit msg with the score breakdown."""
    p = item["payload"]
    if p.get("weight_lbs"):
        weight_kg = p["weight_lbs"] / 2.20462
        save_weight(item["user_id"], item["username"], weight_kg)
    else:
        weight_kg = get_weight(item["user_id"])

    upsert_entry(
        item["user_id"], item["username"], item["day"],
        logged_food=p["logged_food"],
        calories_over=p["calories_over"],
        calories_under=p["calories_under"],
        protein_goal_met=p["protein_goal_met"],
        active_minutes=p["active_minutes"],
        active_calories=p["active_calories"],
        steps=p["steps"],
        weight_kg=weight_kg,
        fitness_logged=bool(p.get("fitness_logged")),
        weight_logged=bool(p.get("weight_logged")),
        move_goal_met=p["move_goal_met"],
        oura_goal_met=p["oura_goal_met"],
        workout_calories=p.get("workout_calories"),
        workout_minutes=p.get("workout_minutes"),
        trend_7d=p["trend_7d"],
        trend_30d=p["trend_30d"],
        trend_90d=p["trend_90d"],
        trend_alltime=p["trend_alltime"],
    )
    with db() as conn:
        saved = conn.execute(
            "SELECT * FROM entries WHERE user_id=? AND day=?",
            (item["user_id"], item["day"]),
        ).fetchone()
    day_food    = bool(saved and saved["logged_food"])
    day_fitness = bool(saved and saved["fitness_logged"])
    day_weight  = bool(saved and saved["weight_logged"])

    rows = all_entries()
    ranked = total_scores(rows, all_adjustments(), get_all_tracker_types())
    user_row   = next((r for r in ranked if r["username"] == item["username"]), {})
    user_total = user_row.get("score", 0)
    cur_streak = user_row.get("streak", 0)
    sb         = user_row.get("streak_bonus", 0)

    lines = []
    s = saved  # use full merged state for all breakdown calculations
    cal_over  = int(s["calories_over"] or 0)
    cal_under = s["calories_under"]
    if cal_over > CALORIE_GRACE_OVER:
        lines.append(f"🔴 {cal_over} cal over goal: **{round(cal_over * -0.1, 3)} pts**")
    elif cal_over > 0:
        lines.append(f"🟡 {cal_over} cal over goal (grace zone): **+{POINTS_CALORIE_ON_GOAL} pts**")
    elif cal_under is not None:
        under = int(cal_under)
        if under < CALORIE_CLOSE_MIN:
            lbl = "Exactly on calorie goal" if under == 0 else f"{under} cal under goal"
            lines.append(f"🎯 {lbl}: **+{POINTS_CALORIE_ON_GOAL} pts**")
        elif under <= CALORIE_CLOSE_MAX:
            lines.append(f"✅ {under} cal under goal: **+{POINTS_CALORIE_CLOSE} pt**")
    if s["protein_goal_met"]:
        lines.append(f"💪 Protein goal met: **+{POINTS_PROTEIN_GOAL} pts**")
    if s["move_goal_met"]:
        lbl = "Move ring 🔴"
        if s["workout_calories"] and s["workout_minutes"]:
            met = compute_met(s["workout_calories"], s["workout_minutes"], weight_kg)
            if met > MET_THRESHOLD:
                goal_pts = round(POINTS_MOVE_GOAL * (1 + met / 100), 3)
                lines.append(f"{lbl} (MET {met:.1f}): **+{goal_pts} pts**")
            else:
                lines.append(f"{lbl}: **+{POINTS_MOVE_GOAL} pts** (MET {met:.1f} — below threshold, no intensity bonus)")
        else:
            lines.append(f"{lbl}: **+{POINTS_MOVE_GOAL} pts** _(post workout details next time for MET bonus)_")
    if s["oura_goal_met"]:
        lbl = "Oura goal 💍"
        active_mins = int(s["active_minutes"] or 0)
        active_cal  = s["active_calories"]
        baseline    = oura_calorie_baseline(active_mins, weight_kg)
        if baseline is None or not active_cal:
            lines.append(f"{lbl}: **+{POINTS_OURA_GOAL} pts** _(missing data — set /setweight for full scoring)_")
        else:
            cal_ok = int(active_cal) > baseline
            min_ok = active_mins >= OURA_MIN_ACTIVE_MINUTES
            if cal_ok and min_ok:
                overtime     = active_mins - OURA_MIN_ACTIVE_MINUTES
                overtime_pts = round(overtime * POINTS_PER_OURA_OVERTIME_MIN, 3)
                total_goal   = POINTS_OURA_GOAL + overtime_pts
                if overtime > 0:
                    lines.append(
                        f"{lbl}: **+{total_goal} pts** "
                        f"({POINTS_OURA_GOAL} base + {overtime_pts} overtime — {overtime} min × {POINTS_PER_OURA_OVERTIME_MIN}) ✅"
                    )
                else:
                    lines.append(f"{lbl}: **+{POINTS_OURA_GOAL} pts** (qualified — exactly 30 min) ✅")
            else:
                reasons = []
                if not cal_ok:
                    reasons.append(f"{active_cal} cal ≤ {baseline:.0f} baseline")
                if not min_ok:
                    reasons.append(f"{active_mins} min < {OURA_MIN_ACTIVE_MINUTES}")
                lines.append(f"{lbl}: **+0 pts** (didn't qualify — {'; '.join(reasons)}) ❌")
    if s["active_minutes"]:
        mins = int(s["active_minutes"])
        is_oura = get_tracker_pref(item["user_id"]) == "oura"
        min_rate = POINTS_PER_OURA_ACTIVE_MIN if is_oura else POINTS_PER_ACTIVE_MIN
        lines.append(f"⏱️ {mins} active min: **+{round(mins * min_rate, 3)} pts** ({min_rate}/min)")
    if s["steps"]:
        step_pts = round(int(s["steps"]) * POINTS_PER_STEP, 3)
        lines.append(f"👟 {int(s['steps']):,} steps: **+{step_pts} pts**")
    for key, lbl, pts in (
        ("trend_7d",      "Lost in 7 days",   POINTS_TREND_7D),
        ("trend_30d",     "Lost in 30 days",  POINTS_TREND_30D),
        ("trend_90d",     "Lost in 90 days",  POINTS_TREND_90D),
        ("trend_alltime", "Lost all time",     POINTS_TREND_ALLTIME),
    ):
        if s[key]:
            lines.append(f"📉 {lbl}: **+{pts} pts**")
    if day_food:
        lines.append(f"🍽️ Food log posted: **+{POINTS_FOOD_LOGGED} pts**")
    if day_fitness:
        lines.append(f"🏃 Fitness posted: **+{POINTS_FITNESS_LOGGED} pts**")
    if day_weight:
        lines.append(f"⚖️ Weight posted: **+{POINTS_WEIGHT_LOGGED} pts**")
    if not day_food and not day_fitness and not day_weight:
        lines.append(f"❌ Nothing posted today: **{POINTS_MISSED_ALL} pts**")
    if sb:
        lines.append(f"🔥 {cur_streak}-day streak bonus: **+{sb} pts** (1 + {cur_streak})")

    breakdown = "\n".join(lines) if lines else "_No individual bonuses this entry._"
    await msg.edit(content=(
        f"✅ **Saved for {item['day']}**\n\n"
        f"{breakdown}\n\n"
        f"**Total: {user_total} pts**"
    ))


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
@tasks.loop(time=dt.time(0, 0, tzinfo=CST))
async def midnight_check():
    """Post a completion report at 12:00am CST for the day that just ended."""
    yesterday = (dt.datetime.now(CST) - dt.timedelta(days=1)).date().isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT u.user_id, u.username,
                   e.logged_food, e.fitness_logged, e.weight_logged
            FROM user_prefs u
            LEFT JOIN entries e ON u.user_id = e.user_id AND e.day = ?
            """,
            (yesterday,),
        ).fetchall()

    if not rows:
        return

    incomplete = []
    for row in rows:
        missing = []
        if not row["logged_food"]:
            missing.append("food log")
        if not row["fitness_logged"]:
            missing.append("fitness")
        if not row["weight_logged"]:
            missing.append("weight")
        if missing:
            incomplete.append(f"<@{row['user_id']}> — missing: {', '.join(missing)}")

    channel = bot.get_channel(TRACKING_CHANNEL_ID)
    if channel is None:
        return
    if incomplete:
        lines = "\n".join(incomplete)
        await channel.send(f"🌙 **Midnight check ({yesterday})** — incomplete submissions:\n{lines}")
    else:
        await channel.send(f"✅ All submissions complete for {yesterday}!")


@bot.event
async def on_ready():
    init_db()
    if not midnight_check.is_running():
        midnight_check.start()
    try:
        await bot.tree.sync()
    except Exception as e:
        log.error("Slash sync failed: %s", e)
    log.info("Logged in as %s", bot.user)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.channel.id != TRACKING_CHANNEL_ID:
        await bot.process_commands(message)
        return

    # Reply with corrections to a pending confirmation
    if (message.reference and
            message.reference.message_id in pending and
            str(message.author.id) == pending[message.reference.message_id]["user_id"]):
        ref_id = message.reference.message_id
        item   = pending[ref_id]
        patch  = _parse_corrections(message.content)
        if patch:
            item["payload"].update(patch)
            pending.pop(ref_id, None)
            channel = bot.get_channel(message.channel.id)
            try:
                orig_msg = await channel.fetch_message(ref_id)
            except discord.NotFound:
                orig_msg = None
            if orig_msg:
                await _save_confirmed(item, orig_msg)
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
            ranked = total_scores(all_entries(), all_adjustments(), get_all_tracker_types())
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
            await ask_tracker(message.channel, user_id, username)
        await bot.process_commands(message)
        return

    images = [a for a in message.attachments
              if (a.content_type or "").startswith("image/")]
    if not images:
        await bot.process_commands(message)
        return

    if message.id in _processing:
        return
    _processing.add(message.id)

    # Require weight to be set before parsing — DM the user privately so weight stays off the channel
    if not get_weight(str(message.author.id)):
        _processing.discard(message.id)
        try:
            await message.author.send(
                f"Hey **{message.author.display_name}**! Before I can score your screenshots I need your weight "
                f"to calculate activity intensity (MET).\n\n"
                f"Run this command in the server:\n"
                f"```\n/setweight weight_lbs: 165.5\n```\n"
                f"Replace **165.5** with your actual weight in lbs, then repost your screenshot."
            )
        except discord.Forbidden:
            # DMs disabled — fall back to a brief channel nudge without revealing weight info
            await message.reply(
                "Please run **/setweight** before posting screenshots so I can calculate your activity points.",
                mention_author=True,
            )
        await message.add_reaction("⚠️")
        return

    day = dt.date.today().isoformat()
    tracker = get_tracker_pref(str(message.author.id))
    results = []
    for att in images:
        raw = await att.read()
        try:
            logging.info(f"Parsing image from {message.author.display_name} ({att.filename}, {len(raw)} bytes)")
            parsed = parse_screenshot(raw, att.content_type or "image/png", tracker_hint=tracker)
        except Exception as e:
            log.error("Vision API error for %s: %s: %s", att.filename, type(e).__name__, e)
            await message.reply(
                "Something went wrong reading that image — try again in a moment.",
                mention_author=False,
            )
            return
        results.append(parsed)

    # Merge what we read across however many images they posted.
    payload = {
        "logged_food": None, "calories_over": None, "calories_under": None,
        "protein_goal_met": None,
        "active_minutes": None, "active_calories": None, "steps": None,
        "workout_calories": None, "workout_minutes": None,
        "move_goal_met": None, "oura_goal_met": None,
        "trend_7d": None, "trend_30d": None, "trend_90d": None, "trend_alltime": None,
        "weight_lbs": None,
        "fitness_logged": False,
        "weight_logged": False,
    }
    readable_any = False
    for p in results:
        if not p.get("readable", False):
            continue
        readable_any = True
        if p.get("kind") == "food_log" and p.get("logged_food"):
            payload["logged_food"] = True
        if p.get("calories_over") is not None:
            payload["calories_over"] = max(int(p["calories_over"]), 0)
        if p.get("calories_under") is not None:
            payload["calories_under"] = max(int(p["calories_under"]), 0)
        if p.get("protein_goal_met") is not None:
            payload["protein_goal_met"] = bool(p["protein_goal_met"])
        if p.get("active_minutes") is not None:
            payload["active_minutes"] = max(
                payload["active_minutes"] or 0, int(p["active_minutes"])
            )
        if p.get("active_calories") is not None:
            payload["active_calories"] = max(
                payload["active_calories"] or 0, int(p["active_calories"])
            )
        if p.get("workout_calories") is not None:
            payload["workout_calories"] = max(payload["workout_calories"] or 0, int(p["workout_calories"]))
        if p.get("workout_minutes") is not None:
            payload["workout_minutes"] = max(payload["workout_minutes"] or 0, int(p["workout_minutes"]))
        if p.get("steps") is not None:
            payload["steps"] = max(
                payload["steps"] or 0, int(p["steps"])
            )
        if p.get("move_goal_met") is not None:
            payload["move_goal_met"] = bool(p["move_goal_met"])
        if p.get("oura_goal_met") is not None:
            payload["oura_goal_met"] = bool(p["oura_goal_met"])
        for trend_key in ("trend_7d", "trend_30d", "trend_90d", "trend_alltime"):
            if p.get(trend_key) is not None:
                payload[trend_key] = bool(p[trend_key])
        if p.get("weight_lbs") is not None:
            payload["weight_lbs"] = float(p["weight_lbs"])
        if p.get("kind") in ("apple_fitness", "oura"):
            payload["fitness_logged"] = True
        if p.get("kind") == "happy_scale":
            payload["weight_logged"] = True

    if not readable_any:
        await message.reply(
            "I couldn't read that clearly — try a fuller, less cropped screenshot.",
            mention_author=False,
        )
        return

    summary = []
    if payload["logged_food"]:
        s_over  = int(payload["calories_over"] or 0)
        s_under = payload["calories_under"]
        if s_over > CALORIE_GRACE_OVER:
            summary.append(f"food logged ⚠️ {s_over} cal over goal ({round(s_over * -0.1, 3)} pts)")
        elif s_over > 0:
            summary.append(f"food logged 🟡 {s_over} cal over (grace zone +{POINTS_CALORIE_ON_GOAL} pts)")
        elif s_under is not None:
            under = int(s_under)
            if under == 0:
                summary.append(f"food logged ✅ exactly on goal (+{POINTS_CALORIE_ON_GOAL} pts)")
            elif under < CALORIE_CLOSE_MIN:
                summary.append(f"food logged ✅ {under} cal under goal (+{POINTS_CALORIE_ON_GOAL} pts)")
            elif under <= CALORIE_CLOSE_MAX:
                summary.append(f"food logged ✅ {under} cal under goal (+{POINTS_CALORIE_CLOSE} pt)")
            else:
                summary.append(f"food logged ✅ {under} cal under goal")
        else:
            summary.append("food logged ✅")
    if payload["protein_goal_met"]:
        summary.append("protein goal met 💪 (+3 pts)")
    if payload["active_minutes"]:
        summary.append(f"{payload['active_minutes']} active min")
    user_weight = get_weight(str(message.author.id))

    def _apple_goal_note(label, emoji):
        cal  = payload.get("workout_calories") or payload.get("active_calories") or 0
        mins = payload.get("workout_minutes") or payload.get("active_minutes") or 0
        if not cal:
            return f"{label} {emoji} ⚠️ workout calories not visible — MET can't be calculated"
        if not user_weight:
            return f"{label} {emoji} ({cal} cal — set weight with /setweight to qualify)"
        met = compute_met(cal, mins, user_weight)
        if met <= MET_THRESHOLD:
            return f"{label} {emoji} ❌ MET {met:.1f} ≤ {MET_THRESHOLD} — doesn't qualify for bonus"
        bonus = met / 100
        return f"{label} {emoji} MET {met:.1f} → +{bonus:.1%} bonus ✅"

    def _oura_goal_note(label, emoji):
        active_mins = int(payload.get("active_minutes") or 0)
        active_cal  = payload.get("active_calories")
        if active_cal is None:
            return f"{label} {emoji} ⚠️ active_calories not visible — can't check qualification"
        if not user_weight:
            return f"{label} {emoji} ({active_cal} cal — set weight with /setweight to qualify)"
        baseline = oura_calorie_baseline(active_mins, user_weight)
        if baseline is None:
            return f"{label} {emoji} ⚠️ can't compute baseline"
        cal_ok = int(active_cal) > baseline
        min_ok = active_mins >= OURA_MIN_ACTIVE_MINUTES
        if cal_ok and min_ok:
            overtime = active_mins - OURA_MIN_ACTIVE_MINUTES
            overtime_pts = round(overtime * POINTS_PER_OURA_OVERTIME_MIN, 3)
            note = f" +{overtime_pts} overtime pts" if overtime > 0 else ""
            return f"{label} {emoji} qualifies ✅ ({active_cal} cal > {baseline:.0f} baseline, {active_mins} min){note}"
        reasons = []
        if not cal_ok:
            reasons.append(f"{active_cal} cal ≤ {baseline:.0f}")
        if not min_ok:
            reasons.append(f"{active_mins} min < {OURA_MIN_ACTIVE_MINUTES}")
        return f"{label} {emoji} ❌ {'; '.join(reasons)}"

    if payload["move_goal_met"]:
        summary.append(_apple_goal_note("move goal closed", "🔴"))
    if payload["oura_goal_met"]:
        summary.append(_oura_goal_note("Oura goal met", "💍"))
    if payload["steps"]:
        step_pts = round(payload["steps"] * POINTS_PER_STEP, 3)
        summary.append(f"{payload['steps']:,} steps (+{step_pts} pts) 👟")
    trends_green = [
        label for key, label in (
            ("trend_7d",      "7d 🟢"),
            ("trend_30d",     "30d 🟢"),
            ("trend_90d",     "90d 🟢"),
            ("trend_alltime", "all time 🟢"),
        ) if payload.get(key)
    ]
    if payload["weight_logged"]:
        if trends_green:
            summary.append(f"Happy Scale posted ⚖️ (+{POINTS_WEIGHT_LOGGED} pts): lost in " + ", ".join(trends_green))
        else:
            summary.append(f"Happy Scale posted ⚖️ (+{POINTS_WEIGHT_LOGGED} pts)")
    summary_text = ", ".join(summary) if summary else "nothing trackable found"

    confirm = await message.reply(
        _format_parsed(payload, day, weight_kg=user_weight),
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
    emoji = str(payload.emoji)

    # Tracker selection
    if payload.message_id in pending_tracker:
        item = pending_tracker[payload.message_id]
        if str(payload.user_id) != item["user_id"]:
            return
        if emoji == TRACKER_EMOJI_APPLE:
            tracker_type, label = "apple_fitness", "Apple Fitness"
        elif emoji == TRACKER_EMOJI_OURA:
            tracker_type, label = "oura", "Oura Ring"
        else:
            return
        save_tracker_pref(item["user_id"], item["username"], tracker_type)
        pending_tracker.pop(payload.message_id, None)
        channel = bot.get_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        await msg.edit(content=f"{emoji} Got it, **{item['username']}**! I'll expect **{label}** screenshots from you.")
        return

    if payload.message_id not in pending:
        return
    item = pending[payload.message_id]
    if str(payload.user_id) != item["user_id"]:
        return  # only the poster confirms their own entry

    if emoji == "✅":
        pending.pop(payload.message_id, None)
        channel = bot.get_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        await _save_confirmed(item, msg)
    elif emoji == "❌":
        channel = bot.get_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        await msg.edit(content="❌ Discarded. Repost when ready.")
        pending.pop(payload.message_id, None)




# --------------------------------------------------------------------------- #
# Leaderboard
# --------------------------------------------------------------------------- #
def build_leaderboard_embed():
    rows = all_entries()
    ranked = total_scores(rows, all_adjustments(), get_all_tracker_types())
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
                  f"{r['qualified_goals']} move goals · streak {r['streak']}",
            inline=False,
        )
    return embed


@bot.tree.command(name="leaderboard", description="Show current standings")
async def leaderboard_cmd(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return
    await interaction.followup.send(embed=build_leaderboard_embed())


@bot.tree.command(name="setweight", description="Set your weight so the bot can calculate your MET for activity goals")
@app_commands.describe(weight_lbs="Your current weight in pounds (e.g. 165.5)")
async def setweight_cmd(interaction: discord.Interaction, weight_lbs: float):
    if weight_lbs <= 0:
        await interaction.response.send_message("Weight must be a positive number.", ephemeral=True)
        return
    user_id  = str(interaction.user.id)
    username = interaction.user.display_name
    weight_kg = weight_lbs / 2.20462
    save_weight(user_id, username, weight_kg)
    await interaction.response.send_message(
        f"Got it, **{username}**! Weight set to **{weight_lbs} lbs** ({weight_kg:.1f} kg).\n"
        f"Activity goal points will now be calculated using your MET.",
        ephemeral=True,
    )


# --------------------------------------------------------------------------- #
# Mod commands
# --------------------------------------------------------------------------- #

def _is_mod(interaction: discord.Interaction) -> bool:
    is_owner = interaction.guild and interaction.guild.owner_id == interaction.user.id
    is_mod = interaction.permissions.manage_guild or interaction.permissions.administrator
    return is_owner or is_mod


@bot.tree.command(name="reset", description="Announce the winner and reset all scores (moderators/owner only)")
async def reset_cmd(interaction: discord.Interaction):
    try:
        if not _is_mod(interaction):
            await interaction.response.send_message("Only moderators or the server owner can reset scores.", ephemeral=True)
            return
        await interaction.response.defer()
    except discord.errors.NotFound:
        return
    rows = all_entries()
    ranked = total_scores(rows, all_adjustments(), get_all_tracker_types())
    if not ranked:
        await interaction.followup.send("No scores to reset.")
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
                  f"{r['qualified_goals']} goals met",
            inline=False,
        )
    await interaction.followup.send(embed=embed)



@bot.tree.command(name="adduser", description="Add a user to the competition (moderators/owner only)")
@app_commands.describe(member="The user to add")
async def adduser_cmd(interaction: discord.Interaction, member: discord.Member):
    if not _is_mod(interaction):
        await interaction.response.send_message("Only moderators or the server owner can use this.", ephemeral=True)
        return
    user_id = str(member.id)
    username = member.display_name
    day = dt.date.today().isoformat()
    with db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM entries WHERE user_id=?", (user_id,)
        ).fetchone()
    if existing:
        await interaction.response.send_message(f"**{username}** is already on the board.", ephemeral=True)
        return
    upsert_entry(user_id, username, day, logged_food=0, active_minutes=0, move_goal_met=0)
    await interaction.response.send_message(f"**{username}** has been added to the competition!")
    await ask_tracker(interaction.channel, user_id, username)


@bot.tree.command(name="addpoints", description="Add points to a user (moderators/owner only)")
@app_commands.describe(member="The user to award points to", points="Number of points to add", reason="Reason for the adjustment")
async def addpoints_cmd(interaction: discord.Interaction, member: discord.Member, points: int, reason: str = "Manual adjustment"):
    if not _is_mod(interaction):
        await interaction.response.send_message("Only moderators or the server owner can use this.", ephemeral=True)
        return
    user_id = str(member.id)
    username = member.display_name
    # Ensure the user is on the board
    with db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM entries WHERE user_id=?", (user_id,)
        ).fetchone()
    if not existing:
        await interaction.response.send_message(f"**{username}** isn't on the board yet. Use `/adduser` first.", ephemeral=True)
        return
    await interaction.response.defer()
    add_point_adjustment(user_id, username, points, reason)
    rows = all_entries()
    ranked = total_scores(rows, all_adjustments(), get_all_tracker_types())
    user_total = next((r["score"] for r in ranked if r["username"] == username), 0)
    await interaction.followup.send(
        f"**+{points} pts** to **{username}** — {reason}\nNew total: **{user_total} pts**"
    )


@bot.tree.command(name="removepoints", description="Remove points from a user (moderators/owner only)")
@app_commands.describe(member="The user to deduct points from", points="Number of points to remove", reason="Reason for the deduction")
async def removepoints_cmd(interaction: discord.Interaction, member: discord.Member, points: int, reason: str = "Manual adjustment"):
    if not _is_mod(interaction):
        await interaction.response.send_message("Only moderators or the server owner can use this.", ephemeral=True)
        return
    user_id = str(member.id)
    username = member.display_name
    with db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM entries WHERE user_id=?", (user_id,)
        ).fetchone()
    if not existing:
        await interaction.response.send_message(f"**{username}** isn't on the board yet.", ephemeral=True)
        return
    await interaction.response.defer()
    add_point_adjustment(user_id, username, -abs(points), reason)
    rows = all_entries()
    ranked = total_scores(rows, all_adjustments(), get_all_tracker_types())
    user_total = next((r["score"] for r in ranked if r["username"] == username), 0)
    await interaction.followup.send(
        f"**-{abs(points)} pts** from **{username}** — {reason}\nNew total: **{user_total} pts**"
    )


@bot.tree.command(name="clearuser", description="Clear all stats for a user (moderators/owner only)")
@app_commands.describe(member="The user whose stats to clear")
async def clearuser_cmd(interaction: discord.Interaction, member: discord.Member):
    if not _is_mod(interaction):
        await interaction.response.send_message("Only moderators or the server owner can clear user stats.", ephemeral=True)
        return
    user_id  = str(member.id)
    username = member.display_name
    with db() as conn:
        has_data = conn.execute("SELECT 1 FROM entries WHERE user_id=?", (user_id,)).fetchone()
    if not has_data:
        await interaction.response.send_message(f"**{username}** has no stats to clear.", ephemeral=True)
        return
    clear_user_entries(user_id)
    await interaction.response.send_message(f"✅ Cleared all entries and point adjustments for **{username}**.")


# --------------------------------------------------------------------------- #
# /checkactivity — show personal calorie burn target to qualify for goal bonus
# --------------------------------------------------------------------------- #

@bot.tree.command(
    name="checkactivity",
    description="See how many active calories you need to burn to qualify for the activity goal bonus",
)
async def checkactivity_cmd(interaction: discord.Interaction):
    user_id   = str(interaction.user.id)
    weight_kg = get_weight(user_id)
    if not weight_kg:
        await interaction.response.send_message(
            "You haven't set your weight yet. Use `/setweight` to set it first.",
            ephemeral=True,
        )
        return

    weight_lbs = weight_kg * 2.20462
    # Minimum active calories needed in 60 min to reach MET 2.8:
    # MET = (cal × 200) / (60 × 2.8 × weight_kg)  →  cal = MET × 60 × 2.8 × weight_kg / 200
    target_cal = MET_THRESHOLD * 60 * MET_THRESHOLD * weight_kg / 200
    bonus_example = round(POINTS_MOVE_GOAL * (1 + MET_THRESHOLD / 100), 1)

    await interaction.response.send_message(
        f"**Your Activity Goal**\n"
        f"Weight: **{weight_lbs:.1f} lbs** ({weight_kg:.1f} kg)\n\n"
        f"Burn at least **{target_cal:.0f} active calories** in 60 minutes to qualify for the "
        f"**+{POINTS_MOVE_GOAL} pt activity goal bonus**.\n\n"
        f"At exactly MET {MET_THRESHOLD} you'd earn **+{bonus_example} pts**. "
        f"Higher intensity = higher MET = bigger bonus (e.g. MET 5.0 → +{round(POINTS_MOVE_GOAL * 1.05, 1)} pts).",
        ephemeral=True,
    )


# --------------------------------------------------------------------------- #
# /catchup — "how much exercise do I need to reach place X?"
# --------------------------------------------------------------------------- #

# (label, emoji, MET value)
EXERCISES = [
    ("Walking (strolling)",     "🚶", 2.0),
    ("Yoga (Hatha)",            "🧘", 2.5),
    ("Walking (brisk)",         "🚶", 3.5),
    ("Cycling (leisure)",       "🚲", 4.0),
    ("Dancing",                 "💃", 4.5),
    ("Hiking",                  "🥾", 5.3),
    ("Elliptical",              "🏃", 5.0),
    ("Rowing (moderate)",       "🚣", 4.8),
    ("Weight training",         "🏋️", 3.5),
    ("Basketball",              "🏀", 6.5),
    ("Soccer",                  "⚽", 7.0),
    ("Swimming (recreational)", "🏊", 6.0),
    ("Jogging",                 "🏃", 7.0),
    ("Boxing",                  "🥊", 7.8),
    ("Cycling (vigorous)",      "🚴", 8.0),
    ("HIIT",                    "🔥", 8.0),
    ("Swimming (laps)",         "🏊", 8.3),
    ("Running",                 "🏃", 9.8),
    ("Jump rope",               "🪢", 11.0),
]


def _ordinal(n: int) -> str:
    """Return '1st', '2nd', '3rd', '4th', etc."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _exercise_pts_per_session(met: float, duration: int = 60) -> float:
    """Points earned from one hypothetical 60-min session at the given MET."""
    goal_pts = POINTS_MOVE_GOAL * (1 + met / 100) if met >= MET_THRESHOLD else 0
    min_pts  = duration * POINTS_PER_ACTIVE_MIN
    return goal_pts + min_pts


@bot.tree.command(
    name="catchup",
    description="See how much exercise you'd need to reach a target leaderboard position",
)
@app_commands.describe(place="The leaderboard position you want to reach (e.g. 2 for 2nd place)")
async def catchup_cmd(interaction: discord.Interaction, place: int):
    await interaction.response.defer(ephemeral=True)
    rows   = all_entries()
    ranked = total_scores(rows, all_adjustments(), get_all_tracker_types())

    if not ranked:
        await interaction.followup.send("No scores on the board yet.")
        return

    if place < 1 or place > len(ranked):
        await interaction.followup.send(
            f"There {'is' if len(ranked) == 1 else 'are'} only **{len(ranked)}** "
            f"{'person' if len(ranked) == 1 else 'people'} on the board. "
            f"Pick a number between 1 and {len(ranked)}.",
        )
        return

    username = interaction.user.display_name
    user_entry = next((r for r in ranked if r["username"] == username), None)
    if user_entry is None:
        await interaction.followup.send(
            'You\'re not on the board yet. Post "oorah" in the tracking channel to join!',
        )
        return

    user_score = user_entry["score"]

    # Determine user's current rank (ties share the same rank number)
    user_rank = 1
    for i, r in enumerate(ranked):
        if i > 0 and r["score"] < ranked[i - 1]["score"]:
            user_rank = i + 1
        if r["username"] == username:
            break

    target_score = ranked[place - 1]["score"]
    target_name  = ranked[place - 1]["username"]

    # Already at or ahead of target?
    if user_rank <= place:
        if user_rank == place:
            await interaction.followup.send(
                f"You're already in **{_ordinal(user_rank)} place**! Keep it up 🏆",
            )
        else:
            await interaction.followup.send(
                f"You're already **{_ordinal(user_rank)}** — ahead of {_ordinal(place)} place. Keep it up 🏆",
            )
        return

    gap = target_score - user_score + 1  # +1 to actually pass them

    lines = [
        f"**{username}** — you're in **{_ordinal(user_rank)} place** with **{user_score} pts**.",
        f"To reach **{_ordinal(place)} place** you need to pass **{target_name}** ({target_score} pts).",
        f"Point gap: **{gap} pts**\n",
        "Here's how many 60-min sessions you'd need:\n",
    ]

    for label, emoji, met in EXERCISES:
        pts      = _exercise_pts_per_session(met)
        sessions = math.ceil(gap / pts)
        if met < MET_THRESHOLD:
            note = f"_(MET {met} — no activity goal bonus, ~{pts:.0f} pts/session)_"
        else:
            note = f"_(MET {met:.1f} → ~{pts:.0f} pts/session)_"
        lines.append(f"{emoji} **{label}**: {sessions} session{'s' if sessions != 1 else ''} {note}")

    await interaction.followup.send("\n".join(lines))


@bot.tree.command(name="state", description="View today's confirmed submission state for a user")
@app_commands.describe(member="The user to check (leave blank for yourself)")
async def state_cmd(interaction: discord.Interaction, member: discord.Member | None = None):
    target = member or interaction.user
    day = dt.datetime.now(CST).date().isoformat()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM entries WHERE user_id=? AND day=?",
            (str(target.id), day),
        ).fetchone()

    if not row:
        await interaction.response.send_message(
            f"No confirmed submissions for **{target.display_name}** today ({day}).",
            ephemeral=True,
        )
        return

    def fmt(val):
        if isinstance(val, int) and val in (0, 1):
            return "✅" if val else "❌"
        return str(val)

    FIELD_DEFAULTS = {
        "logged_food": 0, "calories_over": 0, "protein_goal_met": 0,
        "active_minutes": 0, "active_calories": 0, "steps": 0,
        "move_goal_met": 0, "oura_goal_met": 0,
        "fitness_logged": 0, "weight_logged": 0,
        "trend_7d": 0, "trend_30d": 0, "trend_90d": 0, "trend_alltime": 0,
    }

    weight_lbs = round(row["weight_kg"] * 2.20462, 1) if row["weight_kg"] else None

    display = {
        "logged_food":      row["logged_food"],
        "calories_over":    row["calories_over"],
        "calories_under":   row["calories_under"],
        "protein_goal_met": row["protein_goal_met"],
        "active_minutes":   row["active_minutes"],
        "active_calories":  row["active_calories"],
        "steps":            row["steps"],
        "move_goal_met":    row["move_goal_met"],
        "oura_goal_met":    row["oura_goal_met"],
        "fitness_logged":   row["fitness_logged"],
        "weight_logged":    row["weight_logged"],
        "weight_lbs":       weight_lbs,
        "trend_7d":         row["trend_7d"],
        "trend_30d":        row["trend_30d"],
        "trend_90d":        row["trend_90d"],
        "trend_alltime":    row["trend_alltime"],
    }

    lines = [f"📋 {target.display_name} — {day}\n"]
    for k, v in display.items():
        if v is None:
            continue
        if FIELD_DEFAULTS.get(k) == 0 and v == 0:
            continue
        lines.append(f"  {k:<22} {fmt(v)}")

    await interaction.response.send_message(f"```\n{chr(10).join(lines)}\n```", ephemeral=True)


@bot.tree.command(name="score", description="Show current score breakdown based on today's logged state")
@app_commands.describe(member="The user to check (leave blank for yourself)")
async def score_cmd(interaction: discord.Interaction, member: discord.Member | None = None):
    target = member or interaction.user
    day = dt.datetime.now(CST).date().isoformat()

    with db() as conn:
        saved = conn.execute(
            "SELECT * FROM entries WHERE user_id=? AND day=?",
            (str(target.id), day),
        ).fetchone()

    if not saved:
        await interaction.response.send_message(
            f"No submissions confirmed for **{target.display_name}** today ({day}).",
            ephemeral=True,
        )
        return

    weight_kg = get_weight(str(target.id))
    rows = all_entries()
    ranked = total_scores(rows, all_adjustments(), get_all_tracker_types())
    user_row   = next((r for r in ranked if r["username"] == target.display_name), {})
    user_total = user_row.get("score", 0)
    cur_streak = user_row.get("streak", 0)
    sb         = user_row.get("streak_bonus", 0)

    lines = []
    cal_over  = int(saved["calories_over"] or 0)
    cal_under = saved["calories_under"]
    if cal_over > CALORIE_GRACE_OVER:
        lines.append(f"🔴 {cal_over} cal over goal: **{round(cal_over * -0.1, 3)} pts**")
    elif cal_over > 0:
        lines.append(f"🟡 {cal_over} cal over goal (grace zone): **+{POINTS_CALORIE_ON_GOAL} pts**")
    elif cal_under is not None:
        under = int(cal_under)
        if under < CALORIE_CLOSE_MIN:
            lbl = "Exactly on calorie goal" if under == 0 else f"{under} cal under goal"
            lines.append(f"🎯 {lbl}: **+{POINTS_CALORIE_ON_GOAL} pts**")
        elif under <= CALORIE_CLOSE_MAX:
            lines.append(f"✅ {under} cal under goal: **+{POINTS_CALORIE_CLOSE} pt**")
    if saved["protein_goal_met"]:
        lines.append(f"💪 Protein goal met: **+{POINTS_PROTEIN_GOAL} pts**")
    if saved["move_goal_met"]:
        lbl = "Move ring 🔴"
        if saved["workout_calories"] and saved["workout_minutes"]:
            met = compute_met(saved["workout_calories"], saved["workout_minutes"], weight_kg)
            if met > MET_THRESHOLD:
                goal_pts = round(POINTS_MOVE_GOAL * (1 + met / 100), 3)
                lines.append(f"{lbl} (MET {met:.1f}): **+{goal_pts} pts**")
            else:
                lines.append(f"{lbl}: **+{POINTS_MOVE_GOAL} pts** (MET {met:.1f} — below threshold, no intensity bonus)")
        else:
            lines.append(f"{lbl}: **+{POINTS_MOVE_GOAL} pts** _(post workout details for MET bonus)_")
    if saved["oura_goal_met"]:
        lbl = "Oura goal 💍"
        active_mins = int(saved["active_minutes"] or 0)
        active_cal  = saved["active_calories"]
        baseline    = oura_calorie_baseline(active_mins, weight_kg)
        if baseline is None or not active_cal:
            lines.append(f"{lbl}: **+{POINTS_OURA_GOAL} pts** _(missing data — set /setweight for full scoring)_")
        else:
            cal_ok = int(active_cal) > baseline
            min_ok = active_mins >= OURA_MIN_ACTIVE_MINUTES
            if cal_ok and min_ok:
                overtime     = active_mins - OURA_MIN_ACTIVE_MINUTES
                overtime_pts = round(overtime * POINTS_PER_OURA_OVERTIME_MIN, 3)
                total_goal   = POINTS_OURA_GOAL + overtime_pts
                if overtime > 0:
                    lines.append(
                        f"{lbl}: **+{total_goal} pts** "
                        f"({POINTS_OURA_GOAL} base + {overtime_pts} overtime — {overtime} min × {POINTS_PER_OURA_OVERTIME_MIN}) ✅"
                    )
                else:
                    lines.append(f"{lbl}: **+{POINTS_OURA_GOAL} pts** (qualified — exactly 30 min) ✅")
            else:
                reasons = []
                if not cal_ok:
                    reasons.append(f"{active_cal} cal ≤ {baseline:.0f} baseline")
                if not min_ok:
                    reasons.append(f"{active_mins} min < {OURA_MIN_ACTIVE_MINUTES}")
                lines.append(f"{lbl}: **+0 pts** (didn't qualify — {'; '.join(reasons)}) ❌")
    if saved["active_minutes"]:
        mins = int(saved["active_minutes"])
        is_oura = get_tracker_pref(str(target.id)) == "oura"
        min_rate = POINTS_PER_OURA_ACTIVE_MIN if is_oura else POINTS_PER_ACTIVE_MIN
        lines.append(f"⏱️ {mins} active min: **+{round(mins * min_rate, 3)} pts** ({min_rate}/min)")
    if saved["steps"]:
        step_pts = round(int(saved["steps"]) * POINTS_PER_STEP, 3)
        lines.append(f"👟 {int(saved['steps']):,} steps: **+{step_pts} pts**")
    for key, lbl, pts in (
        ("trend_7d",      "Lost in 7 days",   POINTS_TREND_7D),
        ("trend_30d",     "Lost in 30 days",  POINTS_TREND_30D),
        ("trend_90d",     "Lost in 90 days",  POINTS_TREND_90D),
        ("trend_alltime", "Lost all time",     POINTS_TREND_ALLTIME),
    ):
        if saved[key]:
            lines.append(f"📉 {lbl}: **+{pts} pts**")
    if saved["logged_food"]:
        lines.append(f"🍽️ Food log posted: **+{POINTS_FOOD_LOGGED} pts**")
    if saved["fitness_logged"]:
        lines.append(f"🏃 Fitness posted: **+{POINTS_FITNESS_LOGGED} pts**")
    if saved["weight_logged"]:
        lines.append(f"⚖️ Weight posted: **+{POINTS_WEIGHT_LOGGED} pts**")
    if not saved["logged_food"] and not saved["fitness_logged"] and not saved["weight_logged"]:
        lines.append(f"❌ Nothing posted today: **{POINTS_MISSED_ALL} pts**")
    if sb:
        lines.append(f"🔥 {cur_streak}-day streak bonus: **+{sb} pts** (1 + {cur_streak})")

    breakdown = "\n".join(lines) if lines else "_No bonuses recorded yet._"
    await interaction.response.send_message(
        f"📊 **{target.display_name}** — {day}\n\n{breakdown}\n\n**Total: {user_total} pts**",
        ephemeral=True,
    )


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
