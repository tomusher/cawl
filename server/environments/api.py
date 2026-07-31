"""The Django Ninja API the `cawl` CLI (and agents) talk to.

Thin: every endpoint resolves the principal (via auth), then delegates to the
cawl ControlPlane, which enforces the authorization policy. Primary auth is
bearer tokens (agents); browser session auth uses Ninja's cookie authenticator
so unsafe requests require a valid CSRF token.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import NinjaAPI

from cawl_core.auth import require_admin
from cawl_core.config import ConfigError, load_template_config_text
from cawl_core.control import (
    CawlError, NameConflict, NotFound, PermissionDenied, QuotaExceeded,
)

from .auth import AUTH
from .models import (
    Environment, EnvironmentEvent, Template, TemplateNameLock, TemplateVersion,
)
from .schemas import (
    ExecIn, ExecOut, ExposeIn, ExposureOut, RefreshIn, EnvironmentOut, ShareIn,
    SshCertIn, SshCertOut, TemplateIn, TemplateOut, UpIn, WhoAmI,
)
from .services import build_control, config_for, config_for_environment

api = NinjaAPI(title="cawl", version="1.0", auth=AUTH)


def visible_templates(principal):
    """Global templates plus a user's own private templates (all for admins)."""
    qs = Template.objects.all()
    return qs if principal.is_admin else qs.filter(Q(owner="") | Q(owner=principal.id))


def accessible_template(name, principal, *, active=None):
    qs = visible_templates(principal).filter(name=name)
    if active is not None:
        qs = qs.filter(active=active)
    return get_object_or_404(qs)


def require_template_owner(template, principal, action):
    if not principal.is_admin and template.owner != principal.id:
        raise PermissionDenied(f"{principal.id!r} may not {action} template {template.name!r}")


@api.exception_handler(NotFound)
def _not_found(request, exc):
    return api.create_response(request, {"error": str(exc)}, status=404)


@api.exception_handler(ConfigError)
def _bad_config(request, exc):
    return api.create_response(request, {"error": str(exc)}, status=400)


@api.exception_handler(PermissionDenied)
def _denied(request, exc):
    return api.create_response(request, {"error": str(exc)}, status=403)


@api.exception_handler(QuotaExceeded)
def _quota(request, exc):
    return api.create_response(request, {"error": str(exc)}, status=409)


@api.exception_handler(NameConflict)
def _name_conflict(request, exc):
    return api.create_response(request, {"error": str(exc)}, status=409)


# InvalidName has no handler of its own: it's a CawlError, so it lands here as a 400.
@api.exception_handler(CawlError)
def _cawl_error(request, exc):
    return api.create_response(request, {"error": str(exc)}, status=400)


@api.get("/whoami", response=WhoAmI)
def whoami(request):
    ctx = request.auth
    return {"id": ctx.principal.id, "role": ctx.principal.role.value, "quota": ctx.quota}


@api.post("/environments", response=EnvironmentOut)
def create(request, payload: UpIn):
    ctx = request.auth
    tmpl = accessible_template(payload.template, ctx.principal, active=True)
    cfg = config_for(tmpl)
    res = build_control().up(
        cfg, actor=ctx.principal,
        args=payload.args, name=payload.name, ttl=payload.ttl,
        # A token-level backend guardrail beats whatever the request says.
        backend=ctx.backend or payload.backend, reuse=payload.reuse,
        on_behalf_of=payload.owner, quota=ctx.quota, max_ttl=ctx.max_ttl,
    )
    return EnvironmentOut.of(res.instance)


@api.get("/environments", response=list[EnvironmentOut])
def list_environments(request, template: str = None):
    ctx = request.auth
    insts = build_control().ls(ctx.principal, template=template)
    return [EnvironmentOut.of(i) for i in insts]


@api.get("/environments/{sid}", response=EnvironmentOut)
def get_environment(request, sid: str):
    inst = build_control().status(sid, request.auth.principal)
    return EnvironmentOut.of(inst)


@api.post("/environments/{sid}/exec", response=ExecOut)
def exec_in(request, sid: str, payload: ExecIn):
    res = build_control().exec(sid, request.auth.principal, payload.cmd)
    return {"exit_code": res.exit_code, "stdout": res.stdout, "stderr": res.stderr}


@api.post("/environments/{sid}/stop", response=EnvironmentOut)
def stop(request, sid: str):
    """Shut an env down, keeping its disk. Frees the RAM; `start` brings it back."""
    inst = build_control().stop(sid, request.auth.principal)
    return EnvironmentOut.of(inst)


@api.post("/environments/{sid}/start", response=EnvironmentOut)
def start(request, sid: str):
    sb = get_object_or_404(Environment, pk=sid)
    res = build_control().start(sid, request.auth.principal, config_for_environment(sb))
    return EnvironmentOut.of(res.instance)


@api.post("/environments/{sid}/exposures", response=ExposureOut)
def expose(request, sid: str, payload: ExposeIn):
    """Export a port to the web behind the forward-auth gate (owner only).

    Returns the exposure's URL, plus a magic sign-in link per access email for
    the owner to hand out (each also goes out by email when email is set up).
    """
    from .webauth import magic_link
    control = build_control()
    inst = control.expose(
        sid, request.auth.principal, port=payload.port, name=payload.name,
        access=tuple(payload.access))
    label = (payload.name or inst.id).strip().lower()
    exp = next(e for e in inst.exposures if e.name == label)
    url = control.ingress.url_for(exp.name)
    return {
        "name": exp.name, "port": exp.port, "url": url,
        "access": list(exp.access),
        "links": {email: magic_link(email, url) for email in exp.access},
    }


