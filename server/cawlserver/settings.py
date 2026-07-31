"""Django settings for the cawl control-plane daemon.

The daemon is the trust boundary: it owns the database + the Incus host and
enforces authorization. It reuses the `cawl` package (control plane, runtime,
ingress, auth policy) as a library.
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent          # server/
REPO_ROOT = BASE_DIR.parent                                # repo root

# Dev convenience: cawl_core + the Django project live under server/, so make
# that importable without an install (gunicorn/manage.py both run from here).
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _load_env_file(path: Path) -> None:
    """Minimal .env loader (no dependency). Real env vars win over the file, so
    real process environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if val[:1] in ("'", '"'):                       # quoted: take up to close
            end = val.find(val[0], 1)
            val = val[1:end] if end != -1 else val[1:]
        else:                                           # else drop inline comment
            for sep in (" #", "\t#"):
                val = val.split(sep, 1)[0]
            val = val.strip()
        os.environ.setdefault(key.strip(), val)


_load_env_file(Path(os.environ.get("CAWL_ENV_FILE", BASE_DIR / ".env")))


def _bool(name, default="0"):
    return os.environ.get(name, default) == "1"


DEBUG = _bool("CAWL_DEBUG", "0")
SECRET_KEY = os.environ.get("CAWL_SECRET_KEY", "dev-insecure-change-me")
if not DEBUG and SECRET_KEY in {
    "dev-insecure-change-me",
    "change-me-to-a-long-random-string",
    "replace-with-a-long-random-django-secret",
}:
    raise ImproperlyConfigured(
        "CAWL_SECRET_KEY must be set to a strong, unique value when CAWL_DEBUG=0")
ALLOWED_HOSTS = [h.strip() for h in
                 os.environ.get("CAWL_ALLOWED_HOSTS", "").split(",") if h.strip()]
# Reaching the daemon by MagicDNS name over the (trusted) tailnet should just
# work — allow any *.<tailnet> host. Raw tailnet IPs still need listing above.
_tailnet = os.environ.get("CAWL_TAILNET", "").strip()
if _tailnet and ALLOWED_HOSTS and "*" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("." + _tailnet)

# Traefik terminates TLS and preserves the original Host. Trust its scheme
# header so Django generates HTTPS URLs and applies transport protections.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = int(os.environ.get(
    "CAWL_HSTS_SECONDS", "0" if DEBUG else "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = _bool("CAWL_HSTS_INCLUDE_SUBDOMAINS", "1")
