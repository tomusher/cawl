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

The `cawl-server provision` workflow builds the base image when base-image
provisioning is enabled. To manage it separately, use the
version-matched script materialised in `/srv/cawl/build-base-image.sh`.

See [Incus VMs and agent egress](incus.md) for Incus API certificates, VM
networking, and egress controls.

## Choose a setup flow

| Flow | Use it when | What cawl-server changes |
| --- | --- | --- |
| [Fully automated](#fully-automated-single-host) | One new host will run Incus and the control plane. | Installs Incus and Docker, initialises Incus, creates API credentials, builds images, and starts Compose. |
| [Partially automated](#partially-automated-existing-or-separate-incus) | Incus already exists or lives on another host. | Configures only the selected cawl components; existing Incus and credential hand-off stay operator-owned. |
| [Fully manual](#fully-manual) | You need complete control. | Nothing: use the exported version-matched assets and run each operation yourself. |

## Fully automated single host

Install the operator tool with `uv`. It bundles
the version-matched Compose files and provisioning tools. First write editable
configuration examples, then review them and run the preflight:

```bash
uvx cawl-server init --dir ~/cawl-config
$EDITOR ~/cawl-config/cawl-provision.yml
# Run this on the host that will run Incus and the control plane.
uvx cawl-server provision --config ~/cawl-config/cawl-provision.yml --check
uvx cawl-server provision --config ~/cawl-config/cawl-provision.yml
```

The configuration contains DNS credentials: keep it out of source control and
store it with your normal secret-management process. On the default combined host, provisioning
also creates and trusts a dedicated Incus client certificate and pins the Incus
server certificate for the daemon. Provisioning writes `/srv/cawl/.env`,
generates its database and Django secrets, renders Traefik's configuration,
and starts the Compose stack. Existing Incus hosts and separately managed
control-plane hosts are supported by the inventory and configuration options;
the Incus credential hand-off remains manual for separate hosts.

To update a running control plane, rerun its declarative configuration:

```bash
uvx cawl-server update --config ~/cawl-config/cawl-provision.yml
```

## Partially automated: existing or separate Incus

Create an inventory when the Incus and control-plane hosts differ, then set
these values in `cawl-provision.yml`:

```bash
uvx cawl-server init --dir ~/cawl-config --inventory
$EDITOR ~/cawl-config/inventory.yml ~/cawl-config/cawl-provision.yml
```

```yaml
cawl_provision_incus: false
# Build it manually on the Incus host, or enable only after the script is
# available there.
cawl_provision_base_image: false
cawl_configure_incus_trust: false

cawl_env:
  CAWL_INCUS_URL: https://incus.internal.example:8443
  CAWL_INCUS_CLIENT_CERT: /run/secrets/incus/client.crt
  CAWL_INCUS_CLIENT_KEY: /run/secrets/incus/client.key
  CAWL_INCUS_SERVER_CERT: /run/secrets/incus/server.crt
```

Run `cawl-server provision` with both `--inventory` and `--config`. It leaves
Incus untouched. Create the daemon credentials on the Incus host and transfer
them securely to the control-plane host:

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:secp384r1 -nodes \
  -keyout client.key -out client.crt -days 3650 -subj "/CN=cawl-server"
sudo incus config trust add-certificate client.crt --name cawl-server
scp client.crt client.key control-plane:/srv/cawl/secrets/incus/
scp /var/lib/incus/server.crt control-plane:/srv/cawl/secrets/incus/server.crt
```

Keep `client.key` mode `0600`. The server uses Incus's HTTPS API with this
dedicated client certificate and pinned server certificate.

## Fully manual

Export the assets matching the installed operator CLI into an empty directory:

```bash
uvx cawl-server assets --dir /srv/cawl
cd /srv/cawl
cp .env.example .env
$EDITOR .env
```

Install and initialise Incus and Docker Compose yourself, create the Incus
credentials above, and place them in `secrets/incus/`. Render
`traefik/traefik.yml` from `traefik/traefik.yml.template` by replacing the four
`{{ cawl_env.* }}` values with your `.env` values. Then perform the same
control-plane operations explicitly:

```bash
docker compose up -d postgres
docker compose exec -T postgres pg_isready -U cawl -d cawl
docker compose run --rm cawl python manage.py migrate --noinput
docker compose up -d
docker compose exec -T cawl python manage.py sync_ingress
```

Use `build-base-image.sh --vm` on the Incus host to create the base image.
The exported configuration and commands above are the supported manual
reference for required directories, ownership, and service steps.

## Incus API credentials

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
