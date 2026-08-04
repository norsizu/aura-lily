#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_MODEL_PATH="$ROOT_DIR/.docker/aura-persona/asr-models/ggml-small.bin"
if [[ ! -f "$DEFAULT_MODEL_PATH" ]]; then
  DEFAULT_MODEL_PATH="$ROOT_DIR/.docker/aura-persona/asr-models/ggml-base.bin"
fi
SOURCE_MODEL_PATH="${AURA_WHISPER_MODEL:-$DEFAULT_MODEL_PATH}"
HOST="${AURA_LOCAL_ASR_HOST:-127.0.0.1}"
PORT="${AURA_LOCAL_ASR_PORT:-8766}"
RUNTIME_DIR="${AURA_LOCAL_ASR_RUNTIME_DIR:-$HOME/.aura-lily/local-asr}"
MODEL_PATH="$RUNTIME_DIR/models/$(basename "$SOURCE_MODEL_PATH")"
PID_FILE="$RUNTIME_DIR/local-asr.pid"
LOG_FILE="$RUNTIME_DIR/local-asr.log"
ERR_FILE="$RUNTIME_DIR/local-asr.err.log"
PLIST_FILE="$HOME/Library/LaunchAgents/space.heiyu.aura-lily.local-asr.plist"
RUNNER_FILE="$RUNTIME_DIR/run-local-asr.sh"
LABEL="space.heiyu.aura-lily.local-asr"
SERVER="$RUNTIME_DIR/local_whisper_asr_server.py"
PYTHON_BIN="${AURA_LOCAL_ASR_PYTHON:-$(command -v python3)}"
LAUNCHD_DOMAIN="gui/$(id -u)"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

launchd_available() {
  command -v launchctl >/dev/null 2>&1 && launchctl print "$LAUNCHD_DOMAIN" >/dev/null 2>&1
}

launchd_loaded() {
  launchd_available || return 1
  launchctl print "$LAUNCHD_DOMAIN/$LABEL" >/dev/null 2>&1
}

is_healthy() {
  curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1
}

write_runner() {
  prepare_runtime_files
  cat > "$RUNNER_FILE" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$RUNTIME_DIR"
export AURA_WHISPER_MODEL="$MODEL_PATH"
export AURA_LOCAL_ASR_HOST="$HOST"
export AURA_LOCAL_ASR_PORT="$PORT"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec "$PYTHON_BIN" "$SERVER" --host "$HOST" --port "$PORT"
EOF
  chmod +x "$RUNNER_FILE"
}

write_plist() {
  mkdir -p "$(dirname "$PLIST_FILE")"
  write_runner
  cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$RUNNER_FILE</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AURA_WHISPER_MODEL</key>
    <string>$MODEL_PATH</string>
    <key>AURA_LOCAL_ASR_HOST</key>
    <string>$HOST</string>
    <key>AURA_LOCAL_ASR_PORT</key>
    <string>$PORT</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>$RUNTIME_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_FILE</string>
  <key>StandardErrorPath</key>
  <string>$ERR_FILE</string>
</dict>
</plist>
EOF
}

prepare_runtime_files() {
  mkdir -p "$RUNTIME_DIR/models"
  cp "$ROOT_DIR/tools/local_whisper_asr_server.py" "$SERVER"
  if [[ ! -f "$SOURCE_MODEL_PATH" ]]; then
    echo "missing ASR model: $SOURCE_MODEL_PATH" >&2
    echo "download one first, for example: .docker/aura-persona/asr-models/ggml-base.bin" >&2
    return 1
  fi
  if [[ ! -f "$MODEL_PATH" ]] || ! cmp -s "$SOURCE_MODEL_PATH" "$MODEL_PATH"; then
    cp "$SOURCE_MODEL_PATH" "$MODEL_PATH"
  fi
}

start_with_launchd() {
  write_plist
  if launchd_loaded; then
    if is_healthy; then
      echo "local ASR already healthy via launchd: label=$LABEL url=http://$HOST:$PORT"
      return 0
    fi
    launchctl bootout "$LAUNCHD_DOMAIN/$LABEL" >/dev/null 2>&1 || true
    sleep 0.2
    launchctl bootstrap "$LAUNCHD_DOMAIN" "$PLIST_FILE"
  else
    launchctl bootstrap "$LAUNCHD_DOMAIN" "$PLIST_FILE"
  fi
  for _ in {1..30}; do
    if is_healthy; then
      rm -f "$PID_FILE"
      echo "local ASR started via launchd: label=$LABEL url=http://$HOST:$PORT log=$LOG_FILE"
      return 0
    fi
    sleep 0.2
  done
  if ! is_healthy; then
    echo "local ASR launchd service did not become healthy; see $LOG_FILE and $ERR_FILE" >&2
    return 1
  fi
}

start_with_nohup() {
  prepare_runtime_files
  AURA_WHISPER_MODEL="$MODEL_PATH" \
  AURA_LOCAL_ASR_HOST="$HOST" \
  AURA_LOCAL_ASR_PORT="$PORT" \
    nohup "$PYTHON_BIN" "$SERVER" --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>"$ERR_FILE" &
  echo "$!" > "$PID_FILE"
  sleep 0.5
  if ! is_running; then
    echo "local ASR failed to start; see $LOG_FILE and $ERR_FILE" >&2
    return 1
  fi
  echo "local ASR started: pid=$(cat "$PID_FILE") url=http://$HOST:$PORT log=$LOG_FILE"
}

start_server() {
  mkdir -p "$RUNTIME_DIR"
  if launchd_loaded; then
    if is_healthy; then
      echo "local ASR already managed by launchd: label=$LABEL url=http://$HOST:$PORT"
      return 0
    fi
    echo "local ASR launchd job is loaded but unhealthy; reloading..."
  elif is_running; then
    echo "local ASR already running: pid=$(cat "$PID_FILE") url=http://$HOST:$PORT"
    return 0
  fi
  if [[ ! -f "$SOURCE_MODEL_PATH" ]]; then
    echo "missing ASR model: $SOURCE_MODEL_PATH" >&2
    echo "download one first, for example: .docker/aura-persona/asr-models/ggml-base.bin" >&2
    return 1
  fi
  if launchd_available; then
    start_with_launchd
  else
    start_with_nohup
  fi
}

stop_server() {
  if launchd_loaded; then
    launchctl bootout "$LAUNCHD_DOMAIN/$LABEL" >/dev/null 2>&1 || true
    echo "local ASR launchd service stopped"
  fi
  if ! is_running; then
    rm -f "$PID_FILE"
    if ! launchd_loaded; then
      echo "local ASR not running"
    fi
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "local ASR stopped"
      return 0
    fi
    sleep 0.1
  done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "local ASR stopped"
}

status_server() {
  if launchd_loaded; then
    echo "local ASR managed by launchd: label=$LABEL url=http://$HOST:$PORT"
    if ! curl -fsS "http://$HOST:$PORT/health"; then
      echo
      return 1
    fi
    echo
    return 0
  elif is_running; then
    echo "local ASR running: pid=$(cat "$PID_FILE") url=http://$HOST:$PORT"
    curl -fsS "http://$HOST:$PORT/health" || true
    echo
    return 0
  fi
  echo "local ASR not running"
  return 1
}

case "${1:-status}" in
  start)
    start_server
    ;;
  stop)
    stop_server
    ;;
  restart)
    stop_server
    start_server
    ;;
  status)
    status_server
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
