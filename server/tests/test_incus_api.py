import unittest

from cawl_core.runtime.base import InstanceInfo, InstanceSpec
from cawl_core.runtime.incus_api import IncusApiError, IncusApiRuntime


def _rt(vm=False):
    # Bypass __init__ (which loads TLS certs); we stub the transport below.
    r = IncusApiRuntime.__new__(IncusApiRuntime)
    r.project, r.image_prefix, r.timeout = "default", "cawl", 5
    r.vm = vm
    return r


def _spec(**kw):
    base = dict(id="x", template="s", image="cawl/base")
    base.update(kw)
    return InstanceSpec(**base)


class TestIncusApiRuntime(unittest.TestCase):
    def test_image_name(self):
        r = _rt()
        self.assertEqual(r.image_name("acme"), "cawl/acme")
        self.assertEqual(_rt(vm=True).image_name("acme"), "cawl/acme-vm")
        self.assertEqual(r.image_name("cawl/base"), "cawl/base")  # explicit alias kept

    def test_type_and_config(self):
        r = _rt()
        self.assertEqual(_rt(vm=True)._type_and_config(),
                         ("virtual-machine", {"security.secureboot": "false"}))
        itype, cfg = r._type_and_config()
        self.assertEqual(itype, "container")
        self.assertEqual(cfg["security.nesting"], "true")

    def test_exec_parses_record_output(self):
        r = _rt()
        r._op = lambda m, p, b=None: {
            "metadata": {"return": 3, "output": {"1": "/log/o", "2": "/log/e"}}}
        logs = {"/log/o": b"hi\n", "/log/e": b"boom\n"}
        r._request = lambda method, path, body=None, raw=False: (
            (200, logs[path]) if method == "GET" else (200, b""))
        res = r.exec("x", ["echo", "hi"])
        self.assertEqual((res.exit_code, res.stdout, res.stderr), (3, "hi\n", "boom\n"))

    def test_destroy_force_deletes_in_one_backend_operation(self):
        r = _rt()
        calls = []
        r._op = lambda m, p, b=None: calls.append((m, p)) or {}
        r.destroy("x")
        self.assertEqual(calls, [("DELETE", "/1.0/instances/x?force=true")])

    def test_destroy_ignores_only_explicit_not_found(self):
        r = _rt()
        r._op = lambda *args, **kwargs: (_ for _ in ()).throw(
            IncusApiError("not found", status_code=404))
        r.destroy("x")

        for status in (None, 400, 403, 500, 503):
            with self.subTest(status=status):
                r._op = lambda *args, status=status, **kwargs: (
                    _ for _ in ()).throw(
                        IncusApiError("operation failed", status_code=status))
                with self.assertRaises(IncusApiError):
                    r.destroy("x")

    def test_provision_runs_the_network_boot_script_before_the_hook(self):
        """The access provider's join script is just another rendered hook to
        the backend — run first, so the box is reachable by the time the app
        comes up. (What the script *contains* is test_access's business.)"""
        r = _rt()
        scripts = []
        r._sh = lambda id, script: scripts.append(script)
        r._provision(_spec(network_boot="join-the-network", provision="app up"))
        self.assertIn("join-the-network", scripts[0])
        self.assertIn("app up", scripts[-1])

    def test_provision_trusts_the_ca_and_pins_the_box_to_its_own_id(self):
        r = _rt()
        scripts = []
        r._sh = lambda id, script: scripts.append(script)
        r._provision(_spec(ssh_ca_pubkey="ssh-ed25519 AAAAC3 ca"))
        trust = next(s for s in scripts if "TrustedUserCAKeys" in s)
        self.assertIn("AuthorizedPrincipalsFile /etc/ssh/authorized_principals/%u", trust)
        # Create the drop-in dir rather than assume it: an image without it would
        # fail to provision, and the failure would look like a network problem.
        self.assertIn("install -d -m755 /etc/ssh/sshd_config.d", trust)
        # The principals file names the *sandbox*, not any person — so a grant
        # never has to be written into the box.
        self.assertIn("printf '%s\\n' x > /etc/ssh/authorized_principals/dev", trust)
        self.assertIn("PasswordAuthentication no", trust)
        # The host cert is installed later by the control plane: sshd refuses to
        # start with a HostCertificate that isn't there yet.
        self.assertNotIn("HostCertificate", trust)

    def test_no_ca_configured_means_no_sshd_changes(self):
        r = _rt()
        scripts = []
        r._sh = lambda id, script: scripts.append(script)
        r._provision(_spec())  # no ssh_ca_pubkey
        self.assertFalse(any("TrustedUserCAKeys" in s for s in scripts))

    def test_stop_is_graceful_and_keeps_the_disk(self):
        r = _rt()
        calls = []
        r._op = lambda m, p, b=None: calls.append((m, p, b)) or {}
        r.stop("x")
        self.assertEqual(calls, [("PUT", "/1.0/instances/x/state",
                                  {"action": "stop", "timeout": 30})])
        # not "delete", and not force — a pause, not a kill
        self.assertNotIn("force", str(calls))

    def test_start_rejoins_the_network_and_brings_the_app_back(self):
        """Network membership rarely survives a shutdown (an ephemeral tailnet
        node is dropped and its name stops resolving), and Docker won't restart
        containers with no restart policy. A resume that only powered the box on
        would hand back an unreachable box running nothing."""
        r = _rt()
        scripts = []
        r._sh = lambda id, script: scripts.append(script)
        r._op = lambda m, p, b=None: {}
        r.info = lambda id: InstanceInfo(ip="10.0.0.5", status="running")

        info = r.start(_spec(network_boot="join-the-network",
                             provision="docker compose up -d"))
        self.assertEqual(info.ip, "10.0.0.5")
        self.assertTrue(any("join-the-network" in s for s in scripts))
        self.assertTrue(any("docker compose up -d" in s for s in scripts))
        # The CA trust and host cert live on the box's disk — not re-installed.
        self.assertFalse(any("TrustedUserCAKeys" in s for s in scripts))

    def test_review_envs_get_ssh_setup_like_any_other(self):
        # Purpose never gates capabilities — a review box is a box.
        r = _rt()
        scripts = []
        r._sh = lambda id, script: scripts.append(script)
        r._provision(_spec(ssh_ca_pubkey="ssh-ed25519 AAAAC3 ca"))
        self.assertTrue(any("TrustedUserCAKeys" in s for s in scripts))

    def test_provision_runs_the_hook_after_docker_is_up(self):
        r = _rt()
        scripts = []
        r._sh = lambda id, script: scripts.append(script)
        r._provision(_spec(provision="git checkout feat\ndocker compose up -d"))
        self.assertIn("docker info", scripts[0])  # waits for docker first
        self.assertIn("git checkout feat", scripts[-1])

    def test_provision_without_a_hook_just_waits_for_docker(self):
        r = _rt()
        scripts = []
        r._sh = lambda id, script: scripts.append(script)
        r._provision(_spec())  # e.g. the scratch template: no hooks at all
        self.assertEqual(len(scripts), 1)
        self.assertIn("docker info", scripts[0])

    def test_build_image_runs_the_build_hook(self):
        r = _rt()
        scripts, ops = [], []
        r._sh = lambda id, script: scripts.append((id, script))
        r._op = lambda m, p, b=None: ops.append((m, p)) or {}
        r._request = lambda m, p, body=None, raw=False: (200, b"")
        image = r.build_image(_spec(image="cawl/acme", build="git clone x && make"))
        self.assertEqual(image, "cawl/acme")
        self.assertTrue(any("git clone x && make" in s for _, s in scripts))
        self.assertIn(("POST", "/1.0/images"), ops)  # publishes the builder

    def test_create_issues_create_then_start_then_provision(self):
        r = _rt()
        calls = []
        r._op = lambda m, p, b=None: calls.append((m, p, b)) or {}
        r._provision = lambda spec: calls.append(("provision", spec.id, None))
        r.info = lambda id: InstanceInfo(ip="10.0.0.1", status="Running")
        info = r.create(_spec())
        self.assertEqual(info.ip, "10.0.0.1")
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "/1.0/instances")
        self.assertEqual(calls[0][2]["type"], "container")
        self.assertEqual(calls[0][2]["source"]["alias"], "cawl/base")
        self.assertEqual((calls[1][0], calls[1][2]["action"]), ("PUT", "start"))
        self.assertIn(("provision", "x", None), calls)


if __name__ == "__main__":
    unittest.main()
