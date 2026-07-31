"""`cawl` — a thin, remote-only client for the control-plane daemon.

Authenticate with `cawl login` (browser or `--token`), or set env vars:
  CAWL_API_URL   e.g. https://cawl.example.com
  CAWL_TOKEN     a token minted by the daemon (`manage.py mint_token`)

Every command talks to the daemon over HTTP; there is no local state beyond the
stored login. `--json` is stable for scripting/agents, and `exec` passes through
the inner exit code.
"""

from __future__ import annotations

import json as jsonlib
import os
import shlex
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import click

from cawl import credentials
from cawl.client import ApiClient, ApiError, ConfigError, resolve_client

EXIT_ERROR = 1
EXIT_DENIED = 3


def _fail(as_json: bool, msg: str, code: int = EXIT_ERROR):
    if as_json:
        click.echo(jsonlib.dumps({"error": msg}))
    else:
        click.echo(f"error: {msg}", err=True)
    sys.exit(code)


@contextmanager
def _guard(as_json: bool):
    """Translate client/HTTP errors into friendly exits (403 -> 3)."""
    try:
        yield
    except ConfigError as e:
        _fail(as_json, str(e))
    except ApiError as e:
        _fail(as_json, e.message, EXIT_DENIED if e.status == 403 else EXIT_ERROR)


def _parse_args(pairs: tuple[str, ...], as_json: bool) -> dict:
    """`--arg k=v` (repeatable) -> a dict. What the keys mean is up to the template."""
    args = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            _fail(as_json, f"bad --arg {pair!r}: expected key=value")
        args[key.strip()] = value
    return args


def _fmt_args(args: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted((args or {}).items()))


def _emit(as_json: bool, d: dict, *, next_steps: bool = False):
    if as_json:
        click.echo(jsonlib.dumps(d))
        return

    click.echo(f"{d['id']}  [{d['status']}]  owner={d['owner']}")

    rows = []
    if d.get("args"):
        rows.append(("args", _fmt_args(d["args"])))
    if d.get("shared_with"):
        rows.append(("shared with", ", ".join(d["shared_with"])))
    for e in d.get("exposures") or []:
        gate = f"   (viewers: {', '.join(e['access'])})" if e.get("access") \
            else "   (anyone with access to this env)"
        rows.append((f"{e['name']} :{e['port']}", f"{e.get('url', '')}{gate}"))
    if not d.get("exposures") and d.get("url"):
        rows.append(("url", d["url"]))
    if d.get("ssh"):
        # Deliberately not a bare `ssh` command: the box only accepts a
        # certificate, and `cawl ssh` is what fetches one.
        rows.append(("ssh", f"cawl ssh {d['id']}   (host: {d['ssh']})"))
    rows.append(("expires", d["expires_at"] if d.get("expires_at")
                 else "never — remember to `cawl rm` it"))
    if d.get("error"):
        rows.append(("error", d["error"]))

    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        click.echo(f"  {label + ':':<{width + 1}}  {value}")

    if next_steps and d.get("status") == "ready":
        steps = _next_steps(d)
        pad = max(len(cmd) for cmd, _ in steps)
        click.echo()
        for cmd, why in steps:
            click.echo(f"  {cmd:<{pad}}  # {why}")


def _next_steps(d: dict) -> list[tuple[str, str]]:
    """What to actually do with the env you just got."""
    sid = d["id"]
    steps = []
    if d.get("ssh"):
        steps.append((f"cawl ssh {sid}", "shell in it — a certificate is fetched for you"))
        steps.append((f"cawl exec {sid} -- <cmd>", "run one command; its exit code is yours"))
    steps.append((f"cawl share {sid} --with <who>",
                  "let someone else in (they can't destroy it)"))
    steps.append((f"cawl expose {sid} <port>",
                  "give a port a URL, gated by sign-in"))
    steps.append((f"cawl rm {sid}", "destroy it — the TTL is only a backstop"))
    return steps


@click.group()
def main():
    """Ephemeral environments for whenever your team needs them (remote client)."""


@main.command()
@click.option("--json", "as_json", is_flag=True)
def whoami(as_json):
    """Show the identity and role of the current token."""
    with _guard(as_json):
        data = resolve_client().whoami()
    click.echo(jsonlib.dumps(data))


@main.command()
@click.argument("template")
@click.option("--name", default=None,
              help="Custom id for the env (default: generated, e.g. acme-cms-a1b2). "
                   "2-63 chars of a-z 0-9 '-'; must be free.")
