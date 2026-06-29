"""
Weekly scoring.

Design rationale (read before changing):
  This ranks people on behaviors they can control and sustain — showing up,
  logging, and moving — NOT on how few calories they ate or how much weight
  they lost. Ranking a group on calorie totals or weight change reliably
  pushes the group toward under-eating and crash dieting, drives people out
  after a few weeks, and produces worse long-term results. Consistency and
  activity are what actually predict who's still here in month three.

Points per day:
  Food log:
    99–200 cal under goal          -> POINTS_CALORIE_CLOSE     (+1)
    0–98 cal under goal            -> POINTS_CALORIE_ON_GOAL   (+2)
    0–50 cal over goal (grace)     -> POINTS_CALORIE_ON_GOAL   (+2, no penalty)
    over 50 cal over goal          -> POINTS_PER_CALORIE_OVER  (-0.1 each)
    protein goal met               -> POINTS_PROTEIN_GOAL      (+3)

  Activity — Apple Fitness:
    move ring closed               -> POINTS_MOVE_GOAL (+10 flat)
                                      with workout details and MET > MET_THRESHOLD:
                                      bonus: 10 × (1 + MET/100) e.g. MET 5.0 → +10.5 pts
    per active minute              -> POINTS_PER_ACTIVE_MIN (+0.2, no cap)
    per step                       -> POINTS_PER_STEP (+0.001, no cap)

  Activity — Oura Ring:
    oura goal qualification:
      requires active_cal > baseline AND active_minutes >= OURA_MIN_ACTIVE_MINUTES (30)
      baseline = MET_THRESHOLD^2 × active_minutes × weight_kg / 200
      if qualified             -> POINTS_OURA_GOAL (+10)
      per minute over 30       -> POINTS_PER_OURA_OVERTIME_MIN (+0.25, if qualified)
      missing data fallback    -> flat +10 pts
    per active minute          -> POINTS_PER_OURA_ACTIVE_MIN (+0.1, no cap)
    per step                   -> POINTS_PER_STEP (+0.001, no cap)

  Daily post points (each screenshot type is worth +10 when posted):
    food log posted                -> POINTS_FOOD_LOGGED    (+10)
    fitness screenshot posted      -> POINTS_FITNESS_LOGGED (+10)
    Happy Scale posted             -> POINTS_WEIGHT_LOGGED  (+10)
    none of the three posted       -> POINTS_MISSED_ALL     (-10 penalty)

  Happy Scale weight trends (green = losing):
    lost in 7 days                 -> POINTS_TREND_7D    (+5)
    lost in 30 days                -> POINTS_TREND_30D   (+4)
    lost in 90 days                -> POINTS_TREND_90D   (+3)
    lost all time                  -> POINTS_TREND_ALLTIME (+2)

Streak bonus (consecutive days with food log):
  streak_bonus = 1 + streak_length   (e.g. 5-day streak → +6 pts total)
"""

import datetime as dt

POINTS_PER_CALORIE_OVER  = -0.1
POINTS_CALORIE_CLOSE     = 1      # 99–200 cal under goal
POINTS_CALORIE_ON_GOAL   = 2      # 0–98 cal under goal, or 0–50 cal over goal (grace zone)
POINTS_PROTEIN_GOAL      = 3      # protein goal met
CALORIE_CLOSE_MIN    = 99         # calories under where close bonus starts
CALORIE_CLOSE_MAX    = 200        # calories under where close bonus ends (beyond = no bonus)
CALORIE_GRACE_OVER   = 50         # calories over goal before penalty kicks in

POINTS_MOVE_GOAL  = 10            # Apple Fitness move ring (+10 flat; MET bonus with workout data)
POINTS_OURA_GOAL  = 10            # Oura Ring activity goal (+10 if active_cal > baseline AND active_minutes >= 30)
MET_THRESHOLD     = 3.5
POINTS_PER_ACTIVE_MIN      = 0.2   # Apple Fitness: pts per active minute (no cap)
POINTS_PER_OURA_ACTIVE_MIN = 0.1   # Oura Ring: pts per active minute (no cap)
POINTS_PER_STEP   = 0.001

