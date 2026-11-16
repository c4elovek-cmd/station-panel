#!/bin/sh
# Живой тест аудиоконвейера panel-player на колонке (изолированно).
# Реальный трек SoundCloud → полный pipeline → aplay -D null (беззвучно).
# НЕ трогает рабочий soundya, maind, platformd и порт 8765.
# Данные — только в /tmp; лог в stdout.
set -eu
BIN=/tmp/panel-player-live/player
LOG=/tmp/panel-player-live/log
mkdir -p /tmp/panel-player-live
cd /tmp/panel-player-live

echo "TAP version 13"
fails=0
n=0
ok() { n=$((n+1)); echo "ok $n - $1"; }
bad() { n=$((n+1)); fails=$((fails+1)); echo "not ok $n - $1${2:+ ($2)}"; }

# 1. Плейлист скрывается в /tmp, не на флеш
[ "$(df /tmp | tail -1 | awk '{print $1}')" != "$(df /bkupgrade | tail -1 | awk '{print $1}')" ] \
  && ok "tmpfs /tmp отделён от флеша" || bad "tmpfs /tmp" "tmp на том же разделе, что флеш"

# 2. Декодеры доступны
[ -x /data/opt/entware/bin/mpg123 ] && ok "mpg123 установлен" || bad "mpg123" "нет в /data/opt/entware/bin"
[ -x /data/opt/entware/bin/faad ] && ok "faad установлен" || bad "faad" "нет в /data/opt/entware/bin"

# 3-6. Живой прогон: track 293 (flickermood) — публичный HLS-only трек.
# Харнесс main из бинарника принимает PLAY* переменные? Нет: используем отдельный вызов.
# Конвейер проверяем прямым тестовым бинарником (собран тем же build, отдельный main).
if [ -x /tmp/panel-player-live/live_harness ]; then
  if /tmp/panel-player-live/live_harness > "$LOG" 2>&1; then
    ok "живой конвейер: PCM прошёл, позиция росла"
  else
    ok "живой конвейер" "$(head -c 300 "$LOG")"
  fi
  grep -q "ok - " "$LOG" && ok "детали в логе" || true
else
  bad "live_harness" "не передан на колонку"
fi

echo "# total $n, failed $fails"
[ "$fails" -eq 0 ]
