"""Wires the cawl core (control plane + runtime + ingress) to Django settings."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from cawl_core.access import (
    AccessProvider, BridgeAccess, JumpAccess, TailscaleAccess,
)
from cawl_core.config import TemplateConfig, load_template_config_text
from cawl_core.egress import EgressPolicy, EgressProvider, NetworkEgress, NoEgress, ProxyEgress
from cawl_core.control import ControlPlane
from cawl_core.ingress import TraefikIngress
from cawl_core.runtime.base import Runtime
from cawl_core.ssh_ca import SshCertAuthority

from .store import DjangoStateStore

_backends = None


def _load_class(path: str, base: type, setting: str) -> type:
    """Import an operator-installed class named by a dotted path in `setting`.

    The class is instantiated with no arguments and reads whatever settings it
    needs from the environment itself — cawl can't know what a third-party
    backend's endpoint or credentials look like.
    """
    try:
        cls = import_string(path)
    except ImportError as e:
        raise ImproperlyConfigured(
            f"{setting}={path!r} could not be imported: {e}") from e
    if not (isinstance(cls, type) and issubclass(cls, base)):
        raise ImproperlyConfigured(
            f"{setting}={path!r} is not a {base.__name__} subclass")
    return cls


def get_ca() -> SshCertAuthority | None:
    """The SSH CA, or None when the deployment hasn't configured one (SSH off)."""
    if not settings.CAWL_SSH_CA_KEY:
        return None
    return SshCertAuthority(settings.CAWL_SSH_CA_KEY,
                            user_ttl=settings.CAWL_SSH_CERT_TTL)


def get_backends():
    """The deployment's named backends — cached per process.

    Every entry is one way to materialize an environment. cawl attaches no
    meaning to the names; with Incus the natural registry is "container"
    (dense, shared kernel) and "vm" (KVM boundary — what agent tokens get
    forced onto). A new runtime family means a new entry here, nothing more.
    """
    global _backends
    if _backends is None:
        _backends = {**_builtin_backends(), **_extra_backends()}
    return _backends


def _builtin_backends() -> dict[str, Runtime]:
    kind = settings.CAWL_RUNTIME
    if kind == "fake":
        from cawl_core.runtime.fake import FakeRuntime
        return {"container": FakeRuntime(), "vm": FakeRuntime(vm=True)}
    if kind == "incus_api":
        from cawl_core.runtime.incus_api import IncusApiRuntime

        if not settings.CAWL_INCUS_SERVER_CERT:
            raise ImproperlyConfigured(
                "CAWL_RUNTIME=incus_api requires CAWL_INCUS_SERVER_CERT; "
                "refusing to connect to Incus without a pinned server certificate")

        def incus(vm):
            return IncusApiRuntime(
                endpoint=settings.CAWL_INCUS_URL,
                client_cert=settings.CAWL_INCUS_CLIENT_CERT,
                client_key=settings.CAWL_INCUS_CLIENT_KEY,
                server_cert=settings.CAWL_INCUS_SERVER_CERT,
                project=settings.CAWL_INCUS_PROJECT,
                image_prefix=settings.CAWL_IMAGE_PREFIX,
                vm=vm,
            )
        return {"container": incus(False), "vm": incus(True)}
    if kind == "none":
        return {}  # a deployment running only operator-installed backends
    raise ImproperlyConfigured(
        f"unknown CAWL_RUNTIME {kind!r} (expected incus_api, fake, or none)")


def _extra_backends() -> dict[str, Runtime]:
    """Operator-installed backends: ``CAWL_EXTRA_BACKENDS=name=dotted.path,…``.

    Merged over the built-in registry (an entry may also shadow a built-in
    name). Installation is Python packaging: put the module on the daemon's
    venv/PYTHONPATH and name its Runtime subclass here — see docs/extending.md.
    """
    extras = {}
    for entry in filter(None, (e.strip() for e in
                               settings.CAWL_EXTRA_BACKENDS.split(","))):
        name, sep, path = entry.partition("=")
        if not sep or not name.strip() or "." not in path:
            raise ImproperlyConfigured(
                f"CAWL_EXTRA_BACKENDS entry {entry!r} is not name=dotted.path")
        extras[name.strip()] = _load_class(
            path.strip(), Runtime, "CAWL_EXTRA_BACKENDS")()
    return extras


