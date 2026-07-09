#!/bin/bash
# Runs intraday_trader.py only during market hours (9:28–16:05 ET, Mon–Fri)
# Called every 2 min by launchd. Skips weekends and outside-hours runs silently.

SCRIPT_DIR="/Users/vishalgupta/claude"
LOG="$SCRIPT_DIR/intraday_local.log"
PYTHON="/usr/bin/python3"

# Day of week in ET (1=Mon ... 7=Sun)
DOW=$(TZ="America/New_York" date +%u)
if [ "$DOW" -ge 6 ]; then
    exit 0
fi

# Hour and minute in ET
HOUR=$(TZ="America/New_York" date +%H)
MIN=$(TZ="America/New_York" date +%M)
MINS=$(( HOUR * 60 + MIN ))

# 9:28 AM = 568 min, 4:05 PM = 965 min
if [ "$MINS" -lt 568 ] || [ "$MINS" -gt 965 ]; then
    exit 0
fi

echo "$(TZ='America/New_York' date '+%Y-%m-%d %H:%M:%S ET') — running intraday_trader.py" >> "$LOG"
cd "$SCRIPT_DIR"
$PYTHON intraday_trader.py >> "$LOG" 2>&1

# Push updated state + dashboard to GitHub
git add intraday_trades.json intraday_index.html trading_hub.html consolidated.html
find reports -name 'intraday_*.html' 2>/dev/null | xargs -r git add -f
git diff --cached --quiet && exit 0
git commit -m "intraday: $(TZ='America/New_York' date '+%Y-%m-%d %H:%M') ET local [skip ci]" >> "$LOG" 2>&1
git pull --rebase origin main >> "$LOG" 2>&1
git push origin main >> "$LOG" 2>&1
