# Usage

Most days with cawl fall into one of these patterns.

## A dev box

Bring up the site on the branch you're working on, and treat the box as your
dev machine:

```bash
cawl up acme-cms --name acme-dev --arg branch=feature/search
cawl ssh acme-dev
```

VS Code, Cursor and JetBrains all speak SSH, and so do `rsync`, `scp` and
`sshfs`. None of them know about cawl, so hand them a config once:

```bash
cawl ssh-config > ~/.ssh/cawl.config     # then `Include cawl.config`
```

and `acme-dev` is an ordinary SSH host to all of them — point Remote-SSH at it
and edit in place. See [the CLI page](cli.md#other-ssh-tools) for the include
line and what it's doing. The code, the services and the data are all on the
box, and the dev loop feels like local `docker compose`. Your SSH agent comes
along for the ride with `cawl ssh`, so `git push` from inside the box is just
you.

Working on files that live on your own machine is the one thing to avoid:
mounting your laptop into the box means every file the app reads crosses the
network, and file-change events don't survive the trip, so nothing that watches
for edits — the reloader, a test watcher, a bundler — will notice them. Keep
the source on the box, or `rsync` into it.

Dev boxes don't expire, but memory is the thing the hosts actually run out
of, so when you down tools for a while:

```bash
cawl stop acme-dev     # frees the RAM, keeps the disk
cawl start acme-dev    # picks up where you left off
```

## Working with someone

```bash
cawl share acme-dev --with sue
```

Sue can now see the box, SSH in, and open its URLs in her browser. She can't
delete it or pause it out from under you; it's still yours. `cawl unshare`
undoes it.

## A review link

Run the branch the way production would, give it a lifetime so it cleans
itself up, and let someone in:

```bash
cawl up acme-cms --ttl 7d --arg branch=feature/search --arg mode=review --name acme-search
cawl expose acme-search 8000 --access pat@example.com
```

`expose` prints a sign-in link for each address. Send it over and Pat can
click around while nobody else can. Change who's allowed by running `expose`
again with a different list; someone you remove is locked out on their next
click.

Want a nicer address? Names are free-form:

```bash
cawl expose acme-search 8000 --name acme-preview --access pat@example.com
```

That's `https://acme-preview.<domain>`, and the name is yours until the
environment goes away.

## More than one port

Anything listening in the box can have its own URL:

```bash
cawl expose acme-dev 6006 --name acme-storybook
cawl expose acme-dev 8025 --name acme-mail
```

## A blank box

The `scratch` template is an empty Ubuntu machine with Docker and git
installed, and nothing else. SSH in and clone whatever you're working on;
because your agent is forwarded, your keys never land in the box.

```bash
cawl up scratch --name playground
cawl ssh playground
```

## Boxes for agents

Agents use the same commands, with a few habits that keep things tidy: a
short lifetime, a request that's safe to retry, and cleanup when done.

```bash
cawl up acme-cms --arg branch=fix/tests --reuse-if-exists --json
cawl ssh <id> -- 'cd /srv/app && claude -p "make the tests pass"'
cawl rm <id>
```

`--reuse-if-exists` means asking twice for the same thing hands back the same
box. And agent tokens are minted with a lifetime cap (`max_ttl`), so a crashed
run's boxes age out no matter what the run asked for. If you're wiring up
Claude Code, there's a ready-made skill in the repo under `skill/`.

## Scripting

Every command takes `--json` and prints something with a stable shape. `exec`
and `ssh` pass through the inner command's exit code, and a permission
problem always exits with code 3, so a script can tell "it failed" from
"you're not allowed".

## Getting a new site onto cawl

That's a template's job: one YAML file that says how to build and start the
site. See [Templates](templates.md).