# Usually same-origin requests need no entry. This explicit list supports an
# operator's intentional cross-origin browser clients without trusting every
# untrusted workload under CAWL_BASE_DOMAIN.
CSRF_TRUSTED_ORIGINS = [origin.strip().rstrip("/") for origin in
    os.environ.get("CAWL_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]

# -- logging: everything to stdout (container logs) -------------------------
LOG_LEVEL = os.environ.get("CAWL_LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "cawl": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "cawl",
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# -- apps / middleware ----------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "environments",
]

OIDC_ENABLED = _bool("CAWL_OIDC_ENABLED")
if OIDC_ENABLED:
    INSTALLED_APPS.append("mozilla_django_oidc")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
]
if OIDC_ENABLED:
    AUTHENTICATION_BACKENDS.insert(
        0, "mozilla_django_oidc.auth.OIDCAuthenticationBackend"
    )
    # Populated from your IdP; only read when OIDC is enabled.
    OIDC_RP_CLIENT_ID = os.environ.get("OIDC_RP_CLIENT_ID", "")
    OIDC_RP_CLIENT_SECRET = os.environ.get("OIDC_RP_CLIENT_SECRET", "")
    OIDC_OP_AUTHORIZATION_ENDPOINT = os.environ.get("OIDC_OP_AUTH", "")
    OIDC_OP_TOKEN_ENDPOINT = os.environ.get("OIDC_OP_TOKEN", "")
    OIDC_OP_USER_ENDPOINT = os.environ.get("OIDC_OP_USER", "")
    OIDC_RP_SIGN_ALGO = os.environ.get("OIDC_RP_SIGN_ALGO", "RS256")

ROOT_URLCONF = "cawlserver.urls"
WSGI_APPLICATION = "cawlserver.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

# -- database -------------------------------------------------------------
# Single connection URL via CAWL_DATABASE_URL (dj-database-url); sqlite by default.
#   postgres://cawl:pw@127.0.0.1:5432/cawl
import dj_database_url  # noqa: E402

DATABASES = {
    "default": dj_database_url.config(
        env="CAWL_DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/oidc/authenticate/" if OIDC_ENABLED else "/admin/login/"

# -- cawl control-plane settings (consumed by environments.services) ----------
# Which family of built-in backends fills the registry: incus_api | fake |
# none ("none" = only CAWL_EXTRA_BACKENDS entries below).
CAWL_RUNTIME = os.environ.get("CAWL_RUNTIME", "incus_api")
# Which named backend `up` lands on when the request (or token) doesn't say.
# The registry itself lives in environments.services.get_backends.
CAWL_DEFAULT_BACKEND = os.environ.get("CAWL_DEFAULT_BACKEND", "container")
# Operator-installed backends, merged over the built-ins (and able to shadow
# them): "name=dotted.path,…" naming Runtime subclasses pip-installed into the
# daemon's venv. Each is constructed with no args and reads its own env vars.
CAWL_EXTRA_BACKENDS = os.environ.get("CAWL_EXTRA_BACKENDS", "")
# Incus REST API (CAWL_RUNTIME=incus_api): the daemon holds a client cert and
# pins the server's self-signed cert. No `incus` binary needed on this host.
CAWL_INCUS_URL = os.environ.get("CAWL_INCUS_URL", "https://localhost:8443")
CAWL_INCUS_CLIENT_CERT = os.environ.get("CAWL_INCUS_CLIENT_CERT", "")
CAWL_INCUS_CLIENT_KEY = os.environ.get("CAWL_INCUS_CLIENT_KEY", "")
CAWL_INCUS_SERVER_CERT = os.environ.get("CAWL_INCUS_SERVER_CERT", "") or None
CAWL_INCUS_PROJECT = os.environ.get("CAWL_INCUS_PROJECT", "default")
# SSH access provider (cawl_core/access.py): how developers reach a box's sshd.
# "tailscale" — instances join the tailnet at boot, as transport only; clients
#   dial MagicDNS names. Needs CAWL_TAILSCALE_AUTHKEY (ephemeral + reusable +
#   pre-authorized) and CAWL_TAILNET.
# "jump" — bridge IPs relayed through CAWL_SSH_JUMP, a [user@]host[:port]
#   developers can already SSH to (typically this host). Their hop credentials
#   are their own business, not cawl's.
# "bridge" — bridge IPs dialed directly; routing (LAN, your own VPN) is the
#   deployment's business.
# Unset = inferred: an authkey means tailscale, else a jump host means jump,
# else bridge. Authentication is always the box's own sshd + the CA below,
# whichever transport is picked.
CAWL_ACCESS = os.environ.get("CAWL_ACCESS", "")
CAWL_TAILSCALE_AUTHKEY = os.environ.get("CAWL_TAILSCALE_AUTHKEY", "")
CAWL_TAILSCALE_TAGS = os.environ.get("CAWL_TAILSCALE_TAGS", "")
CAWL_SSH_JUMP = os.environ.get("CAWL_SSH_JUMP", "")
# Outbound-connectivity provider (cawl_core/egress.py). "network" attaches
# a runtime-managed, deny-egress network outside the guest; it never depends on
# removable HTTP_PROXY variables. Operator adapters may be dotted class paths.
CAWL_EGRESS = os.environ.get("CAWL_EGRESS", "none")
CAWL_EGRESS_NETWORK = os.environ.get("CAWL_EGRESS_NETWORK", "")
CAWL_EGRESS_PROXY_URL = os.environ.get("CAWL_EGRESS_PROXY_URL", "")
# Server-selected exact DNS names. Templates and API arguments never contribute.
CAWL_EGRESS_POLICY_NAME = os.environ.get("CAWL_EGRESS_POLICY_NAME", "agent-default")
CAWL_EGRESS_ALLOWED_HOSTS = tuple(h.strip() for h in os.environ.get(
    "CAWL_EGRESS_ALLOWED_HOSTS", "").split(",") if h.strip())
CAWL_EGRESS_POLICY_STORE = os.environ.get(
    "CAWL_EGRESS_POLICY_STORE", "/var/lib/cawl/egress-policies.json")
# SSH certificate authority. Signing a cert is how the daemon lets someone into an
# environment, so this key mints access to every env: keep it root-only, and rotate it
# by re-running `refresh-image` (boxes take the CA at create time). Unset = no SSH.
CAWL_SSH_CA_KEY = os.environ.get("CAWL_SSH_CA_KEY", "")
CAWL_SSH_USER = os.environ.get("CAWL_SSH_USER", "dev")
# Long enough to connect, short enough that revoking a grant means something.
CAWL_SSH_CERT_TTL = os.environ.get("CAWL_SSH_CERT_TTL", "10m")
CAWL_INGRESS_DIR = os.environ.get("CAWL_INGRESS_DIR", str(BASE_DIR / ".dynamic"))
# Every exposure lives one label under this domain (<id>.<domain> or
# <name>--<id>.<domain>), so one wildcard DNS record and one wildcard cert
# cover all of them — and individual hostnames never reach CT logs.
CAWL_BASE_DOMAIN = os.environ.get("CAWL_BASE_DOMAIN", "sbx.example.com")
# Where magic-link login + SSO live. Served by this daemon; Traefik routes it.
CAWL_AUTH_HOST = os.environ.get("CAWL_AUTH_HOST", f"auth.{CAWL_BASE_DOMAIN}")
# How *Traefik* reaches this daemon (internal URL) — for the forward-auth
# subrequest and for proxying the auth host + /.cawl/ paths. Empty = ingress
# files are written without the shared auth pieces (dev/tests).
CAWL_DAEMON_URL = os.environ.get("CAWL_DAEMON_URL", "")
CAWL_FORWARD_AUTH_URL = os.environ.get(
    "CAWL_FORWARD_AUTH_URL",
    (CAWL_DAEMON_URL.rstrip("/") + "/auth/forward") if CAWL_DAEMON_URL else "")
# Host-scoped browser session and one-time magic-link lifetimes (seconds).
CAWL_WEB_SESSION_TTL = int(os.environ.get("CAWL_WEB_SESSION_TTL", str(12 * 3600)))
CAWL_MAGIC_TTL = int(os.environ.get("CAWL_MAGIC_TTL", str(4 * 3600)))
EMAIL_BACKEND = os.environ.get(
    "CAWL_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.environ.get("CAWL_EMAIL_FROM", f"cawl@{CAWL_BASE_DOMAIN}")
# The daemon answers requests for the auth host and (on /.cawl/*) every
# exposure host, all under the base domain.
if ALLOWED_HOSTS and "*" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS += ["." + CAWL_BASE_DOMAIN, CAWL_AUTH_HOST]
CAWL_TAILNET = os.environ.get("CAWL_TAILNET", "ts.net")
CAWL_IMAGE_PREFIX = os.environ.get("CAWL_IMAGE_PREFIX", "cawl")
CAWL_DEFAULT_QUOTA = int(os.environ.get("CAWL_DEFAULT_QUOTA", "0")) or None
