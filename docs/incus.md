# Using Incus for cawl VMs

This guide sets up **Incus**, the software cawl uses to create and delete its
virtual machines. Start here if this is your first Incus installation. It
covers one Linux host first; clustering is an optional final section.

Incus runs on the machine that has the CPU, memory, and disk for environments.
The cawl server asks Incus to create a VM from an image, runs the template hook
inside it, and later deletes it. Incus—not cawl—does the actual virtualization.

## What you need

Use a Linux host with:

- hardware virtualization available (`/dev/kvm`); nested virtualization must
  be enabled if this host is itself a VM;
- enough RAM and disk for the environments you expect to run;
- network access from the cawl daemon to the Incus API and to VM addresses.

The examples use Ubuntu 26.04, with a user running as root. Incus packaging varies by distribution; use the [Incus installation documentation](https://linuxcontainers.org/incus/docs/main/installing/) when your platform differs.

## 1. Install and initialise Incus

## 2. Let the cawl daemon use the Incus API

The daemon uses Incus's HTTPS API with its own client certificate. On the
Incus host, create the certificate and trust it:

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:secp384r1 -nodes \
  -keyout client.key -out client.crt -days 3650 -subj "/CN=cawl-daemon"
sudo incus config trust add-certificate client.crt --name cawl-daemon

sudo install -d -m755 /etc/cawl/incus
sudo install -m600 client.key /etc/cawl/incus/client.key
sudo install -m644 client.crt /etc/cawl/incus/client.crt
sudo cp /var/lib/incus/server.crt /etc/cawl/incus/server.crt
```

The daemon configuration then points at Incus:

```ini
CAWL_RUNTIME=incus_api
CAWL_DEFAULT_BACKEND=vm
CAWL_INCUS_URL=https://127.0.0.1:8443
CAWL_INCUS_CLIENT_CERT=/etc/cawl/incus/client.crt
CAWL_INCUS_CLIENT_KEY=/etc/cawl/incus/client.key
CAWL_INCUS_SERVER_CERT=/etc/cawl/incus/server.crt
```

If the daemon runs on another machine, replace `127.0.0.1` with the Incus
host's reachable API address and restrict that API address with a firewall.
The certificate private key and the Incus API are both administrator-level
access; do not put either in an environment VM.

## 3. Build cawl's base VM image

cawl needs a base image containing Docker, Git, SSH, and the `dev` user. From
the cawl checkout, run:

```bash
cd server/deploy
sudo ./build-base-image.sh --vm
```

The script temporarily creates a builder VM, installs the tools, then publishes
an image named `cawl/base-vm`. Check it exists:

```bash
sudo incus image list cawl/base-vm
```

cawl builds template-specific images from this base image when you run
`cawl refresh-image <template>`.

## 4. Give agent VMs controlled outbound access (recommended)

Agent code is untrusted. Do not give its VM network a general Internet NAT
route. Instead, create a separate Incus network and allow VMs on it to reach
only cawl's private HTTPS proxy.

Create the network:

```bash
sudo incus network create cawl-agent \
  ipv4.address=10.42.0.1/24 ipv4.nat=false ipv6.address=none
```

### Allow a hostname at the proxy

The VM network ACL only allows traffic to the proxy. cawl registers each VM's
source address and server-selected policy; the proxy has no global allow-host
flag. Start the proxy with its root-owned policy document:

```bash
sudo install -m644 server/deploy/cawl-egress-proxy.service /etc/systemd/system/
sudo tee /etc/cawl/egress-proxy.env >/dev/null <<'EOF'
CAWL_EGRESS_PROXY_ARGS=--listen 10.42.0.1:3128 --policy-store /var/lib/cawl/egress-policies.json
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now cawl-egress-proxy
```

Set `CAWL_EGRESS_ALLOWED_HOSTS=example.com,github.com` and
`CAWL_EGRESS_POLICY_STORE=/var/lib/cawl/egress-policies.json` on the daemon.
Names are exact, normalized DNS names; no suffixes, wildcards, or IP literals.

Next, create and attach an Incus network ACL. An ACL is a named set of
network rules; applying it to `cawl-agent` means every VM that cawl attaches
to that network receives the same restrictions.

```bash
sudo incus network acl create cawl-agent-egress
sudo incus network acl edit cawl-agent-egress
```

The second command opens an editor. Replace its contents with the following,
substituting the CIDRs that can legitimately reach the VM. `TRAEFIK_CIDR` is
the address/range Traefik uses to connect to VM application ports;
`SSH_CLIENT_CIDR` is your tailnet, jump-host, or LAN range. If you do not use
one of those access paths, omit its allow rule.

```yaml
config: {}
description: cawl agent VMs may use only the private egress proxy
egress:
  # The one permitted outbound connection. Proxy replies are part of this
  # tracked connection and are allowed automatically.
  - action: allow
    destination: 10.42.0.1/32
    destination_port: "3128"
    protocol: tcp
  # Everything else, including direct DNS and Internet access, is blocked.
  - action: drop
ingress:
  # Replace these example CIDRs and ports for your deployment.
  - action: allow
    source: TRAEFIK_CIDR
    destination_port: "8000"
    protocol: tcp
  - action: allow
    source: SSH_CLIENT_CIDR
    destination_port: "22"
    protocol: tcp
  - action: drop
name: cawl-agent-egress
```

For example, if Traefik runs on this host it may use `10.42.0.1/32`; if a jump
host uses `10.0.5.10`, use `10.0.5.10/32`. Do not leave the placeholder values
in the file. Add further inbound application ports only when a template
actually exposes them.

Attach the ACL to the managed network and enable source-address filtering,
then confirm all three settings are present:

```bash
sudo incus network set cawl-agent security.acls=cawl-agent-egress
sudo incus network set cawl-agent security.ipv4_filtering=true
sudo incus network set cawl-agent security.ipv6_filtering=true
sudo incus network show cawl-agent
```

Both filtering settings are mandatory with `CAWL_EGRESS=proxy`: the proxy
selects policy from the TCP source address, so a guest must not be able to
spoof another environment's address. If the network or NIC type cannot enforce
source filtering, do not use the proxy egress mode on that network.

If your Incus version or network type does not support a network-level ACL,
attach the same ACL to the instance NIC instead:

```bash
sudo incus config device set <instance-name> eth0 security.acls=cawl-agent-egress
```

The network-level attachment is preferred because cawl creates new VMs
continuously and they inherit it automatically.

Configure cawl to attach new VMs to this network and tell compatible tools
where the proxy is:

```ini
CAWL_EGRESS=proxy
CAWL_EGRESS_NETWORK=cawl-agent
CAWL_EGRESS_PROXY_URL=http://10.42.0.1:3128
```

`HTTP_PROXY` and `HTTPS_PROXY` inside a VM are not the protection; an agent can
remove them. The network rule is the protection: without the proxy it has no
path out. The proxy resolves destination names and only permits its configured
hostnames.

## 5. Test before enabling agents

Test the complete cawl path, rather than creating a VM directly with Incus.
Templates are not registered automatically: cawl stores them in its database,
so register the bundled blank `scratch` template once as an administrator.
Your shell also needs `CAWL_API_URL` and an administrator `CAWL_TOKEN` for the
registration step (a normal token may be used afterwards):

```bash
cawl template create < examples/scratch/template.yaml

id=$(cawl up scratch --json | jq -r .id)
trap 'cawl rm "$id"' EXIT
cawl status "$id" --json
```

Run the checks inside that cawl-created VM. The first request uses the exact
hostname currently allowlisted at the proxy.

```bash
cawl ssh "$id" -- sh -lc '
  curl --fail https://example.com/

  # Bypass the proxy deliberately: this must fail.
  if curl --connect-timeout 5 --noproxy "*" https://example.com/; then
    echo "unexpected direct egress" >&2; exit 1
  fi

  # Reach the proxy but request a hostname absent from the allowlist: fail.
  if curl --connect-timeout 5 https://www.iana.org; then
    echo "unexpected proxy allow" >&2; exit 1
  fi
'
```

The first request must succeed and the two negative checks must fail. The
`trap` removes the environment when the shell exits; remove it explicitly with
`cawl rm "$id"` if you interrupt the test.

You can inspect the underlying Incus device only as a diagnostic:

```bash
sudo incus config show "$id" --expanded
```

Its `eth0` device should use `cawl-agent` when egress is enabled.

## Adding Incus hosts later

You do not need a cluster for a first cawl installation. Add one only when one
host no longer has enough capacity or availability.

When you do create an Incus cluster, cawl still talks to the cluster controller
API; Incus chooses the member for each VM. Use a cluster-wide OVN or routed
network for `cawl-agent`, with unique VM addresses reachable by central Traefik
and the cawl daemon. Use a stable proxy VIP/load-balancer address in
`CAWL_EGRESS_PROXY_URL`.

Do not reuse the same local bridge subnet independently on every member if
Traefik or cawl is central: it cannot route uniquely to overlapping VM
addresses.

For each new cluster member:

1. install Incus and join it using the Incus clustering join flow;
2. provide equivalent storage, KVM support, and the cawl base image;
3. extend the cluster workload network to the member;
4. allow the new workload subnet to reach the shared proxy on TCP/3128;
5. create and test a disposable VM before allowing production placement.

No cawl configuration change is needed if the cluster keeps the same
`cawl-agent` network name and stable proxy endpoint.
