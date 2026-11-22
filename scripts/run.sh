#!/bin/bash
# AngelDl bot runner (no systemd): ./run.sh start|stop|restart|status
# Runs bot.py with the venv in this repo; logs to bot.log, pid to bot.pid.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
PIDFILE="$DIR/bot.pid"
LOG="$DIR/bot.log"
PY="$DIR/venv/bin/python"

cmd="${1:-status}"
case "$cmd" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "Already running (pid $(cat "$PIDFILE"))"
      exit 0
    fi
    [ -x "$PY" ] || { echo "venv not found: create it with: python3 -m venv venv && venv/bin/pip install -r requirements.txt"; exit 1; }
    [ -f "$DIR/.env" ] || { echo ".env not found: copy .env.example to .env and fill it in"; exit 1; }
    nohup "$PY" "$DIR/bot.py" >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 3
    echo "Started (pid $(cat "$PIDFILE")). Log: $LOG"
    ;;
  stop)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")"
      rm -f "$PIDFILE"
      echo "Stopped."
    else
      echo "Not running."
      rm -f "$PIDFILE"
    fi
    ;;
  restart)
    "$0" stop || true
    sleep 1
    "$0" start
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "Running (pid $(cat "$PIDFILE"))"
      ps -o pid,rss,cmd -p "$(cat "$PIDFILE")"
    else
      echo "Not running."
    fi
    ;;
  *)
    echo "Usage: $0 start|stop|restart|status"
    exit 1
    ;;
esac
