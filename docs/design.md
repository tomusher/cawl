# cawl environments: design

Ephemeral, isolated environments. One environment definition (a **template**),
consumed by three lifecycles that differ **only in how the app is
launched and how long it lives**.

## Goals

1. **Dev**: a developer spins up a fully-fledged, persistent box to work on a site.
2. **Review**: an ephemeral, public (sign-in-gated) environment for a branch.
3. **Agent**: an AI agent spins up a disposable, isolated environment, codes in it, tears it down.

## Core model

- **Template**: a named, versioned `template.yaml` the daemon stores in its
  database (uploaded with `cawl template create`): which golden image to boot,
  which **arguments** it accepts, and the **hooks** to run. The template name is
  the `name:` key of its YAML, the handle `cawl up` takes.
- **Environment**: a running materialization of a template, keyed by
  `(template, args, owner)`. Gets its own VM, IP, hostname, and TTL.
  ("Instance" survives only at the runtime layer, where it is Incus's own noun
  for the VM realizing an environment.)

There is deliberately no "kind of environment". Boxes are multi-purpose
workspaces: the same box gets worked on, exposed to an outside viewer for a
while, unexposed, and worked on again. Lifetime and isolation come from
template defaults, per-env flags, and token guardrails, not from a type.

**Bringing code up is not the daemon's job.** cawl boots a box from an image and
runs the template's hooks in it. Cloning a repo, checking out a branch, starting a
compose stack, seeding a database: all of that lives in the template, expressed
as shell, with the template's own args substituted in. The daemon validates args
against what the template declares and otherwise attaches no meaning to them.

## Architecture

Every environment is an **Incus VM** running the app's existing **`docker compose`** stack
inside it. This was the key simplification:

- The compose file *is* the service topology (Postgres, Redis, Elasticsearch, app), so there's no re-authoring.
- A VM is a real Linux box, so Docker Just Works (no nested-container pain).
- A VM gives a **hard kernel boundary for every purpose for free**, so a single runtime
  (Incus) covers dev, review, *and* untrusted agent code. No second runtime (SmolVM/Firecracker) is needed.

Backing services live **in-instance** (each env is one self-contained machine). A shared
Elasticsearch cluster is a possible later optimization; Postgres/Redis stay in-instance.

Use **Docker** (not Podman) inside the VM: existing compose files are tested against it, and
the VM boundary already provides the isolation Podman-rootless would buy.

```
┌─ Ingress node ────────────────────────────────────────────┐
│  Traefik on the host's public IP (wildcard LE cert, DNS-01)│
│    · routes *.sbx.example.com by Host → <vm-ip>:<port>      │
│    · forward-auth middleware → daemon decides per request  │
├─ Control plane (the product we build) ────────────────────┤
│  cawl CLI + REST API over Incus · state DB · reaper (TTLs)  │
├─ Incus host(s) — ZFS-backed ──────────────────────────────┤
│  per-template golden VM image → COW-cloned instances       │
│  each VM runs `docker compose up`                          │
└────────────────────────────────────────────────────────────┘
```

Scale target: ~30-40 concurrent envs, ~20 devs, on 1-2 beefy hosts (128-256GB).
ES (~1.5-2GB each) is the RAM driver. Incus clustering adds burst hosts later.

## `template.yaml` (per-repo)

```yaml
name: acme-cms
image: cawl/acme-cms

params:                            # the args this template takes
  branch:
    default: main
    pattern: '^[A-Za-z0-9._/-]+$'

hooks:                             # what it does with them. cawl runs these; it
  build: |                         # doesn't read them.
    git clone git@github.com:acme/acme-cms.git /srv/app
    cd /srv/app && git checkout {{ branch }}
    docker compose build && docker compose up -d
    aws s3 cp s3://acme-dumps/acme/latest.sql.gz - | gunzip \
      | docker compose exec -T db psql -U app -d acme      # ~200MB anonymized dump
    docker compose run --rm app python manage.py migrate
    docker compose stop                                    # keep volumes!

  provision: |
    cd /srv/app
    git fetch --all --quiet && git checkout {{ branch }}
    if [ "$mode" = review ]; then                          # gunicorn, DEBUG off
      docker compose -f docker-compose.yml -f compose.review.yml up -d
    else
      docker compose up -d
    fi

expose:                            # default exposures, live at `up`; the same
  web: 8000                        # rows `cawl expose` edits afterwards

defaults:
  ttl: { dev: none, review: 7d, agent: 4h }
```

