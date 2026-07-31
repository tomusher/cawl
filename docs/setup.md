# Setting It Up

cawl's supported control-plane deployment is Docker Compose. Docker runs the
Django daemon, Postgres, the expiry reaper, and Traefik; Incus runs on the same
host or on a host reachable from Docker.

## Prerequisites

You need an Incus host, Docker Compose on the control-plane host, a public IP
and domain with DNS-provider API credentials for wildcard TLS, and an SSH
transport for developers (Tailscale, a jump host, or routing to the Incus
bridge).

Install and initialise Incus using the [Incus installation documentation](https://linuxcontainers.org/incus/docs/main/installing/):

```bash
apt install incus qemu-system
incus admin init
```

Build the base image from the cawl checkout:

```bash
cd server/deploy
./build-base-image.sh --vm
```

See [Incus VMs and agent egress](incus.md) for Incus API certificates, VM
networking, and egress controls.

## Deploy the control plane

Copy the whole `server/deploy/` directory to the control-plane host. It is a
self-contained Compose deployment: `compose.yaml`, `.env.example`, Traefik
configuration, and an empty `secrets/` directory are all included.

```bash
cd deploy
cp .env.example .env
mkdir -p secrets/incus
# Copy client.crt, client.key, and server.crt to secrets/incus/.
# Generate secrets/ssh_ca if developers will use `cawl ssh`.
docker compose up -d
```

Edit `.env` before starting. It is the canonical deployment configuration.
Compose first migrates the database, then starts the daemon and reaper. The API
is published on `${CAWL_API_PORT:-8000}`; Traefik owns ports 80 and 443.

Set `CAWL_BASE_DOMAIN`, then point both its apex and wildcard DNS records to
the Traefik host. The Traefik container must be able to route to instance IPs,
not merely the Incus REST API. `host.docker.internal` resolves to Docker's host
gateway; use another routable address in `CAWL_INCUS_URL` when Incus is remote.

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
*.sbx.example.com  →  the Traefik host public IP
```

The Compose Traefik service obtains and renews the wildcard certificate with
the DNS challenge provider configured in `.env`. Tailscale Funnel cannot serve
this wildcard domain; use a public IP or suitable tunnel.

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
