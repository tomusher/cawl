"""Operator commands backed by deployment assets bundled in this package."""
from __future__ import annotations

import argparse
import importlib.metadata
from importlib import resources
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys


def installed_version() -> str:
    try:
        return importlib.metadata.version("cawl-server")
    except importlib.metadata.PackageNotFoundError:  # useful from a checkout
        return "0.1.0"


def copy_resource_tree(source: resources.abc.Traversable, destination: Path) -> None:
    """Copy package data without assuming the distribution is an unpacked wheel."""
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            copy_resource_tree(child, target)
        else:
            target.write_bytes(child.read_bytes())


def deployment(cache_dir: str) -> Path:
    """Materialise this CLI's version-matched deployment assets once."""
    destination = Path(cache_dir).expanduser() / installed_version()
    marker = destination / ".cawl-bundle"
    if marker.exists():
        return destination
    staging = destination.with_name(destination.name + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    copy_resource_tree(resources.files("cawl_server").joinpath("data", "deploy"), staging)
    # Deployment artifacts are normally extracted from a tarball. Restore the
    # executable bits when they are materialised from Python package data.
    for script in (staging / "provision", staging / "bootstrap.sh", staging / "build-base-image.sh"):
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    compose = staging / "compose.yaml"
    compose.write_text(compose.read_text().replace("ghcr.io/tomusher/cawl-server:main", f"ghcr.io/tomusher/cawl-server:{installed_version()}"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(destination, ignore_errors=True)
    staging.rename(destination)
    marker.touch()
    return destination


LOCAL_INVENTORY = """all:
  children:
    incus:
      hosts:
        localhost:
          ansible_connection: local
    control_plane:
      hosts:
        localhost:
          ansible_connection: local
"""


def initialise(args: argparse.Namespace) -> int:
    """Write editable examples without overwriting operator configuration."""
    source = deployment(args.cache_dir) / "ansible"
    destination = Path(args.dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    examples = {"cawl-provision.yml": source / "group_vars" / "all.yml.example"}
    if args.inventory:
        examples["inventory.yml"] = source / "inventory.example.yml"
    for name, template in examples.items():
        target = destination / name
        if target.exists():
            raise RuntimeError(f"refusing to overwrite {target}")
        shutil.copy2(template, target)
    return 0


def provision(args: argparse.Namespace) -> int:
    deploy = deployment(args.cache_dir)
    config = Path(args.config).expanduser().resolve()
    if not config.is_file():
        raise RuntimeError("--config must name an existing file")
    if args.inventory:
        inventory = Path(args.inventory).expanduser().resolve()
        if not inventory.is_file():
            raise RuntimeError("--inventory must name an existing file")
    else:
        inventory = deploy / "ansible" / "localhost.inventory.yml"
        inventory.write_text(LOCAL_INVENTORY)
    environment = os.environ | {
        "CAWL_PROVISION_INVENTORY": str(inventory),
        "CAWL_PROVISION_CONFIG": str(config),
    }
    return subprocess.run([str(deploy / "provision"), *args.ansible_args], env=environment).returncode


def update(args: argparse.Namespace) -> int:
    source = deployment(args.cache_dir)
    target = Path(args.dir).expanduser().resolve()
    if not (target / ".env").is_file():
        raise RuntimeError(f"{target} is not an existing cawl deployment (.env is missing)")
    for item in source.iterdir():
        if item.name in {".env", "secrets", ".cawl-bundle", "ansible", "provision", "README.md"}:
            continue
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)
    return subprocess.run(["sh", "bootstrap.sh"], cwd=target).returncode


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="cawl-server")
    result.add_argument("--version", action="version", version=installed_version())
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cache-dir", default="~/.cache/cawl-server/deploy")
    commands = result.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init", parents=[common])
    init_parser.add_argument("--dir", default=".", help="Directory for editable example files")
    init_parser.add_argument("--inventory", action="store_true", help="Also write an inventory example for remote hosts")
    init_parser.set_defaults(func=initialise)
    provision_parser = commands.add_parser("provision", parents=[common])
    provision_parser.add_argument("--inventory", help="Inventory for remote or split-host deployment")
    provision_parser.add_argument("--config", required=True, help="Ansible variables YAML file")
    provision_parser.add_argument("ansible_args", nargs=argparse.REMAINDER)
    provision_parser.set_defaults(func=provision)
    update_parser = commands.add_parser("update", parents=[common])
    update_parser.add_argument("--dir", default="/srv/cawl")
    update_parser.set_defaults(func=update)
    return result


def main() -> None:
    try:
        args, unknown = parser().parse_known_args()
        if args.command == "provision":
            args.ansible_args.extend(unknown)
        elif unknown:
            parser().error(f"unrecognized arguments: {' '.join(unknown)}")
        raise SystemExit(args.func(args))
    except RuntimeError as error:
        raise SystemExit(f"cawl-server: {error}")


if __name__ == "__main__":
    main()
