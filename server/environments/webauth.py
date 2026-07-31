"""Browser access to exposures: forward-auth + magic-link login.

Traefik calls ``/auth/forward`` for every request to an exposed host (fail-closed
— the middleware is on every exposure router). Identity comes from a signed,
host-scoped cookie; authorization is decided here, per request, from the
Exposure row — so revoking an email or unexposing a port takes effect on the
next request, whatever cookies are still out there.

The login flow is a code-for-cookie handoff, deliberately not a parent-domain
cookie (Traefik forwards request cookies into the VM, and agent VMs run
untrusted code — a parent-scoped session would be readable by every exposed
app). Instead:

  browser -> exposed host        no valid cookie -> 302 to the auth host
  auth host (/auth/sso)          has a viewer cookie for this exposure host?
                                 mint a 60s handoff code bound to the host
  exposed host (/.cawl/callback) redeem code -> set __Host- cookie -> original URL

The cookie names the host it was minted for and is checked against the
forwarded host, so a cookie harvested by a malicious app in one VM is useless
against any other exposure.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.utils import timezone
from django.core.mail import send_mail
from django.http import (
    HttpResponse, HttpResponseNotFound, HttpResponseRedirect,
)
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from cawl_core.auth import Principal, can_view
from cawl_core.naming import exposure_label

from .auth import role_for_user
from .models import Exposure, Environment, ViewerMagicToken
from .store import DjangoStateStore

COOKIE = "__Host-cawl"        # host-scoped by definition: Secure, Path=/, no Domain
COOKIE_SALT = "cawl.web-session"
SSO_COOKIE = "__Host-cawl-sso"  # auth-host only; deliberately not a Django session
SSO_COOKIE_SALT = "cawl.viewer-sso"
CODE_SALT = "cawl.handoff-code"
CODE_MAX_AGE = 60             # seconds; it crosses one redirect

ALIVE = ("creating", "ready", "stopped")


# -- identity helpers -------------------------------------------------------
def email_of(user) -> str:
    return (user.email or user.get_username()).strip().lower()


def account_for_email(email: str):
    """Return an existing team account for policy mapping, if there is one.

    Exposure viewers never create Django users; this lookup only lets an
    existing team member's verified email map to their cawl principal.
    """
    User = get_user_model()
    return (User.objects.filter(email__iexact=email).order_by("pk").first()
            or User.objects.filter(username__iexact=email).first())


def principal_for_email(email: str) -> Principal:
    """Map a verified email to the principal the access policy speaks.

    A matching account's username is the principal (that's how ``cawl share
    --with sue`` reaches sue@…); with no account, the email itself is the
    principal, which only ever matches explicit access-list entries.
    """
    user = account_for_email(email)
    if user and "@" not in user.username:
        return Principal(user.username, role_for_user(user))
    return Principal(email.strip().lower())


def _exposure_for_host(host: str) -> tuple[Environment, Exposure] | None:
    label = exposure_label(host, settings.CAWL_PUBLIC_DOMAIN)
    if not label:
        return None
    exp = (Exposure.objects.filter(name=label, environment__status__in=ALIVE)
           .select_related("environment").first())
    return (exp.environment, exp) if exp else None


def _authorized(email: str, sb: Environment, exp: Exposure) -> bool:
    email = email.strip().lower()
    if email in [a.strip().lower() for a in (exp.access or [])]:
        return True
    inst = DjangoStateStore().get(sb.id)
    return inst is not None and can_view(principal_for_email(email), inst)


def magic_link(email: str, next_url: str) -> str:
    """Mint a one-time viewer credential scoped to ``next_url``'s host."""
    target = _valid_next(next_url)
    if not target:
        raise ValueError("magic-link target is not a cawl exposure")
    _, raw = ViewerMagicToken.mint(
        email=email, host=target[0],
        ttl=timedelta(seconds=settings.CAWL_MAGIC_TTL),
    )
    return (f"https://{settings.CAWL_AUTH_HOST}/auth/magic?"
            + urlencode({"token": raw, "next": next_url}))


