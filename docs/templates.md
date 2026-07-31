# Templates

A template teaches cawl one app. It's a single file, `template.yaml`, that
answers three questions: which image to boot, which arguments to accept, and
what to run. cawl deliberately knows nothing about your app: cloning a repo,
starting compose, and seeding a database are all things the template says, in
plain shell.

Templates are registered with the daemon and versioned: each user can create
personal templates, while admins can publish global ones with `--global`.
Re-uploading a template you own creates a new version; running environments
keep the one they launched from. Template handles are deployment-wide unique,
so one user's template cannot shadow another's.

```bash
cawl template create < template.yaml          # personal
cawl template create --global < template.yaml # admin: available to everyone
cawl template ls
cawl template show acme-cms
```

## A full example

```yaml
name: acme-cms                 # the handle `cawl up` takes
image: cawl/acme-cms           # golden image to clone (see below)

params:                        # the arguments this template accepts
  branch:
    default: main
    description: git branch, tag or SHA to check out
    pattern: '^[A-Za-z0-9._/-]+$'
  mode:
    default: dev
    choices: [dev, review]
    description: review starts the stack the way production would

hooks:
  build: |                     # runs once, when the image is baked
    git clone git@github.com:acme/acme-cms.git /srv/app
    cd /srv/app && git checkout {{ branch }}
    docker compose build && docker compose up -d
    aws s3 cp s3://acme-dumps/acme/latest.sql.gz - \
      | gunzip | docker compose exec -T db psql -U app -d acme
    docker compose run --rm app python manage.py migrate
    docker compose stop        # keep the volumes!

  provision: |                 # runs in every new environment
    cd /srv/app
    git fetch --all --quiet && git checkout {{ branch }}
    if [ "$mode" = review ]; then
      docker compose -f docker-compose.yml -f compose.review.yml up -d
    else
      docker compose up -d
    fi

expose:                        # URLs that are live as soon as it's up
  web: 8000

defaults:
  ttl: none              # the default lifetime; `up --ttl` overrides per env
```

The `examples/` directory in the repo has this one and a minimal `scratch`.

## The two hooks

**`build`** runs when an admin bakes the template's image
(`cawl refresh-image acme-cms`): a builder machine boots, the hook does the
slow work (clone, compose build, load the anonymized dump, migrate), and
whatever it leaves on disk becomes the image. Every environment is then a
copy-on-write clone of that, which is why they start in seconds with data
already in place. Rebuild nightly if you want the data fresh.

**`provision`** runs in each new environment once Docker is up, and again
every time a stopped one is started. Keep it fast: move onto the requested
branch, start the stack. The example declares a `mode` arg and uses it to start review
environments the way production would: a template concern, not a cawl one.

## Arguments

`params` declares what `--arg` may pass, and the hooks decide what it means.
Each param can have a `default`, `required: true`, a `choices` list, a
`pattern` to validate against, and a `description` (shown by
`cawl template show`).

Inside a hook, `{{ branch }}` expands to a shell-quoted literal, so an
argument can't break out of the script it lands in. Each arg is also exported
(`$branch`), alongside two built-ins: `$id` and `$template`. A hook that
references an argument the template never declared is rejected at upload, not
discovered by the first person to run it.

Two things to remember about args: they're part of an environment's identity
(`--reuse-if-exists` only matches an env built from the same args), and
they're not secrets: they're stored on the environment and shown in
`cawl ls`.

## Default exposures

The `expose` block lists ports that should have a URL from the moment the
environment is ready. The `web` key is the front door and lands on
`<id>.<domain>`; any other key lands on `<key>--<id>.<domain>`, scoped to the
environment so defaults never collide. They're ordinary exposures, the same
ones `cawl expose` and `cawl unexpose` edit afterwards.

## Defaults

`defaults.ttl` sets the default lifetime (`none` means no expiry),
overridable per environment at `cawl up` time. That's the only default a
template gets to set: which *backend* materializes an environment is
deployment vocabulary, so it comes from the operator's default, the
`--backend` flag, or a token guardrail — never from the template.

## The minimal template

Everything except `name` is optional. This is the whole of `scratch`:

```yaml
name: scratch
image: cawl/base
defaults:
  ttl: { dev: none }
```

No hooks means the box just boots (Ubuntu, Docker, git, an SSH-ready user)
and waits for you. A blank box and a full app are the same machinery; they
just wrote different things in `hooks:`.
