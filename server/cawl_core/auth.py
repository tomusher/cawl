"""Authentication principals and the authorization policy.

Identity *resolution* here is the local-mode convenience: it trusts the OS user
or ``CAWL_ACTOR``. The real deployment runs a control-plane daemon that derives
the principal from the authenticated transport (Tailscale WhoIs / a scoped
token) and ignores anything the client claims — the CLI must never be the trust
boundary. The *policy* functions below are what the daemon enforces.
"""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass
from enum import Enum

from cawl_core.errors import PermissionDenied
from cawl_core.models import Environment


class Role(str, Enum):
    user = "user"
    admin = "admin"


@dataclass(frozen=True)
class Principal:
    id: str
    role: Role = Role.user

    @property
    def is_admin(self) -> bool:
        return self.role is Role.admin


# The reaper and other internal callers act as the system (an admin).
SYSTEM = Principal("system", Role.admin)


# -- policy ---------------------------------------------------------------
# Two tiers, because sharing shouldn't hand over the keys entirely: a grantee can
# use an env (see it, exec in it, SSH to it) but not destroy it or extend its
# life. Those stay with whoever created it — and with admins.
def can_view(actor: Principal, inst: Environment) -> bool:
    """A principal may see/use an env if they own it, hold a grant, or are admin."""
    return actor.is_admin or is_owner(actor, inst) or actor.id in inst.shared_with


def is_owner(actor: Principal, inst: Environment) -> bool:
    return actor.is_admin or inst.owner == actor.id


def require_access(actor: Principal, inst: Environment) -> None:
    if not can_view(actor, inst):
        raise PermissionDenied(f"{actor.id!r} may not access {inst.id}")


def require_owner(actor: Principal, inst: Environment,
                  action: str = "perform this action") -> None:
    """For the irreversible ops. A shared env is still someone's env."""
    if not is_owner(actor, inst):
        raise PermissionDenied(
            f"{actor.id!r} must own {inst.id} to {action} "
            f"(it is shared with you, not yours)"
        )


def require_admin(actor: Principal, action: str = "perform this action") -> None:
    if not actor.is_admin:
        raise PermissionDenied(f"{actor.id!r} must be an admin to {action}")


# -- local-mode identity resolution (NOT the daemon's trust boundary) -----
def resolve_principal(admins: frozenset[str], env: dict | None = None) -> Principal:
    e = env if env is not None else os.environ
    ident = e.get("CAWL_ACTOR")
    if not ident:
        try:
            ident = getpass.getuser()
        except Exception:  # noqa: BLE001
            ident = "unknown"
    role = Role.admin if ident in admins else Role.user
    return Principal(ident, role)
