from __future__ import annotations

from typing import Literal, Optional

from ninja import Field, Schema


class UpIn(Schema):
    template: str
    # Template args (`--arg k=v`). What they mean is the template's business; the
    # daemon only checks them against the params it declares.
    args: dict[str, str] = Field(default_factory=dict)
    name: Optional[str] = None  # custom environment id; generated when omitted
    ttl: Optional[str] = None
    # Which named backend materializes the env; names are the deployment's own
    # (an Incus deployment tends to have "vm" and "container").
    backend: Optional[str] = None
    reuse: bool = False
    owner: Optional[str] = None  # admin-only: on behalf of


class RefreshIn(Schema):
    template: str
    args: dict[str, str] = Field(default_factory=dict)
    backend: Optional[str] = None  # images are per-backend; None = the default


class ExposureOut(Schema):
    name: str
    port: int
    url: str
    access: list[str] = Field(default_factory=list)
    # Magic links, one per access email — returned only to the owner from
    # expose, so they can hand them out. Never listed back afterwards.
    links: Optional[dict[str, str]] = None


class EnvironmentOut(Schema):
    id: str
    template: str
    args: dict[str, str] = Field(default_factory=dict)
    backend: str = ""
    owner: str
    status: str
    vm_ip: Optional[str] = None
    url: Optional[str] = None
    ssh: Optional[str] = None
    image: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    shared_with: list[str] = Field(default_factory=list)
    exposures: list[ExposureOut] = Field(default_factory=list)

    @staticmethod
    def of(inst) -> dict:
        from .services import build_ingress
        d = inst.to_dict()
        ingress = build_ingress()
        for e in d["exposures"]:
            e["url"] = ingress.url_for(e["name"])
        return d


class ExposeIn(Schema):
    port: int
    # The hostname label: <name>.<base domain>. Any free label; None => the
    # environment's own id.
    name: Optional[str] = None
    # Emails to admit via magic link, on top of everyone with access to the env.
    access: list[str] = Field(default_factory=list)


class TemplateIn(Schema):
    yaml: str  # a template.yaml body; the name is taken from its `name:` key
    # Omitted means personal for users and global for admins, preserving the
    # original admin-only publishing workflow.
    scope: Optional[Literal["personal", "global"]] = None


class TemplateOut(Schema):
    name: str
    owner: str = ""  # blank means an admin-published global template
    scope: str
    params: str  # declared arg names, comma-joined (display only)
    version: int
    active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    yaml: Optional[str] = None  # included by `show`, omitted from `ls`

    @staticmethod
    def of(t, *, body: bool = False) -> dict:
        d = {
            "name": t.name, "owner": t.owner,
            "scope": "global" if not t.owner else "personal",
            "params": t.params, "version": t.version, "active": t.active,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }
        if body:
            d["yaml"] = t.raw_yaml
        return d


class ShareIn(Schema):
    principal: str  # who to share with (or un-share from)


class SshCertIn(Schema):
    public_key: str  # the caller's SSH public key, to be signed for this env only


class SshCertOut(Schema):
    """One session's worth of access. Nothing here is durable: the cert expires in
    minutes, and it names one environment."""

    host: str
    user: str
    certificate: str
    ca_pubkey: str  # pin as @cert-authority so the box verifies without TOFU
    jump: Optional[str] = None  # relay hop to reach host, if the provider needs one


class ExecIn(Schema):
    cmd: list[str]


class ExecOut(Schema):
    exit_code: int
    stdout: str
    stderr: str


class WhoAmI(Schema):
    id: str
    role: str
    quota: Optional[int] = None