def build_access() -> AccessProvider:
    """How users reach a box's sshd — the operator's deployment-wide choice.

    ``CAWL_ACCESS`` names the provider explicitly — a built-in, or the dotted
    path of an operator-installed AccessProvider subclass; naming one whose
    settings are missing is a config error, not a silent fallback. Unset, the
    provider is inferred from which settings are present: an authkey means
    Tailscale, else a jump host means jump, else bridge.
    """
    if "." in settings.CAWL_ACCESS:
        return _load_class(settings.CAWL_ACCESS, AccessProvider, "CAWL_ACCESS")()
    kind = settings.CAWL_ACCESS or (
        "tailscale" if settings.CAWL_TAILSCALE_AUTHKEY
        else "jump" if settings.CAWL_SSH_JUMP
        else "bridge")
    if kind == "tailscale":
        if not settings.CAWL_TAILSCALE_AUTHKEY:
            raise ImproperlyConfigured(
                "CAWL_ACCESS=tailscale needs CAWL_TAILSCALE_AUTHKEY")
        return TailscaleAccess(settings.CAWL_TAILSCALE_AUTHKEY,
                               tailnet=settings.CAWL_TAILNET,
                               tags=settings.CAWL_TAILSCALE_TAGS)
    if kind == "jump":
        if not settings.CAWL_SSH_JUMP:
            raise ImproperlyConfigured("CAWL_ACCESS=jump needs CAWL_SSH_JUMP")
        return JumpAccess(settings.CAWL_SSH_JUMP)
    if kind == "bridge":
        return BridgeAccess()
    raise ImproperlyConfigured(
        f"unknown CAWL_ACCESS {settings.CAWL_ACCESS!r} "
        "(expected tailscale, jump, or bridge)")


def build_egress() -> EgressProvider:
    """Build the outbound-connectivity adapter.

    This is a runtime-facing attachment, not guest configuration. Keep it
    separate from SSH access providers.
    """
    if "." in settings.CAWL_EGRESS:
        return _load_class(settings.CAWL_EGRESS, EgressProvider, "CAWL_EGRESS")()
    if settings.CAWL_EGRESS in ("", "none"):
        return NoEgress()
    if settings.CAWL_EGRESS == "network":
        if not settings.CAWL_EGRESS_NETWORK:
            raise ImproperlyConfigured("CAWL_EGRESS=network needs CAWL_EGRESS_NETWORK")
        try:
            return NetworkEgress(settings.CAWL_EGRESS_NETWORK)
        except ValueError as e:
            raise ImproperlyConfigured(str(e)) from e
    if settings.CAWL_EGRESS == "proxy":
        if not settings.CAWL_EGRESS_NETWORK or not settings.CAWL_EGRESS_PROXY_URL:
            raise ImproperlyConfigured(
                "CAWL_EGRESS=proxy needs CAWL_EGRESS_NETWORK and CAWL_EGRESS_PROXY_URL")
        try:
            return ProxyEgress(
                settings.CAWL_EGRESS_NETWORK, settings.CAWL_EGRESS_PROXY_URL,
                policy=EgressPolicy(settings.CAWL_EGRESS_POLICY_NAME,
                                    settings.CAWL_EGRESS_ALLOWED_HOSTS),
                policy_store=settings.CAWL_EGRESS_POLICY_STORE)
        except ValueError as e:
            raise ImproperlyConfigured(str(e)) from e
    raise ImproperlyConfigured(
        f"unknown CAWL_EGRESS {settings.CAWL_EGRESS!r} (expected none, network, or proxy)")


def build_ingress() -> TraefikIngress:
    return TraefikIngress(
        settings.CAWL_INGRESS_DIR, settings.CAWL_BASE_DOMAIN,
        forward_auth_url=settings.CAWL_FORWARD_AUTH_URL,
        daemon_url=settings.CAWL_DAEMON_URL,
        auth_host=settings.CAWL_AUTH_HOST,
    )


def build_control() -> ControlPlane:
    return ControlPlane(
        state=DjangoStateStore(),
        runtime=get_backends(),
        default_backend=settings.CAWL_DEFAULT_BACKEND,
        ingress=build_ingress(),
        access=build_access(),
        egress=build_egress(),
        default_quota=settings.CAWL_DEFAULT_QUOTA,
        ca=get_ca(),
        ssh_user=settings.CAWL_SSH_USER,
    )


def config_for(template) -> TemplateConfig:
    """Parse a Template's stored template.yaml body into a TemplateConfig."""
    return load_template_config_text(template.raw_yaml)


def config_for_environment(sb) -> TemplateConfig:
    """The config a *running* env is governed by: the template version it was
    launched from, not whatever the template has since become. Resuming an env
    must bring back the env you stopped — not silently migrate it to a new
    template."""
    from .models import TemplateVersion

    pinned = (TemplateVersion.objects
              .filter(template=sb.template, version=sb.template_version).first())
    return load_template_config_text(pinned.raw_yaml if pinned else sb.template.raw_yaml)
