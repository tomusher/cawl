"""Local key material for `cawl ssh`.

The keypair here is a throwaway: it's worth nothing without a certificate, and a
certificate is worth nothing after a few minutes. What actually opens an environment
is the daemon's decision to sign, taken against the grants table at the moment
you ask. So there is no key to distribute, revoke, or clean up off a dead box.

Everything lives beside the login credentials, in ~/.config/cawl.
"""

from __future__ import annotations

import ipaddress
import subprocess
from pathlib import Path

from cawl import credentials


class SshKeyError(Exception):
    pass


def _dir() -> Path:
    d = credentials.path().parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_key() -> Path:
    """The private key `cawl ssh` presents. Generated once, kept indefinitely —
    it's inert on its own, so it isn't a secret worth rotating."""
    key = _dir() / "id_ed25519"
    if key.exists():
        return key
    res = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "cawl", "-f", str(key)],
        capture_output=True, text=True)
    if res.returncode != 0:
        raise SshKeyError(f"could not generate an SSH key: {res.stderr.strip()}")
    return key


def public_key(key: Path) -> str:
    return (key.with_suffix(".pub")).read_text().strip()


def write_cert(instance_id: str, cert: str) -> Path:
    """OpenSSH looks for <key>-cert.pub next to the key, but we keep one cert per
    environment and point at it explicitly — you may hold certs for several at once."""
    p = _dir() / f"{instance_id}-cert.pub"
    p.write_text(cert.strip() + "\n")
    p.chmod(0o600)
    return p


def _host_pattern(host: str) -> str:
    """One CA line covers every box a deployment hands out: a wildcard on the
    host's domain for DNS names. A bare IP (bridge-access deployments) has no
    domain to wildcard — carving off its first octet would produce a pattern
    that also matches unrelated hosts — so it's pinned exactly."""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    return f"*.{host.split('.', 1)[1]}" if "." in host else host


def write_known_hosts(host: str, ca_pubkey: str) -> Path:
    """Trust the CA to vouch for environment host keys, instead of trusting whatever
    key answers first. Sandboxes regenerate host keys on every clone and reuse
    names, so trust-on-first-use here would mean a warning per box."""
    pattern = _host_pattern(host)
    p = _dir() / "known_hosts"
    line = f"@cert-authority {pattern} {ca_pubkey.strip()}\n"
    existing = p.read_text().splitlines(keepends=True) if p.exists() else []
    if line not in existing:
        # Replace any older CA line for this pattern (the CA can be rotated).
        kept = [ln for ln in existing if f"@cert-authority {pattern} " not in ln]
        p.write_text("".join(kept) + line)
        p.chmod(0o600)
    return p


def _conf_path(p: Path) -> str:
    """ssh_config splits on whitespace unless the argument is double-quoted.

    Only for path arguments: ProxyCommand takes the rest of the line and hands
    it to a shell as-is, so quoting *that* would ask sh to run a program whose
    name contains spaces.
    """
    s = str(p)
    return f'"{s}"' if " " in s else s


def ssh_config_block(*, alias: str, host: str, user: str, key: Path, cert: Path,
                     known_hosts: Path, jump: str | None = None,
                     refresh: str | None = None) -> str:
    """The same options `ssh_argv` passes on the command line, written as an
    ssh_config stanza — the form rsync, sshfs, scp and editors can all read.

    `Match host <alias> exec …` rather than a plain `Host`, because the file in
    CertificateFile is only good for minutes. An `exec` criterion runs at the
    moment ssh dials, so `refresh` (a `cawl cert <id>` line) re-mints the cert
    just in time, and a daemon that declines to sign exits non-zero, which
    simply makes the stanza not match. Criteria are tested left to right and
    stop at the first failure, so putting `host` first keeps that command from
    running on every unrelated connection you make.
    """
    head = f"Match host {alias}" + (f' exec "{refresh}"' if refresh else "")
    body = [
        ("HostName", host),
        ("User", user),
        ("IdentityFile", _conf_path(key)),
        ("CertificateFile", _conf_path(cert)),
        ("IdentitiesOnly", "yes"),          # don't offer every key in the agent
        ("UserKnownHostsFile", _conf_path(known_hosts)),
        ("StrictHostKeyChecking", "yes"),   # safe: the CA vouches for the box
        # Off by default, unlike `cawl ssh`: these connections are long-lived
        # (an editor holds one for days) and anyone sharing the box has sudo in
        # it. Add `ForwardAgent yes` yourself if you want to push from inside.
        ("ForwardAgent", "no"),
    ]
    if jump:
        # ProxyCommand, not ProxyJump: the options above would be applied to a
        # ProxyJump hop too, and the jump host isn't in this known_hosts file.
        body.append(("ProxyCommand", f"ssh -W %h:%p {jump}"))
    lines = [head] + [f"    {k} {v}" for k, v in body]
    return "\n".join(lines) + "\n"


def ssh_argv(*, host: str, user: str, key: Path, cert: Path, known_hosts: Path,
             forward_agent: bool, command: list[str] | None = None,
             jump: str | None = None) -> list[str]:
    argv = [
        "ssh",
        "-i", str(key),
        "-o", f"CertificateFile={cert}",
        "-o", "IdentitiesOnly=yes",          # don't offer every key in the agent
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "StrictHostKeyChecking=yes",   # safe: the CA vouches for the box
    ]
    if jump:
        # ProxyCommand, not -J: every option above would apply to a -J hop too,
        # and the jump host is in nobody's cawl known_hosts. A fresh inner ssh
        # uses the user's own config, agent, and known_hosts for the hop, while
        # this outer connection keeps the CA-pinned checks for the box itself.
        argv += ["-o", f"ProxyCommand=ssh -W %h:%p {jump}"]
    if forward_agent:
        argv += ["-o", "ForwardAgent=yes"]
    argv.append(f"{user}@{host}")
    if command:
        argv += ["--", *command]
    return argv
