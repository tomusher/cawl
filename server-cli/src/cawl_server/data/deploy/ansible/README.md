# cawl provisioning playbook

This is optional operator automation, not part of the `cawl` developer CLI.
It is deliberately composable: it can provision a new single host, configure
only the Compose control plane, or verify a manually managed deployment.

## Quick start

Install [uv](https://docs.astral.sh/uv/) on the machine from which you
administer the hosts, then create an inventory and configuration outside source
control (the configuration contains DNS credentials). `cawl-server` materialises
this playbook and runs its pinned Ansible dependency automatically; no system
Ansible or cawl checkout is needed.

```sh
cawl-server init --dir ~/cawl-config
$EDITOR ~/cawl-config/cawl-provision.yml
# Run on the combined Incus/control-plane host.
cawl-server provision --config ~/cawl-config/cawl-provision.yml --check
```

Use Ansible Vault for `cawl_env` in a real deployment. By default the
playbook installs Incus from the official stable repository, verifies it is at
least the configured supported version, and runs `incus admin init --auto`. Supply a reviewed
`cawl_incus_preseed` when you need a particular storage or network layout, or
set `cawl_incus_mode: existing` to leave Incus untouched.

## Run only one component

Roles have matching tags:

```sh
cawl-server provision --inventory inventory.yml --config cawl-provision.yml --tags control-plane
cawl-server provision --inventory inventory.yml --config cawl-provision.yml --tags verify
cawl-server provision --inventory inventory.yml --config cawl-provision.yml --tags incus,base-image
```

Alternatively disable roles with the `cawl_provision_*` variables. `--check`
is useful for the package, directory, and configuration changes, but Incus and
Docker's own commands cannot provide a complete dry run.

## Existing or separate Incus

For this split-host case, create an inventory with
`cawl-server init --inventory` and put the Incus machine in the `incus` group
and the Compose machine in the `control_plane` group. Set `cawl_provision_incus: false` when Incus is already
managed. Configure `CAWL_INCUS_URL` and install the API client certificate,
key, and server certificate in `secrets/incus/` on the control-plane host as
described in [`docs/incus.md`](../../../docs/incus.md). This certificate step
is intentionally manual for now: it creates an administrator-level trust
relationship and needs a deliberate hand-off between hosts.

Likewise, egress ACLs, firewall rules, DNS delegation, and clustering remain
operator-owned. The setup and Incus guides cover these manual,
security-sensitive steps.

## Idempotency and updates

The control-plane role preserves existing generated database and Django
secrets; it overwrites only values supplied in `cawl_env`. Upgrade
`cawl-server` and run `cawl-server update` to apply version-matched deployment
files. Back up the Compose volumes before changing or replacing a production
deployment.
