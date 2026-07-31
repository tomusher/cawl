"""HTTP client for the cawl control-plane daemon.

The CLI is a thin wrapper over this — it never touches state, Incus, or the
authorization policy directly; the daemon is the sole source of truth. Uses only
the standard library (no requests dependency).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

# transport: (method, url, headers, data|None) -> (status_code, body_bytes)
Transport = Callable[[str, str, dict, bytes | None], "tuple[int, bytes]"]


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class ConfigError(Exception):
    pass


def _urllib_transport(method, url, headers, data):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@dataclass
class ApiClient:
    base_url: str
    token: str
    transport: Transport = field(default=_urllib_transport)

    def _call(self, method: str, path: str, *, body=None, query=None):
        url = self.base_url.rstrip("/") + path
        if query:
            q = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
            if q:
                url += "?" + q
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        status, raw = self.transport(method, url, headers, data)
        payload = json.loads(raw) if raw else None
        if status >= 400:
            msg = payload.get("error") if isinstance(payload, dict) else None
            raise ApiError(status, msg or f"HTTP {status}")
        return payload

    # -- endpoints ---------------------------------------------------------
    def whoami(self):
        return self._call("GET", "/api/whoami")

    def up(self, **body):
        return self._call("POST", "/api/environments", body=body)

    def ls(self, template=None):
        return self._call("GET", "/api/environments", query={"template": template})

    def status(self, sid):
        return self._call("GET", f"/api/environments/{sid}")

    def exec(self, sid, cmd):
        return self._call("POST", f"/api/environments/{sid}/exec", body={"cmd": cmd})

    def rm(self, sid):
        return self._call("DELETE", f"/api/environments/{sid}")

    def stop(self, sid):
        return self._call("POST", f"/api/environments/{sid}/stop")

    def start(self, sid):
        return self._call("POST", f"/api/environments/{sid}/start")

    def ssh_cert(self, sid, public_key):
        return self._call("POST", f"/api/environments/{sid}/ssh-cert",
                          body={"public_key": public_key})

    def share(self, sid, principal):
        return self._call("POST", f"/api/environments/{sid}/grants",
                          body={"principal": principal})

    def unshare(self, sid, principal):
        return self._call("DELETE", f"/api/environments/{sid}/grants/{principal}")

    def expose(self, sid, port, name=None, access=None):
        return self._call("POST", f"/api/environments/{sid}/exposures",
                          body={"port": port, "name": name,
                                "access": access or []})

    def unexpose(self, sid, name):
        return self._call("DELETE", f"/api/environments/{sid}/exposures/{name}")

    def refresh_image(self, template, args=None, backend=None):
        return self._call("POST", "/api/images/refresh",
                          body={"template": template, "args": args or {},
                                "backend": backend})

    # -- templates ---------------------------------------------------------
    def template_create(self, yaml_text, scope=None):
        body = {"yaml": yaml_text}
        if scope:
            body["scope"] = scope
        return self._call("POST", "/api/templates", body=body)

    def templates(self):
        return self._call("GET", "/api/templates")

    def template_show(self, name):
        return self._call("GET", f"/api/templates/{name}")

    def template_rm(self, name):
        return self._call("DELETE", f"/api/templates/{name}")


def client_from_env(env: dict | None = None) -> ApiClient:
    import os
    e = env if env is not None else os.environ
    url = e.get("CAWL_API_URL")
    token = e.get("CAWL_TOKEN")
    if not url or not token:
        missing = [n for n, v in (("CAWL_API_URL", url), ("CAWL_TOKEN", token)) if not v]
        raise ConfigError("missing " + " and ".join(missing))
    return ApiClient(url, token)


def resolve_client(env: dict | None = None) -> ApiClient:
    """Env first (CAWL_API_URL / CAWL_TOKEN), then stored `cawl login` creds."""
    import os
    from cawl import credentials
    e = env if env is not None else os.environ
    url = e.get("CAWL_API_URL")
    token = e.get("CAWL_TOKEN")
    if not (url and token):
        f_url, f_token = credentials.load()
        url = url or f_url
        token = token or f_token
    if not url or not token:
        raise ConfigError("not logged in — run `cawl login` "
                          "(or set CAWL_API_URL and CAWL_TOKEN)")
    return ApiClient(url, token)
