"""The runtime contract. Everything above only ever calls these methods, so
Incus / SmolVM / a fake are interchangeable behind it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class InstanceSpec:
    """Everything a backend needs to materialize one environment.

    The hooks arrive already rendered (args substituted, values shell-quoted) —
    a backend runs them as-is and never looks inside. That's the whole reason a
    backend knows nothing about git, compose, or any other app concern.
    """

    id: str
    template: str
    image: str
    provision: str = ""  # shell run in each new env, once Docker is up
    build: str = ""      # shell run in the builder when baking the golden image
    # Public keys to seed into the VM's authorized_keys — a static-key escape
    # hatch, independent of the CA below.
    authorized_keys: list[str] = field(default_factory=list)
    # The SSH CA the box should trust. Its principal is the instance's own id, so
    # a cert the daemon signs for this box opens this box and no other. Empty
    # disables cert auth (no CA configured).
    ssh_ca_pubkey: str = ""
    ssh_user: str = "dev"  # the login account certs are accepted for
    # The access provider's join-the-network script, run on every boot before
    # the provision hook. Rendered daemon-side (see cawl_core/access.py), so a
    # backend runs it as-is like the other hooks — it never knows which network
    # stack is in play. Empty for providers with no in-box agent, and for the
    # throwaway builder that bakes golden images (nobody dials in).
    network_boot: str = ""
    # Opaque, deployment-selected outbound-network attachment. The runtime
    # attaches it outside the guest; guest proxy configuration is not security.
    egress_network: str = ""
    # Optional client setup for compatible tools. Direct egress must remain
    # blocked by egress_network even if this script is removed by the guest.
    egress_boot: str = ""
    # Trusted control-plane callback. Runtime invokes it after the workload IP
    # exists and before _boot/provision; guests can neither supply nor bypass it.
    egress_ready: Callable[[str], None] | None = None


@dataclass
class InstanceInfo:
    ip: str | None
    status: str


@dataclass
class ExecResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class Runtime(ABC):
    """A compute backend that turns an InstanceSpec into a running box.

    This is a public extension point: implement these methods and name your
    class in the daemon's config (see docs/extending.md; FakeRuntime is the
    reference implementation, test_incus_api.py the expected orderings). The
    signatures don't capture the whole contract — what the control plane
    additionally relies on:

    - ``info().ip`` is load-bearing: Traefik proxies exposures to it and the
      bridge/jump access providers dial it, so it must be an address the
      daemon host can route to — not a NAT-hidden or cloud-internal one.
    - ``exec`` is the control plane's out-of-band channel (CA trust, host-cert
      install, hooks). It must work with no SSH and no access provider — it's
      what bootstraps both — and must pass through the real exit code.
    - ``egress_network`` is enforced outside the guest by the runtime/network
      adapter. ``create`` then splits provisioning in two: on-disk things once
      (keys, CA trust), then the every-boot things (``network_boot``,
      ``egress_boot``, wait for Docker, ``provision``) — in that order.
    - ``start`` replays that every-boot half; see its docstring.
    """

    @abstractmethod
    def image_name(self, name: str) -> str:
        """Golden image alias for a template (or an explicit base alias).

        Per backend: images are built *by* a backend *for* that backend, so a
        deployment running several registers each with its own image lineage
        (the Incus adapters suffix `-vm` in VM mode, for instance)."""

    @abstractmethod
    def build_image(self, spec: InstanceSpec) -> str:
        """Bake the golden image: boot a builder, run the build hook, publish."""

    @abstractmethod
    def create(self, spec: InstanceSpec) -> InstanceInfo:
        """Clone the golden image, boot it, run the provision hook."""

    @abstractmethod
    def stop(self, id: str) -> None:
        """Shut the box down but keep its disk. Frees the RAM, not the storage."""

    @abstractmethod
    def start(self, spec: InstanceSpec) -> InstanceInfo:
        """Boot a stopped box back up and make it usable again.

        Takes a spec, not an id, because a resume is not just `power on`: the
        box's network membership rarely survives a shutdown (an ephemeral
        tailnet node is dropped and has to re-join or its name never comes
        back) — and the app itself has to be brought up, since Docker won't
        restart containers that have no restart policy. So the boot half of
        provisioning runs again.
        """

    @abstractmethod
    def destroy(self, id: str) -> None:
        ...

    @abstractmethod
    def exec(self, id: str, cmd: list[str]) -> ExecResult:
        """Run a command inside the instance; passes through the inner exit code."""

    @abstractmethod
    def info(self, id: str) -> InstanceInfo:
        ...