def _consume_magic_token(raw: str, expected_host: str | None) -> str | None:
    token = ViewerMagicToken.objects.filter(
        key_hash=ViewerMagicToken.hash_key(raw)).first()
    now = timezone.now()
    if (token is None or token.consumed_at is not None
            or token.expires_at <= now):
        return None
    # Check the target before consuming so a tampered link cannot burn the
    # legitimate one. An absent host is handled as an invalid link by _handoff.
    if expected_host is not None and token.host != expected_host:
        return None
    if expected_host is not None:
        # The conditional update makes consumption atomic even on SQLite,
        # where select_for_update() would not serialize simultaneous requests.
        claimed = ViewerMagicToken.objects.filter(
            pk=token.pk, consumed_at__isnull=True, expires_at__gt=now,
        ).update(consumed_at=now)
        if claimed != 1:
            return None
    return token.email


# -- the forward-auth decision ---------------------------------------------
def forward_auth(request):
    """Traefik's per-request subrequest. 2xx allows; anything else is returned
    to the browser (302 -> login, 403 -> denied page, 404 -> no such exposure)."""
    host = (request.headers.get("X-Forwarded-Host") or "").split(":")[0].lower()
    found = _exposure_for_host(host)
    if not found:
        return HttpResponseNotFound("no such exposure")
    sb, exp = found

    email = _cookie_email(request, host)
    if email is None:
        proto = request.headers.get("X-Forwarded-Proto") or "https"
        uri = request.headers.get("X-Forwarded-Uri") or "/"
        original = f"{proto}://{host}{uri}"
        return HttpResponseRedirect(
            f"https://{settings.CAWL_AUTH_HOST}/auth/sso?"
            + urlencode({"next": original}))

    if not _authorized(email, sb, exp):
        resp = render(request, "webauth_message.html", {
            "title": "No access",
            "message": f"{email} does not have access to {host}. If you were "
                       "given a different address, sign out and use its link.",
            "logout": "/.cawl/logout",
        }, status=403)
        return resp

    resp = HttpResponse("ok")
    resp["X-Cawl-User"] = email
    return resp


def _cookie_email(request, host: str) -> str | None:
    raw = request.COOKIES.get(COOKIE)
    if not raw:
        return None
    try:
        data = signing.loads(raw, salt=COOKIE_SALT,
                             max_age=settings.CAWL_WEB_SESSION_TTL)
    except signing.BadSignature:
        return None
    # The browser scopes the cookie to one host; this check is for everyone
    # else. A cookie exfiltrated by an app in a VM names that VM's host, so it
    # cannot be replayed against another exposure.
    if data.get("h") != host:
        return None
    return data.get("e")


# -- the auth host: viewer SSO + magic-link login ---------------------------
def _valid_next(next_url: str) -> tuple[str, str, str] | None:
    """(host, scheme, path+query) — only ever an exposure host we route for."""
    try:
        u = urlparse(next_url)
    except ValueError:
        return None
    if u.scheme not in ("https", "http") or not u.hostname:
        return None
    host = u.hostname.lower()
    if not exposure_label(host, settings.CAWL_PUBLIC_DOMAIN):
        return None
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return host, u.scheme, path


def _handoff(request, email: str, next_url: str):
    """Mint the one-time code and send the browser to the exposed host's
    callback, which trades it for the host-scoped cookie."""
    target = _valid_next(next_url)
    if not target:
        return render(request, "webauth_message.html", {
            "title": "Invalid link",
            "message": "That link doesn't point at a cawl environment.",
        }, status=400)
    host, scheme, path = target
    code = signing.dumps({"e": email.strip().lower(), "h": host}, salt=CODE_SALT)
    return HttpResponseRedirect(
        f"{scheme}://{host}/.cawl/callback?" + urlencode({"code": code, "next": path}))


def _sso_identity(request) -> tuple[str, str] | None:
    """Return the viewer email and exposure host from the auth-host cookie.

    Magic links write this cookie rather than a Django session. Existing team
    members may separately use their Django session in ``sso`` below.
    """
    raw = request.COOKIES.get(SSO_COOKIE)
    if not raw:
        return None
    try:
        data = signing.loads(raw, salt=SSO_COOKIE_SALT,
                             max_age=settings.CAWL_WEB_SESSION_TTL)
    except signing.BadSignature:
        return None
    if not data.get("e") or not data.get("h"):
        return None
    return data["e"], data["h"]


