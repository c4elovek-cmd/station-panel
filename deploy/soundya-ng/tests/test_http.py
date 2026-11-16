"""Проверки HTTP-контракта собственного плеера без токенов, SoundCloud и звука.

PANEL_TEST_URL указывает на изолированный процесс, обычно порт 18765.
"""
import concurrent.futures
import json
import os
import socket
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlparse

BASE = os.environ.get("PANEL_TEST_URL", "http://127.0.0.1:18765").rstrip("/")
OPENER = build_opener(ProxyHandler({}))


def call(path, body=None, origin=None):
    headers = {"Content-Type": "text/plain"}
    if origin is not None:
        headers["Origin"] = origin
    req = Request(BASE + path, data=body.encode() if body is not None else None, headers=headers)
    try:
        with OPENER.open(req, timeout=3) as response:
            return response.status, response.read().decode()
    except HTTPError as exc:
        return exc.code, exc.read().decode()


@unittest.skipUnless(os.environ.get("PANEL_TEST_URL"), "Нужен отдельный тестовый демон")
class HttpContract(unittest.TestCase):
    def test_status_without_secrets(self):
        code, raw = call("/api/status")
        self.assertEqual(code, 200)
        status = json.loads(raw)
        self.assertEqual(status["backend"], "panel-player")
        self.assertIn(status["state"], ("stopped", "loading", "playing", "paused", "error"))
        self.assertIn("accounts", status)
        for account in status["accounts"].values():
            self.assertNotIn("token", account)
            self.assertNotIn("refresh", account)

    def test_commands_and_bad_requests(self):
        for cmd in ("stop", "pause", "shuffle on", "repeat off"):
            self.assertTrue(call("/api/cmd", cmd)[1].startswith("OK"))
        for cmd in ("not-a-command", "seek nan", "seek 1e999", "seek +1junk", "index 0", "index -1", "index x", "shuffle maybe", "repeat loud", "account invalid", "update install"):
            self.assertTrue(call("/api/cmd", cmd)[1].startswith("error"), cmd)
        self.assertEqual(call("/not-found")[0], 404)

    def test_settings_validation_and_volume(self):
        self.assertEqual(call("/api/settings", '{"volume":35}')[0], 200)
        self.assertEqual(json.loads(call("/api/status")[1])["volume"], 35)
        for raw in ('{', '{"volume":101}', '{"volume":"35"}', '{"volume":null}', '{"account":"bad"}', '{"token":null}', '{"token":"x\\u0000y"}'):
            self.assertEqual(call("/api/settings", raw)[0], 400, raw)
        self.assertEqual(json.loads(call("/api/status")[1])["volume"], 35)
        self.assertEqual(call("/api/cmd", "stop", origin="https://example.invalid")[0], 403)

    def test_parallel_status(self):
        start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: call("/api/status"), range(16)))
        self.assertTrue(all(code == 200 for code, _ in results))
        self.assertLess(time.monotonic() - start, 3)

    def test_slow_client_does_not_block_pause(self):
        url = urlparse(BASE)
        with socket.create_connection((url.hostname, url.port), timeout=2) as slow:
            slow.sendall(b"POST /api/cmd HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\np")
            start = time.monotonic()
            self.assertEqual(call("/api/cmd", "stop"), (200, "OK"))
            self.assertLess(time.monotonic() - start, 1)

    def test_duplicate_length_rejected(self):
        url = urlparse(BASE)
        with socket.create_connection((url.hostname, url.port), timeout=2) as conn:
            conn.sendall(b"POST /api/cmd HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\nContent-Length: 4\r\n\r\nstop")
            self.assertIn(b"400", conn.recv(4096))


if __name__ == "__main__":
    unittest.main()
