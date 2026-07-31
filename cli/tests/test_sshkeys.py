import os
import tempfile
import unittest
from pathlib import Path

from cawl import sshkeys


class TestSshKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CAWL_CONFIG_DIR"] = self.tmp.name
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        os.environ.pop("CAWL_CONFIG_DIR", None)
        self.tmp.cleanup()

    def test_key_is_generated_once_and_reused(self):
        key = sshkeys.ensure_key()
        self.assertTrue(key.exists())
        self.assertIn("ssh-ed25519", sshkeys.public_key(key))
        before = key.read_bytes()
        self.assertEqual(sshkeys.ensure_key().read_bytes(), before)

    def test_known_hosts_pins_the_ca_for_the_tailnet(self):
        """Trusting the CA — not whatever key answers — is what lets a fresh
        sandbox (or a reused name) connect without a host-key warning."""
        p = sshkeys.write_known_hosts("web-test.tail1.ts.net", "ssh-ed25519 AAAAC3 ca")
        self.assertEqual(p.read_text(),
                         "@cert-authority *.tail1.ts.net ssh-ed25519 AAAAC3 ca\n")

    def test_rotating_the_ca_replaces_the_old_line(self):
        sshkeys.write_known_hosts("a.tail1.ts.net", "ssh-ed25519 OLD ca")
        p = sshkeys.write_known_hosts("b.tail1.ts.net", "ssh-ed25519 NEW ca")
        self.assertEqual(p.read_text().count("@cert-authority"), 1)
        self.assertIn("NEW", p.read_text())

    def test_a_second_tailnet_gets_its_own_line(self):
        sshkeys.write_known_hosts("a.tail1.ts.net", "ssh-ed25519 AAA ca")
        p = sshkeys.write_known_hosts("b.other.ts.net", "ssh-ed25519 BBB ca")
        self.assertEqual(p.read_text().count("@cert-authority"), 2)

    def test_an_ip_host_is_pinned_exactly_not_wildcarded(self):
        """Bridge-access deployments dial the box's IP. Carving the first octet
        off for a wildcard would trust the CA for unrelated hosts."""
        p = sshkeys.write_known_hosts("10.213.98.4", "ssh-ed25519 AAA ca")
        self.assertEqual(p.read_text(),
                         "@cert-authority 10.213.98.4 ssh-ed25519 AAA ca\n")

    def test_argv_presents_the_cert_and_verifies_the_host(self):
        argv = sshkeys.ssh_argv(
            host="web-test.tail1.ts.net", user="dev", key=self.dir / "k",
            cert=self.dir / "c", known_hosts=self.dir / "kh",
            forward_agent=True, command=["pytest", "-x"])
        self.assertEqual(argv[0], "ssh")
        self.assertIn(f"CertificateFile={self.dir / 'c'}", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)   # the CA vouches; no TOFU
        self.assertIn("IdentitiesOnly=yes", argv)          # don't spray other keys
        self.assertIn("ForwardAgent=yes", argv)
        self.assertEqual(argv[-4:], ["dev@web-test.tail1.ts.net", "--", "pytest", "-x"])

    def test_a_jump_becomes_a_proxycommand_not_proxyjump(self):
        """-J would apply cawl's key, known_hosts, and strict checking to the
        jump hop too — and the jump host is in nobody's cawl known_hosts. The
        inner ssh uses the user's own config for the hop; the box hop keeps
        the CA-pinned checks."""
        argv = sshkeys.ssh_argv(
            host="10.213.98.4", user="dev", key=self.dir / "k",
            cert=self.dir / "c", known_hosts=self.dir / "kh",
            forward_agent=False, jump="ops@jump.example.com")
        self.assertIn("ProxyCommand=ssh -W %h:%p ops@jump.example.com", argv)
        self.assertNotIn("-J", argv)
        self.assertEqual(argv[-1], "dev@10.213.98.4")

    def test_no_jump_means_no_proxy(self):
        argv = sshkeys.ssh_argv(
            host="h.t.ts.net", user="dev", key=self.dir / "k", cert=self.dir / "c",
            known_hosts=self.dir / "kh", forward_agent=False, jump=None)
        self.assertFalse(any("ProxyCommand" in a for a in argv))

    def test_agent_forwarding_can_be_declined(self):
        argv = sshkeys.ssh_argv(
            host="h.t.ts.net", user="dev", key=self.dir / "k", cert=self.dir / "c",
            known_hosts=self.dir / "kh", forward_agent=False)
        self.assertNotIn("ForwardAgent=yes", argv)
        self.assertEqual(argv[-1], "dev@h.t.ts.net")

    def block(self, **over):
        kw = dict(alias="acme-dev", host="acme-dev.tail1.ts.net", user="dev",
                  key=self.dir / "k", cert=self.dir / "c",
                  known_hosts=self.dir / "kh")
        return sshkeys.ssh_config_block(**{**kw, **over})

    def test_config_block_carries_the_same_checks_as_the_argv(self):
        """Whatever reads this file — rsync, sshfs, an editor — has to end up
        with the connection `cawl ssh` would have made, not a laxer one."""
        text = self.block()
        self.assertIn(f"    CertificateFile {self.dir / 'c'}", text)
        self.assertIn("    StrictHostKeyChecking yes", text)
        self.assertIn("    IdentitiesOnly yes", text)
        self.assertIn(f"    UserKnownHostsFile {self.dir / 'kh'}", text)
        self.assertIn("    HostName acme-dev.tail1.ts.net", text)
        self.assertIn("    User dev", text)

    def test_config_block_does_not_forward_the_agent(self):
        """Unlike `cawl ssh`: an editor holds one of these open for days, and
        anyone the box is shared with has sudo in it."""
        self.assertIn("    ForwardAgent no", self.block())

    def test_the_refresh_command_is_a_match_criterion(self):
        """The cert expires in minutes, so it has to be re-signed as ssh dials
        — and `host` comes first so the command doesn't run on every unrelated
        connection."""
        head = self.block(refresh="cawl cert acme-dev").splitlines()[0]
        self.assertEqual(head, 'Match host acme-dev exec "cawl cert acme-dev"')

    def test_a_block_without_a_refresh_still_matches_the_alias(self):
        self.assertEqual(self.block().splitlines()[0], "Match host acme-dev")

    def test_a_jump_becomes_a_proxycommand_in_the_config_too(self):
        text = self.block(host="10.213.98.4", jump="ops@jump.example.com")
        self.assertIn("    ProxyCommand ssh -W %h:%p ops@jump.example.com", text)
        self.assertNotIn("ProxyJump", text)

    def test_a_path_with_a_space_is_quoted(self):
        """ssh_config splits on whitespace; a home directory with a space in it
        would otherwise silently produce a key path that doesn't exist."""
        text = self.block(key=Path("/home/a b/.config/cawl/id_ed25519"))
        self.assertIn('    IdentityFile "/home/a b/.config/cawl/id_ed25519"', text)


if __name__ == "__main__":
    unittest.main()
