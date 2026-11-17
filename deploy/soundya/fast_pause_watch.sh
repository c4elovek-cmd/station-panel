#!/bin/sh
# Локальная «пауза» из fast-command споттера maind, без новых IPC-компонентов.
# Подтверждён только player.pause: другие интенты не угадываем и не перехватываем.
# Алиса сама завершает свой запрос; cancel здесь намеренно отсутствует.

DIR=${SOUNDYA_DIR:-/data/opt/soundya}
LOG=${FAST_PAUSE_LOG:-/data/quasar/daemons_logs/maind.log}
LOCK=${FAST_PAUSE_LOCK:-/tmp/fast_pause_watch.lock}
CURL=${FAST_PAUSE_CURL:-/data/opt/entware/bin/curl}
BASE=http://127.0.0.1:8765
LAST_EVENT=

note() {
    printf '%s fast-pause: %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*"
}

handle_line() {
    case "$1" in
        *'[fast_command_classifier.cpp:'*'] Parsed frames=[personal_assistant.scenarios.player.pause]') ;;
        *) return 0 ;;
    esac
    # Ротация может повторить строку или отдать старый файл: пропускаем старые события.
    EVENT=$(printf '%s\n' "$1" | cut -d '[' -f 3 | cut -c 1-23)
    [ "$EVENT" = "$LAST_EVENT" ] && return 0
    STAMP=$(printf '%s' "$EVENT" | cut -c 1-19)
    WHEN=$(date -u -d "$STAMP" +%s 2>/dev/null) || return 0
    NOW=${FAST_PAUSE_NOW:-$(date -u +%s)}
    AGE=$((NOW - WHEN))
    [ "$AGE" -ge -2 ] && [ "$AGE" -le 5 ] || return 0
    LAST_EVENT=$EVENT

    STATUS=$("$CURL" --noproxy '*' -fsS --connect-timeout 1 --max-time 2 "$BASE/api/cmd" \
        -H 'Content-Type: text/plain' --data-binary status 2>/dev/null) || {
        note 'событие pause: HTTP-статус soundya недоступен'
        return 0
    }
    case "$STATUS" in
        playing:*|playing) ;;
        *) note 'событие pause пропущено: soundya не играет'; return 0 ;;
    esac
    # Обычный pause сохраняет защиту музыкального будильника внутри soundya.
    RESULT=$("$CURL" --noproxy '*' -fsS --connect-timeout 1 --max-time 3 \
        -H 'Content-Type: text/plain' --data-binary pause "$BASE/api/cmd" 2>/dev/null) || {
        note 'событие pause: HTTP-команда завершилась ошибкой'
        return 0
    }
    if [ "$RESULT" = OK ]; then
        note 'player.pause -> soundya pause: OK'
    else
        # Ответ может содержать пользовательский текст: в журнал его не копируем.
        note 'soundya не подтвердил pause (возможно, играет будильник)'
    fi
    return 0
}

if [ "${1:-}" = --handle-line ]; then
    # Тестовый режим: один проход с подменённым curl, без запуска наблюдателя.
    handle_line "${2:-}"
    exit 0
fi

[ -x "$CURL" ] || { note 'curl не найден'; exit 1; }
umask 077
if ! mkdir "$LOCK" 2>/dev/null; then
    [ ! -L "$LOCK" ] || { note 'лок-ссылка: требуется ручная проверка'; exit 1; }
    OWNER=$(cat "$LOCK/pid" 2>/dev/null)
    case "$OWNER" in
        ''|0|*[!0-9]*) note 'лок без корректного PID: требуется ручная проверка'; exit 1 ;;
    esac
    kill -0 "$OWNER" 2>/dev/null && exit 0
    # Убираем только свои известные файлы; чужие данные не удаляем рекурсивно.
    rm -f "$LOCK/pid" "$LOCK/events"
    rmdir "$LOCK" 2>/dev/null || exit 1
    mkdir "$LOCK" 2>/dev/null || exit 1
fi
printf '%s\n' "$$" > "$LOCK/pid"
TAIL_PID=
cleanup() {
    trap - EXIT HUP INT TERM
    [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null
    [ -n "$TAIL_PID" ] && wait "$TAIL_PID" 2>/dev/null
    # Не удаляем лок, если его уже заменил другой владелец.
    [ ! -L "$LOCK" ] && [ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ] || return 0
    rm -f "$LOCK/events" "$LOCK/pid"
    rmdir "$LOCK" 2>/dev/null
}
trap cleanup EXIT
trap 'exit 0' HUP INT TERM
mkfifo "$LOCK/events" || exit 1
note 'наблюдатель запущен; только свежий player.pause; IPC не используется'
while true; do
    [ -f "$LOG" ] || { sleep 2; continue; }
    tail -F -n 0 -s 1 "$LOG" > "$LOCK/events" 2>/dev/null &
    TAIL_PID=$!
    while IFS= read -r LINE; do
        handle_line "$LINE"
    done < "$LOCK/events"
    wait "$TAIL_PID" 2>/dev/null
    TAIL_PID=
    sleep 2
done
