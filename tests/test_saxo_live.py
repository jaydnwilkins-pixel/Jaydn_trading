"""Read-only Saxo LIVE view — offline, mocked HTTP."""
import json

import httpx
import pytest

from app.tools import saxo_live

ENV = {"SAXO_LIVE_APP_KEY": "appkey",
       "SAXO_LIVE_APP_URL": "http://localhost/claire-saxo"}
T0 = 1_800_000_000


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def tok(tmp_path):
    path = tmp_path / "tok.json"
    path.write_text(json.dumps({
        "access_token": "A1", "refresh_token": "R1",
        "access_expires_at": T0 + 1200, "refresh_expires_at": T0 + 2400,
        "code_verifier": "v", "redirect_uri": "http://localhost:5555/claire-saxo"}))
    return path


def test_only_get_and_only_portfolio_paths():
    assert all(p.startswith("/port/") for p in saxo_live.READ_PATHS.values())
    assert not any("/trade/" in p for p in saxo_live.READ_PATHS.values())
    with pytest.raises(saxo_live.SaxoError):
        saxo_live._get("orders/../../trade/v2/orders")
    src = open(saxo_live.__file__).read()
    assert ".post(" in src and src.count(".post(") == 1   # token endpoint only
    assert ".delete(" not in src and ".put(" not in src and ".patch(" not in src


def test_positions_are_flattened(tok):
    def handler(req):
        assert req.method == "GET"
        assert req.headers["authorization"] == "Bearer A1"
        return httpx.Response(200, json={"Data": [{
            "NetPositionBase": {"Amount": 100, "AssetType": "Stock"},
            "NetPositionView": {"AverageOpenPrice": 10, "CurrentPrice": 12,
                                "ProfitLossOnTrade": 200,
                                "ExposureInBaseCurrency": 1200},
            "DisplayAndFormat": {"Symbol": "D05:xses", "Description": "DBS",
                                 "Currency": "SGD"}}]})
    rows = saxo_live.positions(client=client(handler), clock=lambda: T0,
                               path=tok, env=ENV)
    assert rows == [{"symbol": "D05:xses", "name": "DBS", "asset_type": "Stock",
                     "currency": "SGD", "amount": 100, "avg_open_price": 10,
                     "current_price": 12, "market_value": None, "pnl": 200,
                     "pnl_base": None, "pnl_pct": 20.0,
                     "exposure_base": 1200}]


def test_refresh_rotates_and_persists(tok):
    seen = []

    def handler(req):
        seen.append(req.content.decode())
        return httpx.Response(200, json={
            "access_token": "A2", "refresh_token": "R2", "expires_in": 1200,
            "refresh_token_expires_in": 2400})
    near_expiry = T0 + 1100                  # < 5 min of access left
    t = saxo_live.access_token(env=ENV, client=client(handler),
                               clock=lambda: near_expiry, path=tok)
    assert t == "A2"
    assert "grant_type=refresh_token" in seen[0] and "refresh_token=R1" in seen[0]
    saved = json.loads(tok.read_text())
    assert saved["refresh_token"] == "R2"            # rotated
    assert saved["refresh_expires_at"] == near_expiry + 2400


def test_expired_session_says_sign_in_again(tok):
    with pytest.raises(saxo_live.NotSignedIn, match="bin/saxo login"):
        saxo_live.access_token(env=ENV, clock=lambda: T0 + 9999, path=tok)


def test_summary_is_honest_when_not_signed_in(tmp_path):
    out = saxo_live.summary(path=tmp_path / "none.json", env=ENV)
    assert "not signed in" in out["error"]