OURA_MIN_ACTIVE_MINUTES      = 30    # minimum active minutes to qualify for Oura goal bonus
POINTS_PER_OURA_OVERTIME_MIN = 0.25  # Oura: extra pts per active minute beyond 30 (when qualified)

POINTS_FOOD_LOGGED    = 10         # posted a food log screenshot
POINTS_FITNESS_LOGGED = 10         # posted a fitness screenshot (Apple Fitness or Oura)
POINTS_WEIGHT_LOGGED  = 10         # posted a Happy Scale screenshot
POINTS_MISSED_ALL     = -10        # penalty when none of the three are posted on an entry day

POINTS_TREND_7D      = 5
POINTS_TREND_30D     = 4
POINTS_TREND_90D     = 3
POINTS_TREND_ALLTIME = 2


def compute_met(active_calories, active_minutes, weight_kg):
    """MET = (calories × 200) / (minutes × 2.8 × weight_kg). Returns 0.0 if data missing."""
    if not active_minutes or not weight_kg or active_calories is None:
        return 0.0
    mins, kg, cal = float(active_minutes), float(weight_kg), float(active_calories)
    if mins <= 0 or kg <= 0:
        return 0.0
    return (cal * 200) / (mins * 2.8 * kg)


def oura_calorie_baseline(active_minutes, weight_kg):
    """Minimum active calories Oura user must burn to qualify for POINTS_OURA_GOAL.
    Formula: MET_THRESHOLD^2 * active_minutes * weight_kg / 200
    Returns None if data is missing (caller should fall back to flat 10 pts).
    """
    if active_minutes is None or weight_kg is None:
        return None
    mins, kg = int(active_minutes), float(weight_kg)
    if mins <= 0 or kg <= 0:
        return None
    return MET_THRESHOLD * mins * MET_THRESHOLD * kg / 200


def streak_bonus(streak_length):
    """Bonus points for a streak: 1 + streak_length (0 if no streak)."""
    return (1 + streak_length) if streak_length > 0 else 0


def entry_points(*, logged_food=None, calories_over=None, calories_under=None,
                 protein_goal_met=None, active_minutes=None,
                 active_calories=None, steps=None, weight_kg=None,
                 move_goal_met=None, oura_goal_met=None,
                 workout_calories=None, workout_minutes=None,
                 trend_7d=None, trend_30d=None, trend_90d=None, trend_alltime=None,
                 tracker_type="unknown"):
    """Points earned from a single day's entry (excludes streak and complete-day bonus)."""
    pts = 0.0

    # Food calorie tiers
    cal_over = int(calories_over) if calories_over else 0
    if cal_over > CALORIE_GRACE_OVER:
        pts += cal_over * POINTS_PER_CALORIE_OVER
    elif cal_over > 0:
        pts += POINTS_CALORIE_ON_GOAL          # grace zone: 1–50 over
    elif calories_under is not None:
        under = int(calories_under)
        if under < CALORIE_CLOSE_MIN:          # 0–98 under
            pts += POINTS_CALORIE_ON_GOAL
        elif under <= CALORIE_CLOSE_MAX:       # 99–200 under
            pts += POINTS_CALORIE_CLOSE
    if protein_goal_met:
        pts += POINTS_PROTEIN_GOAL

    # Apple Fitness: per-minute points + MET-based move ring bonus
    if tracker_type != "oura":
        if active_minutes:
            pts += int(active_minutes) * POINTS_PER_ACTIVE_MIN
        if move_goal_met:
            if workout_calories:
                met = compute_met(workout_calories, workout_minutes, weight_kg)
                if met > MET_THRESHOLD:
                    pts += POINTS_MOVE_GOAL * (1 + met / 100)
                else:
                    pts += POINTS_MOVE_GOAL
            else:
                pts += POINTS_MOVE_GOAL

    # Oura Ring: 0.1 pts/min base + qualification-based goal bonus + overtime
    if tracker_type == "oura":
        if active_minutes:
            pts += int(active_minutes) * POINTS_PER_OURA_ACTIVE_MIN
        if oura_goal_met:
            baseline = oura_calorie_baseline(active_minutes, weight_kg)
            if baseline is None or active_calories is None:
                pts += POINTS_OURA_GOAL   # fallback: missing data
            else:
                active_mins_int = int(active_minutes)
                if int(active_calories) > baseline and active_mins_int >= OURA_MIN_ACTIVE_MINUTES:
                    pts += POINTS_OURA_GOAL
                    overtime = active_mins_int - OURA_MIN_ACTIVE_MINUTES
                    if overtime > 0:
                        pts += overtime * POINTS_PER_OURA_OVERTIME_MIN
                # else: didn't qualify — 0 pts for goal

    if steps:
        pts += int(steps) * POINTS_PER_STEP

    # Happy Scale trends
    if trend_7d:
        pts += POINTS_TREND_7D
    if trend_30d:
        pts += POINTS_TREND_30D
    if trend_90d:
        pts += POINTS_TREND_90D
    if trend_alltime:
        pts += POINTS_TREND_ALLTIME

    return round(pts, 3)


