"""Runtime backends: the swappable boundary between the control plane and compute.

- IncusApiRuntime (incus_api) — talks the Incus REST API directly.
- FakeRuntime     (fake)      — in-memory, for tests + local development.
"""

from cawl_core.runtime.base import ExecResult, InstanceInfo, InstanceSpec, Runtime

__all__ = ["Runtime", "InstanceSpec", "InstanceInfo", "ExecResult"]
