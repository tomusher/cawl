"""The SSH certificate authority: how an ownership decision reaches a VM.

The trick that keeps this simple is what the *principal* means. An environment's
``authorized_principals`` file names **the environment itself** (its id) —
never a list of people — and is written once at create. So a cert saying "good
for `web-test`" opens `web-test` and nothing else, and the question of *who* may
hold such a cert is answered here, in the daemon, against the database, at
signing time.

That's what makes ad-hoc sharing cheap: granting someone access is a row in the
grants table, not a write into a running box. Nothing has to be reconciled, and
a stopped or unreachable environment is still shareable. Revocation costs at most
one cert lifetime (minutes) for new connections.

Host certs are signed by the same CA so `cawl ssh` can verify the box without
trust-on-first-use — environments regenerate their host keys on every clone, and
instance names get recycled, so TOFU would mean a stream of key-mismatch
warnings that trains everyone to ignore them.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from cawl_core.errors import CawlError

# One line: "<type> <base64> [comment]". We write this to a file for ssh-keygen,
# so no embedded newline can smuggle in a second key.
_PUBKEY = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|"
    r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r"\s+[A-Za-z0-9+/=]+(\s+\S.*)?$"
)


class SshCaError(CawlError):
    """The CA could not sign (bad key material, missing/unusable CA key)."""


def _clean_pubkey(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) != 1 or not _PUBKEY.match(lines[0]):
        raise SshCaError("not a single valid SSH public key")
    return lines[0]


class SshCertAuthority:
    """Signs short-lived user certs and long-lived host certs with one CA key.

    The key must be unencrypted (the daemon signs unattended) and readable only
    by the daemon — it mints access to every environment, so it wants filesystem
    permissions at least as tight as the Incus client key next to it.
    """

    def __init__(self, key_path: str | Path, *, user_ttl: str = "10m",
                 host_ttl: str = "52w"):
        self.key_path = Path(key_path)
        self.user_ttl = user_ttl
        self.host_ttl = host_ttl

    @property
    def pubkey(self) -> str:
        """The CA's public key — baked into each VM, and pinned by `cawl ssh`
        as a @cert-authority line so host certs verify."""
        pub = self.key_path.with_suffix(self.key_path.suffix + ".pub")
        try:
            return pub.read_text().strip()
        except OSError as e:
            raise SshCaError(f"cannot read CA public key {pub}: {e}") from e

    def _sign(self, public_key: str, *, key_id: str, principals: list[str],
              ttl: str, host: bool, options: list[str]) -> str:
        if not self.key_path.exists():
            raise SshCaError(f"no CA key at {self.key_path}")
        for p in principals:
            if not p or re.search(r"[\s,]", p):
                raise SshCaError(f"bad certificate principal {p!r}")
        key = _clean_pubkey(public_key)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "id.pub"
            target.write_text(key + "\n")
            cmd = ["ssh-keygen", "-q", "-s", str(self.key_path),
                   "-I", key_id, "-n", ",".join(principals), "-V", f"+{ttl}"]
            if host:
                cmd.append("-h")
            for opt in options:
                cmd += ["-O", opt]
            cmd.append(str(target))
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise SshCaError(f"ssh-keygen: {res.stderr.strip() or 'signing failed'}")
            return (Path(tmp) / "id-cert.pub").read_text().strip()

    def sign_user(self, public_key: str, *, environment_id: str, actor: str) -> str:
        """A cert that opens exactly one environment, for a few minutes.

        The principal is the instance — that's the whole authorization. The
        key id carries the human (or agent) purely so sshd's log, and ours,
        record who actually walked through the door.
        """
        return self._sign(
            public_key, key_id=f"{actor}@{environment_id}", principals=[environment_id],
            ttl=self.user_ttl, host=False,
            # Agent forwarding is how a forwarded git identity reaches the box
            # without keys ever landing in it; nothing else is granted.
            options=["clear", "permit-pty", "permit-agent-forwarding"],
        )

    def sign_host(self, public_key: str, *, environment_id: str,
                  hostnames: list[str]) -> str:
        return self._sign(
            public_key, key_id=environment_id, principals=hostnames,
            ttl=self.host_ttl, host=True, options=[],
        )
