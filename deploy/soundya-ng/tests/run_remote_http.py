"""Изолированный прогон HTTP на колонке; рабочие сервисы не меняются."""
import base64
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SSH = ['ssh', '-o', 'ConnectTimeout=8', '-o', 'BatchMode=yes', '-p', '2222', 'root@192.168.0.172']
HIDDEN = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def remote(command, data=None):
    result = subprocess.run(SSH + [command], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, **HIDDEN)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace') + result.stdout.decode('utf-8', 'replace'))
    return result.stdout.decode('utf-8', 'replace')


binary = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'build' / 'panel-player'
# Новый каталог каждый раз: без перезаписи неизвестных файлов на устройстве.
work = remote('mktemp -d /tmp/panel-player-test.XXXXXX').strip()
tunnel = None
try:
    remote(f'base64 -d > {work}/player && chmod 700 {work}/player', base64.b64encode(binary.read_bytes()))
    remote(f'PANEL_PLAYER_DIR={work} PANEL_PLAYER_PORT=18765 setsid {work}/player >{work}/log 2>&1 </dev/null & echo $! > {work}/pid')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    tunnel = subprocess.Popen(SSH + ['-N', '-o', 'ExitOnForwardFailure=yes', '-L', f'127.0.0.1:{port}:127.0.0.1:18765'], **HIDDEN)
    # Готовность проверяется запросом к локальному API, не паузами и не polling задачи агента.
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(200):
        try:
            with opener.open(f'http://127.0.0.1:{port}/health', timeout=0.2) as response:
                response.read()
            break
        except (OSError, urllib.error.URLError):
            if tunnel.poll() is not None:
                raise RuntimeError('SSH-туннель завершился')
    else:
        raise RuntimeError('Тестовый HTTP недоступен')
    env = dict(os.environ, PANEL_TEST_URL=f'http://127.0.0.1:{port}')
    result = subprocess.run([sys.executable, str(ROOT / 'tests' / 'test_http.py'), '-v'], env=env, capture_output=True, **HIDDEN)
    print(result.stdout.decode('utf-8', 'replace'))
    print(result.stderr.decode('utf-8', 'replace'))
    print(remote(f'cat {work}/log'))
    code = result.returncode
finally:
    if tunnel is not None:
        tunnel.terminate()
        tunnel.wait(timeout=10)
    # Удаление ограничено каталогом, который только что создал этот скрипт.
    remote(f'if [ -s {work}/pid ]; then kill -TERM "$(cat {work}/pid)" 2>/dev/null || true; fi')
    # Каталог/лог остаются для диагностики: никаких удаления пользовательских данных.
    print('Тестовый каталог:', work)
sys.exit(code)
