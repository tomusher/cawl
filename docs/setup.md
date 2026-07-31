# Setting It Up

To get cawl working, we need to do a bit of work up front.

This involves:

- Setting up an Incus server
- Running the cawl server daemon (and related components)

When all that's done, all your users need to is install the CLI and start using it.

This documentation covers two common deployment patterns:

- All on one host - deploying everything to a server with hardware virtualization enabled
- cawl server in one place, Incus on another

# All on One Host

Before you start, you'll want:

- a Linux host with a public IP, plenty of RAM, and hardware virtualization enabled.
- a domain
- a way for developers to reach the boxes over SSH: a Tailscale network, a
  jump host they can already SSH to, or your own routing to the Incus bridge.
  Pick one in [SSH access](#ssh-access) below.

These docs assumes a host running Ubuntu 26.04 with a user running as root. Incus packaging varies by distribution; use the [Incus installation documentation](https://linuxcontainers.org/incus/docs/main/installing/) when your platform differs.

## Installing Incus

Install Incus and run its interactive initialiser:

```bash
apt install incus qemu-system
incus admin init
```

The initialiser asks questions about your storage pool and network bridge. The defaults here are all sensible if you are setting up from scratch.

Check it's all working:

```bash
incus launch images:ubuntu/24.04/cloud incus-check --vm
incus list
incus exec incus-check -- echo "Hello, Incus!" # Might need to wait a bit for the VM to start
incus delete incus-check --force
```

## Set up the cawl server

Create a directory for cawl and related files:

```bash
mkdir -p /srv/cawl
```

Copy the example [environment file](server/deploy/daemon.env.example) and [compose file](server/deploy/docker-compose.yml), then edit them to your liking:


Install Incus with a ZFS pool and its default bridge, then bake the image
every environment starts from:

```bash
server/deploy/build-base-image.sh
```

That produces `cawl/base`: Ubuntu with Docker, git, and a `dev` user ready
for SSH. App-specific images come later, from templates, via
`cawl refresh-image <template>`.

The daemon talks to Incus over its API with a client certificate; minting
and trusting one is a couple of `openssl` and `incus` commands, spelled out
in the deploy README. For Incus VMs, controlled agent egress, and adding
cluster members when needed, follow [Incus VMs and agent egress](incus.md).

## The daemon

The daemon is a Django app; give it a database and a superuser:

```bash
cd server
uv sync
uv run manage.py migrate
uv run manage.py createsuperuser
```

Configuration is one env file. Copy `server/deploy/daemon.env.example` to
`/etc/cawl/daemon.env` and work through it; it's commented. The settings
that matter most:

| Setting | What it is |
| --- | --- |
| `CAWL_SECRET_KEY` | a long random string; Django signs sessions and sign-in links with it |
| `CAWL_DATABASE_URL` | Postgres connection string (SQLite is fine for a trial) |
| `CAWL_INCUS_URL` + certs | how the daemon reaches Incus |
| `CAWL_BASE_DOMAIN` | the domain environments live under, e.g. `sbx.example.com` |
| `CAWL_DAEMON_URL` | how Traefik reaches the daemon, e.g. `http://127.0.0.1:8000` |
| `CAWL_TAILSCALE_AUTHKEY` / `CAWL_SSH_JUMP` | which SSH access provider to use; see [SSH access](#ssh-access) |
| `CAWL_SSH_CA_KEY` | path to the SSH certificate authority key (`ssh-keygen -t ed25519`). Root-only: it's the key to every box |

Email is optional at first. Sign-in links are printed by `cawl expose`
anyway, so you can hand them out yourself until SMTP is configured.

## Docker Compose deployment

The repository includes a Compose deployment for the control plane, Postgres,
the expiry reaper, and Traefik. It is suitable when Docker is running on the
same host as Incus (or can route to the Incus API and instance bridge):

```bash
cd server/deploy/docker
cp .env.example .env
# edit .env and place Incus certificates in secrets/incus/;
# generate secrets/ssh_ca if developers will use cawl ssh
docker compose up -d --build
```

The compose file runs migrations before starting the daemon, shares its
Traefik dynamic-config volume with Traefik, and runs `manage.py reap` every
minute. The daemon API is published on `${CAWL_API_PORT:-8000}`; use that
address for `CAWL_API_URL`. Traefik owns ports 80 and 443.

Set `CAWL_BASE_DOMAIN` and make both its apex and wildcard DNS records point
to the host. `TRAEFIK_DNS_PROVIDER` and its provider credentials (for example
`CF_DNS_API_TOKEN`) are used to obtain the wildcard certificate. The Traefik
container must be able to route to Incus instance IPs, not only to the Incus
REST API. The compose file maps `host.docker.internal` to Docker's host gateway;
set `CAWL_INCUS_URL` to another routable address when Incus is remote.

## SSH access

How a developer's machine reaches a box's sshd is one deployment-wide choice,
made in `daemon.env`: set `CAWL_ACCESS` to `tailscale`, `jump`, or `bridge`
(leave it unset and cawl infers it: an authkey means `tailscale`, else a jump
host means `jump`, else `bridge`). It's transport only: whichever you pick, the
box accepts nothing but a certificate the daemon signed, so who gets in never
depends on who can route packets.

**Tailscale** (`CAWL_ACCESS=tailscale`): every box joins your tailnet at boot
and is dialed by MagicDNS name (`<id>.<your-tailnet>.ts.net`):

```bash
# an ephemeral + reusable + pre-authorized key, from the Tailscale admin console
CAWL_TAILSCALE_AUTHKEY=tskey-auth-xxxx
CAWL_TAILNET=tailXXXX.ts.net     # your tailnet name — it's the SSH host suffix
CAWL_TAILSCALE_TAGS=tag:cawl     # optional: tag the nodes for your ACLs
```

Ephemeral matters: a stopped box is dropped from the tailnet and rejoins on
start, so dead environments don't pile up in the admin console. The tailnet
ACL needs nothing but tcp/22 reachability to the boxes; there are no `ssh`
rules to write, because cawl deliberately doesn't use Tailscale SSH.

**Jump host** (`CAWL_ACCESS=jump`): boxes are dialed by bridge IP, relayed
through a host your developers can already SSH to. If they have accounts on
the cawl host itself, that's the whole setup:

```bash
CAWL_SSH_JUMP=cawl-host.example.com     # [user@]host[:port]
```

`cawl ssh` wires the relay up itself (a `ProxyCommand` through the jump, using
each developer's own SSH config and credentials for that hop). Other clients
(VS Code Remote-SSH and friends) add `ProxyJump cawl-host.example.com` for the
bridge subnet in `~/.ssh/config`.

**Bridge** (`CAWL_ACCESS=bridge`): boxes are dialed by bridge IP directly.
For when developers already have a route to the Incus bridge: they're on the
host's LAN, or you run your own WireGuard/NetBird/… into it. cawl runs
nothing in the box and stays out of your network's way.

Naming a provider whose settings are missing (say `CAWL_ACCESS=tailscale`
without an authkey) is a startup error, not a silent fallback. With
`CAWL_ACCESS` unset and both an authkey and a jump host present, the
authkey wins.

None of these fit? `CAWL_ACCESS` also takes the dotted path of a provider
class you install yourself; see [Extending](extending.md).

## DNS and Traefik

One wildcard record, once, and you never think about DNS again:

```
*.sbx.example.com  →  the host's public IP
```

Install Traefik with the config from `server/deploy/traefik/`, and put your
DNS provider's API token in `/etc/cawl/traefik.env`; that's how it gets a
single wildcard certificate covering every environment. From then on the
daemon writes a small routing file per environment into a watched directory,
and Traefik picks up changes on its own.

One thing that trips people up: Tailscale Funnel can't serve this. Funnel
only speaks its own `ts.net` hostname, so the wildcard domain needs a real
public IP (or a Cloudflare Tunnel).

## Services

Three systemd units, all provided in `server/deploy/`:

```bash
systemctl enable --now traefik cawl-daemon cawl-reap.timer
```

Don't skip `cawl-reap.timer`: it runs the reaper every minute, and the
reaper is what destroys expired environments. Without it, review and agent
boxes never die.

## People and tokens

Humans just run `cawl login`; the daemon mints them a token through the
browser. Agents and CI get tokens directly:

```bash
uv run manage.py mint_token ci-bot --quota 5 --ttl 90d --max-ttl 4h
```

`--quota` and `--max-ttl` are the guardrails for tokens you hand to machines:
quota caps how many environments the token can hold at once, and `--max-ttl`
gives everything it creates a lifetime it can't opt out of, so a crashed
agent's boxes always age out.

Admin work (templates, other people's environments, image rebuilds) needs a
token minted with `--role admin`, or the Django admin at `/admin/`.

## Templates

Register a template per app, and rebuild its image whenever you want fresh
data (a nightly timer is the usual choice):

```bash
cawl template create < template.yaml
cawl refresh-image acme-cms
```

Writing one is its own topic; see [Templates](templates.md).
