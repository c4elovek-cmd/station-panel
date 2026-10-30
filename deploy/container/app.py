# -*- coding: utf-8 -*-
"""Точка входа контейнера Pterodactyl (egg python generic, python_3.13).

Поднимает три сервиса за одним внешним портом $SERVER_PORT (aiohttp-роутер):
  - chisel server --reverse  на 127.0.0.1:8080  (WS /chisel* — туннель от колонки)
  - панель (panel/server.py) на 127.0.0.1:8787  (весь остальной HTTP и WS /ws/console)
  - voice-bridge             на 127.0.0.1:8765  (внутренний, для панели)
Дети при падении перезапускаются; /healthz показывает статус без авторизации.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

BASE = Path(__file__).resolve().parent
PANEL_PORT, BRIDGE_PORT, CHISEL_PORT = 8787, 8765, 8080
PANEL = f"http://127.0.0.1:{PANEL_PORT}"
CHISEL = f"http://127.0.0.1:{CHISEL_PORT}"
CHISEL_AUTH = "station:st4a31fa13973b8495e22a8fdf488a3589"

HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "host"}

PROCS: dict[str, subprocess.Popen] = {}
SESSION: aiohttp.ClientSession


def bridge_env() -> dict[str, str]:
    env = dict(os.environ)
    try:
        cfg = json.loads((BASE / "panel" / "config.json").read_text(encoding="utf-8"))
        env["YSE_ALICE_X_TOKEN"] = str(cfg.get("yandex_x_token", ""))
        # один токен на мост и панель: панель шлёт его в voice_bridge_token
        env["VOICE_TOKEN"] = str(cfg.get("voice_bridge_token", ""))
    except Exception:
        pass
    env["BRIDGE_CONFIG"] = str(BASE / "bridge" / "bridge-config.json")
    return env


SPAWNERS = {
    "chisel": lambda: subprocess.Popen(
        [str(BASE / "chisel"), "server", "--reverse", "--auth", CHISEL_AUTH,
         "--port", str(CHISEL_PORT), "--host", "127.0.0.1"], cwd=str(BASE)),
    "panel": lambda: subprocess.Popen(
        [sys.executable, "server.py"], cwd=str(BASE / "panel")),
    "bridge": lambda: subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(BRIDGE_PORT), "--log-level", "warning"],
        cwd=str(BASE / "bridge"), env=bridge_env()),
}


def spawn_all() -> None:
    for name, spawn in SPAWNERS.items():
        if name in PROCS and PROCS[name].poll() is None:
            continue
        try:
            PROCS[name] = spawn()
            print(f"[supervisor] {name} запущен (pid {PROCS[name].pid})", flush=True)
        except Exception as exc:
            print(f"[supervisor] {name} не запустился: {exc}", flush=True)


async def watchdog() -> None:
    while True:
        await asyncio.sleep(5)
        for name, proc in list(PROCS.items()):
            if proc.poll() is not None:
                print(f"[supervisor] {name} вышел, код {proc.poll()} — перезапуск", flush=True)
                PROCS.pop(name, None)
                await asyncio.sleep(3)
                spawn_all()


def port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({
        "ports": {"panel": port_open(PANEL_PORT), "bridge": port_open(BRIDGE_PORT),
                  "chisel": port_open(CHISEL_PORT), "tunnel_ssh": port_open(2222)},
        "procs": {name: proc.poll() is None for name, proc in PROCS.items()},
    })


def upstream_for(path: str) -> str:
    return CHISEL if path == "/chisel" or path.startswith("/chisel/") else PANEL


async def proxy_http(request: web.Request) -> web.StreamResponse:
    url = upstream_for(request.path) + request.rel_url.path_qs
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    body = await request.read()
    try:
        async with SESSION.request(request.method, url, headers=headers, data=body,
                                   allow_redirects=False) as resp:
            out_headers = {k: v for k, v in resp.headers.items()
                           if k.lower() not in HOP_HEADERS}
            out = web.StreamResponse(status=resp.status, headers=out_headers)
            await out.prepare(request)
            async for chunk in resp.content.iter_any():
                await out.write(chunk)
            await out.write_eof()
            return out
    except Exception as exc:
        return web.json_response({"detail": f"бэкенд недоступен: {type(exc).__name__}: {exc}"},
                                 status=502)


async def proxy_ws(request: web.Request) -> web.StreamResponse:
    """WS-туннель: браузер/колонка <-> бэкенд (панель или chisel)."""
    upstream = upstream_for(request.path)
    is_chisel = upstream == CHISEL
    url = upstream + request.rel_url.path_qs
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_HEADERS | {"sec-websocket-key", "sec-websocket-version",
                                                  "sec-websocket-extensions", "sec-websocket-protocol"}}
    # chisel клиент и сервер договариваются через субпротокол chisel-v3 — нужно
    # объявить его с обеих сторон прокси, иначе chisel отклоняет апгрейд
    proto = ["chisel-v3"] if is_chisel else None
    client_ws = web.WebSocketResponse(max_msg_size=0, protocols=proto)
    await client_ws.prepare(request)
    try:
        async with SESSION.ws_connect(url, headers=headers, max_msg_size=0,
                                      protocols=proto,
                                      autoping=True, heartbeat=None) as upstream_ws:
            async def a2b():
                async for msg in client_ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await upstream_ws.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await upstream_ws.send_bytes(msg.data)
                    elif msg.type == aiohttp.WSMsgType.PING:
                        await upstream_ws.ping(msg.data)
                    elif msg.type == aiohttp.WSMsgType.PONG:
                        await upstream_ws.pong(msg.data)
                    else:
                        break
                await upstream_ws.close()

            async def b2a():
                async for msg in upstream_ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await client_ws.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await client_ws.send_bytes(msg.data)
                    elif msg.type == aiohttp.WSMsgType.PING:
                        await client_ws.ping(msg.data)
                    elif msg.type == aiohttp.WSMsgType.PONG:
                        await client_ws.pong(msg.data)
                    else:
                        break
                await client_ws.close()

            done, pending = await asyncio.wait(
                [asyncio.create_task(a2b()), asyncio.create_task(b2a())],
                return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception as exc:
        print(f"[router] ws {request.path} -> {upstream}: {type(exc).__name__}: {exc}", flush=True)
    await client_ws.close()
    return client_ws


async def entry(request: web.Request) -> web.StreamResponse:
    if request.path == "/chisel" or request.path.startswith("/chisel/"):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await proxy_ws(request)
        return await proxy_http(request)
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_ws(request)
    return await proxy_http(request)


async def on_startup(app: web.Application) -> None:
    global SESSION
    SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
    spawn_all()
    app["watchdog"] = asyncio.create_task(watchdog())


async def on_cleanup(app: web.Application) -> None:
    app["watchdog"].cancel()
    for proc in PROCS.values():
        try:
            proc.terminate()
        except Exception:
            pass
    await SESSION.close()


app = web.Application(client_max_size=1024 ** 3)
app.router.add_get("/healthz", healthz)
app.router.add_route("*", "/{tail:.*}", entry)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    port = int(os.environ.get("SERVER_PORT", "19170"))
    print(f"[supervisor] роутер на 0.0.0.0:{port} (панель:{PANEL_PORT} мост:{BRIDGE_PORT} chisel:{CHISEL_PORT})",
          flush=True)
    web.run_app(app, host="0.0.0.0", port=port, print=None, access_log=None)