`{{ branch }}` expands to a **shell-quoted** literal, so an arg can't break out of
the script it lands in; `$branch` is exported too, alongside the built-ins `$id`,
`$template`, `$isolation`. A hook that references an arg the template
never declared is rejected at upload time, not on the first `cawl up`.

Per-mode difference is whatever the hook does with a declared arg (`mode`
above), plus TTL. There's no `services:` block and no `compose:` block: the
compose file owns topology, and the hook owns how it's invoked.

## Golden-image pipeline

```
base VM image (Ubuntu + Docker + cloud-init + incus-agent)   # bundled build-base-image.sh
  └─► per-template golden image (`cawl refresh-image <name>`):
        boot a builder from the base image
        run the template's `build` hook   ── clone, compose build, seed, migrate, stop
        → publish whatever it left on disk as an Incus image
```

Each env is a **COW clone** of that image; the `provision` hook runs in it once
Docker is up, so it's live in seconds with data already present. Rebuild nightly
to refresh anonymized data. A template with no hooks (the `scratch` box) just
boots the image.

## Control plane

**Environment state record** (SQLite/Postgres):
```
id · template · args · args_hash · owner · status · vm_ip · url · image · created_at · expires_at
```

`args_hash` is a digest of the *resolved* args (defaults filled in). It's what
makes `--reuse-if-exists` mean "the env I'd have got", rather than "any env of
this template": two envs of one template built from different args are different envs.

**Runtime interface** (keeps the backend swappable):
```python
class IncusApiRuntime:
    def build_image(spec)           # boot builder → run spec.build → publish image
    def create(spec)                # clone image → wait for Docker → run spec.provision
    def stop(id)                    # graceful shutdown; the disk stays
    def start(spec)                 # power on → *replay the boot half* (see below)
    def destroy(id)                 # DELETE /1.0/instances/<id>
    def exec(id, cmd)               # POST /1.0/instances/<id>/exec
    def info(id)                    # VM IP + status → ingress
    def snapshot(id) / restore(id, snap)   # dev "reset to clean"
```

The hooks reach a runtime **already rendered** (args substituted and quoted), so
no backend knows what git or compose are; it just runs a string in a box.

**Pause (`stop`/`start`).** RAM is the binding constraint (ES ~1.5-2GB/env), so an
env you're not using should be able to give it back without being destroyed. `stop`
is a graceful shutdown: the disk, the id, the name and the SSH identity all stay.

The catch is that `start` is *not* "power on". Provisioning splits in two:

