"""Web search (DESIGN.md §9): Tavily when TAVILY_API_KEY is set, else
DuckDuckGo (free, keyless, noticeably weaker — the recommended upgrade is a
Tavily key)."""
import functools
import ipaddress
import os
import re
import socket

import httpx


# Shared pooled clients: a Client per call was never closed and leaked its
# socket until garbage collection (see tradingview._shared_client).
@functools.cache
def _search_client() -> httpx.Client:
    return httpx.Client(timeout=15, follow_redirects=True)


@functools.cache
def _page_client() -> httpx.Client:
    return httpx.Client(timeout=20, follow_redirects=False,
                        headers={"User-Agent": "Mozilla/5.0"})


def web_search(query: str, *, max_results=8, env=os.environ,
               _client=None) -> list[dict]:
    client = _client or _search_client()
    key = env.get("TAVILY_API_KEY")
    if key:
        r = client.post("https://api.tavily.com/search",
                        json={"api_key": key, "query": query,
                              "max_results": max_results})
        r.raise_for_status()
        return [{"title": x.get("title"), "url": x.get("url"),
                 "snippet": (x.get("content") or "")[:400]}
                for x in r.json().get("results", [])]
    # DuckDuckGo HTML fallback
    r = client.get("https://html.duckduckgo.com/html/", params={"q": query},
                   headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    out = []
    for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            r.text, re.S):
        url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        out.append({"title": title, "url": url, "snippet": ""})
        if len(out) >= max_results:
            break
    return out


class BlockedURL(ValueError):
    """fetch_page refused a URL that points at a non-public address."""


MAX_REDIRECTS = 5


def _check_public(url: str, resolve=socket.getaddrinfo) -> None:
    """Refuse anything but http(s) to public addresses: loopback (claire-api,
    the dashboards), RFC1918/ULA, link-local incl. cloud metadata
    169.254.169.254, CGNAT, multicast, reserved, unspecified. Hostnames are
    resolved and EVERY address must be public."""
    u = httpx.URL(url)
    if u.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme not allowed: {u.scheme!r}")
    host = u.host
    if not host:
        raise BlockedURL("URL has no host")
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        infos = resolve(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        raise BlockedURL(f"cannot resolve {host}: {e}") from e
    addrs = {i[4][0] for i in infos}
    if not addrs:
        raise BlockedURL(f"cannot resolve {host}")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise BlockedURL(f"{host} resolves to non-public address {ip}")


def fetch_page(url: str, *, max_chars=20000, _client=None,
               _resolve=socket.getaddrinfo) -> str:
    """Size-capped readability-ish extraction: strip tags, collapse space.
    Redirects are followed by hand so every hop is re-checked against
    _check_public (an open redirect must not bounce us onto loopback)."""
    client = _client or _page_client()
    for _ in range(MAX_REDIRECTS + 1):
        _check_public(url, _resolve)
        r = client.get(url, follow_redirects=False)
        if r.is_redirect and "location" in r.headers:
            url = str(r.url.join(r.headers["location"]))
            continue
        break
    else:
        raise BlockedURL(f"more than {MAX_REDIRECTS} redirects")
    r.raise_for_status()
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]
