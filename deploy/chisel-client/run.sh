#!/bin/sh
# Клиент chisel: держит обратные туннели колонки на сервер.
# Управление: run.sh start|stop|status
DIR=/data/opt/chisel
ENVF=$DIR/chisel.env
LOG=$DIR/chisel.log
PIDF=$DIR/chisel.pid

[ -f "$ENVF" ] && . "$ENVF"

start() {
  if pidof chisel >/dev/null 2>&1; then
    echo "chisel уже запущен"
    return 0
  fi
  if [ -z "$SERVER_URL" ]; then
    echo "$(date) SERVER_URL не задан в $ENVF" >> "$LOG"
    return 1
  fi
  # SSH колонки: REMOTE_BIND:REMOTE_PORT на сервере -> 127.0.0.1:$LOCAL_PORT колонки
  REMOTES="R:${REMOTE_BIND}:${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT:-2222}"
  # YAM (веб-демон) пробрасывается тем же туннелем, если задан YAM_PORT
  if [ -n "$YAM_PORT" ]; then
    REMOTES="$REMOTES R:127.0.0.1:${YAM_PORT}:127.0.0.1:${YAM_PORT}"
  fi
  nohup "$DIR/chisel" client --auth "$AUTH" --keepalive 15s --max-retry-count -1 \
      "$SERVER_URL" $REMOTES >> "$LOG" 2>&1 &
  echo $! > "$PIDF"
}

stop() {
  [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null
  pidof chisel >/dev/null 2>&1 && killall chisel 2>/dev/null
  rm -f "$PIDF"
}

case "$1" in
  start) start ;;
  stop) stop ;;
  status) pidof chisel >/dev/null 2>&1 && echo running || echo stopped ;;
  *) echo "usage: $0 start|stop|status" ;;
esac
exit 0
