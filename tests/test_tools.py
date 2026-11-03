"""Tools layer tests — market (offline via injected fetcher), narrative jail,
run_python sandbox."""
import os
import socket
import time
from decimal import Decimal

import httpx
import pytest

from app.tools.files import JailError, Narratives
from app.tools.market import (Market, currency_for, normalize_price,
                              yahoo_symbol)
from app.tools.sandbox import run_python
from app.tools.search import BlockedURL, fetch_page


def test_symbol_and_currency_mapping():
    assert yahoo_symbol("C07", "SGX") == "C07.SI"
    assert yahoo_symbol("NVDA", "NASDAQ") == "NVDA"
    assert currency_for("SHEL.L") == "GBp"
    assert currency_for("C07.SI") == "SGD"
    assert currency_for("NVDA") == "USD"


def test_gbp_pence_normalization():
    px, ccy = normalize_price(3550, "GBp")      # LSE quotes are PENCE
    assert (px, ccy) == (Decimal("35.5"), "GBP")
    px, ccy = normalize_price(101.5, "USD")
    assert (px, ccy) == (Decimal("101.5"), "USD")


def make_market(responses, clock=None):
    calls = []

    def fake_get(url, params=None, need_crumb=False):
        calls.append(url)
        for frag, data in responses.items():
            if frag in url:
                return data
        raise AssertionError(f"unexpected URL {url}")
    t = {"now": 1000}
    # TradingView is tried first now; an empty result falls through to Yahoo
    m = Market(_get=fake_get, clock=lambda: t["now"],
               tv_quotes=lambda syms: {})
    return m, calls, t


def test_spark_flat_shape_and_stale_fallback():
    m, calls, _ = make_market({"spark": {
        "NVDA": {"close": 181.5, "previousClose": 180.0, "currency": "USD"},
        "C07.SI": {"close": None, "previousClose": 3.42},   # market shut
    }})
    q = m.spark(["NVDA", "C07.SI"])
    assert q["NVDA"]["price"] == Decimal("181.5")
    assert q["NVDA"]["stale"] is False
    assert q["C07.SI"]["price"] == Decimal("3.42")          # previousClose
    assert q["C07.SI"]["stale"] is True
    assert q["C07.SI"]["currency"] == "SGD"                 # from suffix


def test_fx_via_chart_symbol_and_cache():
    chart = {"chart": {"result": [{"timestamp": [1], "indicators": {"quote": [
        {"open": [1], "high": [1], "low": [1], "close": [0.74],
         "volume": [0]}]}, "meta": {"currency": "USD"}}]}}
    m, calls, t = make_market({"chart/SGDUSD=X": chart})
    assert m.fx("SGD", "USD") == Decimal("0.74")
    n = len(calls)
    assert m.fx("SGD", "USD") == Decimal("0.74")
    assert len(calls) == n                      # cached ~5 min
    t["now"] += 400
    m.fx("SGD", "USD")
    assert len(calls) == n + 1                  # cache expired → refetched
    assert m.fx("USD", "USD") == Decimal(1)


def test_narratives_jail(tmp_path):
    n = Narratives(tmp_path / "narratives")
    n.write("wi_1", "fundamental", "# analysis")
    assert n.read("wi_1", "fundamental") == "# analysis"
    assert n.list("wi_1") == ["fundamental.md"]
    with pytest.raises(JailError):
        n.write("wi_1", "../../../etc/passwd", "nope")
    with pytest.raises(JailError):
        n.write("../escape", "f", "nope")


def test_narratives_jail_confined_to_own_work_item(tmp_path):
    n = Narratives(tmp_path / "narratives")
    n.write("wi_B", "fundamental", "B's own report")
    for name in ("../wi_B/fundamental", "../wi_B/injected", "../wi_C/x",
                 "sub/../../wi_B/x"):
        with pytest.raises(JailError):
            n.write("wi_A", name, "overwrite")
        with pytest.raises(JailError):
            n.read("wi_A", name)
    assert n.read("wi_B", "fundamental") == "B's own report"
    assert not (tmp_path / "narratives" / "wi_C").exists()
    with pytest.raises(JailError):
        n.write("wi_A/../wi_B", "x", "nope")
    with pytest.raises(JailError):
        n.list("..")
    n.write("wi_A", "notes/extra", "ok")         # subfolders of its own: fine
    assert n.read("wi_A", "notes/extra") == "ok"


