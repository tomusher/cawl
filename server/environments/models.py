"""ORM models: the template registry, the environment (VM) record, its history, and
agent API tokens. This is the durable source of truth the daemon owns."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class TemplateNameLock(models.Model):
    """One row per template handle, used to serialize version assignment."""

    name = models.SlugField(primary_key=True)


class OwnerQuotaLock(models.Model):
    """One row per owner, used to serialize environment quota reservations."""

    owner = models.CharField(primary_key=True, max_length=200)


class Template(models.Model):
    """A registered template: a named, versioned template.yaml the daemon owns.

    The config body (``raw_yaml``) lives here in the database — uploaded with
    ``cawl template create`` — rather than in a file on the daemon's disk. The
    name is the ``name:`` key of its YAML, so it's the handle used by ``cawl up``.
    """

    # Handles remain deployment-wide unique. A blank owner denotes an admin-published
    # global template; otherwise it is private to that principal.
    name = models.SlugField(unique=True)
    owner = models.CharField(max_length=200, blank=True, db_index=True)
    raw_yaml = models.TextField(help_text="the template.yaml body; source of truth")
    # Denormalized from raw_yaml on write, for display only: the names of the args
    # this template declares, comma-joined.
    params = models.CharField(max_length=500, blank=True)
    version = models.IntegerField(default=1)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.name} (v{self.version})"


class TemplateVersion(models.Model):
    """Append-only history of a template's config bodies (one row per version)."""

    template = models.ForeignKey(
        Template, on_delete=models.CASCADE, related_name="versions")
    version = models.IntegerField()
    raw_yaml = models.TextField()
    params = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-version"]
        unique_together = [("template", "version")]


class Environment(models.Model):
    """One environment. Mirrors cawl_core.models.Instance; soft-deleted (status
    ``destroyed``) so history survives teardown."""

    id = models.CharField(primary_key=True, max_length=100)
    template = models.ForeignKey(
        Template, on_delete=models.PROTECT, related_name="environments")
    # The template version this env was launched from (pinned at create time).
    template_version = models.IntegerField(null=True, blank=True)
    # The template args this env was created from, resolved (defaults filled in),
    # and their digest — the rest of the (template, purpose, owner) reuse key.
    # Not secrets: shown in `cawl ls` and here in the admin.
    args = models.JSONField(default=dict, blank=True)
    args_hash = models.CharField(max_length=64, blank=True, db_index=True)
    # Which of the deployment's named backends materialized this env.
    backend = models.CharField(max_length=50, blank=True)
    # Trusted server-side policy selected at creation, never a template argument.
    egress_policy = models.CharField(max_length=100, blank=True)
    owner = models.CharField(max_length=200, db_index=True)
    status = models.CharField(max_length=20)
    vm_ip = models.GenericIPAddressField(null=True, blank=True)
    url = models.URLField(max_length=500, null=True, blank=True)
    image = models.CharField(max_length=200, null=True, blank=True)
    error = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    destroyed_at = models.DateTimeField(null=True, blank=True)
    # Reclaiming an id creates a new generation.  Lifecycle workers fence every
    # completion on this value, so a delayed destroy/provision cannot affect a
    # later environment with the same human-readable name.
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    active_operation = models.UUIDField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.id} ({self.owner})"


