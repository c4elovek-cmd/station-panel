#!/bin/sh
# Запуск панели Станции на Linux / VPS (Pterodactyl, ядро python generic).
#
# Быстрый старт:
#   1. Положить рядом: ./panel (server.py + static + config.json), ./bridge (app.py), ./bin/chisel
#   2. sh start.sh          — он сам поставит зависимости (в ./venv) и запустит всё.
#
# Переменные окружения (необязательны, есть значения по умолчанию):
#   CHISEL_PORT   — порт туннеля колонки (по умолчанию 8080)
#   CHISEL_AUTH   — логин:пароль туннеля (тот же, что в /data/opt/chisel/chisel.env на колонке)
#   PANEL_PORT    — порт панели (8787)
#   BRIDGE_PORT   — порт моста голоса (8811)
#   NO_CHISEL=1   — не запускать туннель (если chisel не нужен)
#   NO_BRIDGE=1   — не запускать мост голоса
set -u
cd "$(dirname "$0")" || exit 1

CHISEL_PORT="${CHISEL_PORT:-8080}"
CHISEL_AUTH="${CHISEL_AUTH:-}"
PANEL_PORT="${PANEL_PORT:-8787}"
BRIDGE_PORT="${BRIDGE_PORT:-8811}"
PY="${PYTHON:-python3}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ---------- 1. Автоустановка зависимостей в локальный venv ----------
# venv живёт прямо в папке сервера; если pip недоступен — пробуем системный python.
if [ ! -x venv/bin/python ] || [ ! -e .deps-installed ]; then
  log "первый запуск: создаю venv и ставлю зависимости (~10-20 сек)"
  "$PY" -m venv venv 2>/dev/null || log "venv недоступен, ставлю в системный python"
  PIP="venv/bin/pip"; [ -x venv/bin/pip ] || PIP="$PY -m pip"
  # лёгкий набор: панель + мост. pylaws/pycaw и прочий Windows-специфичный софт не ставится.
  $PIP install -q --upgrade pip 2>/dev/null
  if [ -f requirements-linux.txt ] && $PIP install -q -r requirements-linux.txt; then
    touch .deps-installed
    log "зависимости установлены"
  else
    log "requirements-linux.txt не сработал, ставлю пакеты по одному"
    $PIP install -q fastapi uvicorn aiohttp asyncssh python-multipart httpx && touch .deps-installed
  fi
fi
PYBIN="venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PY"

# ---------- 2. Держатель процессов: упал — поднять через 5 секунд ----------
supervise() {
  name="$1"
  shift
  while true; do
    log "$name: запуск"
    "$@"
    log "$name: остановился (код $?), перезапуск через 5 с"
    sleep 5
  done
}

# ---------- 3. Туннель chisel (обратный туннель колонки) ----------
if [ "$NO_CHISEL" != "1" ] && [ -x ./bin/chisel ]; then
  if [ -n "$CHISEL_AUTH" ]; then
    supervise chisel ./bin/chisel server --port "$CHISEL_PORT" --reverse --auth "$CHISEL_AUTH" &
  else
    log "CHISEL_AUTH не задан — туннель без пароля (не рекомендуется)"
    supervise chisel ./bin/chisel server --port "$CHISEL_PORT" --reverse &
  fi
else
  log "chisel пропущен (NO_CHISEL=1 или нет ./bin/chisel)"
fi

# ---------- 4. Панель ----------
# В panel/config.json на сервере должно быть: panel_host 0.0.0.0,
# station_ip 127.0.0.1, ssh_port 2222 (порт туннеля).
( cd panel && supervise panel "$PYBIN" server.py ) &

# ---------- 5. Мост голоса (TTS + медиа-команды) ----------
if [ "$NO_BRIDGE" != "1" ] && [ -f bridge/app.py ]; then
  ( cd bridge && supervise bridge "$PYBIN" -m uvicorn app:app --host 0.0.0.0 --port "$BRIDGE_PORT" ) &
else
  log "мост пропущен (NO_BRIDGE=1 или нет bridge/app.py)"
fi

wait
