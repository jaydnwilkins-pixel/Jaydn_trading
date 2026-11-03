"""READ-ONLY view of Richard's real (live) Saxo account.

Claire is a paper desk (DESIGN.md §1). This module exists so she can SEE the
real account — positions, cash, P/L — and nothing more. That is enforced here,
in code, not by convention:

- the client can only issue GET requests (`_get` is the only verb), and
- only to the fixed READ_PATHS below — portfolio reads; no /trade/ path exists.

Sign-in is OAuth authorization-code + PKCE against Saxo's live login, done
once with `bin/saxo login`. Saxo access tokens live ~20 min and refresh tokens
~40 min, and every refresh ROTATES the refresh token (the old one dies). So:
- the token file is guarded by an exclusive file lock — two processes
  refreshing at once would invalidate each other;
- claire-api runs a keepalive (`keepalive_loop`) that refreshes well inside
  the window, so the sign-in survives while the desk is up. If the desk is
  down longer than the refresh window, run `bin/saxo login` again.

Secrets: the token file lives in etc/ (0600) beside claire.env, which the
run_python sandbox cannot see and no agent tool can read.
"""
import base64
import fcntl
import hashlib
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parents[2]
TOKEN_FILE = ROOT / "etc" / "saxo_live_token.json"
AUTH_BASE = "https://live.logonvalidation.net"
API_BASE = "https://gateway.saxobank.com/openapi"
REFRESH_MARGIN = 300            # refresh when < 5 min of access token remain
KEEPALIVE_EVERY = 600           # well inside a ~40 min refresh-token window

# The ONLY endpoints this module will call. Reads of the client's own
# portfolio; nothing under /trade/ — there is no way to place, amend or
# cancel an order from here.
READ_PATHS = {
    "me": "/port/v1/users/me",
    "accounts": "/port/v1/accounts/me",
    "balances": "/port/v1/balances/me",
    "positions": "/port/v1/netpositions/me?FieldGroups="
                 "NetPositionBase,NetPositionView,DisplayAndFormat&$top=500",
    "orders": "/port/v1/orders/me?FieldGroups=DisplayAndFormat&$top=200",
}


class SaxoError(Exception):
    pass


class NotSignedIn(SaxoError):
    pass


# ── PKCE / sign-in ───────────────────────────────────────────────────────
def app_config(env=os.environ):
    key = env.get("SAXO_LIVE_APP_KEY", "").strip()
    url = env.get("SAXO_LIVE_APP_URL", "").strip()
    if not key or not url:
        raise NotSignedIn("SAXO_LIVE_APP_KEY / SAXO_LIVE_APP_URL are not set "
                          "in etc/claire.env — register a live app on "
                          "developer.saxo first")
    return {"app_key": key, "redirect_uri": url,
            "secret": env.get("SAXO_LIVE_APP_SECRET", "").strip() or None}


