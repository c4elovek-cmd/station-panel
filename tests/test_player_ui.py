"""DOM/unit-тесты встроенного плеера (static/player.js) без сети и браузера.

Запуск:  python -m unittest discover -s tests -p test_player_ui.py

Что проверяется:
  — контракты к /api/soundya/* (state, cmd, switch, settings) и тела команд;
  — очередь рендерится по кнопке-на-трек, выбор шлёт {cmd:'index', arg:'N'};
  — ошибки сети/авторизации не сбрасывают playing (последний state живёт);
  — textContent-only: строки из «враждебного» ответа не попадают в innerHTML;
  — вкладка с результатами не открывается, alert/confirm не вызываются;
  — visibilitychange: скрытая вкладка останавливает опрос, возврат — возобновляет.
Среда: node:worker_threads с window-стабом и fetch-стабом (child процесс скрыт).
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAYER_JS = ROOT / "static" / "player.js"
PLAYER_HTML = ROOT / "static" / "player.html"
STYLE_CSS = ROOT / "static" / "style.css"

# команды, разрешённые сервером (server.py, только чтение)
ALLOWED_CMD = {"play", "pause", "resume", "toggle", "next", "prev", "stop",
               "shuffle", "repeat", "similar", "likes", "recs", "playlist",
               "status", "queue", "seek", "like", "unlike", "index"}


def run_node_file(path: Path) -> str:
    """Запуск готового JS-харнесса (tests/player_ui_harness.mjs) скрытым node."""
    proc = subprocess.run(
        ["node", str(path)],
        capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
        cwd=str(ROOT), creationflags=0x08000000,  # CREATE_NO_WINDOW: без консоли
    )
    if proc.returncode != 0:
        raise AssertionError("node harness упал:\n" + proc.stdout + proc.stderr)
    return proc.stdout


class PlayerHtmlStructure(unittest.TestCase):
    """Семантика разметки player.html — без браузера."""

    def setUp(self):
        self.html = PLAYER_HTML.read_text(encoding="utf-8")

    def test_lang_title_charset(self):
        self.assertIn('lang="ru"', self.html)
        self.assertIn("<title>", self.html)
        self.assertIn('charset="utf-8"', self.html)

    def test_no_external_sources(self):
        for banned in ("fonts.googleapis", "fonts.gstatic", "cdn.", "unpkg",
                       "jsdelivr", "https://", "http://"):
            self.assertNotIn(banned, self.html, f"внешний ресурс: {banned}")

    def test_semantic_controls_and_labels(self):
        for frag in ('type="range"', 'aria-label', '<button type="button"',
                     'aria-live="polite"', 'role="alert"', '<main', '<header',
                     'class="pl-seek"', 'class="pl-vrange"'):
            self.assertIn(frag, self.html)
        # у громкости есть видимый label
        self.assertIn('<label for="plVolume">Громкость</label>', self.html)
        # очередь — список
        self.assertIn('<ol class="pl-qlist"', self.html)

    def test_no_script_inline(self):
        # скрипт вынесен в player.js: inline-код в iframe-контексте не нужен
        self.assertNotIn("<script>", self.html.replace('<script src="/static/player.js">', ""))
        self.assertIn('/static/player.js', self.html)

    def test_uses_parent_absolute_api_paths(self):
        # шимы нет: все запросы пойдут из player.js — проверим там
        js = PLAYER_JS.read_text(encoding="utf-8")
        self.assertIn("'/api/soundya/state'", js)
        self.assertIn("'/api/soundya/cmd'", js)


class PlayerJsContract(unittest.TestCase):
    """Контракты команд и вставки текста — статически по исходнику + AST."""

    def setUp(self):
        self.js = PLAYER_JS.read_text(encoding="utf-8")

    def test_no_inner_html(self):
        self.assertNotIn("innerHTML", self.js)
        self.assertNotIn("insertAdjacentHTML", self.js)
        self.assertNotIn("document.write", self.js)

    def test_no_window_open_or_dialogs(self):
        for banned in ("window.open", "alert(", "confirm(", "prompt("):
            self.assertNotIn(banned, self.js)

    def test_fetch_has_timeout_signal(self):
        self.assertIn("timeoutSignal(", self.js)
        self.assertIn("AbortSignal.timeout", self.js)

    def test_visibility_pause_present(self):
        self.assertIn("visibilitychange", self.js)
        self.assertIn("document.hidden", self.js)

    def test_sequential_poll(self):
        # опрос строго по цепочке промисов, не параллельные таймеры
        self.assertIn("pollChain = pollChain.then(pollOnce)", self.js)

    def test_index_command_sent_as_cmd_index(self):
        self.assertIn("sendCmd('index'", self.js)

    def test_switch_contract_account_what(self):
        # сервер ждёт {account, what}, не {who, mode}
        self.assertIn("{ account: who, what: 'likes' }", self.js)
        self.assertIn("{ account: 'other', what: 'recs' }", self.js)

    def test_commands_within_server_allowlist(self):
        import re
        sent = set(re.findall(r"sendCmd\('([a-z]+)'", self.js))
        # сервер уже принимает index (см. server.py allowlist /api/soundya/cmd)
        pending = sent - ALLOWED_CMD
        self.assertEqual(pending, set(),
                         "неожиданные команды: %s" % (pending,))

    def test_volume_settings_contract(self):
        self.assertIn("/api/soundya/settings", self.js)
        self.assertIn("{ volume: v }", self.js)

    def test_state_errors_not_conflated(self):
        # сообщение прямо различает «нет связи» и «играет»
        self.assertIn("не значит", self.js)
        self.assertIn("applyNet(false", self.js)

    def test_live_region_only_on_change(self):
        self.assertIn("if (text === announced) return", self.js)


def _node_available():
    import shutil
    return shutil.which("node") is not None


class PlayerRuntime(unittest.TestCase):
    """Прогон player.js в node с DOM-стабом (харнесс tests/player_ui_harness.mjs)."""

    @unittest.skipIf(not _node_available(), "node недоступен")
    def test_dom_harness(self):
        out = run_node_file(ROOT / "tests" / "player_ui_harness.mjs")
        self.assertIn("PASS", out, out)

    def test_node_available(self):
        if not _node_available():
            self.skipTest("node не в PATH — DOM-тесты пропущены")


if __name__ == "__main__":
    unittest.main()
