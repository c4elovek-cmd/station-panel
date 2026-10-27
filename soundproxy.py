# -*- coding: utf-8 -*-
"""
Мини SOCKS5-прокси для колонки: только домены SoundCloud, только локальная сеть.
ПК в VPN — колонка через этот прокси достаёт soundcloud.com.
Запуск: pythonw soundproxy.py (порт 1080 по умолчанию).
"""
import asyncio
import ipaddress
import sys

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 1081
# внешний HTTP-прокси (steelproxy) с авторизацией; звук идёт: колонка → 1081 → steelproxy → soundcloud
# креды берутся из soundproxy-secrets.json рядом (в git не попадает) или из env
_secrets_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soundproxy-secrets.json")
_secrets: dict[str, str] = {}
try:
    with open(_secrets_file, encoding="utf-8") as f:
        _secrets = json.load(f)
except Exception:
    _secrets = {
        "upstream_host": os.environ.get("SOUNDPROXY_UP_HOST", ""),
        "upstream_port": os.environ.get("SOUNDPROXY_UP_PORT", "0"),
        "upstream_user": os.environ.get("SOUNDPROXY_UP_USER", ""),
        "upstream_password": os.environ.get("SOUNDPROXY_UP_PASS", ""),
    }
UPSTREAM_HTTP = (
    _secrets.get("upstream_host", ""),
    int(_secrets.get("upstream_port", "0") or 0),
    _secrets.get("upstream_user", ""),
    _secrets.get("upstream_password", ""),
)

ALLOWED_SUFFIXES = (
    "soundcloud.com",
    "sndcdn.com",
    "scdn.co",
    "soundcloud.app",
    "soundcloud.cloud",
    "spotify.com",
    "spotifycdn.com",
    "akamaized.net",
    "fastly.net",
)


def allowed_host(host: str) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_SUFFIXES)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def upstream_open(host: str, port: int):
    """CONNECT-туннель через внешний HTTP-прокси с авторизацией."""
    import base64

    up_host, up_port, user, pwd = UPSTREAM_HTTP
    up_reader, up_writer = await asyncio.wait_for(
        asyncio.open_connection(up_host, up_port), 15
    )
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    request = (
        f"CONNECT {host}:{port} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Proxy-Authorization: Basic {token}\r\n"
        "Proxy-Connection: keep-alive\r\n\r\n"
    )
    up_writer.write(request.encode())
    await up_writer.drain()
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = await up_reader.read(256)
        if not chunk:
            raise RuntimeError("upstream закрыл соединение")
        response += chunk
    status = response.split(b"\r\n", 1)[0]
    if b" 200 " not in status:
        raise RuntimeError(f"upstream CONNECT: {status!r}")
    return up_reader, up_writer


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")[0]
    print(f"[conn] from {peer}", flush=True)
    if not ipaddress.ip_address(peer).is_private:
        writer.close()
        return
    # greeting: VER, NMETHODS, METHODS (мы всегда отвечаем «без авторизации»)
    ver = (await reader.readexactly(1))[0]
    if ver != 5:
        writer.close()
        return
    nmethods = (await reader.readexactly(1))[0]
    await reader.readexactly(nmethods)
    writer.write(b"\x05\x00")
    await writer.drain()
    # CONNECT
    head = await reader.readexactly(4)
    if head[1] != 1:  # только CONNECT
        writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
        await writer.drain()
        writer.close()
        return
    atyp = head[3]
    if atyp == 1:
        host = socket_host = ".".join(str(b) for b in await reader.readexactly(4))
    elif atyp == 3:
        length = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(length)).decode("idna")
    elif atyp == 4:
        raw = await reader.readexactly(16)
        host = str(ipaddress.IPv6Address(raw))
    else:
        writer.close()
        return
    port = int.from_bytes(await reader.readexactly(2), "big")
    if not allowed_host(host):
        # колонка может слать и IP: разрешаем, только если это soundcloud-адрес проверить нельзя — запрещаем не-домены
        if atyp != 3:
            writer.write(b"\x05\x02\x00\x01" + b"\x00" * 6)
            await writer.drain()
            writer.close()
            return
        writer.write(b"\x05\x02\x00\x01" + b"\x00" * 6)
        await writer.drain()
        writer.close()
        return
    try:
        remote_reader, remote_writer = await upstream_open(host, port)
    except Exception as exc:
        print(f"upstream fail {host}:{port}: {exc}", flush=True)
        writer.write(b"\x05\x04\x00\x01" + b"\x00" * 6)
        await writer.drain()
        writer.close()
        return
    writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
    await writer.drain()
    print(f"[ok] tunnel {host}:{port}", flush=True)
    await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))


async def main() -> None:
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    print(f"soundproxy on {LISTEN_HOST}:{LISTEN_PORT}, allow: {', '.join(ALLOWED_SUFFIXES)}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
