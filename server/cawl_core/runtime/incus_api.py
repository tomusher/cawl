"""Incus backend that talks the REST API directly over HTTPS with a client
certificate — no `incus` CLI or configured remote on the daemon host.

TLS: presents the daemon's client cert; verifies the server by pinning its
self-signed cert (hostname check off, since the cert's SAN rarely matches the
address we dial). Instance lifecycle uses async operations; `exec` uses
``record-output`` so we get buffered stdout/stderr + the real exit code without
driving the exec websocket.
"""

from __future__ import annotations

import http.client
import json
import shlex
import ssl
import time
from urllib.parse import urlparse

from cawl_core.runtime import sshd
from cawl_core.runtime.base import ExecResult, InstanceInfo, InstanceSpec, Runtime
IMAGES_SERVER = "https://images.linuxcontainers.org"


def _first_ipv4(row: dict) -> str | None:
    """Return the instance's bridge address, never a Docker-in-guest address."""
    net = (row.get("state") or {}).get("network") or {}

    def globals_on(iface) -> list[str]:
        return [a["address"] for a in iface.get("addresses", [])
                if a.get("family") == "inet" and a.get("scope") == "global"]

    # eth0 is normal for containers; enp5s0 is the common VM primary NIC.
    for name in ("eth0", "enp5s0"):
        if name in net and (addrs := globals_on(net[name])):
            return addrs[0]
    for name, iface in net.items():
        if name == "lo" or name.startswith(("docker", "br-", "veth", "cni")):
            continue
        if addrs := globals_on(iface):
            return addrs[0]
    return None


