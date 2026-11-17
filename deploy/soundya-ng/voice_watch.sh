#!/bin/sh
# Свежие музыкальные BUSY из maind; произвольная речь и ответы HTTP не журналируются.
# Алиса может отвечать одновременно: cancel и IPC здесь намеренно не используются.

LOG=${VOICE_WATCH_LOG:-/data/quasar/daemons_logs/maind.log}
LOCK=${VOICE_WATCH_LOCK:-/tmp/voice_watch.lock}
CURL=${VOICE_WATCH_CURL:-/data/opt/entware/bin/curl}
BASE=http://127.0.0.1:8765
SEEN_EVENTS=
RECENT_COMMANDS=

note() {
    printf '%s voice-watch: %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*"
}

map_phrase() {
    CMD=
    LABEL=
    # BusyBox не умеет менять регистр кириллицы: перечисляем начальную капитализацию.
    case "$PHRASE" in
        Стоп|стоп|Stop|stop) CMD=stop; LABEL=stop ;;
        Пауза|пауза|Pause|pause) CMD=pause; LABEL=pause ;;
        Продолжай|продолжай|Продолжи|продолжи|Продолжить|продолжить|Resume|resume)
            CMD=resume; LABEL=resume ;;
        Следующий|следующий|'Следующий трек'|'следующий трек'|Дальше|дальше|Next|next)
            CMD=next; LABEL=next ;;
        Предыдущий|предыдущий|'Предыдущий трек'|'предыдущий трек'|Назад|назад|Prev|prev)
            CMD=prev; LABEL=prev ;;
        'Включи любимое'|'включи любимое'|Любимое|любимое|\
        'Включи мои любимые треки'|'включи мои любимые треки')
            CMD='account other likes'; LABEL=other-likes ;;
        'Мои лайки'|'мои лайки'|'Включи мои лайки'|'включи мои лайки')
            CMD='account me likes'; LABEL=me-likes ;;
        'Моя волна'|'моя волна'|'Включи мою волну'|'включи мою волну')
            CMD='account me recs'; LABEL=me-recs ;;
        'Любимая волна'|'любимая волна'|'Волна брата'|'волна брата'|\
        'Включи волну брата'|'включи волну брата')
            CMD='account other recs'; LABEL=other-recs ;;
        Волна|волна|'Включи волну'|'включи волну') CMD=recs; LABEL=active-recs ;;
        'Включи scorpions'|'включи scorpions'|'Включи Scorpions'|'включи Scorpions')
            CMD='play scorpions'; LABEL=search ;;
        'Включи трек '*|'включи трек '*|\
        'Включи песню '*|'включи песню '*|\
        'Включи музыку '*|'включи музыку '*)
            QUERY=${PHRASE#* }
            QUERY=${QUERY#* }
            # Никаких переводов строк, управления терминалом или маршрутов чужих сервисов.
            case "$QUERY" in
                ''|*[[:cntrl:]]*) return 0 ;;
            esac
            [ "$(printf '%s' "$QUERY" | tr -d '[:space:]')" ] || return 0
            # tr меняет только ASCII; русские имена проверяем отдельными паттернами.
            ROUTE=$(printf '%s' "$QUERY" | tr 'A-Z' 'a-z')
            case "$ROUTE" in
                *youtube*|*youtu.be*|*yandex*|*Яндекс*|*яндекс*|\
                *Ютуб*|*ютуб*|*Ютьюб*|*ютьюб*|*Радио*|*радио*) return 0 ;;
            esac
            CMD="play $QUERY"; LABEL=search ;;
    esac
}

