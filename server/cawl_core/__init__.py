"""cawl_core — the domain library behind the control-plane daemon.

Config, models, the control plane, runtime backends (Incus API/CLI, fake),
ingress, and the authorization policy. The Django daemon (cawlserver +
environments) is its consumer; the `cawl` CLI is a separate remote client and does
not import this.
"""

__version__ = "0.1.0"
