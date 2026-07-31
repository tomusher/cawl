"""Request → Principal resolution for the Ninja API.

Two authenticators, tried in order:
  1. Bearer token (agents) — a minted ApiToken carries subject/role/quota.
  2. Session (humans) — a Django user authenticated via OIDC; admin role from
     ``is_superuser``.

This is the daemon's trust boundary: identity comes from a verified token or an
authenticated session, never from a client-supplied field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone
from ninja.security import HttpBearer, SessionAuth

from cawl_core.auth import Principal, Role
from cawl_core.naming import parse_ttl


@dataclass
class AuthContext:
    principal: Principal
    quota: int | None = None
    # Guardrails on what this caller creates (see ApiToken): a lifetime cap,
    # and a forced backend ("" = caller's choice).
    max_ttl: timedelta | None = None
    backend: str = ""


def role_for_user(user) -> Role:
    """Map Django privileges to cawl's deployment-wide role consistently."""
    return Role.admin if user.is_superuser else Role.user


def _principal_for_user(user) -> Principal:
    return Principal(user.get_username(), role_for_user(user))


def authenticate_token(raw: str, now=None) -> AuthContext | None:
    from .models import ApiToken
    try:
        tok = ApiToken.objects.get(key_hash=ApiToken.hash_key(raw))
    except ApiToken.DoesNotExist:
        return None
    if not tok.is_valid(now):
        return None
    tok.last_used_at = now or timezone.now()
    tok.save(update_fields=["last_used_at"])
    return AuthContext(Principal(tok.subject, Role(tok.role)), tok.quota,
                       parse_ttl(tok.max_ttl or None), tok.backend)


class TokenAuth(HttpBearer):
    def authenticate(self, request, token):
        return authenticate_token(token)


class SessionContextAuth(SessionAuth):
    """Django session auth with Ninja's cookie CSRF enforcement intact."""

    def authenticate(self, request, key):
        user = super().authenticate(request, key)
        if user is not None:
            return AuthContext(_principal_for_user(user))
        return None


# Keep bearer auth first: token-only clients do not need CSRF protection. If it
# does not authenticate, SessionContextAuth validates CSRF before trusting the
# browser's session cookie.
AUTH = [TokenAuth(), SessionContextAuth()]
