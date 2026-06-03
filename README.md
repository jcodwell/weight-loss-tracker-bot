# Fitness accountability bot

A Discord bot for a group that's working on fitness together. Members post
screenshots (food-log app + Apple Fitness) in a tracking channel; the bot reads
them, confirms the parse, stores the data, and posts a weekly leaderboard.

## How the ranking works (and why)

The leaderboard ranks on **consistency + activity**, not on calorie totals or
weight lost. This is deliberate. A public ranking driven by calorie counts or
the scale tends to push a group toward eating less and less to "win," burns
people out, and produces worse results than rewarding the habits that actually
stick. So the screenshots are used as proof you *logged* and as your *activity*
data — the bot never ranks anyone on how little they ate.

Default points (all tunable in `scoring.py`):
- logging food for the day: 10
- closing the Apple Fitness move ring: 8
- active minutes: 0.2 each, capped
- consecutive-day streak: 3 per day

If your group wants different incentives, edit the constants at the top of
`scoring.py` — the rationale is documented there.

## Setup

1. Install Python 3.10+, then:
   ```
   pip install -r requirements.txt
   ```

2. Create a bot at https://discord.com/developers/applications
   - Add a bot, copy its token.
   - Under "Privileged Gateway Intents," enable **Message Content Intent**.
   - Invite it to your server with the `bot` and `applications.commands` scopes
     and permission to read/send messages + add reactions.

3. Get an Anthropic API key at https://console.anthropic.com — this powers the
   screenshot reading. Current models and pricing:
   https://docs.claude.com/en/docs/about-claude/models

4. Find your tracking channel's ID (enable Developer Mode in Discord, right-click
   the channel → Copy ID).

5. Create a `.env` file:
   ```
   DISCORD_TOKEN=your_discord_token
   ANTHROPIC_API_KEY=your_anthropic_key
   TRACKING_CHANNEL_ID=123456789012345678
   LEADERBOARD_CHANNEL_ID=123456789012345678   # optional; defaults to tracking channel
   ```

6. Run it:
   ```
   python bot.py
   ```

## Using it

- Post one or more screenshots in the tracking channel.
- The bot replies with what it read; react ✅ to save or ❌ to discard.
  (Vision can misread, so the confirmation step matters.)
- `/leaderboard` shows the current week's standings any time.
- A full leaderboard auto-posts Sundays at 20:00 server time (change the cron in
  `bot.py`'s `on_ready`).

## Notes & extension ideas

- **Accuracy:** if members post cluttered or cropped images, switch `VISION_MODEL`
  in `bot.py` to a Sonnet model for better reading.
- **Cost:** each screenshot is one cheap vision call; for a small group it's
  negligible. Check current pricing at the link above.
- **Privacy:** the bot stores logged/active data per user in a local SQLite file
  (`fitness.db`). It does not store the calorie numbers themselves. Tell your
  members what's kept.
- **Healthy group norms:** consider pinning a short channel rule that this is
  about consistency and showing up, not racing to the lowest number — it sets the
  tone and keeps the competitive element friendly.
