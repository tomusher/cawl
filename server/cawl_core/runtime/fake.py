"""In-memory runtime for tests and for developing cawl itself without Incus.

Optionally persists its instance map to a JSON file so state survives across
separate CLI invocations (real VMs do); tests use the in-memory default.
"""

from __future__ import annotations

import json
from pathlib import Path

from cawl_core.runtime import sshd
from cawl_core.runtime.base import ExecResult, InstanceInfo, InstanceSpec, Runtime

# A real (throwaway) ed25519 public key, so a fake box answers the host-key read
# the way a real one does. Without it, pointing the fake runtime at a configured
# CA fails at create with "not a single valid SSH public key" — the fake has to
# behave like a box, or it only tests the paths where SSH is switched off.
FAKE_HOST_PUBKEY = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDQvJPekHrGv8sn/sc6Maqp7"
                    "iLXcSfGjzogeD+VEileH fake-sandbox-host")


class FakeRuntime(Runtime):
    def __init__(self, state_path: str | Path | None = None,
                 host_pubkey: str = FAKE_HOST_PUBKEY, vm: bool = False):
        self.state_path = Path(state_path) if state_path else None
        self.vm = vm
        self.instances: dict[str, str] = {}  # id -> ip
        self.specs: dict[str, InstanceSpec] = {}  # id -> spec (in-process only)
        self.images: set[str] = set()
        self.execs: list[tuple[str, list[str]]] = []
        self.stopped: set[str] = set()
        self.booted: list[str] = []   # ids resumed, in order (in-process only)
        # What `cat /etc/ssh/ssh_host_ed25519_key.pub` returns, so the control
        # plane's host-cert signing can be exercised without a real box.
        self.host_pubkey = host_pubkey
        self._next_ip = 10
        self._load()

    def _load(self):
        if self.state_path and self.state_path.exists():
            data = json.loads(self.state_path.read_text())
            self.instances = data.get("instances", {})
            self.images = set(data.get("images", []))
            self._next_ip = data.get("next_ip", 10)

    def _save(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "instances": self.instances,
            "images": sorted(self.images),
            "next_ip": self._next_ip,
        }))

    def image_name(self, name: str) -> str:
        ref = name if "/" in name else f"fake/{name}"
        return ref + ("-vm" if self.vm else "")

    def build_image(self, spec: InstanceSpec) -> str:
        self.images.add(spec.image)
        self._save()
        return spec.image

    def create(self, spec: InstanceSpec) -> InstanceInfo:
        ip = f"10.0.0.{self._next_ip}"
        self._next_ip += 1
        self.instances[spec.id] = ip
        self.specs[spec.id] = spec
        self._save()
        if spec.egress_ready:
            spec.egress_ready(ip)
        return InstanceInfo(ip=ip, status="running")

    def stop(self, id: str) -> None:
        self.instances.pop(id, None)   # a stopped box has no IP
        self.stopped.add(id)
        self._save()

    def start(self, spec: InstanceSpec) -> InstanceInfo:
        # A fresh IP on resume — real DHCP usually re-leases the same one, but the
        # control plane must not depend on that, so the fake never gives it back.
        ip = f"10.0.0.{self._next_ip}"
        self._next_ip += 1
        self.instances[spec.id] = ip
        self.specs[spec.id] = spec
        self.stopped.discard(spec.id)
        self.booted.append(spec.id)
        self._save()
        if spec.egress_ready:
            spec.egress_ready(ip)
        return InstanceInfo(ip=ip, status="running")

    def destroy(self, id: str) -> None:
        self.instances.pop(id, None)
        self.stopped.discard(id)
        self._save()

    def exec(self, id: str, cmd: list[str]) -> ExecResult:
        self.execs.append((id, cmd))
        if id not in self.instances:
            return ExecResult(1, "", f"no such instance: {id}")
        if self.host_pubkey and cmd == sshd.read_host_pubkey_cmd():
            return ExecResult(0, self.host_pubkey + "\n", "")
        return ExecResult(0, f"ran: {' '.join(cmd)}\n", "")

    def info(self, id: str) -> InstanceInfo:
        ip = self.instances.get(id)
        return InstanceInfo(ip=ip, status="running" if ip else "unknown")
