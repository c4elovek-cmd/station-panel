#!/bin/sh
# Совместимый автозапуск через S99soundya; старые файлы сохранены для отката.
# BusyBox kill не понимает отрицательный PID: сигналим только проверенному PID.
DIR=/data/opt/panel-player
PID=/run/panel-player.pid
STOP=/run/panel-player.stop
LOG=/tmp/panel-player.log

is_running() {
    P=$(cat "$PID" 2>/dev/null)
    case "$P" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$P" 2>/dev/null || return 1
    # PID мог достаться чужому процессу после аварии — такую группу не трогаем.
    grep -aq '/data/opt/panel-player/run.sh' "/proc/$P/cmdline" 2>/dev/null
}

stop_supervisor() {
    touch "$STOP"
    is_running || { rm -f "$PID"; echo 'panel-player остановлен'; return 0; }
    kill -TERM "$P"
    i=0
    while [ "$i" -lt 30 ]; do
        kill -0 "$P" 2>/dev/null || break
        sleep 1
        i=$((i + 1))
    done
    if kill -0 "$P" 2>/dev/null; then
        kill -KILL "$P" 2>/dev/null
        sleep 1
    fi
    rm -f "$PID"
    echo 'panel-player остановлен'
}

case "${1:-}" in
    start)
        is_running && { echo 'panel-player уже работает'; exit 0; }
        rm -f "$STOP"
        nohup setsid "$DIR/run.sh" </dev/null >> "$LOG" 2>&1 &
        ;;
    stop)
        stop_supervisor
        ;;
    restart)
        "$0" stop
        "$0" start
        ;;
    status)
        is_running && echo 'panel-player работает' || { echo 'panel-player остановлен'; exit 1; }
        ;;
    *) echo 'Использование: start|stop|restart|status' ;;
esac
