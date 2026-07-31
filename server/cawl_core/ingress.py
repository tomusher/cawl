"""Traefik file-provider ingress: one dynamic-config file per environment.

Traefik watches ``dynamic_dir``; cawl rewrites ``<id>.yml`` whenever an
environment's exposures change and removes it on stop/destroy. Every exposure
router carries a forward-auth middleware pointing at the daemon, which decides
per request whether the visitor may view that exposure — possession of a URL
grants nothing on its own.

Two shared pieces live in ``_cawl.yml`` (the leading underscore can't collide
with an environment id): the ``cawl-auth`` forward-auth middleware, the
``cawl-daemon`` service, and the router for the auth host (magic-link login +
SSO). Each exposed host additionally routes the reserved ``/.cawl/`` path to
the daemon — that's where the login handoff sets the host-scoped cookie, so no
parent-domain cookie ever exists for an app inside a VM to see.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

import yaml

from cawl_core.models import Environment
from cawl_core.naming import exposure_host

# Paths routed to the daemon (never an environment VM).
CAWL_PATH_PREFIX = "/.cawl/"
AUTH_PATH_PREFIX = "/auth/"


class TraefikIngress:
    def __init__(self, dynamic_dir: str | Path, base_domain: str, *,
                 forward_auth_url: str = "", daemon_url: str = "",
                 auth_host: str = "", cert_resolver: str = "le"):
        self.dir = Path(dynamic_dir)
        self.base_domain = base_domain
        # How *Traefik* reaches the daemon — an internal URL, not the public one.
        self.forward_auth_url = forward_auth_url
        self.daemon_url = daemon_url
        self.auth_host = auth_host
        self.cert_resolver = cert_resolver

    def _path(self, id: str) -> Path:
        return self.dir / f"{id}.yml"

    def url_for(self, name: str) -> str:
        return f"https://{exposure_host(name, self.base_domain)}"

    def sync(self, inst: Environment) -> None:
        """Make the routes match the environment: one router+service per exposure,
        plus the /.cawl/ handoff router per host. Idempotent; call it whenever
        exposures or the VM's IP change. No exposures (or no IP) => no file."""
        if not inst.exposures or not inst.vm_ip:
            self.deregister(inst.id)
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self._ensure_shared()
        self._atomic_write(
            self._path(inst.id), yaml.safe_dump(self._render(inst), sort_keys=False))

    def deregister(self, id: str) -> None:
        self._path(id).unlink(missing_ok=True)

    def _render(self, inst: Environment) -> dict:
        routers: dict = {}
        services: dict = {}
        for exp in inst.exposures:
            host = exposure_host(exp.name, self.base_domain)
            key = f"{inst.id}-{exp.name}"
            routers[key] = {
                "rule": f"Host(`{host}`)",
                "service": key,
                "entryPoints": ["websecure"],
                "middlewares": ["cawl-auth"],  # fail-closed: every router, no bypass
                "tls": {"certResolver": self.cert_resolver},
            }
            # The login handoff. Higher priority than the app router so the
            # daemon, not the VM, answers /.cawl/* on this host.
            routers[f"{key}-cawl"] = {
                "rule": f"Host(`{host}`) && PathPrefix(`{CAWL_PATH_PREFIX}`)",
                "service": "cawl-daemon",
                "entryPoints": ["websecure"],
                "priority": 1000,
                "tls": {"certResolver": self.cert_resolver},
            }
            services[key] = {
                "loadBalancer": {
                    "servers": [{"url": f"http://{inst.vm_ip}:{exp.port}"}]
                }
            }
        return {"http": {"routers": routers, "services": services}}

    def _ensure_shared(self) -> None:
        """The pieces every environment file references: the forward-auth
        middleware, the daemon service, and the auth host's own router.
        Rewritten (not appended) so config changes propagate."""
        if not (self.forward_auth_url and self.daemon_url):
            return
        doc: dict = {
            "http": {
                "middlewares": {
                    "cawl-auth": {
                        "forwardAuth": {
                            "address": self.forward_auth_url,
                            "authResponseHeaders": ["X-Cawl-User"],
                        }
                    }
                },
                "services": {
                    "cawl-daemon": {
                        "loadBalancer": {"servers": [{"url": self.daemon_url}]}
                    }
                },
            }
        }
        if self.auth_host:
            doc["http"]["routers"] = {
                "cawl-auth-host": {
                    # Only viewer-auth endpoints are public on this host. Without
                    # the path matcher, /api/, /admin/, and /cli/login would also
                    # expose the daemon through Traefik.
                    "rule": (f"Host(`{self.auth_host}`) && "
                             f"PathPrefix(`{AUTH_PATH_PREFIX}`)"),
                    "service": "cawl-daemon",
                    "entryPoints": ["websecure"],
                    "tls": {"certResolver": self.cert_resolver},
                }
            }
        path = self.dir / "_cawl.yml"
        text = yaml.safe_dump(doc, sort_keys=False)
        if not path.exists() or path.read_text() != text:
            self._atomic_write(path, text)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Publish a complete dynamic-config file in one filesystem operation.

        Traefik watches this directory, so writing the destination directly can
        make it parse a partially written YAML document.  Keep the temporary
        file off the ``*.yml`` watch set, flush it, then atomically replace the
        destination in the same directory.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
        finally:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
