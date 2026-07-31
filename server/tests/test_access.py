import unittest

from cawl_core.access import BridgeAccess, JumpAccess, TailscaleAccess


class TestTailscaleAccess(unittest.TestCase):
    def setUp(self):
        self.access = TailscaleAccess("tskey-abc", tailnet="tail1.ts.net",
                                      tags="tag:cawl")

    def test_boot_script_joins_the_tailnet_as_this_box(self):
        script = self.access.boot_script("web-test")
        self.assertIn("tailscale up", script)
        self.assertIn("tskey-abc", script)
        self.assertIn("--hostname=web-test", script)
        self.assertIn("--advertise-tags=tag:cawl", script)

    def test_the_tailnet_is_transport_only_never_tailscale_ssh(self):
        """--ssh would hand authentication to tailnet identity, which knows
        nothing about who owns an environment. sshd + the CA decide instead."""
        self.assertNotIn("--ssh", self.access.boot_script("web-test"))

    def test_no_tags_means_no_advertise_flag(self):
        access = TailscaleAccess("tskey-abc", tailnet="tail1.ts.net")
        self.assertNotIn("--advertise-tags", access.boot_script("web-test"))

    def test_ssh_host_is_the_magicdns_name_not_the_ip(self):
        # The name survives stop/start and IP moves — and exists even while the
        # box is down, so `cawl ssh` output stays stable.
        self.assertEqual(self.access.ssh_host("web-test", "10.0.0.5"),
                         "web-test.tail1.ts.net")
        self.assertEqual(self.access.ssh_host("web-test", None),
                         "web-test.tail1.ts.net")


class TestBridgeAccess(unittest.TestCase):
    def test_no_agent_in_the_box(self):
        # Routing to the bridge is the operator's business (LAN, their own
        # VPN); cawl runs nothing in the box for it.
        self.assertEqual(BridgeAccess().boot_script("web-test"), "")

    def test_ssh_host_is_the_bridge_ip(self):
        self.assertEqual(BridgeAccess().ssh_host("web-test", "10.0.0.5"),
                         "10.0.0.5")

    def test_a_stopped_box_has_no_address(self):
        self.assertIsNone(BridgeAccess().ssh_host("web-test", None))

    def test_direct_providers_need_no_jump(self):
        self.assertIsNone(BridgeAccess().ssh_jump("web-test"))
        self.assertIsNone(
            TailscaleAccess("tskey-abc", tailnet="t.ts.net").ssh_jump("web-test"))


class TestJumpAccess(unittest.TestCase):
    """Bridge access relayed through a hop developers can already SSH to —
    typically the daemon host. The hop is transport; the box's CA-only sshd
    is still the door."""

    def setUp(self):
        self.access = JumpAccess("ops@jump.example.com")

    def test_dials_the_bridge_ip_via_the_jump(self):
        self.assertEqual(self.access.ssh_host("web-test", "10.0.0.5"), "10.0.0.5")
        self.assertEqual(self.access.ssh_jump("web-test"), "ops@jump.example.com")

    def test_nothing_runs_in_the_box(self):
        self.assertEqual(self.access.boot_script("web-test"), "")

    def test_a_stopped_box_still_has_no_address(self):
        # The jump can't reach a box that isn't there.
        self.assertIsNone(self.access.ssh_host("web-test", None))


if __name__ == "__main__":
    unittest.main()
