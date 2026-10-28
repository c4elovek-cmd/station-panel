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
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

import aiohttp
import asyncssh
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
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


@app.on_event("startup")
async def _start_clock_pin() -> None:
    """Поднять фоновую задачу подтверждения закреплённого времени на часах."""
    asyncio.create_task(_clock_pin_loop())


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


@app.post("/api/detect")
async def api_detect(request: Request) -> JSONResponse:
    """Найти колонку: сначала текущий IP, затем скан /24 подсети по порту YAM."""
    require_auth(request)

    # сеть берём из текущего IP; если не задан — из локального адреса панели
    network_prefix = ".".join(cfg["station_ip"].split(".")[:3]) if cfg.get("station_ip") else None
    if not network_prefix or not all(p.isdigit() for p in network_prefix.split(".")):
        network_prefix = None
        try:
            local = aiohttp.ClientSession()
            await local.close()
        except Exception:
            pass

    async def probe(ip: str) -> str | None:
        try:
            timeout = aiohttp.ClientTimeout(total=1.2)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"http://{ip}:{cfg['yam_port']}/api/device") as resp:
                    if resp.status == 200:
                        device = await resp.json(content_type=None)
                        if isinstance(device, dict) and device.get("model"):
                            return ip
        except Exception:
            pass
        return None

    candidates = []
    current = cfg.get("station_ip", "")
    if network_prefix:
        local_octet = current.split(".")[3] if current.startswith(network_prefix + ".") and current.split(".")[-1].isdigit() else None
        for octet in range(1, 255):
            if str(octet) == local_octet:
                continue
            candidates.append(f"{network_prefix}.{octet}")

    # текущий IP проверяем первым — железо уже настроено
    checked_current = await probe(current) if current else None
    if checked_current:
        return JSONResponse({"found": True, "ip": checked_current, "scanned": 0})

    # параллельный скан подсети, пачками
    found = None
    batch_size = 40
    for i in range(0, len(candidates), batch_size):
        results = await asyncio.gather(*(probe(ip) for ip in candidates[i:i + batch_size]))
        hit = next((ip for ip in results if ip), None)
        if hit:
            found = hit
            break

    if found:
        cfg["station_ip"] = found
        save_config(cfg)
        _status_cache["ts"] = 0.0
        print(f"[+] колонка найдена автоматически: {found}")
        return JSONResponse({"found": True, "ip": found, "scanned": len(candidates)})
    return JSONResponse({"found": False, "scanned": len(candidates)})


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
        "echo '#ADB'; pidof adbd"
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
            elif section and section not in ("MEM", "DISK", "TEMP", "UP"):
                data[section.lower()] = line

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
    повторный запуск не плодит дубли в ~/.ssh/authorized_keys.
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
        'umask 077; mkdir -p ~/.ssh; '
        'touch ~/.ssh/authorized_keys; '
        'grep -v "station-panel" ~/.ssh/authorized_keys > ~/.ssh/ak_new; '
        f'echo "{pub_line}" >> ~/.ssh/ak_new; '
        'mv ~/.ssh/ak_new ~/.ssh/authorized_keys; '
        'chmod 600 ~/.ssh/authorized_keys; echo OK'
    )
    code, out, err = await ssh_run(script, timeout=20)
    ok = code == 0 and "OK" in out
    return {"installed": ok, "public_key": pub_line, "error": err.strip() if not ok else ""}


async def sftp_probe() -> dict[str, Any]:
    """Проверка SFTP-подсистемы.

    dropbear обычно собран без sftp-server — файловый менеджер панели
    в этом случае работает через exec-каналы SSH, это не ошибка.
    """
    try:
        async with await ssh_connect() as conn:
            try:
                sftp = await conn.start_sftp()
                await sftp.listdir(".")
                return {"sftp": "subsystem", "detail": "SFTP-подсистема доступна"}
            except (asyncssh.SFTPError, OSError, NotImplementedError) as exc:
                return {"sftp": "no", "detail": f"подсистема недоступна: {exc}"[:200]}
    except Exception as exc:
        return {"sftp": "no", "detail": f"SSH недоступен: {exc}"[:200]}


