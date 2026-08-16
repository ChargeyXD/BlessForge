#!/usr/bin/env bash
# Local dev runner: start/stop/restart the app against a live Crafty.
# Usage: ./dev.sh start|stop|restart|status
set -u
cd "$(dirname "$0")"
PIDFILE=.dev.pid
LOG=${LOG:-/tmp/cms-dev.log}

start() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
    echo "already running (pid $(cat $PIDFILE))"; return 0
  fi
  [ -f .env.test ] && . ./.env.test
  setsid nohup .venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port "${PORT:-8710}" > "$LOG" 2>&1 < /dev/null &
  echo $! > "$PIDFILE"
  sleep 5
  status
}

stop() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat $PIDFILE)" 2>/dev/null
    rm -f "$PIDFILE"
    echo "stopped"
  else
    echo "not running"
  fi
}

status() {
  if curl -s -m 8 "localhost:${PORT:-8710}/api/health" > /dev/null 2>&1; then
    echo "UP"
  else
    echo "DOWN"; tail -5 "$LOG" 2>/dev/null
  fi
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  *) echo "usage: $0 start|stop|restart|status"; exit 1 ;;
esac
