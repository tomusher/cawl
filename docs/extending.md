# Extending cawl

cawl has three extension points an administrator can build on, in increasing
order of ambition: **access providers** (how developers' machines reach a box's
sshd), **backends** (what a box *is*: an Incus VM, a container, or something
else entirely), and the **control plane** itself. The first two are
installable: write a class, include it in the control-plane image, and name it
in Compose's `.env`. The third is a library you embed.

A rule that holds everywhere: extensions plug in at the daemon, by the
operator. Templates never choose networking or compute. A template describes
an app, and anything an untrusted template hook could reach is something an
untrusted template hook *will* reach.

## Installing code

"Install" means ordinary Python packaging: the class just has to be importable
by the daemon. Either of these works:

```bash
# Add a package to the control-plane image, then rebuild and publish it.
# Or include my_backends.py somewhere on the image's PYTHONPATH.
```

Classes named by dotted path are constructed **with no arguments** and read
whatever settings they need from the environment themselves (their own
`MYCO_*` env vars, say), since cawl can't know what a third-party backend's
endpoint or credentials look like. Misnaming a class, or naming one of the
wrong kind, is a startup error, never a silent fallback.

## A custom access provider

Subclass `AccessProvider` (`server/cawl_core/access.py`). The contract is
three questions, two of them optional:

```python
# my_access.py — a provider for an operator-run NetBird network
import os
from cawl_core.access import AccessProvider

class NetBirdAccess(AccessProvider):
    def boot_script(self, environment_id):
        """Shell run in the box on *every* boot (join scripts rarely survive
        a shutdown). Rendered daemon-side: credentials in it never appear in
        a template. Return "" if no agent runs in the box."""
        key = os.environ["MYCO_NETBIRD_SETUP_KEY"]
        return f"netbird up --setup-key {key} --hostname {environment_id}"

    def ssh_host(self, environment_id, ip):
        """What clients dial — also the name the box's host certificate is
        signed for. Return None when the box has no reachable address."""
        return f"{environment_id}.netbird.selfhosted"

    def ssh_jump(self, environment_id):
        """Optional: a [user@]host[:port] hop to relay through (see the
        built-in JumpAccess). None = dial ssh_host directly."""
        return None
```

Enable it:

```bash
CAWL_ACCESS=my_access.NetBirdAccess
```

Whatever the transport, authentication is untouched: the box's sshd accepts
only certificates signed by the daemon's CA, so a provider decides how packets
travel, never who gets in.

## A custom backend

Subclass `Runtime` (`server/cawl_core/runtime/base.py`), the interface that
makes Incus, a fake, and anything else interchangeable. Eight methods:
`image_name`, `build_image`, `create`, `stop`, `start`, `destroy`, `exec`,
`info`. `FakeRuntime` (`runtime/fake.py`) is the reference implementation and
`test_incus_api.py` pins the expected orderings.

The signatures don't capture the whole contract. What the control plane
additionally relies on:

- **`info().ip` is load-bearing.** Traefik proxies exposures to it and the
  bridge/jump access providers dial it, so it must be an address the daemon
  host can route to. A cloud backend returning an unreachable internal IP
  will pass every unit test and fail in production.
- **`exec` is the out-of-band channel.** CA trust, host-cert install, and all
  hooks travel through it, and it must work with no SSH and no access
  provider, because it's what bootstraps both. Pass through the real exit
  code.
- **`create` splits provisioning in two**: on-disk things once (seeded keys,
  CA trust), then the every-boot things (`network_boot`, the Docker wait,
  `provision`), in that order.
- **`start` replays the every-boot half.** That's why it takes a spec, not an
  id: network membership and the compose stack don't survive a shutdown.
- Hooks arrive **already rendered and shell-quoted**; run them as-is and
  never parse them. A backend that knows what git or compose are is doing it
  wrong.

Backends are *named entries in a registry*, so a custom one installs alongside
the built-ins rather than replacing them:

```bash
# keep the Incus pair ("container", "vm"), add one of yours:
CAWL_EXTRA_BACKENDS=firecracker=my_backends.FirecrackerRuntime

# or run nothing but your own:
CAWL_RUNTIME=none
CAWL_EXTRA_BACKENDS=ec2=cawl_ec2.Ec2Runtime
CAWL_DEFAULT_BACKEND=ec2
```

Users pick one per environment with `cawl up --backend firecracker`; unnamed
requests land on `CAWL_DEFAULT_BACKEND`. Images are built *by* a backend *for*
that backend (`cawl refresh-image --backend …`), so each entry keeps its own
image lineage.

## Your own control plane

The deepest cut needs no hook at all: `cawl_core` is a plain Python library
with zero Django in it. The Django daemon is just one consumer that wires it
to settings in `environments/services.py`; the CLI's local mode is another.
Embed it in your own service and do the wiring yourself:

```python
from cawl_core.control import ControlPlane

plane = ControlPlane(
    state=YourStateStore(),          # the StateStore interface
    runtime={"vm": YourRuntime()},   # named backends, as above
    ingress=your_ingress,
    access=YourAccessProvider(),
    ca=your_ca,
)
```

This is deliberately a library boundary and not a `CAWL_CONTROL_PLANE=`
setting: `ControlPlane` is the authorization boundary (every owner/grant check
lives in it), and swapping the policy engine should be visible in code you
own, not a line in an env file.
