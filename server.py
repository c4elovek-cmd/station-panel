#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Веб-панель для Яндекс Станции (Мини 2 и другие модели с root).

Что умеет:
  * вход по паролю (сессии на cookie, пароль задаётся в панели);
  * SSH-терминал к колонке прямо в браузере (WebSocket + pty);
  * менеджер пакетов YAM: список, установка .yap, запуск/остановка/удаление;
  * управление подсветкой (6 диодов на Мини 2): цвета, пресеты, выключение;
  * датчики с графиком (освещённость, температура);
  * статус системы: аптайм, память, диск, температура, версия YAM;
  * настройки: IP/порты станции, профиль и число диодов, разрешение бинарников,
    автообновление YAM, смена пароля панели, перезагрузка колонки.

Проксирует API демона yamd (порт 8099) и ходит на колонку по SSH (порт 2222).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import aiohttp
import asyncssh
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
PANEL_BIN = BASE / "bin"      # инструменты, скачанные из «Чек-листа» (ffmpeg, yt-dlp)
PANEL_BUNDLES = BASE / "bundles"  # бинари для колонки, поставляемые с панелью


def bin_tool(name: str) -> str | None:
    """Путь к инструменту: сначала bin/ панели, затем PATH."""
    local = PANEL_BIN / (name + (".exe" if os.name == "nt" else ""))
    if local.exists():
        return str(local)
    return shutil.which(name)
CONFIG_PATH = BASE / "config.json"
STATIC_DIR = BASE / "static"
PASSWORD_HINT = BASE / "password.txt"
PLUGIN_CONFIG_DIR = BASE / "plugin-configs"

DEFAULTS: dict[str, Any] = {
    "station_ip": "192.168.0.172",
    "ssh_port": 2222,
    "ssh_user": "root",
    "yam_port": 8099,
    "wakeword_port": 8780,
    "panel_port": 8787,
    "panel_host": "0.0.0.0",
    "password_salt": "",
    "password_hash": "",
    "session_hours": 72,
    "voice_phrases": ["дипсик"],
    "voice_bridge_url": "",
    "voice_bridge_token": "",
    "voice_music_source": "youtube",
    # --- локальный голосовой мост (voice-bridge встроен в панель) ---
    "bridge_enabled": False,
    "ipc_media_listen": True,  # фразы «стоп/пауза/дальше» без «Алисы» -> мост
    "ipc_media_dedupe_sec": 5,
    "bridge_path": "",  # путь к папке voice-bridge; пусто = ../voice-bridge рядом с панелью
    "llm_provider": "deepseek-web",  # deepseek-web | openrouter | none
    "deepseek_token": "",   # bearer userToken из localStorage chat.deepseek.com
    "deepseek_cookie": "",  # куки веб-чата (aws-waf-token и т.п.), формат "a=b; c=d"
    "llm_key": "",          # ключ OpenRouter (резерв)
    "llm_model": "deepseek/deepseek-chat-v3-0324:free",
    "voice_proxy": "",      # прокси для yt-dlp (например socks5://...)
}

SESSIONS: dict[str, float] = {}  # token -> expires_at
_ssh_lock = asyncio.Lock()

# «Закреплённое» время LED-часов: вендорный maind перерисовывает реальное время
# каждые ~60 с, поэтому панель периодически подтверждает кастомное значение.
clock_pin: dict[str, Any] = {"time": "", "brightness": 170, "active": False}
clock_night: dict[str, Any] = {
    "enabled": False, "start": "23:00", "end": "07:00",
    "night_brightness": 12, "day_brightness": 170, "was_night": False,
}


# --------------------------------------------------------------------------- #
# конфиг
# --------------------------------------------------------------------------- #
def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:  # битый конфиг не должен валить панель
            print(f"[!] config.json не читается: {exc}", file=sys.stderr)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def init_config() -> dict[str, Any]:
    cfg = load_config()
    changed = False
    if not cfg["password_hash"]:
        cfg["password_salt"] = secrets.token_hex(16)
        generated = secrets.token_urlsafe(9)
        cfg["password_hash"] = hash_password(generated, cfg["password_salt"])
        PASSWORD_HINT.write_text(
            "Пароль веб-панели Станции (смените его в панели):\n" + generated + "\n",
            encoding="utf-8",
        )
        print(f"[*] Пароль панели создан и записан в {PASSWORD_HINT}: {generated}")
        changed = True
    if changed or not CONFIG_PATH.exists():
        save_config(cfg)
    return cfg


cfg = init_config()
app = FastAPI(title="Station Panel", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------- #
# Медиа-фразы без слова активации («стоп», «пауза», «дальше»…): слушаем IPC
# колонки (127.0.0.1:9875) и финальные результаты распознавания без «алисы»
# переводим в команды моста. Вендорский споттер рисует значок, но штатным
# плеером не управляет (играет soundya/spotiya) — здесь доводим до плеера.
# --------------------------------------------------------------------------- #
IPC_MEDIA_PHRASES = {
    "стоп": "stop",
    "пауза": "pause",
    "паузу": "pause",
    "дальше": "next",
    "следующий": "next",
    "следующий трек": "next",
    "вперёд": "next",
    "назад": "prev",
    "предыдущий": "prev",
    "предыдущий трек": "prev",
    "продолжи": "play",
    "продолжай": "play",
    "играть": "play",
}
_media_listener_task: asyncio.Task | None = None


async def _ipc_media_listener() -> None:
    """Долгоживущий слушатель IPC: переподключается, шлёт совпавшие фразы мосту."""
    last_sent: dict[str, float] = {}
    while True:
        bridge_base = str(cfg.get("voice_bridge_url") or "").split("/api/voice")[0].rstrip("/")
        token = str(cfg.get("voice_bridge_token") or "")
        if not (cfg.get("ipc_media_listen") and bridge_base and token):
            await asyncio.sleep(5)
            continue
        conn = None
        try:
            conn = await ssh_connect()
            reader, writer = await conn.open_connection("127.0.0.1", 9875)
            # ВАЖНО: без кадра «подписка на AliceCapability» — maind считает
            # подписчика компонентом речи и уводит активации «Алисы» ему
            # («не отвечает даже на слово алиса»). Текстовые события
            # распознавания приходят широковещательно и без подписки.
            print("[ipc-media] слушаю фразы без «Алисы»")
            while True:
                size = int.from_bytes(await asyncio.wait_for(reader.readexactly(4), 30), "little")
                if not 0 < size <= 512 * 1024:
                    break
                envelope = _ipc_parse(await asyncio.wait_for(reader.readexactly(size), 30))
                if envelope.get(1) not in (1, 3) or not isinstance(envelope.get(2), bytes):
                    continue  # интересуют события распознавания с текстом
                text = envelope[2].decode("utf-8", "replace").strip().lower()
                if not text or "алиса" in text:
                    continue
                cmd = IPC_MEDIA_PHRASES.get(text)
                if not cmd:
                    continue
                now = time.time()
                if now - last_sent.get(cmd, 0) < float(cfg.get("ipc_media_dedupe_sec", 5)):
                    continue  # тот же финал может прийти повторно
                last_sent[cmd] = now
                timeout = aiohttp.ClientTimeout(total=12)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    await session.post(bridge_base + "/api/media",
                                       json={"media": cmd},
                                       headers={"authorization": f"Bearer {token}"})
                print(f"[ipc-media] «{text}» -> {cmd}")
        except Exception:
            pass  # колонка офлайн/туннель рвался — переподключимся
        finally:
            if conn:
                conn.close()
        await asyncio.sleep(5)


@app.get("/api/ipc_media_listen")
async def api_ipc_media_listen_get(request: Request) -> JSONResponse:
    require_auth(request)
    return JSONResponse({"enabled": bool(cfg.get("ipc_media_listen"))})


@app.put("/api/ipc_media_listen")
async def api_ipc_media_listen_put(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    cfg["ipc_media_listen"] = bool(data.get("enabled"))
    save_config(cfg)
    return JSONResponse({"ok": True, "enabled": bool(cfg["ipc_media_listen"])})


@app.on_event("startup")
async def _start_background_loops() -> None:
    """Фоновые задачи: закрепление времени, ночной профиль, синхронизация громкости."""
    asyncio.create_task(_clock_pin_loop())
    asyncio.create_task(_night_clock_loop())
    asyncio.create_task(_volume_sync_loop())
    asyncio.create_task(_ipc_media_listener())


# --------------------------------------------------------------------------- #
# авторизация
# --------------------------------------------------------------------------- #
def new_session() -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = time.time() + cfg["session_hours"] * 3600
    return token


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    exp = SESSIONS.get(token)
    if not exp:
        return False
    if exp < time.time():
        SESSIONS.pop(token, None)
        return False
    return True


def check_password(password: str) -> bool:
    return secrets.compare_digest(
        hash_password(password, cfg["password_salt"]), cfg["password_hash"]
    )


def require_auth(request: Request) -> None:
    if not session_valid(request.cookies.get("sp_session")):
        raise HTTPException(status_code=401, detail="не авторизован")


@app.get("/login")
async def login_page() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "login.html").read_text(encoding="utf-8"))


@app.post("/api/login")
async def api_login(request: Request) -> JSONResponse:
    data = await request.json()
    if not check_password(str(data.get("password", ""))):
        raise HTTPException(status_code=403, detail="неверный пароль")
    token = new_session()
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "sp_session", token, httponly=True, samesite="lax", max_age=cfg["session_hours"] * 3600
    )
    return response


@app.post("/api/logout")
async def api_logout(request: Request) -> JSONResponse:
    SESSIONS.pop(request.cookies.get("sp_session", ""), None)
    response = JSONResponse({"ok": True})
    response.delete_cookie("sp_session")
    return response


@app.get("/")
async def index(request: Request) -> Response:
    if not session_valid(request.cookies.get("sp_session")):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "index.html")


# --------------------------------------------------------------------------- #
# SSH к колонке (по ключу, с откатом на пустой пароль dropbear -B)
# --------------------------------------------------------------------------- #
KEY_DIR = BASE / "keys"
KEY_PRIV = KEY_DIR / "id_ed25519"
KEY_PUB = KEY_DIR / "id_ed25519.pub"


async def ssh_connect():
    """Подключиться к колонке: сначала пробуем свой SSH-ключ, затем пустой пароль."""
    options = dict(
        host=cfg["station_ip"],
        port=cfg["ssh_port"],
        username=cfg["ssh_user"],
        known_hosts=None,
        login_timeout=10,
    )
    if KEY_PRIV.exists():
        try:
            return await asyncssh.connect(**options, client_keys=[str(KEY_PRIV)], password="")
        except (asyncssh.PermissionDenied, asyncssh.DisconnectError):
            pass  # ключ ещё не установлен или отвергнут — пробуем пустой пароль
    return await asyncssh.connect(**options, client_keys=None, password="")


async def ssh_run(command: str, timeout: float = 20.0) -> tuple[int, str, str]:
    """Выполнить команду на колонке по SSH и вернуть (код, stdout, stderr)."""
    try:
        async with await ssh_connect() as conn:
            result = await asyncio.wait_for(conn.run(command, check=False, encoding="utf-8"), timeout)
            return result.exit_status or 0, result.stdout or "", result.stderr or ""
    except asyncio.TimeoutError:
        return 124, "", "таймаут выполнения команды"
    except Exception as exc:
        return 255, "", f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# прокси к демону YAM (порт 8099)
# --------------------------------------------------------------------------- #
@app.api_route("/yam/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def yam_proxy(path: str, request: Request) -> Response:
    require_auth(request)
    url = f"http://{cfg['station_ip']}:{cfg['yam_port']}/{path}"
    body = await request.body()
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(request.method, url, data=body or None, headers=headers) as resp:
                payload = await resp.read()
                return Response(
                    content=payload,
                    status_code=resp.status,
                    media_type=resp.headers.get("content-type", "application/json"),
                )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"демон YAM недоступен: {exc}")


# --------------------------------------------------------------------------- #
# прокси к демону своих слов активации (wakeword, порт 8780)
# --------------------------------------------------------------------------- #
@app.api_route("/wakeword/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def wakeword_proxy(path: str, request: Request) -> Response:
    """Проксирует API wakeword-демона, чтобы панель была единственным интерфейсом.

    Демон отдаёт и JSON, и WAV-записи (прослушивание), поэтому тело пробрасывается
    как есть, вместе с content-type. Запись фона (8 с) и речи (20 с) — блокирующие,
    отсюда увеличенный таймаут.
    """
    require_auth(request)
    url = f"http://{cfg['station_ip']}:{cfg.get('wakeword_port', 8780)}/{path}"
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = {}
    if body:
        headers["content-type"] = request.headers.get("content-type", "application/json")
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(request.method, url, data=body or None, headers=headers) as resp:
                payload = await resp.read()
                return Response(
                    content=payload,
                    status_code=resp.status,
                    media_type=resp.headers.get("content-type", "application/json"),
                )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"демон слов активации недоступен: {exc}")


# --------------------------------------------------------------------------- #
# статус и служебные ручки
# --------------------------------------------------------------------------- #
_status_cache: dict[str, Any] = {"ts": 0.0, "data": None}


def valid_plugin_id(plugin_id: str) -> str:
    value = plugin_id.strip()
    if not value or len(value) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in value):
        raise HTTPException(status_code=400, detail="недопустимый id плагина")
    return value


def normalize_settings_schema(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    schema: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(key, str):
            continue
        if isinstance(entry, dict):
            definition = dict(entry)
            kind = definition.get("type")
            if kind not in {"string", "integer", "number", "boolean", "array", "object"}:
                default = definition.get("default")
                kind = "boolean" if isinstance(default, bool) else "integer" if isinstance(default, int) else "number" if isinstance(default, float) else "array" if isinstance(default, list) else "object" if isinstance(default, dict) else "string"
                definition["type"] = kind
            schema[key] = definition
        else:
            kind = "boolean" if isinstance(entry, bool) else "integer" if isinstance(entry, int) else "number" if isinstance(entry, float) else "array" if isinstance(entry, list) else "object" if isinstance(entry, dict) else "string"
            schema[key] = {"type": kind, "default": entry}
    return schema


def default_plugin_values(schema: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {key: definition["default"] for key, definition in schema.items() if "default" in definition}


def validate_plugin_values(values: dict[str, Any], schema: dict[str, dict[str, Any]]) -> dict[str, Any]:
    merged = default_plugin_values(schema)
    merged.update(values)
    for key, value in merged.items():
        definition = schema.get(key)
        if not definition:
            continue
        kind = definition.get("type")
        valid = {
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "array": lambda v: isinstance(v, list),
            "object": lambda v: isinstance(v, dict),
        }.get(kind, lambda _v: True)
        if not valid(value):
            raise HTTPException(status_code=400, detail=f"параметр {key}: ожидался тип {kind}")
        if "enum" in definition and value not in definition["enum"]:
            raise HTTPException(status_code=400, detail=f"параметр {key}: значение не входит в список")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in definition and value < definition["minimum"]:
                raise HTTPException(status_code=400, detail=f"параметр {key}: меньше минимума")
            if "maximum" in definition and value > definition["maximum"]:
                raise HTTPException(status_code=400, detail=f"параметр {key}: больше максимума")
        if isinstance(value, str) and "maxLength" in definition and len(value) > int(definition["maxLength"]):
            raise HTTPException(status_code=400, detail=f"параметр {key}: слишком длинная строка")
    return merged


def lua_literal(value: Any) -> str:
    if value is None:
        return "nil"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise HTTPException(status_code=400, detail="NaN и бесконечность не поддерживаются")
        return repr(value)
    if isinstance(value, str):
        escaped = []
        for byte in value.encode("utf-8"):
            if byte == 34:
                escaped.append('\\"')
            elif byte == 92:
                escaped.append("\\\\")
            elif byte == 10:
                escaped.append("\\n")
            elif byte == 13:
                escaped.append("\\r")
            elif byte == 9:
                escaped.append("\\t")
            elif byte < 32 or byte == 127:
                escaped.append(f"\\{byte:03d}")
            else:
                escaped.append(bytes([byte]).decode("latin-1"))
        # Lua source is UTF-8. Convert byte-preserving latin-1 placeholders back to the original UTF-8 text.
        raw = "".join(escaped).encode("latin-1").decode("utf-8")
        return '"' + raw + '"'
    if isinstance(value, list):
        return "{" + ", ".join(lua_literal(item) for item in value) + "}"
    if isinstance(value, dict):
        pairs = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise HTTPException(status_code=400, detail="ключи JSON-объектов должны быть строками")
            pairs.append("[" + lua_literal(key) + "] = " + lua_literal(item))
        return "{" + ", ".join(pairs) + "}"
    raise HTTPException(status_code=400, detail=f"тип {type(value).__name__} нельзя передать Lua-плагину")


def lua_settings_prelude(values: dict[str, Any]) -> bytes:
    literal = lua_literal(values)
    source = (
        "-- Настройки, синхронизированные панелью Станции. Редактируйте через панель.\\n"
        "local __station_panel_values = " + literal + "\\n"
        "alice.settings = {\\n"
        "  get = function(key) return __station_panel_values[key] end,\\n"
        "  all = function() return __station_panel_values end,\\n"
        "  values = __station_panel_values,\\n"
        "}\\n"
        "-- Конец настроек панели.\\n"
    )
    return source.encode("utf-8")


async def yam_plugin(plugin_id: str) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://{cfg['station_ip']}:{cfg['yam_port']}/api/plugins") as response:
                if response.status != 200:
                    raise HTTPException(status_code=502, detail=f"YAM вернул HTTP {response.status}")
                plugins = await response.json(content_type=None)
        for plugin in plugins:
            if (plugin.get("manifest") or {}).get("id") == plugin_id:
                return plugin
        raise HTTPException(status_code=404, detail="плагин не найден в YAM")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"не удалось обратиться к YAM: {exc}")


async def write_remote_atomic(sftp: Any, path: str, content: bytes, mode: int | None = None) -> None:
    temporary = path + ".panel-tmp-" + secrets.token_hex(5)
    try:
        async with sftp.open(temporary, "wb") as remote_file:
            await remote_file.write(content)
        if mode is not None:
            await sftp.chmod(temporary, mode)
        await sftp.rename(temporary, path)
    except Exception:
        try:
            await sftp.remove(temporary)
        except Exception:
            pass
        raise


@app.get("/api/plugins/{plugin_id}/settings")
async def get_plugin_settings(plugin_id: str, request: Request) -> JSONResponse:
    require_auth(request)
    plugin_id = valid_plugin_id(plugin_id)
    plugin = await yam_plugin(plugin_id)
    manifest = plugin.get("manifest") or {}
    schema = normalize_settings_schema(manifest.get("settings", {}))
    path = PLUGIN_CONFIG_DIR / f"{plugin_id}.json"
    values = default_plugin_values(schema)
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and isinstance(saved.get("values"), dict):
                values.update(saved["values"])
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"конфиг повреждён: {exc}")
    return JSONResponse({"id": plugin_id, "manifest": manifest, "schema": schema, "values": values, "running": bool(plugin.get("running")), "kind": (manifest.get("launch") or {}).get("kind", "lua")})