def test_pkce_challenge_matches_verifier():
    import base64
    import hashlib
    v, c = saxo_live.new_pkce()
    assert c == base64.urlsafe_b64encode(
        hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    url = saxo_live.authorize_url({"app_key": "k"}, "http://localhost:1/x", c, "s")
    assert url.startswith("https://live.logonvalidation.net/authorize?")
    assert "code_challenge_method=S256" in url


# ── sign-in alerts ───────────────────────────────────────────────────────
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def watch(tok, tmp_path):
    sent, clock = [], Clock(T0)
    w = saxo_live.SignInWatch(notify=lambda text, title: sent.append(text),
                              clock=clock, state_path=tmp_path / "st.json",
                              token_path=tok)
    return w, sent, clock


def test_quiet_while_refreshing_fine(watch):
    w, sent, _ = watch
    for _ in range(5):
        assert w.observe(refreshed=True) == "ok"
    assert sent == []


def test_warns_once_then_lapses_then_recovers(watch, tok):
    w, sent, clock = watch
    clock.t = T0 + 2400 - 600                 # 10 min of refresh window left
    err = httpx.ConnectError("network down")
    assert w.observe(refreshed=False, error=err) == "warning"
    assert w.observe(refreshed=False, error=err) == "warning"   # no repeat
    clock.t = T0 + 2500                       # window gone
    assert w.observe(refreshed=False, error=err) == "lapsed"
    assert w.observe(refreshed=False, error=err) == "lapsed"    # no repeat
    clock.t += saxo_live.REMIND_EVERY         # ...but reminds twice a day
    w.observe(refreshed=False, error=err)
    assert len(sent) == 3
    assert "lapses in about 10 min" in sent[0]
    assert "EXPIRED" in sent[1] and "bin/saxo login" in sent[1]
    # bin/saxo login writes a fresh token; the next keepalive succeeds
    data = json.loads(tok.read_text())
    data["refresh_expires_at"] = clock.t + 2400
    tok.write_text(json.dumps(data))
    assert w.observe(refreshed=True) == "ok"
    assert "signed in to Saxo live again" in sent[-1]


def test_saxo_rejecting_the_refresh_is_a_lapse_at_once(watch):
    w, sent, _ = watch
    assert w.observe(refreshed=False,
                     error=saxo_live.NotSignedIn("HTTP 401")) == "lapsed"
    assert "EXPIRED" in sent[0]


def test_silent_when_saxo_never_set_up(tmp_path):
    sent = []
    w = saxo_live.SignInWatch(notify=lambda t, title: sent.append(t),
                              state_path=tmp_path / "s.json",
                              token_path=tmp_path / "none.json")
    assert w.observe(refreshed=False) == "not_set_up" and sent == []


def test_lapse_survives_a_restart_without_repeating(watch, tok, tmp_path):
    w, sent, clock = watch
    clock.t = T0 + 9999
    w.observe(refreshed=False)
    w2 = saxo_live.SignInWatch(notify=lambda t, title: sent.append(t),
                               clock=clock, state_path=tmp_path / "st.json",
                               token_path=tok)
    w2.observe(refreshed=False)
    assert len(sent) == 1


def test_alerts_use_bobs_bot_and_never_raise(tmp_path):
    from app import alerts
    env = tmp_path / "telegram.env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=111,222\n")
    seen = []

    def h(req):
        seen.append((str(req.url), req.content.decode()))
        return httpx.Response(200, json={"ok": True})
    out = alerts.send("hello", path=env,
                      client=httpx.Client(transport=httpx.MockTransport(h)))
    assert "telegram" in out
    assert "/botabc/sendMessage" in seen[0][0] and "chat_id=111" in seen[0][1]

    def boom(req):
        raise httpx.ConnectError("offline")
    alerts.send("x", path=env,
                client=httpx.Client(transport=httpx.MockTransport(boom)))


def test_sim_app_env_targets_sim_and_its_own_token_file():
    """SAXO_APP_ENV=sim must point sign-in, API and token file at SIM, so a
    SIM sign-in can never be mistaken for the live account."""
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "from app.tools import saxo_live as s;"
         "print(s.AUTH_BASE, s.API_BASE, s.TOKEN_FILE.name)"],
        env={"SAXO_APP_ENV": "sim", "PATH": "/usr/bin"}, capture_output=True,
        text=True, cwd=str(saxo_live.ROOT), check=True).stdout.split()
    assert out == ["https://sim.logonvalidation.net",
                   "https://gateway.saxobank.com/sim/openapi",
                   "saxo_sim_token.json"]


def test_app_config_reads_new_and_old_names():
    assert saxo_live.app_config({"SAXO_APP_KEY": "k", "SAXO_APP_URL": "u"}
                                )["app_key"] == "k"
    assert saxo_live.app_config(ENV)["app_key"] == "appkey"   # old names



