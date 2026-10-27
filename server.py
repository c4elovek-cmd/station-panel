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
from pathlib import Path
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
    "panel_port": 8787,
    "panel_host": "0.0.0.0",
    "password_salt": "",
    "password_hash": "",
    "session_hours": 72,
}

SESSIONS: dict[str, float] = {}  # token -> expires_at
_ssh_lock = asyncio.Lock()


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
# SSH к колонке
# --------------------------------------------------------------------------- #
async def ssh_connect():
    return await asyncssh.connect(
        cfg["station_ip"],
        port=cfg["ssh_port"],
        username=cfg["ssh_user"],
        password="",
        known_hosts=None,
        client_keys=None,
        login_timeout=10,
    )


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
        # git@github.com:owner/repo(.git) → owner/repo
        slug = re.sub(r"^[^:/]+[:/]|\.git$", "", remote_url.strip())
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
if __name__ == "__main__":
    import uvicorn

    print(f"[*] Панель: http://127.0.0.1:{cfg['panel_port']}  (станция {cfg['station_ip']})")
    uvicorn.run(app, host=cfg["panel_host"], port=int(cfg["panel_port"]), log_level="warning")