class LifecycleOperation(models.Model):
    """Durable, fenced intent for one external lifecycle side effect.

    The database transaction creates and claims this record; Incus, egress and
    Traefik work happens after commit.  A worker may complete it only if its
    generation and id still match ``Environment.active_operation``.
    """

    KIND = [(v, v) for v in ("provision", "start", "stop", "destroy", "sync-ingress")]
    STATE = [(v, v) for v in ("queued", "running", "succeeded", "failed")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    environment = models.ForeignKey(
        Environment, on_delete=models.CASCADE, related_name="lifecycle_operations")
    generation = models.UUIDField(editable=False)
    kind = models.CharField(max_length=20, choices=KIND)
    state = models.CharField(max_length=20, choices=STATE, default="queued", db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]


class NamespaceLease(models.Model):
    """Atomic ownership of an environment/exposure DNS label.

    Environment ids and exposure names intentionally share this table because
    they share one externally visible namespace.
    """

    name = models.CharField(primary_key=True, max_length=100)
    environment = models.ForeignKey(
        Environment, on_delete=models.CASCADE, related_name="namespace_leases")


class EnvironmentGrant(models.Model):
    """Someone other than the owner who may use this environment.

    This row *is* the access. The environment itself holds no list of people — its
    authorized_principals file names the environment — so the daemon consults this
    table when it decides whether to sign an SSH certificate. Granting and
    revoking therefore never touch the running box, and can't drift from it.

    A grantee gets what the owner gets minus the irreversible parts: they may
    see, exec in, and SSH to the env, but not destroy it (see cawl_core.auth).
    """

    environment = models.ForeignKey(
        Environment, on_delete=models.CASCADE, related_name="grants")
    principal = models.CharField(max_length=200, db_index=True,
                                 help_text="the principal id being granted access")
    granted_by = models.CharField(max_length=200, blank=True)
    granted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [("environment", "principal")]
        ordering = ["principal"]

    def __str__(self):
        return f"{self.environment_id} → {self.principal}"


class Exposure(models.Model):
    """One exported port of a environment, reachable at its own hostname behind the
    daemon's forward-auth gate.

    Like a grant, this row *is* the exposure: Traefik's route files are rendered
    from it, and the forward-auth endpoint consults it on every request — so
    changing ``access`` (or deleting the row) never touches the VM and takes
    effect on the next request. ``access`` is a list of email addresses admitted
    via magic link; empty means whoever has access to the environment itself.
    """

    environment = models.ForeignKey(
        Environment, on_delete=models.CASCADE, related_name="exposures")
    # The hostname label (<name>.<base domain>): a global namespace, freely
    # chosen. Exposure rows are deleted when an environment is destroyed, so a
    # global unique constraint closes races while still releasing old labels.
    name = models.CharField(max_length=63, unique=True)
    port = models.IntegerField()
    access = models.JSONField(default=list, blank=True)
    created_by = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.environment_id}:{self.name} -> {self.port}"


class EnvironmentEvent(models.Model):
    """Append-only history for a environment."""

    KIND = [
        ("created", "created"),
        ("status", "status change"),
        ("exec", "exec"),
        ("shared", "shared"),
        ("unshared", "unshared"),
        ("exposed", "exposed"),
        ("unexposed", "unexposed"),
        ("ssh-cert", "ssh cert issued"),
        ("destroyed", "destroyed"),
        ("error", "error"),
    ]
    environment = models.ForeignKey(Environment, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(default=timezone.now, db_index=True)
    kind = models.CharField(max_length=20, choices=KIND)
    actor = models.CharField(max_length=200, blank=True)
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    detail = models.TextField(blank=True)

    class Meta:
        ordering = ["-at"]


class ViewerMagicToken(models.Model):
    """A one-time, exposure-scoped credential for a browser viewer.

    Viewer identities deliberately do not use ``AUTH_USER_MODEL``. Only a hash
    of the bearer token is stored, just as for API tokens.
    """

    email = models.EmailField()
    host = models.CharField(max_length=253)
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    @staticmethod
    def hash_key(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def mint(cls, *, email: str, host: str,
             ttl: timedelta) -> tuple["ViewerMagicToken", str]:
        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            email=email.strip().lower(), host=host.lower(),
            key_hash=cls.hash_key(raw), expires_at=timezone.now() + ttl,
        )
        return token, raw

    def __str__(self):
        return f"{self.email} → {self.host}"


class ApiToken(models.Model):
    """A capability token for programmatic callers (agents). The plaintext is
    shown once at mint time; only its hash is stored."""

    name = models.CharField(max_length=200)
    subject = models.CharField(max_length=200, help_text="principal id")
    role = models.CharField(max_length=20, default="user")  # user | admin
    quota = models.IntegerField(null=True, blank=True, help_text="max concurrent envs")
    # Guardrails for agent/CI tokens. max_ttl: every environment this token
    # creates expires within this ttl spec (e.g. "4h") — filled in when no TTL
    # applies, a ceiling on any that's requested. backend: when set, every
    # environment this token creates lands on that named backend, whatever the
    # request says. Blank = no guardrail.
    max_ttl = models.CharField(max_length=10, blank=True)
    backend = models.CharField(max_length=50, blank=True)
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    prefix = models.CharField(max_length=12, help_text="display only")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} [{self.subject}]"

    @staticmethod
    def hash_key(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def mint(cls, *, name, subject, role="user", quota=None, max_ttl="",
             backend="", ttl: timedelta | None = None,
             created_by=None) -> tuple["ApiToken", str]:
        raw = "cawl_" + secrets.token_urlsafe(32)
        tok = cls.objects.create(
            name=name, subject=subject, role=role, quota=quota, max_ttl=max_ttl,
            backend=backend,
            key_hash=cls.hash_key(raw), prefix=raw[:12], created_by=created_by,
            expires_at=(timezone.now() + ttl) if ttl else None,
        )
        return tok, raw

    def is_valid(self, now=None) -> bool:
        now = now or timezone.now()
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return True
