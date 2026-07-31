"""Core domain types: the environment state record and its enums."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Status(str, Enum):
    creating = "creating"
    ready = "ready"
    starting = "starting"
    stopping = "stopping"
    destroying = "destroying"
    stopped = "stopped"
    error = "error"
    destroy_failed = "destroy-failed"  # teardown may have left a live workload
    destroyed = "destroyed"  # soft-deleted; kept for history (Django store)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


@dataclass(frozen=True)
class Exposure:
    """One exported port of an environment.

    ``name`` is the handle (`cawl unexpose <id> <name>`) and the **hostname
    label**, globally unique: the exposure answers at ``<name>.<domain>``.
    Freely chosen (`--name acme-preview`); when unset it defaults to the
    environment id, and template ``expose:`` keys default to ``<key>--<id>``.
    ``access`` is the list of email addresses allowed to view it from the
    browser — empty means anyone with access to the environment itself
    (owner, grantees, admins).
    """

    name: str
    port: int
    access: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"name": self.name, "port": self.port, "access": list(self.access)}


# The template expose: key that materializes as the bare-id label — the env's
# "front door", surfaced as Environment.url.
PRIMARY_EXPOSURE = "web"


@dataclass
class Environment:
    """A running (or being-built) materialization of a template.

    The natural key is (template, args_hash, owner) — `args` are the template
    arguments this env was created from, and two envs of the same template
    built from different args are different environments. `id` is the unique
    handle used everywhere else and as the Incus instance / ingress route name.
    """

    id: str
    template: str
    owner: str
    status: Status
    # Which of the deployment's named backends materialized this env — an
    # opaque handle into the operator's registry, recorded so lifecycle ops
    # (exec/stop/start/destroy) route to the right one.
    backend: str = ""
    # Server-selected policy name; never derived from template args or guests.
    egress_policy: str = ""
    args: dict[str, str] = field(default_factory=dict)
    args_hash: str = ""
    vm_ip: str | None = None
    url: str | None = None
    # Derived, never persisted: the ControlPlane fills this from its access
    # provider on every read, so it tracks the current deployment config.
    ssh: str | None = None
    image: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    # Principals the owner has shared this env with. They get the same access the
    # owner has *except* destroying it or changing its TTL — see cawl_core.auth.
    shared_with: tuple[str, ...] = ()
    # Ports exported to the web (see Exposure). Loaded by the state stores.
    exposures: tuple[Exposure, ...] = ()
    # Fencing values assigned by the durable lifecycle store.  They are not
    # client-controlled: a completion may mutate this environment only while
    # both values still identify its active operation.
    generation: str = ""
    operation_id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "template": self.template,
            "args": dict(self.args),
            "args_hash": self.args_hash,
            "backend": self.backend,
            "egress_policy": self.egress_policy,
            "owner": self.owner,
            "status": self.status.value,
            "vm_ip": self.vm_ip,
            "url": self.url,
            "ssh": self.ssh,
            "image": self.image,
            "error": self.error,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "shared_with": list(self.shared_with),
            "exposures": [e.to_dict() for e in self.exposures],
            "generation": self.generation,
        }

    @classmethod
    def from_row(cls, row: dict) -> "Environment":
        # `args` arrives as a dict from the ORM (JSONField) and as JSON text from
        # the raw-SQLite store.
        args = row.get("args") or {}
        if isinstance(args, str):
            args = json.loads(args or "{}")
        return cls(
            id=row["id"],
            template=row["template"],
            args=args,
            args_hash=row.get("args_hash") or "",
            backend=row.get("backend") or "",
            egress_policy=row.get("egress_policy") or "",
            owner=row["owner"],
            status=Status(row["status"]),
            vm_ip=row["vm_ip"],
            url=row["url"],
            ssh=row.get("ssh"),
            image=row["image"],
            error=row["error"],
            created_at=_parse(row["created_at"]),
            expires_at=_parse(row["expires_at"]),
            shared_with=tuple(row.get("shared_with") or ()),
            generation=row.get("generation") or "",
            operation_id=row.get("operation_id") or "",
            exposures=tuple(
                e if isinstance(e, Exposure)
                else Exposure(name=e["name"], port=int(e["port"]),
                              access=tuple(e.get("access") or ()))
                for e in (row.get("exposures") or ())
            ),
        )
