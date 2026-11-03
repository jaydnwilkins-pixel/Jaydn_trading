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
                     "pnl_base": None, "exposure_base": 1200}]


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
