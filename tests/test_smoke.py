"""Live smoke (DESIGN.md §18 layer 6) — OPT-IN: `RONATHAN_SMOKE=1 pytest
tests/test_smoke.py`. These talk to the RUNNING desk (one of them sends
Ronathan a real chat turn), so the default suite leaves them out; they used to
run whenever the services happened to be up (REVIEW-2026-11-11 §3.7)."""
import os

import httpx
import pytest

OPTED_IN = bool(os.environ.get("RONATHAN_SMOKE"))


def _desk_urls():
    """The running desk's two addresses: its ports are settings
    (app/ports.py), and the dashboards answer on RONATHAN_DASHBOARD_BIND
    (loopback by default) — read from etc/ronathan.env the way the services
    and bin/ronathan do."""
    from app import ports
    from app.api.server import ROOT
    env = dict(os.environ)
    try:
        for line in (ROOT / "etc" / "ronathan.env").read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k in ("RONATHAN_API_PORT", "RONATHAN_DASHBOARD_PORT",
                             "RONATHAN_DASHBOARD_BIND"):
                env.setdefault(k, v.strip().strip("'\""))
    except OSError:
        pass
    return ports.api_base(env), ports.dashboard_base(env)


API, DASH = _desk_urls() if OPTED_IN else ("http://127.0.0.1:7798",
                                           "http://127.0.0.1:7797")


def _up(url):
    if not OPTED_IN:                # do not even probe the live desk
        return False
    try:
        return httpx.get(url, timeout=3).status_code < 500
    except Exception:                              # noqa: BLE001
        return False


needs_api = pytest.mark.skipif(not _up(API + "/status"),
                               reason="set RONATHAN_SMOKE=1 and start ronathan-api")
needs_dash = pytest.mark.skipif(not _up(DASH + "/api/overview"),
                                reason="set RONATHAN_SMOKE=1 and start the dashboards")


@needs_api
def test_api_status():
    body = httpx.get(API + "/status", timeout=5).json()
    assert body["ok"] is True


@needs_api
def test_ask_streams_ndjson():
    with httpx.stream("POST", API + "/ask",
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
    r = httpx.get(DASH + "/api/quote?symbols=NVDA",
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
