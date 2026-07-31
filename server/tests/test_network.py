import unittest

from cawl_core.runtime.incus_api import _first_ipv4


def row(network):
    return {"state": {"network": network}}


def iface(addr, scope="global"):
    return {"addresses": [{"family": "inet", "scope": scope, "address": addr}]}


class TestFirstIpv4(unittest.TestCase):
    def test_prefers_eth0_over_docker0(self):
        net = {
            "lo": iface("127.0.0.1", scope="local"),
            "docker0": iface("172.17.0.1"),   # appears once Docker is up
            "eth0": iface("10.157.230.5"),
        }
        self.assertEqual(_first_ipv4(row(net)), "10.157.230.5")

    def test_vm_primary_nic(self):
        net = {"docker0": iface("172.17.0.1"), "enp5s0": iface("10.0.0.9")}
        self.assertEqual(_first_ipv4(row(net)), "10.0.0.9")

    def test_skips_docker_and_bridges(self):
        net = {
            "lo": iface("127.0.0.1", scope="local"),
            "docker0": iface("172.17.0.1"),
            "br-abc": iface("172.18.0.1"),
            "vethXYZ": iface("172.19.0.1"),
        }
        self.assertIsNone(_first_ipv4(row(net)))  # nothing routable for ingress

    def test_none_when_empty(self):
        self.assertIsNone(_first_ipv4(row({})))


if __name__ == "__main__":
    unittest.main()
