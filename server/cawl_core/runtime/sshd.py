"""The sshd side of certificate auth, as shell a backend can run in a box.

Shared by both Incus runtimes so the trust setup is written once. Two phases,
and the order is forced by sshd itself: it refuses to start with a
``HostCertificate`` pointing at a file that isn't there, and the box's host key
only exists once the box is up. So:

  1. ``trust_script`` (at provision) — teach sshd the CA and tell it this box's
     principal is its own instance id.
  2. ``host_cert_script`` (once the daemon has signed the host key) — install
     the host cert, so `cawl ssh` verifies the box instead of prompting.

A box thus never learns who its users are. It only learns which box it is.
"""

from __future__ import annotations

import shlex

CA_KEY = "/etc/ssh/cawl_ca.pub"
PRINCIPALS_DIR = "/etc/ssh/authorized_principals"
HOST_KEY_PUB = "/etc/ssh/ssh_host_ed25519_key.pub"
HOST_CERT = "/etc/ssh/ssh_host_ed25519_key-cert.pub"

# Ubuntu's stock sshd_config ends with `Include /etc/ssh/sshd_config.d/*.conf`,
# so drop-ins are the supported way in and survive an image rebuild. The dir is
# created rather than assumed — an image without it would otherwise fail to
# provision, and the failure would look like a networking problem.
CONF_DIR = "/etc/ssh/sshd_config.d"
_USER_CA_CONF = f"{CONF_DIR}/10-cawl-ca.conf"
_HOST_CERT_CONF = f"{CONF_DIR}/20-cawl-host.conf"

# `ssh reload` on Ubuntu; the fallbacks cover distros/units that name it sshd.
_RELOAD = ("systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null "
           "|| systemctl restart ssh 2>/dev/null || systemctl restart sshd")


def trust_script(ca_pubkey: str, *, principal: str, login_user: str = "dev") -> str:
    """Trust the CA, and declare this box's principal — its own instance id.

    The principals file is the only place the box states who may enter, and it
    names the *environment*, not a person. It is written once and never edited, so
    sharing and revoking an environment never touch it.
    """
    q_ca, q_principal = shlex.quote(ca_pubkey), shlex.quote(principal)
    q_user = shlex.quote(login_user)
    return f"""set -e
        printf '%s\\n' {q_ca} > {CA_KEY}
        chmod 644 {CA_KEY}
        install -d -m755 {CONF_DIR}
        install -d -m755 {PRINCIPALS_DIR}
        printf '%s\\n' {q_principal} > {PRINCIPALS_DIR}/{q_user}
        chmod 644 {PRINCIPALS_DIR}/{q_user}
        cat > {_USER_CA_CONF} <<'EOF'
# cawl: a user cert signed by the CA, whose principal is this instance id, is
# the only way in. The daemon decides who gets one.
TrustedUserCAKeys {CA_KEY}
AuthorizedPrincipalsFile {PRINCIPALS_DIR}/%u
PubkeyAuthentication yes
PasswordAuthentication no
EOF
        chmod 644 {_USER_CA_CONF}
        # The golden image ships without host keys (they're wiped so clones don't
        # share an identity), so make sure this box has generated its own.
        ssh-keygen -A
        {_RELOAD}
    """


def read_host_pubkey_cmd() -> list[str]:
    return ["cat", HOST_KEY_PUB]


def host_cert_script(cert: str) -> str:
    """Install the CA-signed host cert. Without this, every fresh environment — and
    every reused instance name — greets its owner with a host-key warning."""
    return f"""set -e
        printf '%s\\n' {shlex.quote(cert)} > {HOST_CERT}
        chmod 644 {HOST_CERT}
        cat > {_HOST_CERT_CONF} <<'EOF'
HostCertificate {HOST_CERT}
EOF
        chmod 644 {_HOST_CERT_CONF}
        {_RELOAD}
    """
