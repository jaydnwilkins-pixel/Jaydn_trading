#!/usr/bin/env python3
"""Saxo Bank OpenAPI MCP server for Jaydn's trading desk.

Exposes Saxo account data, instrument search, quotes, and (SIM-only) order
placement as MCP tools. No community/official Saxo MCP existed, so this wraps
the OpenAPI directly with stdlib HTTP.

Environment:
  SAXO_ENV           - "sim" (default) or "live".
  SAXO_ACCESS_TOKEN  - optional bearer token. On SIM, leave it unset: the
                       server then uses the desk's own Saxo SIM sign-in
                       (bin/saxo login sim, or the Saxo accounts page), which
                       ronathan-api renews every 10 minutes — no daily token.
                       Set it only to override that with a 24h developer-
                       portal token. For live: OAuth app token (read-only).

SAFETY: order placement and cancellation REFUSE to run unless SAXO_ENV=sim.
Live is read-only by design (the desk's paper-only rule).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("saxo")

ENV = os.environ.get("SAXO_ENV", "sim").strip().lower()
BASE = ("https://gateway.saxobank.com/sim/openapi" if ENV == "sim"
        else "https://gateway.saxobank.com/openapi")


ROOT = Path(__file__).resolve().parents[1]


def _desk_token():
    """The desk's Saxo SIM sign-in (etc/saxo_sim_token.json), refreshed under
    the desk's own file lock when close to expiry, so this server and
    ronathan-api can share it safely. SIM only: never the live sign-in."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.api.server import load_env          # app key for a refresh
    from app.tools import saxo_live
    load_env(ROOT / "etc" / "ronathan.env")
    return saxo_live.access_token("sim")


def _token():
    token = os.environ.get("SAXO_ACCESS_TOKEN", "").strip()
    if token or ENV != "sim":
        return token, None
    try:
        return _desk_token(), None
    except Exception as e:                       # noqa: BLE001 — reported
        return "", (f"no Saxo SIM sign-in: {e}. Sign in on the desk's Saxo "
                    "accounts page (or bin/saxo login sim), or pass a 24h "
                    "token with claude mcp add -e SAXO_ACCESS_TOKEN=...")


def _call(method, path, payload=None):
    token, problem = _token()
    if not token:
        return {"error": problem or "SAXO_ACCESS_TOKEN is not set"}
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        if e.code == 401:
            detail = ("token expired or invalid — sign in again on the "
                      "desk's Saxo accounts page" if not os.environ.get(
                          "SAXO_ACCESS_TOKEN") else "token expired or "
                      "invalid — get a fresh 24h token from developer.saxo")
        return {"error": f"HTTP {e.code}", "detail": detail}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def saxo_status() -> str:
    """Check Saxo connection: environment (sim/live), token validity, user info."""
    me = _call("GET", "/port/v1/users/me")
    return json.dumps({"env": ENV, "trading_allowed": ENV == "sim", "user": me})


@mcp.tool()
def saxo_search_instrument(keyword: str, asset_type: str = "Stock") -> str:
    """Search Saxo instruments by keyword/ticker. Returns matches with the Uic
    (Saxo's instrument id) needed for quotes and orders.
    asset_type: Stock, Etf, FxSpot, etc."""
    r = _call("GET", f"/ref/v1/instruments/?Keywords={urllib.request.quote(keyword)}"
                     f"&AssetTypes={asset_type}&$top=10")
    if "Data" in r:
        r = [{"Uic": i.get("Identifier"), "Symbol": i.get("Symbol"),
              "Description": i.get("Description"), "Exchange": i.get("ExchangeId"),
              "Currency": i.get("CurrencyCode"), "AssetType": i.get("AssetType")}
             for i in r["Data"]]
    return json.dumps(r)


@mcp.tool()
def saxo_quote(uic: int, asset_type: str = "Stock") -> str:
    """Get a price snapshot for an instrument by Uic (from saxo_search_instrument)."""
    return json.dumps(_call(
        "GET", f"/trade/v1/infoprices/?Uic={uic}&AssetType={asset_type}"
               f"&FieldGroups=Quote,PriceInfo,PriceInfoDetails"))


@mcp.tool()
def saxo_account() -> str:
    """Get Saxo accounts and cash balances."""
    accounts = _call("GET", "/port/v1/accounts/me")
    balances = _call("GET", "/port/v1/balances/me")
    return json.dumps({"accounts": accounts, "balances": balances})


@mcp.tool()
def saxo_positions() -> str:
    """List open positions with P/L."""
    return json.dumps(_call(
        "GET", "/port/v1/positions/me?FieldGroups="
               "PositionBase,PositionView,DisplayAndFormat"))


@mcp.tool()
def saxo_orders() -> str:
    """List working (open) orders."""
    return json.dumps(_call("GET", "/port/v1/orders/me?$top=50"))


@mcp.tool()
def saxo_place_order(account_key: str, uic: int, buy_sell: str, amount: int,
                     order_type: str = "Limit", price: float = 0.0,
                     asset_type: str = "Stock",
                     stop_loss: float = 0.0, take_profit: float = 0.0) -> str:
    """Place an order on the Saxo SIM (paper) environment. REFUSES on live.
    buy_sell: "Buy" or "Sell". order_type: "Limit" or "Market".
    price required for Limit. Optional stop_loss / take_profit attach related
    orders (bracket). account_key comes from saxo_account."""
    if ENV != "sim":
        return json.dumps({"error": "REFUSED: live trading is disabled by design. "
                                    "Set SAXO_ENV=sim for paper trading."})
    order = {
        "AccountKey": account_key, "Uic": uic, "AssetType": asset_type,
        "BuySell": buy_sell, "Amount": amount, "OrderType": order_type,
        "ManualOrder": False,
        "OrderDuration": {"DurationType": "GoodTillCancel"},
    }
    if order_type == "Limit":
        if not price:
            return json.dumps({"error": "Limit order needs a price"})
        order["OrderPrice"] = price
    related = []
    if stop_loss:
        related.append({"AssetType": asset_type, "Uic": uic, "Amount": amount,
                        "BuySell": "Sell" if buy_sell == "Buy" else "Buy",
                        "OrderType": "StopIfTraded", "OrderPrice": stop_loss,
                        "OrderDuration": {"DurationType": "GoodTillCancel"},
                        "ManualOrder": False})
    if take_profit:
        related.append({"AssetType": asset_type, "Uic": uic, "Amount": amount,
                        "BuySell": "Sell" if buy_sell == "Buy" else "Buy",
                        "OrderType": "Limit", "OrderPrice": take_profit,
                        "OrderDuration": {"DurationType": "GoodTillCancel"},
                        "ManualOrder": False})
    if related:
        order["Orders"] = related
    return json.dumps(_call("POST", "/trade/v2/orders", order))


@mcp.tool()
def saxo_cancel_order(order_id: str, account_key: str) -> str:
    """Cancel a working order (SIM only)."""
    if ENV != "sim":
        return json.dumps({"error": "REFUSED: live trading is disabled by design."})
    return json.dumps(_call(
        "DELETE", f"/trade/v2/orders/{order_id}/?AccountKey={account_key}"))


if __name__ == "__main__":
    mcp.run()