@app.put("/api/plugins/{plugin_id}/settings")
async def put_plugin_settings(plugin_id: str, request: Request) -> JSONResponse:
    require_auth(request)
    plugin_id = valid_plugin_id(plugin_id)
    plugin = await yam_plugin(plugin_id)
    manifest = plugin.get("manifest") or {}
    launch = manifest.get("launch") or {}
    kind = launch.get("kind", "lua")
    schema = normalize_settings_schema(manifest.get("settings", {}))
    payload = await request.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("values"), dict):
        raise HTTPException(status_code=400, detail="ожидается {values: {...}}")
    values = validate_plugin_values(payload["values"], schema)
    if len(json.dumps(values, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
        raise HTTPException(status_code=413, detail="конфиг больше 256 КиБ")

    PLUGIN_CONFIG_DIR.mkdir(exist_ok=True)
    local_path = PLUGIN_CONFIG_DIR / f"{plugin_id}.json"
    tmp_path = local_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps({"values": values}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(local_path)

    remote_base = str(manifest.get("directory") or plugin.get("directory") or "")
    if not remote_base.endswith("/plugins/" + plugin_id):
        # PluginInfo.directory is authoritative; the extra check prevents malformed paths.
        directory = str(plugin.get("directory") or "").rstrip("/")
        if not directory.endswith("/" + plugin_id) or "/plugins/" not in directory:
            raise HTTPException(status_code=502, detail="YAM вернул неожиданную папку плагина")
        remote_base = directory
    else:
        remote_base = remote_base.rstrip("/")

    try:
        async with await ssh_connect() as conn:
            async with conn.start_sftp_client() as sftp:
                remote_json = remote_base + "/settings.json"
                await write_remote_atomic(sftp, remote_json, json.dumps(values, ensure_ascii=False, indent=2).encode("utf-8"), 0o600)

                if kind == "lua":
                    relative = str(launch.get("path") or manifest.get("entry") or "")
                    from pathlib import PurePosixPath
                    relative_path = PurePosixPath(relative)
                    if relative_path.is_absolute() or not relative_path.parts or any(part in ("..", ".") for part in relative_path.parts) or "\\" in relative:
                        raise HTTPException(status_code=400, detail="небезопасный путь entry в манифесте")
                    remote_entry = remote_base + "/" + "/".join(relative_path.parts)
                    backup = remote_entry + ".station-panel-original"
                    if await sftp.exists(backup):
                        async with sftp.open(backup, "rb") as original_file:
                            original = await original_file.read()
                    else:
                        async with sftp.open(remote_entry, "rb") as original_file:
                            original = await original_file.read()
                        attrs = await sftp.stat(remote_entry)
                        mode = attrs.permissions & 0o777 if attrs.permissions else 0o644
                        await write_remote_atomic(sftp, backup, original, mode)
                    attrs = await sftp.stat(remote_entry)
                    mode = attrs.permissions & 0o777 if attrs.permissions else 0o644
                    await write_remote_atomic(sftp, remote_entry, lua_settings_prelude(values) + original, mode)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"не удалось применить настройки на колонке: {exc}")

    return JSONResponse({"ok": True, "id": plugin_id, "applied": True, "restart_required": bool(plugin.get("running")), "runtime_api": "alice.settings.get(key), alice.settings.all(), alice.settings.values" if kind == "lua" else "settings.json рядом с бинарным плагином"})


@app.delete("/api/plugins/{plugin_id}/settings")
async def reset_plugin_settings(plugin_id: str, request: Request) -> JSONResponse:
    require_auth(request)
    plugin_id = valid_plugin_id(plugin_id)
    plugin = await yam_plugin(plugin_id)
    manifest = plugin.get("manifest") or {}
    kind = ((manifest.get("launch") or {}).get("kind", "lua"))
    PLUGIN_CONFIG_DIR.mkdir(exist_ok=True)
    (PLUGIN_CONFIG_DIR / f"{plugin_id}.json").unlink(missing_ok=True)
    remote_base = str(plugin.get("directory") or "").rstrip("/")
    if not remote_base.endswith("/" + plugin_id) or "/plugins/" not in remote_base:
        raise HTTPException(status_code=502, detail="YAM вернул неожиданную папку плагина")
    try:
        async with await ssh_connect() as conn:
            async with conn.start_sftp_client() as sftp:
                try:
                    await sftp.remove(remote_base + "/settings.json")
                except (OSError, asyncssh.SFTPNoSuchFile):
                    pass
                if kind == "lua":
                    launch = manifest.get("launch") or {}
                    relative = str(launch.get("path") or manifest.get("entry") or "")
                    from pathlib import PurePosixPath
                    relative_path = PurePosixPath(relative)
                    if relative_path.is_absolute() or not relative_path.parts or any(part in ("..", ".") for part in relative_path.parts) or "\\" in relative:
                        raise HTTPException(status_code=400, detail="небезопасный путь entry в манифесте")
                    remote_entry = remote_base + "/" + "/".join(relative_path.parts)
                    backup = remote_entry + ".station-panel-original"
                    if await sftp.exists(backup):
                        async with sftp.open(backup, "rb") as original_file:
                            original = await original_file.read()
                        attrs = await sftp.stat(remote_entry)
                        mode = attrs.permissions & 0o777 if attrs.permissions else 0o644
                        await write_remote_atomic(sftp, remote_entry, original, mode)
                        await sftp.remove(backup)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"не удалось сбросить настройки на колонке: {exc}")
    return JSONResponse({"ok": True, "id": plugin_id})


# --------------------------------------------------------------------------- #
# Обнаружение станций: mDNS (ловит и «сырые» колонки без YAM), затем скан по YAM
# --------------------------------------------------------------------------- #
# Сервисы, которыми анонсятся Яндекс-станции. Первый — основной (YandexIO),
# остальные добавлены как возможные варианты: чего нет в сети, то просто молчит.
MDNS_TYPES = (
    "_yandexio._tcp.local.",
    "_yandex-station._tcp.local.",
    "_quasar._tcp.local.",
    "_alice._tcp.local.",
)
MDNS_HINTS = ("yandex", "quasar", "alice", "yandexio")


def _local_subnets() -> list[str]:
    """Префиксы /24 всех локальных IPv4-адресов этой машины.

    Нужен для скана: дефолтный IP станции может указывать не в ту сеть,
    поэтому ищем колонки в тех подсетях, где машина реально живёт.
    Первой идёт подсеть маршрута по умолчанию, остальные — за ней.
    """
    default_prefix: str | None = None
    prefixes: list[str] = []

    def add(ip: str) -> None:
        nonlocal default_prefix
        parts = ip.split(".")
        if len(parts) != 4 or not all(p.isdigit() for p in parts) or ip.startswith("127."):
            return
        prefix = ".".join(parts[:3])
        if prefix in prefixes:
            return
        prefixes.append(prefix)

    # адрес интерфейса, через который уходит маршрут по умолчанию — самый вероятный
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            s_ip = s.getsockname()[0]
        finally:
            s.close()
        add(s_ip)
        default_prefix = ".".join(s_ip.split(".")[:3])
    except Exception:
        pass
    # все адреса хоста по имени
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except Exception:
        pass
    if default_prefix and default_prefix in prefixes:
        prefixes.remove(default_prefix)
        prefixes.insert(0, default_prefix)
    return prefixes


def _mdns_scan(timeout: float = 4.0) -> list[dict[str, Any]]:
    """Слушает mDNS и собирает найденные устройства. Блокирующая — звать в executor."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except Exception:
        return []

    found: dict[str, dict[str, Any]] = {}

    def addr_str(raw: Any) -> str:
        try:
            if isinstance(raw, (bytes, bytearray)):
                return socket.inet_ntoa(raw)
        except Exception:
            pass
        return str(raw)

    def decode(props: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, val in (props or {}).items():
            name = key.decode(errors="replace") if isinstance(key, bytes) else str(key)
            if isinstance(val, (bytes, bytearray)):
                text = val.decode(errors="replace")
            else:
                text = "" if val is None else str(val)
            out[name] = text
        return out

    class Listener(ServiceListener):
        def add_service(self, zc: Any, service_type: str, name: str) -> None:
            self.keep(zc, service_type, name)

        def update_service(self, zc: Any, service_type: str, name: str) -> None:
            self.keep(zc, service_type, name)

        def remove_service(self, zc: Any, service_type: str, name: str) -> None:
            return None

        def keep(self, zc: Any, service_type: str, name: str) -> None:
            try:
                info = zc.get_service_info(service_type, name, timeout=1500)
            except Exception:
                return
            if not info:
                return
            props = decode(info.properties)
            host = (info.server or name).rstrip(".")
            for raw in (info.addresses or []):
                ip = addr_str(raw)
                if not ip or ip.startswith("127."):
                    continue
                found[ip] = {
                    "ip": ip,
                    "name": props.get("name") or host.split(".")[0],
                    "model": props.get("model") or props.get("platform") or "",
                    "device_id": (props.get("device_id") or props.get("deviceId")
                                  or props.get("id") or props.get("serial") or ""),
                    "service": service_type,
                    "source": "mDNS",
                }

    zc = None
    browsers: list[Any] = []
    try:
        zc = Zeroconf()
        listener = Listener()
        browsers = [ServiceBrowser(zc, service_type, listener) for service_type in MDNS_TYPES]

        # Заодно спрашиваем у сети список всех сервисов и добавляем «яндексоподобные»:
        # так находятся станции, анонсящиеся под неожиданным именем.
        try:
            class Types(ServiceListener):
                def add_service(self, zc2: Any, service_type: str, name: str) -> None:
                    low = name.lower()
                    if any(hint in low for hint in MDNS_HINTS) and name not in MDNS_TYPES:
                        browsers.append(ServiceBrowser(zc2, name, listener))

                def update_service(self, *args: Any) -> None:
                    return None

                def remove_service(self, *args: Any) -> None:
                    return None

            browsers.append(ServiceBrowser(zc, "_services._dns-sd._udp.local.", Types()))
        except Exception:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.2)
    except Exception:
        pass
    finally:
        for browser in browsers:
            try:
                browser.cancel()
            except Exception:
                pass
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass
    return list(found.values())


async def _yam_device(ip: str) -> dict[str, Any] | None:
    """Ответ демона YAM (:8099) по IP — признак, что на колонке стоит YAM."""
    try:
        timeout = aiohttp.ClientTimeout(total=1.2)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://{ip}:{cfg['yam_port']}/api/device") as resp:
                if resp.status == 200:
                    device = await resp.json(content_type=None)
                    if isinstance(device, dict) and device.get("model"):
                        return device
    except Exception:
        pass
    return None


async def _port_open(ip: str, port: int, timeout: float = 0.8) -> bool:
    """Открыт ли TCP-порт (например, root-SSH 2222)."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


@app.post("/api/detect")
async def api_detect(request: Request) -> JSONResponse:
    """Найти станции: mDNS (включая «сырые», без YAM), затем скан подсети по YAM."""
    require_auth(request)

    devices: dict[str, dict[str, Any]] = {}

    def note(ip: str, **kw: Any) -> None:
        if not ip:
            return
        item = devices.setdefault(ip, {
            "ip": ip, "name": "", "model": "", "device_id": "",
            "source": "", "has_yam": False, "ssh": False,
        })
        for key, value in kw.items():
            if key in ("has_yam", "ssh"):
                item[key] = item[key] or bool(value)
            elif value:
                item[key] = value

    current = cfg.get("station_ip", "")
    scanned = 0

    # 1. Текущий IP: если отвечает YAM — это рабочая колонка.
    if current:
        device = await _yam_device(current)
        if device:
            note(current, name=device.get("name") or "", model=device.get("model") or "",
                 device_id=device.get("device_id") or "", source="текущий IP", has_yam=True)

    # 2. mDNS: так же, как alice bridge, видит станции без YAM.
    try:
        loop = asyncio.get_running_loop()
        mdns = await asyncio.wait_for(loop.run_in_executor(None, _mdns_scan, 4.0), timeout=15)
    except Exception:
        mdns = []
    for item in mdns:
        note(item["ip"], name=item.get("name", ""), model=item.get("model", ""),
             device_id=item.get("device_id", ""), source=item.get("source", "mDNS"))

    # 3. Если совсем ничего не нашли — скан всех локальных подсетей по порту YAM.
    network_prefixes = _local_subnets()
    if current:
        prefix = ".".join(current.split(".")[:3])
        if prefix not in network_prefixes:
            network_prefixes.append(prefix)
    scanned = 0
    if not devices and network_prefixes:
        candidates: list[str] = []
        for prefix in network_prefixes:
            if not all(p.isdigit() for p in prefix.split(".")):
                continue
            local_octet = current.split(".")[3] if current.startswith(prefix + ".") else None
            candidates.extend(f"{prefix}.{o}" for o in range(1, 255) if str(o) != local_octet)
        scanned = len(candidates)
        for start in range(0, scanned, 40):
            batch = candidates[start:start + 40]
            results = await asyncio.gather(*(_yam_device(ip) for ip in batch))
            for ip, device in zip(batch, results):
                if device:
                    note(ip, name=device.get("name") or "", model=device.get("model") or "",
                         device_id=device.get("device_id") or "", source="скан подсети", has_yam=True)

    # 4. Для найденных уточняем признаки: YAM и root-SSH.
    for item in devices.values():
        if not item["has_yam"]:
            device = await _yam_device(item["ip"])
            if device:
                item["has_yam"] = True
                item["model"] = item["model"] or (device.get("model") or "")
                item["device_id"] = item["device_id"] or (device.get("device_id") or "")
        if not item["ssh"]:
            item["ssh"] = await _port_open(item["ip"], int(cfg.get("ssh_port") or 2222))

    listed = sorted(devices.values(), key=lambda d: (not d["has_yam"], d["ip"]))
    ready = next((d for d in listed if d["has_yam"]), None)
    if ready:
        cfg["station_ip"] = ready["ip"]
        save_config(cfg)
        _status_cache["ts"] = 0.0
        print(f"[+] колонка с YAM найдена автоматически: {ready['ip']}")
        return JSONResponse({"found": True, "ip": ready["ip"], "has_yam": True,
                             "scanned": scanned, "devices": listed})

    if listed:
        print(f"[i] найдены станции без YAM: {[d['ip'] for d in listed]}")
    return JSONResponse({"found": False, "ip": "", "has_yam": False,
                         "scanned": scanned, "devices": listed})


@app.get("/api/status")
async def api_status(request: Request) -> JSONResponse:
    require_auth(request)
    if _status_cache["data"] and time.time() - _status_cache["ts"] < 4:
        return JSONResponse(_status_cache["data"])

    cmd = (
        "echo '#UP'; cat /proc/uptime | cut -d' ' -f1; "
        "echo '#MEM'; free | sed -n '2p'; "
        "echo '#DISK'; df -h / | sed -n '2p'; "
        "echo '#LOAD'; cat /proc/loadavg; "
        "echo '#TEMP'; cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null; "
        "echo '#HOST'; hostname; "
        "echo '#KERNEL'; uname -sr; "
        "echo '#YAMD'; pidof yamd; "
        "echo '#SSH'; pidof dropbear; "
        "echo '#ADB'; pidof adbd; "
        "echo '#LAN'; ip -4 addr show wlan0 2>/dev/null || ifconfig wlan0 2>/dev/null"
    )
    code, out, err = await ssh_run(cmd, timeout=15)

    data: dict[str, Any] = {
        "reachable": code == 0,
        "error": err.strip() if code != 0 else "",
        "station_ip": cfg["station_ip"],
        "yam_online": False,
        "plugins": None,
        "device": None,
    }

    if code == 0:
        section = None
        for raw in out.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                section = line[1:]
                data.setdefault(section.lower(), "")
                continue
            if section == "MEM":
                parts = line.split()
                if len(parts) >= 3:
                    total, used = int(parts[1]), int(parts[2])
                    data["mem_total_mb"] = round(total / 1024, 1)
                    data["mem_used_mb"] = round(used / 1024, 1)
                    data["mem_percent"] = round(used / total * 100, 1) if total else 0
            elif section == "DISK":
                parts = line.split()
                if len(parts) >= 5:
                    data["disk_size"] = parts[1]
                    data["disk_used"] = parts[2]
                    data["disk_percent"] = parts[4].rstrip("%")
            elif section == "TEMP":
                try:
                    data["cpu_temp"] = round(int(line) / 1000, 1)
                except ValueError:
                    pass
            elif section == "UP":
                try:
                    data["uptime_sec"] = int(float(line))
                except ValueError:
                    pass
            elif section == "LAN":
                data.setdefault("lan_raw", []).append(line)
            elif section and section not in ("MEM", "DISK", "TEMP", "UP"):
                data[section.lower()] = line

    lan_match = re.search(
        r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", " ".join(data.pop("lan_raw", [])))
    data["lan_ip"] = lan_match.group(1) if lan_match else ""

    # данные демона YAM
    timeout = aiohttp.ClientTimeout(total=6)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://{cfg['station_ip']}:{cfg['yam_port']}/api/device") as resp:
                data["device"] = await resp.json(content_type=None)
                data["yam_online"] = True
            async with session.get(f"http://{cfg['station_ip']}:{cfg['yam_port']}/api/plugins") as resp:
                plugins = await resp.json(content_type=None)
                data["plugins"] = len(plugins)
                data["plugins_running"] = sum(1 for p in plugins if p.get("running"))
    except Exception as exc:
        data["yam_error"] = f"{type(exc).__name__}: {exc}"

    _status_cache["ts"] = time.time()
    _status_cache["data"] = data
    return JSONResponse(data)


@app.post("/api/exec")
async def api_exec(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    command = str(data.get("command", "")).strip()
    if not command:
        raise HTTPException(status_code=400, detail="пустая команда")
    code, out, err = await ssh_run(command, timeout=float(data.get("timeout", 30)))
    return JSONResponse({"code": code, "stdout": out, "stderr": err})


@app.post("/api/reboot")
async def api_reboot(request: Request) -> JSONResponse:
    require_auth(request)
    try:
        async with await ssh_connect() as conn:
            await asyncio.wait_for(conn.run("reboot", check=False), 5)
    except Exception:
        pass  # связь рвётся штатно — колонка уходит в перезагрузку
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# Телеметрия Яндекса: autostart-службы telemetry и fluent-bitd в
# /system/vendor/quasar/quasar.cfg (JSON вендора). rootfs смонтирован ro —
# перед правкой поднимаем rw и возвращаем обратно; исходник бэкапится один раз
# в quasar.cfg.bak-panel. Применение — после перезагрузки колонки.
# --------------------------------------------------------------------------- #
TELEMETRY_CFG = "/system/vendor/quasar/quasar.cfg"
TELEMETRY_BAK = TELEMETRY_CFG + ".bak-panel"
_TELEMETRY_CMD = (
    "echo __CFG__; cat " + TELEMETRY_CFG + "; echo; "
    "echo __MOUNT__; grep ubi0:rootfs /proc/mounts; "
    "echo __PS__; ps | grep -E 'maind --(telemetry|fluent-bitd)' | grep -v grep; "
    "echo __BAK__; [ -f " + TELEMETRY_BAK + " ] && echo y || echo n; "
    "echo __END__"
)


def _find_json_key(obj: Any, key: str) -> Any:
    """Найти ключ рекурсивно (servicesList лежит не в корне конфига)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_json_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_json_key(item, key)
            if found is not None:
                return found
    return None


def _telemetry_patch(text: str) -> str:
    """Убрать telemetry/fluent-bitd из servicesList и добавить флажок.

    Правим текст, а не пересобранный JSON, чтобы сохранить форматирование
    вендорного файла; валидность проверяем json.loads до записи."""
    lines = []
    for line in text.split("\n"):
        if '"servicesList"' in line:
            line = line.replace(",telemetry", "").replace(",fluent-bitd", "")
        lines.append(line)
    patched = "\n".join(lines)
    if '"fluentBitDisabled"' not in patched:
        cut = patched.find("\n")
        if cut < 0:
            raise ValueError("неожиданный формат quasar.cfg")
        patched = patched[:cut + 1] + '   "fluentBitDisabled" : true,\n' + patched[cut + 1:]
    json.loads(patched)  # отказ до записи, если что-то сломали
    return patched


def _telemetry_unpatch(text: str) -> str:
    """Вернуть службы в список и убрать флажок (запасной путь без бэкапа)."""
    lines = []
    for line in text.split("\n"):
        if '"servicesList"' in line and "telemetry" not in line:
            line = line.replace(
                '"quasar_launcher2,updatesd,mediad,platformd"',
                '"quasar_launcher2,updatesd,mediad,telemetry,platformd,fluent-bitd"',
            )
        if '"fluentBitDisabled"' in line and line.strip().endswith(","):
            continue  # наш вставленный ключ всегда с запятой — строка уходит целиком
        lines.append(line)
    patched = "\n".join(lines)
    json.loads(patched)
    return patched


async def _telemetry_state() -> dict[str, Any]:
    """Снять состояние: список служб, флажок, монтирование, бэкап, процессы."""
    code, out, err = await ssh_run(_TELEMETRY_CMD, timeout=25)
    state: dict[str, Any] = {"ok": code == 0}
    if code != 0:
        state["detail"] = (err or out or "команда не выполнилась").strip()[:200]
        return state
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in out.split("\n"):
        tag = line.strip()
        if tag in ("__CFG__", "__MOUNT__", "__PS__", "__BAK__", "__END__"):
            current = tag[2:-2].lower()
            sections[current] = []
        elif current and current != "end":
            sections.setdefault(current, []).append(line)
    cfg_text = "\n".join(sections.get("cfg", [])).strip("\n").strip()
    mount_line = "\n".join(sections.get("mount", [])).strip()
    ps_text = "\n".join(sections.get("ps", []))
    backup = "\n".join(sections.get("bak", [])).strip().lower().startswith("y")

    services: list[str] = []
    fluent_disabled = False
    json_ok = False
    try:
        cfg = json.loads(cfg_text)
        json_ok = True
        raw = _find_json_key(cfg, "servicesList")
        if isinstance(raw, str):
            services = [item for item in raw.split(",") if item]
        if isinstance(cfg, dict) and "fluentBitDisabled" in cfg:
            fluent_disabled = bool(cfg["fluentBitDisabled"])
    except Exception:
        state["detail"] = "quasar.cfg не распознан как JSON"
    fields = mount_line.split()
    mounted = fields[3][:2] if len(fields) >= 4 and fields[3][:2] in ("rw", "ro") else "?"
    state.update({
        "json_ok": json_ok,
        "services_list": services,
        "telemetry_in_list": "telemetry" in services,
        "fluent_in_list": "fluent-bitd" in services,
        "fluent_bit_disabled": fluent_disabled,
        "mounted": mounted,
        "backup": backup,
        "running_telemetry": sum(1 for line in ps_text.split("\n") if "--telemetry" in line),
        "running_fluent": sum(1 for line in ps_text.split("\n") if "--fluent-bitd" in line),
    })
    return state


@app.get("/api/telemetry")
async def api_telemetry_get(request: Request) -> JSONResponse:
    require_auth(request)
    return JSONResponse(await _telemetry_state())


@app.post("/api/telemetry")
async def api_telemetry_put(request: Request) -> JSONResponse:
    """Отключить (enabled:false) или вернуть телеметрию Яндекса.

    Пишет quasar.cfg на колонке: remount rw → бэкап → правка → remount ro.
    Действующим службам это не мешает — применение после перезагрузки."""
    require_auth(request)
    data = await request.json()
    enable = bool(data.get("enabled", False))
    try:
        conn = await ssh_connect()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="SSH: " + str(exc)[:160])
    try:
        try:
            await asyncio.wait_for(conn.run("mount -o remount,rw /", check=False, encoding="utf-8"), 15)
            if not enable:
                # бэкап исходника ровно один раз — повторные отключения не портят его
                await asyncio.wait_for(conn.run(
                    f"[ -f {TELEMETRY_BAK} ] || cp -p {TELEMETRY_CFG} {TELEMETRY_BAK}",
                    check=False, encoding="utf-8"), 15)
            current = (await asyncio.wait_for(conn.run(
                f"cat {TELEMETRY_CFG}", check=False, encoding="utf-8"), 25)).stdout or ""
            if not current.strip():
                raise ValueError("quasar.cfg пуст или не читается")
            if enable:
                bak = await asyncio.wait_for(conn.run(
                    f"[ -f {TELEMETRY_BAK} ] && echo y || echo n", check=False, encoding="utf-8"), 15)
                if (bak.stdout or "").strip().lower() == "y":
                    text = (await asyncio.wait_for(conn.run(
                        f"cat {TELEMETRY_BAK}", check=False, encoding="utf-8"), 25)).stdout or ""
                    json.loads(text)
                else:
                    text = _telemetry_unpatch(current)
            else:
                text = _telemetry_patch(current)
            result = await asyncio.wait_for(conn.run(
                f"cat > {TELEMETRY_CFG}", input=text, check=False, encoding="utf-8"), 25)
            if result.exit_status not in (0, None):
                raise ValueError(f"запись не удалась (status {result.exit_status})")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:180])
        finally:
            # ro возвращаем всегда — даже при ошибке правки
            try:
                await asyncio.wait_for(conn.run("mount -o remount,ro /", check=False, encoding="utf-8"), 15)
            except Exception:
                pass
    finally:
        conn.close()
    state = await _telemetry_state()
    if not state.get("json_ok"):
        raise HTTPException(status_code=502, detail="записан некорректный конфиг: " + str(state.get("detail", "")))
    return JSONResponse({"ok": True, "enabled": enable, **state})


@app.get("/api/system/minidumps")
async def api_system_minidumps(request: Request) -> JSONResponse:
    """Список минидампов падений демонов (/data/quasar/minidump) — для диагностики."""
    require_auth(request)
    code, out, _ = await ssh_run_bytes(
        "ls -la /data/quasar/minidump 2>/dev/null | tail -25; echo ---; "
        "du -sh /data/quasar/minidump 2>/dev/null")
    if code != 0:
        return JSONResponse({"ok": False, "items": [], "detail": "каталог недоступен"})
    items: list[dict] = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        parts = line.split(None, 8)
        if len(parts) >= 9 and (parts[0].startswith('-') or parts[0].startswith('d')) and parts[8] not in ('.', '..'):
            items.append({"name": parts[8], "size": parts[4], "date": f"{parts[5]} {parts[6]} {parts[7]}"})
    total = out.decode("utf-8", errors="replace").split('---')[-1].strip()
    return JSONResponse({"ok": True, "items": items, "total": total.splitlines()[-1] if total else ''})


# --------------------------------------------------------------------------- #
# аудит безопасности: что открыто на колонке и что с этим делать
# --------------------------------------------------------------------------- #
AUDIT_TCP_PORTS = [21, 22, 23, 80, 443, 5555, 8080, 8099, 8123, 8780, 8443]


@app.get("/api/security/audit")
async def api_security_audit(request: Request) -> JSONResponse:
    """Только чтение: сканируем порты колонки с ПК, смотрим слушающих демонов
    и флаги dropbear, проверяем YAM API на анонимный доступ. Ничего не меняем."""
    require_auth(request)
    ip = cfg["station_ip"]

    # 1. TCP-доступность портов с компьютера панели
    tcp: dict[int, str] = {}

    async def probe(port: int) -> None:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), 0.8)
            writer.close()
            tcp[port] = "open"
        except Exception:
            tcp[port] = "closed"

    await asyncio.gather(*(probe(port) for port in AUDIT_TCP_PORTS))

    # 2. слушающие сокеты и процессы на самой колонке
    code, out, _ = await ssh_run_bytes(
        "netstat -tln 2>/dev/null | awk '$4 !~ /127.0.0.1|::1/ {print $4}' | sort -u; echo ---; "
        "ps w | grep -E 'dropbear|adbd' | grep -v grep; echo ---; "
        "grep -rl 'dropbear' /etc/init.d /data/opt/etc/init.d 2>/dev/null | head -3; echo ---; "
        "[ -f /root/.ssh/authorized_keys ] && wc -l < /root/.ssh/authorized_keys || echo 0")
    exposed, procs, init_scripts, key_lines = [], [], [], "0"
    if code == 0:
        parts = out.decode("utf-8", errors="replace").split("---")
        for line in parts[0].splitlines():
            m = re.search(r":([0-9A-Fa-f]{4})$", line.strip())
            if m:
                port = int(m.group(1), 16)
                if port:
                    exposed.append(port)
        procs = [l.strip() for l in parts[1].splitlines() if l.strip()]
        init_scripts = [l.strip() for l in parts[2].splitlines() if l.strip()]
        key_lines = (parts[3].strip().splitlines() or ["0"])[-1] if len(parts) > 3 else "0"

    dropbear_blank = any("dropbear" in p and " -B" in p for p in procs)
    dropbear_cmd = next((p for p in procs if "dropbear" in p and " -r" in p), "")
    adbd_on = any("adbd" in p for p in procs)

    # 3. YAM API анонимно (с ПК, без ключей панели)
    yam_anon = None
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://{ip}:{cfg['yam_port']}/api/settings") as resp:
                yam_anon = resp.status == 200
    except Exception:
        yam_anon = None

    findings: list[dict] = []
    if dropbear_blank:
        if key_lines not in ("", "0"):
            advice = ("Ключ панели уже установлен — вход по ключу работает без пароля. Перезапусти dropbear без флага -B "
                      "(команды для «Терминала»): kill $(pidof dropbear); " + (dropbear_cmd.replace(" -B", "") or "/data/opt/bin/dropbear -p 2222")
                      + " &  — учти: при перезагрузке колонки -B вернётся. Постоянно: убери -B в " +
                      (init_scripts[0] if init_scripts else "init-скрипте dropbear") + ".")
        else:
            advice = ("Сначала установи ключ панели: «Мастер настройки» или автонастройка в этом разделе ставит ключ на колонку. "
                      "После этого выключай флаг -B (см. подсказку при повторной проверке) — иначе рискуешь потерять доступ к колонке.")
        findings.append({"level": "high", "title": "SSH принимает пустой пароль (dropbear -B)", "detail":
                         "Любое устройство в локальной сети может зайти на колонку root-ом без пароля: полный доступ к системе и микрофону.",
                         "advice": advice})
    elif key_lines and key_lines != "0":
        findings.append({"level": "ok", "title": "SSH: вход по ключу, пустые пароли выключены", "detail": "", "advice": ""})
    if yam_anon:
        findings.append({"level": "warn", "title": f"YAM API ({cfg['yam_port']}) доступен без пароля всей сети",
                         "detail": "Кто угодно в Wi-Fi может ставить пакеты, читать файлы и управлять подсветкой/звуком колонки.",
                         "advice": "Сам YAM авторизации не умеет. Варианты: не выпускать порт 8099 за пределы доверенной сети (изоляция гостевого Wi-Fi на роутере), либо держать YAM выключенным, когда он не нужен."})
    if adbd_on and tcp.get(5555) == "open":
        findings.append({"level": "warn", "title": "ADB-консоль открыта по сети (порт 5555)",
                         "detail": "Android Debug Bridge позволяет выполнять любые команды на колонке без пароля.",
                         "advice": "Если ADB не нужен постоянно: в «Терминале» stop adbd; и удалить маркер автозапуска /data/quasar/activate_network_adb."})
    if tcp.get(23) == "open":
        findings.append({"level": "high", "title": "Telnet (23) открыт", "detail": "Незашифрованный шелл без авторизации.", "advice": "Закрыть: обычно это busybox telnetd — найти, кто запустил, и отключить."})
    if tcp.get(22) == "open" and cfg["ssh_port"] != 22:
        findings.append({"level": "info", "title": "SSH отвечает и на стандартном порту 22", "detail": "", "advice": "Если не нужен — отключи второй инстанс dropbear в init-скриптах."})
    unexpected = sorted(set(exposed) - {22, cfg["ssh_port"], cfg["yam_port"], 5555, 53})
    if unexpected:
        findings.append({"level": "info", "title": "Другие порты, слушающие на всех интерфейсах: " + ", ".join(map(str, unexpected)),
                         "detail": "Внутренние службы колонки (обычно безобидны, но стоит знать).", "advice": ""})
    findings.append({"level": "info", "title": "Доступ из интернета панель проверить не может",
                     "detail": "Проброс портов (22/2222, 8099) на роутере наружу делать не нужно — колонке достаточно локальной сети.",
                     "advice": ""})

    return JSONResponse({
        "ok": True,
        "ip": ip,
        "tcp": {str(k): v for k, v in tcp.items()},
        "exposed": exposed,
        "key_installed": key_lines not in ("", "0"),
        "findings": findings,
    })


# --------------------------------------------------------------------------- #
# настройки Алисы: user_config.dat (protobuf-обёртка с JSON) + Wi-Fi + Matter
# --------------------------------------------------------------------------- #
USER_CONFIG_PATH = "/data/quasar/data/user_config.dat"

# что разрешено править из панели: путь в JSON конфига -> тип значения
ALICE_SETTING_PATHS: dict[str, type] = {
    "account_config.aliceAdaptiveVolume.enabled": bool,
    "account_config.audio_player.crossfadeEnabled": bool,
    "account_config.jingle": bool,
    "account_config.alwaysOnMicForShortcuts": bool,
    "account_config.enabledCommandSpotters.music.playAndPause": bool,
    "account_config.enabledCommandSpotters.music.volume": bool,
    "account_config.enabledCommandSpotters.music.navigation": bool,
    "account_config.enabledCommandSpotters.music.feedback": bool,
    "account_config.enabledCommandSpotters.music.bluetooth": bool,
    "account_config.enabledCommandSpotters.tv.navigation": bool,
    "account_config.enabledCommandSpotters.tv.backToHome": bool,
    "account_config.enabledCommandSpotters.smartHome.light": bool,
    "account_config.enabledCommandSpotters.smartHome.tv": bool,
    "account_config.enabledCommandSpotters.call.answer": bool,
    "device_config.dndMode.enabled": bool,
    "device_config.dndMode.features.allowIncomingCalls": bool,
    "system_config.phone_calls.enabled": bool,
    "system_config.wifi_capability.reassociateWhenNoInternet": bool,
    "device_config.name": str,
}

# сторож смены Wi-Fi пароля: живёт на колонке, при потере шлюза откатывает бэкап
WIFI_WATCHDOG_TEMPLATE = """#!/bin/sh
# Сторож смены Wi-Fi пароля (ставит панель): при потере шлюза возвращает бэкап.
CONF=__CONF__
BAK=$CONF.bak-panel
RES=/tmp/wifi_watchdog.result
GW=$(ip route 2>/dev/null | awk '/default/ {print $3; exit}')
[ -z "$GW" ] && GW=192.168.0.1
i=0
while [ $i -lt 45 ]; do
  sleep 2
  ping -c1 -W2 $GW >/dev/null 2>&1 && { echo ok > $RES; exit 0; }
  i=$((i+1))
done
cp $BAK $CONF 2>/dev/null
kill -HUP $(pidof wpa_supplicant) 2>/dev/null
echo rolled_back > $RES
"""


def _pb_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _pb_fields(buf: bytes) -> list[tuple[int, bytes]]:
    """Разобрать верхний уровень protobuf-сообщения на (номер поля, payload)."""
    fields: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(buf):
        tag, shift = 0, 0
        while True:
            byte = buf[pos]
            pos += 1
            tag |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        if tag & 0x07 != 2:
            raise ValueError(f"wire type {tag & 7} не поддерживается")
        length, shift = 0, 0
        while True:
            byte = buf[pos]
            pos += 1
            length |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        fields.append((tag >> 3, buf[pos:pos + length]))
        pos += length
    return fields


def _pb_serialize(fields: list[tuple[int, bytes]]) -> bytes:
    out = bytearray()
    for num, payload in fields:
        out += _pb_varint(num << 3 | 2) + _pb_varint(len(payload)) + payload
    return bytes(out)


def _extract_user_config(raw: bytes) -> tuple[dict, list[tuple[int, bytes]]]:
    """Вытащить JSON из user_config.dat: поле 1 (сообщение) содержит поле 2 (JSON)."""
    outer = _pb_fields(raw)
    inner = next((p for n, p in outer if n == 1), None)
    if inner is None:
        raise ValueError("нет поля 1 в user_config.dat")
    json_bytes = next((p for n, p in _pb_fields(inner) if n == 2), None)
    if json_bytes is None:
        raise ValueError("нет JSON в user_config.dat")
    return json.loads(json_bytes.decode("utf-8")), outer


def _rebuild_user_config(config: dict, outer: list[tuple[int, bytes]]) -> bytes:
    inner = next(p for n, p in outer if n == 1)
    # заменяем поле 2 и сортируем поля по номеру — как в заводском файле (1,2,3)
    inner_fields = sorted(
        ((2, json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
         if n == 2 else (n, p) for n, p in _pb_fields(inner)),
        key=lambda f: f[0])
    return _pb_serialize(
        [(1, _pb_serialize(inner_fields)) if n == 1 else (n, p) for n, p in outer])


async def ssh_run_bytes(command: str, input_data: bytes | None = None,
                        timeout: float = 25.0) -> tuple[int, bytes, bytes]:
    """Как ssh_run, но без перекодировки — для бинарных файлов (user_config.dat)."""
    try:
        async with await ssh_connect() as conn:
            result = await asyncio.wait_for(
                conn.run(command, check=False, encoding=None, input=input_data), timeout)
            return result.exit_status or 0, result.stdout or b"", result.stderr or b""
    except asyncio.TimeoutError:
        return 124, b"", "таймаут выполнения команды".encode()
    except Exception as exc:
        return 255, b"", f"{type(exc).__name__}: {exc}".encode()


@app.get("/api/alice/settings")
async def api_alice_settings_get(request: Request) -> JSONResponse:
    require_auth(request)
    code, out, err = await ssh_run_bytes(f"cat {USER_CONFIG_PATH}")
    if code != 0:
        raise HTTPException(status_code=502, detail="SSH: " + err.decode(errors="replace")[:160])
    try:
        config, _ = _extract_user_config(out)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"конфиг не разобран: {exc}")
    return JSONResponse({
        "ok": True,
        "config": config,
        "editable": {p: t.__name__ for p, t in ALICE_SETTING_PATHS.items()},
        "warnings": [
            "Изменения применяются после перезагрузки колонки.",
            "Облако Яндекса (syncd, раз в 30 минут) может вернуть значения из аккаунта — если правка слетела, повторите её.",
        ],
    })


@app.put("/api/alice/settings")
async def api_alice_settings_put(request: Request) -> JSONResponse:
    """Применить набор правок к user_config.dat: {sets: [{path, value}]}.

    Бэкап .bak-panel создаётся перед каждой записью; JSON пересобирается внутри
    protobuf-обёртки без изменения остальных полей."""
    require_auth(request)
    body = await request.json()
    sets = body.get("sets") or ([{"path": body["path"], "value": body["value"]}] if "path" in body else [])
    if not sets:
        raise HTTPException(status_code=400, detail="пустой набор изменений")
    code, out, err = await ssh_run_bytes(f"cat {USER_CONFIG_PATH}")
    if code != 0:
        raise HTTPException(status_code=502, detail="SSH: " + err.decode(errors="replace")[:160])
    try:
        config, outer = _extract_user_config(out)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"конфиг не разобран: {exc}")
    applied: list[str] = []
    for item in sets:
        path = str(item.get("path", ""))
        if path not in ALICE_SETTING_PATHS:
            raise HTTPException(status_code=400, detail=f"путь {path} недоступен для правки")
        target_type = ALICE_SETTING_PATHS[path]
        if target_type is bool:
            value = bool(item.get("value"))
        else:
            value = str(item.get("value") or "").strip()
            if not 1 <= len(value) <= 64:
                raise HTTPException(status_code=400, detail="имя устройства: от 1 до 64 символов")
        node: Any = config
        keys = path.split(".")
        for key in keys[:-1]:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                raise HTTPException(status_code=400, detail=f"путь {path} конфликтует со структурой конфига")
        node[keys[-1]] = value
        applied.append(path)
    blob = _rebuild_user_config(config, outer)
    try:
        conn = await ssh_connect()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="SSH: " + str(exc)[:160])
    try:
        await asyncio.wait_for(conn.run(
            f"cp -p {USER_CONFIG_PATH} {USER_CONFIG_PATH}.bak-panel",
            check=False, encoding="utf-8"), 15)
        result = await asyncio.wait_for(
            conn.run(f"cat > {USER_CONFIG_PATH}", input=blob, check=False, encoding=None), 25)
        if result.exit_status not in (0, None):
            raise ValueError(f"запись не удалась (status {result.exit_status})")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:180])
    finally:
        conn.close()
    return JSONResponse({"ok": True, "applied": applied})


@app.get("/api/alice/wifi")
async def api_alice_wifi_get(request: Request) -> JSONResponse:
    """Состояние Wi-Fi колонки: сохранённые сети из wpa_supplicant.conf и сторож."""
    require_auth(request)
    code, out, err = await ssh_run_bytes(
        "readlink -f /data/wpa_supplicant.conf 2>/dev/null; echo ---; "
        "ps w 2>/dev/null | grep wpa_supplicant | grep -v grep | head -2; echo ---; "
        "cat /data/wpa_supplicant.conf; echo ---; cat /tmp/wifi_watchdog.result 2>/dev/null || true")
    if code != 0:
        raise HTTPException(status_code=502, detail="SSH: " + err.decode(errors="replace")[:160])
    parts = out.decode("utf-8", errors="replace").split("---")
    conf_text = parts[2] if len(parts) > 2 else ""
    networks: list[dict] = []
    current: dict | None = None
    for line in conf_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("network={"):
            current = {"ssid": "", "psk": ""}
        elif stripped == "}" and current is not None:
            networks.append(current)
            current = None
        elif current is not None:
            if stripped.startswith("ssid="):
                current["ssid"] = stripped[5:].strip().strip('"')
            elif stripped.startswith("psk="):
                current["psk"] = stripped[4:].strip().strip('"')
    return JSONResponse({
        "ok": True,
        "conf_path": (parts[0] or "").strip(),
        "process": (parts[1] or "").strip()[:120],
        "networks": networks,
        "watchdog": (parts[3] if len(parts) > 3 else "").strip(),
    })


@app.put("/api/alice/wifi/password")
async def api_alice_wifi_password(request: Request) -> JSONResponse:
    """Сменить пароль сохранённой Wi-Fi сети с автооткатом при потере связи.

    Бэкап conf → новый conf → SIGHUP wpa_supplicant → фоновый сторож на колонке
    90 секунд пингует шлюз; если сети нет — возвращает бэкап и перечитывает конфиг.
    Пароль заменяется во всех сохранённых сетях (обычно она одна)."""
    require_auth(request)
    data = await request.json()
    psk = str(data.get("psk", ""))
    if not 8 <= len(psk) <= 63:
        raise HTTPException(status_code=400, detail="пароль WPA: от 8 до 63 символов")
    code, out, err = await ssh_run_bytes(
        "readlink -f /data/wpa_supplicant.conf 2>/dev/null || echo /data/wpa_supplicant.conf; echo ---; "
        "cat /data/wpa_supplicant.conf")
    if code != 0:
        raise HTTPException(status_code=502, detail="SSH: " + err.decode(errors="replace")[:160])
    parts = out.decode("utf-8", errors="replace").split("---")
    conf_path = parts[0].strip() or "/data/wpa_supplicant.conf"
    conf_text = parts[1] if len(parts) > 1 else ""
    new_lines: list[str] = []
    replaced = 0
    in_block = False
    for line in conf_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("network={"):
            in_block = True
        elif stripped == "}" and in_block:
            in_block = False
        if in_block and stripped.startswith("psk="):
            new_lines.append(f'psk="{psk}"')
            replaced += 1
        else:
            new_lines.append(line)
    if not replaced:
        raise HTTPException(status_code=400, detail="в конфиге нет сохранённых сетей с паролем")
    new_conf = ("\n".join(new_lines) + "\n").encode("utf-8")
    watchdog = WIFI_WATCHDOG_TEMPLATE.replace("__CONF__", conf_path).encode()
    try:
        conn = await ssh_connect()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="SSH: " + str(exc)[:160])
    try:
        try:
            await asyncio.wait_for(conn.run(
                f"cp -p {conf_path} {conf_path}.bak-panel", check=False, encoding="utf-8"), 15)
            result = await asyncio.wait_for(
                conn.run(f"cat > {conf_path}", input=new_conf, check=False, encoding=None), 20)
            if result.exit_status not in (0, None):
                raise ValueError(f"запись конфига не удалась (status {result.exit_status})")
            result2 = await asyncio.wait_for(
                conn.run("cat > /tmp/wifi_watchdog.sh", input=watchdog, check=False, encoding=None), 15)
            if result2.exit_status not in (0, None):
                raise ValueError("не удалось создать сторож")
            await asyncio.wait_for(conn.run(
                "rm -f /tmp/wifi_watchdog.result; sh /tmp/wifi_watchdog.sh >/dev/null 2>&1 &",
                check=False, encoding="utf-8"), 10)
            hup = await asyncio.wait_for(conn.run(
                "kill -HUP $(pidof wpa_supplicant) 2>/dev/null || true",
                check=False, encoding="utf-8"), 10)
            if hup.exit_status not in (0, None):
                raise ValueError("wpa_supplicant не перечитал конфиг")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:180])
    finally:
        conn.close()
    return JSONResponse({
        "ok": True,
        "conf_path": conf_path,
        "networks_updated": replaced,
        "watchdog": "колонка проверяет связь ~90 секунд; при неудаче пароль откатится",
    })


@app.get("/api/alice/matter")
async def api_alice_matter(request: Request) -> JSONResponse:
    """Matter: включена ли возможность и «сырые» записи из базы устройств."""
    require_auth(request)
    code, out, _ = await ssh_run_bytes(
        "strings /data/quasar/data/matter/devices.lmdb 2>/dev/null | head -60 || true")
    names: list[str] = []
    if code == 0:
        for line in out.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if len(line) >= 4 and line.lower() not in ("devices", "state", "scenarios", "db_exists"):
                names.append(line)
    return JSONResponse({"ok": True, "records": names[:40]})


# --------------------------------------------------------------------------- #
# облачные функции через аккаунт Яндекса: куки -> x_token -> API квазара
# Эндпоинты те же, что у приложения «Дом с Алисой» (iot.quasar.yandex.ru),
# значения применяются в облаке сразу и спускаются на колонку сами.
# --------------------------------------------------------------------------- #
PASSPORT_CLIENT_ID = "c0ebe342af7d48fbbbfcf2d2eedb8f9e"
PASSPORT_CLIENT_SECRET = "ad0a908f0aa341a182a37ecd75bc319e"
CLOUD_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CLOUD_STATE: dict = {"session": None, "csrf": None, "login": None}

# быстрые облачные тумблеры /m/user/settings: id в панели -> payload
CLOUD_SETTINGS_TOGGLES: dict[str, dict] = {
    "c-dj": {"on": {"music": {"announce_tracks": True}}, "off": {"music": {"announce_tracks": False}}},
    "c-whisper": {"on": {"tts_whisper": True}, "off": {"tts_whisper": False}},
    "c-sh-silent": {"on": {"iot": {"response_reaction_type": "sound"}}, "off": {"iot": {"response_reaction_type": "nlg"}}},
    "c-hide-orders": {"on": {"order": {"hide_item_names": True}}, "off": {"order": {"hide_item_names": False}}},
}


def _cloud_session() -> aiohttp.ClientSession:
    session = CLOUD_STATE["session"]
    if session is None or session.closed:
        raise HTTPException(status_code=400, detail="облачная сессия не открыта — сохраните куку аккаунта")
    return session


async def cloud_short_info(x_token: str) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get(
                "https://mobileproxy.passport.yandex.net/1/bundle/account/short_info/",
                params={"avatar_size": "islands-300"},
                headers={"Authorization": f"OAuth {x_token}", "User-Agent": CLOUD_UA},
                timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json()


async def cloud_open_session(x_token: str) -> dict:
    """Открыть облачную сессию: x_token -> куки паспорта (как в YandexStation)."""
    await cloud_close_session()
    session = aiohttp.ClientSession(headers={"User-Agent": CLOUD_UA})
    # фикс YandexStation: без этого aiohttp квотирует куки Session_id,
    # и Яндекс отдаёт 403 на всех POST/PUT (мутациях)
    setattr(session.cookie_jar, "_quote_cookie", False)
    try:
        async with session.post(
                "https://mobileproxy.passport.yandex.net/1/bundle/auth/x_token/",
                data={"type": "x-token", "retpath": "https://www.yandex.ru"},
                headers={"Ya-Consumer-Authorization": f"OAuth {x_token}"},
                timeout=aiohttp.ClientTimeout(total=20)) as r:
            resp = await r.json()
        if resp.get("status") != "ok" or not resp.get("track_id"):
            raise ValueError("паспорт не принял x_token: " + str(resp)[:120])
        host = resp.get("passport_host") or "https://passport.yandex.ru"
        async with session.get(f"{host}/auth/session/", params={"track_id": resp["track_id"]},
                               allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=20)) as r2:
            if r2.status not in (200, 302):
                raise ValueError(f"сессия не создана (HTTP {r2.status})")
    except Exception:
        await session.close()
        raise
    CLOUD_STATE["session"] = session
    CLOUD_STATE["csrf"] = None
    return resp


async def cloud_close_session() -> None:
    session = CLOUD_STATE["session"]
    CLOUD_STATE.update(session=None, csrf=None)
    if session is not None and not session.closed:
        try:
            await session.close()
        except Exception:
            pass


async def cloud_request(method: str, url: str, payload: Any = None, retry: int = 2) -> Any:
    """Запрос к облаку с куками сессии; для не-GET нужен x-csrf-token."""
    session = _cloud_session()
    headers: dict = {}
    if method.lower() != "get":
        csrf = CLOUD_STATE["csrf"]
        if not csrf:
            async with session.get("https://yandex.ru/iot",
                                   timeout=aiohttp.ClientTimeout(total=20)) as r:
                raw = await r.text()
            m = re.search(r'"csrfToken2":"(.+?)"', raw)
            if not m:
                raise HTTPException(status_code=502, detail="не удалось получить CSRF-токен облака")
            csrf = CLOUD_STATE["csrf"] = m[1]
        headers["x-csrf-token"] = csrf
    kwargs: dict = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=25)}
    if payload is not None:
        kwargs["json"] = payload
    async with session.request(method, url, **kwargs) as r:
        text = await r.text()
    if r.status == 403 and retry:
        CLOUD_STATE["csrf"] = None
        return await cloud_request(method, url, payload, retry - 1)
    if r.status == 401 and retry:
        x_token = str(cfg.get("yandex_x_token") or "")
        if x_token:
            await cloud_open_session(x_token)
            return await cloud_request(method, url, payload, retry - 1)
    if r.status != 200:
        raise HTTPException(status_code=502, detail=f"{url.split('//')[-1]}: HTTP {r.status}: {text[:140]}")
    try:
        return json.loads(text)
    except Exception:
        return text


@app.get("/api/alice/cloud")
async def api_alice_cloud_get(request: Request) -> JSONResponse:
    require_auth(request)
    x_token = str(cfg.get("yandex_x_token") or "")
    return JSONResponse({
        "ok": True,
        "configured": bool(x_token),
        "session_open": CLOUD_STATE["session"] is not None and not CLOUD_STATE["session"].closed,
        "login": CLOUD_STATE.get("login"),
    })


@app.put("/api/alice/cloud")
async def api_alice_cloud_put(request: Request) -> JSONResponse:
    """Сохранить куку аккаунта Яндекса: обмен на x_token и проверка.

    Кука берётся из браузера (alice.yandex.ru, F12 -> Сеть -> заголовок Cookie).
    В config.json панели хранится только x_token — кука не нужна после обмена."""
    require_auth(request)
    data = await request.json()
    cookie = str(data.get("cookie") or "").strip()
    if not cookie or "=" not in cookie:
        raise HTTPException(status_code=400, detail="вставьте строку заголовка Cookie (key=value; ...)")
    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(
                    "https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid",
                    data={"client_id": PASSPORT_CLIENT_ID, "client_secret": PASSPORT_CLIENT_SECRET},
                    headers={"Ya-Client-Host": "passport.yandex.ru", "Ya-Client-Cookie": cookie,
                             "User-Agent": CLOUD_UA},
                    timeout=aiohttp.ClientTimeout(total=25)) as r:
                resp = await r.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail="паспорт недоступен: " + str(exc)[:120])
    x_token = resp.get("access_token")
    if not x_token:
        raise HTTPException(status_code=400, detail="куки не приняты: " + str(resp.get("error_description") or resp.get("error") or resp)[:140])
    info = await cloud_short_info(x_token)
    if not info.get("uid"):
        raise HTTPException(status_code=400, detail="x_token получен, но аккаунт не читается: " + str(info)[:120])
    cfg["yandex_x_token"] = x_token
    save_config(cfg)
    try:
        await cloud_open_session(x_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="токен сохранён, но сессия не открыта: " + str(exc)[:140])
    login = str(info.get("login") or info.get("uid") or "?")
    CLOUD_STATE["login"] = login
    return JSONResponse({"ok": True, "login": login})


@app.delete("/api/alice/cloud")
async def api_alice_cloud_delete(request: Request) -> JSONResponse:
    """Забыть сохранённый x_token и закрыть сессию."""
    require_auth(request)
    await cloud_close_session()
    cfg.pop("yandex_x_token", None)
    save_config(cfg)
    CLOUD_STATE["login"] = None
    return JSONResponse({"ok": True})


@app.get("/api/alice/cloud/devices")
async def api_alice_cloud_devices(request: Request) -> JSONResponse:
    """Список устройств аккаунта из облака (проверка, что кука рабочая)."""
    require_auth(request)
    data = await cloud_request("GET", "https://iot.quasar.yandex.ru/m/v3/user/devices")
    if data.get("status") != "ok":
        raise HTTPException(status_code=502, detail="облако ответило: " + str(data)[:140])
    speakers: list[dict] = []
    for house in data.get("households", []):
        for dev in house.get("all", []):
            if str(dev.get("type", "")).startswith("devices.types.smart_speaker"):
                speakers.append({
                    "id": dev.get("id"),
                    "name": dev.get("name"),
                    "platform": (dev.get("quasar_info") or {}).get("platform") or dev.get("platform"),
                    "online": dev.get("online"),
                    "house": house.get("name"),
                })
    return JSONResponse({"ok": True, "login": CLOUD_STATE.get("login"), "speakers": speakers})


@app.get("/api/alice/cloud/config")
async def api_alice_cloud_config(request: Request) -> JSONResponse:
    """Полный облачный конфиг аккаунта (quasar.yandex.ru/get_account_config)."""
    require_auth(request)
    data = await cloud_request("GET", "https://quasar.yandex.ru/get_account_config")
    if data.get("status") != "ok":
        raise HTTPException(status_code=502, detail="облако ответило: " + str(data)[:140])
    return JSONResponse({"ok": True, "config": data.get("config")})


@app.post("/api/alice/cloud/config")
async def api_alice_cloud_config_post(request: Request) -> JSONResponse:
    """Дописать патч в облачный конфиг аккаунта (ключи как в user_config.dat)."""
    require_auth(request)
    data = await request.json()
    patch = data.get("patch")
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(status_code=400, detail="нужен непустой объект patch")
    current = await cloud_request("GET", "https://quasar.yandex.ru/get_account_config")
    if current.get("status") != "ok":
        raise HTTPException(status_code=502, detail="конфиг не прочитан: " + str(current)[:140])
    payload = current.get("config") or {}
    payload.update(patch)
    result = await cloud_request("POST", "https://quasar.yandex.ru/set_account_config", payload)
    if result.get("status") != "ok":
        raise HTTPException(status_code=502, detail="облако отклонило: " + str(result)[:140])
    return JSONResponse({"ok": True})


@app.post("/api/alice/cloud/settings")
async def api_alice_cloud_settings_post(request: Request) -> JSONResponse:
    """Тумблеры пользовательских настроек (DJ, шёпот, умный дом, заказы).

    Тело: {id: "c-dj", value: true} либо {payload: {...}} напрямую."""
    require_auth(request)
    data = await request.json()
    payload = data.get("payload")
    if payload is None:
        toggle_id = str(data.get("id") or "")
        toggle = CLOUD_SETTINGS_TOGGLES.get(toggle_id)
        if not toggle:
            raise HTTPException(status_code=400, detail=f"неизвестный тумблер {toggle_id}")
        payload = toggle["on" if bool(data.get("value")) else "off"]
    result = await cloud_request("POST", "https://iot.quasar.yandex.ru/m/user/settings", payload)
    if isinstance(result, dict) and result.get("status") not in (None, "ok"):
        raise HTTPException(status_code=502, detail="облако отклонило: " + str(result)[:140])
    return JSONResponse({"ok": True, "payload": payload})


@app.get("/api/alice/cloud/settings")
async def api_alice_cloud_settings_get(request: Request) -> JSONResponse:
    """Попытка прочитать текущие /m/user/settings (эндпоинт чтения не документирован)."""
    require_auth(request)
    try:
        data = await cloud_request("GET", "https://iot.quasar.yandex.ru/m/user/settings")
    except HTTPException as exc:
        return JSONResponse({"ok": False, "detail": str(exc.detail)[:100]})
    return JSONResponse({"ok": True, "settings": data})


@app.post("/api/alice/cloud/action")
async def api_alice_cloud_action(request: Request) -> JSONResponse:
    """Команда устройству через облако: {id, item_type, instance, value}.

    Например pause: instance="pause", value=true (toggle), громкость:
    instance="volume", value=0..100 (range). Тот же механизм, что у
    приложения «Дом с Алисой»."""
    require_auth(request)
    data = await request.json()
    device_id = str(data.get("id") or "")
    item_type = str(data.get("item_type") or "speaker")
    instance = str(data.get("instance") or "")
    value = data.get("value")
    if not device_id or not instance:
        raise HTTPException(status_code=400, detail="нужны id и instance")
    cap_type = str(data.get("type") or ("devices.capabilities.toggle" if instance in ("pause", "mute") else "devices.capabilities.range"))
    payload = {"actions": [{"type": cap_type, "state": {"instance": instance, "value": value}}]}
    result = await cloud_request(
        "POST", f"https://iot.quasar.yandex.ru/m/user/{item_type}s/{device_id}/actions", payload)
    if isinstance(result, dict) and result.get("status") not in (None, "ok"):
        raise HTTPException(status_code=502, detail="облако отклонило: " + str(result)[:140])
    return JSONResponse({"ok": True, "result": result})


# --------------------------------------------------------------------------- #
# Чек-лист: панель сама проверяет всё, что ей нужно, и умеет докачать.
# --------------------------------------------------------------------------- #
CHECKLIST_PIP_PACKAGES = ["fastapi", "uvicorn", "aiohttp", "asyncssh", "multipart", "zeroconf", "pycaw", "comtypes", "edge-tts"]
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"


def _has_tool(name: str) -> bool:
    return bin_tool(name) is not None


async def _download_to(url: str, dest: Path) -> str:
    """Скачать файл потоком (для больших архивов), вернуть размер в МБ."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=900, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status} от {url}")
            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 20):
                    f.write(chunk)
    return f"{dest.stat().st_size / 1048576:.1f}"


async def _checklist_items() -> list[dict]:
    import importlib.metadata
    import importlib.util

    items: list[dict] = []

    # 1. python-пакеты
    missing_pkgs = [p for p in CHECKLIST_PIP_PACKAGES if importlib.util.find_spec(p) is None]
    versions = []
    for p in CHECKLIST_PIP_PACKAGES:
        try:
            versions.append(f"{p} {importlib.metadata.version(p if p != 'multipart' else 'python-multipart')}")
        except importlib.metadata.PackageNotFoundError:
            pass
    items.append({
        "id": "pip", "title": "Python-пакеты панели",
        "status": "missing" if missing_pkgs else "ok",
        "detail": ("нет: " + ", ".join(missing_pkgs)) if missing_pkgs else " · ".join(versions),
        "hint": "fastapi/uvicorn/aiohttp/asyncssh/python-multipart — ставятся кнопкой в python -m pip.",
        "action": missing_pkgs and "Установить",
    })

    # 2-3. ffmpeg / ffplay
    for tool, title, hint in (
        ("ffmpeg", "FFmpeg (конвертация звука, TTS)", "Скачается с gyan.dev (~100 МБ), ляжет в папку панели bin\\."),
        ("ffplay", "FFplay (плеер «на ПК»)", "Ставится вместе с FFmpeg одной кнопкой."),
    ):
        ok = _has_tool(tool)
        items.append({"id": tool, "title": title, "status": "ok" if ok else "missing",
                      "detail": bin_tool(tool) or "не найден ни в PATH, ни в bin\\ панели",
                      "hint": hint, "action": None if ok else ("Установить" + ("" if tool != "ffmpeg" else " (~100 МБ)"))})

    # 4. yt-dlp
    ok = _has_tool("yt-dlp")
    items.append({"id": "ytdlp", "title": "yt-dlp (поиск музыки для плеера «на ПК»)",
                  "status": "ok" if ok else "missing",
                  "detail": bin_tool("yt-dlp") or "не найден",
                  "hint": "Один exe (~17 МБ) с GitHub, ляжет в bin\\ панели.",
                  "action": None if ok else "Установить"})

    # 5. ssh-ключ на колонке
    code, out, _ = await ssh_run_bytes(
        "[ -f /root/.ssh/authorized_keys ] && grep -c station-panel /root/.ssh/authorized_keys || echo 0")
    key_ok = code == 0 and out.decode().strip() not in ("", "0")
    items.append({"id": "sshkey", "title": "SSH-ключ панели на колонке",
                  "status": "ok" if key_ok else "missing",
                  "detail": "вход по ключу работает" if key_ok else "панель ходит по пустому паролю (dropbear -B)",
                  "hint": "Ключ избавляет от пустого пароля — потом можно выключить -B (см. «Аудит безопасности»).",
                  "action": None if key_ok else "Установить"})

    # 6. колонка: music-плагины + мост (один ssh-вызов)
    code, out, _ = await ssh_run_bytes(
        "echo S=$(command -v soundya >/dev/null 2>&1 && echo y || echo n)"
        "; echo P=$([ -d /data/opt/spotify-connect ] && echo y || echo n)"
        "; echo V=$([ -d /data/quazar/yam/data/plugins/station-voice ] && echo y || echo n)")
    text = out.decode() if code == 0 else "S=n; P=n; V=n"
    has = lambda tag: (re.search(tag + r"=(y|n)", text) or [None, "n"])[1] == "y"
    items.append({"id": "soundya", "title": "SoundCloud-плеер (soundya) на колонке",
                  "status": "ok" if has("S") else "missing",
                  "detail": "установлен, управление во вкладке «Музыка»" if has("S") else "не установлен",
                  "hint": "Ставится пакетом во вкладке «Пакеты» (реестр yapkg).", "action": None})
    items.append({"id": "spotiya", "title": "Spotify Connect (spotiya) на колонке",
                  "status": "ok" if has("P") else "missing",
                  "detail": "установлен" if has("P") else "не установлен",
                  "hint": "Пакет во вкладке «Пакеты».", "action": None})
    items.append({"id": "bridge", "title": "Голосовой мост (свои команды)",
                  "status": "ok" if has("V") else "missing",
                  "detail": "плагин station-voice стоит на колонке" if has("V") else "плагина station-voice нет на колонке",
                  "hint": "Ставится из «Системы» → «Голосовые команды» → «Показать ключи»/мастер.", "action": None})

    # 7. демон слов на 8780
    ww = "missing"
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(cfg["station_ip"], 8780), 0.7)
        w.close()
        ww = "ok"
    except Exception:
        pass
    items.append({"id": "wakeword", "title": "Демон слов активации (:8780)",
                  "status": ww,
                  "detail": "работает, вкладка «Слова» активна" if ww == "ok" else "не запущен на колонке",
                  "hint": "Требует отдельного бинаря под модель колонки — пока ставится вручную.",
                  "action": None})
    return items