def test_sim_and_live_are_independent(tmp_path):
    """Two sign-ins, two token files, two login servers, two gateways."""
    assert saxo_live.token_file("sim") != saxo_live.token_file("live")
    cfg_s = saxo_live.app_config({"SAXO_SIM_APP_KEY": "s"}, "sim")
    cfg_l = saxo_live.app_config({"SAXO_LIVE_APP_KEY": "l"}, "live")
    assert cfg_s["auth_base"] == "https://sim.logonvalidation.net"
    assert cfg_l["auth_base"] == "https://live.logonvalidation.net"
    assert cfg_s["redirect_uri"] == cfg_l["redirect_uri"] == \
        "http://localhost/claire-saxo"
    with pytest.raises(saxo_live.NotSignedIn, match="SAXO_LIVE_APP_KEY"):
        saxo_live.app_config({"SAXO_SIM_APP_KEY": "s"}, "live")
    seen = []

    def h(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"Data": []})
    for w in ("sim", "live"):
        saxo_live.token_file(w).write_text(json.dumps({
            "access_token": w, "refresh_token": "r",
            "access_expires_at": T0 + 1200, "refresh_expires_at": T0 + 2400,
            "code_verifier": "v", "redirect_uri": "x"}))
        saxo_live.positions(w, client=client(h), clock=lambda: T0)
    assert seen[0].startswith("https://gateway.saxobank.com/sim/openapi/")
    assert seen[1].startswith("https://gateway.saxobank.com/openapi/")


def test_summary_names_the_account(tmp_path):
    out = saxo_live.summary("live", env={"SAXO_LIVE_ACCOUNT_ID": "99999999"})
    assert out["account_id"] == "99999999" and "error" in out
    assert "real money" in out["account"]


# ── sign-in flow (shared by bin/saxo and the dashboard button) ───────────
def _token_server(account_ids):
    """Fake Saxo: the token endpoint issues tokens, the accounts endpoint
    says which AccountId(s) the login holds."""
    def h(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={
                "access_token": "NEW", "refresh_token": "R",
                "expires_in": 1200, "refresh_token_expires_in": 3600})
        return httpx.Response(200, json={"Data": [
            {"AccountId": a, "AccountKey": f"k{a}"} for a in account_ids]})
    return client(h)


SIMENV = {"SAXO_SIM_APP_KEY": "s", "SAXO_SIM_ACCOUNT_ID": "11111111"}


def test_sign_in_installs_tokens_for_the_expected_account():
    url = saxo_live.begin_login("sim", "http://localhost:7787/claire-saxo",
                                env=SIMENV, clock=lambda: T0)
    assert url.startswith("https://sim.logonvalidation.net/authorize?")
    state = url.split("state=")[1]
    assert saxo_live.complete_login(state, "code", env=SIMENV,
                                    client=_token_server(["11111111"]),
                                    clock=lambda: T0) == "sim"
    assert json.loads(saxo_live.token_file("sim").read_text())[
        "access_token"] == "NEW"
    # single use: the same state cannot complete twice
    with pytest.raises(saxo_live.SaxoError, match="start the sign-in again"):
        saxo_live.complete_login(state, "code", env=SIMENV, clock=lambda: T0)


def test_wrong_account_is_refused_and_the_old_sign_in_survives():
    saxo_live.token_file("sim").write_text('{"access_token": "OLD"}')
    url = saxo_live.begin_login("sim", "http://localhost/claire-saxo",
                                env=SIMENV, clock=lambda: T0)
    with pytest.raises(saxo_live.SaxoError, match="11111111"):
        saxo_live.complete_login(url.split("state=")[1], "code", env=SIMENV,
                                 client=_token_server(["99999999"]),
                                 clock=lambda: T0)
    assert json.loads(saxo_live.token_file("sim").read_text())[
        "access_token"] == "OLD"
    leftovers = list(saxo_live.token_file("sim").parent.glob("*.pending*"))
    assert leftovers == []


def test_unknown_or_expired_state_is_refused():
    with pytest.raises(saxo_live.SaxoError):
        saxo_live.complete_login("forged", "code", env=SIMENV)
    url = saxo_live.begin_login("sim", "http://localhost/x", env=SIMENV,
                                clock=lambda: T0)
    with pytest.raises(saxo_live.SaxoError, match="expired"):
        saxo_live.complete_login(url.split("state=")[1], "c", env=SIMENV,
                                 clock=lambda: T0 + saxo_live.LOGIN_TTL + 1)


def test_saxo_symbols_map_to_desk_chart_symbols():
    from app.tools.saxo_broker import desk_symbol
    assert desk_symbol("D05:xses") == "D05.SI"
    assert desk_symbol("AAPL:xnas") == "AAPL"
    assert desk_symbol("7203:xtks") == "7203.T"
    assert desk_symbol("SAP:xetr") == "SAP.DE"
    assert desk_symbol("XYZ:xmil") is None and desk_symbol(None) is None
