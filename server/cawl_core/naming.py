"""Deterministic naming: environment ids, hostname labels, TTL parsing."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

_TTL_RE = re.compile(r"^(\d+)([smhdw])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# A name doubles as the Incus instance name and a DNS label: 2-63 chars, starts
# with a letter (an all-numeric label is not a legal hostname), ends alphanumeric.
# "--" is rejected in environment names so that the default exposure labels they
# generate (<key>--<id>) read unambiguously.
_NAME_RE = re.compile(r"^(?!.*--)[a-z][a-z0-9-]{0,61}[a-z0-9]$")

# A template's expose: keys are label fragments, kept short so the default
# label <key>--<id> stays a legal 63-char DNS label.
_EXPOSE_KEY_RE = re.compile(r"^(?!.*--)[a-z][a-z0-9-]{0,18}[a-z0-9]$|^[a-z]$")

# An exposure label is a full hostname label, freely chosen (`--` allowed, so
# the default <key>--<id> form is itself a valid label). "xn--" is refused —
# that prefix would make it an IDN punycode label.
_LABEL_RE = re.compile(r"^(?!xn--)[a-z][a-z0-9-]{0,61}[a-z0-9]$")


def sanitize(s: str, maxlen: int = 40) -> str:
    """Lowercase DNS-label-safe slug (a-z0-9-), no leading/trailing dash."""
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:maxlen].strip("-") or "x"


def new_environment_id(template: str) -> str:
    """A DNS-safe handle with a 128-bit random suffix."""
    return f"{sanitize(template, 30)}-{secrets.token_hex(16)}"


def validate_name(name: str) -> str:
    """Normalize a user-supplied env name (``cawl up --name``) into an id.

    Only case and surrounding space are forgiven; anything else invalid is
    rejected rather than quietly slugified, because the name is the handle the
    caller types back at ``exec``/``rm`` — silently turning ``my env`` into
    ``my-env`` would hand back an id they didn't ask for.
    """
    n = str(name).strip().lower()
    if not _NAME_RE.match(n):
        raise ValueError(
            f"invalid name {name!r}: use 2-63 characters of a-z, 0-9 and '-', "
            "starting with a letter and ending with a letter or digit"
        )
    return n


def validate_expose_key(key: str) -> str:
    """Normalize a template ``expose:`` key. Same contract as ``validate_name``:
    forgive case and space, reject anything else."""
    k = str(key).strip().lower()
    if not _EXPOSE_KEY_RE.match(k):
        raise ValueError(
            f"invalid expose key {key!r}: use 1-20 characters of a-z, 0-9 "
            "and '-' (no '--'), starting with a letter and ending with a letter "
            "or digit"
        )
    return k


def validate_exposure_label(name: str) -> str:
    """Normalize an exposure's hostname label (``cawl expose --name``). Labels
    are freely chosen and globally unique — the daemon resolves a request's
    hostname to an exposure by looking the label up, not by parsing it."""
    n = str(name).strip().lower()
    if not _LABEL_RE.match(n):
        raise ValueError(
            f"invalid exposure name {name!r}: use 2-63 characters of a-z, 0-9 "
            "and '-', starting with a letter and ending with a letter or digit"
        )
    return n


def default_exposure_label(environment_id: str, key: str) -> str:
    """The label a template ``expose:`` key materializes as: ``web`` (the
    primary) gets the bare environment id, anything else ``<key>--<id>`` — scoped
    to the env, so template defaults can never collide across envs."""
    from cawl_core.models import PRIMARY_EXPOSURE  # avoid import cycle at module load

    return environment_id if key == PRIMARY_EXPOSURE else f"{key}--{environment_id}"


def exposure_host(label: str, base_domain: str) -> str:
    """Every exposure is one label under the base domain, so a single wildcard
    DNS record and certificate cover all of them."""
    return f"{label}.{base_domain}"


def exposure_label(host: str, base_domain: str) -> str | None:
    """The label of ``host`` if it's exactly one label under ``base_domain``,
    else None. Which exposure (if any) owns the label is a DB lookup."""
    host = host.strip().lower().rstrip(".").split(":")[0]
    suffix = "." + base_domain.strip().lower()
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if not label or "." in label:
        return None
    return label


def parse_ttl(spec: str | None) -> timedelta | None:
    """``"7d"`` -> timedelta; ``"none"``/``None``/``""`` -> None (no expiry)."""
    if spec is None:
        return None
    spec = str(spec).strip().lower()
    if spec in ("", "none", "never", "0"):
        return None
    m = _TTL_RE.match(spec)
    if not m:
        raise ValueError(f"invalid ttl {spec!r} (expected e.g. 30m, 4h, 7d)")
    return timedelta(seconds=int(m.group(1)) * _UNIT[m.group(2)])


def compute_expiry(now: datetime, ttl: timedelta | None) -> datetime | None:
    return now + ttl if ttl else None