@app.get("/api/checklist")
async def api_checklist(request: Request) -> JSONResponse:
    require_auth(request)
    return JSONResponse({"ok": True, "items": await _checklist_items()})


@app.post("/api/checklist/install")
async def api_checklist_install(request: Request) -> JSONResponse:
    """Установка одного пункта чек-листа: pip | ffmpeg | ytdlp | sshkey."""
    require_auth(request)
    data = await request.json()
    what = str(data.get("id") or "")
    PANEL_BIN.mkdir(parents=True, exist_ok=True)

    if what == "pip":
        req = BASE / "requirements.txt"
        if not req.exists():
            raise HTTPException(status_code=404, detail="requirements.txt не найден рядом с панелью")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "-r", str(req), "--disable-pip-version-check",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), 600)
        tail = out.decode("utf-8", "replace").strip().splitlines()[-3:]
        if proc.returncode != 0:
            raise HTTPException(status_code=502, detail="pip: " + " | ".join(tail)[:200])
        items = await _checklist_items()
        return JSONResponse({"ok": True, "output": " | ".join(tail), "items": items})

    if what in ("ffmpeg", "ffplay", "ytdlp"):
        if what == "ytdlp":
            dest = PANEL_BIN / "yt-dlp.exe" if os.name == "nt" else PANEL_BIN / "yt-dlp"
            mb = await _download_to(YTDLP_URL, dest)
            if os.name != "nt":
                dest.chmod(0o755)
            return JSONResponse({"ok": True, "output": f"yt-dlp скачан ({mb} МБ) → {dest}"})
        # ffmpeg: zip ~100 МБ, внутри ffmpeg-…-essentials_build/bin/*.exe
        import zipfile
        dest = PANEL_BIN / "ffmpeg-essentials.zip"
        mb = await _download_to(FFMPEG_URL, dest)
        wanted = ("ffmpeg.exe", "ffplay.exe", "ffprobe.exe") if os.name == "nt" else ("ffmpeg", "ffplay", "ffprobe")
        taken = []
        with zipfile.ZipFile(dest) as z:
            for info in z.infolist():
                base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                if base in wanted:
                    with open(PANEL_BIN / base, "wb") as f:
                        f.write(z.read(info))
                    taken.append(base)
        dest.unlink(missing_ok=True)
        if "ffmpeg.exe" not in taken and "ffmpeg" not in taken:
            raise HTTPException(status_code=502, detail="в архиве не нашлись ffmpeg.exe/ffplay.exe")
        return JSONResponse({"ok": True, "output": f"ffmpeg скачан ({mb} МБ), извлечено: {', '.join(taken)} → bin\\"})

    if what == "sshkey":
        result = await setup_ssh_key()
        if not result["installed"]:
            raise HTTPException(status_code=502, detail="ключ не установился: " + result["error"][:180])
        return JSONResponse({"ok": True, "output": "ключ панели установлен на колонку"})

    raise HTTPException(status_code=400, detail=f"неизвестный пункт: {what}")


