import subprocess
import tempfile
import unittest
from pathlib import Path

from cawl_core.ssh_ca import SshCaError, SshCertAuthority


def keygen(path: Path, comment: str = "test") -> Path:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment,
                    "-f", str(path)], check=True, capture_output=True)
    return path


def cert_body(cert: str) -> str:
    """What ssh-keygen -L prints for a cert — the fields sshd will act on."""
    with tempfile.NamedTemporaryFile("w", suffix="-cert.pub") as f:
        f.write(cert + "\n")
        f.flush()
        return subprocess.run(["ssh-keygen", "-L", "-f", f.name],
                              capture_output=True, text=True, check=True).stdout


class TestSshCertAuthority(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.ca = SshCertAuthority(keygen(d / "ca"), user_ttl="10m")
        self.user_key = keygen(d / "user")

    def tearDown(self):
        self.tmp.cleanup()

    def pub(self, key: Path) -> str:
        return key.with_suffix(".pub").read_text()

    # -- what the certificate actually says --------------------------------
    def test_user_cert_names_the_sandbox_not_the_person(self):
        """The principal is the instance id — that *is* the authorization. The
        person appears only as the key id, for the audit trail."""
        out = cert_body(self.ca.sign_user(
            self.pub(self.user_key), environment_id="web-test", actor="tom"))
        self.assertIn("Principals:", out)
        self.assertIn("web-test", out)
        self.assertIn('Key ID: "tom@web-test"', out)
        self.assertIn("Type: ssh-ed25519-cert", out)
        self.assertIn("user certificate", out)

    def test_user_cert_is_short_lived(self):
        out = cert_body(self.ca.sign_user(
            self.pub(self.user_key), environment_id="web-test", actor="tom"))
        self.assertNotIn("Valid: forever", out)

    def test_user_cert_grants_only_pty_and_agent_forwarding(self):
        out = cert_body(self.ca.sign_user(
            self.pub(self.user_key), environment_id="web-test", actor="tom"))
        self.assertIn("permit-pty", out)
        self.assertIn("permit-agent-forwarding", out)
        # -O clear dropped the rest: no port forwarding, no X11, no user rc.
        self.assertNotIn("permit-port-forwarding", out)
        self.assertNotIn("permit-X11-forwarding", out)

    def test_host_cert_covers_the_tailnet_name(self):
        out = cert_body(self.ca.sign_host(
            self.pub(self.user_key), environment_id="web-test",
            hostnames=["web-test.tail1.ts.net", "web-test"]))
        self.assertIn("host certificate", out)
        self.assertIn("web-test.tail1.ts.net", out)

    # -- input handling ----------------------------------------------------
    def test_rejects_junk_public_key(self):
        with self.assertRaises(SshCaError):
            self.ca.sign_user("not a key", environment_id="web-test", actor="tom")

    def test_rejects_a_second_smuggled_key(self):
        """The key is written to a file for ssh-keygen, so a newline must not be
        able to bring a friend."""
        two = self.pub(self.user_key).strip() + "\n" + self.pub(self.user_key)
        with self.assertRaises(SshCaError):
            self.ca.sign_user(two, environment_id="web-test", actor="tom")

    def test_rejects_a_principal_that_could_smuggle_another(self):
        with self.assertRaises(SshCaError):
            self.ca.sign_user(self.pub(self.user_key),
                              environment_id="web-test,other-box", actor="tom")

    def test_missing_ca_key_is_an_error_not_a_silent_pass(self):
        ca = SshCertAuthority(Path(self.tmp.name) / "absent")
        with self.assertRaises(SshCaError):
            ca.sign_user(self.pub(self.user_key), environment_id="x", actor="tom")


if __name__ == "__main__":
    unittest.main()
