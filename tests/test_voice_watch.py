"""Проверки реального voice_watch.sh: curl всегда подменён локальным shell-моком.

Запуск: python -B -m unittest discover -s tests -p test_voice_watch.py -v
VOICE_TEST_SHELL задаёт путь к sh; VOICE_TEST_BUSYBOX — путь к BusyBox.
Рабочие сервисы, maind.log и SSH не используются. Дочерние процессы Windows скрыты.
"""
import datetime as dt
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "soundya-ng" / "voice_watch.sh"
if not SCRIPT.exists():
    SCRIPT = Path(__file__).resolve().parents[1] / "voice_watch.sh"
HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def msys_path(shell_exe):
    """PATH с /usr/bin Git: без него msys-sh не находит date и проверки молча падают."""
    exe = Path(shell_exe).resolve()
    # G:\...\Git\usr\bin\sh.exe -> G:\...\Git\usr\bin
    usr = exe.parent
    if usr.name != "bin" or usr.parent.name != "usr":
        usr = exe.parent.parent / "usr" / "bin"
    if usr.exists():
        return str(usr) + os.pathsep + os.environ.get("PATH", "")
    return os.environ.get("PATH", "")


# 2026-11-17 13:01:07 UTC — фиксированная эпоха (не зависит от локального date).
NOW = 1794920467


def shell_command():
    if os.environ.get("VOICE_TEST_BUSYBOX"):
        return [os.environ["VOICE_TEST_BUSYBOX"], "sh"]
    if os.environ.get("VOICE_TEST_SHELL"):
        return [os.environ["VOICE_TEST_SHELL"]]
    if os.name != "nt":
        return [shutil.which("sh") or "/bin/sh"]
    for path in (r"G:\Program Files\Git\usr\bin\sh.exe",
                 r"G:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\sh.exe", shutil.which("sh")):
        if path and Path(path).exists():
            return [str(path)]
    raise unittest.SkipTest("нет sh; задайте VOICE_TEST_SHELL или VOICE_TEST_BUSYBOX")


