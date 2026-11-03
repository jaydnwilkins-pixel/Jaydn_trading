"""run_python jail (review item C2): the isolation must hold against code that
deliberately tries to escape, not just against polite code."""
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.tools import sandbox
from app.tools.sandbox import UNAVAILABLE, run_python

REPO = Path(__file__).resolve().parent.parent
SECRET_PATHS = [REPO / "etc" / "claire.env", REPO / "var" / "desk.db",
                Path("/home/richard/claire_trading/etc/claire.env"),
                Path("/home/richard/claire_trading/var/desk.db"),
                Path.home() / ".claude", Path.home()]


def ok(out):
    assert "result" in out, out
    return out["result"]


def test_pandas_calc_still_works():
    r = ok(run_python(
        "import numpy as np, pandas as pd\n"
        "df = pd.DataFrame(DATA)\n"
        "df['sma3'] = df['close'].rolling(3).mean()\n"
        "print('debug output is fine')\n"
        "RESULT = {'sma3': float(df['sma3'].iloc[-1]),"
        " 'std': float(np.std(df['close']))}\n",
        {"close": [10, 11, 12, 13, 14], "volume": [1, 2, 3, 4, 5]}))
    assert r["sma3"] == 13.0
    assert r["std"] == pytest.approx(1.41421356)


@pytest.fixture
def host_listener():
    """A TCP server on the HOST loopback (like claire-api on :7788)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    hits = []
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                hits.append(1)
                c.close()
            except OSError:
                pass
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield srv.getsockname()[1], hits
    stop.set()
    t.join()
    srv.close()


def test_network_blocked_every_way(host_listener):
    port, hits = host_listener
    code = f"""
import importlib, subprocess, socket, _socket
res = {{}}
def attempt(name, fn):
    try:
        fn(); res[name] = 'OPEN'
    except Exception as e:
        res[name] = 'blocked'
def raw():
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.settimeout(2); s.connect(('127.0.0.1', {port}))
def reloaded():
    importlib.reload(socket)
    socket.create_connection(('127.0.0.1', {port}), timeout=2)
def internet():
    s = _socket.socket(); s.settimeout(2); s.connect(('1.1.1.1', 443))
def dns():
    socket.getaddrinfo('example.com', 443)
attempt('raw_socket_host_loopback', raw)
attempt('reloaded_socket', reloaded)
attempt('internet', internet)
attempt('dns', dns)
for url in ('http://127.0.0.1:{port}/', 'https://1.1.1.1/'):
    p = subprocess.run(['curl', '-sS', '-m', '3', url], capture_output=True)
    res['curl ' + url] = 'OPEN' if p.returncode == 0 else 'blocked'
