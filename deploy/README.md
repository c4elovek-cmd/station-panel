# Панель Станции — деплой

Два способа. Оба лёгкие: проект — два python-файла и статика, зависимостей минимум.

---

## Способ 1: обычный Linux-сервер (VPS с root) — `install.sh`

Одна команда:

```sh
sudo sh install.sh
```

Скрипт сам: поставит python3/ffmpeg/ssh (apt/apk/dnf), скопирует файлы в `/opt/station`,
создаст venv, поставит pip-зависимости и заведёт systemd-службы. Панель будет на
`http://<IP сервера>:8787`, автозапуск при загрузке.

Зависимости (ставятся сами): `fastapi, uvicorn, aiohttp, asyncssh, python-multipart, httpx` (+ опционально `zeroconf`, `edge-tts`).
Системные: `python3, ffmpeg, openssh-client`. pycaw/comtypes не нужны — это Windows-специфика.

Переменные (необязательно): `STATION_DIR=/opt/station`, `PANEL_PORT=8787`, `BRIDGE_PORT=8811`, `CHISEL_PORT=8080`, `CHISEL_AUTH=station:<токен>`.

Управление: `systemctl status|restart|stop station-panel station-bridge`.

---

## Способ 2: хостинг с Pterodactyl (ядро python generic) — `start.sh`

1. Создать сервер, ядро **python generic**. Порты: 8080 (туннель), 8787 (панель), 8811 (мост).
2. Через файловый менеджер или SFTP загрузить рядом со скриптом:

   ```
   start.sh
   requirements-linux.txt
   bin/chisel          ← бинарник linux amd64 (можно пропустить, если туннель не нужен)
   panel/              ← server.py, static/, config.json, requirements-linux.txt
   bridge/             ← app.py
   ```

3. Startup Command: `sh start.sh`.
4. Первый запуск сам создаёт venv и ставит зависимости (10–20 сек в логе). Никаких ручных pip.
5. В `panel/config.json` на сервере: `panel_host: "0.0.0.0"`, `station_ip: "127.0.0.1"`, `ssh_port: 2222`.

Флаги: `NO_CHISEL=1` — не поднимать туннель, `NO_BRIDGE=1` — не поднимать мост.

---

## Схема работы туннеля

```
колонка  ──chisel client──▶  сервер:8080 (chisel server)
   ▲                              │
   └───── SSH :2222 ◀─────────────┘   (порт туннеля внутри сервера)
                                  │
                              панель :8787 ──▶ 127.0.0.1:2222
```

Роутер не трогается, SSH колонки наружу не открывается.

На колонке chisel уже настроен (`/data/opt/chisel`, `/etc/init.d/S97chisel`).
В `/data/opt/chisel/chisel.env` укажи `SERVER_URL=ws://<IP сервера>:8080` и тот же
токен, что в `CHISEL_AUTH`.

---

## Обновление версии

Способ 1: `sudo sh install.sh` поверх — файлы перезапишутся, venv переиспользуется.
Способ 2: перезалить `panel/server.py` и `static/`, перезапустить сервер кнопкой в панели хостинга.

## Ошибки

- **первый запуск висит на pip** — у хостинга закрыт выход в pypi; разово поставить пакеты локально и залить папку venv целиком.
- **панель не открывается** — проверь `panel/config.json` → `panel_host: "0.0.0.0"`, и что порт открыт в файрволе хостинга.
- **колонка не подключается по туннелю** — на колонке `chisel.env` → `SERVER_URL` и токен должны совпадать с сервером; проверить: `curl http://<сервер>:8080` из внешней сети.
