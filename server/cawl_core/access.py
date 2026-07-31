"""How a user's machine reaches a box's sshd — the access layer.

An access provider owns *transport only*: whatever network a box joins at boot,
and the name or address clients dial. Authentication never lives here — that is
the SSH CA's job (ssh_ca.py, runtime/sshd.py), and it works the same over any
transport.

The provider is the operator's choice, made once per deployment in settings —
never per template. Templates describe applications; wiring a network stack (and
its join credential) into template hooks would hand a network-wide secret to
user-authored scripts, and would let two templates on one server demand two
different client setups.
"""

from __future__ import annotations

import shlex
from abc import ABC, abstractmethod


class AccessProvider(ABC):
    """The two questions the control plane asks about SSH transport."""

    def boot_script(self, environment_id: str) -> str:
        """Shell to run inside the box on *every* boot, create and resume alike —
        network membership rarely survives a shutdown. Rendered daemon-side, so
        any credential in it never appears in a template. Empty = nothing to do.
        """
        return ""

    @abstractmethod
    def ssh_host(self, environment_id: str, ip: str | None) -> str | None:
        """What clients dial to reach the box's sshd — also the name its host
        cert is signed for. None when the box has no reachable address."""

    def ssh_jump(self, environment_id: str) -> str | None:
        """A hop clients must relay through to reach ``ssh_host`` (a
        ``[user@]host[:port]`` for ProxyJump/ProxyCommand), or None when the
        host is dialed directly. Authenticating *to* the hop is not cawl's
        business — the hop only carries packets, like any other transport."""
        return None


class BridgeAccess(AccessProvider):
    """No agent in the box: clients dial the instance's bridge IP directly.

    For deployments where users already have a route to the Incus bridge — the
    same host, a LAN, or the operator's own VPN (WireGuard, NetBird, …). cawl
    doesn't need to know which; routing is the operator's business.
    """

    def ssh_host(self, environment_id: str, ip: str | None) -> str | None:
        return ip


class JumpAccess(BridgeAccess):
    """Bridge access relayed through a jump host — typically the daemon host
    itself, or any box with a route to the Incus bridge.

    Developers authenticate to the jump with whatever accounts the operator
    already runs there; cawl neither knows nor cares. The security story is
    unchanged: the final hop is still the box's own sshd, which accepts nothing
    but a cert the daemon signed — jump access without a grant opens no doors.
    """

    def __init__(self, jump: str):
        self.jump = jump  # [user@]host[:port]

    def ssh_jump(self, environment_id: str) -> str | None:
        return self.jump


class TailscaleAccess(AccessProvider):
    """Boxes join a tailnet at boot; clients dial `<id>.<tailnet>` (MagicDNS).

    The authkey should be *ephemeral*, reusable, and pre-authorized: ephemeral
    means a stopped node is dropped and its name freed — which is why the join
    runs on every boot, not once at create.
    """

    def __init__(self, authkey: str, tailnet: str = "ts.net", tags: str = ""):
        self.authkey = authkey
        self.tailnet = tailnet
        self.tags = tags

    def boot_script(self, environment_id: str) -> str:
        # Join as the network only. Authentication stays sshd's job, so no
        # --ssh: Tailscale SSH would claim port 22 and authenticate from tailnet
        # identity, which knows nothing about who owns this environment.
        tags = (f" --advertise-tags={shlex.quote(self.tags)}" if self.tags else "")
        return f"""set -e
            systemctl start tailscaled 2>/dev/null || true
            for i in $(seq 1 30); do
                systemctl is-active --quiet tailscaled && break
                sleep 1
            done
            tailscale up --authkey={shlex.quote(self.authkey)} \
                --accept-dns=false --hostname={shlex.quote(environment_id)}{tags}
        """

    def ssh_host(self, environment_id: str, ip: str | None) -> str | None:
        # The MagicDNS name, not the IP — it survives stop/start and IP moves.
        return f"{environment_id}.{self.tailnet}"
