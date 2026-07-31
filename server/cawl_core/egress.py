"""Trusted per-environment outbound egress policy providers."""
from __future__ import annotations

from abc import ABC
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shlex
import tempfile
from urllib.parse import urlparse


def normalize_host(value: str) -> str:
    value = value.rstrip(".").lower()
    if not value or len(value) > 253 or any(c.isspace() for c in value):
        raise ValueError("invalid hostname")
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        pass
    else:
        raise ValueError("IP literal destinations are forbidden")
    labels = value.split(".")
    if any(not x or len(x) > 63 or x[0] == "-" or x[-1] == "-" or
           not all(c.isascii() and (c.isalnum() or c == "-") for c in x)
           for x in labels):
        raise ValueError("invalid hostname")
    return value


@dataclass(frozen=True)
class EgressPolicy:
    name: str
    allowed_hosts: tuple[str, ...]

    def __post_init__(self):
        if not self.name or any(c.isspace() for c in self.name):
            raise ValueError("egress policy name is required")
        object.__setattr__(self, "allowed_hosts", tuple(dict.fromkeys(
            normalize_host(h) for h in self.allowed_hosts)))


@dataclass(frozen=True)
class EgressAttachment:
    network: str = ""


class EgressProvider(ABC):
    """Deployment-selected egress provider; never controlled by a guest."""
    policy = EgressPolicy("none", ())

    def attachment(self, environment_id: str) -> EgressAttachment:
        return EgressAttachment()

    def boot_script(self, environment_id: str) -> str:
        return ""

    def register(self, environment_id: str, source_ip: str, allowed_hosts: tuple[str, ...]) -> None:
        """Activate a source-IP policy before any guest hook can run."""

    def unregister(self, environment_id: str, source_ip: str | None = None) -> None:
        """Remove all matching active source-IP policies."""


class NoEgress(EgressProvider):
    """Default deny-egress attachment; registration is intentionally a no-op."""


class JsonPolicyStore:
    """Atomic file-backed implementation of the proxy control-store port."""
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _mutation_lock(self):
        """Serialize read-modify-write across all daemon worker processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError("policy store must be an object")
        # Validate before a write can preserve / consume the document.
        result = {}
        for cidr, item in data.items():
            network = ipaddress.ip_network(cidr, strict=True)
            if network.prefixlen != network.max_prefixlen or not isinstance(item, dict):
                raise ValueError("policy entries must use single-IP CIDRs")
            env, hosts = item.get("environment_id"), item.get("hosts")
            if not isinstance(env, str) or not env or not isinstance(hosts, list):
                raise ValueError("invalid policy entry")
            result[str(network)] = {"environment_id": env,
                                    "hosts": list(dict.fromkeys(normalize_host(h) for h in hosts))}
        return result

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            try: os.unlink(name)
            except FileNotFoundError: pass

    def register(self, environment_id: str, source_ip: str, allowed_hosts: tuple[str, ...]) -> None:
        ip = ipaddress.ip_address(source_ip)
        with self._mutation_lock():
            data = self._read()
            # An environment can only own its current address.
            data = {k: v for k, v in data.items()
                    if v["environment_id"] != environment_id}
            data[f"{ip}/{ip.max_prefixlen}"] = {
                "environment_id": environment_id,
                "hosts": list(dict.fromkeys(normalize_host(h) for h in allowed_hosts)),
            }
            self._write(data)

    def unregister(self, environment_id: str, source_ip: str | None = None) -> None:
        with self._mutation_lock():
            data = self._read()
            if source_ip is not None:
                ip = ipaddress.ip_address(source_ip)
                data.pop(f"{ip}/{ip.max_prefixlen}", None)
            else:
                data = {k: v for k, v in data.items()
                        if v["environment_id"] != environment_id}
            self._write(data)


class ProxyEgress(EgressProvider):
    """Protected network plus a source-policy store consumed by the proxy."""
    def __init__(self, network: str, url: str, *, policy: EgressPolicy | None = None,
                 policy_store: str | Path | None = None):
        if not network or any(c.isspace() for c in network):
            raise ValueError("egress network must be a non-empty runtime network name")
        parsed = urlparse(url)
        if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("egress proxy URL must be an unauthenticated http://host[:port] URL")
        self.network, self.url = network, url.rstrip("/")
        self.policy = policy or EgressPolicy("agent-default", ())
        # Library/test callers get a disposable local default; daemon deployments
        # set the root-owned store path explicitly.
        self.store = JsonPolicyStore(policy_store or Path(tempfile.gettempdir()) / "cawl-egress-policies.json")

    def attachment(self, environment_id: str) -> EgressAttachment:
        return EgressAttachment(self.network)

    def register(self, environment_id: str, source_ip: str, allowed_hosts: tuple[str, ...]) -> None:
        self.store.register(environment_id, source_ip, allowed_hosts)

    def unregister(self, environment_id: str, source_ip: str | None = None) -> None:
        self.store.unregister(environment_id, source_ip)

    def boot_script(self, environment_id: str) -> str:
        url = shlex.quote(self.url)
        return f"""set -e
cat >/etc/profile.d/cawl-egress-proxy.sh <<'EOF'
export https_proxy={url}
export HTTPS_PROXY={url}
export NO_PROXY=localhost,127.0.0.1,::1
export no_proxy=$NO_PROXY
EOF
chmod 644 /etc/profile.d/cawl-egress-proxy.sh
"""


class NetworkEgress(EgressProvider):
    def __init__(self, network: str):
        if not network or any(c.isspace() for c in network):
            raise ValueError("egress network must be a non-empty runtime network name")
        self.network = network

    def attachment(self, environment_id: str) -> EgressAttachment:
        return EgressAttachment(network=self.network)
