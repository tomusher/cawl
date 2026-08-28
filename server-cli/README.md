# cawl-server

`cawl-server` is the lightweight CLI for bootstrapping and managing a cawl
server (Incus, control plane, etc.)

It bundles the deployment scripts, Compose configuration, and locked Ansible
playbook matching its own version. On first use it materialises them under
`~/.cache/cawl-server/deploy`.

```sh
# Write an editable configuration example in the current directory.
uvx cawl-server init

# Run this on the single host that will run Incus and the control plane.
uvx cawl-server provision --config ./cawl-provision.yml --check

# Reconcile an existing deployment through the control-plane role.
uvx cawl-server update --config ./cawl-provision.yml
```

`--config` is an Ansible variables YAML file based on the bundled
`ansible/group_vars/all.yml.example`. No inventory is needed for the normal
single-host path: `cawl-server` generates a localhost inventory. For remote
or split-host deployment, use `cawl-server init --inventory` and pass the
result with `--inventory`. The CLI and deployment assets are intentionally version-matched;
install a different `cawl-server` version to use a different release.
