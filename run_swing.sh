#!/bin/bash
# Runs swing_trader.py once daily at ~10:15 AM ET, Mon–Fri
# launchd fires this at 10:15 ET. Script double-checks day/hour as a safeguard.

SCRIPT_DIR="/Users/vishalgupta/claude"
LOG="$SCRIPT_DIR/swing_local.log"
PYTHON="/usr/bin/python3"

DOW=$(TZ="America/New_York" date +%u)
if [ "$DOW" -ge 6 ]; then
    exit 0
fi

# Only run between 10:10–10:20 AM ET — handles both EDT (UTC-4) and EST (UTC-5)
# launchd fires this at 14:15 and 15:15 UTC; exactly one will land in this window
HOUR=$(TZ="America/New_York" date +%H)
MIN=$(TZ="America/New_York" date +%M)
MINS=$(( 10#$HOUR * 60 + 10#$MIN ))
if [ "$MINS" -lt 610 ] || [ "$MINS" -gt 620 ]; then
    exit 0
fi

# Guard against double-run on DST transition day
LOCKFILE="$SCRIPT_DIR/.swing_ran_$(TZ='America/New_York' date +%Y-%m-%d)"
[ -f "$LOCKFILE" ] && exit 0
touch "$LOCKFILE"
# Clean up yesterday's lockfile
find "$SCRIPT_DIR" -name ".swing_ran_*" -not -name "$(basename $LOCKFILE)" -delete

echo "$(TZ='America/New_York' date '+%Y-%m-%d %H:%M:%S ET') — running swing_trader.py" >> "$LOG"
cd "$SCRIPT_DIR"
$PYTHON swing_trader.py >> "$LOG" 2>&1
$PYTHON build_hub.py >> "$LOG" 2>&1
$PYTHON build_consolidated.py >> "$LOG" 2>&1

# Push only HTML dashboards — JSON state files are owned by GitHub Actions to avoid conflicts
git add swing_index.html trading_hub.html consolidated.html
find reports -name 'swing_*.html' 2>/dev/null | xargs -r git add -f
git diff --cached --quiet && exit 0
git commit -m "swing: $(TZ='America/New_York' date '+%Y-%m-%d %H:%M') ET local [skip ci]" >> "$LOG" 2>&1
git pull --rebase -X theirs origin main >> "$LOG" 2>&1
git push origin main >> "$LOG" 2>&1