def test_sandbox_computes_with_numpy():
    out = run_python(
        "import numpy as np\n"
        "closes = np.array(DATA['close'], dtype=float)\n"
        "RESULT = {'sma3': float(closes[-3:].mean())}\n",
        {"close": [10, 11, 12, 13, 14]})
    assert out["result"]["sma3"] == 13.0


def test_sandbox_blocks_network_and_scrubs_env():
    os.environ["CLAIRE_INTERNAL_SECRET"] = "supersecret"
    try:
        out = run_python(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), timeout=2)\n"
            "    net = 'open'\n"
            "except Exception as e:\n"
            "    net = 'blocked'\n"
            "import os\n"
            "RESULT = {'net': net,"
            " 'secret_visible': 'CLAIRE_INTERNAL_SECRET' in os.environ}\n",
            {})
    finally:
        del os.environ["CLAIRE_INTERNAL_SECRET"]
    assert out["result"] == {"net": "blocked", "secret_visible": False}


def test_sandbox_timeout():
    out = run_python("while True: pass", {}, timeout=2)
    assert "error" in out


# ── fetch_page SSRF guard ────────────────────────────────────────────────
def _resolver(table):
    def resolve(host, port, proto=0):
        if host not in table:
            raise socket.gaierror(f"unknown {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
                for a in table[host]]
    return resolve


DNS = {"example.com": ["93.184.215.14"], "redir.example": ["93.184.215.15"],
       "evil-internal.example": ["10.0.0.5"],
       "dual.example": ["93.184.215.16", "127.0.0.1"],
       "127.0.0.1": ["127.0.0.1"], "localhost": ["127.0.0.1"],
       "169.254.169.254": ["169.254.169.254"], "192.168.1.10": ["192.168.1.10"],
       "::1": ["::1"], "2130706433": ["127.0.0.1"],
       "::ffff:127.0.0.1": ["::ffff:127.0.0.1"], "100.64.0.1": ["100.64.0.1"]}


def _client(routes, seen):
    def handler(req):
        seen.append(str(req.url))
        status, headers, body = routes.get(str(req.url), (404, {}, ""))
        return httpx.Response(status, headers=headers, text=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:7788/internal/state", "http://localhost:7787/api/runs",
    "http://169.254.169.254/latest/meta-data/", "http://192.168.1.10/",
    "http://[::1]:7788/", "http://2130706433/", "http://[::ffff:127.0.0.1]/",
    "http://100.64.0.1/", "http://evil-internal.example/",
    "http://dual.example/", "file:///etc/passwd", "ftp://example.com/"])
def test_fetch_page_blocks_non_public(url):
    seen = []
    with pytest.raises(BlockedURL):
        fetch_page(url, _client=_client({}, seen), _resolve=_resolver(DNS))
    assert seen == []                       # never even sent


def test_fetch_page_checks_every_redirect_hop():
    seen = []
    routes = {
        "http://example.com/a": (302, {"location": "http://redir.example/b"}, ""),
        "http://redir.example/b": (301, {"location":
                                         "http://127.0.0.1:7788/internal"}, ""),
    }
    with pytest.raises(BlockedURL):
        fetch_page("http://example.com/a", _client=_client(routes, seen),
                   _resolve=_resolver(DNS))
    assert seen == ["http://example.com/a", "http://redir.example/b"]


def test_fetch_page_follows_public_redirects_and_extracts():
    seen = []
    routes = {
        "http://example.com/a": (302, {"location": "/b"}, ""),
        "http://example.com/b": (200, {}, "<html><script>x()</script>"
                                 "<p>Hello   <b>world</b></p></html>"),
    }
    out = fetch_page("http://example.com/a", _client=_client(routes, seen),
                     _resolve=_resolver(DNS))
    assert out.strip() == "Hello world"
    assert seen == ["http://example.com/a", "http://example.com/b"]


