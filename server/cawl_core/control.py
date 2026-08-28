"""The control plane: ties state + runtime + ingress into the lifecycle ops
the CLI and agents drive, and enforces the authorization policy.

Every mutating/reading op takes an ``actor`` Principal. In production this runs
inside the daemon, where the actor is derived from the authenticated transport
(not from the client), so these checks are the real access-control boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Protocol

from cawl_core.access import AccessProvider, BridgeAccess
from cawl_core.auth import (
    Principal, SYSTEM, require_access, require_admin, require_owner,
)
from cawl_core.config import TemplateConfig
from cawl_core.egress import EgressProvider, NoEgress
from cawl_core.errors import (
    CawlError, InvalidName, NameConflict, NotFound, PermissionDenied, QuotaExceeded,
)
from cawl_core.ingress import TraefikIngress
from cawl_core.models import Environment, Exposure, Status
from cawl_core.naming import (
    compute_expiry, default_exposure_label, new_environment_id, parse_ttl,
    validate_exposure_label, validate_name,
)
from cawl_core.params import ParamError, args_hash, render, resolve
from cawl_core.runtime import sshd
from cawl_core.runtime.base import ExecResult, InstanceSpec, Runtime
from cawl_core.ssh_ca import SshCertAuthority

logger = logging.getLogger(__name__)


__all__ = [
    "ControlPlane", "UpResult", "SshAccess",
    "CawlError", "NotFound", "QuotaExceeded", "PermissionDenied",
    "InvalidName", "NameConflict", "ParamError",
]

# A destroyed or errored env still owns its row (the Django store soft-deletes),
# but no longer owns its name — those ids are free to claim again.
_DEAD = (Status.destroyed, Status.error)


class StateStore(Protocol):
    """Persistence operations required by the control plane."""

    def reserve(self, inst: Environment, quota: int | None = None) -> bool: ...
    def complete_operation(self, inst: Environment, *, succeeded: bool) -> bool: ...
    def begin_operation(self, id: str, kind: str, expected: tuple[Status, ...],
                        transitional: Status) -> Environment | None: ...
    def upsert(self, inst: Environment) -> None: ...
    def get(self, id: str) -> Environment | None: ...
    def find(self, template: str, args_hash: str, owner: str) -> Environment | None: ...
    def list(self, *, template: str | None = None, owner: str | None = None,
             visible_to: str | None = None) -> list[Environment]: ...
    def expired(self, now: datetime) -> list[Environment]: ...
    def count_active(self, owner: str) -> int: ...
    def grants(self, instance_id: str) -> list[str]: ...
    def grant(self, instance_id: str, principal: str, *, granted_by: str = "") -> None: ...
    def revoke(self, instance_id: str, principal: str) -> bool: ...
    def exposures(self, instance_id: str) -> list[Exposure]: ...
    def set_exposure(self, instance_id: str, exposure: Exposure) -> None: ...
    def find_exposure(self, name: str) -> tuple[str, Exposure] | None: ...
    def remove_exposure(self, instance_id: str, name: str) -> bool: ...
    def delete(self, id: str) -> None: ...


@dataclass
class UpResult:
    instance: Environment


@dataclass
class SshAccess:
    """Everything a client needs for one SSH session, and nothing durable."""

    host: str            # what the access provider says clients dial
    user: str            # the login account on the box
    certificate: str     # signed for this actor, this box, minutes of validity
    ca_pubkey: str       # pin as @cert-authority — verifies the box, no TOFU
    jump: str | None = None  # relay hop to reach host, if the provider needs one


class ControlPlane:
    def __init__(
        self,
        state: StateStore,
        runtime: Runtime | dict[str, Runtime],
        ingress: TraefikIngress,
        *,
        default_backend: str | None = None,
        access: AccessProvider | None = None,
        egress: EgressProvider | None = None,
        default_quota: int | None = None,
        ca: SshCertAuthority | None = None,
        ssh_user: str = "dev",
    ):
        self.state = state
        # Named backends — the operator's registry of ways to materialize an
        # environment (cawl names none of them; "vm"/"container" are just what
        # an Incus deployment tends to call its two). A bare Runtime is the
        # single-backend deployment, registered as "default".
        self.backends = runtime if isinstance(runtime, dict) else {"default": runtime}
        if not self.backends:
            raise ValueError("at least one backend is required")
        self.default_backend = default_backend or next(iter(self.backends))
        if self.default_backend not in self.backends:
            raise ValueError(f"default backend {self.default_backend!r} is not registered")
        self.ingress = ingress
        # How users reach a box's sshd (cawl_core/access.py) — the operator's
        # deployment-wide choice. Default: dial the bridge IP directly.
        self.access = access or BridgeAccess()
        # Outbound connectivity is a separate deployment concern from SSH
        # transport. Its attachment is applied by the runtime outside the guest.
        self.egress = egress or NoEgress()
        self.default_quota = default_quota
        self.ca = ca
        self.ssh_user = ssh_user

    def _now(self, now: datetime | None) -> datetime:
        return now or datetime.now(timezone.utc)

    def _backend(self, name: str) -> Runtime:
        """Resolve a backend name — the whole of backend validation. Unknown
        names fail here, listing what this deployment actually offers."""
        try:
            return self.backends[name]
        except KeyError:
            raise CawlError(
                f"unknown backend {name!r} (this deployment has: "
                f"{', '.join(sorted(self.backends))})") from None

    def _runtime(self, inst: Environment) -> Runtime:
        return self._backend(inst.backend or self.default_backend)

    def _ssh_target(self, inst: Environment) -> str | None:
        # One login account for every environment: the cert, not the username,
        # says who you are. sshd's log records the cert's key id, so a shared
        # account still attributes the session to a person.
        host = self.access.ssh_host(inst.id, inst.vm_ip)
        return f"{self.ssh_user}@{host}" if host else None

    def _get_or_404(self, id: str) -> Environment:
        inst = self.state.get(id)
        if not inst or inst.status is Status.destroyed:
            raise NotFound(id)
        # `ssh` is presentation, not state: derived from the current access
        # provider on every read, so it can't go stale when the provider (or
        # the box's address) changes. Only a running box has a target.
        inst.ssh = self._ssh_target(inst) if inst.status is Status.ready else None
        return inst

    def _claim_name(self, name: str, actor: Principal, reuse: bool) -> tuple[str, Environment | None]:
        """Validate a custom name and check it's free. Returns (id, reusable env).

        The name *is* the instance id — the Incus instance, the SSH host and the
        state key all derive from it — so it has to be a legal label and unheld.
        Uniqueness is global (the namespace is shared across owners), so the
        conflict message deliberately doesn't say who holds it.
        """
        try:
            environment_id = validate_name(name)
        except ValueError as e:
            raise InvalidName(str(e)) from e

        # Environment ids and exposure labels share the same DNS namespace.
        # Check before creating the runtime so a squatted label fails fast.
        self._require_label_free(environment_id, environment_id)

        held = self.state.get(environment_id)
        if held is None or held.status in _DEAD:
            return environment_id, None
        if reuse:
            require_access(actor, held)  # never hand back someone else's env
            return environment_id, held
        raise NameConflict(f"name {environment_id!r} is already in use")

    def _hook_values(self, inst: Environment) -> dict[str, str]:
        """What a hook sees: the env's resolved args, plus the daemon's builtins."""
        return {
            "id": inst.id,
            "template": inst.template,
            **inst.args,
        }

    def _spec_for(self, inst: Environment, template: TemplateConfig,
                  authorized_keys: list[str] | None = None) -> InstanceSpec:
        """The runtime's view of an env. Shared by create and resume so the two
        can't drift — a resumed box gets the same hooks and the same CA."""
        if inst.egress_policy and inst.egress_policy != self.egress.policy.name:
            raise CawlError(f"egress policy {inst.egress_policy!r} is not configured")
        return InstanceSpec(
            id=inst.id, template=template.name,
            image=inst.image or "",
            provision=render(template.hooks.provision, self._hook_values(inst)),
            authorized_keys=authorized_keys or [],
            ssh_ca_pubkey=self.ca.pubkey if self.ca else "",
            ssh_user=self.ssh_user,
            network_boot=self.access.boot_script(inst.id),
            egress_network=self.egress.attachment(inst.id).network,
            egress_boot=self.egress.boot_script(inst.id),
            # NoEgress has no source-address policy to register. Requiring an
            # address in that mode turns an otherwise usable local VM into a
            # startup failure while DHCP/guest state is still settling.
            egress_ready=(
                lambda ip: self.egress.register(
                    inst.id, ip, self.egress.policy.allowed_hosts)
            ) if self.egress.policy.name != "none" else None,
        )

    def up(
        self,
        template: TemplateConfig,
        actor: Principal,
        *,
        args: dict[str, str] | None = None,
        name: str | None = None,
        ttl: str | None = None,
        reuse: bool = False,
        on_behalf_of: str | None = None,
        quota: int | None = None,
        max_ttl: timedelta | None = None,
        backend: str | None = None,
        authorized_keys: list[str] | None = None,
        now: datetime | None = None,
    ) -> UpResult:
        now = self._now(now)

        # owner is the authenticated principal; only admins may create for others.
        owner = actor.id
        if on_behalf_of and on_behalf_of != actor.id:
            require_admin(actor, "create environments for another owner")
            owner = on_behalf_of

        # Validate the args against what the template declares, and fill in its
        # defaults — everything downstream works from the resolved set.
        resolved = resolve(template.params, args or {})
        key = args_hash(template.name, resolved)

        # A custom name pins the id; otherwise it's generated. With --reuse-if-exists
        # the name is the lookup key, taking the place of the (template,args,owner)
        # natural key — you asked for *that* env, not one that merely matches.
        environment_id = None
        if name is not None:
            environment_id, reusable = self._claim_name(name, actor, reuse)
            if reusable is not None:
                return self._reuse(reusable, template)
        elif reuse:
            existing = self.state.find(template.name, key, owner)
            if existing and existing.status is not Status.error:
                return self._reuse(existing, template)

        limit = quota if quota is not None else self.default_quota

        ttl_spec = ttl if ttl is not None else template.ttl
        expires = compute_expiry(now, parse_ttl(ttl_spec))
        # A caller-level lifetime cap (e.g. an agent token minted with
        # max_ttl=4h): fills in when nothing above set an expiry, and clamps
        # anything longer — a guardrail the request can't talk its way out of.
        if max_ttl is not None:
            cap = now + max_ttl
            if expires is None or expires > cap:
                expires = cap

        backend = backend or self.default_backend
        runtime = self._backend(backend)
        image = runtime.image_name(template.image or template.name)

        def candidate(candidate_id: str) -> Environment:
            return Environment(
                id=candidate_id,
                template=template.name,
                args=resolved,
                args_hash=key,
                backend=backend,
                egress_policy=self.egress.policy.name,
                owner=owner,
                status=Status.creating,
                image=image,
                created_at=now,
                expires_at=expires,
            )

        if environment_id is not None:
            inst = candidate(environment_id)
            if not self.state.reserve(inst, limit):
                # Close the race between _claim_name's friendly preflight check
                # and the database-backed reservation.
                raise NameConflict(f"name {environment_id!r} is already in use")
        else:
            # Collisions are vanishingly unlikely with the 128-bit suffix, but
            # reservation (rather than upsert) makes one harmless and retries it.
            for _ in range(10):
                inst = candidate(new_environment_id(template.name))
                if self.state.reserve(inst, limit):
                    break
            else:
                raise CawlError("could not reserve a unique environment id")

        spec = self._spec_for(inst, template, authorized_keys)
        runtime_created = False

        try:
            info = runtime.create(spec)
            runtime_created = True
            inst.vm_ip = info.ip
            inst.ssh = self._ssh_target(inst)
            if inst.ssh:
                self._install_host_cert(inst)
            # Materialize the template's default exposures, then publish routes.
            for key, port in template.expose.items():
                label = default_exposure_label(inst.id, key)
                self._require_label_free(label, inst.id)
                self.state.set_exposure(inst.id, Exposure(name=label, port=port))
            inst.exposures = tuple(self.state.exposures(inst.id))
            inst.url = self._primary_url(inst)
            inst.status = Status.ready
            if not self.state.complete_operation(inst, succeeded=True):
                raise CawlError(f"lifecycle operation for {inst.id} was superseded")
            self.ingress.sync(inst)
        except Exception as e:  # noqa: BLE001 — record failure, surface upward
            inst.status = Status.error
            inst.error = str(e)
            self.state.complete_operation(inst, succeeded=False)
            # Never delete a same-named runtime merely because create raised:
            # only a successful create return proves this operation owns it.
            if runtime_created:
                try:
                    self.egress.unregister(inst.id)
                except Exception:  # noqa: BLE001 — runtime cleanup must still run
                    logger.exception("failed to unregister egress for %s", inst.id)
                try:
                    runtime.destroy(inst.id)
                except Exception as cleanup_error:  # noqa: BLE001
                    # Provisioning failed, but the successfully-created runtime
                    # may still be alive. Keep its namespace/quota reservation
                    # and make destroy retryable instead of treating it as dead.
                    inst.status = Status.destroy_failed
                    inst.error = f"provisioning failed: {e}; teardown failed: {cleanup_error}"
                    self.state.upsert(inst)
                    logger.exception(
                        "failed to clean up environment %s after provisioning error",
                        inst.id,
                    )
            raise CawlError(f"failed to bring up {inst.id}: {e}") from e

        return UpResult(inst)

    def _reuse(self, inst: Environment, template: TemplateConfig) -> UpResult:
        """Hand back an env that already exists — running. `--reuse-if-exists`
        promises "the env I'd have got", and the env you'd have got would be up,
        so a stopped one is resumed rather than returned dead."""
        if inst.status is Status.destroy_failed:
            raise CawlError(
                f"environment {inst.id} has a failed teardown; retry destroy first")
        if inst.status is Status.stopped:
            environment_id = inst.id
            inst = self.state.begin_operation(
                environment_id, "start", (Status.stopped,), Status.starting)
            if inst is None:
                raise CawlError(f"{environment_id} lifecycle operation was superseded")
            return self._resume(inst, template)
        return UpResult(inst)

    def _install_host_cert(self, inst: Environment) -> None:
        """Sign the box's own host key so clients can verify it against the CA.

        Every clone regenerates its host keys and instance names get recycled, so
        without this each new environment is a trust-on-first-use prompt and a reused
        name is a key-mismatch warning — the kind people learn to click through.
        """
        if not self.ca:
            return
        res = self._runtime(inst).exec(inst.id, sshd.read_host_pubkey_cmd())
        if res.exit_code != 0 or not res.stdout.strip():
            raise CawlError(
                f"could not read the host key of {inst.id}: "
                f"{res.stderr.strip() or 'no output'}")
        host = self.access.ssh_host(inst.id, inst.vm_ip)
        cert = self.ca.sign_host(
            res.stdout, environment_id=inst.id,
            hostnames=[h for h in dict.fromkeys((host, inst.id)) if h],
        )
        res = self._runtime(inst).exec(inst.id, ["sh", "-c", sshd.host_cert_script(cert)])
        if res.exit_code != 0:
            raise CawlError(
                f"could not install the host cert on {inst.id}: {res.stderr.strip()}")

    def ssh(self, id: str, actor: Principal, public_key: str) -> SshAccess:
        """Mint a short-lived cert for one environment — the SSH equivalent of `exec`.

        This is the whole of SSH authorization: the same ``require_access`` that
        guards exec and status, applied at signing time. The box knows only its
        own principal, so a cert we decline to sign is a door that doesn't open.
        """
        inst = self._get_or_404(id)
        require_access(actor, inst)
        if not self.ca:
            raise CawlError("no SSH certificate authority is configured")
        host = self.access.ssh_host(inst.id, inst.vm_ip)
        if not host:
            raise CawlError(f"{id} has no reachable SSH address (is it running?)")
        return SshAccess(
            host=host,
            user=self.ssh_user,
            certificate=self.ca.sign_user(
                public_key, environment_id=inst.id, actor=actor.id),
            ca_pubkey=self.ca.pubkey,
            jump=self.access.ssh_jump(inst.id),
        )

    def status(self, id: str, actor: Principal) -> Environment:
        inst = self._get_or_404(id)
        require_access(actor, inst)
        return inst

    def ls(self, actor: Principal, **filters) -> list[Environment]:
        # Non-admins see their own environments and the ones shared with them.
        if not actor.is_admin:
            filters["visible_to"] = actor.id
        envs = self.state.list(**filters)
        for inst in envs:
            inst.ssh = self._ssh_target(inst) if inst.status is Status.ready else None
        return envs

    def exec(self, id: str, actor: Principal, cmd: list[str]) -> ExecResult:
        inst = self._get_or_404(id)
        require_access(actor, inst)
        return self._runtime(inst).exec(id, cmd)

    # -- sharing ----------------------------------------------------------
    # A grant is a row, never a write into the box: the environment's principal is
    # its own id, so who may hold a cert for it is decided here, at signing time.
    # That means sharing works on a stopped or unreachable env, and nothing can
    # drift out of sync with the database.
    def share(self, id: str, actor: Principal, principal: str) -> Environment:
        inst = self._get_or_404(id)
        require_owner(actor, inst, "share it")
        if principal == inst.owner:
            raise CawlError(f"{principal!r} already owns {id}")
        self.state.grant(id, principal, granted_by=actor.id)
        return self._get_or_404(id)

    def unshare(self, id: str, actor: Principal, principal: str) -> Environment:
        """Revoke a grant. New connections stop working within a cert lifetime;
        a session already established survives until it ends (kill it with
        ``exec`` if you need the door shut now)."""
        inst = self._get_or_404(id)
        require_owner(actor, inst, "unshare it")
        if not self.state.revoke(id, principal):
            raise NotFound(f"{id} is not shared with {principal!r}")
        return self._get_or_404(id)

    # -- exposures ---------------------------------------------------------
    # An exposure is a row, like a grant: the route files are rendered from the
    # rows, and the forward-auth check consults them per request — so exposing,
    # changing access, and unexposing never touch the VM, and revoking an email
    # takes effect on that person's next request.
    def _primary_url(self, inst: Environment) -> str | None:
        """The env's front door: the exposure at its own id, or the only one."""
        if any(e.name == inst.id for e in inst.exposures):
            return self.ingress.url_for(inst.id)
        if len(inst.exposures) == 1:
            return self.ingress.url_for(inst.exposures[0].name)
        return None

    def _require_label_free(self, name: str, environment_id: str) -> None:
        """Labels are a global namespace (they're hostnames): free unless held
        by this same env. Environment ids are reserved too — every env's default
        primary label is its own id — as is the auth host's."""
        held = self.state.find_exposure(name)
        if held and held[0] != environment_id:
            raise NameConflict(f"hostname {name!r} is already exposed by another environment")
        other = self.state.get(name)
        if other and other.status not in _DEAD and other.id != environment_id:
            raise NameConflict(f"hostname {name!r} belongs to another environment")
        if self.ingress.auth_host == f"{name}.{self.ingress.base_domain}":
            raise NameConflict(f"hostname {name!r} is reserved for sign-in")

    def expose(self, id: str, actor: Principal, port: int,
               name: str | None = None,
               access: tuple[str, ...] = ()) -> Environment:
        """Export a port of the env to the web, gated by forward-auth.

        ``name`` is the hostname label — any free one (`--name acme-preview` =>
        acme-preview.<domain>); default, the env's own id. Owner-only: opening
        a door on an env isn't a grantee's call. Empty ``access`` means the
        env's own access list (owner, grantees, admins); emails in ``access``
        additionally admit those people via magic link. Re-exposing an existing
        name updates it in place.
        """
        inst = self._get_or_404(id)
        require_owner(actor, inst, "expose a port on it")
        try:
            name = validate_exposure_label(name if name is not None else inst.id)
        except ValueError as e:
            raise InvalidName(str(e)) from e
        if not (0 < int(port) < 65536):
            raise CawlError(f"invalid port {port!r}")
        self._require_label_free(name, inst.id)
        emails = tuple(dict.fromkeys(a.strip().lower() for a in access if a.strip()))
        self.state.set_exposure(inst.id, Exposure(name=name, port=int(port),
                                                  access=emails))
        return self._sync_exposures(inst.id)

    def unexpose(self, id: str, actor: Principal, name: str) -> Environment:
        inst = self._get_or_404(id)
        require_owner(actor, inst, "unexpose a port on it")
        if not self.state.remove_exposure(inst.id, name):
            raise NotFound(f"{id} has no exposure named {name!r}")
        return self._sync_exposures(inst.id)

    def _sync_exposures(self, id: str) -> Environment:
        inst = self._get_or_404(id)          # re-read: exposures changed
        inst.url = self._primary_url(inst)
        self.state.upsert(inst)
        if inst.status is Status.ready:
            self.ingress.sync(inst)
        return inst

    # -- pause / resume ----------------------------------------------------
    def stop(self, id: str, actor: Principal, template: TemplateConfig | None = None) -> Environment:
        """Shut an env down without destroying it. Frees the RAM; keeps the disk.

        Owner-only, like destroy: it's reversible, but it yanks the box out from
        under everyone the env is shared with, so it isn't a grantee's call.
        """
        inst = self._get_or_404(id)
        require_owner(actor, inst, "stop it")
        if inst.status is Status.stopped:
            return inst                       # idempotent
        if inst.status is not Status.ready:
            raise CawlError(f"{id} is {inst.status.value}, not running")
        inst = self.state.begin_operation(
            id, "stop", (Status.ready,), Status.stopping)
        if inst is None:
            raise CawlError(f"{id} lifecycle operation was superseded")

        # Revoke before stopping so no stale lease remains if the backend fails.
        try:
            self.egress.unregister(inst.id, inst.vm_ip)
            self._runtime(inst).stop(id)
        except Exception as e:  # the runtime may still be running; retain ready
            inst.status = Status.ready
            inst.error = str(e)
            self.state.complete_operation(inst, succeeded=False)
            raise CawlError(f"failed to stop {id}: {e}") from e
        inst.status = Status.stopped
        inst.vm_ip = None                     # it has no address while it's down
        inst.ssh = None
        self.state.complete_operation(inst, succeeded=True)
        # No routes while it's down — a stopped env should 404, not 502. The
        # exposure rows stay; `start` re-renders the same URLs from them.
        self.ingress.deregister(id)
        return inst

    def start(self, id: str, actor: Principal, template: TemplateConfig) -> UpResult:
        """Boot a stopped env back up.

        Not just `power on`. Network membership rarely survives a shutdown (a
        tailnet drops an ephemeral node that goes offline), and Docker won't
        restart containers with no restart policy — so the runtime replays the
        boot half of provisioning, and the env comes back with the name, the
        app, and the SSH access it had. The CA trust stays on its disk; the
        host cert is re-signed for whatever clients now dial (the address may
        have moved).
        """
        inst = self._get_or_404(id)
        require_owner(actor, inst, "start it")
        if inst.status is Status.ready:
            return UpResult(inst)             # idempotent
        if inst.status is not Status.stopped:
            raise CawlError(f"{id} is {inst.status.value}, not stopped")
        inst = self.state.begin_operation(
            id, "start", (Status.stopped,), Status.starting)
        if inst is None:
            raise CawlError(f"{id} lifecycle operation was superseded")
        return self._resume(inst, template)

    def _resume(self, inst: Environment, template: TemplateConfig) -> UpResult:
        try:
            info = self._runtime(inst).start(self._spec_for(inst, template))
            inst.vm_ip = info.ip
            inst.ssh = self._ssh_target(inst)
            # Re-sign the host cert: what clients dial may have moved (an
            # IP-based provider hands out a fresh address on every boot), and a
            # cert naming yesterday's address is a verification failure — the
            # exact key-warning noise the CA exists to eliminate. Idempotent
            # when the name is stable, and also heals a provider switch.
            if inst.ssh:
                self._install_host_cert(inst)
            inst.status = Status.ready
            if not self.state.complete_operation(inst, succeeded=True):
                raise CawlError(f"lifecycle operation for {inst.id} was superseded")
            # Routes are re-rendered from the exposure rows — the IP may have
            # moved, but the URLs (and who may view them) are unchanged.
            self.ingress.sync(inst)
        except Exception as e:  # noqa: BLE001 — leave it stopped, and say why
            # A failed replay must not leave a live source lease behind.
            try:
                self.egress.unregister(inst.id)
            except Exception:  # noqa: BLE001
                pass
            inst.status = Status.stopped
            inst.error = str(e)
            self.state.complete_operation(inst, succeeded=False)
            raise CawlError(f"failed to start {inst.id}: {e}") from e
        return UpResult(inst)

    def destroy(self, id: str, actor: Principal) -> None:
        # Sharing an env doesn't hand over the right to delete it.
        inst = self._get_or_404(id)
        require_owner(actor, inst, "destroy it")
        self._destroy(inst)

    def _destroy(self, inst: Environment) -> None:
        """Teardown with no authz check — internal use (reaper, post-auth)."""
        self.ingress.deregister(inst.id)
        self.egress.unregister(inst.id, inst.vm_ip)
        try:
            self._runtime(inst).destroy(inst.id)
        except Exception as e:  # noqa: BLE001 — retain an operator-visible orphan
            # This state remains active: it keeps its name and quota reservation,
            # and a later destroy/reaper pass can retry the idempotent teardown.
            inst.status = Status.destroy_failed
            inst.error = f"teardown failed: {e}"
            self.state.upsert(inst)
            logger.exception("failed to destroy environment %s", inst.id)
            raise CawlError(f"failed to destroy {inst.id}: {e}") from e
        self.state.delete(inst.id)

    def refresh_image(self, template: TemplateConfig, actor: Principal, *,
                      args: dict[str, str] | None = None,
                      backend: str | None = None) -> str:
        """Bake the golden image — per backend, since images are built *by* a
        backend *for* that backend (admin only)."""
        require_admin(actor, "refresh golden images")
        runtime = self._backend(backend or self.default_backend)
        resolved = resolve(template.params, args or {})
        builder_id = f"{template.name}-builder"
        values = {"id": builder_id, "template": template.name, **resolved}
        spec = InstanceSpec(
            id=builder_id, template=template.name,
            image=runtime.image_name(template.image or template.name),
            build=render(template.hooks.build, values),
            # No network_boot: a throwaway build machine; nobody dials in.
        )
        return runtime.build_image(spec)

    def reap(self, actor: Principal = SYSTEM, now: datetime | None = None) -> list[str]:
        require_admin(actor, "reap environments")
        now = self._now(now)
        reaped = []
        for inst in self.state.expired(now):
            self._destroy(inst)
            reaped.append(inst.id)
        return reaped
