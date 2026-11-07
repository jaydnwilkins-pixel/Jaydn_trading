"""Live smoke (DESIGN.md §18 layer 6) — auto-skips when services are down.
Start them with systemd (or by hand) and re-run to exercise for real."""
import re
from pathlib import Path

import httpx
import pytest


def _dash_base():
    """Where the running dashboards answer: RONATHAN_DASHBOARD_BIND from
    etc/ronathan.env (loopback by default, as bin/ronathan does)."""
    env = Path(__file__).resolve().parents[1] / "etc" / "ronathan.env"
    host = ""
    if env.exists():
        m = re.findall(r"(?m)^RONATHAN_DASHBOARD_BIND=(.*)$", env.read_text())
        host = m[-1].strip().strip("'\"") if m else ""
    if host in ("", "0.0.0.0", "::", "localhost"):
        host = "127.0.0.1"
    return f"http://{host}:7797"


DASH = _dash_base()


def _up(url):
    try:
        return httpx.get(url, timeout=3).status_code < 500
    except Exception:                              # noqa: BLE001
        return False


needs_api = pytest.mark.skipif(not _up("http://127.0.0.1:7798/status"),
                               reason="ronathan-api not running")
needs_dash = pytest.mark.skipif(not _up(f"{DASH}/api/overview"),
                                reason="dashboards not running")


@needs_api
def test_api_status():
    body = httpx.get("http://127.0.0.1:7798/status", timeout=5).json()
    assert body["ok"] is True


@needs_api
def test_ask_streams_ndjson():
    with httpx.stream("POST", "http://127.0.0.1:7798/ask",
                      json={"text": "status?"}, timeout=60) as r:
        lines = [line for line in r.iter_lines() if line]
    assert lines, "no NDJSON lines from /ask"
    import json
    kinds = {json.loads(line)["kind"] for line in lines}
    assert "done" in kinds


@needs_dash
def test_every_page_serves():
    for page in ("/", "/portfolio", "/markets", "/trading", "/agents",
                 "/providers"):
        r = httpx.get(f"{DASH}{page}", timeout=5)
        assert r.status_code == 200, page


@needs_dash
def test_live_quote_plausible():
    r = httpx.get(f"{DASH}/api/quote?symbols=NVDA",
                  timeout=30).json()
    if "error" in r and "429" in r["error"]:
        pytest.skip("Yahoo rate-limited right now")
    assert float(r["NVDA"]["price"]) > 1


def test_load_env_reads_quoted_values_like_bash(tmp_path, monkeypatch):
    from app.api.server import load_env
    p = tmp_path / "x.env"
    p.write_text("A_Q='abc|Def=='\nB_Q=\"x y\"\nC_Q=plain\n")
    for k in ("A_Q", "B_Q", "C_Q"):
        monkeypatch.delenv(k, raising=False)
    load_env(p)
    import os
    assert os.environ["A_Q"] == "abc|Def=="
    assert os.environ["B_Q"] == "x y" and os.environ["C_Q"] == "plain"
