"""Template parameters: what a template accepts, and how its hooks see them.

A template declares the arguments it takes (``params:``) and what to do with
them (``hooks:``). cawl attaches no meaning to any of them: it validates values
against the declaration and hands them to the template's own shell hooks.
Anything app-specific — cloning a repo, checking out a branch, bringing a
compose stack up — lives in the hook, not in the daemon.

Args are *not* secrets. They are stored on the environment row, shown in `cawl ls`,
and visible in the admin.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field

from cawl_core.errors import CawlError

# Param names double as shell variable names in a hook's environment.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
# Values are shell-quoted before they reach a hook, so this guards the places
# they are *not* quoted: DNS labels, `cawl ls`, the DB.
_VALUE_RE = re.compile(r"^[\x20-\x7e]{0,200}$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,30})\s*\}\}")

# Supplied by the daemon, always available to a hook alongside the args.
BUILTINS = ("id", "template")

MAX_ARGS = 20


class ParamError(CawlError):
    """An arg the template didn't declare, or a value it won't accept."""


@dataclass
class Param:
    name: str
    default: str | None = None
    required: bool = False
    choices: list[str] = field(default_factory=list)
    pattern: str = ""
    description: str = ""

    def check(self, value: str) -> str:
        if not _VALUE_RE.match(value):
            raise ParamError(
                f"{self.name}: must be printable ASCII, at most 200 characters")
        if self.choices and value not in self.choices:
            raise ParamError(f"{self.name}: must be one of {', '.join(self.choices)}")
        if self.pattern and not re.match(self.pattern, value):
            raise ParamError(f"{self.name}: does not match {self.pattern!r}")
        return value


def resolve(params: dict[str, Param], supplied: dict[str, str]) -> dict[str, str]:
    """Validate supplied args against a template's declaration; fill defaults in.

    The result is the env's *resolved* args — the full set its hooks will see,
    and what the reuse key is computed from. So `--arg branch=main` and an
    omitted `branch` that defaults to `main` describe the same environment.
    """
    if len(supplied) > MAX_ARGS:
        raise ParamError(f"too many args (max {MAX_ARGS})")

    for name in supplied:
        if not _NAME_RE.match(name):
            raise ParamError(
                f"invalid arg name {name!r}: use a-z, 0-9 and '_', starting with a letter")
        if name not in params:
            known = ", ".join(sorted(params)) or "none"
            raise ParamError(f"unknown arg {name!r} (this template declares: {known})")

    resolved: dict[str, str] = {}
    for name, p in params.items():
        if name in supplied:
            resolved[name] = p.check(str(supplied[name]))
        elif p.default is not None:
            resolved[name] = p.check(str(p.default))
        elif p.required:
            raise ParamError(f"missing required arg {name!r}")
    return resolved


def args_hash(template: str, args: dict[str, str]) -> str:
    """Stable digest of a template's resolved args — the rest of the reuse key.

    `--reuse-if-exists` hands back an env only if it was created from the same
    args; without this, two differently-parameterized envs would look identical
    to the (template, owner) key and reuse each other.
    """
    blob = json.dumps({"template": template, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def placeholders(text: str) -> set[str]:
    return {m.group(1) for m in _PLACEHOLDER_RE.finditer(text or "")}


def expand(text: str, values: dict[str, str]) -> str:
    """Substitute `{{name}}` with the raw value. For non-shell text (hostnames)."""
    def sub(m):
        name = m.group(1)
        if name not in values:
            raise ParamError(f"undeclared placeholder {{{{{name}}}}}")
        return values[name]

    return _PLACEHOLDER_RE.sub(sub, text or "")


def render(script: str, values: dict[str, str]) -> str:
    """Render a shell hook: `{{name}}` becomes a shell-quoted literal, and every
    value is also exported, so `$name` works too.

    Substituting the *quoted* value is what stops an arg from breaking out of the
    script it lands in: `--arg branch='x; rm -rf /'` becomes one inert word.
    """
    if not (script or "").strip():
        return ""

    def sub(m):
        name = m.group(1)
        if name not in values:
            raise ParamError(f"undeclared placeholder {{{{{name}}}}}")
        return shlex.quote(values[name])

    body = _PLACEHOLDER_RE.sub(sub, script)
    exports = "".join(f"{k}={shlex.quote(v)}; export {k}\n"
                      for k, v in sorted(values.items()))
    return exports + body
