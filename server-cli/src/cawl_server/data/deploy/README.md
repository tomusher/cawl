# Deploying cawl

These are version-matched deployment assets bundled with the `cawl-server`
operator CLI. Use `cawl-server provision` for a first deployment and
`cawl-server update` to update an existing `/srv/cawl` installation.

`bootstrap.sh` configures and starts the Docker Compose control plane after
`.env` has been supplied. The embedded `ansible/` roles are composable: existing
Incus hosts and separate control-plane hosts remain supported. See the cawl
setup and Incus documentation for manual and security-sensitive steps.
