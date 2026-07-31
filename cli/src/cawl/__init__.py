"""cawl — remote CLI + client for the environment control-plane daemon.

A thin, stateless client: `cawl.client` is a stdlib HTTP client for the daemon's
API, and `cawl.cli` is the command surface. Configure with CAWL_API_URL +
CAWL_TOKEN. The domain library lives separately in the server (cawl_core).
"""

__version__ = "0.1.0"