@click.option("--arg", "args", multiple=True, metavar="KEY=VALUE",
              help="Template argument (repeatable). Which ones a template takes — "
                   "and what it does with them — is up to the template; see "
                   "`cawl template show <template>`. Not for secrets: args are stored "
                   "and shown in `cawl ls`.")
@click.option("--ttl", default=None, help="Override TTL, e.g. 4h (or 'none').")
@click.option("--backend", default=None,
              help="Which of the deployment's named backends materializes the "
                   "env (an Incus deployment tends to have 'vm' and "
                   "'container'). Default: the deployment's choice.")
@click.option("--reuse-if-exists", "reuse", is_flag=True,
              help="Return the existing env for (template,args,owner,purpose) — or for "
                   "--name, the env of that name — if present.")
@click.option("--owner", default=None, help="Admin only: create on behalf of another owner.")
@click.option("--json", "as_json", is_flag=True)
def up(template, name, args, ttl, backend, reuse, owner, as_json):
    """Bring up an environment (owned by your token's identity).

    The name is the handle for `exec`, `status` and `rm`, and is also the VM's
    hostname, so it's unique across everyone's envs. Names of destroyed envs are
    free to reuse.

    Example: cawl up acme-cms --name web-test --arg branch=feature/x
    """
    with _guard(as_json):
        data = resolve_client().up(
            template=template, name=name, args=_parse_args(args, as_json),
            ttl=ttl, backend=backend, reuse=reuse, owner=owner)
    _emit(as_json, data, next_steps=True)


@main.command()
@click.option("--template", default=None)
@click.option("--json", "as_json", is_flag=True)
def ls(template, as_json):
    """List environments (yours; admins see all)."""
    with _guard(as_json):
        rows = resolve_client().ls(template=template)
    if as_json:
        click.echo(jsonlib.dumps(rows))
        return
    if not rows:
        click.echo("no environments")
        return
    for r in rows:
        click.echo(f"{r['id']:32}  {r['owner']:14}  "
                   f"{r['status']:8}  {_fmt_args(r.get('args'))}")


@main.command()
@click.argument("id")
@click.option("--json", "as_json", is_flag=True)
def status(id, as_json):
    """Show one environment."""
    with _guard(as_json):
        data = resolve_client().status(id)
    _emit(as_json, data)


@main.command(context_settings={"ignore_unknown_options": True})
@click.argument("id")
@click.argument("cmd", nargs=-1, type=click.UNPROCESSED, required=True)
def exec(id, cmd):
    """Run a command inside an environment; exits with the inner exit code.

    Example: cawl exec acme-cms-a1b2 -- python manage.py test
    """
    with _guard(as_json=False):
        res = resolve_client().exec(id, list(cmd))
    if res.get("stdout"):
        click.echo(res["stdout"], nl=False)
    if res.get("stderr"):
        click.echo(res["stderr"], nl=False, err=True)
    sys.exit(res["exit_code"])


def _fetch_cert(client, sid: str):
    """Sign a certificate for one environment and write what ssh needs locally.

    Shared by `ssh`, `cert` and `ssh-config`: all three ask the daemon to sign,
    every time, so access always reflects the current grants. Raises ApiError if
    it declines (or if the box has no address yet).
    """
    from cawl import sshkeys
    key = sshkeys.ensure_key()
    access = client.ssh_cert(sid, sshkeys.public_key(key))
    cert = sshkeys.write_cert(sid, access["certificate"])
    known = sshkeys.write_known_hosts(access["host"], access["ca_pubkey"])
    return access, key, cert, known


def _cawl_command() -> str:
    """How to spell this CLI inside an ssh_config `exec`, which runs under a
    bare shell whose PATH may not be the one you log in with. Prefer the
    absolute path of the program being run; fall back to the bare name if it
    can't survive being embedded in a double-quoted ssh_config token.
    """
    exe = shutil.which("cawl")
    if not exe:
        # Running as `python -m cawl`: argv[0] is a module file, which the
        # shell can't run. Better a bare name that might miss than a path
        # that's certain to, since ssh reports neither — an exec that fails
        # just means the stanza quietly doesn't match.
        argv0 = str(Path(sys.argv[0]).resolve())
        exe = argv0 if os.access(argv0, os.X_OK) else "cawl"
    quoted = shlex.quote(exe)
    return "cawl" if '"' in quoted else quoted