def event(phrase="Стоп", delta=0, milliseconds=482, state="BUSY", request="14251:14279"):
    stamp = dt.datetime.fromtimestamp(NOW + delta, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return (f"[{request}] [{stamp}.{milliseconds:03d}] [sdk] [info] "
            f"[alice_state_machine.cpp:414] onAliceStateChanged: {state}, "
            f"recognizedPhrase='{phrase}'")


class VoiceWatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = shell_command()

    def setUp(self):
        # На Windows артефакты проверки по умолчанию на G:, на Linux — изолированный /tmp.
        self.temp = tempfile.TemporaryDirectory(
            prefix="voice-watch-test-", dir=str(SCRIPT.parent) if os.name == "nt" else None)
        self.work = Path(self.temp.name)
        self.mock = self.work / "curl-mock.sh"
        self.arguments = self.work / "arguments"
        self.commands = self.work / "commands"
        self.mock.write_text(
            '#!/bin/sh\n'
            'printf "call\\n" >> "$MOCK_ARGUMENTS"\n'
            'for arg do printf "%s\\n" "$arg" >> "$MOCK_ARGUMENTS"; done\n'
            'while [ "$#" -gt 0 ]; do\n'
            '    if [ "$1" = --data-binary ]; then\n'
            '        shift; printf "%s\\n" "$1" >> "$MOCK_COMMANDS"\n'
            '    fi\n'
            '    shift\n'
            'done\n'
            'printf "%s\\n" "$MOCK_RESPONSE"\n'
            '[ -n "$MOCK_STDERR" ] && printf "%s\\n" "$MOCK_STDERR" >&2\n'
            'exit "$MOCK_EXIT"\n', encoding="utf-8", newline="\n")
        self.mock.chmod(0o700)

    def tearDown(self):
        self.temp.cleanup()

    def run_lines(self, lines, *, now=NOW, response="OK", exit_code=0,
                  stderr="", single=False):
        for path in (self.commands, self.arguments):
            if path.exists():
                path.unlink()
        env = dict(os.environ, VOICE_WATCH_NOW=str(now),
                   VOICE_WATCH_LOG=(self.work / "no-maind.log").as_posix(),
                   VOICE_WATCH_LOCK=(self.work / "no-lock").as_posix(),
                   VOICE_WATCH_CURL=self.mock.as_posix(),
                   MOCK_COMMANDS=self.commands.as_posix(),
                   MOCK_ARGUMENTS=self.arguments.as_posix(),
                   MOCK_RESPONSE=response, MOCK_EXIT=str(exit_code), MOCK_STDERR=stderr)
        if os.name == "nt":
            # msys-sh ищет date/tr/tail по PATH: без Git usr/bin проверки тихо ломаются.
            env["PATH"] = msys_path(self.shell[0])
        args = ["--handle-line", lines[0]] if single else ["--handle-lines"]
        # text=True на Windows превращает LF в CRLF. Журнал Linux содержит LF:
        # передаём точные байты, включая завершающий перевод строки.
        payload = b"" if single else ("\n".join(lines) + "\n").encode("utf-8")
        result = subprocess.run(self.shell + [SCRIPT.as_posix()] + args,
                                input=payload, capture_output=True, env=env,
                                timeout=30, **HIDDEN)
        stdout = result.stdout.decode("utf-8")
        stderr_text = result.stderr.decode("utf-8")
        self.assertEqual(result.returncode, 0, stderr_text)
        self.assertEqual(stderr_text, "", "ошибки curl не должны содержать ответ/речь")
        self.assertFalse((self.work / "no-lock").exists(), "тестовый режим не создаёт lock")
        self.assertFalse((self.work / "no-maind.log").exists())
        commands = self.commands.read_text(encoding="utf-8").splitlines() if self.commands.exists() else []
        return commands, stdout

    def test_handle_line_exact_busy_format(self):
        commands, out = self.run_lines([event()], single=True)
        self.assertEqual(commands, ["stop"])
        self.assertIn("stop: OK", out)
        self.assertNotIn("Стоп", out)

    def test_all_account_phrases_both_initial_cases(self):
        phrases = {
            "включи любимое": "account other likes",
            "любимое": "account other likes",
            "включи мои любимые треки": "account other likes",
            "мои лайки": "account me likes",
            "включи мои лайки": "account me likes",
            "моя волна": "account me recs",
            "включи мою волну": "account me recs",
            "любимая волна": "account other recs",
            "волна брата": "account other recs",
            "включи волну брата": "account other recs",
            "волна": "recs", "включи волну": "recs",
        }
        for phrase, command in phrases.items():
            for variant in (phrase, phrase[0].upper() + phrase[1:]):
                with self.subTest(phrase=variant):
                    commands, out = self.run_lines([event(variant)], single=True)
                    self.assertEqual(commands, [command])
                    self.assertNotIn(variant, out)

    def test_fixed_commands_and_resume_is_not_search(self):
        for phrase, command in (("Пауза", "pause"), ("Стоп", "stop"),
                                ("Продолжай", "resume"), ("Следующий трек", "next"),
                                ("Дальше", "next"), ("Предыдущий", "prev"), ("Назад", "prev"),
                                ("pause", "pause"), ("stop", "stop"), ("resume", "resume"),
                                ("next", "next"), ("prev", "prev")):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.run_lines([event(phrase)], single=True)[0], [command])

    def test_explicit_search_and_scorpions_alias(self):
        for phrase, command in (("Включи трек scorpions", "play scorpions"),
                                ("включи песню wind of change", "play wind of change"),
                                ("Включи музыку русский рок", "play русский рок"),
                                ("Включи scorpions", "play scorpions"),
                                ("включи Scorpions", "play scorpions")):
            with self.subTest(phrase=phrase):
                commands, out = self.run_lines([event(phrase)], single=True)
                self.assertEqual(commands, [command])
                self.assertIn("search: OK", out)
                self.assertNotIn(command, out)

    def test_foreign_services_and_non_music_are_not_intercepted(self):
        for phrase in ("Включи свет", "Поставь будильник", "Включи радио", "Включи радио maximum",
                       "Включи youtube", "Включи Яндекс", "Включи музыку на ютубе",
                       "Включи трек на YouTube", "Включи песню на Яндекс Музыке",
                       "Включи волну на ютубе", "Продолжай разговор", "Стоп слова",
                       "Включи", "Включи музыку", "Включи песню   ", "Включи трек ",
                       "Включи metallica", "play", "Мои лайки и выключи свет"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.run_lines([event(phrase)], single=True), ([], ""))

    def test_non_busy_states_even_with_busy_in_phrase(self):
        for state in ("LISTENING", "SPEAKING", "IDLE", "NOT_BUSY", "BUSY_OTHER"):
            with self.subTest(state=state):
                self.assertEqual(self.run_lines([event(state=state)], single=True), ([], ""))

    def test_wrong_source_and_malformed_records_are_ignored(self):
        for line in (event().replace("alice_state_machine.cpp", "another.cpp"),
                     event().replace("[sdk]", "[other]"), event()[:-1],
                     "onAliceStateChanged: BUSY, recognizedPhrase='Стоп'",
                     event().replace("14251:14279", "not-a-pid"),
                     event().replace("2026-11-17", "bad-date"), "", "обычная речь"):
            with self.subTest(line=line):
                self.assertEqual(self.run_lines([line], single=True), ([], ""))

    def test_freshness_inclusive_boundaries(self):
        for delta, expected in ((-6, []), (-5, ["stop"]), (-2, ["stop"]),
                                (0, ["stop"]), (2, ["stop"]), (3, [])):
            with self.subTest(delta=delta):
                self.assertEqual(self.run_lines([event(delta=delta)], single=True)[0], expected)

    def test_rotation_old_file_is_ignored(self):
        self.assertEqual(self.run_lines([event(delta=-3600), event(delta=-86400)]), ([], ""))

    def test_dedup_timestamp_request_survives_interleaved_command(self):
        commands, _ = self.run_lines([event(), event("Пауза", milliseconds=600), event()])
        self.assertEqual(commands, ["stop", "pause"])

    def test_same_timestamp_request_is_one_event_even_if_phrase_differs(self):
        self.assertEqual(self.run_lines([event(), event("Пауза")])[0], ["stop"])

    def test_different_requests_same_timestamp_are_distinct(self):
        self.assertEqual(self.run_lines([event(), event("Пауза", request="14251:14280")])[0],
                         ["stop", "pause"])

    def test_command_debounce_through_another_command(self):
        lines = [event(), event("Пауза", milliseconds=600), event(delta=1)]
        self.assertEqual(self.run_lines(lines)[0], ["stop", "pause"])

    def test_command_debounce_millisecond_boundary(self):
        for delta, fraction, expected in ((2, 482, ["stop"]), (2, 483, ["stop", "stop"]),
                                          (2, 1, ["stop"]), (3, 482, ["stop", "stop"])):
            with self.subTest(delta=delta, fraction=fraction):
                lines = [event(), event(delta=delta, milliseconds=fraction)]
                self.assertEqual(self.run_lines(lines, now=NOW + 3)[0], expected)

    def test_debounce_aliases_share_same_command(self):
        lines = [event("Включи любимое"), event("Любимое", delta=1)]
        self.assertEqual(self.run_lines(lines)[0], ["account other likes"])

    def test_different_search_queries_not_debounced(self):
        lines = [event("Включи трек one"), event("Включи трек two", delta=1)]
        self.assertEqual(self.run_lines(lines)[0], ["play one", "play two"])

    def test_http_failure_suppresses_output_and_retry(self):
        commands, out = self.run_lines([event(), event(delta=1)], response="секретный ответ",
                                       stderr="секретная диагностика", exit_code=22)
        self.assertEqual(commands, ["stop"])
        self.assertIn("stop: HTTP-ошибка", out)
        self.assertNotIn("секрет", out)

    def test_ok_prefix_only_and_user_response_privacy(self):
        for response, confirmed in (("OK", True), ("OK queued", True),
                                    ("OK account other", True), ("error: мой секрет", False),
                                    ("not OK", False), ("", False)):
            with self.subTest(response=response):
                _, out = self.run_lines([event()], response=response, single=True)
                self.assertEqual("stop: OK" in out, confirmed)
                self.assertNotIn("секрет", out)

    def test_transport_exact_binary_body_and_noproxy(self):
        self.run_lines([event("Включи мои лайки")], single=True)
        args = self.arguments.read_text(encoding="utf-8").splitlines()
        self.assertEqual(args, ["call", "--noproxy", "*", "-fsS", "--connect-timeout", "1",
                                "--max-time", "3", "-H", "Content-Type: text/plain",
                                "--data-binary", "account me likes", "http://127.0.0.1:8765/api/cmd"])

    def test_search_is_literal_no_shell_execution(self):
        marker = self.work / "must-not-exist"
        query = f"$(touch {marker.as_posix()}); `false` & echo secret"
        commands, out = self.run_lines([event("Включи трек " + query)], single=True)
        self.assertEqual(commands, ["play " + query])
        self.assertFalse(marker.exists())
        self.assertNotIn(query, out)

    def test_search_apostrophes_and_backslashes_are_preserved(self):
        query = r"Guns N' Roses a\b"
        lines = [event("Включи трек " + query), event("Включи трек " + query, delta=1)]
        self.assertEqual(self.run_lines(lines)[0], ["play " + query])

    def test_syntax(self):
        result = subprocess.run(self.shell + ["-n", SCRIPT.as_posix()], capture_output=True,
                                encoding="utf-8", timeout=10, **HIDDEN)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
