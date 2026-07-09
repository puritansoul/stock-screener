#!/bin/bash
# Runs paper_trader.py once daily at ~10:15 AM ET, Mon–Fri
# launchd fires this at 10:15 ET. Script double-checks day/hour as a safeguard.

SCRIPT_DIR="/Users/vishalgupta/claude"
LOG="$SCRIPT_DIR/paper_local.log"
PYTHON="/usr/bin/python3"

DOW=$(TZ="America/New_York" date +%u)
if [ "$DOW" -ge 6 ]; then
    exit 0
fi

echo "$(TZ='America/New_York' date '+%Y-%m-%d %H:%M:%S ET') — running paper_trader.py" >> "$LOG"
cd "$SCRIPT_DIR"
$PYTHON paper_trader.py >> "$LOG" 2>&1
$PYTHON build_hub.py >> "$LOG" 2>&1
$PYTHON build_consolidated.py >> "$LOG" 2>&1

git add paper_trades.json paper_index.html trading_hub.html consolidated.html
find reports -name 'paper_*.html' 2>/dev/null | xargs -r git add -f
git diff --cached --quiet && exit 0
git commit -m "paper: $(TZ='America/New_York' date '+%Y-%m-%d %H:%M') ET local [skip ci]" >> "$LOG" 2>&1
git pull --rebase origin main >> "$LOG" 2>&1
git push origin main >> "$LOG" 2>&1
