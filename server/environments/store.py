"""DjangoStateStore — adapts the ORM to the state-store interface the cawl
`ControlPlane` expects, so all the tested orchestration logic runs unchanged
against Postgres. Also records history events and soft-deletes.
"""

from __future__ import annotations

from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from cawl_core.errors import NameConflict, QuotaExceeded
from cawl_core.models import (
    Environment as CoreEnvironment, Exposure as CoreExposure, Status,
)

DESTROYED = Status.destroyed.value


class DjangoStateStore:
    # -- mapping -----------------------------------------------------------
    def _to_environment(self, sb) -> CoreEnvironment:
        return CoreEnvironment(
            id=sb.id, template=sb.template.name, args=sb.args or {},
            args_hash=sb.args_hash, backend=sb.backend,
            egress_policy=sb.egress_policy,
            owner=sb.owner, status=Status(sb.status), vm_ip=sb.vm_ip, url=sb.url,
            image=sb.image, error=sb.error,
            generation=str(sb.generation),
            operation_id=str(sb.active_operation) if sb.active_operation else "",
            created_at=sb.created_at, expires_at=sb.expires_at,
            # prefetched by the queryset helpers below, so listing N environments
            # doesn't run N grant/exposure queries
            shared_with=tuple(g.principal for g in sb.grants.all()),
            exposures=tuple(
                CoreExposure(name=e.name, port=e.port,
                             access=tuple(e.access or ()))
                for e in sb.exposures.all()),
        )

    def _qs(self):
        from .models import Environment
        return (Environment.objects.select_related("template")
                .prefetch_related("grants", "exposures"))

    # -- StateStore interface ---------------------------------------------
    def reserve(self, inst: CoreEnvironment, quota: int | None = None) -> bool:
        """Reserve the namespace and quota slot in one transaction.

        The per-owner lock makes the count-and-insert sequence serial even when
        the owner has no environments yet. The namespace lease unifies instance
        ids and exposure labels, whose database tables otherwise cannot enforce
        their shared DNS namespace.
        """
        from .models import Environment, NamespaceLease, OwnerQuotaLock

        try:
            with transaction.atomic():
                OwnerQuotaLock.objects.get_or_create(owner=inst.owner)
                OwnerQuotaLock.objects.select_for_update().get(owner=inst.owner)
                if quota is not None and self.count_active(inst.owner) >= quota:
                    raise QuotaExceeded(f"owner {inst.owner!r} at quota ({quota})")

                held = (Environment.objects.select_for_update()
                        .filter(pk=inst.id).first())
                if held is not None and held.status not in (
                        DESTROYED, Status.error.value):
                    return False
                if held is not None:
                    NamespaceLease.objects.filter(environment=held).delete()
                self.upsert(inst)
                # `upsert` has created/reclaimed the row inside this transaction.
                # The operation is durable before any caller may contact Incus.
                sb = Environment.objects.select_for_update().get(pk=inst.id)
                from .models import LifecycleOperation
                op = LifecycleOperation.objects.create(
                    environment=sb, generation=sb.generation, kind="provision")
                sb.active_operation = op.id
                sb.save(update_fields=["active_operation"])
                inst.generation = str(sb.generation)
                inst.operation_id = str(op.id)
                NamespaceLease.objects.create(
                    name=inst.id, environment_id=inst.id)
            return True
        except IntegrityError:
            # Primary-key/lease uniqueness decides concurrent namespace claims.
            if (Environment.objects.filter(pk=inst.id).exists() or
                    NamespaceLease.objects.filter(name=inst.id).exists()):
                return False
            raise

    def begin_operation(self, id: str, kind: str, expected: tuple[Status, ...],
                        transitional: Status) -> CoreEnvironment | None:
        """Atomically claim an environment before performing a side effect."""
        from .models import Environment, EnvironmentEvent, LifecycleOperation

        with transaction.atomic():
            sb = (Environment.objects.select_for_update().select_related("template")
                  .filter(pk=id).first())
            if sb is None or sb.active_operation is not None:
                return None
            if sb.status not in {status.value for status in expected}:
                return None
            old_status = sb.status
            op = LifecycleOperation.objects.create(
                environment=sb, generation=sb.generation, kind=kind)
            sb.status = transitional.value
            sb.active_operation = op.id
            sb.save(update_fields=["status", "active_operation"])
            EnvironmentEvent.objects.create(
                environment=sb, kind="status", from_status=old_status,
                to_status=sb.status, detail=f"operation {op.id} ({kind})")
            # Reverse relations are intentionally loaded after the lock; no
            # external work has started until this transaction commits.
            result = self._to_environment(sb)
            result.generation = str(sb.generation)
            result.operation_id = str(op.id)
            return result

    def complete_operation(self, inst: CoreEnvironment, *, succeeded: bool) -> bool:
        """Commit a lifecycle result only for the operation that owns it.

        External calls are intentionally outside the transaction.  This compare
        under a row lock is the fence that prevents a stale request/worker from
        changing a reclaimed environment name.
        """
        from .models import Environment, EnvironmentEvent, LifecycleOperation

        if not inst.operation_id or not inst.generation:
            return False
        with transaction.atomic():
            sb = Environment.objects.select_for_update().filter(pk=inst.id).first()
            if (sb is None or str(sb.generation) != inst.generation or
                    str(sb.active_operation or "") != inst.operation_id):
                return False
            op = LifecycleOperation.objects.select_for_update().filter(
                pk=inst.operation_id, environment=sb, generation=sb.generation).first()
            if op is None or op.state not in ("queued", "running"):
                return False
            # The row lock remains held while upsert re-reads it, so this is not
            # a check-then-write race.
            self.upsert(inst)
            op.state = "succeeded" if succeeded else "failed"
            op.error = inst.error or ""
            op.finished_at = timezone.now()
            op.save(update_fields=["state", "error", "finished_at"])
            # Keep the externally meaningful transition in the audit stream in
            # addition to the internal claimed state (stopping/starting).
            if succeeded and op.kind in ("stop", "start"):
                from_status, to_status = ((Status.ready.value, Status.stopped.value)
                                          if op.kind == "stop" else
                                          (Status.stopped.value, Status.ready.value))
                EnvironmentEvent.objects.create(
                    environment=sb, kind="status", from_status=from_status,
                    to_status=to_status, detail=f"operation {op.id} completed")
            sb.active_operation = None
            sb.save(update_fields=["active_operation"])
            inst.operation_id = ""
            return True

    def upsert(self, inst: CoreEnvironment) -> None:
        from .models import Environment, EnvironmentEvent, Template

        template = Template.objects.get(name=inst.template)
        sb = Environment.objects.filter(pk=inst.id).first()
        # A row whose env is dead (destroyed/errored) can have its name re-claimed by
        # `up --name`; that's a fresh create reusing the row, not an update of the old
        # env — so re-pin the template version and clear destroyed_at. The row's event
        # log then spans both envs, which is the accepted cost of recycling names.
        creating = sb is None or (sb.status in (DESTROYED, Status.error.value)
                                  and inst.status is Status.creating)
        old_status = None if creating else sb.status
        if creating:
            # Also clean rows left by environments destroyed before teardown
            # started revoking capabilities. This makes reclaim safe across an
            # upgrade as well as for newly destroyed environments.
            if sb is not None:
                sb.grants.all().delete()
                sb.exposures.all().delete()
            # Pin the template version this env launches from, once, at create.
            sb = Environment(id=inst.id, template=template,
                         template_version=template.version)

        sb.args = dict(inst.args)
        sb.args_hash = inst.args_hash
        sb.backend = inst.backend
        sb.egress_policy = inst.egress_policy
        sb.owner = inst.owner
        sb.status = inst.status.value
        sb.vm_ip = inst.vm_ip
        sb.url = inst.url
        sb.image = inst.image
        sb.error = inst.error
        sb.created_at = inst.created_at
        sb.expires_at = inst.expires_at
        sb.save()

        if creating:
            EnvironmentEvent.objects.create(
                environment=sb, kind="created", to_status=sb.status, actor=inst.owner)
        elif old_status != sb.status:
            EnvironmentEvent.objects.create(
                environment=sb, kind="status", from_status=old_status,
                to_status=sb.status, detail=inst.error or "")

    def get(self, id: str) -> CoreEnvironment | None:
        sb = self._qs().filter(pk=id).first()
        return self._to_environment(sb) if sb else None

    def find(self, template: str, args_hash: str,
             owner: str) -> CoreEnvironment | None:
        sb = (self._qs()
              .filter(template__name=template, args_hash=args_hash, owner=owner)
              .exclude(status=DESTROYED)
              .order_by("-created_at").first())
        return self._to_environment(sb) if sb else None

    def list(self, *, template=None,
             owner=None, visible_to=None) -> list[CoreEnvironment]:
        from django.db.models import Q
        qs = self._qs().exclude(status=DESTROYED)
        if template:
            qs = qs.filter(template__name=template)
        if owner:
            qs = qs.filter(owner=owner)
        if visible_to:
            # Mine, plus the ones someone shared with me.
            qs = qs.filter(
                Q(owner=visible_to) | Q(grants__principal=visible_to)).distinct()
        return [self._to_environment(s) for s in qs.order_by("-created_at")]

    def expired(self, now: datetime) -> list[CoreEnvironment]:
        qs = (self._qs().exclude(status=DESTROYED)
              .filter(expires_at__isnull=False, expires_at__lte=now))
        return [self._to_environment(s) for s in qs]

    # -- grants ------------------------------------------------------------
    def grants(self, instance_id: str) -> list[str]:
        from .models import EnvironmentGrant
        return list(EnvironmentGrant.objects.filter(environment_id=instance_id)
                    .values_list("principal", flat=True))

    def grant(self, instance_id: str, principal: str, *, granted_by: str = "") -> None:
        from .models import Environment, EnvironmentEvent, EnvironmentGrant
        _, created = EnvironmentGrant.objects.get_or_create(
            environment_id=instance_id, principal=principal,
            defaults={"granted_by": granted_by})
        if created:
            EnvironmentEvent.objects.create(
                environment=Environment.objects.get(pk=instance_id), kind="shared",
                actor=granted_by, detail=principal)

    def revoke(self, instance_id: str, principal: str) -> bool:
        from .models import Environment, EnvironmentEvent, EnvironmentGrant
        deleted, _ = EnvironmentGrant.objects.filter(
            environment_id=instance_id, principal=principal).delete()
        if deleted:
            EnvironmentEvent.objects.create(
                environment=Environment.objects.get(pk=instance_id), kind="unshared",
                detail=principal)
        return bool(deleted)

    # -- exposures ----------------------------------------------------------
    def exposures(self, instance_id: str) -> list[CoreExposure]:
        from .models import Exposure
        return [CoreExposure(name=e.name, port=e.port, access=tuple(e.access or ()))
                for e in Exposure.objects.filter(environment_id=instance_id)]

    def find_exposure(self, name: str) -> tuple[str, CoreExposure] | None:
        """Global label lookup among live environments (labels are hostnames, so
        the namespace is shared; destroyed envs release theirs)."""
        from .models import Exposure
        e = (Exposure.objects.filter(name=name)
             .exclude(environment__status=DESTROYED).first())
        if not e:
            return None
        return e.environment_id, CoreExposure(
            name=e.name, port=e.port, access=tuple(e.access or ()))

    def set_exposure(self, instance_id: str, exposure: CoreExposure) -> None:
        from .models import Exposure, Environment, EnvironmentEvent, NamespaceLease
        try:
            with transaction.atomic():
                environment = Environment.objects.select_for_update().get(pk=instance_id)
                lease, _ = NamespaceLease.objects.get_or_create(
                    name=exposure.name, defaults={"environment": environment})
                if lease.environment_id != instance_id:
                    raise NameConflict(
                        f"hostname {exposure.name!r} is already exposed by another environment")
                Exposure.objects.update_or_create(
                    environment_id=instance_id, name=exposure.name,
                    defaults={"port": exposure.port, "access": list(exposure.access)})
                EnvironmentEvent.objects.create(
                    environment=environment, kind="exposed",
                    detail=f"{exposure.name} -> :{exposure.port}"
                           + (f" access={','.join(exposure.access)}"
                              if exposure.access else ""))
        except IntegrityError:
            # A concurrent insert may lose either unique constraint.
            if NamespaceLease.objects.filter(name=exposure.name).exclude(
                    environment_id=instance_id).exists():
                raise NameConflict(
                    f"hostname {exposure.name!r} is already exposed by another environment")
            raise

    def remove_exposure(self, instance_id: str, name: str) -> bool:
        from .models import Exposure, Environment, EnvironmentEvent, NamespaceLease
        with transaction.atomic():
            environment = Environment.objects.select_for_update().get(pk=instance_id)
            deleted, _ = Exposure.objects.filter(
                environment_id=instance_id, name=name).delete()
            if deleted:
                # The environment's own id remains leased even without a route.
                if name != instance_id:
                    NamespaceLease.objects.filter(
                        name=name, environment_id=instance_id).delete()
                EnvironmentEvent.objects.create(
                    environment=environment, kind="unexposed", detail=name)
            return bool(deleted)

    def count_active(self, owner: str) -> int:
        from .models import Environment
        return (Environment.objects.filter(owner=owner)
                .exclude(status__in=[DESTROYED, Status.error.value]).count())

    def delete(self, id: str) -> None:
        """Soft-delete the environment and revoke all access attached to it."""
        from .models import Environment, EnvironmentEvent

        # The Environment row is recycled when a destroyed name is claimed again.
        # Grants and exposures are capabilities, not history, so they must not
        # survive that boundary. Keep the event log, which is the historical record.
        with transaction.atomic():
            sb = Environment.objects.select_for_update().filter(pk=id).first()
            if sb is None:
                return
            old = sb.status
            sb.grants.all().delete()
            sb.exposures.all().delete()
            sb.namespace_leases.all().delete()
            sb.status = DESTROYED
            sb.destroyed_at = timezone.now()
            sb.save(update_fields=["status", "destroyed_at"])
            EnvironmentEvent.objects.create(
                environment=sb, kind="destroyed", from_status=old,
                to_status=DESTROYED)