@main.command(context_settings={"ignore_unknown_options": True})
@click.argument("id")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@click.option("--no-forward-agent", is_flag=True,
              help="Don't forward your SSH agent. Consider this on a shared env: "
                   "anyone else with access has sudo in the box, and so could use "
                   "your forwarded agent while you're connected.")
def ssh(id, command, no_forward_agent):
    """SSH into an environment (yours, or one shared with you).

    Fetches a certificate that is good for this one env for a few minutes, then
    hands off to ssh. Nothing durable is installed in the box, so access follows
    the grants in the daemon — losing a share means the next cert isn't signed.

    Example: cawl ssh web-test
             cawl ssh web-test -- python manage.py migrate
    """
    from cawl import sshkeys
    with _guard(as_json=False):
        access, key, cert, known = _fetch_cert(resolve_client(), id)
    argv = sshkeys.ssh_argv(
        host=access["host"], user=access["user"], key=key, cert=cert,
        known_hosts=known, forward_agent=not no_forward_agent,
        command=list(command) or None,
        jump=access.get("jump"),
    )
    os.execvp(argv[0], argv)  # become ssh: its exit code and tty are the user's


@main.command()
@click.argument("id")
@click.option("--json", "as_json", is_flag=True)
def cert(id, as_json):
    """Write a fresh certificate for an environment without connecting to it.

    This is what the stanzas from `cawl ssh-config` run as ssh dials, so the
    few-minute certificate is always newly signed by the time it's presented.
    Quiet when it works; a refusal to sign is a non-zero exit.

    Example: cawl cert acme-dev
    """
    with _guard(as_json):
        access, key, cert_path, known = _fetch_cert(resolve_client(), id)
    if as_json:
        click.echo(jsonlib.dumps({
            "id": id, "host": access["host"], "user": access["user"],
            "jump": access.get("jump"), "key": str(key),
            "certificate": str(cert_path), "known_hosts": str(known),
        }))


@main.command()
@click.argument("ids", nargs=-1)
def ssh_config(ids):
    """Print ssh_config stanzas for environments — every one you can see, or
    just the ones you name.

    This is how everything else that speaks SSH reaches a box: rsync and scp to
    move files, sshfs to mount one, an editor's Remote-SSH to work in it.

    \b
        cawl ssh-config > ~/.ssh/cawl.config

    then put `Include cawl.config` on the *first* line of ~/.ssh/config — in
    ssh_config the first value found wins, so it has to come before your own
    Host blocks. After that the environment's id is an ordinary SSH host:
    `ssh acme-dev`, `rsync -a ./src acme-dev:/srv`, Remote-SSH to `acme-dev`.
    Each stanza re-mints its own certificate, so there's still nothing durable
    installed in the box, and losing a share closes the door at the next dial.

    Re-run it when you make or destroy environments, and after a restart on
    deployments that hand out bridge addresses: the stanza pins the address the
    box had when it was written.
    """
    from cawl import sshkeys
    with _guard(as_json=False):
        client = resolve_client()
        if ids:
            targets = list(ids)
        else:
            # An admin's `ls` is the whole deployment, and a stanza for each
            # would have the daemon sign — and log — fifty certificates nobody
            # asked for. Naming nothing means the boxes you actually work in.
            me = client.whoami()["id"]
            targets = [e["id"] for e in client.ls()
                       if e.get("ssh") and (e["owner"] == me
                                            or me in (e.get("shared_with") or []))]

    blocks, skipped = [], []
    for sid in targets:
        try:
            access, key, cert_path, known = _fetch_cert(client, sid)
        except ApiError as e:
            skipped.append((sid, e.message))  # stopped, or not ours any more
            continue
        blocks.append(sshkeys.ssh_config_block(
            alias=sid, host=access["host"], user=access["user"], key=key,
            cert=cert_path, known_hosts=known, jump=access.get("jump"),
            refresh=f"{_cawl_command()} cert {sid}",
        ))

    for sid, why in skipped:
        click.echo(f"# skipped {sid}: {why}", err=True)
    if not blocks:
        _fail(False, "no environments to write a config for"
                     " — `cawl up` one first, or check `cawl ls`")
    click.echo("# Generated by `cawl ssh-config`. Regenerate after `cawl up`,"
               "\n# `cawl rm`, or a restart. Each stanza signs its own cert.\n")
    click.echo("\n".join(blocks), nl=False)