RESULT = res
"""
    r = ok(run_python(code, {}, timeout=30))
    assert r and all(v == "blocked" for v in r.values()), r
    time.sleep(0.3)
    assert hits == []                       # nothing reached the host port


def test_cannot_read_secrets_or_home():
    paths = [str(p) for p in SECRET_PATHS]
    r = ok(run_python(
        "import os\n"
        "out = {}\n"
        "for p in DATA['paths']:\n"
        "    try:\n"
        "        open(p, 'rb').read(1); out[p] = 'READ'\n"
        "    except Exception as e:\n"
        "        out[p] = 'denied' if not os.path.exists(p) else 'EXISTS'\n"
        "out['/proc/1/environ has secret'] = 'CLAIRE' in open('/proc/self/environ').read()\n"
        "out['env'] = sorted(os.environ)\n"
        "RESULT = out\n",
        {"paths": paths}))
    for p in paths:
        assert r[p] == "denied", (p, r[p])
    assert r["/proc/1/environ has secret"] is False
    assert not any("CLAIRE" in k or "KEY" in k or "SECRET" in k
                   for k in r["env"])


def test_env_scrubbed(monkeypatch):
    monkeypatch.setenv("CLAIRE_INTERNAL_SECRET", "supersecret")
    r = ok(run_python(
        "import os\nRESULT = 'supersecret' in repr(dict(os.environ))", {}))
    assert r is False


def test_cannot_write_outside_work_dir(tmp_path):
    canary = tmp_path / "escaped.txt"          # a host path (pytest tmp)
    targets = ["/usr/escaped", "/venv/escaped", "/etc/escaped", "/escaped",
               str(canary), str(REPO / "var" / "escaped"),
               str(Path.home() / "escaped")]
    r = ok(run_python(
        "out = {}\n"
        "for p in DATA['targets']:\n"
        "    try:\n"
        "        open(p, 'w').write('x'); out[p] = 'WROTE'\n"
        "    except Exception:\n"
        "        out[p] = 'denied'\n"
        "open('/work/scratch.csv', 'w').write('a,b\\n1,2\\n')\n"
        "import pandas as pd\n"
        "out['work'] = int(pd.read_csv('/work/scratch.csv')['b'][0])\n"
        "RESULT = out\n", {"targets": targets}))
    assert r.pop("work") == 2
    assert set(r.values()) == {"denied"}, r
    assert not canary.exists()
    assert not (REPO / "var" / "escaped").exists()
    assert not (Path.home() / "escaped").exists()


def _alive(marker):
    p = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return p.stdout.split()


def test_timeout_kills_children_and_grandchildren():
    marker = "4242.4242"
    code = f"""
import os, subprocess
subprocess.Popen(['sleep', '{marker}'])
subprocess.Popen(['sleep', '{marker}'], start_new_session=True)
if os.fork() == 0:              # double-fork daemon, reparented away
    os.setsid()
    if os.fork() == 0:
        os.execvp('sleep', ['sleep', '{marker}'])
    os._exit(0)
while True:
    pass
"""
    assert _alive(marker) == []
    t0 = time.time()
    out = run_python(code, {}, timeout=2)
    assert "timed out" in out.get("error", ""), out
    assert time.time() - t0 < 10
    deadline = time.time() + 3
    while _alive(marker) and time.time() < deadline:
        time.sleep(0.1)
    assert _alive(marker) == []


def test_process_limit_stops_fork_bomb():
    r = ok(run_python(
        "import subprocess\n"
        "n = 0\n"
        "try:\n"
        "    for _ in range(500):\n"
        "        subprocess.Popen(['sleep', '30']); n += 1\n"
        "except Exception as e:\n"
        "    pass\n"
        "RESULT = n\n", {}, timeout=20))
    assert 10 < r < 100          # per-namespace RLIMIT_NPROC, not host-wide


def test_memory_limit():
    out = run_python("x = bytearray(3 * 1024**3)\nRESULT = len(x)", {})
    assert "error" in out and "MemoryError" in out["error"], out


def test_fails_closed_without_bwrap(monkeypatch, tmp_path):
    canary = tmp_path / "ran.txt"
    monkeypatch.setattr(sandbox, "_find_bwrap", lambda: None)
    sandbox.sandbox_available.cache_clear()
    try:
        out = run_python(f"open({str(canary)!r}, 'w').write('x')\nRESULT=1",
                         {})
    finally:
        monkeypatch.undo()
        sandbox.sandbox_available.cache_clear()
    assert out == {"error": UNAVAILABLE}
    assert not canary.exists()


def test_fails_closed_when_bwrap_cannot_start(monkeypatch, tmp_path):
    canary = tmp_path / "ran.txt"
    monkeypatch.setattr(sandbox, "_find_bwrap", lambda: "/bin/false")
    sandbox.sandbox_available.cache_clear()
    try:
        out = run_python(f"open({str(canary)!r}, 'w').write('x')\nRESULT=1",
                         {})
    finally:
        monkeypatch.undo()
        sandbox.sandbox_available.cache_clear()
    assert out == {"error": UNAVAILABLE}
    assert not canary.exists()
