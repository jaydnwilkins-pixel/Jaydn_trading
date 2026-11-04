"""bin/saxo — sign in to, and read, the two Saxo accounts (read-only).

  bin/saxo login sim|live      sign in (browser on THIS machine)
  bin/saxo status              both sign-ins: alive, and for how long
  bin/saxo positions [sim|live]  cash + positions (default: both)
  bin/saxo test-alert          send a test Telegram/desktop alert
"""
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse, urlunparse

from . import saxo_live


def login(which):
    try:
        cfg = saxo_live.app_config(which=which)
    except saxo_live.SaxoError as e:
        sys.exit(str(e))
    registered = urlparse(cfg["redirect_uri"])
    if registered.hostname not in ("localhost", "127.0.0.1"):
        sys.exit("the app's redirect URL must be a localhost URL (e.g. "
                 "http://localhost/claire-saxo) so this machine can catch "
                 "the sign-in redirect")
    box = {}

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
    url = saxo_live.begin_login(which, redirect)
    state = parse_qs(urlparse(url).query)["state"][0]
    print(f"Opening the Saxo {which.upper()} sign-in in your browser. If it "
          "does not open, visit:"
          f"\n\n  {url}\n")
    webbrowser.open(url)
    srv.serve_forever()
    if not box.get("code"):
        sys.exit(f"Sign-in failed: {box.get('error')}")
    try:
        saxo_live.complete_login(state, box["code"])
    except saxo_live.SaxoError as e:
        sys.exit(f"Sign-in refused: {e}")
    print("Signed in. claire-api keeps the session alive while it runs.")
    print(json.dumps(saxo_live.status(which), indent=2))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "positions"
    arg = argv[2] if len(argv) > 2 else None
    which = [arg] if arg else list(saxo_live.ENVS)
    if arg and arg not in saxo_live.ENVS:
        sys.exit("environment must be sim or live")
    if cmd == "login":
        if not arg:
            sys.exit("usage: bin/saxo login sim|live")
        login(arg)
    elif cmd == "status":
        print(json.dumps([saxo_live.status(w) for w in which], indent=2))
    elif cmd == "positions":
        print(json.dumps([saxo_live.summary(w, with_orders=True)
                          for w in which], indent=2, default=str))
    elif cmd == "test-alert":
        from .. import alerts
        print("delivered via:", alerts.send(
            "test alert — this is how you'll hear when the Saxo sign-in is "
            "about to lapse or has lapsed.", title="Claire · Saxo") or "none")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
