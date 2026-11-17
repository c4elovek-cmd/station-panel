#!/bin/sh
# Мок curl для теста voice_watch: пишет тело команды в файл-журнал.
OUT="${MOCK_CURL_OUT:-$(dirname "$0")/commands}"
# Тело придёт после --data-binary:BusyBox-параметра, обычно последним аргументом.
BODY=
prev=
for arg in "$@"; do
    [ "$prev" = "--data-binary" ] && BODY=$arg
    prev=$arg
done
printf '%s\n' "$BODY" >> "$OUT"
printf 'OK\n'
exit 0