handle_line() {
    LINE=$1
    # Литералы в кавычках: [sdk]/[info] не должны стать классами символов glob.
    Q="'"
    case "$LINE" in
        "["*"] ["*"] [sdk] [info] [alice_state_machine.cpp:"*"] onAliceStateChanged: BUSY, recognizedPhrase=$Q"*"$Q") ;;
        *) return 0 ;;
    esac
    # Только BUSY: LISTENING/SPEAKING и другие состояния не трогаем.
    STATE_TAIL=${LINE#*"onAliceStateChanged: "}
    case "$STATE_TAIL" in
        BUSY,*|BUSY) ;;
        *) return 0 ;;
    esac
    # Закрывающая кавычка уже проверена; апострофы внутри названия песни сохраняем.
    MARK=${LINE#*"onAliceStateChanged: BUSY, recognizedPhrase=$Q"}
    [ "$MARK" = "$LINE" ] && return 0
    PHRASE=${MARK%"$Q"}
    [ -n "$PHRASE" ] || return 0
    # [PID:TID] [YYYY-MM-DD HH:MM:SS.mmm] — UUID в этом событии нет.
    REQUEST=${LINE%%]*}
    REQUEST=${REQUEST#\[}
    case "$REQUEST" in
        *[!0-9:]*|''|:*|*:) return 0 ;;
        *:*) ;;
        *) return 0 ;;
    esac
    # Дата-время — второй квадратный сегмент; что после него, не трогаем.
    REST=${LINE#*] [}
    EVENT=${REST%%]*}
    case "$EVENT" in
        ????-??-??' '??:??:??.???) ;;
        *) return 0 ;;
    esac
    STAMP=${EVENT%.*}
    WHEN=$(date -u -d "$STAMP" +%s 2>/dev/null) || return 0
    NOW=${VOICE_WATCH_NOW:-$(date -u +%s)}
    case "$NOW" in ''|*[!0-9]*) return 0 ;; esac
    AGE=$((NOW - WHEN))
    [ "$AGE" -ge -2 ] && [ "$AGE" -le 5 ] || return 0

    map_phrase
    [ -n "$CMD" ] || return 0

    # Дедуп ключ = timestamp + PID:TID (UUID в событии нет): повтор строки
    # при ротации/перезапуске tail подавляется, старые ротации отсекает свежесть.
    KEY="$EVENT/$REQUEST"
    case "
$SEEN_EVENTS
" in *"
$KEY
"*) return 0 ;; esac
    SEEN_EVENTS=$(printf '%s\n%s\n' "$SEEN_EVENTS" "$KEY" | tail -n 32)

    # Миллисекунды важны на границе 2 с; «1»-префикс исключает восьмеричное число.
    FRACTION=${EVENT##*.}
    case "$FRACTION" in *[!0-9]*) return 0 ;; esac
    EVENT_MS=$((WHEN * 1000 + 1$FRACTION - 1000))
    # По каждой команде отдельно; запрос остаётся только в памяти текущего процесса.
    # read -r вместо awk -v: обратные слеши в запросе не интерпретируются как escape.
    PREVIOUS=$(printf '%s\n' "$RECENT_COMMANDS" | while IFS= read -r ENTRY; do
        [ "${ENTRY#* }" = "$CMD" ] && printf '%s\n' "${ENTRY%% *}"
    done | tail -n 1)
    if [ -n "$PREVIOUS" ]; then
        GAP=$((EVENT_MS - PREVIOUS))
        [ "$GAP" -ge -2000 ] && [ "$GAP" -le 2000 ] && return 0
    fi
    # Не ретраим автоматически даже при HTTP-ошибке: команда могла успеть выполниться.
    RECENT_COMMANDS=$(printf '%s\n%s %s\n' "$RECENT_COMMANDS" "$EVENT_MS" "$CMD" | tail -n 32)
    # HTTP-клиент не должен читать поток журнала/FIFO, даже через shell-обёртку.
    RESULT=$("$CURL" --noproxy '*' -fsS --connect-timeout 1 --max-time 3 \
        -H 'Content-Type: text/plain' --data-binary "$CMD" "$BASE/api/cmd" </dev/null 2>/dev/null) || {
        note "$LABEL: HTTP-ошибка"
        return 0
    }
    case "$RESULT" in
        OK*) note "$LABEL: OK" ;;
        *) note "$LABEL: команда не подтверждена" ;;
    esac
    return 0
}

if [ "${1:-}" = --handle-line ]; then
    # Тестовый режим: без lock, FIFO и наблюдения за рабочим журналом.
    handle_line "${2:-}"
    exit 0
fi
if [ "${1:-}" = --handle-lines ]; then
    while IFS= read -r LINE || [ -n "$LINE" ]; do
        handle_line "$LINE"
    done
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
    # Только свои известные файлы; неизвестные данные не удаляем рекурсивно.
    rm -f "$LOCK/pid" "$LOCK/events"
    rmdir "$LOCK" 2>/dev/null || exit 1
    mkdir "$LOCK" 2>/dev/null || exit 1
fi
printf '%s\n' "$$" > "$LOCK/pid" || exit 1
TAIL_PID=
cleanup() {
    trap - EXIT HUP INT TERM
    [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null
    [ -n "$TAIL_PID" ] && wait "$TAIL_PID" 2>/dev/null
    # Не удаляем lock, если его уже заменил другой владелец.
    [ ! -L "$LOCK" ] && [ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ] || return 0
    rm -f "$LOCK/events" "$LOCK/pid"
    rmdir "$LOCK" 2>/dev/null
}
trap cleanup EXIT
trap 'exit 0' HUP INT TERM
mkfifo "$LOCK/events" || exit 1
note 'запущен: свежий музыкальный BUSY; cancel не используется'
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
