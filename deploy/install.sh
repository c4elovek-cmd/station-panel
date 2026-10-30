#!/bin/sh
# Установка панели Станции на Linux одной командой:
#   sudo sh install.sh
# Что делает:
#   - ставит системные пакеты (python3, venv, pip, ffmpeg, openssh-client) — если есть apt/apk/dnf
#   - копирует panel/, bridge/, bin/ в /opt/station (или $STATION_DIR)
#   - создаёт venv и ставит python-зависимости
#   - создаёт systemd-службы station-panel.service (панель) и при наличии chisel — station-chisel
#   - запускает их и печатает адрес панели
set -e
cd "$(dirname "$0")"

STATION_DIR="${STATION_DIR:-/opt/station}"
PANEL_PORT="${PANEL_PORT:-8787}"
BRIDGE_PORT="${BRIDGE_PORT:-8811}"
CHISEL_PORT="${CHISEL_PORT:-8080}"
CHISEL_AUTH="${CHISEL_AUTH:-}"

echo "[*] Панель Станции — установка в $STATION_DIR"

# ---------- системные пакеты ----------
if command -v apt-get >/dev/null 2>&1; then
  echo "[*] apt: ставлю python3, venv, ffmpeg, openssh-client"
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip ffmpeg openssh-client >/dev/null
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache python3 py3-pip ffmpeg openssh-client >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-pip ffmpeg openssh-clients >/dev/null
else
  echo "[!] пакетный менеджер не найден — предполагаю, что python3/ffmpeg/ssh уже есть"
fi

# ---------- файлы ----------
mkdir -p "$STATION_DIR"
for d in panel bridge bin; do
  [ -d "$d" ] && cp -r "$d" "$STATION_DIR/"
done
# requirements лежит в корне проекта, start.sh — рядом с install.sh
[ -f ../requirements-linux.txt ] && cp ../requirements-linux.txt "$STATION_DIR/"
[ -f requirements-linux.txt ] && cp requirements-linux.txt "$STATION_DIR/"
cp start.sh "$STATION_DIR/"
chmod +x "$STATION_DIR/bin/chisel" 2>/dev/null || true
cd "$STATION_DIR"

# ---------- python-зависимости ----------
echo "[*] создаю venv и ставлю зависимости"
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements-linux.txt

# ---------- config панели ----------
if [ -f panel/config.json ]; then
  python3 - <<'EOF'
import json
p = "panel/config.json"
c = json.load(open(p))
c["panel_host"] = "0.0.0.0"
c["panel_port"] = int(__import__("os").environ.get("PANEL_PORT", "8787"))
c["station_ip"] = c.get("station_ip") or "127.0.0.1"
c["ssh_port"] = int(c.get("ssh_port") or 2222) if c.get("station_ip") == "127.0.0.1" else c.get("ssh_port")
json.dump(c, open(p, "w"), ensure_ascii=False, indent=2)
print("[*] panel/config.json: panel_host=0.0.0.0")
EOF
fi

# ---------- systemd ----------
SYSTEMD_UNIT="[Unit]
Description=Station Panel (панель управления колонкой)
After=network.target

[Service]
WorkingDirectory=$STATION_DIR/panel
ExecStart=$STATION_DIR/venv/bin/python server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target"
echo "$SYSTEMD_UNIT" > /etc/systemd/system/station-panel.service
systemctl daemon-reload
systemctl enable --now station-panel

if [ -x "$STATION_DIR/bin/chisel" ] && [ -n "$CHISEL_AUTH" ]; then
  CHISEL_UNIT="[Unit]
Description=Station chisel tunnel server
After=network.target

[Service]
WorkingDirectory=$STATION_DIR
ExecStart=$STATION_DIR/bin/chisel server --port $CHISEL_PORT --reverse --auth $CHISEL_AUTH
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target"
  echo "$CHISEL_UNIT" > /etc/systemd/system/station-chisel.service
  systemctl daemon-reload
  systemctl enable --now station-chisel
fi

# ---------- мост (второй сервис) ----------
BRIDGE_UNIT="[Unit]
Description=Station Voice Bridge (TTS)
After=network.target

[Service]
WorkingDirectory=$STATION_DIR/bridge
ExecStart=$STATION_DIR/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port $BRIDGE_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target"
if [ -f "$STATION_DIR/bridge/app.py" ]; then
  echo "$BRIDGE_UNIT" > /etc/systemd/system/station-bridge.service
  systemctl daemon-reload
  systemctl enable --now station-bridge
fi

sleep 2
systemctl --no-pager --lines 3 status station-panel | head -8
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "[✓] Готово. Панель:  http://${IP:-127.0.0.1}:$PANEL_PORT"
echo "    Логин и пароль — в panel/password.txt (пароль по умолчанию задаётся при первом входе)."
echo "    Сервисы: station-panel, station-bridge$( [ -n "$CHISEL_AUTH" ] && echo ', station-chisel' )."
echo "    Остановить: systemctl stop station-panel station-bridge"
