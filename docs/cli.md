# CLI

`cawl` is a thin client that talks to the daemon; it keeps nothing on your
machine beyond your login. Everything below works the same from a laptop, a
CI job, or an agent.

## Signing in

```bash
cawl login                   # browser flow
cawl login --headless        # paste a code instead (for SSH sessions)
cawl login --token cawl_...  # supply a token directly
cawl whoami
cawl logout
```

Scripts and agents skip login and set two environment variables instead;
they take precedence over stored credentials:

```bash
export CAWL_API_URL=https://cawl.example.com
export CAWL_TOKEN=cawl_...
```

## Environments

```bash
cawl up <template> [options]
cawl ls [--template <template>]
cawl status <id>
cawl rm <id>
```

Options for `up`:

| Option | What it does |
| --- | --- |
| `--name` | pick the id instead of getting a generated one |
| `--arg k=v` | a template argument (repeatable); `cawl template show <template>` lists them |
| `--ttl` | override the lifetime: `4h`, `7d`, `none` |
| `--backend` | which named backend materializes the env — the names are your deployment's own (an Incus one tends to have `vm` and `container`) |
| `--reuse-if-exists` | hand back the matching environment if one already exists |

## Getting inside

```bash
cawl ssh <id>                 # a shell
cawl ssh <id> -- <command>    # one command, over SSH
cawl exec <id> -- <command>   # one command, over the API
```

Both hand you the inner command's exit code. `ssh` fetches a short-lived
certificate each time, so there are no keys to install or clean up.

## Other SSH tools

```bash
cawl ssh-config [<id>...]     # stanzas for anything else that speaks SSH
cawl cert <id>                # sign a certificate without connecting
```

`ssh-config` prints an `ssh_config` stanza per environment — every one you can
see, or just the ones you name. Write it somewhere and include it:

```bash
cawl ssh-config > ~/.ssh/cawl.config
```

```
Include cawl.config     # the FIRST line of ~/.ssh/config
```

It has to come first: in `ssh_config` the first value found for a keyword wins,
so a `Host *` block of your own placed above it would override these. From then
on an environment's id is an ordinary SSH host, and the tools cawl doesn't
implement work by themselves:

```bash
ssh acme-dev                             # no `cawl` in front
rsync -a ./data/ acme-dev:/srv/data/     # copy files in
scp acme-dev:/tmp/report.csv .           # and out
sshfs acme-dev:/srv/app ~/mnt/app        # mount the box's filesystem locally
code --remote ssh-remote+acme-dev /srv/app
```

Nothing durable is installed in the box by any of that. Each stanza runs `cawl
cert <id>` as ssh dials, which asks the daemon to sign a fresh certificate; if
the environment stops being yours, the next dial simply isn't signed. Agent
forwarding is off in these stanzas, unlike `cawl ssh` — an editor holds a
connection open for days, and anyone the box is shared with has sudo in it.
Add `ForwardAgent yes` to a stanza if you want `git push` from inside to be you.

Regenerate after `cawl up` or `cawl rm`. Also regenerate after a restart if
your deployment hands out bridge addresses rather than stable names: the
address is pinned in the stanza, and a restarted box may not have the old one.

## URLs

```bash
cawl expose <id> <port> [--name <label>] [--access a@x.com,b@y.com]
cawl unexpose <id> <name>
```

The name is the hostname: `https://<name>.<domain>`, any label that's free.
Left out, it defaults to the environment's id. `--access` admits outside
viewers by email; everyone with access to the environment itself always gets
in.

## Sharing and pausing

```bash
cawl share <id> --with <who>
cawl unshare <id> --from <who>
cawl stop <id>                # frees the RAM, keeps the disk
cawl start <id>               # brings it back, same id and URLs
```

## Templates and images

```bash
cawl template ls
cawl template show <name>
cawl template create < template.yaml          # personal template
cawl template create --global < template.yaml # admin: publish for everyone
cawl template rm <name>
cawl refresh-image <name> [--arg k=v]         # admin only
```

## Conventions

- `--json` on any command prints machine-readable output with a stable shape.
- Exit code `3` means the daemon said no: not yours, or not an admin.
  Anything else nonzero is an actual failure.
- Ids are hostnames: lowercase letters, digits and dashes, unique across
  everyone. A destroyed environment's id is free to reuse.