@main.command()
@click.argument("id")
@click.option("--with", "principal", required=True, metavar="PRINCIPAL",
              help="The principal to share with (their `cawl whoami` id).")
@click.option("--json", "as_json", is_flag=True)
def share(id, principal, as_json):
    """Give someone else access to your environment.

    They can see it, `exec` in it and `ssh` to it — but not destroy it or change
    its TTL. Takes effect immediately; the environment itself is never touched.
    """
    with _guard(as_json):
        data = resolve_client().share(id, principal)
    if as_json:
        click.echo(jsonlib.dumps(data))
    else:
        click.echo(f"{id} shared with {principal}")


@main.command()
@click.argument("id")
@click.option("--from", "principal", required=True, metavar="PRINCIPAL",
              help="The principal to revoke.")
@click.option("--json", "as_json", is_flag=True)
def unshare(id, principal, as_json):
    """Revoke someone's access to your environment.

    They can't get a new certificate, so they can't open a new session. A session
    they already have runs until it ends — close it now with
    `cawl exec <id> -- pkill -u dev` if that matters.
    """
    with _guard(as_json):
        data = resolve_client().unshare(id, principal)
    if as_json:
        click.echo(jsonlib.dumps(data))
    else:
        click.echo(f"{id} no longer shared with {principal}")


@main.command()
@click.argument("id")
@click.argument("port", type=int)
@click.option("--name", default=None,
              help="Hostname label: <name>.<domain>. Any label not already in "
                   "use (default: the env's id).")
@click.option("--access", default=None, metavar="EMAILS",
              help="Comma-separated emails allowed to view it (magic-link "
                   "sign-in). Omit for: anyone with access to the env.")
@click.option("--json", "as_json", is_flag=True)
def expose(id, port, name, access, as_json):
    """Give a port of an environment its own URL, behind sign-in.

    The URL is https://<name>.<domain> — any free name (it's a shared
    namespace; taken names are refused). Every exposure is gated: without
    --access, whoever can use the env (you, grantees, admins) can view it in a
    browser; with --access, those email addresses can too. A sign-in link per
    address is printed — send it on, or they can request one themselves at the
    URL. Re-run to update in place; revoke someone by re-running without their
    address.

    Example: cawl expose web-test 6006 --name acme-storybook --access sue@client.com
    """
    emails = [e.strip() for e in (access or "").split(",") if e.strip()]
    with _guard(as_json):
        data = resolve_client().expose(id, port, name=name, access=emails)
    if as_json:
        click.echo(jsonlib.dumps(data))
        return
    click.echo(f"{data['url']}  ->  :{data['port']}")
    for email, link in (data.get("links") or {}).items():
        click.echo(f"  sign-in link for {email}:\n    {link}")


@main.command()
@click.argument("id")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True)
def unexpose(id, name, as_json):
    """Take an exposure down. The URL stops resolving to the env immediately."""
    with _guard(as_json):
        data = resolve_client().unexpose(id, name)
    if as_json:
        click.echo(jsonlib.dumps(data))
    else:
        click.echo(f"unexposed {name} on {id}")


@main.command()
@click.argument("id")
@click.option("--json", "as_json", is_flag=True)
def stop(id, as_json):
    """Shut an environment down without destroying it.

    Frees its RAM; the disk (and everything on it) stays. `cawl start` brings it
    back with the same id, name, URL and SSH access. A stopped env still counts
    against your quota, and its TTL keeps running — the reaper will still collect
    it when it expires, so this is a pause, not a preservation order.
    """
    with _guard(as_json):
        data = resolve_client().stop(id)
    _emit(as_json, data)


@main.command()
@click.argument("id")
@click.option("--json", "as_json", is_flag=True)
def start(id, as_json):
    """Bring a stopped environment back up.

    Rejoins the network and re-runs the template's provision hook, so the app
    comes back the way it came up. Your SSH access is unchanged.
    """
    with _guard(as_json):
        data = resolve_client().start(id)
    _emit(as_json, data, next_steps=True)


@main.command()
@click.argument("id")
@click.option("--json", "as_json", is_flag=True)
def rm(id, as_json):
    """Destroy an environment (owner only)."""
    with _guard(as_json):
        data = resolve_client().rm(id)
    click.echo(jsonlib.dumps(data) if as_json else f"destroyed {data.get('destroyed', id)}")


