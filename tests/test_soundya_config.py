"""Проверки адреса soundya без запуска фоновых задач и подключения к колонке.

Запуск: python -m unittest discover -s tests -p test_soundya_config.py
"""
import ast
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.responses import JSONResponse


def load_handlers():
    """Загружаем реальные обработчики, не импортируя сервер с побочными эффектами."""
    source = Path(__file__).resolve().parents[1] / "server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"api_get_config", "api_put_config", "soundya_web_base"}
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    initial = {"station_ip": "127.0.0.1", "ssh_port": 2222,
               "yam_port": 8099, "ssh_user": "root"}
    saved = []
    namespace = {"cfg": initial, "Any": object, "Request": object,
                 "JSONResponse": JSONResponse, "HTTPException": HTTPException,
                 "re": __import__("re"), "require_auth": lambda request: None,
                 "save_config": lambda cfg: saved.append(copy.deepcopy(cfg)),
                 "_status_cache": {"ts": 123}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace, saved


class SoundyaConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.handlers, self.saved = load_handlers()

    async def put(self, payload):
        async def request_json():
            return payload
        return await self.handlers["api_put_config"](SimpleNamespace(json=request_json))

    async def test_tunnel_changes_immediately(self):
        base = self.handlers["soundya_web_base"]
        self.assertEqual(base(), "http://127.0.0.1:8765")
        response = await self.put({"soundya_web_host": "127.0.0.1", "soundya_web_port": 8766})
        self.assertEqual(json.loads(response.body), {"ok": True})
        self.assertEqual(base(), "http://127.0.0.1:8766")
        self.assertEqual(self.saved[-1]["soundya_web_port"], 8766)

    async def test_readback(self):
        await self.put({"soundya_web_host": "localhost", "soundya_web_port": "8766"})
        response = await self.handlers["api_get_config"](object())
        self.assertEqual(json.loads(response.body)["soundya_web_port"], 8766)
        self.assertEqual(json.loads(response.body)["soundya_web_host"], "localhost")

    async def test_invalid_port_does_not_change_config(self):
        original = copy.deepcopy(self.handlers["cfg"])
        for port in (0, -1, 65536, "oops", None, True, 8766.5):
            with self.subTest(port=port):
                with self.assertRaises(HTTPException) as caught:
                    await self.put({"soundya_web_host": "localhost", "soundya_web_port": port})
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(self.handlers["cfg"], original)
        self.assertEqual(self.saved, [])

    async def test_host_cannot_contain_url_or_credentials(self):
        for host in ("", "http://localhost", "user@localhost", "localhost/path", "localhost:8766"):
            with self.subTest(host=host):
                with self.assertRaises(HTTPException):
                    await self.put({"soundya_web_host": host})
        self.assertEqual(self.saved, [])

    async def test_public_origin(self):
        await self.put({"panel_public_origin": "https://panel.c4elovek.online/"})
        self.assertEqual(self.handlers["cfg"]["panel_public_origin"], "https://panel.c4elovek.online")
        for origin in ("https://user:password@localhost", "https://localhost/path", "javascript:alert(1)", ""):
            with self.subTest(origin=origin):
                with self.assertRaises(HTTPException):
                    await self.put({"panel_public_origin": origin})

    async def test_normal_config_save_preserves_tunnel(self):
        await self.put({"soundya_web_host": "localhost", "soundya_web_port": 8766})
        await self.put({"station_ip": "192.168.0.172", "yam_port": 8099})
        self.assertEqual(self.handlers["soundya_web_base"](), "http://localhost:8766")
        self.assertEqual(self.handlers["cfg"]["station_ip"], "192.168.0.172")


if __name__ == "__main__":
    unittest.main()