@require_http_methods(["GET"])
def sso(request):
    next_url = request.GET.get("next", "")
    target = _valid_next(next_url)
    identity = _sso_identity(request)
    email = (identity[0] if identity and target and identity[1] == target[0]
             else None)
    # One-way bridge for team members: an existing Django login may identify a
    # viewer, but viewer magic links never create a Django login.
    if not email and request.user.is_authenticated:
        email = email_of(request.user)
    if email:
        return _handoff(request, email, next_url)
    return render(request, "weblogin.html", {"next": next_url})


@require_http_methods(["POST"])
def login_submit(request):
    """The email form. Sends a magic link — but only when that address would
    actually be let in, and without revealing which of the two it was."""
    email = (request.POST.get("email") or "").strip().lower()
    next_url = request.POST.get("next", "")
    target = _valid_next(next_url)
    if email and target:
        found = _exposure_for_host(target[0])
        if found and _authorized(email, *found):
            send_mail(
                subject="Your cawl sign-in link",
                message=("Follow this link to open the page you requested:\n\n"
                         f"{magic_link(email, next_url)}\n\n"
                         "If you didn't request this, ignore this email."),
                from_email=None,  # DEFAULT_FROM_EMAIL
                recipient_list=[email],
            )
    return render(request, "webauth_message.html", {
        "title": "Check your email",
        "message": f"If {email or 'that address'} has access, a sign-in link "
                   "is on its way.",
    })


@require_http_methods(["GET"])
def magic(request):
    """Land a magic link, establish viewer SSO, then hand off to the exposure.

    Viewer SSO is a separately signed cookie, not a django.contrib.auth
    session, and the token is backed by a viewer-only record rather than a
    Django user.
    """
    next_url = request.GET.get("next", "")
    target = _valid_next(next_url)
    email = _consume_magic_token(
        request.GET.get("token", ""), target[0] if target else None)
    if email is None:
        return render(request, "webauth_message.html", {
            "title": "Link expired",
            "message": "This sign-in link is no longer valid. Ask for a fresh "
                       "one, or use the email form on the page you came from.",
        }, status=410)
    response = _handoff(request, email, next_url)
    if response.status_code == 302:
        response.set_cookie(
            SSO_COOKIE,
            signing.dumps({"e": email, "h": target[0]}, salt=SSO_COOKIE_SALT),
            max_age=settings.CAWL_WEB_SESSION_TTL,
            secure=True, httponly=True, samesite="Lax", path="/",
        )
    return response


# -- on every exposed host: /.cawl/* (routed to the daemon, never the VM) --
@require_http_methods(["GET"])
def callback(request):
    """Trade a one-time code for the host-scoped session cookie."""
    host = request.get_host().split(":")[0].lower()
    try:
        data = signing.loads(request.GET.get("code", ""), salt=CODE_SALT,
                             max_age=CODE_MAX_AGE)
    except signing.BadSignature:
        return render(request, "webauth_message.html", {
            "title": "Sign-in expired",
            "message": "That sign-in attempt timed out — go back and try again.",
        }, status=400)
    if data.get("h") != host:
        return HttpResponse("code is for a different host", status=400)

    next_path = request.GET.get("next", "/")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    resp = HttpResponseRedirect(next_path)
    resp.set_cookie(
        COOKIE,
        signing.dumps({"e": data["e"], "h": host}, salt=COOKIE_SALT),
        max_age=settings.CAWL_WEB_SESSION_TTL,
        secure=True, httponly=True, samesite="Lax", path="/",
    )
    return resp


@require_http_methods(["GET"])
def logout_view(request):
    resp = render(request, "webauth_message.html", {
        "title": "Signed out",
        "message": "You are signed out of this environment.",
    })
    resp.delete_cookie(COOKIE, path="/")
    return resp