@main.command(name="refresh-image")
@click.argument("template")
@click.option("--arg", "args", multiple=True, metavar="KEY=VALUE",
              help="Template argument for the build hook (repeatable).")
@click.option("--backend", default=None,
              help="Bake for this named backend (images are per-backend).")
@click.option("--json", "as_json", is_flag=True)
def refresh_image(template, args, backend, as_json):
    """Rebuild a template's golden image by re-running its build hook (admin only)."""
    with _guard(as_json):
        data = resolve_client().refresh_image(template, _parse_args(args, as_json),
                                              backend=backend)
    click.echo(jsonlib.dumps(data) if as_json else f"built {data['image']}")


@main.command()
@click.option("--api-url", default=None, help="Daemon URL (or set CAWL_API_URL).")
@click.option("--token", default=None,
              help="Log in with a token directly, skipping the browser.")
@click.option("--headless", is_flag=True,
              help="No same-machine browser (e.g. over SSH): authorize on any "
                   "device and paste the code. Use this on a remote VM.")
@click.option("--no-browser", is_flag=True, help="Print the URL; don't auto-open.")
def login(api_url, token, headless, no_browser):
    """Log in and store credentials.

    Default: browser loopback flow (browser and CLI on the same machine).
    --headless: open the URL on any device, paste back the code (for remote VMs).
    --token: supply a token directly.
    """
    api_url = api_url or os.environ.get("CAWL_API_URL")
    if not api_url:
        api_url = click.prompt("cawl API URL (e.g. https://cawl.example.com)")
    if token:
        pass
    elif headless:
        url = api_url.rstrip("/") + "/cli/login"
        click.echo(f"On any device, open this URL and authorize:\n  {url}\n")
        token = click.prompt("Paste the code shown").strip()
    else:
        from cawl.login import browser_login
        try:
            token = browser_login(api_url, open_browser=not no_browser)
        except Exception as e:  # noqa: BLE001
            _fail(False, f"login failed: {e}")
    try:
        who = ApiClient(api_url, token).whoami()
    except ApiError as e:
        _fail(False, f"token rejected: {e.message}")
    credentials.save(api_url, token)
    click.echo(f"Logged in as {who['id']} ({who['role']}). "
               f"Credentials saved to {credentials.path()}.")


@main.command()
def logout():
    """Remove stored credentials."""
    click.echo("Logged out." if credentials.clear() else "Not logged in.")


@main.group()
def template():
    """Manage personal templates and admin-published global templates."""


@template.command(name="create")
@click.option("--file", "-f", "path", type=click.Path(exists=True),
              help="Read the template.yaml from a file instead of stdin.")
@click.option("--global", "global_template", is_flag=True,
              help="Publish for every user (admin only).")
@click.option("--json", "as_json", is_flag=True)
def template_create(path, global_template, as_json):
    """Upload a personal template (re-uploading it creates a new version).

    Use --global to publish for every user (admin only). Admins without an
    explicit scope keep the legacy behavior of creating global templates.
    """
    text = Path(path).read_text() if path else sys.stdin.read()
    with _guard(as_json):
        data = resolve_client().template_create(
            text, scope="global" if global_template else None)
    click.echo(jsonlib.dumps(data) if as_json
               else f"saved template {data['name']} (v{data['version']})")


@template.command(name="ls")
@click.option("--json", "as_json", is_flag=True)
def template_ls(as_json):
    """List registered templates."""
    with _guard(as_json):
        rows = resolve_client().templates()
    if as_json:
        click.echo(jsonlib.dumps(rows))
        return
    if not rows:
        click.echo("no templates")
        return
    for r in rows:
        flag = "" if r["active"] else "  (inactive)"
        click.echo(f"{r['name']:24}  v{r['version']:<4}  "
                   f"args: {r['params'] or '-'}{flag}")


@template.command(name="show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True)
def template_show(name, as_json):
    """Print a template's stored template.yaml."""
    with _guard(as_json):
        data = resolve_client().template_show(name)
    if as_json:
        click.echo(jsonlib.dumps(data))
        return
    click.echo(data.get("yaml", ""), nl=False)


@template.command(name="rm")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True)
def template_rm(name, as_json):
    """Deactivate a template (hidden from `up`; existing envs unaffected)."""
    with _guard(as_json):
        data = resolve_client().template_rm(name)
    click.echo(jsonlib.dumps(data) if as_json
               else f"deactivated {data.get('deactivated', name)}")


if __name__ == "__main__":
    main()
