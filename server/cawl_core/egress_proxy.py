"""Private source-policy HTTPS CONNECT proxy for cawl workload networks."""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
from pathlib import Path
import socket

from cawl_core.egress import normalize_host

LOG = logging.getLogger("cawl.egress")
host = normalize_host


def authority(value: str) -> tuple[str, int]:
    if value.count(":") != 1:
        raise ValueError("destination must be hostname:443")
    name, port = value.rsplit(":", 1)
    if port != "443":
        raise ValueError("only port 443 is allowed")
    return host(name), 443


class SourcePolicyStore:
    """Read a daemon-written document atomically, retaining the last valid map."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._stamp = None
        self._policies: dict[ipaddress._BaseNetwork, frozenset[str]] = {}

    def _reload(self) -> None:
        try:
            stamp = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return                         # empty/last valid is safest
        if stamp == self._stamp:
            return
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            policies = {}
            for cidr, entry in raw.items():
                net = ipaddress.ip_network(cidr, strict=True)
                if net.prefixlen != net.max_prefixlen or not isinstance(entry, dict):
                    raise ValueError("invalid source policy")
                env, hosts = entry.get("environment_id"), entry.get("hosts")
                if not isinstance(env, str) or not env or not isinstance(hosts, list):
                    raise ValueError("invalid source policy")
                policies[net] = frozenset(host(h) for h in hosts)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            LOG.error("retaining last valid egress policy: %s", exc)
            return
        self._policies, self._stamp = policies, stamp

    def hosts_for(self, source: str) -> frozenset[str] | None:
        self._reload()
        try:
            ip = ipaddress.ip_address(source)
        except ValueError:
            return None
        for network, hosts in self._policies.items():
            if ip in network:
                return hosts
        return None


class Proxy:
    def __init__(self, policy_store: SourcePolicyStore, private_cidrs: list[str] = []):
        self.policy_store = policy_store
        self.private_networks = tuple(ipaddress.ip_network(c, strict=True) for c in private_cidrs)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        source = peer[0] if peer else "unknown"
        upstream = None
        try:
            line = await asyncio.wait_for(reader.readuntil(b"\r\n"), 10)
            method, target, version = line[:-2].decode("ascii").split(" ")
            if method != "CONNECT" or version != "HTTP/1.1":
                raise ValueError("only HTTP/1.1 CONNECT is supported")
            name, port = authority(target)
            await self._headers(reader)
            allowed = self.policy_store.hosts_for(source)
            if allowed is None:
                raise ValueError("source is not registered")
            if name not in allowed:
                raise ValueError("destination is not allowed")
            upstream_reader, upstream = await self._connect(name, port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            LOG.info("allow source=%s host=%s", source, name)
            await self._relay(reader, writer, upstream_reader, upstream)
            return
        except (ValueError, UnicodeDecodeError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, asyncio.TimeoutError, OSError) as exc:
            LOG.info("deny source=%s reason=%s", source, exc)
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            try: await writer.drain()
            except ConnectionError: pass
        finally:
            if upstream:
                upstream.close()
                await upstream.wait_closed()
            writer.close()
            await writer.wait_closed()

    async def _headers(self, reader):
        size = 0
        while True:
            line = await asyncio.wait_for(reader.readuntil(b"\r\n"), 10)
            size += len(line)
            if size > 32 * 1024: raise ValueError("headers too large")
            if line == b"\r\n": return

    async def _connect(self, name, port):
        infos = await asyncio.get_running_loop().getaddrinfo(name, port, type=socket.SOCK_STREAM)
        infos = [i for i in infos if ipaddress.ip_address(i[4][0]).is_global or
                 any(ipaddress.ip_address(i[4][0]) in n for n in self.private_networks)]
        if not infos: raise ValueError("hostname has no public address")
        last = None
        for family, _, _, _, address in infos:
            try:
                return await asyncio.wait_for(asyncio.open_connection(address[0], address[1], family=family), 10)
            except OSError as exc: last = exc
        raise OSError(str(last))

    async def _relay(self, a, aw, b, bw):
        async def copy(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data); await writer.drain()
            finally:
                if writer.can_write_eof(): writer.write_eof()
        tasks = [asyncio.create_task(copy(a, bw)), asyncio.create_task(copy(b, aw))]
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main_async(args):
    bind, port = args.listen.rsplit(":", 1)
    server = await asyncio.start_server(
        Proxy(SourcePolicyStore(args.policy_store), args.allow_private_cidr).handle, bind, int(port))
    async with server: await server.serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", required=True)
    parser.add_argument("--policy-store", required=True,
                        help="atomically-written cawl source-policy JSON document")
    parser.add_argument("--allow-private-cidr", action="append", default=[])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main_async(args))

if __name__ == "__main__": main()