# --------------------------------------------------------------------------- #
# Каталог пакетов: реестр yapkg -> скачивание + sha256 + установка в YAM
# --------------------------------------------------------------------------- #
YAPKG_REGISTRY = "https://raw.githubusercontent.com/nyakokitsu/yapkg/registry/index.json"
_catalog_cache: dict[str, Any] = {"ts": 0.0, "pkgs": []}


async def _fetch_registry() -> list:
    if time.time() - _catalog_cache["ts"] < 600 and _catalog_cache["pkgs"]:
        return _catalog_cache["pkgs"]
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(YAPKG_REGISTRY) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=502, detail=f"реестр: HTTP {resp.status}")
            data = await resp.json(content_type=None)
    pkgs = data.get("packages", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    _catalog_cache.update(ts=time.time(), pkgs=pkgs)
    return pkgs


@app.get("/api/packages/catalog")
async def api_packages_catalog(request: Request) -> JSONResponse:
    """Каталог yapkg: пакеты с пометками installed/running из YAM."""
    require_auth(request)
    try:
        pkgs = await _fetch_registry()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"реестр недоступен: {exc}"[:160])
    installed: dict[str, bool] = {}
    try:
        st, plugins = await yam_api("GET", "/api/plugins")
        for p in (plugins if isinstance(plugins, list) else []):
            m = p.get("manifest") or {}
            if m.get("id"):
                installed[str(m["id"])] = bool(p.get("running"))
    except Exception:
        pass
    by_id: dict[str, dict] = {}
    for p in pkgs:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        cur = by_id.get(pid)
        if cur is None or str(p.get("version") or "") >= str(cur.get("version") or ""):
            by_id[pid] = p
    items = []
    for pid, p in sorted(by_id.items()):
        items.append({
            "id": pid,
            "name": p.get("name") or pid,
            "version": p.get("version") or "",
            "description": str(p.get("description") or "")[:220],
            "sha256": p.get("sha256") or "",
            "installed": pid in installed,
            "running": installed.get(pid) is True,
        })
    return JSONResponse({"ok": True, "items": items})


@app.post("/api/packages/catalog/install")
async def api_packages_catalog_install(request: Request) -> JSONResponse:
    """Скачать .yap из реестра (проверка sha256) и установить в YAM с автозапуском."""
    require_auth(request)
    data = await request.json()
    pid = str(data.get("id") or "")
    if not pid:
        raise HTTPException(status_code=400, detail="нужен id пакета")
    pkgs = await _fetch_registry()
    entry = None
    for p in pkgs:
        if str(p.get("id") or "") == pid:
            if entry is None or str(p.get("version") or "") >= str(entry.get("version") or ""):
                entry = p
    if not entry or not entry.get("url"):
        raise HTTPException(status_code=404, detail="пакет не найден в реестре")
    timeout = aiohttp.ClientTimeout(total=600, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(str(entry["url"])) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=502, detail=f"скачивание: HTTP {resp.status}")
            blob = await resp.read()
    if entry.get("sha256"):
        import hashlib
        got = hashlib.sha256(blob).hexdigest()
        if got != str(entry["sha256"]).lower():
            raise HTTPException(status_code=502, detail=f"sha256 не совпал: {got[:20]}…")
    import io as _io

    boundary = "----stationpanel" + secrets.token_hex(8)
    part = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="package"; filename="{pid}.yap"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + blob + f"\r\n--{boundary}--\r\n".encode("utf-8")
    status, body = await yam_api("POST", "/api/plugins", data=part,
                                 content_type=f"multipart/form-data; boundary={boundary}")
    if status not in (200, 201):
        return JSONResponse({"detail": f"YAM вернул {status}: {str(body)[:200]}"}, status_code=502)
    st1, _ = await yam_api("POST", f"/api/plugins/{pid}/toggle")
    st2, _ = await yam_api("POST", f"/api/plugins/{pid}/start")
    return JSONResponse({"ok": True, "id": pid, "version": entry.get("version"),
                         "size": len(blob), "toggle": st1, "start": st2})


# --------------------------------------------------------------------------- #
# Автонастройка: SSH-ключ, SFTP, автодетект параметров
# --------------------------------------------------------------------------- #
def ssh_key_public_line() -> str | None:
    if KEY_PUB.exists():
        return KEY_PUB.read_text(encoding="utf-8").strip()
    return None


async def setup_ssh_key() -> dict[str, Any]:
    """Сгенерировать ключ панели и установить его в authorized_keys колонки.

    Идемпотентно: перед добавлением убираем свою старую строку, поэтому
    повторный запуск не плодит дубли в ~/.ssh/authorized_keys. Корневая ФС
    колонки смонтирована read-only — на время записи делаем remount rw/ro,
    успех подтверждаем только фактическим наличием ключа в файле.
    """
    KEY_DIR.mkdir(exist_ok=True)
    if not KEY_PRIV.exists():
        key = asyncssh.generate_private_key("ssh-ed25519")
        key.write_private_key(str(KEY_PRIV))
        pub = key.export_public_key("openssh")
        KEY_PUB.write_text(f"{pub} station-panel\n", encoding="utf-8")
        KEY_PRIV.chmod(0o600)
        print("[+] создан SSH-ключ панели: keys/id_ed25519")
    pub_line = KEY_PUB.read_text(encoding="utf-8").strip()

    script = (
        'umask 077; mkdir -p ~/.ssh && '
        'grep -v "station-panel" ~/.ssh/authorized_keys > ~/.ssh/ak_new 2>/dev/null; '
        f'echo "{pub_line}" >> ~/.ssh/ak_new && '
        'mv ~/.ssh/ak_new ~/.ssh/authorized_keys && '
        'chmod 600 ~/.ssh/authorized_keys && '
        'grep -q station-panel ~/.ssh/authorized_keys && echo OK'
    )
    error = ""
    try:
        conn = await ssh_connect()
    except Exception as exc:
        return {"installed": False, "public_key": pub_line, "error": f"{type(exc).__name__}: {exc}"[:200]}
    try:
        try:
            await asyncio.wait_for(conn.run("mount -o remount,rw /", check=False, encoding="utf-8"), 15)
            result = await asyncio.wait_for(conn.run(script, check=False, encoding="utf-8"), 25)
            ok = result.exit_status == 0 and "OK" in (result.stdout or "")
            if not ok:
                error = ((result.stderr or "").strip() or "ключ не появился в authorized_keys")[:200]
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            try:
                await asyncio.wait_for(conn.run("mount -o remount,ro /", check=False, encoding="utf-8"), 15)
            except Exception:
                pass
    finally:
        conn.close()
    return {"installed": ok, "public_key": pub_line, "error": error}


async def sftp_probe() -> dict[str, Any]:
    """Проверка SFTP-подсистемы.

    dropbear обычно собран без sftp-server — файловый менеджер панели
    в этом случае работает через exec-каналы SSH, это не ошибка.
    """
    try:
        async with await ssh_connect() as conn:
            try:
                sftp = await conn.start_sftp_client()
                await sftp.listdir(".")
                return {"sftp": "subsystem", "detail": "SFTP-подсистема доступна"}
            except (asyncssh.SFTPError, OSError, NotImplementedError) as exc:
                return {"sftp": "no", "detail": f"подсистема недоступна: {exc}"[:200]}
    except Exception as exc:
        return {"sftp": "no", "detail": f"SSH недоступен: {exc}"[:200]}


def _probe_ip_sync(ip: str) -> str | None:
    """Синхронная проверка одного IP: отвечает ли YAM (для asyncio.to_thread)."""
    import urllib.request

    ports: list[int] = []
    for port in (cfg["yam_port"], 8099, 8080, 80):
        if port not in ports:
            ports.append(int(port))
    for port in ports:
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"http://{ip}:{port}/api/device", timeout=1.5
            ) as resp:
                if resp.status == 200:
                    device = json.loads(resp.read(4096))
                    if isinstance(device, dict) and device.get("model"):
                        return ip
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# развёртывание панели на VPS по SFTP
# --------------------------------------------------------------------------- #
VPS_JOBS: dict[str, dict[str, Any]] = {}


def _vps_requirements() -> bytes:
    """requirements для контейнера: linux-набор + websockets для WS-терминала."""
    text = (BASE / "requirements-linux.txt").read_text(encoding="utf-8")
    if "websockets" not in text:
        text += "\n# WS uvicorn (терминал в браузере; на VPS uvicorn без [standard])\nwebsockets>=12\n"
    return text.encode("utf-8")


