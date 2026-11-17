#!/bin/sh
# Только собственный плеер и HTTP-адаптеры; службы Алисы не трогаем.
DIR=/data/opt/panel-player
PID=/run/panel-player.pid
LOCKFILE=/run/panel-player.lock
STOP=/run/panel-player.stop
LOG=/tmp/panel-player.log
umask 077

# Единственный супервизор: второй экземпляр не должен воевать за порт и PID.
exec 9>"$LOCKFILE" || exit 1
flock -n 9 || { echo 'panel-player уже запущен'; exit 0; }
echo $$ > "$PID"

export PANEL_PLAYER_DIR="$DIR"
export PANEL_PLAYER_HOST=127.0.0.1
export PANEL_PLAYER_PORT=8765
export PANEL_PLAYER_DEVICE=default
export PANEL_IMPORT_LEGACY=1

CHILD=
FAST_PID=
VOICE_PID=

cleanup() {
    trap - EXIT HUP INT TERM
    [ -n "$CHILD" ] && kill -TERM "$CHILD" 2>/dev/null
    [ -n "$FAST_PID" ] && kill -TERM "$FAST_PID" 2>/dev/null
    [ -n "$VOICE_PID" ] && kill -TERM "$VOICE_PID" 2>/dev/null
    [ -n "$CHILD" ] && wait "$CHILD" 2>/dev/null
    [ -n "$FAST_PID" ] && wait "$FAST_PID" 2>/dev/null
    [ -n "$VOICE_PID" ] && wait "$VOICE_PID" 2>/dev/null
    # PID удаляем только если он всё ещё наш.
    [ "$(cat "$PID" 2>/dev/null)" = "$$" ] && rm -f "$PID"
}
trap cleanup EXIT
trap 'exit 0' HUP INT TERM

if [ -x "$DIR/fast_pause_watch.sh" ]; then
    "$DIR/fast_pause_watch.sh" >> /tmp/fast_pause_watch.log 2>&1 &
    FAST_PID=$!
fi
if [ -x "$DIR/voice_watch.sh" ]; then
    "$DIR/voice_watch.sh" >> /tmp/panel-player-voice.log 2>&1 &
    VOICE_PID=$!
fi

while [ ! -f "$STOP" ]; do
    "$DIR/panel-player" >> "$LOG" 2>&1 &
    CHILD=$!
    wait "$CHILD"
    CHILD=
    [ -f "$STOP" ] && break
    sleep 3
done
