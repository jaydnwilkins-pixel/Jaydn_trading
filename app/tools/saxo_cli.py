"""bin/saxo — sign in to, and read, the real Saxo account (read-only)."""
import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse, urlunparse

from . import saxo_live


def login():
    cfg = saxo_live.app_config()
    registered = urlparse(cfg["redirect_uri"])
    if registered.hostname not in ("localhost", "127.0.0.1"):
        sys.exit("SAXO_LIVE_APP_URL must be a localhost URL (e.g. "
                 "http://localhost/claire-saxo) so this machine can catch "
                 "the sign-in redirect")
    box, state = {}, secrets.token_urlsafe(16)

    class Catch(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            if urlparse(self.path).path != (registered.path or "/"):
                self.send_response(404)
                self.end_headers()
                return
            ok = q.get("state", [""])[0] == state and "code" in q
            box["code"] = q["code"][0] if ok else None
            box["error"] = None if ok else (q.get("error_description") or
                                            q.get("error") or ["bad state"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<h3>Signed in — you can close this tab.</h3>"
                              if ok else "<h3>Sign-in failed — see terminal."
                              "</h3>").encode())
            threading.Thread(target=self.server.shutdown).start()

        def log_message(self, *a):
            pass

    # Saxo matches the registered host+path and accepts any port.
    srv = HTTPServer(("127.0.0.1", 0), Catch)
    port = srv.server_address[1]
    redirect = urlunparse(registered._replace(
        netloc=f"{registered.hostname}:{port}"))
    verifier, challenge = saxo_live.new_pkce()
    url = saxo_live.authorize_url(cfg, redirect, challenge, state)
    print("Opening Saxo sign-in in your browser. If it does not open, visit:"
          f"\n\n  {url}\n")
    webbrowser.open(url)
    srv.serve_forever()
    if not box.get("code"):
        sys.exit(f"Sign-in failed: {box.get('error')}")
    saxo_live.exchange_code(cfg, box["code"], verifier, redirect)
    print("Signed in. claire-api keeps the session alive while it runs.")
    print(json.dumps(saxo_live.status(), indent=2))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "positions"
    if cmd == "login":
        login()
    elif cmd == "status":
        print(json.dumps(saxo_live.status(), indent=2))
    elif cmd == "positions":
        print(json.dumps(saxo_live.summary(), indent=2, default=str))
    else:
        sys.exit("usage: bin/saxo login | status | positions")


if __name__ == "__main__":
    main(sys.argv)