@api.delete("/environments/{sid}/exposures/{name}", response=EnvironmentOut)
def unexpose(request, sid: str, name: str):
    inst = build_control().unexpose(sid, request.auth.principal, name)
    return EnvironmentOut.of(inst)


@api.post("/environments/{sid}/ssh-cert", response=SshCertOut)
def ssh_cert(request, sid: str, payload: SshCertIn):
    """Sign a short-lived certificate for one environment.

    This is the SSH authorization boundary. The cert's principal is the environment's
    id, and the box trusts only certs bearing its own id — so declining to sign
    is the same as locking the door. The check is the one that guards `exec`.
    """
    actor = request.auth.principal
    access = build_control().ssh(sid, actor, payload.public_key)
    EnvironmentEvent.objects.create(
        environment=Environment.objects.get(pk=sid), kind="ssh-cert", actor=actor.id)
    return access.__dict__


@api.get("/environments/{sid}/grants", response=list[str])
def list_grants(request, sid: str):
    return build_control().status(sid, request.auth.principal).shared_with


@api.post("/environments/{sid}/grants", response=EnvironmentOut)
def add_grant(request, sid: str, payload: ShareIn):
    """Share an env (owner only). A row in the DB — the box is never touched."""
    inst = build_control().share(sid, request.auth.principal, payload.principal)
    return EnvironmentOut.of(inst)


@api.delete("/environments/{sid}/grants/{principal}", response=EnvironmentOut)
def remove_grant(request, sid: str, principal: str):
    inst = build_control().unshare(sid, request.auth.principal, principal)
    return EnvironmentOut.of(inst)


@api.delete("/environments/{sid}")
def destroy(request, sid: str):
    build_control().destroy(sid, request.auth.principal)
    return {"destroyed": sid}


@api.post("/images/refresh")
def refresh_image(request, payload: RefreshIn):
    tmpl = accessible_template(payload.template, request.auth.principal)
    image = build_control().refresh_image(
        config_for(tmpl), request.auth.principal, args=payload.args,
        backend=payload.backend)
    return {"image": image}


# -- templates: the registry of environment definitions, DB-backed ----------
@api.post("/templates", response=TemplateOut)
def create_template(request, payload: TemplateIn):
    """Create or version a personal template; admins may publish global ones.

    Handles are deployment-wide unique, so a template can never silently shadow
    another user's template. Running environments keep their launched version.
    """
    principal = request.auth.principal
    global_template = (payload.scope == "global" or
                       (payload.scope is None and principal.is_admin))
    if global_template:
        require_admin(principal, "publish global templates")
    owner = "" if global_template else principal.id
    cfg = load_template_config_text(payload.yaml)  # validate; ConfigError -> 400
    params = ",".join(sorted(cfg.params))
    try:
        with transaction.atomic():
            # A stable lock row also serializes the first creation, when there
            # is no Template row for select_for_update() to lock yet.
            TemplateNameLock.objects.get_or_create(name=cfg.name)
            TemplateNameLock.objects.select_for_update().get(name=cfg.name)
            tmpl = Template.objects.select_for_update().filter(name=cfg.name).first()
            if tmpl and tmpl.owner != owner:
                raise NameConflict(f"template name {cfg.name!r} is already in use")
            if tmpl is None:
                tmpl = Template.objects.create(
                    name=cfg.name, owner=owner, raw_yaml=payload.yaml, params=params)
            else:
                tmpl.version += 1
                tmpl.raw_yaml = payload.yaml
                tmpl.params = params
                tmpl.active = True
                tmpl.updated_at = timezone.now()
                tmpl.save()
            TemplateVersion.objects.create(
                template=tmpl, version=tmpl.version, raw_yaml=payload.yaml,
                params=params, created_by=principal.id)
    except IntegrityError as exc:
        # Defensive translation for databases with weaker locking semantics.
        raise NameConflict(f"template name {cfg.name!r} was concurrently updated") from exc
    return TemplateOut.of(tmpl)


@api.get("/templates", response=list[TemplateOut])
def list_templates(request, active: bool = None):
    qs = visible_templates(request.auth.principal)
    if active is not None:
        qs = qs.filter(active=active)
    qs = qs.order_by("name")
    return [TemplateOut.of(t) for t in qs]


@api.get("/templates/{name}", response=TemplateOut)
def get_template(request, name: str):
    tmpl = accessible_template(name, request.auth.principal)
    return TemplateOut.of(tmpl, body=True)


@api.delete("/templates/{name}")
def delete_template(request, name: str):
    """Deactivate one of your templates (or any template as an admin).

    A soft delete — existing environments reference the template (PROTECT), so it's
    never hard-deleted out from under them.
    """
    tmpl = accessible_template(name, request.auth.principal)
    require_template_owner(tmpl, request.auth.principal, "deactivate")
    tmpl.active = False
    tmpl.updated_at = timezone.now()
    tmpl.save(update_fields=["active", "updated_at"])
    return {"deactivated": name}
