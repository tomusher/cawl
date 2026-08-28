# Setting It Up

cawl's supported control-plane deployment is Docker Compose. Docker runs the
Django daemon, Postgres, the expiry reaper, and Traefik; Incus runs on the same
host or on a host reachable from Docker.

## Prerequisites

You need a host with KVM support for Incus, a public IP and domain with
DNS-provider API credentials for wildcard TLS, and an SSH transport for
developers (Tailscale, a jump host, or routing to the Incus bridge).

By default, `cawl-server provision` installs Docker Compose and Incus, then
initialises Incus with `incus admin init --auto`. Set
`cawl_incus_mode: existing` in its configuration when Incus is already managed
or lives on a separate host. Provide `cawl_incus_preseed` when you need a
specific storage or network layout.

The `cawl-server provision` workflow builds the base image when its
`base-image` role is enabled. To manage it separately, use the
version-matched script materialised in `/srv/cawl/build-base-image.sh`.

See [Incus VMs and agent egress](incus.md) for Incus API certificates, VM
networking, and egress controls.

## Deploy the control plane

Install the operator tool with `uv` (no cawl checkout or system Ansible is
needed). It bundles the version-matched Compose files, scripts, and Ansible
roles. First write editable configuration examples, then review them and run
the preflight:

```bash
uvx cawl-server init --dir ~/cawl-config
$EDITOR ~/cawl-config/cawl-provision.yml
# Run this on the host that will run Incus and the control plane.
uvx cawl-server provision --config ~/cawl-config/cawl-provision.yml --check
uvx cawl-server provision --config ~/cawl-config/cawl-provision.yml
```

The configuration contains DNS credentials: keep it out of source control and
use Ansible Vault in production. The control-plane role writes `/srv/cawl/.env`,
generates its database and Django secrets, renders Traefik's configuration,
and starts the Compose stack. Existing Incus hosts and separately managed
control-plane hosts are supported by the inventory and configuration options.

To update a running control plane, rerun the same declarative control-plane
role with the configuration used for its deployment:

```bash
uvx cawl-server update --config ~/cawl-config/cawl-provision.yml
```

## Let the cawl server use the Incus API

The server uses Incus's HTTPS API with its own client certificate. On the
Incus host, create the certificate and trust it:

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:secp384r1 -nodes \
  -keyout client.key -out client.crt -days 3650 -subj "/CN=cawl-server"
sudo incus config trust add-certificate client.crt --name cawl-server

cp /var/lib/incus/server.crt server.crt
```

Copy `client.crt`, `client.key`, and `/var/lib/incus/server.crt` to `/srv/cawl/secrets`:

```bash
cp client.crt client.key /srv/cawl/secrets/
cp /var/lib/incus/server.crt /srv/cawl/secrets/
```

## SSH access

Set `CAWL_ACCESS` in `.env` to `tailscale`, `jump`, or `bridge`. Leaving it
unset infers Tailscale from an auth key, then jump from a jump host, and
otherwise uses bridge routing. This is transport only: each box accepts only a
certificate signed by cawl's SSH CA.

For Tailscale, set an ephemeral reusable `CAWL_TAILSCALE_AUTHKEY`,
`CAWL_TAILNET`, and optionally `CAWL_TAILSCALE_TAGS=tag:cawl`. Do not enable
Tailscale SSH; the tailnet only carries TCP/22 to the box's sshd.

For a jump host, set `CAWL_ACCESS=jump` and
`CAWL_SSH_JUMP=cawl-host.example.com`. For direct Incus-bridge routing, set
`CAWL_ACCESS=bridge`.

## DNS and Traefik

A wildcard record is enough for every exposure:

```
*.cawl.example.com  →  the Traefik host public IP
```

The Compose Traefik service obtains and renews the wildcard certificate with
the DNS challenge provider configured in `.env`. Tailscale Funnel cannot serve
this wildcard domain; use a public IP or suitable tunnel.

## Hardening the control-plane API with Tailscale

The default deployment makes the API available at `CAWL_API_DOMAIN` through
Traefik. Sandbox exposures still need public HTTPS, but the control-plane API
can be limited to tailnet source addresses without changing how Traefik
publishes it. Set this in `.env` and restart the stack:

```dotenv
# Comma-separated CIDRs; these are Tailscale's default IPv4 and IPv6 ranges.
CAWL_API_PRIVATE_SOURCE_RANGES=100.64.0.0/10,fd7a:115c:a1e0::/48
```

When `CAWL_API_PRIVATE_SOURCE_RANGES` is set, Traefik applies it as an IP
allow-list to the API router. Leave it unset for a public API. Replace the
example with the CIDRs for another private network. Public requests to the API
are rejected, while sandbox exposure routers remain public.

Ensure tailnet clients reach the Traefik host through its Tailscale address.
For example, use split DNS or your existing publishing configuration so
`CAWL_API_DOMAIN` resolves to that address for tailnet clients. The CLI URL
does not change:

```bash
export CAWL_API_URL=https://cawl.example.com
```

This restricts network reachability only; API tokens and the daemon's normal
authorization checks still apply.

## People, tokens, and templates

```bash
cd deploy
docker compose exec cawl python manage.py createsuperuser
docker compose exec cawl python manage.py mint_token ci-bot --quota 5 --ttl 90d --max-ttl 4h
```

Humans use `cawl login`. Register templates and rebuild template images through
the CLI:

```bash
cawl template create < template.yaml
cawl refresh-image acme-cms
```

See [Templates](templates.md) for template authoring.