def _vps_config_payload(panel_version: str = "") -> bytes:
    """Конфиг для сервера: станция приходит через chisel-туннель (127.0.0.1),
    панель слушает только localhost, пароль панели наследуется."""
    payload = {
        "station_ip": "127.0.0.1",
        "ssh_port": int(cfg.get("ssh_port", 2222)),
        "ssh_user": cfg.get("ssh_user", "root"),
        "yam_port": int(cfg.get("yam_port", 8099)),
        "wakeword_port": int(cfg.get("wakeword_port", 8780)),
        "panel_port": 8787,
        "panel_host": "127.0.0.1",
        "password_salt": cfg.get("password_salt", ""),
        "password_hash": cfg.get("password_hash", ""),
        "session_hours": cfg.get("session_hours", 72),
        "voice_bridge_url": "http://127.0.0.1:8765/api/voice",
        "voice_bridge_token": cfg.get("voice_bridge_token", ""),
        "voice_music_source": cfg.get("voice_music_source", "youtube"),
        "bridge_enabled": False,
        "say_engine": "ipc",
        "yandex_x_token": cfg.get("yandex_x_token", ""),
        "panel_version": panel_version,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _vps_package(include_keys: bool) -> list[tuple[str, Any, int]]:
    """Состав поставки: (путь на сервере, источник: путь или bytes, права)."""
    items: list[tuple[str, Any, int]] = [
        ("app.py", BASE / "deploy" / "container" / "app.py", 0o755),
        ("chisel", BASE / "deploy" / "bin" / "chisel", 0o755),
        ("requirements.txt", _vps_requirements(), 0o644),
        ("panel/server.py", BASE / "server.py", 0o644),
        ("panel/config.json", _vps_config_payload(), 0o600),
        ("bridge/app.py", (BASE.parent / "voice-bridge" / "app.py"), 0o644),
        ("bridge/bridge-config.json",
         json.dumps({"source": cfg.get("voice_music_source", "youtube")}).encode("utf-8"), 0o644),
    ]
    if include_keys:
        for name in ("id_ed25519", "id_ed25519.pub"):
            p = BASE / "keys" / name
            if p.exists():
                items.append((f"panel/keys/{name}", p, 0o600 if name == "id_ed25519" else 0o644))
    for p in STATIC_DIR.rglob("*"):
        if p.is_file():
            items.append((f"panel/static/{p.relative_to(STATIC_DIR).as_posix()}", p, 0o644))
    return items


async def _vps_deploy_job(job_id: str, host: str, port: int, user: str,
                          password: str, panel_port: int, include_keys: bool,
                          setup_tunnel: bool, panel_version: str = "") -> None:
    """Залить поставку по SFTP (paramiko: wings-серверы нестабильны на asyncssh)
    и, при желании, переподключить туннель колонки."""
    job = VPS_JOBS[job_id]
    job["status"] = "подключение"

    def log(line: str) -> None:
        job["lines"].append(line)

    def sync_upload() -> None:
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("нет пакета paramiko: pip install paramiko") from exc

        transport = paramiko.Transport((host, port))
        transport.connect(username=user, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        log(f"подключено: {host}:{port}")

        package = _vps_package(include_keys)
        package = [(r, s, m) if r != "panel/config.json"
                   else (r, _vps_config_payload(panel_version), m) for r, s, m in package]
        made_dirs: set[str] = set()

        def ensure_dir(rdir: str) -> None:
            if not rdir or rdir in made_dirs:
                return
            parent = str(PurePosixPath(rdir).parent)
            if parent not in (".", "/"):
                ensure_dir(parent)
            try:
                sftp.mkdir(rdir)
            except OSError:
                pass
            made_dirs.add(rdir)

        for rpath, source, mode in package:
            try:
                sftp.remove(rpath)  # wings не даёт перезаписывать существующие файлы
            except OSError:
                pass
            if isinstance(source, (bytes, bytearray)):
                ensure_dir(str(PurePosixPath(rpath).parent))
                with sftp.open(rpath, "wb") as f:
                    f.write(bytes(source))
            else:
                p = Path(source)
                if not p.exists():
                    log(f"пропуск (нет файла): {rpath}")
                    continue
                ensure_dir(str(PurePosixPath(rpath).parent))
                sftp.put(str(p), rpath)
            sftp.chmod(rpath, mode)
            log(f"✓ {rpath}")

        job["status"] = "проверка"
        for probe in ("/app.py", "/chisel", "/panel/server.py", "/panel/static/index.html"):
            st = sftp.stat(probe)
            if not st.st_size:
                raise RuntimeError(f"файл пуст после загрузки: {probe}")
        log("проверка: файлы на месте")
        sftp.close()
        transport.close()

    try:
        await asyncio.to_thread(sync_upload)

        if setup_tunnel and panel_port:
            ip = socket.gethostbyname(host)
            url = f"http://{ip}:{panel_port}/chisel"
            # chisel.env правится перезаписью целиком: sed с вложенными кавычками
            # на busybox рвёт файл (был случай SERVER_URL=http::80)
            code, out, _ = await ssh_run("cat /data/opt/chisel/chisel.env 2>/dev/null", timeout=15)
            lines = [l for l in out.splitlines() if l.strip() and not l.startswith("SERVER_URL=")]
            env_text = f'SERVER_URL="{url}"\n' + "\n".join(lines) + "\n"
            code, _, err = await ssh_run_bytes(
                "cat > /data/opt/chisel/chisel.env", input_data=env_text.encode("utf-8"), timeout=20)
            if code != 0:
                raise RuntimeError("не удалось записать chisel.env: " + err.decode("utf-8", "replace")[:100])
            await ssh_run("/etc/init.d/S97chisel restart", timeout=30)
            log("колонка: туннель переподключён на " + url)

        job["ok"] = True
        job["status"] = "готово"
        log("Дальше: 1) запусти сервер в панели хостинга (если ещё не запущен);")
        log("2) первый старт ставит зависимости ~1 мин;")
        log("3) открой http://<IP>:<порт панели> — логин и пароль такие же, как здесь;")
        log("4) HTTPS: добавь домен в Cloudflare (A-запись + Origin Rule на порт панели + SSL Flexible).")
    except Exception as exc:
        job["ok"] = False
        job["status"] = "ошибка"
        log(f"ошибка: {type(exc).__name__}: {str(exc)[:200]}")


@app.post("/api/vps/deploy")
async def api_vps_deploy(request: Request) -> JSONResponse:
    """Запустить развёртывание панели на сервер по SFTP (фоновая задача)."""
    require_auth(request)
    data = await request.json()
    host = str(data.get("host") or "").strip()
    user = str(data.get("user") or "").strip()
    password = str(data.get("password") or "")
    port = int(data.get("port") or 22)
    panel_port = int(data.get("panel_port") or 0)
    include_keys = bool(data.get("include_keys", True))
    setup_tunnel = bool(data.get("setup_tunnel", True))
    if not host or not user or not password:
        raise HTTPException(status_code=400, detail="заполни адрес, логин и пароль SFTP")
    if not (BASE / "deploy" / "container" / "app.py").exists():
        raise HTTPException(status_code=400, detail="в установке нет deploy/container/app.py — обнови панель")

    now = time.time()
    for jid in [j for j, jv in VPS_JOBS.items() if now - jv.get("ts", now) > 3600]:
        VPS_JOBS.pop(jid, None)

    job_id = secrets.token_hex(4)
    job = {"status": "запуск", "lines": [], "ok": None, "ts": now}
    VPS_JOBS[job_id] = job
    try:
        panel_version = str(cfg.get("panel_version") or "") or _latest_release_tag(_update_repo_slug())
    except Exception:
        panel_version = str(cfg.get("panel_version") or "")
    task = asyncio.create_task(_vps_deploy_job(
        job_id, host, port, user, password, panel_port, include_keys, setup_tunnel, panel_version))
    job["task"] = task
    return JSONResponse({"ok": True, "job": job_id})


@app.get("/api/vps/deploy/status")
async def api_vps_deploy_status(request: Request, job: str = "") -> JSONResponse:
    require_auth(request)
    job_data = VPS_JOBS.get(job)
    if not job_data:
        raise HTTPException(status_code=404, detail="задача не найдена")
    return JSONResponse({"status": job_data["status"], "lines": job_data["lines"],
                         "ok": job_data["ok"]})


@app.post("/api/setup/ssh-key")
async def api_setup_ssh_key(request: Request) -> JSONResponse:
    """Установить (или обновить) SSH-ключ панели на колонке."""
    require_auth(request)
    try:
        result = await setup_ssh_key()
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse(
            {"installed": False, "public_key": None, "error": f"{type(exc).__name__}: {exc}"},
            status_code=502,
        )


@app.get("/api/setup/ssh-key")
async def api_setup_ssh_key_get(request: Request) -> JSONResponse:
    """Показать публичный ключ панели без установки."""
    require_auth(request)
    line = ssh_key_public_line()
    return JSONResponse({"exists": bool(line), "public_key": line})


@app.get("/api/setup/sftp-probe")
async def api_setup_sftp_probe(request: Request) -> JSONResponse:
    require_auth(request)
    return JSONResponse(await sftp_probe())


@app.post("/api/setup/full")
async def api_setup_full(request: Request) -> JSONResponse:
    """Полная автонастройка: детект IP → порт YAM → SSH → ключ → SFTP.

    Каждый шаг не роняет следующий: то, что не нашлось, помечается в steps.
    """
    require_auth(request)
    steps: list[dict[str, Any]] = []

    # 1. детект IP — дублируем логику /api/detect через прямой вызов probe
    found_ip: str | None = None
    scanned = 0
    if cfg.get("station_ip"):
        try:
            timeout = aiohttp.ClientTimeout(total=1.2)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"http://{cfg['station_ip']}:{cfg['yam_port']}/api/device") as resp:
                    if resp.status == 200:
                        device = await resp.json(content_type=None)
                        if isinstance(device, dict) and device.get("model"):
                            found_ip = cfg["station_ip"]
        except Exception:
            pass
    if not found_ip:
        network_prefixes = _local_subnets()
        if current and ".".join(current.split(".")[:3]) not in network_prefixes:
            network_prefixes.append(".".join(current.split(".")[:3]))
        candidates = []
        for prefix in network_prefixes:
            if not all(p.isdigit() for p in prefix.split(".")):
                continue
            local_octet = current.split(".")[3] if current.startswith(prefix + ".") else None
            candidates.extend(f"{prefix}.{o}" for o in range(1, 255) if str(o) != local_octet)
        if candidates:
            for i in range(0, len(candidates), 40):
                batch = candidates[i:i + 40]
                scanned += len(batch)
                results = await asyncio.gather(*(asyncio.to_thread(_probe_ip_sync, ip) for ip in batch))
                hit = next((ip for ip in results if ip), None)
                if hit:
                    found_ip = hit
                    break
    if not found_ip and not candidates:
        return JSONResponse({"ok": False, "steps": steps,
                             "error": "не удалось определить локальную сеть — задайте IP станции вручную"})
    steps.append({"step": "IP станции", "ok": bool(found_ip),
                  "detail": found_ip or "не найдена — проверьте, что колонка в той же сети"})
    if not found_ip:
        return JSONResponse({"ok": False, "steps": steps, "error": "колонка не найдена в сети"})
    if found_ip != cfg["station_ip"]:
        cfg["station_ip"] = found_ip
        _status_cache["ts"] = 0.0

    # 2. порт YAM
    yam_port = None
    for port in (cfg["yam_port"], 8099, 8080, 80):
        try:
            timeout = aiohttp.ClientTimeout(total=1.5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"http://{found_ip}:{port}/api/device") as resp:
                    if resp.status == 200:
                        yam_port = port
                        break
        except Exception:
            continue
    if yam_port:
        cfg["yam_port"] = yam_port
    steps.append({"step": "Порт YAM", "ok": bool(yam_port), "detail": str(yam_port or "не найден")})

    # 3. SSH-порт
    ssh_ok = False
    ssh_ports = [cfg["ssh_port"], 2222, 22] if cfg["ssh_port"] not in (2222, 22) else [cfg["ssh_port"], 22]
    for port in ssh_ports:
        cfg["ssh_port"] = port
        try:
            async with await ssh_connect() as conn:
                result = await asyncio.wait_for(conn.run("echo alive", check=False, encoding="utf-8"), 8)
                if (result.exit_status or 0) == 0 and "alive" in (result.stdout or ""):
                    ssh_ok = True
                    break
        except Exception:
            continue
    steps.append({"step": "SSH", "ok": ssh_ok, "detail": f"порт {cfg['ssh_port']}" if ssh_ok else "не отвечает на портах 2222/22"})
    save_config(cfg)
    if not ssh_ok:
        return JSONResponse({"ok": False, "steps": steps, "error": "SSH недоступен"})

    # 4. SSH-ключ
    key_res = await setup_ssh_key()
    steps.append({"step": "SSH-ключ", "ok": key_res["installed"],
                  "detail": "установлен" if key_res["installed"] else (key_res["error"] or "не установился")})

    # 5. проверка входа по ключу (отключаем fallback на пустой пароль)
    if key_res["installed"]:
        try:
            async with await asyncssh.connect(
                cfg["station_ip"], port=cfg["ssh_port"], username=cfg["ssh_user"],
                client_keys=[str(KEY_PRIV)], password="", known_hosts=None, login_timeout=10,
            ) as conn:
                result = await conn.run("echo key-login", check=False, encoding="utf-8")
                key_login = "key-login" in (result.stdout or "")
        except Exception:
            key_login = False
        steps.append({"step": "Вход по ключу", "ok": key_login,
                      "detail": "работает" if key_login else "пока вход по пустому паролю"})
        if key_login and not cfg.get("ssh_key_only"):
            cfg["ssh_key_only"] = True
            save_config(cfg)

    # 6. SFTP
    sftp_res = await sftp_probe()
    if sftp_res["sftp"] == "subsystem":
        steps.append({"step": "SFTP", "ok": True, "detail": "SFTP-подсистема доступна — файлы идут через неё"})
    else:
        steps.append({"step": "SFTP", "ok": True,
                      "detail": "SFTP-подсистемы нет (обычно у dropbear) — файловый менеджер работает через SSH-каналы"})

    steps.append({"step": "Готово", "ok": True, "detail": "автонастройка завершена"})
    return JSONResponse({"ok": True, "steps": steps})


@app.get("/api/config")
async def api_get_config(request: Request) -> JSONResponse:
    require_auth(request)
    return JSONResponse(
        {
            "station_ip": cfg["station_ip"],
            "ssh_port": cfg["ssh_port"],
            "ssh_user": cfg["ssh_user"],
            "yam_port": cfg["yam_port"],
        }
    )


@app.put("/api/config")
async def api_put_config(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    if data.get("station_ip"):
        ip = str(data["station_ip"]).strip()
        parts = ip.split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            raise HTTPException(status_code=400, detail=f"некорректный IP станции: {ip!r}")
        cfg["station_ip"] = ip
    for key in ("ssh_port", "yam_port"):
        if data.get(key):
            cfg[key] = int(data[key])
    if data.get("ssh_user"):
        cfg["ssh_user"] = str(data["ssh_user"]).strip()
    save_config(cfg)
    _status_cache["ts"] = 0.0
    return JSONResponse({"ok": True})


@app.post("/api/password")
async def api_password(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    current, new = str(data.get("current", "")), str(data.get("new", ""))
    if not check_password(current):
        raise HTTPException(status_code=403, detail="текущий пароль неверен")
    if len(new) < 4:
        raise HTTPException(status_code=400, detail="новый пароль короче 4 символов")
    cfg["password_salt"] = secrets.token_hex(16)
    cfg["password_hash"] = hash_password(new, cfg["password_salt"])
    save_config(cfg)
    if PASSWORD_HINT.exists():
        PASSWORD_HINT.write_text("Пароль веб-панели Станции (смените его в панели):\n" + new + "\n", encoding="utf-8")
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# Самообновление панели из GitHub
# --------------------------------------------------------------------------- #
DEFAULT_UPDATE_REPO = "c4elovek-cmd/station-panel"


def _update_repo_slug() -> str:
    git = shutil.which("git")
    if git and (BASE / ".git").exists():
        try:
            proc = subprocess.run(
                [git, "remote", "get-url", "origin"], cwd=str(BASE), capture_output=True,
                text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if proc.returncode == 0:
                slug = re.sub(r"^(?:https?://|git@)?[^:/]*[:/]+github\.com[:/]?", "", proc.stdout.strip())
                slug = re.sub(r"\.git$", "", slug)
                if slug:
                    return slug
        except Exception:
            pass
    return str(cfg.get("update_repo") or DEFAULT_UPDATE_REPO)


def _latest_release_tag(slug: str) -> str:
    import urllib.request

    req = urllib.request.Request(
        f"https://api.github.com/repos/{slug}/releases/latest",
        headers={"User-Agent": "station-panel", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace")).get("tag_name", "")


@app.get("/api/panel/update")
async def api_panel_update_check(request: Request) -> JSONResponse:
    """Сравнить локальную версию с последней в Git-репозитории на GitHub."""
    require_auth(request)
    slug = _update_repo_slug()
    root_dir = Path(__file__).resolve().parent
    if not (root_dir / ".git").exists():
        # SFTP-развёртывание без git: версия берётся из config, обновление — tarball
        try:
            latest = _latest_release_tag(slug)
        except Exception as exc:
            return JSONResponse({"ok": False, "detail": f"GitHub недоступен: {str(exc)[:140]}"}, status_code=500)
        local = str(cfg.get("panel_version") or "")
        return JSONResponse({
            "ok": True, "git": False, "repo": slug, "local": local, "latest": latest,
            "up_to_date": bool(local) and local == latest,
        })

    git = shutil.which("git")
    if not git:
        return JSONResponse({"ok": False, "detail": "git не найден в PATH"}, status_code=500)
    try:
        async def run(args: str) -> str:
            proc = await asyncio.create_subprocess_exec(
                git, *shlex.split(args), cwd=str(root_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), 15)
            if proc.returncode != 0:
                raise RuntimeError(err.decode("utf-8", "replace").strip() or args)
            return out.decode("utf-8", "replace").strip()

        local_commit = await run("rev-parse HEAD")
        remote_url = await run("remote get-url origin")
        # https://github.com/owner/repo(.git) или git@github.com:owner/repo(.git) → owner/repo
        slug = re.sub(r"^(?:https?://|git@)?[^:/]*[:/]+github\.com[:/]?", "", remote_url.strip())
        slug = re.sub(r"\.git$", "", slug)
        await run("fetch origin --quiet")
    except Exception as exc:
        return JSONResponse({"ok": False, "detail": str(exc)[:400]}, status_code=500)
    try:
        remote_commit = (await run("rev-parse FETCH_HEAD")).split()[0]
    except Exception as exc:
        return JSONResponse({"ok": False, "detail": str(exc)[:400]}, status_code=500)
    up_to_date = local_commit == remote_commit
    return JSONResponse({
        "ok": True, "commit": local_commit[:7], "remote_commit": remote_commit[:7],
        "repo": slug, "up_to_date": up_to_date,
    })


def _sync_tarball_update(slug: str) -> tuple[bool, str]:
    """Обновление без git: скачать tarball ветки main и заменить файлы панели."""
    import io
    import tarfile
    import urllib.request

    url = f"https://codeload.github.com/{slug}/tar.gz/refs/heads/main"
    req = urllib.request.Request(url, headers={"User-Agent": "station-panel"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    root_prefix = None
    replaced: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            rel = m.name
            if "/" not in rel:
                continue
            prefix, rest = rel.split("/", 1)
            if root_prefix is None:
                root_prefix = prefix
            if prefix != root_prefix:
                continue
            data = tf.extractfile(m).read()
            if rest == "server.py":
                (BASE / "server.py").write_bytes(data)
                replaced.append("server.py")
            elif rest.startswith("static/"):
                dest = BASE / rest
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                replaced.append(rest)
            elif rest == "deploy/container/app.py" and (BASE.parent / "app.py").exists():
                (BASE.parent / "app.py").write_bytes(data)
                replaced.append("app.py (супервизор)")
    if "server.py" not in replaced:
        return False, "в архиве не найден server.py — странный релиз, обновление отменено"
    try:
        tag = _latest_release_tag(slug)
    except Exception:
        tag = ""
    if tag:
        cfg["panel_version"] = tag
        save_config(cfg)
    return True, "обновлено: " + ", ".join(replaced[:6]) + ("…" if len(replaced) > 6 else "")


def _delayed_self_restart() -> None:
    """Убить свой процесс: супервизор (VPS) перезапустит панель, на ПК — start.ps1/планировщик."""
    async def _kill():
        await asyncio.sleep(2)
        os.kill(os.getpid(), 15)  # SIGTERM
    asyncio.create_task(_kill())


@app.post("/api/panel/update")
async def api_panel_update(request: Request) -> JSONResponse:
    """git pull (или tarball без git) → перезапуск панели."""
    require_auth(request)
    root_dir = Path(__file__).resolve().parent
    slug = _update_repo_slug()

    if not (root_dir / ".git").exists():
        ok, log = await asyncio.get_event_loop().run_in_executor(None, _sync_tarball_update, slug)
        if not ok:
            return JSONResponse({"detail": log}, status_code=500)
        _delayed_self_restart()
        return JSONResponse({"ok": True, "log": log,
                             "note": "панель перезапустится сама; если менялись зависимости — рестартни контейнер в консоли хостинга"})

    git = shutil.which("git")

    def _sync_pull() -> tuple[bool, str]:
        if not git:
            return False, "git не найден в PATH"
        try:
            proc = subprocess.run(
                [git, "pull", "--ff-only"], cwd=root_dir, capture_output=True,
                text=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if proc.returncode != 0:
                return False, (proc.stderr or proc.stdout or "pull failed")[:500]
        except Exception as exc:
            return False, str(exc)[:400]
        pip = shutil.which("pip") or shutil.which("python")
        try:
            if shutil.which("pip"):
                subprocess.run(["pip", "install", "-r", "requirements.txt", "--quiet"],
                               cwd=root_dir, capture_output=True, text=True, timeout=180,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception:
            pass  # зависимости могли не поменяться — не критично
        return True, proc.stdout[-300:]

    ok, log = await asyncio.get_event_loop().run_in_executor(None, _sync_pull)
    if not ok:
        return JSONResponse({"detail": log}, status_code=500)
    return JSONResponse({"ok": True, "log": log, "note": "панель сейчас перезапустится"})


# --------------------------------------------------------------------------- #
# Голосовые команды: плагин на колонке с кастомными фразами
# --------------------------------------------------------------------------- #
VOICE_PLUGIN_ID = "station-voice"
VOICE_LUA_TEMPLATE = """\
-- Плагин панели Станции: слушает распознанную речь и шлёт запросы на мост.
-- Триггеры и адрес моста задаются в панели, файл генерируется автоматически.
local TRIGGERS = {__PHRASES__}
local BRIDGE_URL = __BRIDGE_URL__
local BRIDGE_TOKEN = __TOKEN__

-- Медиа-команды без слова «Алиса»: точные фразы -> команда мосту
local MEDIA = {
  ["стоп"] = "stop",
  ["пауза"] = "pause",
  ["продолжи"] = "play",
  ["дальше"] = "next",
  ["следующий"] = "next",
  ["следующий трек"] = "next",
  ["назад"] = "prev",
  ["предыдущий"] = "prev",
  ["предыдущий трек"] = "prev",
}

local function normalize(text)
  -- как в yamd: буквы/цифры остаются, остальное — пробел; пробелы схлопываем
  local out = tostring(text):lower():gsub("[^%wа-яё ]", " ")
  local parts = {}
  for word in out:gmatch("%S+") do parts[#parts + 1] = word end
  return table.concat(parts, " ")
end

local function match_trigger(transcript)
  local spoken = normalize(transcript)
  if spoken == "" then return nil end
  for _, trigger in ipairs(TRIGGERS) do
    local t = normalize(trigger)
    if t ~= "" then
      for _, prefix in ipairs({t, "алиса " .. t}) do
        if spoken == prefix then
          return ""
        elseif spoken:sub(1, #prefix + 1) == prefix .. " " then
          return spoken:sub(#prefix + 2)
        end
      end
    end
  end
  return nil
end

local function json_escape(s)
  return tostring(s):gsub('[\\"%c]', { ['\\\\'] = '\\\\\\\\', ['\"'] = '\\\\\"' })
end

local function handle(transcript, query)
  if BRIDGE_URL == "" then
    print("[station-voice] мост не настроен в панели")
    return
  end
  print("[station-voice] запрос: " .. query)
  local body = '{"transcript":"' .. json_escape(query) .. '"}'
  local url = BRIDGE_URL .. (BRIDGE_URL:find("?", 1, true) and "&" or "?") .. "token=" .. BRIDGE_TOKEN
  local ok, response = pcall(alice.http.post, url, body)
  if not ok or type(response) ~= "table" then
    print("[station-voice] мост недоступен")
    return
  end
  print("[station-voice] ответ " .. tostring(response.status) .. ": " .. tostring(response.body):sub(1, 300))
  -- звуковые ответы появятся позже (нужен TTS-файл в пакете)
end

local function handle_media(cmd)
  if BRIDGE_URL == "" then
    print("[station-voice] мост не настроен в панели")
    return
  end
  local media_url = BRIDGE_URL:gsub("/api/voice$", "/api/media")
  local url = media_url .. (media_url:find("?", 1, true) and "&" or "?") .. "token=" .. BRIDGE_TOKEN
  local ok, response = pcall(alice.http.post, url, '{"media":"' .. cmd .. '"}')
  if not ok or type(response) ~= "table" then
    print("[station-voice] мост недоступен (media)")
    return
  end
  print("[station-voice] media " .. cmd .. " -> " .. tostring(response.status) .. ": " .. tostring(response.body):sub(1, 200))
end

local function match_media(transcript)
  local spoken = normalize(transcript)
  if spoken == "" then return nil end
  return MEDIA[spoken]
end

-- «Алиса, включи …» -> перехват: играет soundya, голос отвечает «включаю …»
local function match_play(transcript)
  local spoken = normalize(transcript)
  local m = spoken:match("^алиса включи (.+)$")
      or spoken:match("^алиса поставь (.+)$")
      or spoken:match("^алиса запусти (.+)$")
      or spoken:match("^включи (.+)$")
      or spoken:match("^поставь (.+)$")
  if m and #m > 1 then return m end
  return nil
end

local function handle_play(query)
  if BRIDGE_URL == "" then
    print("[station-voice] мост не настроен в панели")
    return
  end
  local media_url = BRIDGE_URL:gsub("/api/voice$", "/api/media")
  local url = media_url .. (media_url:find("?", 1, true) and "&" or "?") .. "token=" .. BRIDGE_TOKEN
  local body = '{"play":"' .. json_escape(query) .. '"}'
  local ok, response = pcall(alice.http.post, url, body)
  if not ok or type(response) ~= "table" then
    print("[station-voice] мост недоступен (play)")
    return
  end
  print("[station-voice] play '" .. query .. "' -> " .. tostring(response.status) .. ": " .. tostring(response.body):sub(1, 200))
end

print("[station-voice] слушаю: " .. table.concat(TRIGGERS, ", "))
while true do
  local ok, event = pcall(alice.ipc.next_speech, 60000)
  if ok and type(event) == "table" and type(event.transcript) == "string" then
    local media = match_media(event.transcript)
    if media then
      pcall(alice.command.cancel)
      pcall(handle_media, media)
    else
      local play_query = match_play(event.transcript)
      if play_query then
        pcall(alice.command.cancel)
        pcall(handle_play, play_query)
      else
        local query = match_trigger(event.transcript)
        if query then
          pcall(alice.command.cancel)
          pcall(handle, event.transcript, query == "" and "привет" or query)
        end
      end
    end
  end
  alice.sleep(50)
end
"""


def build_voice_plugin_yap(phrases: list[str], bridge_url: str, bridge_token: str) -> bytes:
    """Собрать .yap (ZIP) с plugin.toml и main.lua."""
    import io
    import zipfile

    host = ""
    if bridge_url:
        # локальный мост по LAN работает по http, удалённый (VPS) — по https
        match = re.match(r"^https?://([^/]+)", bridge_url)
        if not match:
            raise HTTPException(status_code=400, detail="адрес моста должен начинаться с http:// или https://")
        host = match.group(1)

    phrase_list = ", ".join('"%s"' % p.replace('"', "") for p in phrases)
    permissions = ["ipc.speech", "commands", "audio"]
    if host:
        permissions.append("network")
    toml = (
        f'id = "{VOICE_PLUGIN_ID}"\n'
        'name = "Station Voice"\n'
        'version = "1.2.0"\n'
        'entry = "main.lua"\n'
        'description = "Голосовые команды панели Станции"\n'
        "\n"
        f'permissions = [{", ".join(chr(34) + p + chr(34) for p in permissions)}]\n'
        "\n"
        "[launch]\n"
        'kind = "lua"\n'
        'path = "main.lua"\n'
        "\n"
        "[commands]\n"
        f"phrases = [{phrase_list}]\n"
        "\n"
    )
    if host:
        toml += "[network]\n" f'allow = ["{host}"]\n' "\n"
    toml += '[compatibility]\nfeatures = ["runtime.lua.v1", "plugin.yap.v1"]\n'

    lua = VOICE_LUA_TEMPLATE
    lua = lua.replace("__PHRASES__", phrase_list)
    lua = lua.replace("__BRIDGE_URL__", json.dumps(bridge_url))
    lua = lua.replace("__TOKEN__", json.dumps(bridge_token))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.toml", toml)
        archive.writestr("main.lua", lua)
    return buffer.getvalue()


async def yam_api(method: str, path: str, data: Any = None, content_type: str | None = None) -> tuple[int, Any]:
    url = f"http://{cfg['station_ip']}:{cfg['yam_port']}{path}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, data=data, headers={"content-type": content_type} if content_type else None) as response:
            raw = await response.read()
            try:
                return response.status, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return response.status, raw.decode("utf-8", "replace")[:400]


@app.get("/api/voice/config")
async def api_voice_config(request: Request) -> JSONResponse:
    require_auth(request)
    return JSONResponse({
        "phrases": cfg["voice_phrases"],
        "bridge_url": cfg["voice_bridge_url"],
        "has_token": bool(cfg["voice_bridge_token"]),
        "music_source": cfg.get("voice_music_source", "youtube"),
    })


@app.put("/api/voice/config")
async def api_voice_config_put(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    phrases = data.get("phrases")
    if not isinstance(phrases, list) or not phrases:
        raise HTTPException(status_code=400, detail="нужна хотя бы одна фраза")
    clean = []
    for phrase in phrases:
        phrase = str(phrase).strip()
        if not phrase:
            continue
        if len(phrase.encode("utf-8")) > 128 or len(clean) >= 32:
            raise HTTPException(status_code=400, detail="фразы: до 32 штук, каждая до 128 байт")
        clean.append(phrase)
    if not clean:
        raise HTTPException(status_code=400, detail="нужна хотя бы одна непустая фраза")
    cfg["voice_phrases"] = clean
    if "bridge_url" in data:
        url = str(data["bridge_url"]).strip()
        if url and not re.match(r"^https?://", url):
            raise HTTPException(status_code=400, detail="адрес моста должен начинаться с http:// или https://")
        cfg["voice_bridge_url"] = url
    if "bridge_token" in data:
        cfg["voice_bridge_token"] = str(data["bridge_token"]).strip()
    if "music_source" in data:
        source = str(data["music_source"]).lower()
        if source not in ("youtube", "soundcloud", "spotify"):
            raise HTTPException(status_code=400, detail="источник: youtube | soundcloud | spotify")
        cfg["voice_music_source"] = source
    save_config(cfg)
    return JSONResponse({"ok": True})


@app.post("/api/voice/bridge-config")
async def api_voice_bridge_config(request: Request) -> JSONResponse:
    """Отправить выбранный источник музыки в мост (его ручка /api/bridge/config)."""
    require_auth(request)
    url, token = cfg["voice_bridge_url"].strip(), cfg["voice_bridge_token"].strip()
    if not url or not token:
        raise HTTPException(status_code=400, detail="сначала задай адрес и токен моста")
    base = url.split("/api/voice")[0].rstrip("/")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.put(
            f"{base}/api/bridge/config",
            json={"source": cfg.get("voice_music_source", "youtube")},
            headers={"authorization": f"Bearer {token}"},
        ) as response:
            body = await response.text()
    if response.status != 200:
        return JSONResponse({"detail": f"мост вернул {response.status}: {body[:200]}"}, status_code=502)
    return JSONResponse({"ok": True})


@app.api_route("/bridge/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def bridge_proxy(path: str, request: Request) -> Response:
    """Проксировать локальный мост (:8811) — удобно для UI и тестов из браузера."""
    require_auth(request)
    url = f"http://127.0.0.1:{BRIDGE_PORT}/{path}"
    query = request.url.query
    if "token=" not in query and cfg.get("voice_bridge_token"):
        query = (query + "&" if query else "") + "token=" + __import__("urllib.parse", fromlist=["quote"]).quote(str(cfg["voice_bridge_token"]))
    if query:
        url += "?" + query
    body = await request.body()
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(request.method, url, data=body or None,
                                       headers={"Content-Type": request.headers.get("content-type", "application/json")}) as resp:
                payload = await resp.read()
                return Response(content=payload, status_code=resp.status,
                                media_type=resp.headers.get("content-type", "application/json"))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"мост недоступен: {exc}")


@app.post("/api/voice/answer-tts")
async def api_voice_answer_tts(request: Request) -> JSONResponse:
    """Вкл/выкл озвучку ответов моста на колонке (флаг-файл, мост читает на лету)."""
    require_auth(request)
    data = await request.json()
    enabled = bool(data.get("enabled"))
    cfg["bridge_answer_tts"] = enabled
    save_config(cfg)
    flag = BASE / "answer-tts.flag"
    if enabled:
        flag.write_text("on", encoding="utf-8")
    else:
        flag.unlink(missing_ok=True)
    return JSONResponse({"ok": True, "enabled": enabled})


@app.post("/api/voice/deploy")
async def api_voice_deploy(request: Request) -> JSONResponse:
    """Собрать .yap с текущими фразами и установить на колонку."""
    require_auth(request)
    yap = build_voice_plugin_yap(cfg["voice_phrases"], cfg["voice_bridge_url"], cfg["voice_bridge_token"])
    try:
        # multipart с полем package, как ждёт POST /api/plugins в yamd
        import io as _io

        boundary = "----stationpanel" + secrets.token_hex(8)
        part = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="package"; filename="station-voice.yap"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + yap + f"\r\n--{boundary}--\r\n".encode("utf-8")
        status, body = await yam_api(
            "POST", "/api/plugins", data=part,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        if status not in (200, 201):
            return JSONResponse({"detail": f"YAM вернул {status}: {body}"}, status_code=502)

        # включить пакет, автозапуск и СТАРТ (остановка в конце оставляла плагин мёртвым)
        await yam_api("POST", f"/api/plugins/{VOICE_PLUGIN_ID}/toggle")
        status, body = await yam_api("POST", f"/api/plugins/{VOICE_PLUGIN_ID}/service/enable")
        if status not in (200, 202, 204):
            return JSONResponse({"detail": f"автозапуск: YAM вернул {status}: {body}"}, status_code=502)
        await yam_api("POST", f"/api/plugins/{VOICE_PLUGIN_ID}/start")
        return JSONResponse({"ok": True, "manifest": body})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"не удалось установить плагин: {exc}")


@app.get("/api/voice/status")
async def api_voice_status(request: Request) -> JSONResponse:
    require_auth(request)
    status, plugins = await yam_api("GET", "/api/plugins")
    if status != 200 or not isinstance(plugins, list):
        return JSONResponse({"installed": False, "detail": "демон недоступен"})
    for plugin in plugins:
        if (plugin.get("manifest") or {}).get("id") == VOICE_PLUGIN_ID:
            return JSONResponse({
                "installed": True,
                "enabled": bool(plugin.get("enabled")),
                "service": bool(plugin.get("service_enabled")),
                "running": bool(plugin.get("running")),
                "phrases": (plugin.get("manifest") or {}).get("commands", {}).get("phrases", []),
            })
    return JSONResponse({"installed": False})


# --------------------------------------------------------------------------- #
# Локальный голосовой мост (voice-bridge) — запускается панелью как подпроцесс
# --------------------------------------------------------------------------- #
BRIDGE_PORT = 8811
_bridge_proc: asyncio.subprocess.Process | None = None


def _detect_pc_ip() -> str | None:
    """IP этого ПК в подсети станции — чтобы колонка достучалась до моста.

    Открываем UDP-сокет к адресу станции (без реальной отправки) и смотрим,
    с какого локального адреса ушёл пакет. Это самый надёжный способ узнать
    свой IP в нужной подсети, когда ПК в нескольких сетях.
    """
    import socket

    station = str(cfg.get("station_ip", "")).strip()
    if not station:
        return None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((station, 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:
        return None


def _bridge_dir() -> Path:
    """Папка с кодом voice-bridge: настройка из конфига или ../voice-bridge рядом."""
    custom = str(cfg.get("bridge_path", "")).strip()
    if custom:
        return Path(custom)
    return BASE.parent / "voice-bridge"


def _bridge_env() -> dict[str, str]:
    """Переменные окружения для моста из настроек панели."""
    import os as _os

    env = dict(_os.environ)
    env["VOICE_TOKEN"] = cfg["voice_bridge_token"] or secrets.token_urlsafe(24)
    env["LLM_PROVIDER"] = str(cfg.get("llm_provider", "deepseek-web"))
    env["LLM_KEY"] = str(cfg.get("llm_key", ""))
    env["LLM_MODEL"] = str(cfg.get("llm_model", ""))
    env["DEEPSEEK_TOKEN"] = str(cfg.get("deepseek_token", ""))
    env["DEEPSEEK_COOKIE"] = str(cfg.get("deepseek_cookie", ""))
    env["VOICE_PROXY"] = str(cfg.get("voice_proxy", ""))
    env["VOICE_STATE"] = str(BASE / "bridge-state.json")
    env["BRIDGE_CONFIG"] = str(BASE / "bridge-config.json")
    env["YSE_SSH_HOST"] = str(cfg.get("station_ip", ""))
    env["YSE_SSH_PORT"] = str(cfg.get("ssh_port", "2222"))
    env["YSE_SSH_USER"] = str(cfg.get("ssh_user", "root"))
    if KEY_PRIV.exists():
        env["YSE_SSH_KEY"] = str(KEY_PRIV)
    # озвучка ответов моста: движок как в «Сказать на колонке»
    env["YSE_TTS_ENGINE"] = str(cfg.get("say_engine", "edge"))
    env["YSE_ELEVEN_KEY"] = str(cfg.get("eleven_api_key", ""))
    env["YSE_ELEVEN_PROXY"] = str(cfg.get("eleven_proxy", ""))
    env["YSE_ELEVEN_VOICE"] = str(cfg.get("eleven_voice_id", ""))
    env["YSE_ELEVEN_MODEL"] = str(cfg.get("eleven_model", "eleven_multilingual_v2"))
    env["YSE_ALICE_X_TOKEN"] = str(cfg.get("yandex_x_token", ""))
    env["YSE_FFMPEG"] = bin_tool("ffmpeg") or ""
    env["YSE_TTS_FLAG"] = str(BASE / "answer-tts.flag")
    return env


async def _bridge_health(timeout: float = 2.0) -> bool:
    """Жив ли локальный мост: быстрый GET /health."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(f"http://127.0.0.1:{BRIDGE_PORT}/health") as resp:
                return resp.status == 200
    except Exception:
        return False


async def _bridge_start() -> bool:
    """Запустить мост как подпроцесс, если ещё не работает."""
    global _bridge_proc
    if await _bridge_health():
        return True  # уже поднят (например, из прошлого раза)
    bdir = _bridge_dir()
    if not (bdir / "app.py").exists():
        return False
    _bridge_proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", "app:app",
        "--host", str(cfg.get("bridge_host", "0.0.0.0")), "--port", str(BRIDGE_PORT), "--log-level", "warning",
        cwd=str(bdir), env=_bridge_env(),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    # ждём, пока поднимется (до ~8 с)
    for _ in range(16):
        await asyncio.sleep(0.5)
        if await _bridge_health():
            return True
    return False


async def _bridge_stop() -> None:
    """Остановить подпроцесс моста, если он запущен нами."""
    global _bridge_proc
    if _bridge_proc and _bridge_proc.returncode is None:
        try:
            _bridge_proc.terminate()
            await asyncio.wait_for(_bridge_proc.wait(), timeout=5)
        except Exception:
            try:
                _bridge_proc.kill()
            except Exception:
                pass
    _bridge_proc = None


def _bridge_llm_ready() -> dict[str, Any]:
    """Что настроено из LLM (без вывода секретов)."""
    provider = str(cfg.get("llm_provider", "deepseek-web"))
    return {
        "provider": provider,
        "deepseek_token": bool(cfg.get("deepseek_token")),
        "deepseek_cookie": bool(cfg.get("deepseek_cookie")),
        "openrouter_key": bool(cfg.get("llm_key")),
        "model": cfg.get("llm_model", ""),
    }


@app.get("/api/bridge/status")
async def api_bridge_status(request: Request) -> JSONResponse:
    """Состояние локального моста: код найден, запущен (поднимаем при необходимости), /health, LLM."""
    require_auth(request)
    bdir = _bridge_dir()
    running = await _bridge_health()
    if not running and (bdir / "app.py").exists():
        running = await _bridge_start()
    return JSONResponse({
        "dir": str(bdir),
        "code_found": (bdir / "app.py").exists(),
        "running": running,
        "port": BRIDGE_PORT,
        "url": cfg.get("voice_bridge_url", ""),
        "has_token": bool(cfg.get("voice_bridge_token")),
        "llm": _bridge_llm_ready(),
    })


@app.post("/api/bridge/setup")
async def api_bridge_setup(request: Request) -> JSONResponse:
    """Автонастройка локального моста: токен, URL по IP ПК, запуск, проверка."""
    require_auth(request)
    steps: list[dict[str, Any]] = []

    # 1. код моста на месте?
    bdir = _bridge_dir()
    code_found = (bdir / "app.py").exists()
    steps.append({"name": "код voice-bridge", "ok": code_found,
                  "detail": str(bdir) if code_found else "не найден — укажи путь вручную"})
    if not code_found:
        return JSONResponse({"ok": False, "steps": steps})

    # 2. токен: генерируем и сохраняем, если пуст
    if not cfg.get("voice_bridge_token"):
        cfg["voice_bridge_token"] = secrets.token_urlsafe(24)
        save_config(cfg)
    steps.append({"name": "токен моста", "ok": True, "detail": "создан и сохранён"})

    # 3. URL моста по IP ПК в подсети станции (плагин на колонке должен достучаться)
    pc_ip = _detect_pc_ip()
    if pc_ip:
        cfg["voice_bridge_url"] = f"http://{pc_ip}:{BRIDGE_PORT}/api/voice"
        save_config(cfg)
        steps.append({"name": "адрес моста", "ok": True, "detail": cfg["voice_bridge_url"]})
    else:
        steps.append({"name": "адрес моста", "ok": False,
                      "detail": "не удалось определить IP ПК — задай вручную"})

    # 4. запуск подпроцесса и проверка /health
    started = await _bridge_start()
    steps.append({"name": "запуск моста", "ok": started,
                  "detail": f"http://127.0.0.1:{BRIDGE_PORT}" if started else "не поднялся — смотри зависимости (pip install -r voice-bridge/requirements.txt)"})

    # 5. при желании сразу отправить выбранный источник музыки в мост
    if started:
        try:
            status, body = await _post_bridge_config()
            steps.append({"name": "источник музыки", "ok": 200 <= status < 300,
                          "detail": cfg.get("voice_music_source", "youtube")})
        except Exception as exc:
            steps.append({"name": "источник музыки", "ok": False, "detail": str(exc)[:120]})

    ok = all(s["ok"] for s in steps if s["name"] != "источник музыки")
    return JSONResponse({"ok": ok, "steps": steps, "llm": _bridge_llm_ready()})


async def _post_bridge_config() -> tuple[int, str]:
    """Отправить источник музыки в локальный мост напрямую (минуя внешний URL)."""
    url = f"http://127.0.0.1:{BRIDGE_PORT}/api/bridge/config"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.put(
            url,
            json={"source": cfg.get("voice_music_source", "youtube")},
            headers={"X-Voice-Token": cfg.get("voice_bridge_token", "")},
        ) as resp:
            return resp.status, await resp.text()


@app.post("/api/bridge/stop")
async def api_bridge_stop(request: Request) -> JSONResponse:
    """Остановить локальный мост."""
    require_auth(request)
    await _bridge_stop()
    return JSONResponse({"ok": True, "running": await _bridge_health()})


@app.put("/api/bridge/llm")
async def api_bridge_llm(request: Request) -> JSONResponse:
    """Сохранить настройки LLM для моста. Секреты не возвращаются."""
    require_auth(request)
    data = await request.json()
    mapping = {
        "provider": "llm_provider", "deepseek_token": "deepseek_token",
        "deepseek_cookie": "deepseek_cookie", "llm_key": "llm_key",
        "model": "llm_model", "proxy": "voice_proxy", "path": "bridge_path",
    }
    for key, field in mapping.items():
        if key in data:
            cfg[field] = str(data[key]).strip()
    save_config(cfg)
    # перезапуск, чтобы мост подхватил новые переменные окружения
    was_running = await _bridge_health()
    await _bridge_stop()
    if was_running:
        await _bridge_start()
    return JSONResponse({"ok": True, "llm": _bridge_llm_ready(), "running": await _bridge_health()})


# --------------------------------------------------------------------------- #
# «Сказать на колонке»: синтез речи и воспроизведение через aplay
# --------------------------------------------------------------------------- #
SAY_MOODS: dict[str, str | None] = {
    # ffmpeg-фильтры поверх обычного синтеза; имена — как у TTS Алисы (TTtsGenerate.Emotion)
    "normal": None,
    "joy": "asetrate=16000*1.07,aresample=16000,atempo=1.05",       # EmotionalJoy
    "smile": "asetrate=16000*1.03,aresample=16000,atempo=1.02",     # EmotionalSmile
    "sadness": "asetrate=16000*0.92,aresample=16000,atempo=0.92",   # EmotionalSadness
    "angry": "asetrate=16000*0.97,aresample=16000,atempo=1.06,volume=3dB",  # EmotionalAngry
    "wow": "asetrate=16000*1.12,aresample=16000,atempo=1.08",       # EmotionalWow
    "whisper": "volume=-26dB,highpass=f=180,lowpass=f=3400",        # is_whisper
}
SAY_VOICES = {"svetlana": "ru-RU-SvetlanaNeural", "dmitry": "ru-RU-DmitryNeural"}
SAY_REMOTE = "/tmp/station-panel-say.wav"
# Сценарий облачного TTS Алисы: один на панель, текст меняется перед каждым запуском.
ALICE_SCENARIO_NAME = "Панель Станции — голос"
ELEVEN_MODELS = [
    ("eleven_multilingual_v2", "Multilingual v2 — лучшее качество (русский есть)"),
    ("eleven_turbo_v2_5", "Turbo v2.5 — быстрее и дешевле"),
    ("eleven_flash_v2_5", "Flash v2.5 — самый быстрый"),
]


async def _alice_say(text: str) -> None:
    """Произнести текст родным голосом Алисы на колонке.

    Через облачную сессию Яндекса (yandex_x_token) находим/создаём сценарий
    с capability «tts», меняем в нём текст и запускаем — колонка произносит
    текст тем же голосом, что и свои ответы. Протокол актуальный (v4),
    как в интеграции YandexStation.
    """
    # 1. устройства: колонка лежит в household.all
    devices = await cloud_request("GET", "https://iot.quasar.yandex.ru/m/v3/user/devices")
    speaker = None
    for h in devices.get("households", []):
        for d in h.get("all", []):
            if str(d.get("type", "")).startswith("devices.types.media_device") or \
               str(d.get("type", "")).startswith("devices.types.smart_speaker"):
                speaker = d
                break
        if speaker:
            break
    if speaker is None:
        raise HTTPException(status_code=404, detail="колонка не найдена в аккаунте Яндекса")
    speaker_id = speaker["id"]

    # 2. сценарии — ищем наш
    scenarios = await cloud_request("GET", "https://iot.quasar.yandex.ru/m/user/scenarios")
    scenario_id = None
    for s in scenarios.get("scenarios", []):
        if s.get("name") == ALICE_SCENARIO_NAME:
            scenario_id = s["id"]
            break

    scenario_payload = {
        "name": ALICE_SCENARIO_NAME,
        "icon": "home",
        "triggers": [{"trigger": {"type": "scenario.trigger.voice", "value": ALICE_SCENARIO_NAME}}],
        "steps": [{
            "type": "scenarios.steps.actions.v2",
            "parameters": {
                "items": [{
                    "id": speaker_id,
                    "type": "step.action.item.device",
                    "value": {
                        "id": speaker_id,
                        "item_type": "device",
                        "capabilities": [{
                            "type": "devices.capabilities.quasar",
                            "state": {"instance": "tts", "value": {"text": text}},
                        }],
                    },
                }],
            },
        }],
    }
    if scenario_id:
        # 3а. обновляем текст в существующем сценарии
        await cloud_request("PUT", f"https://iot.quasar.yandex.ru/m/v4/user/scenarios/{scenario_id}", scenario_payload)
    else:
        # 3б. создаём сценарий
        created = await cloud_request("POST", "https://iot.quasar.yandex.ru/m/v4/user/scenarios", scenario_payload)
        scenario_id = created.get("scenario_id")
        if not scenario_id:
            raise HTTPException(status_code=502, detail="Яндекс не создал сценарий: " + json.dumps(created, ensure_ascii=False)[:140])

    # 4. запускаем сценарий — колонка произносит текст голосом Алисы
    await cloud_request("POST", f"https://iot.quasar.yandex.ru/m/user/scenarios/{scenario_id}/actions", {})



async def _eleven_tts(text: str, voice_id: str, model: str) -> bytes:
    """Синтез через ElevenLabs API (mp3 44.1 кГц). Ключ и прокси — в config.json.

    API ElevenLabs закрыт геоблокировкой из РФ — запросы идут через прокси
    (тот же steelproxy, что у плеера «на ПК»), если он задан."""
    key = str(cfg.get("eleven_api_key") or "")
    if not key:
        raise HTTPException(status_code=400, detail="ключ ElevenLabs не задан — впиши его в настройках «Сказать на колонке»")
    if not voice_id:
        raise HTTPException(status_code=400, detail="не выбран голос ElevenLabs")
    proxy = str(cfg.get("eleven_proxy") or "").strip() or None
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": key, "Content-Type": "application/json",
                         "Accept": "audio/mpeg"},
                json={"text": text, "model_id": model or "eleven_multilingual_v2"},
                proxy=proxy,
        ) as resp:
            data = await resp.read()
            if resp.status != 200:
                detail = data.decode("utf-8", "replace")[:200]
                raise HTTPException(status_code=502, detail=f"ElevenLabs HTTP {resp.status}: {detail}")
            return data


@app.get("/api/say/config")
async def api_say_config_get(request: Request) -> JSONResponse:
    require_auth(request)
    return JSONResponse({
        "ok": True,
        "engine": cfg.get("say_engine", "edge"),
        "has_key": bool(cfg.get("eleven_api_key")),
        "voice_id": cfg.get("eleven_voice_id", ""),
        "model": cfg.get("eleven_model", "eleven_multilingual_v2"),
        "proxy": cfg.get("eleven_proxy", "http://4ys1MYfr:IAWl3Vmz@isp01.steelproxy.com:3071"),
        "models": [{"id": i, "name": n} for i, n in ELEVEN_MODELS],
    })


@app.put("/api/say/config")
async def api_say_config_put(request: Request) -> JSONResponse:
    """Настройки синтеза речи: движок, ключ и голос ElevenLabs."""
    require_auth(request)
    data = await request.json()
    engine = str(data.get("engine") or "edge")
    if engine not in ("edge", "elevenlabs", "alice"):
        raise HTTPException(status_code=400, detail="движок: edge, elevenlabs или alice")
    cfg["say_engine"] = engine
    if "eleven_api_key" in data and str(data["eleven_api_key"]).strip():
        cfg["eleven_api_key"] = str(data["eleven_api_key"]).strip()
    if "eleven_voice_id" in data:
        cfg["eleven_voice_id"] = str(data["eleven_voice_id"]).strip()
    if "eleven_model" in data:
        model = str(data["eleven_model"]).strip()
        if model and not any(model == i for i, _ in ELEVEN_MODELS):
            raise HTTPException(status_code=400, detail="неизвестная модель ElevenLabs")
        cfg["eleven_model"] = model or "eleven_multilingual_v2"
    if "eleven_proxy" in data:
        cfg["eleven_proxy"] = str(data["eleven_proxy"]).strip()
    save_config(cfg)
    return JSONResponse({"ok": True, "engine": cfg["say_engine"], "has_key": bool(cfg.get("eleven_api_key"))})


@app.get("/api/say/voices")
async def api_say_voices(request: Request) -> JSONResponse:
    """Список голосов выбранного движка."""
    require_auth(request)
    engine = str(request.query_params.get("engine") or cfg.get("say_engine", "edge"))
    if engine == "alice":
        return JSONResponse({"ok": True, "voices": [{"id": "alice", "name": "Алиса (родной голос)"}]})
    if engine == "edge":
        return JSONResponse({"ok": True, "voices": [{"id": k, "name": v.split("-")[-1].replace("Neural", " (Эдж)")} for k, v in SAY_VOICES.items()]})
    key = str(cfg.get("eleven_api_key") or "")
    if not key:
        return JSONResponse({"ok": False, "voices": [], "detail": "сначала впиши ключ ElevenLabs"})
    proxy = str(cfg.get("eleven_proxy") or "").strip() or None
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get("https://api.elevenlabs.io/v1/voices",
                               headers={"xi-api-key": key}, proxy=proxy) as resp:
            data = await resp.json(content_type=None)
    if resp.status != 200:
        detail = (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))[:120] if data else f"HTTP {resp.status}"
        hint = " — похоже на геоблокировку: задай прокси в настройках голоса" if resp.status in (301, 302) else ""
        return JSONResponse({"ok": False, "voices": [], "detail": f"ElevenLabs HTTP {resp.status}: {detail}{hint}"})
    voices = [{"id": v.get("voice_id"), "name": v.get("name")} for v in data.get("voices", [])]
    return JSONResponse({"ok": True, "voices": voices})


@app.get("/api/say/quota")
async def api_say_quota(request: Request) -> JSONResponse:
    """Остаток символов ElevenLabs (тарифный лимит месяца)."""
    require_auth(request)
    key = str(cfg.get("eleven_api_key") or "")
    if not key:
        return JSONResponse({"ok": False, "detail": "ключ не задан"})
    proxy = str(cfg.get("eleven_proxy") or "").strip() or None
    timeout = aiohttp.ClientTimeout(total=40)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.elevenlabs.io/v1/user/subscription",
                                   headers={"xi-api-key": key}, proxy=proxy) as resp:
                data = await resp.json(content_type=None)
        if resp.status != 200:
            return JSONResponse({"ok": False, "detail": f"HTTP {resp.status}"})
        return JSONResponse({
            "ok": True,
            "used": data.get("character_count"),
            "limit": data.get("character_limit"),
            "next_reset": data.get("next_character_count_reset_unix") or data.get("next_character_count_reset_unix_ms"),
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "detail": str(exc)[:120]})


# --- нативная озвучка через локальный IPC (без куки и токенов) ---
def _ipc_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _ipc_integer(field: int, value: int) -> bytes:
    return _ipc_varint(field * 8) + _ipc_varint(value)


def _ipc_byte_field(field: int, value: bytes) -> bytes:
    return _ipc_varint(field * 8 + 2) + _ipc_varint(len(value)) + value


def _ipc_parse(data: bytes) -> dict[int, Any]:
    offset, result = 0, {}

    def number() -> int:
        nonlocal offset
        value, shift = 0, 0
        while shift < 70:
            if offset >= len(data):
                raise ValueError("обрезанный protobuf")
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
        raise ValueError("неверный protobuf-varint")

    while offset < len(data):
        key = number()
        field, wire = key >> 3, key & 7
        if wire == 0:
            result[field] = number()
        elif wire == 2:
            size = number()
            result[field] = data[offset:offset + size]
            offset += size
        elif wire == 1:
            result[field] = data[offset:offset + 8]
            offset += 8
        elif wire == 5:
            result[field] = data[offset:offset + 4]
            offset += 4
        else:
            raise ValueError(f"неизвестный protobuf-wire {wire}")
    return result


def _ipc_frame(field: int, payload: bytes) -> bytes:
    remote = _ipc_byte_field(7, b"AliceCapability") + _ipc_byte_field(field, payload)
    envelope = _ipc_byte_field(252, remote) + _ipc_integer(253, 210)
    return len(envelope).to_bytes(4, "little") + envelope


def _ipc_vocalize_frame(text: str) -> bytes:
    # START_VOCALIZE=18, events=true, TVocalizeDirective(text, Append=1)
    directive = _ipc_byte_field(1, text.encode("utf-8")) + _ipc_integer(2, 1)
    return _ipc_frame(8, _ipc_integer(1, 18) + _ipc_integer(3, 1) + _ipc_byte_field(13, directive))


async def _ipc_say(text: str, timeout: float = 30.0) -> None:
    """Нативная озвучка голосом Алисы через локальный IPC колонки (127.0.0.1:9875,
    AliceCapability START_VOCALIZE). Куки и токены Яндекса не нужны — работает
    авторизация самой колонки. Формат кадров повторяет contrib/alice-say.py из вики
    hackalice.me; на Мини 2 события приходят с эхом rpc-id 210 (253=210), поэтому
    принимаются оба значения. Озвучка идёт через SSH-порт-форвард — socat не нужен."""
    conn = await ssh_connect()
    try:
        reader, writer = await conn.open_connection("127.0.0.1", 9875)
        writer.write(_ipc_frame(20, _ipc_byte_field(1, b"AliceCapability")) + _ipc_vocalize_frame(text))
        started = False
        try:
            while True:
                size = int.from_bytes(await asyncio.wait_for(reader.readexactly(4), timeout), "little")
                if not 0 < size <= 512 * 1024:
                    raise HTTPException(status_code=502, detail="IPC: битый размер кадра")
                envelope = _ipc_parse(await asyncio.wait_for(reader.readexactly(size), timeout))
                if envelope.get(253) not in (3, 210):
                    continue
                remote = _ipc_parse(envelope.get(252, b""))
                if 18 not in remote:
                    continue
                event = _ipc_parse(remote[18]).get(1)
                if event == 6:
                    started = True
                elif event == 1 and started:
                    return
                elif event == 4:
                    raise HTTPException(status_code=502, detail="колонка сообщила об ошибке озвучки")
        except asyncio.TimeoutError:
            if started:
                return  # озвучка идёт дольше лимита — не считаем ошибкой
            raise HTTPException(status_code=502, detail="IPC: колонка не подтвердила озвучку")
        except asyncssh.Error as exc:
            raise HTTPException(status_code=502, detail=f"IPC-канал: {str(exc)[:120]}")
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=f"IPC: {exc}")
        finally:
            writer.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"IPC-озвучка не удалась: {str(exc)[:140]}")
    finally:
        conn.close()


@app.post("/api/speaker/say")
async def api_speaker_say(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    text = str(data.get("text", "")).strip()
    engine = str(data.get("engine") or cfg.get("say_engine", "edge"))
    if engine not in ("edge", "elevenlabs", "alice", "ipc"):
        engine = "edge"
    limit = 2000 if engine == "ipc" else 500
    if not text or len(text) > limit:
        raise HTTPException(status_code=400, detail=f"текст от 1 до {limit} символов")

    import tempfile

    # Нативная озвучка через локальный IPC: без кук и токенов, голос Алисы.
    if engine == "ipc":
        await _ipc_say(text)
        return JSONResponse({"ok": True, "voice": "alice", "mood": "normal", "engine": "ipc"})

    # Родной голос Алисы: текст уходит в облако, колонка произносит сама.
    # Интонации (mood) недоступны — Яндекс не даёт менять темп/высоту фразы.
    if engine == "alice":
        if not cfg.get("yandex_x_token"):
            raise HTTPException(status_code=400, detail="нет входа в Яндекс: вкладка «Алиса» → облачные тумблеры → войти")
        await _alice_say(text)
        return JSONResponse({"ok": True, "voice": "alice", "mood": "normal", "engine": "alice"})

    if engine == "edge":
        voice = SAY_VOICES.get(str(data.get("voice", "svetlana")))
        if not voice:
            raise HTTPException(status_code=400, detail="неизвестный голос Эджа")
    else:
        voice = str(data.get("voice") or cfg.get("eleven_voice_id") or "")
    mood = str(data.get("mood", "normal"))
    if mood not in SAY_MOODS:
        raise HTTPException(status_code=400, detail="неизвестная интонация")

    tmp_dir = Path(tempfile.gettempdir())
    mp3_path = tmp_dir / f"station-say-{secrets.token_hex(4)}.mp3"
    wav_path = tmp_dir / f"station-say-{secrets.token_hex(4)}.wav"
    try:
        try:
            if engine == "elevenlabs":
                mp3_path.write_bytes(await _eleven_tts(
                    text, voice or str(cfg.get("eleven_voice_id") or ""),
                    str(cfg.get("eleven_model") or "eleven_multilingual_v2")))
            else:
                import edge_tts

                communicate = edge_tts.Communicate(text, voice)
                await communicate.save(str(mp3_path))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"синтез речи не удался: {exc}")

        ffmpeg = bin_tool("ffmpeg")
        if not ffmpeg:
            raise HTTPException(status_code=500, detail="ffmpeg не найден — установи во вкладке «Чек-лист»")
        command = [ffmpeg, "-y", "-loglevel", "error", "-i", str(mp3_path)]
        if SAY_MOODS[mood]:
            command += ["-af", SAY_MOODS[mood]]
        command += ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_path)]
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), 30)
        if proc.returncode != 0 or not wav_path.exists():
            raise HTTPException(status_code=500, detail="ffmpeg: " + err.decode("utf-8", "replace")[:200])

        content = wav_path.read_bytes()
        code, _, err = await ssh_run_bytes(f"cat > {shlex.quote(SAY_REMOTE)}", input_data=content, timeout=120)
        if code != 0:
            raise HTTPException(status_code=502, detail="не удалось залить аудио на колонку")
        # aplay блокируется до конца воспроизведения — ждём с запасом
        code, _, aplay_err = await ssh_run_bytes(f"aplay -q {shlex.quote(SAY_REMOTE)}", timeout=180)
        if code != 0:
            raise HTTPException(status_code=502, detail="aplay: " + aplay_err.decode("utf-8", "replace")[:200])
        return JSONResponse({"ok": True, "voice": voice, "mood": mood, "bytes": len(content)})
    finally:
        mp3_path.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Общая громкость колонки — регулятор ALSA «Master» (его же двигает Алиса
# через /system/vendor/quasar/set_volume.sh). Шкала 0..100 (%).
# Синхронизация с ПК: громкость ПК → колонка; изменение на колонке — в ползунок.
# --------------------------------------------------------------------------- #
volume_state: dict[str, Any] = {
    "col": None, "pc": None, "last_pc": None,
    "last_pushed_col": None, "last_reported_col": None,
}


async def _read_master_volume() -> int | None:
    """Текущая громкость Master в % (amixer -c 0 sget Master)."""
    code, out, _ = await ssh_run("amixer -c 0 sget Master 2>/dev/null")
    if code != 0:
        return None
    match = re.search(r"Front Left:\s*Playback\s+\d+\s+\[(\d+)%\]", out)
    return int(match.group(1)) if match else None


def _read_pc_volume() -> int | None:
    """Громкость динамиков Windows (0..100) через Core Audio (pycaw).

    Вызывается из фонового потока — COM нужно инициализировать в нём же."""
    try:
        import comtypes
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        from pycaw.pycaw import AudioUtilities
        return int(AudioUtilities.GetSpeakers().volume_percent)
    except Exception:
        return None


def _set_pc_volume(level: int) -> bool:
    try:
        import comtypes
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.GetSpeakers()
        device.EndpointVolume.SetMasterVolumeLevelScalar(level / 100.0, None)
        return True
    except Exception:
        return False


async def _volume_sync_loop() -> None:
    """Каждые 2 с читает громкость ПК; при изменении гонит её на колонку.
    Каждые 6 с читает громкость колонки в кэш (для ползунка панели)."""
    tick = 0
    while True:
        await asyncio.sleep(2)
        tick += 1
        try:
            pc = await asyncio.to_thread(_read_pc_volume)
            if pc is not None:
                volume_state["pc"] = pc
                sync_on = bool(cfg.get("volume_sync_enabled"))
                if (sync_on and volume_state["last_pc"] is not None
                        and pc != volume_state["last_pc"] and volume_state["col"] is not None):
                    code, _, err = await ssh_run(f"amixer -q set Master {pc}%")
                    if code == 0:
                        volume_state["col"] = pc
                        volume_state["last_pushed_col"] = pc
                        volume_state["last_reported_col"] = pc
                    else:
                        sys.stderr.write("volume sync: " + err[:120] + "\n")
            volume_state["last_pc"] = pc
        except Exception:
            pass
        if tick % 3:
            continue
        try:
            col = await _read_master_volume()
            if col is not None:
                volume_state["col"] = col
                pushed = volume_state["last_pushed_col"]
                if pushed is None or col != pushed:
                    volume_state["last_reported_col"] = col
        except Exception:
            pass
        if tick % 5:
            continue
        # сторож: упавший station-voice блокирует wake word («component attached but stopped»)
        try:
            st, plugins = await yam_api("GET", "/api/plugins")
            for p in (plugins if isinstance(plugins, list) else []):
                if (p.get("manifest") or {}).get("id") == "station-voice" and not p.get("running"):
                    await yam_api("POST", "/api/plugins/station-voice/start")
        except Exception:
            pass


async def _start_volume_loop() -> None:
    asyncio.create_task(_volume_sync_loop())


@app.get("/api/volume")
async def api_volume_get(request: Request) -> JSONResponse:
    require_auth(request)
    volume = volume_state["col"]
    if volume is None:
        volume = await _read_master_volume()
        volume_state["col"] = volume
    if volume is None:
        return JSONResponse({"ok": False, "detail": "не удалось прочитать громкость (amixer)"}, status_code=502)
    return JSONResponse({
        "ok": True, "volume": volume,
        "pc": volume_state["pc"],
        "sync": bool(cfg.get("volume_sync_enabled")),
    })


@app.post("/api/volume/sync")
async def api_volume_sync(request: Request) -> JSONResponse:
    """Включить/выключить синхронизацию громкости ПК → колонка."""
    require_auth(request)
    data = await request.json()
    cfg["volume_sync_enabled"] = bool(data.get("enabled"))
    save_config(cfg)
    return JSONResponse({"ok": True, "sync": bool(cfg.get("volume_sync_enabled"))})


@app.put("/api/volume")
async def api_volume_put(request: Request) -> JSONResponse:
    """Поставить громкость 0..100 (та же шкала, что у кнопок и голоса Алисы)."""
    require_auth(request)
    data = await request.json()
    try:
        level = max(0, min(100, int(data.get("volume"))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="громкость: целое 0..100")
    code, _, err = await ssh_run(f"amixer -q set Master {level}%")
    if code != 0:
        raise HTTPException(status_code=502, detail="amixer: " + err[:160])
    return JSONResponse({"ok": True, "volume": level})


@app.put("/api/volume/pc")
async def api_volume_pc_put(request: Request) -> JSONResponse:
    """Громкость динамиков ПК (0..100) — отдельно от колонки."""
    require_auth(request)
    data = await request.json()
    try:
        level = max(0, min(100, int(data.get("volume"))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="громкость: целое 0..100")
    if not _set_pc_volume(level):
        raise HTTPException(status_code=500, detail="не удалось выставить громкость ПК (pycaw)")
    volume_state["pc"] = level
    return JSONResponse({"ok": True, "pc": level})


# --------------------------------------------------------------------------- #
# Файловый менеджер: exec-команды по SSH (dropbear без SFTP-подсистемы)
# --------------------------------------------------------------------------- #
def safe_remote_path(value: Any) -> str:
    path = str(value or "").strip()
    if not path.startswith("/") or "\\" in path or ".." in PurePosixPath(path).parts:
        raise HTTPException(status_code=400, detail="нужен абсолютный путь без «..»")
    return path.rstrip("/") or "/"


def safe_remote_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", "..") or len(name) > 255:
        raise HTTPException(status_code=400, detail="недопустимое имя файла")
    return name


# /data на прошивке — симлинк на /bkupgrade/data: rm по этим корням = factory reset.
WRITABLE_PREFIXES = ("/data/", "/tmp/", "/bkupgrade/data/")
TOUCHY_ROOTS = {"/", "/data", "/bkupgrade", "/bkupgrade/data", "/opt", "/system", "/etc", "/usr", "/lib", "/bin", "/sbin"}


def safe_writable_path(value: Any) -> str:
    """Путь для изменения/удаления: только пользовательские области."""
    path = safe_remote_path(value)
    if not path.startswith(WRITABLE_PREFIXES):
        raise HTTPException(
            status_code=403,
            detail="изменение разрешено только внутри /data и /tmp (защита от сброса колонки)",
        )
    return path


async def assert_realpath_deletable(path: str) -> None:
    """Второй рубеж: проверяем фактический путь (с раскрытием симлинков)."""
    code, out, _ = await ssh_run_bytes(f"readlink -f {shlex.quote(path)}")
    real = out.decode("utf-8", "replace").strip() if code == 0 else path
    if real in TOUCHY_ROOTS:
        raise HTTPException(status_code=403, detail=f"«{path}» указывает на системный путь — удаление запрещено")
    if not real.startswith(WRITABLE_PREFIXES):
        raise HTTPException(status_code=403, detail=f"«{path}» вне /data и /tmp — удаление запрещено")


LS_LINE = re.compile(
    r"^([\-dlcbps][rwxsStT-]{9})\s+\d+\s+\S+\s+\S+\s+(\d+)\s+"
    r"(\w{3}\s+\d{1,2}\s+(?:\d{4}|\d{2}:\d{2}))\s(.*)$"
)


async def ssh_run_bytes(command: str, input_data: bytes | None = None, timeout: float = 60) -> tuple[int, bytes, bytes]:
    """Выполнить команду на колонке, вернуть (код, stdout, stderr) в байтах."""
    try:
        async with await ssh_connect() as conn:
            process = await conn.run(command, input=input_data, check=False, encoding=None)
            return process.exit_status, process.stdout or b"", process.stderr or b""
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"команда на колонке не выполнена: {exc}")


@app.get("/api/files/list")
async def api_files_list(request: Request, path: str = "/") -> JSONResponse:
    require_auth(request)
    path = safe_remote_path(path)
    code, out, err = await ssh_run_bytes(f"ls -l {shlex.quote(path)} 2>&1; echo EXIT:$?")
    text = out.decode("utf-8", "replace")
    if "EXIT:0" not in text:
        detail = [l for l in text.splitlines() if l and not l.startswith(("total ", "EXIT:"))]
        raise HTTPException(status_code=502, detail=" ; ".join(detail)[:300] or f"ls вернул {code}")
    entries = []
    for line in text.splitlines():
        match = LS_LINE.match(line)
        if not match:
            continue
        perms, size, date, name = match.groups()
        if name in (".", ".."):
            continue
        entries.append({
            "name": name,
            "is_dir": perms.startswith("d"),
            "is_link": perms.startswith("l"),
            "size": int(size),
            "mtime": date,
            "perms": perms,
        })
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return JSONResponse({"path": path, "entries": entries})


@app.get("/api/files/read")
async def api_files_read(request: Request, path: str) -> JSONResponse:
    require_auth(request)
    path = safe_remote_path(path)
    code, out, _ = await ssh_run_bytes(f"wc -c < {shlex.quote(path)}")
    if code != 0:
        raise HTTPException(status_code=502, detail="файл недоступен")
    size = int(out.split()[0])
    if size > 256 * 1024:
        raise HTTPException(status_code=400, detail=f"файл больше 256 КиБ ({size} байт) — открой через скачивание")
    code, content, _ = await ssh_run_bytes(f"cat {shlex.quote(path)}")
    if code != 0:
        raise HTTPException(status_code=502, detail="не удалось прочитать файл")
    try:
        text = content.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        text = content.decode("utf-8", "replace")
        binary = True
    return JSONResponse({"path": path, "size": size, "binary": binary, "content": text})


@app.put("/api/files/write")
async def api_files_write(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    path = safe_writable_path(data.get("path"))
    content = str(data.get("content", "")).encode("utf-8")
    if len(content) > 256 * 1024:
        raise HTTPException(status_code=400, detail="больше 256 КиБ — заливай файлом через загрузку")
    code, _, err = await ssh_run_bytes(f"cat > {shlex.quote(path)}", input_data=content)
    if code != 0:
        raise HTTPException(status_code=502, detail="не удалось записать: " + err.decode("utf-8", "replace")[:200])
    return JSONResponse({"ok": True})


@app.get("/api/files/download")
async def api_files_download(request: Request, path: str) -> Response:
    require_auth(request)
    path = safe_remote_path(path)
    code, out, _ = await ssh_run_bytes(f"wc -c < {shlex.quote(path)}")
    if code != 0:
        raise HTTPException(status_code=502, detail="файл недоступен")
    size = int(out.split()[0])
    if size > 256 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="файл больше 256 МиБ")
    code, content, err = await ssh_run_bytes(f"cat {shlex.quote(path)}", timeout=120)
    if code != 0:
        raise HTTPException(status_code=502, detail="не удалось скачать: " + err.decode("utf-8", "replace")[:200])
    from urllib.parse import quote

    name = PurePosixPath(path).name
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"content-disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@app.post("/api/files/upload")
async def api_files_upload(request: Request, file: UploadFile = File(...), path: str = "/") -> JSONResponse:
    require_auth(request)
    directory = safe_writable_path(path)
    name = safe_remote_name(file.filename)
    content = await file.read()
    if len(content) > 256 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="файл больше 256 МиБ")
    target = f"{directory}/{name}"
    code, _, err = await ssh_run_bytes(f"cat > {shlex.quote(target)}", input_data=content, timeout=180)
    if code != 0:
        raise HTTPException(status_code=502, detail="не удалось загрузить: " + err.decode("utf-8", "replace")[:200])
    return JSONResponse({"ok": True, "path": target, "size": len(content)})


@app.post("/api/files/mkdir")
async def api_files_mkdir(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    path = safe_writable_path(data.get("path"))
    code, _, err = await ssh_run_bytes(f"mkdir {shlex.quote(path)}")
    if code != 0:
        raise HTTPException(status_code=502, detail="не удалось создать папку: " + err.decode("utf-8", "replace")[:200])
    return JSONResponse({"ok": True})


@app.post("/api/files/delete")
async def api_files_delete(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    path = safe_writable_path(data.get("path"))
    if path == "/":
        raise HTTPException(status_code=400, detail="корень удалить нельзя")
    await assert_realpath_deletable(path)
    code, _, err = await ssh_run_bytes(f"rm -rf {shlex.quote(path)}", timeout=120)
    if code != 0:
        raise HTTPException(status_code=502, detail="не удалось удалить: " + err.decode("utf-8", "replace")[:200])
    return JSONResponse({"ok": True})


@app.post("/api/files/rename")
async def api_files_rename(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    source = safe_writable_path(data.get("from"))
    target = safe_writable_path(data.get("to"))
    code, _, err = await ssh_run_bytes(f"mv {shlex.quote(source)} {shlex.quote(target)}")
    if code != 0:
        raise HTTPException(status_code=502, detail="не удалось переименовать: " + err.decode("utf-8", "replace")[:200])
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# Бэкапы /data колонки на ПК (busybox tar без -z → pipeline с gzip)
# --------------------------------------------------------------------------- #
BACKUPS_DIR = BASE / "backups"
BACKUP_EXCLUDES = ["data/opt/entware", "data/MediaCache", "data/opt/backup"]


@app.post("/api/files/backup")
async def api_files_backup(request: Request) -> JSONResponse:
    """Архивировать /data колонки (без entware и кешей) и сохранить на ПК."""
    require_auth(request)
    BACKUPS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    remote = f"/tmp/station-backup-{stamp}.tar.gz"
    excludes = " ".join(f"--exclude={shlex.quote(e)}" for e in BACKUP_EXCLUDES)
    code, _, err = await ssh_run_bytes(
        # /data — симлинк: упаковываем из /bkupgrade, где data — реальный каталог
        f"cd /bkupgrade && busybox tar -cf - {excludes} data | busybox gzip -6 > {shlex.quote(remote)}",
        timeout=600,
    )
    if code != 0:
        raise HTTPException(status_code=502, detail="tar не удался: " + err.decode("utf-8", "replace")[:200])
    code, content, _ = await ssh_run_bytes(f"cat {shlex.quote(remote)}", timeout=600)
    await ssh_run_bytes(f"rm -f {shlex.quote(remote)}")
    if code != 0 or not content or len(content) < 1024:
        raise HTTPException(status_code=502, detail="архив подозрительно мал — бэкап не сохранён")
    name = f"station-data-{stamp}.tar.gz"
    (BACKUPS_DIR / name).write_bytes(content)
    return JSONResponse({"ok": True, "name": name, "size": len(content)})


@app.get("/api/files/backups")
async def api_files_backups(request: Request) -> JSONResponse:
    require_auth(request)
    items = []
    if BACKUPS_DIR.exists():
        for path in sorted(BACKUPS_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True):
            items.append({"name": path.name, "size": path.stat().st_size, "mtime": int(path.stat().st_mtime)})
    return JSONResponse({"backups": items})


@app.get("/api/files/backups/download")
async def api_files_backup_download(request: Request, name: str) -> Response:
    require_auth(request)
    safe_remote_name(name)
    local = BACKUPS_DIR / name
    if not local.exists():
        raise HTTPException(status_code=404, detail="бэкап не найден")
    return Response(
        content=local.read_bytes(),
        media_type="application/gzip",
        headers={"content-disposition": f"attachment; filename={name}"},
    )


@app.post("/api/files/restore")
async def api_files_restore(request: Request) -> JSONResponse:
    """Залить бэкап обратно на колонку. Пути внутри архива проверяются."""
    require_auth(request)
    data = await request.json()
    name = safe_remote_name(data.get("name"))
    local = BACKUPS_DIR / name
    if not local.exists():
        raise HTTPException(status_code=404, detail="бэкап не найден")
    content = local.read_bytes()
    remote = "/tmp/station-restore.tar.gz"
    try:
        code, _, err = await ssh_run_bytes(f"cat > {shlex.quote(remote)}", input_data=content, timeout=300)
        if code != 0:
            raise HTTPException(status_code=502, detail="не удалось загрузить архив на колонку")
        code, out, _ = await ssh_run_bytes(
            f"busybox gzip -dc {shlex.quote(remote)} | busybox tar -tf -", timeout=300,
        )
        if code != 0:
            raise HTTPException(status_code=400, detail="архив повреждён или это не tar.gz")
        entries = [line for line in out.decode("utf-8", "replace").splitlines() if line.strip()]
        bad = [e for e in entries if not (e.startswith("data/") or e.startswith("tmp/"))]
        if bad:
            raise HTTPException(
                status_code=403,
                detail=f"в архиве запрещённые пути ({bad[:3]}...) — восстановление отменено",
            )
        code, _, err = await ssh_run_bytes(
            f"busybox gzip -dc {shlex.quote(remote)} | busybox tar -xf - -C /", timeout=600,
        )
        if code != 0:
            raise HTTPException(status_code=502, detail="распаковка не удалась: " + err.decode("utf-8", "replace")[:200])
        return JSONResponse({"ok": True, "restored": len(entries)})
    finally:
        await ssh_run_bytes(f"rm -f {shlex.quote(remote)}")


# --------------------------------------------------------------------------- #
# Музыка: управление soundya (SoundCloud) и spotiya (Spotify Connect)
# --------------------------------------------------------------------------- #
@app.get("/api/music/status")
async def api_music_status(request: Request) -> JSONResponse:
    require_auth(request)
    result: dict[str, Any] = {}
    code, out, _ = await ssh_run_bytes("soundya status 2>&1", timeout=20)
    text = out.decode("utf-8", "replace").strip()
    result["soundya"] = {
        "installed": "running" in text or "stopped" in text,
        "running": "running" in text,
        "playing": text.lower().startswith("playing") or "playing" in text.lower().split(";")[0] if text else False,
        "raw": text[:300],
    }
    code, out, _ = await ssh_run_bytes("spotiya status 2>&1", timeout=20)
    try:
        spotiya = json.loads(out.decode("utf-8", "replace"))
        result["spotiya"] = {
            "installed": True,
            "active": bool(spotiya.get("active")),
            "connected": bool(spotiya.get("connected")),
            "paused": bool(spotiya.get("paused")),
            "voice": bool(spotiya.get("voice")),
            "airplay": bool(spotiya.get("airplay_running")),
            "title": spotiya.get("title") or "",
            "artist": spotiya.get("artist") or "",
            "volume": spotiya.get("volume"),
            "error": spotiya.get("error") or "",
        }
    except json.JSONDecodeError:
        result["spotiya"] = {"installed": False, "raw": out.decode("utf-8", "replace")[:200]}
    return JSONResponse(result)


@app.post("/api/music/soundya")
async def api_music_soundya(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    cmd = str(data.get("cmd", "")).strip()
    arg = str(data.get("arg", "")).strip()
    allowed = {"play", "pause", "resume", "toggle", "next", "prev", "stop", "shuffle", "repeat", "similar", "likes", "recs", "playlist", "status"}
    if cmd not in allowed:
        raise HTTPException(status_code=400, detail="неизвестная команда soundya")
    if cmd in ("play", "playlist") and not arg:
        raise HTTPException(status_code=400, detail="нужен запрос")
    command = f"soundya {cmd}" + (f" {shlex.quote(arg)}" if arg else "")
    code, out, err = await ssh_run_bytes(command + " 2>&1", timeout=90)
    return JSONResponse({"ok": code == 0, "output": out.decode("utf-8", "replace")[:400] + err.decode("utf-8", "replace")[:200]})


@app.post("/api/music/spotiya")
async def api_music_spotiya(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    cmd = str(data.get("cmd", "")).strip()
    arg = str(data.get("arg", "")).strip()
    allowed = {"play", "pause", "resume", "next", "previous", "stop", "release", "retry", "start", "stop"}
    if cmd not in allowed:
        raise HTTPException(status_code=400, detail="неизвестная команда spotiya")
    command = f"spotiya {cmd}" + (f" {shlex.quote(arg)}" if arg else "")
    code, out, err = await ssh_run_bytes(command + " 2>&1", timeout=60)
    return JSONResponse({"ok": code == 0, "output": out.decode("utf-8", "replace")[:400] + err.decode("utf-8", "replace")[:200]})


@app.post("/api/music/spotiya/volume")
async def api_music_spotiya_volume(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    try:
        value = max(0, min(100, int(data.get("volume"))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="громкость: 0..100")
    code, out, _ = await ssh_run_bytes(f"spotiya volume {value} 2>&1", timeout=30)
    return JSONResponse({"ok": code == 0})


# --------------------------------------------------------------------------- #
# Spotify Soloist (официальный Connect, midi-kit): управление по SSH
# --------------------------------------------------------------------------- #
SOLOIST_DIR = "/data/local/spotify"
SOLOIST_CTL = (
    f"LD_LIBRARY_PATH={SOLOIST_DIR}/runtime/lib "
    f"{SOLOIST_DIR}/runtime/bin/soloist ctl -w 127.0.0.1:24879"
)


@app.get("/api/music/soloist/status")
async def api_music_soloist_status(request: Request) -> JSONResponse:
    require_auth(request)
    code, out, _ = await ssh_run_bytes(f"{SOLOIST_DIR}/service.sh status 2>&1", timeout=20)
    service = out.decode("utf-8", "replace").strip()[:200]
    code, out, _ = await ssh_run_bytes(f"{SOLOIST_CTL} now --json 2>&1", timeout=20)
    player: Any = None
    text = out.decode("utf-8", "replace").strip()
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                player = json.loads(line)
            except json.JSONDecodeError:
                player = None
            break
    code, key_out, _ = await ssh_run_bytes(f"test -s {SOLOIST_DIR}/api-key && echo yes || echo no", timeout=10)
    return JSONResponse({
        "service": service,
        "player": player,
        "has_key": key_out.decode("utf-8", "replace").strip() == "yes",
    })


@app.post("/api/music/soloist/cmd")
async def api_music_soloist_cmd(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    cmd = str(data.get("cmd", "")).strip()
    arg = str(data.get("arg", "")).strip()
    if cmd not in {"play", "pause", "resume", "next", "previous", "volume"}:
        raise HTTPException(status_code=400, detail="неизвестная команда soloist")
    command = f"{SOLOIST_CTL} {cmd}" + (f" {shlex.quote(arg)}" if arg else "")
    code, out, _ = await ssh_run_bytes(command + " 2>&1", timeout=30)
    return JSONResponse({"ok": code == 0, "output": out.decode("utf-8", "replace")[:300]})


@app.post("/api/music/soloist/service")
async def api_music_soloist_service(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    action = str(data.get("action", "")).strip()
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=400, detail="action: start | stop | restart")
    code, out, _ = await ssh_run_bytes(f"{SOLOIST_DIR}/service.sh {action} 2>&1", timeout=60)
    return JSONResponse({"ok": code == 0, "output": out.decode("utf-8", "replace")[:300]})


@app.post("/api/music/soloist/apikey")
async def api_music_soloist_apikey(request: Request) -> JSONResponse:
    """Сохранить spak_-ключ Soloist (создаётся на developer.spotify.com/dashboard/soloist)."""
    require_auth(request)
    data = await request.json()
    key = str(data.get("key", "")).strip()
    if not key.startswith("spak_"):
        raise HTTPException(status_code=400, detail="ключ должен начинаться с spak_")
    key_path = f"{SOLOIST_DIR}/api-key"
    code, _, err = await ssh_run_bytes(f"umask 077; cat > {shlex.quote(key_path)}", input_data=key.encode(), timeout=30)
    if code != 0:
        raise HTTPException(status_code=502, detail="не удалось записать ключ: " + err.decode("utf-8", "replace")[:150])
    code, out, _ = await ssh_run_bytes(f"{SOLOIST_DIR}/service.sh restart 2>&1", timeout=90)
    return JSONResponse({"ok": code == 0, "output": out.decode("utf-8", "replace")[:300]})


@app.post("/api/music/soundya/token")
async def api_music_soundya_token(request: Request) -> JSONResponse:
    """Обновить oauth_token SoundCloud в /data/opt/soundya/config.json."""
    require_auth(request)
    data = await request.json()
    token = str(data.get("token", "")).strip()
    # формат куки oauth_token у SoundCloud: <версия>-<uid>-<время>-<секрет>
    # (например 2-332675-1600559952-phiBI8CyfxpF4); на случай смены формата
    # допускаем и просто длинную строку без пробелов
    if not (re.fullmatch(r"[0-9]{1,3}-[0-9]{2,15}-[0-9]{8,14}-[A-Za-z0-9_\-]{6,64}", token)
            or (20 <= len(token) <= 300 and not re.search(r"\s", token))):
        raise HTTPException(status_code=400, detail="не похоже на oauth_token: это значение куки oauth_token с soundcloud.com, вида 2-332675-1600559952-…")
    code, current, _ = await ssh_run_bytes("cat /data/opt/soundya/config.json 2>/dev/null", timeout=15)
    try:
        config = json.loads(current.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        config = {}
    config.setdefault("proxy", "socks5://192.168.0.179:1081")
    config.setdefault("keyword_only", False)
    config.setdefault("ring", False)
    config["token"] = token
    code, _, err = await ssh_run_bytes(
        "cat > /data/opt/soundya/config.json",
        input_data=json.dumps(config, ensure_ascii=False).encode(), timeout=15,
    )
    if code != 0:
        raise HTTPException(status_code=502, detail="не удалось записать конфиг: " + err.decode("utf-8", "replace")[:150])
    code, out, _ = await ssh_run_bytes("/etc/init.d/S99soundya restart 2>&1; sleep 2; soundya likes 2>&1 | head -2", timeout=60)
    return JSONResponse({"ok": True, "output": out.decode("utf-8", "replace")[:300]})


# --------------------------------------------------------------------------- #
# Локальный плеер ПК: поиск yt-dlp + воспроизведение ffplay («колонка на ПК»)
# --------------------------------------------------------------------------- #
LOCAL_PLAYER: dict[str, Any] = {"proc": None, "title": "", "started": 0.0}
# порядок прокси для поиска с ПК: сначала VPN-канал (xraycore), затем steelproxy
LOCAL_PLAYER_PROXIES = [
    "socks5://127.0.0.1:10811",
    "http://4ys1MYfr:IAWl3Vmz@isp01.steelproxy.com:3071",
]


def _kill_local_player() -> None:
    proc = LOCAL_PLAYER.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    LOCAL_PLAYER["proc"] = None


def _player_running() -> bool:
    proc = LOCAL_PLAYER.get("proc")
    return bool(proc and proc.poll() is None)


@app.post("/api/player/local")
async def api_player_local(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    query = str(data.get("query", "")).strip()
    if not query:
        raise HTTPException(status_code=400, detail="пустой запрос")
    source = str(data.get("source", "youtube")).lower()
    ytdlp = bin_tool("yt-dlp")
    if not ytdlp:
        raise HTTPException(status_code=500, detail="yt-dlp не установлен на ПК — установи во вкладке «Чек-лист»")
    search = {"soundcloud": "scsearch5:"}.get(source, "ytsearch1:")
    _kill_local_player()
    info = None
    detail = ""
    for proxy in LOCAL_PLAYER_PROXIES:
        try:
            proc = subprocess.run(
                [ytdlp, "--dump-json", "--no-playlist", "--no-warnings", "--ignore-errors",
                 "--default-search", search, "--proxy", proxy, query],
                capture_output=True, text=True, timeout=180,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            detail = str(exc)[:150]
            continue
        lines = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
        for line in lines:
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            # DRM-треки SoundCloud отдаёт без рабочего потока — пропускаем
            if candidate.get("formats") or candidate.get("url"):
                info = candidate
                break
        if info:
            break
        detail = (proc.stderr or detail)[-200:]
    if info is None:
        raise HTTPException(status_code=404, detail="играбельного трека нет: " + detail)
    url = info.get("url") or info.get("webpage_url")
    title = info.get("title") or query

    ffplay = bin_tool("ffplay")
    if not ffplay:
        raise HTTPException(status_code=500, detail="ffplay не найден (ставится вместе с ffmpeg в «Чек-листе»)")
    LOCAL_PLAYER["proc"] = subprocess.Popen(
        [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", "80", url],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    LOCAL_PLAYER["title"] = title
    LOCAL_PLAYER["started"] = time.time()
    return JSONResponse({"ok": True, "title": title, "source": source})


@app.post("/api/player/local/stop")
async def api_player_local_stop(request: Request) -> JSONResponse:
    require_auth(request)
    _kill_local_player()
    return JSONResponse({"ok": True})


@app.get("/api/player/local/status")
async def api_player_local_status(request: Request) -> JSONResponse:
    require_auth(request)
    return JSONResponse({
        "playing": _player_running(),
        "title": LOCAL_PLAYER.get("title", ""),
        "elapsed": round(time.time() - LOCAL_PLAYER.get("started", 0.0)),
    })


# --------------------------------------------------------------------------- #
# SSH-терминал в браузере
# --------------------------------------------------------------------------- #
@app.websocket("/ws/console")
async def ws_console(websocket: WebSocket) -> None:
    if not session_valid(websocket.cookies.get("sp_session")):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    conn = None
    process = None
    try:
        conn = await ssh_connect()
        process = await conn.create_process(
            term_type="xterm-256color",
            term_size=(120, 30),
            encoding=None,
            request_pty=True,
        )
        await websocket.send_json({"type": "open"})

        async def pump_stdout() -> None:
            assert process is not None
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                await websocket.send_json(
                    {"type": "data", "data": chunk.decode("utf-8", errors="replace")}
                )

        async def pump_stdin() -> None:
            assert process is not None
            while True:
                message = await websocket.receive_json()
                kind = message.get("type")
                if kind == "input":
                    payload = message.get("data", "")
                    if isinstance(payload, str):
                        payload = payload.encode("utf-8")
                    process.stdin.write(payload)
                    await process.stdin.drain()
                elif kind == "resize":
                    try:
                        process.change_terminal_size(
                            int(message.get("cols", 120)), int(message.get("rows", 30))
                        )
                    except Exception:
                        pass

        out_task = asyncio.create_task(pump_stdout())
        in_task = asyncio.create_task(pump_stdin())
        done, pending = await asyncio.wait(
            {out_task, in_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                await websocket.send_json({"type": "error", "message": str(exc)})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
    finally:
        try:
            if process is not None:
                process.close()
            if conn is not None:
                conn.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Закрепление LED-часов (вендорный maind перерисовывает реальное время каждые ~60 с)
# --------------------------------------------------------------------------- #
async def _clock_pin_loop() -> None:
    """Подтверждать кастомное время каждые 5 с, пока включено закрепление.

    Вендорный maind перерисовывает реальное время по своему таймеру (~раз в 30 с),
    поэтому короткий интервал сводит мигание реального времени к минимуму.
    """
    while True:
        await asyncio.sleep(5)
        if not clock_pin.get("active") or not clock_pin.get("time"):
            continue
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                await session.put(
                    f"http://{cfg['station_ip']}:{cfg['yam_port']}/api/clock",
                    json={"time": clock_pin["time"], "brightness": int(clock_pin.get("brightness", 170))},
                )
        except Exception:
            pass  # станция недоступна — повторим на следующем такте


@app.get("/api/clock/pin")
async def api_clock_pin_get(request: Request) -> JSONResponse:
    require_auth(request)
    return JSONResponse(dict(clock_pin))


@app.post("/api/clock/pin")
async def api_clock_pin_set(request: Request) -> JSONResponse:
    """Включить/выключить закрепление времени на LED-часах."""
    require_auth(request)
    data = await request.json()
    active = bool(data.get("active"))
    time_value = str(data.get("time", "")).strip()
    if active and not re.fullmatch(r"\d{2}:\d{2}", time_value):
        raise HTTPException(status_code=400, detail="время в формате ЧЧ:ММ")
    clock_pin.update({
        "active": active,
        "time": time_value,
        "brightness": int(data.get("brightness", clock_pin.get("brightness", 170))),
    })
    if active:
        # первый показ — сразу же, не дожидаясь такта (PUT /api/clock отвечает 204)
        status, _ = await yam_api(
            "PUT", "/api/clock",
            data=json.dumps({"time": time_value, "brightness": int(clock_pin["brightness"])}),
            content_type="application/json",
        )
        if not (200 <= status < 300):
            raise HTTPException(status_code=502, detail="не удалось показать время на колонке")
    return JSONResponse(dict(clock_pin))


# --------------------------------------------------------------------------- #
# Ночной профиль LED-часов: в заданное окно панель держит низкую яркость,
# показывая реальное время; днём один раз возвращает дневную яркость.
# Работает, пока запущена панель; закрепление времени (pin) приоритетнее.
# --------------------------------------------------------------------------- #
def _hhmm_in_window(hhmm: str, start: str, end: str) -> bool:
    try:
        minutes = int(hhmm[:2]) * 60 + int(hhmm[3:5])
        start_m = int(start[:2]) * 60 + int(start[3:5])
        end_m = int(end[:2]) * 60 + int(end[3:5])
    except (ValueError, IndexError):
        return False
    if start_m == end_m:
        return False
    return start_m <= minutes < end_m if start_m < end_m else minutes >= start_m or minutes < end_m


async def _night_clock_loop() -> None:
    while True:
        await asyncio.sleep(60)
        if not clock_night.get("enabled") or clock_pin.get("active"):
            continue
        now = datetime.now().strftime("%H:%M")
        in_window = _hhmm_in_window(now, str(clock_night["start"]), str(clock_night["end"]))
        target = None
        if in_window:
            target = int(clock_night["night_brightness"])
            clock_night["was_night"] = True
        elif clock_night.get("was_night"):
            target = int(clock_night["day_brightness"])
            clock_night["was_night"] = False
        if target is None:
            continue
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                await session.put(
                    f"http://{cfg['station_ip']}:{cfg['yam_port']}/api/clock",
                    json={"time": now, "brightness": target},
                )
        except Exception:
            pass  # станция недоступна — повторим на следующем такте


@app.get("/api/clock/night")
async def api_clock_night_get(request: Request) -> JSONResponse:
    require_auth(request)
    return JSONResponse(dict(clock_night))


@app.post("/api/clock/night")
async def api_clock_night_set(request: Request) -> JSONResponse:
    """Настройки ночного профиля: окно времени и яркости (0–255)."""
    require_auth(request)
    data = await request.json()
    for key in ("start", "end"):
        value = str(data.get(key, clock_night[key]))
        if not re.fullmatch(r"\d{2}:\d{2}", value):
            raise HTTPException(status_code=400, detail=f"«{key}» в формате ЧЧ:ММ")
        clock_night[key] = value
    for key in ("night_brightness", "day_brightness"):
        if key in data:
            value = int(data[key])
            if not 0 <= value <= 255:
                raise HTTPException(status_code=400, detail="яркость 0–255")
            clock_night[key] = value
    clock_night["enabled"] = bool(data.get("enabled"))
    if not clock_night["enabled"]:
        clock_night["was_night"] = False
    return JSONResponse(dict(clock_night))


# --------------------------------------------------------------------------- #
# Системные звуки колонки: /data/quasar/data/sounds/*.opus — приветствие
# активации, ошибки и т.д. Замена с бэкапом; загрузка конвертируется в opus.
# --------------------------------------------------------------------------- #
SOUNDS_DIR = "/data/quasar/data/sounds"
SOUND_SUBDIRS = ["common", "ru-RU"]


def _ffmpeg_bin() -> str:
    found = bin_tool("ffmpeg")
    if not found:
        raise HTTPException(status_code=500, detail="ffmpeg не найден на ПК с панелью — установи его во вкладке «Чек-лист»")
    return found


@app.get("/api/sounds")
async def api_sounds_list(request: Request) -> JSONResponse:
    require_auth(request)
    code, out, err = await ssh_run_bytes(
        f"ls -la {SOUNDS_DIR}/common/ {SOUNDS_DIR}/ru-RU/ 2>/dev/null")
    if code != 0:
        raise HTTPException(status_code=502, detail="SSH: " + err.decode(errors="replace")[:160])
    sounds: list[dict] = []
    current: str | None = None
    for line in out.decode("utf-8", errors="replace").splitlines():
        if line.startswith(f"{SOUNDS_DIR}/") and line.rstrip().endswith(":"):
            current = line.rstrip().rstrip(":").rstrip("/").split("/")[-1]
            continue
        parts = line.split(None, 8)
        if current and len(parts) >= 9 and parts[0].startswith("-") and parts[8].endswith(".opus"):
            sounds.append({"dir": current, "name": parts[8], "size": parts[4],
                           "date": f"{parts[5]} {parts[6]} {parts[7]}", "backup": ""})
    # бэкапы: отдельный проход
    code2, out2, _ = await ssh_run_bytes(f"ls {SOUNDS_DIR}/*/*.bak-panel 2>/dev/null")
    backups = {line.split("/")[-1].replace(".bak-panel", "") for line in out2.decode(errors="replace").splitlines() if line.strip()}
    for item in sounds:
        item["backup"] = "да" if item["name"] in backups else ""
    return JSONResponse({"ok": True, "sounds": sounds})


@app.get("/api/sounds/file")
async def api_sounds_file(request: Request, name: str, backup: str = "") -> Response:
    """Прослушивание звука (или его бэкапа) — стримим opus как есть."""
    require_auth(request)
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+\.opus", name) or "/" in name:
        raise HTTPException(status_code=400, detail="некорректное имя файла")
    subdir = "ru-RU" if name != "alice-iot-sounds-turn-on.opus" and name != "alice-iot-sounds-turn-off.opus" else "common"
    if not any(s["dir"] == subdir and s["name"] == name for s in (await api_sounds_list_data())):
        raise HTTPException(status_code=404, detail="такого звука нет на колонке")
    suffix = ".bak-panel" if backup == "1" else ""
    code, out, err = await ssh_run_bytes(f"cat {SOUNDS_DIR}/{subdir}/{name}{suffix}")
    if code != 0:
        raise HTTPException(status_code=502, detail="SSH: " + err.decode(errors="replace")[:160])
    return Response(content=out, media_type="audio/ogg")


async def api_sounds_list_data() -> list[dict]:
    """Внутренний хелпер: тот же список, что у GET /api/sounds, без обёртки."""
    code, out, _ = await ssh_run_bytes(f"ls -la {SOUNDS_DIR}/common/ {SOUNDS_DIR}/ru-RU/ 2>/dev/null")
    sounds: list[dict] = []
    current: str | None = None
    for line in out.decode("utf-8", errors="replace").splitlines():
        if line.startswith(f"{SOUNDS_DIR}/") and line.rstrip().endswith(":"):
            current = line.rstrip().rstrip(":").rstrip("/").split("/")[-1]
            continue
        parts = line.split(None, 8)
        if current and len(parts) >= 9 and parts[0].startswith("-") and parts[8].endswith(".opus"):
            sounds.append({"dir": current, "name": parts[8]})
    return sounds


@app.post("/api/sounds/upload")
async def api_sounds_upload(request: Request) -> JSONResponse:
    """Заменить системный звук: принимаем opus/ogg/wav/mp3, конвертируем в opus.

    Оригинал бэкапится один раз в <имя>.bak-panel и восстанавливается кнопкой."""
    require_auth(request)
    form = await request.form()
    upload = form.get("file")
    name = str(form.get("name") or "")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="нужен файл")
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+\.opus", name):
        raise HTTPException(status_code=400, detail="выбери имя существующего звука")
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="файл пуст")
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="файл больше 8 МБ")
    subdir = "common" if name.startswith("alice-iot") else "ru-RU"
    sounds = await api_sounds_list_data()
    if not any(s["dir"] == subdir and s["name"] == name for s in sounds):
        raise HTTPException(status_code=404, detail="такого звука нет на колонке")

    # конвертация в opus (48 кГц, mono) локально через ffmpeg
    ffmpeg = _ffmpeg_bin()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input.bin"
        dst = Path(tmp) / "out.opus"
        src.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
            "-c:a", "libopus", "-b:a", "24k", "-ar", "48000", "-ac", "1", str(dst),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(proc.communicate(), 60)
        if proc.returncode != 0 or not dst.exists():
            raise HTTPException(status_code=400, detail="ffmpeg не смог сконвертировать: " + stderr.decode(errors="replace")[-160:])
        opus = dst.read_bytes()
    if len(opus) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="сконвертированный звук больше 2 МБ")

    remote = f"{SOUNDS_DIR}/{subdir}/{name}"
    try:
        conn = await ssh_connect()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="SSH: " + str(exc)[:160])
    try:
        await asyncio.wait_for(conn.run(
            f"[ -f {remote}.bak-panel ] || cp -p {remote} {remote}.bak-panel",
            check=False, encoding="utf-8"), 15)
        result = await asyncio.wait_for(conn.run(f"cat > {remote}", input=opus, check=False, encoding=None), 25)
        if result.exit_status not in (0, None):
            raise ValueError(f"запись не удалась (status {result.exit_status})")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:180])
    finally:
        conn.close()
    return JSONResponse({"ok": True, "name": name, "size": len(opus), "backup": "создан при первой замене"})


@app.post("/api/sounds/restore")
async def api_sounds_restore(request: Request) -> JSONResponse:
    """Вернуть оригинальный звук из бэкапа .bak-panel."""
    require_auth(request)
    data = await request.json()
    name = str(data.get("name") or "")
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+\.opus", name):
        raise HTTPException(status_code=400, detail="некорректное имя файла")
    subdir = "common" if name.startswith("alice-iot") else "ru-RU"
    remote = f"{SOUNDS_DIR}/{subdir}/{name}"
    code, out, err = await ssh_run_bytes(
        f"[ -f {remote}.bak-panel ] && cp -p {remote}.bak-panel {remote} && echo OK || echo NOBACKUP")
    text = out.decode(errors="replace").strip()
    if "NOBACKUP" in text:
        raise HTTPException(status_code=404, detail="бэкапа этого звука нет — его не меняли")
    if "OK" not in text:
        raise HTTPException(status_code=502, detail="SSH: " + err.decode(errors="replace")[:160])
    return JSONResponse({"ok": True, "name": name})


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn

    print(f"[*] Панель: http://127.0.0.1:{cfg['panel_port']}  (станция {cfg['station_ip']})")
    uvicorn.run(app, host=cfg["panel_host"], port=int(cfg["panel_port"]), log_level="warning")