class IncusApiError(RuntimeError):
    """An Incus failure, retaining the HTTP status when one is available."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def not_found(self) -> bool:
        return self.status_code == 404


class IncusApiRuntime(Runtime):
    def __init__(self, endpoint: str, client_cert: str, client_key: str,
                 server_cert: str, *, project: str = "default",
                 image_prefix: str = "cawl", timeout: int = 300,
                 vm: bool = False):
        u = urlparse(endpoint)
        self.host = u.hostname
        self.port = u.port or 8443
        self.project = project
        self.image_prefix = image_prefix
        self.timeout = timeout
        # One adapter, one materialization: a deployment offering both KVM VMs
        # and system containers registers this class twice, as two backends.
        self.vm = vm

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_cert_chain(client_cert, client_key)
        ctx.check_hostname = False  # self-signed; SAN won't match the address
        # The Incus server's self-signed certificate is an explicit trust pin.
        # Never silently downgrade this privileged client to unauthenticated TLS.
        ctx.load_verify_locations(server_cert)
        ctx.verify_mode = ssl.CERT_REQUIRED
        self._ctx = ctx

    # -- HTTP plumbing -----------------------------------------------------
    def _request(self, method: str, path: str, body=None, raw: bool = False):
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}project={self.project}"
        conn = http.client.HTTPSConnection(
            self.host, self.port, context=self._ctx, timeout=self.timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if data else {}
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
        finally:
            conn.close()
        if raw:
            return resp.status, payload
        doc = json.loads(payload) if payload else {}
        if resp.status >= 400 or doc.get("type") == "error":
            error = doc.get("error") or doc.get("error_code") or "request failed"
            raise IncusApiError(
                f"{method} {path}: {error}", status_code=resp.status)
        return resp.status, doc

    def _sync(self, method: str, path: str, body=None):
        _, doc = self._request(method, path, body)
        return doc.get("metadata", {})

    def _op(self, method: str, path: str, body=None) -> dict:
        """Run an async request and wait for the operation; return its metadata."""
        _, doc = self._request(method, path, body)
        if doc.get("type") != "async":
            return doc.get("metadata", {})
        op = doc["operation"]  # e.g. /1.0/operations/<uuid>
        _, wait = self._request("GET", f"{op}/wait?timeout={self.timeout}")
        operation = wait.get("metadata", {})
        if operation.get("status_code") != 200:
            raise IncusApiError(
                operation.get("err") or operation.get("status") or "operation failed",
                status_code=operation.get("status_code"),
            )
        return operation

    # -- Runtime -----------------------------------------------------------
    def image_name(self, name: str) -> str:
        ref = name if "/" in name else f"{self.image_prefix}/{name}"
        return ref + ("-vm" if self.vm else "")

    def _type_and_config(self) -> tuple[str, dict]:
        if self.vm:
            return "virtual-machine", {"security.secureboot": "false"}
        return "container", {"security.nesting": "true"}

    def create(self, spec: InstanceSpec) -> InstanceInfo:
        itype, config = self._type_and_config()
        body = {
            "name": spec.id, "type": itype,
            "source": {"type": "image", "alias": spec.image},
            "config": config,
        }
        if spec.egress_network:
            # Overrides the profile's eth0 so a workload cannot land on the
            # default/NAT bridge by accident.
            body["devices"] = {"eth0": {
                "type": "nic", "network": spec.egress_network, "name": "eth0",
            }}
        self._op("POST", "/1.0/instances", body)
        self._op("PUT", f"/1.0/instances/{spec.id}/state",
                 {"action": "start", "timeout": self.timeout})
        info = self._wait_for_ip(spec)
        self._provision(spec)
        return info

    def _provision(self, spec: InstanceSpec) -> None:
        """Everything a *new* box needs — the parts that persist on its disk."""
        if spec.authorized_keys:
            keys = "\n".join(spec.authorized_keys)
            self._sh(spec.id, f"""set -e
                u=dev
                install -d -m700 -o "$u" -g "$u" /home/"$u"/.ssh
                printf '%s\\n' {shlex.quote(keys)} >> /home/"$u"/.ssh/authorized_keys
                chown "$u":"$u" /home/"$u"/.ssh/authorized_keys
            """)
        # Teach sshd the CA. The matching host cert is installed by the control
        # plane once it has signed this box's host key (sshd won't start with a
        # HostCertificate that doesn't exist yet). Both live on disk, so a
        # stop/start keeps them — only _boot has to run again.
        if spec.ssh_ca_pubkey:
            self._sh(spec.id, sshd.trust_script(
                spec.ssh_ca_pubkey, principal=spec.id, login_user=spec.ssh_user))
        self._boot(spec)

    def _boot(self, spec: InstanceSpec) -> None:
        """Everything that has to happen on *every* boot — create and resume alike.

        Nothing here survives a shutdown: network membership is typically
        ephemeral, and Docker won't bring back containers that carry no restart
        policy.
        """
        # The access provider's join script (see cawl_core/access.py) — rendered
        # daemon-side; the backend runs it as-is like any other hook.
        if spec.network_boot:
            self._sh(spec.id, spec.network_boot)
        if spec.egress_boot:
            self._sh(spec.id, spec.egress_boot)
        # Docker is part of every cawl base image, so wait for it (bounded) before
        # handing over to the template — otherwise every hook opens with this loop.
        self._sh(spec.id, "for i in $(seq 1 60); do docker info >/dev/null 2>&1 "
                          "&& exit 0; sleep 1; done; echo 'docker not ready' >&2; exit 1")
        if spec.provision:
            self._sh(spec.id, f"set -e\n{spec.provision}")

    def stop(self, id: str) -> None:
        self._op("PUT", f"/1.0/instances/{id}/state",
                 {"action": "stop", "timeout": 30})   # graceful; no --force

    def start(self, spec: InstanceSpec) -> InstanceInfo:
        self._op("PUT", f"/1.0/instances/{spec.id}/state",
                 {"action": "start", "timeout": self.timeout})
        info = self._wait_for_ip(spec)
        self._boot(spec)
        return info

    def destroy(self, id: str) -> None:
        # Let Incus atomically stop and remove a running instance. A separate
        # stop request cannot safely distinguish "already stopped" from a real
        # backend failure and introduces a race before deletion.
        try:
            self._op("DELETE", f"/1.0/instances/{id}?force=1")
        except IncusApiError as exc:
            # Idempotent cleanup treats only Incus' explicit not-found response
            # as success. Timeouts, authorization errors, and server failures
            # must reach the control plane so it does not forget a live orphan.
            if not exc.not_found:
                raise

    def exec(self, id: str, cmd: list[str]) -> ExecResult:
        # A VM start operation can complete before QEMU and its in-guest Incus
        # agent accept exec requests. This is independent of DHCP/egress.
        deadline = time.monotonic() + min(self.timeout, 60)
        while True:
            try:
                op = self._op("POST", f"/1.0/instances/{id}/exec", {
                    "command": cmd, "wait-for-websocket": False,
                    "record-output": True, "interactive": False,
                })
                break
            except IncusApiError as exc:
                transient = (
                    "VM agent isn't currently running" in str(exc)
                    or "Instance is not running" in str(exc)
                )
                if not transient or time.monotonic() >= deadline:
                    raise
                time.sleep(1)
        meta = op.get("metadata", {})
        out = self._fetch_log(meta.get("output", {}).get("1"))
        err = self._fetch_log(meta.get("output", {}).get("2"))
        return ExecResult(meta.get("return", -1), out, err)

    def _sh(self, id: str, script: str) -> None:
        res = self.exec(id, ["sh", "-lc", script])
        if res.exit_code != 0:
            raise IncusApiError(f"provision step failed ({res.exit_code}): {res.stderr}")

    def _fetch_log(self, path: str | None) -> str:
        if not path:
            return ""
        status, data = self._request("GET", path, raw=True)
        if status >= 400:
            return ""
        self._request("DELETE", path, raw=True)  # tidy up the record file
        return data.decode(errors="replace")

    def info(self, id: str) -> InstanceInfo:
        try:
            state = self._sync("GET", f"/1.0/instances/{id}/state")
        except IncusApiError:
            return InstanceInfo(ip=None, status="unknown")
        return InstanceInfo(ip=_first_ipv4({"state": state}),
                            status=state.get("status", "unknown"))

    def _wait_for_ip(self, spec: InstanceSpec) -> InstanceInfo:
        info = self.info(spec.id)
        # The address is persisted for bridge SSH even when egress is disabled.
        # DHCP/guest-agent state often lags the successful start operation,
        # especially for VMs, so wait before recording the environment state.
        deadline = time.monotonic() + min(self.timeout, 60)
        while not info.ip and time.monotonic() < deadline:
            time.sleep(1)
            info = self.info(spec.id)
        if not info.ip:
            # Proxy egress cannot safely run without a source address. Other
            # access modes may still become reachable independently (for
            # example through Tailscale), so do not fail their startup here.
            if spec.egress_ready:
                raise IncusApiError(
                    "started instance has no workload IP after 60 seconds; "
                    "check its Incus NIC and DHCP network"
                )
            return info
        if spec.egress_ready:
            spec.egress_ready(info.ip)
        return info

    def build_image(self, spec: InstanceSpec) -> str:
        builder = f"{spec.image.replace('/', '-')}-builder"
        _, config = self._type_and_config()
        self._op("POST", "/1.0/instances", {
            "name": builder, "type": self._type_and_config()[0],
            "source": {"type": "image", "mode": "pull", "server": IMAGES_SERVER,
                       "protocol": "simplestreams", "alias": "ubuntu/24.04/cloud"},
            "config": config,
        })
        self._op("PUT", f"/1.0/instances/{builder}/state",
                 {"action": "start", "timeout": self.timeout})
        if spec.build:
            self._sh(builder, "until docker info >/dev/null 2>&1; do sleep 1; done")
            # Cloning the app, baking its images, seeding its DB: all the template's
            # business. Whatever state the hook leaves behind is what gets published.
            self._sh(builder, f"set -e\n{spec.build}")
        self._op("PUT", f"/1.0/instances/{builder}/state",
                 {"action": "stop", "force": True, "timeout": 30})

        # Replace any existing alias, then publish the builder as the image.
        self._request("DELETE", f"/1.0/images/aliases/{spec.image}", raw=True)
        self._op("POST", "/1.0/images", {
            "source": {"type": "instance", "name": builder},
            "aliases": [{"name": spec.image}],
        })
        self._op("DELETE", f"/1.0/instances/{builder}")
        return spec.image