def new_pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(
        b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(cfg, redirect_uri, challenge, state):
    return AUTH_BASE + "/authorize?" + urlencode({
        "response_type": "code", "client_id": cfg["app_key"],
        "redirect_uri": redirect_uri, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state})


def _token_request(cfg, data, client=None):
    data = {**data, "client_id": cfg["app_key"]}
    auth = (cfg["app_key"], cfg["secret"]) if cfg.get("secret") else None
    c = client or httpx.Client(timeout=20)
    r = c.post(AUTH_BASE + "/token", data=data, auth=auth)
    if r.status_code != 200 and r.status_code != 201:
        raise NotSignedIn(f"Saxo token endpoint said HTTP {r.status_code}: "
                          f"{r.text[:200]} — run bin/saxo login again")
    return r.json()


def _stamp(tok, verifier, redirect_uri, now):
    return {"access_token": tok["access_token"],
            "refresh_token": tok["refresh_token"],
            "access_expires_at": now + int(tok.get("expires_in", 1200)),
            "refresh_expires_at": now + int(
                tok.get("refresh_token_expires_in", 2400)),
            "code_verifier": verifier, "redirect_uri": redirect_uri}


def exchange_code(cfg, code, verifier, redirect_uri, *, client=None,
                  clock=time.time, path=TOKEN_FILE):
    tok = _token_request(cfg, {"grant_type": "authorization_code",
                               "code": code, "redirect_uri": redirect_uri,
                               "code_verifier": verifier}, client)
    with _locked(path) as f:
        _write(f, path, _stamp(tok, verifier, redirect_uri, int(clock())))


# ── token store (locked; rotation-safe) ──────────────────────────────────
@contextmanager
def _locked(path=TOKEN_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path=TOKEN_FILE):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise NotSignedIn("not signed in to Saxo — run bin/saxo login")


def _write(_, path, data):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def access_token(*, env=os.environ, client=None, clock=time.time,
                 path=TOKEN_FILE, force=False):
    """A valid access token, refreshing (under the lock) when close to
    expiry. The re-read inside the lock matters: another process may have
    just rotated the refresh token, and using the old one would fail."""
    tok = _read(path)
    now = int(clock())
    if not force and tok["access_expires_at"] - now > REFRESH_MARGIN:
        return tok["access_token"]
    with _locked(path):
        tok = _read(path)
        now = int(clock())
        if not force and tok["access_expires_at"] - now > REFRESH_MARGIN:
            return tok["access_token"]
        if tok["refresh_expires_at"] <= now:
            raise NotSignedIn("Saxo sign-in expired (the desk was down "
                              "longer than Saxo's refresh window) — run "
                              "bin/saxo login")
        cfg = app_config(env)
        new = _token_request(cfg, {"grant_type": "refresh_token",
                                   "refresh_token": tok["refresh_token"],
                                   "redirect_uri": tok["redirect_uri"],
                                   "code_verifier": tok["code_verifier"]},
                             client)
        _write(None, path, _stamp(new, tok["code_verifier"],
                                  tok["redirect_uri"], now))
        return new["access_token"]


def status(*, clock=time.time, path=TOKEN_FILE):
    try:
        tok = _read(path)
    except NotSignedIn as e:
        return {"signed_in": False, "detail": str(e)}
    now = int(clock())
    return {"signed_in": tok["refresh_expires_at"] > now,
            "access_valid_s": max(tok["access_expires_at"] - now, 0),
            "refresh_valid_s": max(tok["refresh_expires_at"] - now, 0)}


# ── reads ────────────────────────────────────────────────────────────────
def _get(what, *, env=os.environ, client=None, clock=time.time,
         path=TOKEN_FILE):
    if what not in READ_PATHS:                 # nothing else is reachable
        raise SaxoError(f"{what!r} is not a read-only Saxo endpoint")
    token = access_token(env=env, client=client, clock=clock, path=path)
    c = client or httpx.Client(timeout=20)
    r = c.get(API_BASE + READ_PATHS[what],
              headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 401:
        raise NotSignedIn("Saxo rejected the token — run bin/saxo login")
    if r.status_code >= 400:
        raise SaxoError(f"Saxo {what}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def positions(**kw):
    """Open net positions, flattened to what a person reads."""
    out = []
    for p in _get("positions", **kw).get("Data", []):
        base, view = p.get("NetPositionBase", {}), p.get("NetPositionView", {})
        disp = p.get("DisplayAndFormat", {})
        out.append({
            "symbol": disp.get("Symbol"), "name": disp.get("Description"),
            "asset_type": base.get("AssetType"),
            "currency": disp.get("Currency"),
            "amount": base.get("Amount"),
            "avg_open_price": view.get("AverageOpenPrice"),
            "current_price": view.get("CurrentPrice"),
            "market_value": view.get("MarketValue"),
            "pnl": view.get("ProfitLossOnTrade"),
            "pnl_base": view.get("ProfitLossOnTradeInBaseCurrency"),
            "exposure_base": view.get("ExposureInBaseCurrency"),
        })
    return sorted(out, key=lambda r: -abs(r.get("exposure_base") or 0))


def balance(**kw):
    b = _get("balances", **kw)
    keep = ("Currency", "CashBalance", "TotalValue", "MarginAvailableForTrading",
            "UnrealizedPositionsValue", "NonMarginPositionsValue",
            "OpenPositionsCount")
    return {k: b.get(k) for k in keep if k in b}


def summary(**kw):
    """One call for Claire / the CLI: cash + positions, or an honest reason."""
    try:
        return {"account": "Saxo LIVE (read-only)", "balance": balance(**kw),
                "positions": positions(**kw)}
    except NotSignedIn as e:
        return {"error": str(e)}


# ── keepalive (claire-api) ───────────────────────────────────────────────
def keepalive_loop(stop_event=None, *, every=KEEPALIVE_EVERY):
    """Refresh before the ~40 min refresh window closes, so the sign-in
    survives while the desk is up. Silent when not signed in."""
    while not (stop_event and stop_event.is_set()):
        try:
            if status()["signed_in"]:
                access_token(force=True)
        except Exception:                       # noqa: BLE001 — retry later;
            pass                                # summary() reports the state
        if stop_event:
            stop_event.wait(every)
        else:
            time.sleep(every)
