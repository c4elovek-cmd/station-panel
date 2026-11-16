"""Маршрутизация собственного плеера без импорта сервера и сетевых сервисов.

Запуск: python -m unittest discover -s tests -p test_panel_player_routing.py
"""
import ast
import asyncio
import copy
import json
import re
import shlex
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


SOURCE = Path(__file__).resolve().parents[1] / "server.py"
TOKEN = "eyJ" + "a" * 30
REFRESH = "r" * 40
PANEL_STATUS = {
    "backend": "panel-player", "active_account": "other", "state": "playing",
    "accounts": {
        "me": {"has_token": True, "has_refresh": False, "login": "ok"},
        "other": {"has_token": True, "has_refresh": True, "login": "unchecked"},
    },
}


def load_handlers():
    """AST извлекает реальные функции; init_config, фоновые задачи и IPC не запускаются."""
    names = {
        "_likes_phrase_user", "_music_account_phrase", "_soundya_status_json",
        "_soundya_is_panel_player", "_soundya_switch_account", "_soundya_require_feature",
        "_soundya_post_settings", "_soundya_cmd", "_soundya_apply_token",
        "_soundya_token_loop", "api_soundya_state", "api_soundya_account",
        "api_soundya_switch", "api_soundya_settings", "api_music_soundya",
        "api_soundya_alarms", "api_soundya_alarms_post", "api_soundya_alarm_del",
        "api_soundya_pin", "api_soundya_update", "api_soundya_web",
    }
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in {"_LIKES_ME", "_LIKES_OTHER", "SOUNDYA_ACCOUNTS", "SOUNDYA_STATE_DIR"}
                for target in node.targets):
            nodes.append(node)
    namespace = {
        "asyncio": asyncio, "json": json, "re": re, "shlex": shlex,
        "Request": object, "Response": object, "JSONResponse": JSONResponse,
        "HTMLResponse": HTMLResponse, "FileResponse": FileResponse,
        "HTTPException": HTTPException, "require_auth": Mock(),
        "cfg": {"soundya_auto_firefox": True}, "STATIC_DIR": Mock(),
        "soundya_web": AsyncMock(), "ssh_run": AsyncMock(return_value=(0, "me\n", "")),
        "ssh_run_bytes": AsyncMock(return_value=(0, b"legacy\n", b"")),
        "_soundya_web_fetch": AsyncMock(return_value=(200, "text/html", b"<head></head>")),
        "SOUNDYA_PROXY_SHIM": "<script>shim</script>",
        "_firefox_soundcloud_token": Mock(return_value=TOKEN),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


def request(payload=None):
    return SimpleNamespace(json=AsyncMock(return_value=payload or {}))


def decoded(response):
    return json.loads(response.body)


class PlayerRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = load_handlers()
        self.status = copy.deepcopy(PANEL_STATUS)
        self.cmd_response = (200, "OK")
        self.settings_response = (200, "OK")

        async def web(method, path, body=None, timeout=20):
            if method == "GET" and path == "/api/status":
                return 200, json.dumps(self.status)
            if path == "/api/cmd":
                return self.cmd_response
            if path == "/api/settings":
                return self.settings_response
            raise AssertionError((method, path, body))
        self.h["soundya_web"].side_effect = web

    async def call(self, name, payload=None):
        return await self.h[name](request(payload))

    def assert_no_ssh(self):
        self.h["ssh_run"].assert_not_awaited()
        self.h["ssh_run_bytes"].assert_not_awaited()

    async def test_new_state_preserves_flags_labels_and_active(self):
        state = decoded(await self.call("api_soundya_state"))["state"]
        self.assertEqual(state["active_account"], "other")
        self.assertEqual(state["accounts"]["me"], {**PANEL_STATUS["accounts"]["me"], "label": "мой аккаунт"})
        self.assertEqual(state["accounts"]["other"]["has_refresh"], True)
        self.assertEqual(state["capabilities"], {"alarms": False, "pin": False, "external_update": False})
        self.assert_no_ssh()

    async def test_legacy_state_uses_active_file(self):
        self.status = {"state": "stopped"}
        self.h["ssh_run"].return_value = (0, "other\n", "")
        state = decoded(await self.call("api_soundya_state"))["state"]
        self.assertEqual(state["active_account"], "other")
        self.h["ssh_run"].assert_awaited_once()

    async def test_save_other_is_not_activation(self):
        for refresh_key in ("refresh", "sc_refresh"):
            with self.subTest(refresh_key=refresh_key):
                result = decoded(await self.call("api_soundya_account", {"account": "other", "token": TOKEN, refresh_key: REFRESH}))
                self.assertFalse(result["activated"])
                self.h["soundya_web"].assert_any_await("POST", "/api/settings", body={"save_account": "other", "token": TOKEN, "sc_refresh": REFRESH}, timeout=25)
        self.assert_no_ssh()

    async def test_save_omitted_refresh_does_not_clear_rotated_token(self):
        await self.call("api_soundya_account", {"account": "me", "token": TOKEN})
        self.h["soundya_web"].assert_any_await("POST", "/api/settings", body={"save_account": "me", "token": TOKEN}, timeout=25)
        self.assert_no_ssh()

    async def test_save_failure_has_no_legacy_fallback(self):
        self.settings_response = (400, "error: bad token")
        with self.assertRaises(HTTPException) as caught:
            await self.call("api_soundya_account", {"account": "other", "token": TOKEN})
        self.assertEqual(caught.exception.status_code, 502)
        self.assert_no_ssh()

    async def test_legacy_save_uses_settings_and_refresh_file(self):
        self.status = {"state": "stopped"}
        await self.call("api_soundya_account", {"account": "me", "token": TOKEN, "refresh": REFRESH})
        self.h["soundya_web"].assert_any_await("POST", "/api/settings", body={"token": TOKEN, "sc_refresh": REFRESH}, timeout=25)
        self.assertIn("/data/opt/soundya/refresh_token", self.h["ssh_run_bytes"].await_args.args[0])

    async def test_new_switch_likes_and_wave_are_distinct_http_commands(self):
        for who in ("me", "other"):
            for what in ("likes", "recs"):
                with self.subTest(who=who, what=what):
                    result = decoded(await self.call("api_soundya_switch", {"account": who, "what": what}))
                    self.assertTrue(result["ok"])
                    self.h["soundya_web"].assert_any_await("POST", "/api/cmd", body=f"account {who} {what}", timeout=90.0)
        self.assert_no_ssh()

    async def test_new_switch_failure_does_not_run_shell(self):
        self.cmd_response = (503, "busy")
        self.assertFalse(decoded(await self.call("api_soundya_switch", {"account": "other", "what": "recs"}))["ok"])
        self.assert_no_ssh()

    async def test_legacy_switch_still_calls_script(self):
        self.status = {"backend": "soundya"}
        await self.call("api_soundya_switch", {"account": "other", "what": "recs"})
        self.h["ssh_run_bytes"].assert_awaited_once_with("/data/opt/soundya/likes_as.sh other recs", timeout=90.0)

    async def test_voice_legacy_likes_keeps_old_script_arguments(self):
        self.status = {"backend": "soundya"}
        await self.h["_soundya_switch_account"]("me", "likes", timeout=60, legacy_likes_only=True)
        self.h["ssh_run_bytes"].assert_awaited_once_with("/data/opt/soundya/likes_as.sh me", timeout=60)

    async def test_invalid_account_and_source_rejected(self):
        for payload in ({"account": "bad"}, {"account": "me", "what": "wave"}, {"account": "other", "what": "likes\nstop"}):
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as caught:
                await self.call("api_soundya_switch", payload)
            self.assertEqual(caught.exception.status_code, 400)
        self.h["soundya_web"].assert_not_awaited()
        self.assert_no_ssh()

    async def test_settings_activate_volume_proxy_without_restart(self):
        patch = {"account": "me", "volume": 42, "proxy": "socks5://127.0.0.1:1081"}
        self.assertTrue(decoded(await self.call("api_soundya_settings", patch))["ok"])
        self.h["soundya_web"].assert_any_await("POST", "/api/settings", body=patch, timeout=25)
        self.assert_no_ssh()

    async def test_settings_validation(self):
        for patch in ({"volume": True}, {"volume": 101}, {"volume": "42"}, {"account": []}, {"proxy": "file:///etc/passwd"}, {"token": TOKEN}, {}):
            with self.subTest(patch=patch), self.assertRaises(HTTPException) as caught:
                await self.call("api_soundya_settings", patch)
            self.assertEqual(caught.exception.status_code, 400)
        self.h["soundya_web"].assert_not_awaited()

    async def test_new_update_check_does_not_contact_external_updater(self):
        result = decoded(await self.call("api_soundya_update", {"mode": "check"}))
        self.assertFalse(result["available"])
        self.assertEqual(result["backend"], "panel-player")
        self.assertIn("вместе с панелью", result["output"])
        self.assertEqual(self.h["soundya_web"].await_count, 1)
        self.assert_no_ssh()

    async def test_new_external_install_forbidden(self):
        with self.assertRaises(HTTPException) as caught:
            await self.call("api_soundya_update", {"mode": "install"})
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.h["soundya_web"].await_count, 1)

    async def test_legacy_updater_remains_available(self):
        self.status = {"backend": "soundya"}
        for mode, timeout in (("check", 60), ("install", 600)):
            await self.call("api_soundya_update", {"mode": mode})
            self.h["soundya_web"].assert_any_await("POST", "/api/cmd", body=f"update {mode}", timeout=timeout)

    async def test_legacy_alarm_and_pin_keep_command_api(self):
        self.status = {"backend": "soundya"}
        self.cmd_response = (200, '{"alarms":[]}')
        self.assertTrue(decoded(await self.call("api_soundya_alarms"))["ok"])
        self.h["soundya_web"].assert_any_await("POST", "/api/cmd", body="alarm json", timeout=30.0)
        self.cmd_response = (200, "OK")
        await self.call("api_soundya_pin", {"pin": "1234"})
        self.h["soundya_web"].assert_any_await("POST", "/api/cmd", body="pin set 1234", timeout=30.0)

    async def test_new_alarm_and_pin_explicitly_unsupported(self):
        for handler in ("api_soundya_alarms", "api_soundya_alarms_post", "api_soundya_alarm_del", "api_soundya_pin"):
            with self.subTest(handler=handler), self.assertRaises(HTTPException) as caught:
                await self.call(handler)
            self.assertEqual(caught.exception.status_code, 501)
        self.assertTrue(all(call.args[0] == "GET" for call in self.h["soundya_web"].await_args_list))
        self.assert_no_ssh()

    async def test_web_serves_own_file_without_legacy_fetch(self):
        page = Path("G:/panel-fixture/static/player.html")
        self.h["STATIC_DIR"].__truediv__ = Mock(return_value=Mock(is_file=Mock(return_value=True)))
        # FileResponse не читает файл до отправки ASGI; подставляем фабрику.
        self.h["FileResponse"] = Mock(return_value=page)
        result = await self.call("api_soundya_web")
        self.assertEqual(result, page)
        self.h["FileResponse"].assert_called_once()
        self.h["_soundya_web_fetch"].assert_not_awaited()

    async def test_missing_own_page_is_explicit_unavailable(self):
        self.h["STATIC_DIR"].__truediv__ = Mock(return_value=Mock(is_file=Mock(return_value=False)))
        response = await self.call("api_soundya_web")
        self.assertEqual(response.status_code, 503)
        self.h["_soundya_web_fetch"].assert_not_awaited()

    async def test_legacy_web_keeps_proxy_shim(self):
        self.status = {"state": "stopped"}
        response = await self.call("api_soundya_web")
        self.assertIn(b"<head><script>shim</script>", response.body)
        self.h["_soundya_web_fetch"].assert_awaited_once_with("/")

    async def test_index_forwards_zero_based_queue_number_via_web(self):
        for backend in ("panel-player", "soundya"):
            self.status = {"backend": backend}
            for number in ("0", "12"):
                with self.subTest(backend=backend, number=number):
                    result = decoded(await self.call("api_music_soundya", {"cmd": "index", "arg": number}))
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["via"], "web")
                    self.h["soundya_web"].assert_any_await("POST", "/api/cmd", body=f"index {number}", timeout=30.0)
        self.assert_no_ssh()

    async def test_index_rejects_missing_or_invalid_number_before_routing(self):
        payloads = [{"cmd": "index"}] + [
            {"cmd": "index", "arg": value}
            for value in ("", " ", "-1", "1.5", "next", "1 2", "1\nstop", "0; stop", "１２", None, True, [], {})
        ]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as caught:
                await self.call("api_music_soundya", payload)
            self.assertEqual(caught.exception.status_code, 400)
        self.h["soundya_web"].assert_not_awaited()
        self.assert_no_ssh()

    async def test_index_daemon_error_stays_on_web(self):
        self.cmd_response = (200, "error: index — номер трека очереди")
        result = decoded(await self.call("api_music_soundya", {"cmd": "index", "arg": "12"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["via"], "web")
        self.assertIn("номер трека очереди", result["output"])
        self.assert_no_ssh()

    async def test_index_failure_has_no_panel_player_ssh_fallback(self):
        self.cmd_response = (503, "busy")
        response = await self.call("api_music_soundya", {"cmd": "index", "arg": "0"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(decoded(response)["via"], "web")
        self.assertFalse(decoded(response)["ok"])
        self.assert_no_ssh()

    async def test_legacy_index_fallback_keeps_queue_number(self):
        self.status = {"backend": "soundya"}
        self.cmd_response = (503, "busy")
        result = decoded(await self.call("api_music_soundya", {"cmd": "index", "arg": "12"}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "ssh")
        self.h["ssh_run_bytes"].assert_awaited_once_with("soundya index 12 2>&1", timeout=90)

    async def test_music_command_error_has_no_new_backend_ssh_fallback(self):
        self.cmd_response = (503, "busy")
        result = await self.call("api_music_soundya", {"cmd": "pause"})
        self.assertEqual(result.status_code, 502)
        self.assertFalse(decoded(result)["ok"])
        self.assert_no_ssh()

    async def test_legacy_music_fallback_remains(self):
        self.status = {"state": "stopped"}
        self.cmd_response = (503, "busy")
        result = decoded(await self.call("api_music_soundya", {"cmd": "pause"}))
        self.assertEqual(result["via"], "ssh")
        self.h["ssh_run_bytes"].assert_awaited_once_with("soundya pause 2>&1", timeout=90)

    async def test_token_apply_is_safe_for_active_other(self):
        ok, _ = await self.h["_soundya_apply_token"](TOKEN)
        self.assertTrue(ok)
        self.h["soundya_web"].assert_any_await("POST", "/api/settings", body={"save_account": "me", "token": TOKEN}, timeout=25)
        self.assert_no_ssh()

    async def test_token_apply_failure_does_not_restart_new_backend(self):
        self.settings_response = (503, "busy")
        ok, _ = await self.h["_soundya_apply_token"](TOKEN)
        self.assertFalse(ok)
        self.assert_no_ssh()

    async def test_firefox_loop_uses_new_status_and_does_not_touch_other(self):
        self.h["asyncio"] = SimpleNamespace(to_thread=AsyncMock(return_value=TOKEN), sleep=AsyncMock(side_effect=asyncio.CancelledError))
        with self.assertRaises(asyncio.CancelledError):
            await self.h["_soundya_token_loop"]()
        self.h["asyncio"].to_thread.assert_not_awaited()
        self.assert_no_ssh()

    async def test_firefox_loop_saves_me_without_activation(self):
        self.status["active_account"] = "me"
        self.h["asyncio"] = SimpleNamespace(to_thread=AsyncMock(return_value=TOKEN), sleep=AsyncMock(side_effect=asyncio.CancelledError))
        with self.assertRaises(asyncio.CancelledError):
            await self.h["_soundya_token_loop"]()
        self.h["soundya_web"].assert_any_await("POST", "/api/settings", body={"save_account": "me", "token": TOKEN}, timeout=25)
        self.assert_no_ssh()

    async def test_backend_detection_requires_exact_live_marker(self):
        for reply, expected in ((json.dumps(PANEL_STATUS), True), ('{"backend":"soundya"}', False), ('{"state":"playing"}', False), ('[]', False), ('not json', False)):
            with self.subTest(reply=reply):
                self.h["soundya_web"].side_effect = None
                self.h["soundya_web"].return_value = (200, reply)
                self.assertEqual(await self.h["_soundya_is_panel_player"](), expected)

    def test_voice_phrase_accounts_and_real_recommendations(self):
        phrases = {
            "включи мои лайки": ("me", "likes"), "Мои лайки": ("me", "likes"),
            "включи любимое": ("other", "likes"), "любимое": ("other", "likes"),
            "включи мои любимые треки": ("other", "likes"),
            "моя волна": ("me", "recs"), "включи мою волну": ("me", "recs"),
            "волна брата": ("other", "recs"), "включи волну брата": ("other", "recs"),
            "любимая волна": ("other", "recs"), "включи любимую волну": ("other", "recs"),
            "это мои любимые песни": None, "пауза": None,
        }
        for phrase, expected in phrases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(self.h["_music_account_phrase"](phrase), expected)

    def test_command_alias_and_listener_channel_contract(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        command = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_music_soundya")
        routes = {node.args[0].value for node in command.decorator_list}
        self.assertEqual(routes, {"/api/soundya/cmd", "/api/music/soundya"})
        listener = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ipc_media_listener")
        channel_calls = [node for node in ast.walk(listener) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "open_connection"]
        self.assertEqual(len(channel_calls), 1)
        self.assertEqual([arg.value for arg in channel_calls[0].args], ["127.0.0.1", 9875])
        switch_calls = [node for node in ast.walk(listener) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_soundya_switch_account"]
        self.assertEqual(len(switch_calls), 1)


if __name__ == "__main__":
    unittest.main()
