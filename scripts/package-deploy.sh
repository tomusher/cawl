#!/usr/bin/env sh
# Build a clean, version-pinned deployment bundle from the committed tree.
set -eu

version=${1:?usage: package-deploy.sh VERSION GITHUB_OWNER [OUTPUT_DIR]}
owner=${2:?usage: package-deploy.sh VERSION GITHUB_OWNER [OUTPUT_DIR]}
out_dir=${3:-.}
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
bundle="cawl-server-deploy-${version}"
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT HUP INT TERM

mkdir -p "$out_dir"
# git archive intentionally excludes local .env files and Incus/SSH credentials.
git -C "$root" archive --format=tar --prefix="${bundle}/" HEAD:server/deploy \
  | tar -x -C "$staging"

sed -i "s|ghcr.io/tomusher/cawl-server:main|ghcr.io/${owner}/cawl-server:${version}|g" \
  "$staging/${bundle}/compose.yaml"

tar -C "$staging" -czf "$out_dir/${bundle}.tar.gz" "$bundle"
(
  cd "$out_dir"
  sha256sum "${bundle}.tar.gz" > "${bundle}.tar.gz.sha256"
)