def _probe_ip_sync(ip: str) -> str | None:
    """Синхронная проверка одного IP: отвечает ли YAM (для asyncio.to_thread)."""
    try:
        import urllib.request

        with urllib.request.urlopen(  # noqa: S310
            f"http://{ip}:{cfg['yam_port']}/api/device", timeout=1.5
        ) as resp:
            if resp.status == 200:
                device = json.loads(resp.read(4096))
                if isinstance(device, dict) and device.get("model"):
                    return ip
    except Exception:
        pass
    return None


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
        network_prefix = ".".join(str(cfg.get("station_ip", "")).split(".")[:3])
        if not all(p.isdigit() for p in network_prefix.split(".")):
            return JSONResponse({"ok": False, "steps": steps,
                                 "error": "не знаю, какую подсеть сканировать — задайте IP станции"})
        current = cfg["station_ip"]
        local_octet = current.split(".")[3] if current.startswith(network_prefix + ".") else None
        candidates = [f"{network_prefix}.{o}" for o in range(1, 255) if str(o) != local_octet]
        for i in range(0, len(candidates), 40):
            batch = candidates[i:i + 40]
            scanned += len(batch)
            results = await asyncio.gather(*(asyncio.to_thread(_probe_ip_sync, ip) for ip in batch))
            hit = next((ip for ip in results if ip), None)
            if hit:
                found_ip = hit
                break
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
        cfg["station_ip"] = str(data["station_ip"]).strip()
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
@app.get("/api/panel/update")
async def api_panel_update_check(request: Request) -> JSONResponse:
    """Сравнить локальный коммит с последним в Git-репозитории на GitHub."""
    require_auth(request)
    root_dir = Path(__file__).resolve().parent
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


@app.post("/api/panel/update")
async def api_panel_update(request: Request) -> JSONResponse:
    """git pull → pip install → перезапуск панели. Панель после ответа рестартует."""
    require_auth(request)
    root_dir = Path(__file__).resolve().parent
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

print("[station-voice] слушаю: " .. table.concat(TRIGGERS, ", "))
while true do
  local ok, event = pcall(alice.ipc.next_speech, 60000)
  if ok and type(event) == "table" and type(event.transcript) == "string" then
    local query = match_trigger(event.transcript)
    if query then
      pcall(alice.command.cancel)
      pcall(handle, event.transcript, query == "" and "привет" or query)
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
        'version = "1.1.1"\n'
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

        # включить пакет и поставить на автозапуск
        await yam_api("POST", f"/api/plugins/{VOICE_PLUGIN_ID}/toggle")
        await yam_api("POST", f"/api/plugins/{VOICE_PLUGIN_ID}/stop")
        status, body = await yam_api("POST", f"/api/plugins/{VOICE_PLUGIN_ID}/service/enable")
        if status not in (200, 202, 204):
            return JSONResponse({"detail": f"автозапуск: YAM вернул {status}: {body}"}, status_code=502)
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
        "--host", "127.0.0.1", "--port", str(BRIDGE_PORT), "--log-level", "warning",
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
    """Состояние локального моста: код найден, запущен, /health, LLM."""
    require_auth(request)
    bdir = _bridge_dir()
    return JSONResponse({
        "dir": str(bdir),
        "code_found": (bdir / "app.py").exists(),
        "running": await _bridge_health(),
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


@app.post("/api/speaker/say")
async def api_speaker_say(request: Request) -> JSONResponse:
    require_auth(request)
    data = await request.json()
    text = str(data.get("text", "")).strip()
    if not text or len(text) > 500:
        raise HTTPException(status_code=400, detail="текст от 1 до 500 символов")
    voice = SAY_VOICES.get(str(data.get("voice", "svetlana")))
    mood = str(data.get("mood", "normal"))
    if not voice or mood not in SAY_MOODS:
        raise HTTPException(status_code=400, detail="неизвестный голос или интонация")

    import tempfile

    import edge_tts

    tmp_dir = Path(tempfile.gettempdir())
    mp3_path = tmp_dir / f"station-say-{secrets.token_hex(4)}.mp3"
    wav_path = tmp_dir / f"station-say-{secrets.token_hex(4)}.wav"
    try:
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(mp3_path))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"синтез речи не удался: {exc}")

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise HTTPException(status_code=500, detail="ffmpeg не найден в PATH")
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
# --------------------------------------------------------------------------- #
async def _read_master_volume() -> int | None:
    """Текущая громкость Master в % (amixer -c 0 sget Master)."""
    code, out, _ = await ssh_run("amixer -c 0 sget Master 2>/dev/null")
    if code != 0:
        return None
    match = re.search(r"Front Left:\s*Playback\s+\d+\s+\[(\d+)%\]", out)
    return int(match.group(1)) if match else None


@app.get("/api/volume")
async def api_volume_get(request: Request) -> JSONResponse:
    require_auth(request)
    volume = await _read_master_volume()
    if volume is None:
        return JSONResponse({"ok": False, "detail": "не удалось прочитать громкость (amixer)"}, status_code=502)
    return JSONResponse({"ok": True, "volume": volume})


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
    if len(token) < 40:
        raise HTTPException(status_code=400, detail="похоже, это не oauth_token")
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
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        raise HTTPException(status_code=500, detail="yt-dlp не установлен на ПК")
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

    ffplay = shutil.which("ffplay")
    if not ffplay:
        raise HTTPException(status_code=500, detail="ffplay не найден (ставится вместе с ffmpeg)")
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
if __name__ == "__main__":
    import uvicorn

    print(f"[*] Панель: http://127.0.0.1:{cfg['panel_port']}  (станция {cfg['station_ip']})")
    uvicorn.run(app, host=cfg["panel_host"], port=int(cfg["panel_port"]), log_level="warning")
