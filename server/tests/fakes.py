"""Test doubles shared by the cawl_core unit tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import uuid

from cawl_core.errors import QuotaExceeded
from cawl_core.models import Environment, Exposure, Status


class FakeStateStore:
    """In-memory test double for the production Django state store."""

    def __init__(self):
        self._environments: dict[str, Environment] = {}
        self._grants: dict[str, set[str]] = {}
        self._exposures: dict[str, dict[str, Exposure]] = {}

    def _environment(self, inst: Environment) -> Environment:
        result = deepcopy(inst)
        result.shared_with = tuple(sorted(self._grants.get(inst.id, ())))
        result.exposures = tuple(
            deepcopy(exposure)
            for _, exposure in sorted(self._exposures.get(inst.id, {}).items())
        )
        return result

    def reserve(self, inst: Environment, quota: int | None = None) -> bool:
        if quota is not None and self.count_active(inst.owner) >= quota:
            raise QuotaExceeded(f"owner {inst.owner!r} at quota ({quota})")
        held = self._environments.get(inst.id)
        if held is not None and held.status not in (Status.destroyed, Status.error):
            return False
        exposed = self.find_exposure(inst.id)
        if exposed and exposed[0] != inst.id:
            return False
        self._grants.pop(inst.id, None)
        self._exposures.pop(inst.id, None)
        inst.generation = inst.generation or str(uuid.uuid4())
        inst.operation_id = str(uuid.uuid4())
        self._environments[inst.id] = deepcopy(inst)
        return True

    def begin_operation(self, id: str, kind: str, expected: tuple[Status, ...],
                        transitional: Status) -> Environment | None:
        inst = self._environments.get(id)
        if (not inst or inst.status not in expected or inst.operation_id):
            return None
        inst = deepcopy(inst)
        inst.status = transitional
        inst.operation_id = str(uuid.uuid4())
        self._environments[id] = deepcopy(inst)
        return self._environment(inst)

    def complete_operation(self, inst: Environment, *, succeeded: bool) -> bool:
        held = self._environments.get(inst.id)
        if not held or held.generation != inst.generation or held.operation_id != inst.operation_id:
            return False
        inst.operation_id = ""
        self._environments[inst.id] = deepcopy(inst)
        return True

    def upsert(self, inst: Environment) -> None:
        self._environments[inst.id] = deepcopy(inst)

    def get(self, id: str) -> Environment | None:
        inst = self._environments.get(id)
        return self._environment(inst) if inst else None

    def find(self, template: str, args_hash: str, owner: str) -> Environment | None:
        matches = (
            inst for inst in self._environments.values()
            if inst.template == template and inst.args_hash == args_hash
            and inst.owner == owner and inst.status is not Status.destroyed
        )
        inst = max(matches, key=lambda item: item.created_at or datetime.min, default=None)
        return self._environment(inst) if inst else None

    def list(self, *, template=None, owner=None, visible_to=None) -> list[Environment]:
        environments = []
        for inst in self._environments.values():
            if inst.status is Status.destroyed:
                continue
            if template and inst.template != template:
                continue
            if owner and inst.owner != owner:
                continue
            if (visible_to and inst.owner != visible_to
                    and visible_to not in self._grants.get(inst.id, ())):
                continue
            environments.append(self._environment(inst))
        return sorted(environments, key=lambda item: item.created_at or datetime.min,
                      reverse=True)

    def expired(self, now: datetime) -> list[Environment]:
        return [
            self._environment(inst) for inst in self._environments.values()
            if inst.status is not Status.destroyed
            and inst.expires_at is not None and inst.expires_at <= now
        ]

    def grants(self, instance_id: str) -> list[str]:
        return sorted(self._grants.get(instance_id, ()))

    def grant(self, instance_id: str, principal: str, *, granted_by: str = "") -> None:
        self._grants.setdefault(instance_id, set()).add(principal)

    def revoke(self, instance_id: str, principal: str) -> bool:
        grants = self._grants.get(instance_id, set())
        if principal not in grants:
            return False
        grants.remove(principal)
        return True

    def exposures(self, instance_id: str) -> list[Exposure]:
        return [
            deepcopy(exposure)
            for _, exposure in sorted(self._exposures.get(instance_id, {}).items())
        ]

    def set_exposure(self, instance_id: str, exposure: Exposure) -> None:
        self._exposures.setdefault(instance_id, {})[exposure.name] = deepcopy(exposure)

    def find_exposure(self, name: str) -> tuple[str, Exposure] | None:
        for instance_id, exposures in self._exposures.items():
            inst = self._environments.get(instance_id)
            if name in exposures and inst and inst.status is not Status.destroyed:
                return instance_id, deepcopy(exposures[name])
        return None

    def remove_exposure(self, instance_id: str, name: str) -> bool:
        exposures = self._exposures.get(instance_id, {})
        return exposures.pop(name, None) is not None

    def count_active(self, owner: str) -> int:
        return sum(
            inst.owner == owner and inst.status not in (Status.destroyed, Status.error)
            for inst in self._environments.values()
        )

    def delete(self, id: str) -> None:
        inst = self._environments.get(id)
        if inst:
            inst.status = Status.destroyed
        self._grants.pop(id, None)
        self._exposures.pop(id, None)
