"""
Weekly scoring.

Design rationale (read before changing):
  This ranks people on behaviors they can control and sustain — showing up,
  logging, and moving — NOT on how few calories they ate or how much weight
  they lost. Ranking a group on calorie totals or weight change reliably
  pushes the group toward under-eating and crash dieting, drives people out
  after a few weeks, and produces worse long-term results. Consistency and
  activity are what actually predict who's still here in month three.

  Tune the weights below to match what your group values. The screenshots
  are used as PROOF OF LOGGING (you tracked your food) and as ACTIVITY data —
  the bot does not rank on calorie magnitude.

Points per day:
  logged food (tracked)          -> POINTS_LOGGED
  hit move goal (ring closed)    -> POINTS_MOVE_GOAL
  per active minute              -> POINTS_PER_ACTIVE_MIN  (capped by ACTIVE_MIN_CAP)

Weekly bonus:
  streak (consecutive logged days) -> POINTS_PER_STREAK_DAY
"""

import datetime as dt

POINTS_LOGGED = 10
POINTS_MOVE_GOAL = 8
POINTS_PER_ACTIVE_MIN = 0.2
ACTIVE_MIN_CAP = 60          # don't reward grinding past a healthy daily cap
POINTS_PER_STREAK_DAY = 3
LATE_FOOD_PENALTY = -5       # deducted when food log is posted after 7 PM EST


def entry_points(*, logged_food=None, active_minutes=None, move_goal_met=None):
    """Points earned from a single day's entry."""
    pts = 0
    if logged_food:
        pts += POINTS_LOGGED
    if move_goal_met:
        pts += POINTS_MOVE_GOAL
    if active_minutes:
        capped = min(int(active_minutes), ACTIVE_MIN_CAP)
        pts += capped * POINTS_PER_ACTIVE_MIN
    return round(pts)


def _longest_streak(days_sorted):
    """Longest run of consecutive calendar days that were logged."""
    best = cur = 0
    prev = None
    for d in days_sorted:
        if prev is not None and (d - prev).days == 1:
            cur += 1
        else:
            cur = 1
        best = max(best, cur)
        prev = d
    return best


def total_scores(rows):
    """rows: list of entry dicts -> ranked list of per-user summaries."""
    by_user = {}
    for r in rows:
        u = by_user.setdefault(r["user_id"], {
            "user_id": r["user_id"],
            "username": r["username"],
            "days_logged": 0,
            "active_minutes": 0,
            "move_goals": 0,
            "late_penalties": 0,
            "logged_dates": [],
        })
        u["username"] = r["username"]
        if r.get("logged_food"):
            u["days_logged"] += 1
            u["logged_dates"].append(dt.date.fromisoformat(r["day"]))
        u["active_minutes"] += int(r.get("active_minutes") or 0)
        u["move_goals"] += 1 if r.get("move_goal_met") else 0
        u["late_penalties"] += 1 if r.get("late_penalty") else 0

    results = []
    for u in by_user.values():
        streak = _longest_streak(sorted(u["logged_dates"]))
        capped_active = u["active_minutes"]
        score = (
            u["days_logged"] * POINTS_LOGGED
            + u["move_goals"] * POINTS_MOVE_GOAL
            + capped_active * POINTS_PER_ACTIVE_MIN
            + streak * POINTS_PER_STREAK_DAY
            + u["late_penalties"] * LATE_FOOD_PENALTY
        )
        results.append({
            "username": u["username"],
            "days_logged": u["days_logged"],
            "active_minutes": u["active_minutes"],
            "move_goals": u["move_goals"],
            "streak": streak,
            "score": round(score),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results
