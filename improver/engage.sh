#!/bin/bash
# improver/engage.sh — Claude Code-driven X engagement
#
# Searches for relevant posts in your niche, scores them, and delivers
# a like/follow candidate list to Chatwork (manual engagement workflow).
#
# Usage:
#   bash improver/engage.sh           # Run engagement session
#   bash improver/engage.sh --dry-run # Preview only

set -euo pipefail

export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="${XPOST_LOG_DIR:-/tmp/xpost-logs}"
CLAUDE_MODEL=${CLAUDE_MODEL_ENGAGER:-sonnet}
SESSION_TIMEOUT=1500  # 25 minutes (search budget拡大対応)
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

cd "$PROJECT_ROOT"

TIMESTAMP=$(TZ=Asia/Tokyo date '+%Y%m%d_%H%M')
TODAY=$(TZ=Asia/Tokyo date '+%Y-%m-%d')
log() { echo "[$(TZ=Asia/Tokyo date '+%H:%M:%S')] $*"; }

NOTIFY="$SCRIPT_DIR/scripts/notify.py"
notify_error() {
    local reason="$1"
    log "$reason"
    if [ -f "$NOTIFY" ]; then
        python3 "$NOTIFY" \
            "engage.sh エラー $(TZ=Asia/Tokyo date '+%Y-%m-%d %H:%M')" \
            "⚠️ $reason

ログ: $LOG_DIR/engager-stdout.log" 2>/dev/null || true
    fi
}

# Prerequisites
CLAUDE_BIN="${CLAUDE_BIN:-$(which claude 2>/dev/null || echo "$HOME/.local/bin/claude")}"
[ -x "$CLAUDE_BIN" ] || { notify_error "claude CLI not found at $CLAUDE_BIN"; exit 1; }

mkdir -p "$LOG_DIR"
mkdir -p "$SCRIPT_DIR/data/engagement"

# Start xmcp if not already running
XMCP_DIR="$PROJECT_ROOT/xmcp"
XMCP_PID=""
if [ -f "$XMCP_DIR/server.py" ]; then
    # Check if xmcp is already running
    if curl -s http://127.0.0.1:8200/mcp > /dev/null 2>&1; then
        log "xmcp already running"
    else
        log "Starting xmcp..."
        (cd "$XMCP_DIR" && source .venv/bin/activate && python server.py > "$LOG_DIR/xmcp-engage.log" 2>&1) &
        XMCP_PID=$!
        sleep 5
        if kill -0 "$XMCP_PID" 2>/dev/null; then
            log "xmcp started (PID: $XMCP_PID)"
        else
            notify_error "xmcp failed to start (port 8200 occupied?). See $LOG_DIR/xmcp-engage.log"
            exit 1
        fi
    fi
fi

trap '[ -n "$XMCP_PID" ] && kill "$XMCP_PID" 2>/dev/null' EXIT

# Refresh following cache (TTL 6h). Engager runs with stale/missing cache on failure.
# Skipped in dry-run to avoid API side effects during preview.
FETCH_SCRIPT="$SCRIPT_DIR/scripts/engagement/fetch_following.py"
if [ -f "$FETCH_SCRIPT" ] && ! $DRY_RUN; then
    log "Refreshing following cache..."
    if (cd "$XMCP_DIR" && source .venv/bin/activate && \
            python "$FETCH_SCRIPT" --ttl-hours 6 --date "$TODAY") \
            >> "$LOG_DIR/fetch-following-${TIMESTAMP}.log" 2>&1; then
        log "fetch_following.py: $(tail -1 "$LOG_DIR/fetch-following-${TIMESTAMP}.log")"
    else
        log "WARN: fetch_following.py failed (exit $?); engager runs with stale or no cache"
    fi
fi

# Load prompt and substitute date
PROMPT=$(<"$SCRIPT_DIR/prompts/engager.md")
PROMPT="${PROMPT//__DATE__/$TODAY}"

if $DRY_RUN; then
    log "Dry run mode — would invoke Claude Code with engager prompt"
    log "Model: $CLAUDE_MODEL"
    log "Timeout: ${SESSION_TIMEOUT}s"
    exit 0
fi

log "=== X Engagement (Claude Code) ==="
log "Model: $CLAUDE_MODEL"
log "Timeout: ${SESSION_TIMEOUT}s"

EXIT_CODE=0
timeout "$SESSION_TIMEOUT" \
    "$CLAUDE_BIN" -p --model "$CLAUDE_MODEL" \
    --permission-mode bypassPermissions \
    --allowedTools "Bash(cd $PROJECT_ROOT*),Bash(git *),Read($PROJECT_ROOT/**),Write($PROJECT_ROOT/improver/data/engagement/*),Glob($PROJECT_ROOT/**),Grep($PROJECT_ROOT/**)" \
    --verbose \
    --output-format stream-json \
    "$PROMPT" < /dev/null > "$LOG_DIR/engage-${TIMESTAMP}.log" 2>&1 || EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    log "Engagement session completed successfully"
elif [ $EXIT_CODE -eq 124 ]; then
    notify_error "Engagement session timed out (${SESSION_TIMEOUT}s limit). See $LOG_DIR/engage-${TIMESTAMP}.log"
else
    notify_error "Engagement session exited with code $EXIT_CODE. See $LOG_DIR/engage-${TIMESTAMP}.log"
fi