- **once, at create**: things that persist on the box's disk: the CA trust,
  any seeded keys. (The host cert also persists, but is *re-signed* on every
  start: under an IP-based access provider the address clients dial moves,
  and a cert naming yesterday's address is a verification failure.)
- **every boot**: things that don't survive a shutdown: the box's network
  membership (with Tailscale access the authkey is **ephemeral**, so a stopped
  node is dropped and its MagicDNS name stops resolving), and the app itself
  (Docker won't restart containers that carry no restart policy).

`start` replays the second half. A resume that only powered the box on would hand
back an unreachable box running nothing. Two consequences worth stating. The route
is **rebound**, not re-registered; re-registering mints a fresh basic-auth password
and would silently lock a viewer out of a review URL they already hold. And a
stopped env still counts against quota and still ages toward its TTL, so `stop` is
a pause, not a way to park a VM past the reaper.

**Reaper**: a loop that destroys anything past `expires_at`. Non-negotiable, or
review and agent envs bury the hosts.

## Ingress & access

- **Public URLs are exposures** (see "Exposures & browser access" below): the
  daemon writes a Traefik route per exposed port (`Host(<id>.sbx.example.com)`
  or `Host(<name>--<id>.…)` → `<vm-ip>:<port>`), each behind a forward-auth
  middleware that asks the daemon, per request, whether the visitor may view
  it. Traefik terminates TLS on the host's public IP with one wildcard cert
  (DNS-01). Not Tailscale Funnel: Funnel is single-hostname and SNI-routed, so
  it can't serve a custom wildcard domain.
- **Dev access = certificate SSH over an access provider**: how a client reaches a
  box's sshd is a deployment-wide choice (`cawl_core/access.py`) that supplies
  connectivity and a dial target, but *not* authentication. The box's own sshd
  trusts one thing: a cert signed by the daemon's CA, whose principal is that
  box's instance id. `cawl ssh <id>` obtains one. VS Code Remote-SSH / Cursor /
  JetBrains Gateway ride over it like any sshd. Three providers ship:
  **Tailscale** (boxes join the tailnet at boot and clients dial MagicDNS
  names; the batteries-included default when an authkey is set), **jump**
  (bridge IPs relayed through a host developers can already SSH to, typically
  the cawl host itself; `cawl ssh` wires up the ProxyCommand, and how they
  authenticate to the hop is their existing account, not cawl's concern), and
  **bridge** (clients dial the instance's bridge IP directly; routing to it, a
  LAN or the operator's own WireGuard/NetBird, is the deployment's business,
  and cawl runs nothing in the box for it). Networking is deliberately *not* a
  template concern: templates describe apps, and a join credential in template
  hooks would hand a network-wide secret to user-authored scripts.

  **Not Tailscale SSH**, deliberately. It authenticates from tailnet identity
  inside `tailscaled`, never reading `sshd_config` or `authorized_keys`, and
  knows nothing about who *owns* an environment, so any tailnet member matching
  an `ssh` policy rule could enter any matching box. That's precisely the
  ownership hole the API doesn't have. See "Access control" below.

## Exposures & browser access

An **exposure** is one exported port of an environment: a row `(environment, name,
port, access)` in the daemon, exactly parallel to a share grant. Route files
are rendered from the rows and the forward-auth check reads them per request,
so exposing, changing access, and unexposing never touch the VM and revocation
is immediate. `cawl expose <id> <port> [--name] [--access emails]`; templates
declare defaults (`expose: {web: 8000}`) that `up` materializes as ordinary
rows.

**Names are free-form labels in one global namespace.** `--name acme-preview`
=> `acme-preview.<domain>`; the daemon resolves a request's hostname to an
exposure by *looking the label up*, not by parsing it, so a name needn't
mention the VM. Uniqueness is enforced across live envs (destroyed ones
release their names), every env's own id is reserved as its default label, and
the auth host's label is off-limits. Defaults keep a scoped convention so they
can't collide: bare `--name`-less exposes and the template `web` key land on
`<id>.<domain>`; other template keys on `<key>--<id>.<domain>`. Everything is
one label deep, so a single wildcard DNS record and a single wildcard
certificate cover every exposure forever, and individual hostnames never
appear in CT logs.

**Authorization is the daemon's, per request.** Traefik's `cawl-auth`
forward-auth middleware (on every exposure router, with no bypass, so it fails
closed) subrequests `/auth/forward`. Identity comes from a signed, host-scoped
cookie; the decision is: email on the exposure's `access` list, or, mapped to a
principal via their account, the same `can_view` that guards `exec` and
`ssh`. So `cawl share` extends browser access automatically, and an empty
access list means "the people who can already use this env".

**Login is a code-for-cookie handoff, deliberately not a parent-domain
cookie.** Traefik forwards request cookies into the VM, and agent VMs run
untrusted code, so a parent-scoped session would be readable (and replayable) by
every exposed app. Instead: an unauthenticated request 302s to the auth host
(`auth.<domain>`, served by the daemon), which authenticates via a dedicated
exposure-host-scoped viewer cookie or a one-time, host-scoped **magic link** (sent by
email, and returned by `cawl expose` for handing out). Viewer credentials use
their own token records and cookie, separate from Django users and authentication
sessions, so a magic link can never sign its browser into the admin or API. An
existing Django/OIDC session is accepted as viewer identity for team members,
but that bridge is one-way. The auth host redirects back to
`https://<host>/.cawl/callback?code=…`, a reserved path Traefik routes to the
daemon on every exposed host. The callback trades the 60-second, host-bound
code for a `__Host-` cookie naming that host. A cookie harvested inside one VM
names that VM's host and fails the check everywhere else. Magic links are
one-time and host-scoped, and every request is still authorized against the
current access list.

## The three workflows

These aren't environment types (there is no purpose flag), just the three
shapes environments habitually take, assembled from the same parts: template
defaults, per-env `--ttl`/`--isolation`, template args (like `mode`), and
token guardrails (`max_ttl`, forced isolation) for machine callers.

| Workflow | What runs | Isolation | TTL | Access |
|---|---|---|---|---|
| Dev | dev compose, in-VM checkout, runserver | VM | none | `cawl ssh` / Remote editor |
| Review | review-override compose, code baked at SHA, gunicorn | VM | 7d | exposure URL + sign-in; SSH like any env |
| Agent | dev compose, writable checkout | VM | 4h | `cawl exec` / `cawl ssh`, agent runs in-VM |

**Dev workflow**: SSH into the VM and work directly there; there is no host
bind-mount. Compose bind-mounts are local to the VM's own filesystem, so the dev
loop behaves like native Linux `docker compose`. The VM is a pet but
reconstructible: source of truth stays in git; nightly snapshots + normal push
hygiene cover data loss.

## Agent invocation model

Agents (e.g. a Claude Code skill) call the same `cawl` CLI. Design the surface so an agent
can drive it reliably without a human in the loop.

### Contract

```
$ cawl up acme-cms --arg branch=feature/x --json
{
  "id": "acme-cms-a1b2c3d4e5f60718293a4b5c6d7e8f90",
  "status": "ready",
  "args": {"branch": "feature/x"},
  "ssh": "dev@acme-cms-a1b2c3d4e5f60718293a4b5c6d7e8f90.<tailnet>",   # reach it with `cawl ssh <id>`
  "url": "https://acme-cms-a1b2c3d4e5f60718293a4b5c6d7e8f90.review.example.com",
  "shared_with": [],
  "expires_at": "2026-07-08T18:00:00Z"
}
```

### Principles for agent-friendliness

1. **`--json` on every command**, stable schema. `up`, `status`, `ls`, `exec` all emit
   machine-readable output.
2. **Honest exit codes + structured errors** so a skill can branch on failure.
3. **`cawl exec` is transparent**: it passes through stdout/stderr and the inner exit code,
   so `cawl exec <id> -- pytest` fails the way the agent expects.
4. **Idempotent / leased**: `cawl up --reuse-if-exists` keyed on `(template, args, owner)` so a
   retried skill doesn't spawn duplicates: same args, same env. Each agent gets a unique
   `--owner agent-<uuid>`.
5. **Quotas + auto-reap**: per-owner max concurrent envs + short default TTL, so a
   runaway agent can't leak 500 VMs. Guardrails live on the *token*, not the
   request: `mint_token --quota 5 --max-ttl 4h` gives every env that token
   creates a lifetime it can't opt out of (fills in when no TTL applies, caps
   any that's requested). The reaper is the backstop; explicit `cawl rm` on
   completion is the happy path.
6. **Attribution**: agents authenticate with an identity/token → quota + audit per requester.

### Preferred execution shape: run the agent *inside* the VM

Two options for where the agent's tools operate:

- **(A) Agent on host, drives via `cawl exec`**: every command round-trips through exec,
  and file editing is awkward (the agent's Edit/Read tools want a local FS).
- **(B) Agent runs inside the VM**: recommended. The outer orchestration does `cawl up`,
  gets the SSH target, then launches the agent (e.g. `claude`) *in* the VM. All of the
  agent's native tools (Bash, Edit, Read, test runners) operate directly on the in-VM
  checkout, with the same ergonomics as a developer SSH'd in. The VM's kernel boundary
  contains any code the agent runs.

Typical skill flow (option B):
```
id=$(cawl up "$TEMPLATE" --arg branch="$BRANCH" --json | jq -r .id)
# `cawl ssh` fetches a certificate for this env (the same ownership check as exec)
# and hands off to ssh — no keys to place in the box, none to clean up after.
cawl ssh "$id" -- "cd /srv/$SITE && claude -p '<task>'"
cawl rm "$id"          # explicit teardown; reaper is the backstop
```

### Ship a Claude Code skill

Bundle a `SKILL.md` documenting the `cawl` command surface (up/status/exec/rm, the `--json`
schema, quotas) so any Claude Code agent can discover and use the tool. The skill is a thin
wrapper: bring the env up, run the task inside it, tear it down.

## Blank / scratch dev environments

A template with no `hooks` (and no `params`/`expose`) is a **blank dev box**:
`template.yaml` needs only a `name:`. `create` boots the base golden image
(Ubuntu + Docker + git + tooling + a `dev` user) and stops there; there's no
hook to run. The user SSHes in and clones their own repos:

```
cawl ssh scratch-<id>                   # cert-authenticated; agent forwarded
git clone git@github.com:you/app        # uses the forwarded agent — keys never enter the VM
docker compose up                       # the cloned repo brings its own services
```

Access is SSH-only (no public URL); TTL `none` (persistent). Register it
like any template: `cawl template create < scratch.yaml` uploads the body to
the daemon, blank included. The daemon owns every template definition (versioned
in its database), so there's no config repo to check out.

This is the same machinery as `acme-cms`, with the hooks left empty: a template
that brings up an app and a template that hands you a bare box differ only in what
they wrote in `hooks:`.

**SSH auth** is the certificate path described under "Access control" below: the
box takes the CA at create time and nothing else. (`InstanceSpec.authorized_keys`
survives as a static-key escape hatch, seeding public keys for the `dev` user, but
nothing in the daemon populates it.)

## Access control

`owner` is not a self-asserted flag; it's the **authenticated principal**, set
server-side. Two roles, and two tiers of access to an environment:

| Action | user | shared with | admin |
|---|---|---|---|
| `up` (owned by self) | ✅ | - | ✅ |
| `ls` / `status` / `exec` / `ssh` | own environments | ✅ | any environment |
| `share` / `unshare` | own environments | ❌ | ✅ |
| `rm`, change TTL | own environments | ❌ | ✅ |
| `up --owner X` (on behalf of) | ❌ | ❌ | ✅ |
| `refresh-image`, `reap` | ❌ | ❌ | ✅ (reaper runs as `system`) |

Denied operations exit non-zero (CLI exit code 3). Sharing an env is not handing
it over: a grantee can use it, not destroy it.

### SSH is the same decision, not a second one

The hard part of SSH authorization is that the daemon isn't in the connection
path, so the policy has to reach the box somehow. Three places it could live:
the tailnet policy (per-owner tags, but tags are static strings with no
interpolation, so every user and every share means a policy edit), the box
(`authorized_keys` per grantee, but then every grant is a write into a running
VM that can fail, and the DB and the box can disagree), or **the daemon**, which
is where we put it.

The move that makes it work is what the certificate's *principal* means:

- Each environment's `authorized_principals` file names **the environment itself** (its
  instance id). Written once at create. It never lists a person, and is never
  edited again.
- The daemon holds the CA. `cawl ssh <id>` sends a public key; the daemon runs
  `require_access(actor, inst)` (the *same* check `exec` and `rm` use) and, if
  it passes, signs a cert with `principal = <id>`, `key_id = <actor>@<id>`, valid
  ~10 minutes.
- sshd accepts it because the CA signed it and the principal matches the box.

So an environment never learns who its users are; it only ever learns which one it
is. Consequences worth naming:

- **Sharing is a row.** `EnvironmentGrant` in the DB. Granting and revoking never
  touch the VM, so they work on a stopped box, can't half-apply, and can't drift.
  Teams (later) expand to principals *at signing time*; nothing else needs to
  learn what a team is.
- **Revocation is real**: a revoked grantee simply isn't signed for again. An
  already-open session survives until it ends (kill it via `exec` if needed).
  That's the honest limit of this design.
- **The CA key is the crown jewel**: it mints access to every environment. Root-only
  on the daemon host.
- **One login account** (`dev`) for every purpose, since the cert carries the
  identity; sshd logs the cert's key id, so a shared account still attributes the
  session to a person. The cost is a shared home directory; per-user accounts
  would mean writing into the box on every grant, i.e. the drift we just avoided.
- **Host keys are signed too**, by the same CA, so clients verify a box instead of
  trusting it on first sight. Necessary, not decorative: clones regenerate host
  keys and instance names get recycled, so TOFU would produce mismatch warnings
  as routine background noise.

**Identity comes from the transport, and this requires a control-plane daemon.**
Authz is only real if users cannot touch `state.db` or the `incus` socket
directly, so the deployment is client/server:

- The **daemon** owns state + Incus and enforces the policy above. It derives the
  principal from the authenticated transport and never trusts client claims.
- **Humans**: put the daemon behind `tailscale serve`; the caller's verified
  tailnet identity (`tailscale whois` / `Tailscale-User-Login` header) is the
  principal. No passwords. Admin membership from a cawl admin-list (or a
  Tailscale ACL tag/grant).
- **Agents**: a scoped **capability token** whose subject is the principal, with
  its own quota + short TTL. A distinct token per agent session gives isolation
  and audit.

The policy lives in `auth.py` (`Principal`, `Role`, `can_view`, `require_access`,
`require_owner`, `require_admin`) and is enforced in `control.py` on every op,
including `ssh`, which is why SSH can't route around it. The CLI's local-mode
identity (`CAWL_ACTOR` / OS user) is a dev convenience the daemon ignores.

### The daemon (`server/`, Django + Ninja)

The daemon is that trust boundary, implemented as a Django app that reuses the
cawl core as a library:

- **`DjangoStateStore`** adapts the ORM to the state-store interface, so the
  tested `ControlPlane` runs unchanged against Postgres. Environments are
  **soft-deleted** (`status=destroyed`) so history survives teardown.
- **Models**: `Template` (registry: DB-stored `template.yaml` body + `version`),
  `TemplateVersion` (append-only config history), `Environment` (mirrors the
  core record, pins the `template_version` it launched from),
  `EnvironmentEvent` (append-only history), `ApiToken` (hashed, subject + role + quota + expiry).
- **API** (Django Ninja, `/api`): `whoami`, `environments` CRUD, `exec`,
  `images/refresh`. Auth = bearer token (agents) or session (humans via OIDC);
  the ControlPlane enforces authz. 401/403/404/409 mapped from cawl exceptions.
- **Admin**: Django admin with destroy/extend actions that call the
  ControlPlane (real teardown + ingress cleanup), plus event history inline.
- **Reaping**: `manage.py reap` runs continuously in the Compose `reaper` service.
- **Tokens**: `manage.py mint_token <subject> --role --quota --ttl`.

**The CLI is a remote-only client** (`cawl/client.py` + `cawl/cli.py`): it talks
to the daemon over HTTP via `CAWL_API_URL` + `CAWL_TOKEN` and holds no state, so
the daemon is the sole source of truth. The SQLite `StateStore` remains only as
an alternate backend exercised by the core tests.

## Build order

1. `template.yaml` + `IncusApiRuntime.build_image`/`create`/`exec`: one app up manually.
2. Golden-image seeding + COW clone: prove the 200MB story boots in seconds.
3. Control-plane state DB + reaper.
4. Traefik ingress (public IP + wildcard DNS-01) + exposures; an access provider (tailnet or bridge IPs) + certificate SSH for dev access.
5. Agent surface: `--json`, quotas, leasing + the Claude Code skill.

## Open questions

- Fresh `git clone` on create vs baked-main + `fetch/checkout` (speed vs branch flexibility).
- Shared ES cluster threshold: at what env count does per-VM ES stop being affordable?
- Dotfiles bootstrap mechanism for dev VMs (chezmoi / personal repo) on `post_create`.
- Agent identity/token issuance + quota policy.
