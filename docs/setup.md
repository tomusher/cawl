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

Grab the latest deployment files from [GitHub](https://github.com/tomusher/cawl/releases):

```bash
mkdir -p /srv/cawl
cd  /srv/cawl
curl -L https://github.com/tomusher/cawl/releases/download/v0.1.0/cawl-server-deploy-v0.1.0.tar.gz | tar -xzf --strip-components=1
```

Then make a copy of the `.env.example` file and edit it to your liking.

```bash
cp .env.example .env
```

Edit `.env` before starting, you'll need to set these things:

- `CAWL_API_DOMAIN`: control-plane API hostname (e.g. `cawl.example.com`)
- `CAWL_PUBLIC_DOMAIN`: public sandbox namespace (e.g. `public.example.com`)
- `CAWL_INCUS_URL`: the Incus REST API URL (e.g. `https://incus.example.com` or `host.docker.internal:8443`)
- `POSTGRES_PASSWORD`: a nice random password
- `CAWL_SECRET_KEY`: a random secret key

Point the apex of `CAWL_API_DOMAIN` at the API network endpoint. Point both the apex and wildcard of `CAWL_PUBLIC_DOMAIN` at the public Traefik endpoint.

The daemon is served at `https://$CAWL_API_DOMAIN` through Traefik. Set `CAWL_API_URL` to that URL for the CLI. 

Before the first start or an update, run the bootstrap script. It starts Postgres, applies migrations, seeds Traefik's static configuration volume, and starts the stack:

```bash
./bootstrap.sh
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
*.sbx.example.com  →  the Traefik host public IP
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