def _longest_streak(days_sorted):
    """Longest run of consecutive calendar days."""
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


def total_scores(rows, adjustments=None, tracker_types=None):
    """rows: list of entry dicts -> ranked list of per-user summaries.
    tracker_types: dict of {user_id: tracker_type} from user_prefs.
    """
    if adjustments is None:
        adjustments = {}
    if tracker_types is None:
        tracker_types = {}
    by_user = {}
    for r in rows:
        u = by_user.setdefault(r["user_id"], {
            "user_id": r["user_id"],
            "username": r["username"],
            "days_logged": 0,
            "total_calories_over": 0,
            "calorie_close_days": 0,
            "calorie_on_goal_days": 0,
            "protein_goal_days": 0,
            "active_minutes": 0,
            "total_steps": 0,
            "activity_goal_pts": 0.0,
            "qualified_goals": 0,
            "food_logged_days": 0,
            "fitness_logged_days": 0,
            "weight_logged_days": 0,
            "missed_all_days": 0,
            "trend_7d_count": 0,
            "trend_30d_count": 0,
            "trend_90d_count": 0,
            "trend_alltime_count": 0,
            "logged_dates": [],
        })
        u["username"] = r["username"]
        if r.get("logged_food"):
            u["days_logged"] += 1
            u["logged_dates"].append(dt.date.fromisoformat(r["day"]))

        cal_over  = int(r.get("calories_over") or 0)
        cal_under = r.get("calories_under")
        if cal_over > CALORIE_GRACE_OVER:
            u["total_calories_over"] += cal_over   # penalty on all calories over
        elif cal_over > 0:
            u["calorie_on_goal_days"] += 1         # grace zone: 1–50 over → +2
        elif cal_under is not None:
            under = int(cal_under)
            if under < CALORIE_CLOSE_MIN:          # 0–98 under
                u["calorie_on_goal_days"] += 1
            elif under <= CALORIE_CLOSE_MAX:       # 99–200 under
                u["calorie_close_days"] += 1
        if r.get("protein_goal_met"):
            u["protein_goal_days"] += 1

        u["active_minutes"] += int(r.get("active_minutes") or 0)
        u["total_steps"] += int(r.get("steps") or 0)
        food = bool(r.get("logged_food"))
        fit  = bool(r.get("fitness_logged"))
        wt   = bool(r.get("weight_logged"))
        if food:
            u["food_logged_days"] += 1
        if fit:
            u["fitness_logged_days"] += 1
        if wt:
            u["weight_logged_days"] += 1
        # Only penalize if the row has real data (not a bare signup/placeholder row)
        has_data = (
            int(r.get("active_minutes") or 0) > 0 or
            int(r.get("steps") or 0) > 0 or
            int(r.get("calories_over") or 0) > 0 or
            r.get("calories_under") is not None or
            r.get("trend_7d") or r.get("trend_30d") or
            r.get("trend_90d") or r.get("trend_alltime")
        )
        if not food and not fit and not wt and has_data:
            u["missed_all_days"] += 1
        u["trend_7d_count"] += 1 if r.get("trend_7d") else 0
        u["trend_30d_count"] += 1 if r.get("trend_30d") else 0
        u["trend_90d_count"] += 1 if r.get("trend_90d") else 0
        u["trend_alltime_count"] += 1 if r.get("trend_alltime") else 0

        tracker = tracker_types.get(r["user_id"], "unknown")
        u["tracker_type"] = tracker  # stable per user; updated each row

        # Apple Fitness: MET-based move ring bonus
        if tracker != "oura" and r.get("move_goal_met"):
            if r.get("workout_calories"):
                met = compute_met(r.get("workout_calories"), r.get("workout_minutes"), r.get("weight_kg"))
                if met > MET_THRESHOLD:
                    u["qualified_goals"] += 1
                    u["activity_goal_pts"] += POINTS_MOVE_GOAL * (1 + met / 100)
                else:
                    u["activity_goal_pts"] += POINTS_MOVE_GOAL
            else:
                u["activity_goal_pts"] += POINTS_MOVE_GOAL

        # Oura Ring: calorie baseline + overtime bonus
        if tracker == "oura" and r.get("oura_goal_met"):
            active_mins = r.get("active_minutes")
            active_cal  = r.get("active_calories")
            weight_kg_r = r.get("weight_kg")
            baseline    = oura_calorie_baseline(active_mins, weight_kg_r)
            if baseline is None or active_cal is None:
                u["activity_goal_pts"] += POINTS_OURA_GOAL  # fallback
            else:
                active_mins_int = int(active_mins)
                if int(active_cal) > baseline and active_mins_int >= OURA_MIN_ACTIVE_MINUTES:
                    u["qualified_goals"] += 1
                    u["activity_goal_pts"] += POINTS_OURA_GOAL
                    overtime = active_mins_int - OURA_MIN_ACTIVE_MINUTES
                    if overtime > 0:
                        u["activity_goal_pts"] += overtime * POINTS_PER_OURA_OVERTIME_MIN
                # else: 0 pts — didn't qualify

    results = []
    for u in by_user.values():
        cur_streak = _longest_streak(sorted(u["logged_dates"]))
        sb = streak_bonus(cur_streak)
        adjustment = adjustments.get(u["user_id"], 0)
        per_min_rate = (POINTS_PER_OURA_ACTIVE_MIN if u.get("tracker_type") == "oura"
                        else POINTS_PER_ACTIVE_MIN)
        score = (
            u["total_calories_over"] * POINTS_PER_CALORIE_OVER
            + u["calorie_close_days"] * POINTS_CALORIE_CLOSE
            + u["calorie_on_goal_days"] * POINTS_CALORIE_ON_GOAL
            + u["protein_goal_days"] * POINTS_PROTEIN_GOAL
            + u["activity_goal_pts"]
            + u["active_minutes"] * per_min_rate
            + u["total_steps"] * POINTS_PER_STEP
            + u["food_logged_days"] * POINTS_FOOD_LOGGED
            + u["fitness_logged_days"] * POINTS_FITNESS_LOGGED
            + u["weight_logged_days"] * POINTS_WEIGHT_LOGGED
            + u["missed_all_days"] * POINTS_MISSED_ALL
            + sb
            + u["trend_7d_count"] * POINTS_TREND_7D
            + u["trend_30d_count"] * POINTS_TREND_30D
            + u["trend_90d_count"] * POINTS_TREND_90D
            + u["trend_alltime_count"] * POINTS_TREND_ALLTIME
            + adjustment
        )
        results.append({
            "username": u["username"],
            "days_logged": u["days_logged"],
            "active_minutes": u["active_minutes"],
            "total_steps": u["total_steps"],
            "qualified_goals": u["qualified_goals"],
            "streak": cur_streak,
            "streak_bonus": sb,
            "score": round(score, 3),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results
