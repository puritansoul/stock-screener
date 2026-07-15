#!/bin/bash
# Local primary runner — owns intraday_trades.json
# Runs every 2 min via launchd. GH Actions is fallback only.

SCRIPT_DIR="/Users/vishalgupta/claude"
LOG="$SCRIPT_DIR/intraday_launchd.log"
PYTHON="/usr/bin/python3"

# Weekend check
DOW=$(TZ="America/New_York" date +%u)
if [ "$DOW" -ge 6 ]; then
    exit 0
fi

# Market hours check: 9:28 AM – 4:05 PM ET
HOUR=$(TZ="America/New_York" date +%H)
MIN=$(TZ="America/New_York" date +%M)
MINS=$(( 10#$HOUR * 60 + 10#$MIN ))
if [ "$MINS" -lt 568 ] || [ "$MINS" -gt 965 ]; then
    exit 0
fi

# Prevent overlapping runs
LOCKFILE="$SCRIPT_DIR/.intraday_running"
if [ -f "$LOCKFILE" ]; then
    echo "$(TZ='America/New_York' date '+%Y-%m-%d %H:%M:%S ET') — skipped (previous run still active)" >> "$LOG"
    exit 0
fi
touch "$LOCKFILE"
trap "rm -f '$LOCKFILE'" EXIT

cd "$SCRIPT_DIR"
echo "$(TZ='America/New_York' date '+%Y-%m-%d %H:%M:%S ET') — running" >> "$LOG"

# Pull latest state from remote before running — GH Actions fallback may have committed
git pull --rebase -X theirs origin main >> "$LOG" 2>&1

# Run the bot
$PYTHON intraday_trader.py >> "$LOG" 2>&1
$PYTHON build_hub.py >> "$LOG" 2>&1
$PYTHON build_consolidated.py >> "$LOG" 2>&1

# Commit and push everything — JSON state + HTML dashboards
git add intraday_trades.json intraday_index.html trading_hub.html consolidated.html
find reports -name 'intraday_*.html' 2>/dev/null | xargs -r git add -f
git diff --cached --quiet && exit 0
git commit -m "intraday: $(TZ='America/New_York' date '+%Y-%m-%d %H:%M') ET local [skip ci]" >> "$LOG" 2>&1
git pull --rebase -X theirs origin main >> "$LOG" 2>&1
git push origin main >> "$LOG" 2>&1
