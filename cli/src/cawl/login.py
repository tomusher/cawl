"""Browser-based login: OAuth-style loopback flow.

Start a throwaway HTTP server on 127.0.0.1, open the daemon's /cli/login page,
and wait for it to redirect back with a freshly minted token. The token only
ever travels to localhost.
"""

from __future__ import annotations

import http.server
import secrets
import urllib.parse
import webbrowser

_SUCCESS = ("<!doctype html><meta charset=utf-8>"
            "<body style='font-family:sans-serif;text-align:center;margin-top:4rem'>"
            "<h2>cawl: you're logged in ✓</h2><p>You can close this tab.</p>"
            ).encode("utf-8")
_FAIL = ("<!doctype html><meta charset=utf-8>"
         "<body style='font-family:sans-serif;text-align:center;margin-top:4rem'>"
         "<h2>cawl login failed</h2><p>State mismatch — try again.</p>"
         ).encode("utf-8")


def browser_login(api_url: str, *, timeout: int = 300, open_browser: bool = True) -> str:
    """Return a token obtained via the loopback redirect, or raise."""
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = params.get("token", [None])[0]
            if token:  # ignore stray requests (e.g. /favicon.ico)
                result["token"] = token
                result["state"] = params.get("state", [""])[0]
            ok = token and result.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS if ok else _FAIL)

        def log_message(self, *a):  # silence stderr
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    httpd.timeout = timeout
    port = httpd.server_address[1]
    callback = f"http://127.0.0.1:{port}/"
    login_url = api_url.rstrip("/") + "/cli/login?" + urllib.parse.urlencode(
        {"callback": callback, "state": state})

    print(f"Opening your browser to log in:\n  {login_url}")
    if open_browser:
        try:
            webbrowser.open(login_url)
        except Exception:  # noqa: BLE001 — headless; the printed URL still works
            pass

    while "token" not in result:
        httpd.handle_request()  # blocks up to `timeout`; loops past stray hits

    if result.get("state") != state:
        raise RuntimeError("login failed: state mismatch")
    return result["token"]
