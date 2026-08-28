# What cawl is

cawl is a tool for letting your team run short-lived environments for their work.

Each environment is an isolated VM running on a server you manage.

It's designed around three use case:

**Dev boxes** Somewhere to work on stuff that you can own. Run your docker
compose stack, SSH to it, rsync data to it, or share it with teammates when you
need someone else to take a look.

**Review apps** Run a branch or just a quick experiment on the server and
expose a URL so others can see it, as long as they have access.

**Agent sandboxes** Safely run AI agents in their own limited environment,
so they can build without breaking stuff.

## The parts

cawl is made up of three parts

- **Incus** is the underlying virtualization tool that cawl uses to run the VMs.
- **The server** is a daemon that manages the machines, plus some other
  software like Traefik for exposing public URLs. This can live on the same machine as Incus,
  or on a separate one.
- **The CLI** is the `cawl` command that devs install on their machine.
  It let's you talk to the server and run things on it.

If you're setting up cawl for the first time, see [Setting It Up](setup.md).

If your team already has a cawl server running, see [Usage](usage.md).

If you're an administrator looking to manage the server, see [Administration](administration.md).
