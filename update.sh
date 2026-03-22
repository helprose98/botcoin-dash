#!/bin/bash
# BotCoin Dash update script
# Triggered either by: cron auto-check (every 30 min) or manual trigger file
REPO_DIR="/root/botcoin-dashboard"
LOG="$REPO_DIR/data/update.log"

# Remove trigger file if present
rm -f "$REPO_DIR/data/update.trigger"

cd "$REPO_DIR"

# Check if there's actually a new version before rebuilding
LOCAL=$(cat VERSION 2>/dev/null || echo "0.0.0")
REMOTE=$(curl -sf https://raw.githubusercontent.com/helprose98/botcoin-dash/main/VERSION || echo "$LOCAL")

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0  # Nothing to do
fi

echo "[update] New version available: $LOCAL -> $REMOTE" > "$LOG"
echo "[update] Starting dash update at $(date)" >> "$LOG"

git fetch origin >> "$LOG" 2>&1
git reset --hard origin/main >> "$LOG" 2>&1
docker compose down >> "$LOG" 2>&1
docker compose build --no-cache >> "$LOG" 2>&1
docker compose up -d >> "$LOG" 2>&1

echo "[update] Done at $(date). Now on $REMOTE" >> "$LOG"