def test_fetch_page_redirect_loop_capped():
    routes = {"http://example.com/a": (302, {"location": "/a"}, "")}
    with pytest.raises(BlockedURL):
        fetch_page("http://example.com/a", _client=_client(routes, []),
                   _resolve=_resolver(DNS))


# ── market cache: stale flag + rate budget ───────────────────────────────
class _Primary:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def supports(self, s):
        return True

    def quote(self, syms):
        self.calls += 1
        if self.fail:
            raise RuntimeError("429")
        return {s: {"symbol": s, "price": Decimal("10"), "stale": False,
                    "src": "primary"} for s in syms}

    def chart(self, symbol, range_, interval):
        self.calls += 1
        if self.fail:
            raise RuntimeError("429")
        return {"symbol": symbol, "close": [1, 2, 3], "src": "primary"}


def _primary_market(t):
    p = _Primary()
    m = Market(_get=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("free chain hit")), clock=lambda: t["now"],
        tv_quotes=lambda syms: {s: {"symbol": s, "price": Decimal("9"),
                                    "stale": False, "src": "tv"}
                                for s in syms}, primary=p)
    return m, p


def test_stale_cache_fallback_is_flagged():
    t = {"now": 1000}
    m, p = _primary_market(t)
    assert m.quote("NVDA")["stale"] is False
    assert m.chart("NVDA").get("stale") is None
    t["now"] += 1000                        # both entries expired
    p.fail = True                           # provider now erroring
    q = m.quote("NVDA")
    assert q["src"] == "primary" and q["stale"] is True
    c = m.chart("NVDA")
    assert c["src"] == "primary" and c["stale"] is True
    p.fail = False
    t["now"] += 1000
    assert m.quote("NVDA")["stale"] is False    # recovers when fresh
    # the cached object itself was not mutated by the stale marking
    assert m._cache[("pq", ("NVDA",))][1]["NVDA"]["stale"] is False


def test_yahoo_stale_cache_fallback_is_flagged():
    t = {"now": 1000}
    state = {"fail": False}
    chart = {"chart": {"result": [{"timestamp": [1], "indicators": {"quote": [
        {"open": [1], "high": [1], "low": [1], "close": [5.0],
         "volume": [0]}]}, "meta": {"currency": "USD"}}]}}
    spark = {"NVDA": {"close": 181.5, "previousClose": 180.0}}

    def get(url, params=None, need_crumb=False):
        if state["fail"]:
            raise RuntimeError("429")
        return chart if "chart" in url else spark
    m = Market(_get=get, clock=lambda: t["now"], tv_quotes=lambda s: {})
    assert m.chart("NVDA")["stale"] is False
    assert m.quote("NVDA")["stale"] is False
    t["now"] += 5000
    state["fail"] = True
    assert m.chart("NVDA")["stale"] is True
    assert m.quote("NVDA")["stale"] is True


def test_expired_cache_entry_goes_through_rate_budget():
    t = {"now": 1000}
    m, p = _primary_market(t)
    m.chart("NVDA")
    assert p.calls == 1
    m.chart("NVDA")                          # fresh hit: no budget spent
    assert p.calls == 1 and len(m._primary_calls) == 1
    m._primary_calls = [time.monotonic()] * m.PRIMARY_PER_MIN   # budget gone
    t["now"] += 1000                         # cache entry expired
    m._get = lambda *a, **k: {"chart": {"result": [{"timestamp": [1],
        "indicators": {"quote": [{"close": [7]}]}, "meta": {}}]}}
    c = m.chart("NVDA")
    assert p.calls == 1                      # primary NOT called over budget
    assert c["src"] == "yahoo"               # fell through to the free chain
    # quotes: same rule
    m.quote("NVDA")                          # budget gone → TradingView
    assert p.calls == 1
