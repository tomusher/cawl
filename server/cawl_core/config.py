"""Parsing and validation of a ``template.yaml``.

A template says three things: which golden image to clone, which arguments it
accepts (``params``), and what to run (``hooks``). The daemon knows nothing
about what any of it means — a template that wants a git checkout writes the
checkout into its own provision hook. Everything else here (TTLs,
default exposures) is cawl's own bookkeeping, not the app's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from cawl_core.naming import parse_ttl, validate_expose_key
from cawl_core.params import BUILTINS, Param, placeholders


class ConfigError(ValueError):
    """Raised for a malformed or incomplete template.yaml."""


@dataclass
class Hooks:
    """Shell run inside the box. `build` runs in the builder when the golden
    image is baked; `provision` runs in each new env once Docker is up."""

    build: str = ""
    provision: str = ""


@dataclass
class TemplateConfig:
    name: str
    image: str = ""  # golden image base alias; default <prefix>/<name>
    params: dict[str, Param] = field(default_factory=dict)
    hooks: Hooks = field(default_factory=Hooks)
    # Default exposures (key -> port), materialized as Exposure rows at `up` so
    # an env comes up with its URLs already live: the `web` key at <id>.<domain>,
    # any other at <key>--<id>.<domain>. Sugar over `cawl expose`, not a second
    # mechanism — `expose`/`unexpose` edit the same rows afterwards.
    expose: dict[str, int] = field(default_factory=dict)
    # The default lifetime `cawl up` starts from — overridable per env
    # (--ttl) and, for machine tokens, capped at the token (max_ttl). Which
    # backend materializes an env is deployment vocabulary, so templates
    # deliberately have no say in it.
    ttl: str | None = None


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise ConfigError(f"missing required key {where}.{key}")
    return d[key]


def _parse_params(raw: dict) -> dict[str, Param]:
    params: dict[str, Param] = {}
    for name, spec in (raw or {}).items():
        if name in BUILTINS:
            raise ConfigError(
                f"params.{name} shadows a built-in ({', '.join(BUILTINS)})")
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ConfigError(f"params.{name} must be a mapping")
        p = Param(
            name=str(name),
            default=None if spec.get("default") is None else str(spec["default"]),
            required=bool(spec.get("required", False)),
            choices=[str(c) for c in (spec.get("choices") or [])],
            pattern=str(spec.get("pattern", "")),
            description=str(spec.get("description", "")),
        )
        if p.required and p.default is not None:
            raise ConfigError(f"params.{name}: a required param can't also have a default")
        try:
            if p.default is not None:
                p.check(p.default)
        except Exception as e:  # ParamError
            raise ConfigError(f"params.{name}: invalid default: {e}") from e
        params[str(name)] = p
    return params


def _check_placeholders(text: str, params: dict[str, Param], where: str) -> None:
    """A template that references an arg it never declared is a broken template —
    catch it on upload, not on the first `cawl up` that trips over it."""
    known = set(params) | set(BUILTINS)
    for name in sorted(placeholders(text) - known):
        raise ConfigError(
            f"{where} references {{{{{name}}}}}, which is not a declared param "
            f"(declared: {', '.join(sorted(params)) or 'none'})")


def parse_template_config(data: dict) -> TemplateConfig:
    if not isinstance(data, dict):
        raise ConfigError("template.yaml must be a mapping")

    name = _require(data, "name", "")
    params = _parse_params(data.get("params", {}) or {})

    hraw = data.get("hooks", {}) or {}
    if not isinstance(hraw, dict):
        raise ConfigError("hooks must be a mapping")
    if unknown := set(hraw) - {"build", "provision"}:
        raise ConfigError(f"hooks has unknown key(s): {', '.join(sorted(unknown))}")
    hooks = Hooks(build=str(hraw.get("build", "") or ""),
                  provision=str(hraw.get("provision", "") or ""))
    _check_placeholders(hooks.build, params, "hooks.build")
    _check_placeholders(hooks.provision, params, "hooks.provision")

    expose: dict[str, int] = {}  # optional: absent => SSH/tailnet-only, no URLs
    eraw = data.get("expose", {}) or {}
    if not isinstance(eraw, dict):
        raise ConfigError("expose must be a mapping of name: port")
    for key, port in eraw.items():
        try:
            key = validate_expose_key(str(key))
        except ValueError as e:
            raise ConfigError(f"expose: {e}") from e
        if not isinstance(port, int) or not (0 < port < 65536):
            raise ConfigError(f"expose.{key} must be a port number, got {port!r}")
        expose[key] = port

    defaults = data.get("defaults", {}) or {}
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be a mapping")
    if unknown := set(defaults) - {"ttl"}:
        raise ConfigError(f"defaults has unknown key(s): {', '.join(sorted(unknown))}")
    ttl = defaults.get("ttl")
    ttl = None if ttl is None else str(ttl)
    try:
        parse_ttl(ttl)              # catch a bad spec at upload, not at `up`
    except ValueError as e:
        raise ConfigError(f"defaults.ttl: {e}") from e

    return TemplateConfig(
        name=name,
        image=data.get("image", ""),
        params=params,
        hooks=hooks,
        expose=expose,
        ttl=ttl,
    )


def load_template_config_text(text: str) -> TemplateConfig:
    """Parse + validate a template.yaml given as a string (e.g. a DB-stored
    template body). Raises ConfigError on malformed YAML or config."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}") from e
    return parse_template_config(data)


def load_template_config(path: str | Path) -> TemplateConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"no template.yaml at {path}")
    return load_template_config_text(path.read_text())
