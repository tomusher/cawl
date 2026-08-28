#!/usr/bin/env bash
# Build the cawl golden base image: Ubuntu 24.04 + Docker + compose + git +
# Tailscale + a `dev` user, published as an Incus image that per-site golden
# images (and blank dev boxes) are cloned from.
#
#   ./build-base-image.sh                       # local system container (needs nesting)
#   ./build-base-image.sh --vm                   # local hardware-isolated VM (needs /dev/kvm)
#   REMOTE=cawl: ./build-base-image.sh --vm     # publish through a named remote
#
# Tools are baked directly into the image (via exec, NOT cloud-init) so clones
# don't reinstall anything and services (docker, tailscaled) start cleanly.
set -euo pipefail

REMOTE="${REMOTE:-}"
BUILDER="${REMOTE}cawl-base-builder"

if [[ "${1:-}" == "--vm" ]]; then
  # The VM runtime appends -vm to template image names (for example,
  # cawl/base becomes cawl/base-vm), so publish the matching alias.
  ALIAS="${ALIAS:-cawl/base-vm}"
  ISO=(--vm -c security.secureboot=false)   # hardware isolation (own kernel)
else
  ALIAS="${ALIAS:-cawl/base}"
  ISO=(-c security.nesting=true)            # system container + Docker-in-nesting
fi

wait_for_agent() {
  # A VM is reported as running before the in-guest Incus agent accepts exec
  # requests. Wait for it rather than racing the first provisioning command.
  for _attempt in $(seq 1 60); do
    if incus exec "$BUILDER" -- true >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Incus VM agent did not become ready for $BUILDER" >&2
  incus info "$BUILDER" >&2 || true
  return 1
}

echo ">> launching builder ($([[ ${1:-} == --vm ]] && echo VM || echo container))"
incus delete "$BUILDER" --force 2>/dev/null || true
incus launch images:ubuntu/24.04/cloud "$BUILDER" "${ISO[@]}"
wait_for_agent
incus exec "$BUILDER" -- cloud-init status --wait >/dev/null 2>&1 || true

echo ">> installing docker + compose + git + tailscale + sshd + a dev user"
incus exec "$BUILDER" -- bash -lc '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq docker.io docker-compose-v2 git curl openssh-server
  curl -fsSL https://tailscale.com/install.sh | sh
  # Kernel/tun mode (default) so the tailnet can carry inbound TCP to sshd.
  # /dev/net/tun is already present in nesting containers — no device wiring.
  id -u dev >/dev/null 2>&1 || useradd -m -s /bin/bash -G docker,sudo dev
  echo "dev ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/dev && chmod 440 /etc/sudoers.d/dev
  # sshd is the authenticator: at create time the daemon drops in the CA it
  # trusts (see cawl_core/runtime/sshd.py). Ubuntu ships the Include already, but
  # without it cert auth would silently never engage — so make sure of it.
  mkdir -p /etc/ssh/sshd_config.d
  if ! grep -q "^Include /etc/ssh/sshd_config.d/" /etc/ssh/sshd_config; then
    printf "Include /etc/ssh/sshd_config.d/*.conf\n" \
      | cat - /etc/ssh/sshd_config > /tmp/sshd_config
    mv /tmp/sshd_config /etc/ssh/sshd_config
  fi
  systemctl enable docker tailscaled ssh
'
echo ">> verifying Docker runs inside"
incus exec "$BUILDER" -- bash -lc 'systemctl start docker; docker run --rm hello-world >/dev/null && echo "  docker OK"'

echo ">> resetting identity so clones are unique (no cloud-init re-run)"
incus exec "$BUILDER" -- bash -lc ': > /etc/machine-id; rm -f /var/lib/dbus/machine-id /etc/ssh/ssh_host_*'

echo ">> publishing ${REMOTE}${ALIAS}"
incus stop "$BUILDER"
if [[ -n "$REMOTE" ]]; then
  incus publish "$BUILDER" "$REMOTE" --alias "$ALIAS" --reuse
else
  incus publish "$BUILDER" --alias "$ALIAS" --reuse
fi
incus delete "$BUILDER" --force
echo ">> done: ${REMOTE}${ALIAS}"
