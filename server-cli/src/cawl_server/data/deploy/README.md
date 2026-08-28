# Deploying cawl

These are version-matched deployment assets bundled with the `cawl-server`
operator CLI. Use `cawl-server provision` for a first deployment and
`cawl-server update --config ...` to reconcile an existing installation.

The embedded `ansible/` control-plane role configures and starts Docker
Compose after `.env` has been supplied. Its roles are composable: existing
Incus hosts and separate control-plane hosts remain supported. See the cawl
setup and Incus documentation for manual and security-sensitive steps.
