"""Shared exception types (kept here to avoid import cycles)."""

from __future__ import annotations


class CawlError(Exception):
    """Base for all cawl control-plane errors."""


class NotFound(CawlError):
    pass


class QuotaExceeded(CawlError):
    pass


class PermissionDenied(CawlError):
    pass


class InvalidName(CawlError):
    """A custom `--name` that isn't a legal instance id."""


class NameConflict(CawlError):
    """A custom `--name` already held by a live environment."""
