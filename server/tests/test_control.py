import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cawl_core.access import BridgeAccess, TailscaleAccess
from cawl_core.auth import Principal, Role
from cawl_core.config import parse_template_config
from cawl_core.egress import ProxyEgress
from cawl_core.control import (
    CawlError, ControlPlane, InvalidName, NameConflict, NotFound, ParamError,
    PermissionDenied, QuotaExceeded,
)
from cawl_core.ingress import TraefikIngress
from cawl_core.models import Exposure, Status
from cawl_core.runtime.fake import FakeRuntime
from cawl_core.ssh_ca import SshCertAuthority
from fakes import FakeStateStore

NOW = datetime(2026, 7, 8, 12, tzinfo=timezone.utc)
TOM = Principal("tom")
SUE = Principal("sue")
ADMIN = Principal("root", Role.admin)
# The batteries-included access provider; bridge access has its own tests.
_ACCESS = TailscaleAccess("tskey-test", tailnet="tail1.ts.net")


def template():
    return parse_template_config({
        "name": "acme",
        "params": {"branch": {"default": "main"}},
        "hooks": {"build": "git clone git@x/acme.git /srv/app",
                  "provision": "git checkout {{ branch }} && docker compose up -d"},
        "expose": {"web": 8000},
    })


def blank(**kw):
    """A template that declares no params and no hooks (the scratch box)."""
    return parse_template_config({"name": "scratch", **kw})


class TestEgressProviderBoundary(unittest.TestCase):
    def test_provider_boot_script_reaches_runtime_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime()
            control = ControlPlane(
                FakeStateStore(), runtime, TraefikIngress(tmp, "example.com"),
                egress=ProxyEgress("cawl-agent", "http://10.42.0.1:3128"),
            )
            inst = control.up(blank(), TOM, now=NOW).instance
            self.assertIn("https_proxy=http://10.42.0.1:3128", runtime.specs[inst.id].egress_boot)


class TestControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ingress_dir = Path(self.tmp.name) / "dynamic"
        self.runtime = FakeRuntime()
        self.control = ControlPlane(
            state=FakeStateStore(),
            runtime=self.runtime,
            ingress=TraefikIngress(self.ingress_dir, "review.example.com"),
            access=_ACCESS,
            default_quota=3,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def up(self, branch, actor=TOM, **kw):
        """`template()` declares one param, `branch` — so most tests vary just that."""
        return self.control.up(template(), actor, args={"branch": branch},
                               now=NOW, **kw)

    # -- lifecycle --------------------------------------------------------
    def test_up_creates_ready_env(self):
        inst = self.up("feature/x", ttl="7d").instance
        self.assertEqual(inst.status, Status.ready)
        self.assertEqual(inst.owner, "tom")
        # The template's default `web` exposure is live, at the bare-id hostname.
        self.assertEqual(inst.url, f"https://{inst.id}.review.example.com")
        self.assertEqual(inst.exposures, (Exposure(inst.id, 8000),))
        self.assertEqual(inst.ssh, f"dev@{inst.id}.tail1.ts.net")
        self.assertEqual(inst.expires_at, NOW + timedelta(days=7))
        self.assertTrue((self.ingress_dir / f"{inst.id}.yml").exists())

    def test_template_default_ttl_applies_when_none_requested(self):
        cfg = parse_template_config({"name": "shortlived",
                                     "defaults": {"ttl": "4h"}})
        res = self.control.up(cfg, Principal("agent-1"), now=NOW)
        self.assertEqual(res.instance.expires_at, NOW + timedelta(hours=4))

    def test_reuse_if_exists(self):
        a = self.up("main", reuse=True)
        b = self.up("main", reuse=True)
        self.assertEqual(a.instance.id, b.instance.id)
        self.assertEqual(len(self.control.ls(ADMIN)), 1)

    def test_max_ttl_caps_and_fills_in(self):
        """A caller-level lifetime cap (agent tokens): a guardrail the request
        can't talk its way out of."""
        cap = timedelta(hours=4)
        # fills in where nothing set an expiry (even an explicit --ttl none)
        a = self.control.up(template(), TOM, ttl="none",
                            max_ttl=cap, now=NOW).instance
        self.assertEqual(a.expires_at, NOW + cap)
        # clamps a longer request...
        b = self.control.up(template(), SUE, ttl="7d",
                            max_ttl=cap, now=NOW).instance
        self.assertEqual(b.expires_at, NOW + cap)
        # ...and leaves a shorter one alone
        c = self.control.up(template(), Principal("al"), ttl="1h",
                            max_ttl=cap, now=NOW).instance
        self.assertEqual(c.expires_at, NOW + timedelta(hours=1))

    def test_quota(self):
        for _ in range(3):
            self.up("main", actor=Principal("greedy"), ttl="1h")
        with self.assertRaises(QuotaExceeded):
            self.up("main", actor=Principal("greedy"))

    def test_exec_passthrough(self):
        inst = self.up("main").instance
        out = self.control.exec(inst.id, TOM, ["echo", "hi"])
        self.assertEqual(out.exit_code, 0)
        self.assertIn("echo hi", out.stdout)

    def test_destroy_cleans_state_and_ingress(self):
        id = self.up("feature/x").instance.id
        self.control.destroy(id, TOM)
        self.assertFalse((self.ingress_dir / f"{id}.yml").exists())
        self.assertNotIn(id, self.runtime.instances)
        with self.assertRaises(NotFound):
            self.control.status(id, ADMIN)

    def test_reap_expired(self):
        live = self.up("main", actor=Principal("a1"), ttl="4h")
        dead = self.up("main", actor=Principal("a2"), ttl="1h")
        reaped = self.control.reap(now=NOW + timedelta(hours=2))
        self.assertEqual(reaped, [dead.instance.id])
        self.assertIsNotNone(self.control.status(live.instance.id, ADMIN))

    def test_failed_create_marks_error(self):
        def boom(spec):
            raise RuntimeError("incus exploded")
        self.runtime.create = boom
        with self.assertRaises(Exception):
            self.up("main")
        insts = self.control.ls(ADMIN)
        self.assertEqual(insts[0].status, Status.error)
        self.assertIn("incus exploded", insts[0].error)

    def test_generated_id_collision_does_not_touch_existing_environment(self):
        collided_id = "acme-" + "0" * 32
        replacement_id = "acme-" + "1" * 32
        victim = self.up("victim", actor=SUE, name=collided_id).instance

        with patch("cawl_core.control.new_environment_id",
                   side_effect=[collided_id, replacement_id]):
            created = self.up("new").instance

        self.assertEqual(created.id, replacement_id)
        untouched = self.control.status(victim.id, SUE)
        self.assertEqual(untouched.owner, "sue")
        self.assertEqual(untouched.status, Status.ready)
        self.assertIn(victim.id, self.runtime.instances)

    # -- backends ----------------------------------------------------------
    # A backend is a named entry in the operator's registry; cawl attaches no
    # meaning to the names and validates nothing but membership.
    def _dual(self):
        self.vm_runtime = FakeRuntime(vm=True)
        return ControlPlane(
            state=FakeStateStore(),
            runtime={"container": self.runtime, "vm": self.vm_runtime},
            ingress=TraefikIngress(self.ingress_dir, "review.example.com"),
            access=_ACCESS, default_backend="container",
        )

    def test_the_default_backend_applies(self):
        inst = self._dual().up(template(), TOM, now=NOW).instance
        self.assertEqual(inst.backend, "container")
        self.assertEqual(inst.image, "fake/acme")
        self.assertIn(inst.id, self.runtime.specs)

    def test_choosing_a_backend_routes_the_whole_lifecycle(self):
        control = self._dual()
        inst = control.up(template(), TOM, backend="vm", now=NOW).instance
        self.assertEqual(inst.backend, "vm")
        self.assertEqual(inst.image, "fake/acme-vm")   # per-backend image lineage
        self.assertIn(inst.id, self.vm_runtime.specs)
        self.assertNotIn(inst.id, self.runtime.specs)
        control.stop(inst.id, TOM)                      # ...and ops keep routing
        self.assertNotIn(inst.id, self.vm_runtime.instances)

    def test_an_unknown_backend_is_refused_with_the_menu(self):
        with self.assertRaises(CawlError) as ctx:
            self._dual().up(template(), TOM, backend="firecracker", now=NOW)
        self.assertIn("firecracker", str(ctx.exception))
        self.assertIn("container", str(ctx.exception))  # says what exists

    def test_blank_site_uses_explicit_base_image(self):
        inst = self.control.up(blank(image="cawl/base"), TOM,
                               now=NOW).instance
        self.assertEqual(inst.image, "cawl/base")             # not fake/scratch

    # -- blank / scratch dev env ------------------------------------------
    def test_blank_env_boots_without_ingress(self):
        res = self.control.up(blank(), TOM,
                              authorized_keys=["ssh-ed25519 AAAA... tom"], now=NOW)
        inst = res.instance
        self.assertEqual(inst.status, Status.ready)
        self.assertIsNone(inst.url)                       # no public URL
        self.assertEqual(inst.exposures, ())
        self.assertEqual(inst.ssh, f"dev@{inst.id}.tail1.ts.net")
        self.assertFalse((self.ingress_dir / f"{inst.id}.yml").exists())
        # keys threaded to the runtime for the plain-sshd path
        self.assertEqual(self.runtime.specs[inst.id].authorized_keys,
                         ["ssh-ed25519 AAAA... tom"])
        # No hooks: the box boots and stops there.
        self.assertEqual(self.runtime.specs[inst.id].provision, "")
        self.assertEqual(inst.args, {})

    # -- template args -----------------------------------------------------
    def test_provision_hook_is_rendered_with_the_args(self):
        inst = self.up("feature/x").instance
        script = self.runtime.specs[inst.id].provision
        self.assertIn("git checkout feature/x", script)   # {{branch}} substituted
        self.assertIn("branch=feature/x; export branch", script)
        self.assertIn("template=acme; export template", script)  # builtins too

    def test_arg_value_cannot_break_out_of_the_hook(self):
        inst = self.up("x; rm -rf /").instance
        script = self.runtime.specs[inst.id].provision
        self.assertIn("git checkout 'x; rm -rf /'", script)

    def test_unknown_arg_is_rejected(self):
        with self.assertRaises(ParamError):
            self.control.up(template(), TOM, args={"bogus": "1"}, now=NOW)

    def test_omitted_arg_takes_the_template_default(self):
        inst = self.control.up(template(), TOM, now=NOW).instance
        self.assertEqual(inst.args, {"branch": "main"})

    def test_args_are_part_of_the_reuse_key(self):
        a = self.up("main", reuse=True).instance
        b = self.up("main", reuse=True).instance
        c = self.up("feature/x", reuse=True).instance
        self.assertEqual(a.id, b.id)      # same args -> same env
        self.assertNotEqual(a.id, c.id)   # different args -> a different env
        self.assertEqual(len(self.control.ls(TOM)), 2)

    def test_default_and_explicit_same_value_reuse_each_other(self):
        a = self.control.up(template(), TOM, reuse=True, now=NOW).instance
        b = self.up("main", reuse=True).instance
        self.assertEqual(a.id, b.id)

    # -- authorization ----------------------------------------------------
    def test_user_sees_only_own(self):
        self.up("main", actor=TOM)
        self.up("main", actor=SUE)
        self.assertEqual({i.owner for i in self.control.ls(TOM)}, {"tom"})
        self.assertEqual({i.owner for i in self.control.ls(SUE)}, {"sue"})
        self.assertEqual({i.owner for i in self.control.ls(ADMIN)}, {"tom", "sue"})

    def test_user_cannot_view_others(self):
        id = self.up("main", actor=SUE).instance.id
        with self.assertRaises(PermissionDenied):
            self.control.status(id, TOM)
        # admin can
        self.assertEqual(self.control.status(id, ADMIN).id, id)

    def test_user_cannot_control_others(self):
        id = self.up("main", actor=SUE).instance.id
        with self.assertRaises(PermissionDenied):
            self.control.exec(id, TOM, ["ls"])
        with self.assertRaises(PermissionDenied):
            self.control.destroy(id, TOM)

    def test_admin_can_control_any(self):
        id = self.up("main", actor=SUE).instance.id
        self.control.destroy(id, ADMIN)  # no raise
        with self.assertRaises(NotFound):
            self.control.status(id, ADMIN)

    def test_on_behalf_of_admin_only(self):
        with self.assertRaises(PermissionDenied):
            self.up("main", actor=TOM, on_behalf_of="sue")
        inst = self.up("main", actor=ADMIN, on_behalf_of="sue").instance
        self.assertEqual(inst.owner, "sue")

    def test_refresh_image_admin_only(self):
        with self.assertRaises(PermissionDenied):
            self.control.refresh_image(template(), TOM)
        self.assertEqual(self.control.refresh_image(template(), ADMIN), "fake/acme")

    def test_reap_requires_admin(self):
        with self.assertRaises(PermissionDenied):
            self.control.reap(TOM)

    # -- custom names ------------------------------------------------------
    def test_name_becomes_the_id_and_the_ssh_host(self):
        inst = self.up("main", name="Web-Test").instance
        self.assertEqual(inst.id, "web-test")  # normalized, and used verbatim
        self.assertEqual(inst.ssh, "dev@web-test.tail1.ts.net")
        self.assertEqual(self.control.status("web-test", TOM), inst)  # addressable by name

    def test_invalid_name_rejected(self):
        with self.assertRaises(InvalidName):
            self.up("main", name="my env")

    def test_name_conflict_with_live_env(self):
        self.up("main", name="web-test")
        with self.assertRaises(NameConflict):
            self.up("other", name="web-test")

    def test_name_conflict_is_global_across_owners(self):
        self.up("main", name="web-test", actor=TOM)
        with self.assertRaises(NameConflict):
            self.up("main", name="web-test", actor=SUE)

    def test_name_held_by_exposure_fails_before_runtime_create(self):
        self.up("main", name="holder")
        self.control.expose("holder", TOM, 9000, name="web-test")
        before = set(self.runtime.instances)
        with self.assertRaises(NameConflict):
            self.up("other", name="web-test", actor=SUE)
        self.assertEqual(set(self.runtime.instances), before)

    def test_name_freed_by_destroy(self):
        first = self.up("main", name="web-test").instance
        self.control.destroy("web-test", TOM)
        second = self.up("other", name="web-test").instance
        self.assertEqual(second.id, "web-test")
        self.assertEqual(second.args, {"branch": "other"})
        self.assertIsNot(second, first)

    def test_failed_runtime_destroy_is_operator_visible(self):
        self.up("main", name="web-test")

        def boom(_id):
            raise RuntimeError("incus delete failed")

        self.runtime.destroy = boom
        with self.assertRaisesRegex(CawlError, "failed to destroy web-test"):
            self.control.destroy("web-test", TOM)
        failed = self.control.state.get("web-test")
        self.assertEqual(failed.status, Status.destroy_failed)
        self.assertIn("incus delete failed", failed.error)
        # A teardown failure may still be live, so it keeps its name and quota
        # reservation and can be retried rather than recycled as a fresh env.
        with self.assertRaises(NameConflict):
            self.up("other", name="web-test")

        self.runtime.destroy = lambda id: self.runtime.instances.pop(id, None)
        self.control.destroy("web-test", TOM)
        self.assertEqual(self.control.state.get("web-test").status, Status.destroyed)

    def test_failed_cleanup_after_provisioning_keeps_live_namespace_reserved(self):
        self.control.ingress.sync = lambda inst: (_ for _ in ()).throw(
            RuntimeError("route render failed"))
        self.runtime.destroy = lambda id: (_ for _ in ()).throw(
            RuntimeError("incus delete failed"))

        with self.assertRaisesRegex(CawlError, "failed to bring up web-test"):
            self.up("main", name="web-test")

        failed = self.control.state.get("web-test")
        self.assertEqual(failed.status, Status.destroy_failed)
        self.assertIn("teardown failed: incus delete failed", failed.error)
        with self.assertRaises(NameConflict):
            self.up("other", name="web-test")

    def test_name_freed_by_failed_create(self):
        real_create = self.runtime.create

        def boom(spec):
            raise RuntimeError("incus exploded")

        self.runtime.create = boom
        with self.assertRaises(CawlError):
            self.up("main", name="web-test")

        # The errored env keeps its row, but not its claim on the name.
        self.runtime.create = real_create
        inst = self.up("main", name="web-test").instance
        self.assertEqual(inst.status, Status.ready)

    def test_reuse_with_name_returns_that_env(self):
        a = self.up("main", name="web-test", reuse=True).instance
        # Different args: the natural key doesn't match, but the name does — and the
        # name is what was asked for, so it wins. The args you passed are ignored,
        # along with everything else about the env you'd have got instead.
        b = self.up("other", name="web-test", reuse=True).instance
        self.assertEqual(a.id, b.id)
        self.assertEqual(b.args, {"branch": "main"})
        self.assertEqual(len(self.control.ls(TOM)), 1)

    def test_reuse_with_name_wont_hand_over_another_owners_env(self):
        self.up("main", name="web-test", actor=TOM)
        with self.assertRaises(PermissionDenied):
            self.up("main", name="web-test", actor=SUE, reuse=True)


class TestStopStart(unittest.TestCase):
    """Pause without deleting. The point is RAM, so a stopped env gives up its
    address and its memory but keeps its disk, its name and its identity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ingress_dir = Path(self.tmp.name) / "dynamic"
        self.runtime = FakeRuntime()
        self.control = ControlPlane(
            state=FakeStateStore(), runtime=self.runtime,
            ingress=TraefikIngress(self.ingress_dir, "review.example.com"),
            access=_ACCESS, default_quota=3,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def up(self, actor=TOM, **kw):
        return self.control.up(template(), actor, args={"branch": "main"},
                               now=NOW, **kw).instance

    def test_stop_keeps_the_env_but_drops_its_address(self):
        inst = self.up(name="web-test")
        self.assertIsNotNone(inst.vm_ip)
        stopped = self.control.stop("web-test", TOM)
        self.assertEqual(stopped.status, Status.stopped)
        self.assertIsNone(stopped.vm_ip)
        # Still there — this is a pause, not a delete.
        self.assertEqual(self.control.status("web-test", TOM).id, "web-test")
        self.assertNotIn("web-test", self.runtime.instances)

    def test_start_brings_it_back_with_a_new_address(self):
        old_ip = self.up(name="web-test").vm_ip
        self.control.stop("web-test", TOM)
        inst = self.control.start("web-test", TOM, template()).instance
        self.assertEqual(inst.status, Status.ready)
        self.assertIsNotNone(inst.vm_ip)
        self.assertNotEqual(inst.vm_ip, old_ip)   # the fake re-leases; state follows
        self.assertEqual(inst.ssh, "dev@web-test.tail1.ts.net")  # identity unchanged

    def test_start_replays_the_boot_half_so_the_app_and_tailnet_come_back(self):
        """A stopped node is dropped from the tailnet (ephemeral authkey) and
        Docker won't restart containers with no restart policy — so a resume that
        only powered the box on would hand back a box you can't reach running
        nothing."""
        self.up(name="web-test")
        self.control.stop("web-test", TOM)
        self.control.start("web-test", TOM, template())
        self.assertEqual(self.runtime.booted, ["web-test"])
        self.assertIn("git checkout main",
                      self.runtime.specs["web-test"].provision)

    def test_stop_start_keeps_urls_and_repoints_routes(self):
        """Exposure rows survive a stop, so `start` re-renders the same URLs at
        the box's new address. While it's down there's no route at all — a
        stopped env should 404, not hang on a dead IP."""
        inst = self.control.up(template(), TOM, args={"branch": "x"},
                               name="rev", now=NOW).instance
        self.assertEqual(inst.url, "https://rev.review.example.com")
        before = (self.ingress_dir / "rev.yml").read_text()

        self.control.stop("rev", TOM)
        self.assertFalse((self.ingress_dir / "rev.yml").exists())

        res = self.control.start("rev", TOM, template())
        self.assertEqual(res.instance.url, "https://rev.review.example.com")
        self.assertEqual(res.instance.exposures, (Exposure("rev", 8000),))
        after = (self.ingress_dir / "rev.yml").read_text()
        self.assertNotEqual(before, after)         # repointed...
        self.assertIn(res.instance.vm_ip, after)   # ...at the new IP

    def test_stop_is_idempotent_and_start_of_a_running_env_is_a_no_op(self):
        self.up(name="web-test")
        self.control.stop("web-test", TOM)
        self.assertEqual(self.control.stop("web-test", TOM).status, Status.stopped)
        self.control.start("web-test", TOM, template())
        self.assertEqual(
            self.control.start("web-test", TOM, template()).instance.status, Status.ready)

    def test_only_the_owner_can_stop_it(self):
        self.up(name="web-test")
        self.control.share("web-test", TOM, "sue")
        # A grantee may use the env, but not yank it out from under everyone else.
        with self.assertRaises(PermissionDenied):
            self.control.stop("web-test", SUE)
        with self.assertRaises(PermissionDenied):
            self.control.start("web-test", SUE, template())

    def test_a_stopped_env_still_counts_against_quota(self):
        """It's holding disk, and it'll be back. Quota is about what you're
        keeping, not what's currently burning RAM."""
        for i in range(3):
            self.up(name=f"box-{i}", actor=Principal("greedy"))
        self.control.stop("box-0", Principal("greedy"))
        with self.assertRaises(QuotaExceeded):
            self.up(name="box-3", actor=Principal("greedy"))

    def test_the_reaper_still_collects_a_stopped_env(self):
        """Stopping is a pause, not a preservation order — otherwise 'stop' would
        become the way to leak a VM past its TTL."""
        self.control.up(template(), TOM, args={"branch": "m"},
                        name="agent-box", ttl="1h", now=NOW)
        self.control.stop("agent-box", TOM)
        self.assertEqual(self.control.reap(now=NOW + timedelta(hours=2)),
                         ["agent-box"])

    def test_reuse_if_exists_hands_back_a_running_env_not_a_stopped_one(self):
        """`--reuse-if-exists` promises the env you'd have got — and the env you'd
        have got would be up. An agent retrying must not get a dead box."""
        self.control.up(template(), TOM, args={"branch": "m"},
                        reuse=True, now=NOW)
        first = self.control.ls(TOM)[0]
        self.control.stop(first.id, TOM)

        res = self.control.up(template(), TOM, args={"branch": "m"},
                              reuse=True, now=NOW)
        self.assertEqual(res.instance.id, first.id)
        self.assertEqual(res.instance.status, Status.ready)
        self.assertIsNotNone(res.instance.vm_ip)

    def test_you_cannot_start_an_env_that_was_never_stopped_wrongly(self):
        self.up(name="web-test")
        self.control.stop("web-test", TOM)
        self.control.destroy("web-test", TOM)
        with self.assertRaises(NotFound):
            self.control.start("web-test", TOM, template())


class TestExposures(unittest.TestCase):
    """An exposure is a row, like a grant: routes render from the rows, and
    changing them never touches the box. Its name is a hostname label — a
    global namespace, freely chosen, defaulting to the env's own id."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ingress_dir = Path(self.tmp.name) / "dynamic"
        self.runtime = FakeRuntime()
        self.control = ControlPlane(
            state=FakeStateStore(), runtime=self.runtime,
            ingress=TraefikIngress(self.ingress_dir, "sbx.example.com",
                                   auth_host="auth.sbx.example.com"),
            access=_ACCESS,
        )
        self.inst = self.control.up(blank(), TOM,
                                    name="web-test", now=NOW).instance

    def tearDown(self):
        self.tmp.cleanup()

    def route(self):
        return (self.ingress_dir / "web-test.yml").read_text()

    def test_expose_defaults_to_the_envs_own_id(self):
        inst = self.control.expose("web-test", TOM, 8000)
        self.assertEqual(inst.exposures, (Exposure("web-test", 8000),))
        self.assertEqual(inst.url, "https://web-test.sbx.example.com")
        self.assertIn("Host(`web-test.sbx.example.com`)", self.route())

    def test_any_free_name_can_be_chosen(self):
        inst = self.control.expose("web-test", TOM, 8000, name="acme-preview")
        self.assertEqual(inst.exposures, (Exposure("acme-preview", 8000),))
        self.assertIn("Host(`acme-preview.sbx.example.com`)", self.route())
        # its only exposure, so it's the env's URL too
        self.assertEqual(inst.url, "https://acme-preview.sbx.example.com")

    def test_with_several_exposures_the_id_label_is_the_front_door(self):
        self.control.expose("web-test", TOM, 8000)
        inst = self.control.expose("web-test", TOM, 6006, name="storybook")
        self.assertEqual(len(inst.exposures), 2)
        self.assertIn("Host(`storybook.sbx.example.com`)", self.route())
        self.assertEqual(inst.url, "https://web-test.sbx.example.com")

    def test_names_are_globally_unique(self):
        self.control.up(blank(), SUE, name="sue-box", now=NOW)
        self.control.expose("sue-box", SUE, 8000, name="acme-preview")
        with self.assertRaises(NameConflict):
            self.control.expose("web-test", TOM, 8000, name="acme-preview")
        # ...but re-exposing your own name updates it in place.
        inst = self.control.expose("sue-box", SUE, 3000, name="acme-preview")
        self.assertEqual(inst.exposures, (Exposure("acme-preview", 3000),))

    def test_another_envs_id_is_not_a_free_name(self):
        # Every env's default label is its own id; letting someone else claim
        # it would shadow their front door.
        self.control.up(blank(), SUE, name="sue-box", now=NOW)
        with self.assertRaises(NameConflict):
            self.control.expose("web-test", TOM, 8000, name="sue-box")

    def test_the_auth_host_label_is_reserved(self):
        with self.assertRaises(NameConflict):
            self.control.expose("web-test", TOM, 8000, name="auth")

    def test_a_destroyed_env_releases_its_names(self):
        self.control.expose("web-test", TOM, 8000, name="acme-preview")
        self.control.destroy("web-test", TOM)
        self.control.up(blank(), SUE, name="sue-box", now=NOW)
        inst = self.control.expose("sue-box", SUE, 8000, name="acme-preview")
        self.assertEqual(inst.exposures, (Exposure("acme-preview", 8000),))

    def test_access_emails_are_normalized_and_stored(self):
        inst = self.control.expose(
            "web-test", TOM, 8000,
            access=("  Sue@Client.COM ", "sue@client.com", "", "bob@x.io"))
        self.assertEqual(inst.exposures[0].access,
                         ("sue@client.com", "bob@x.io"))

    def test_unexpose_removes_row_route_and_url(self):
        self.control.expose("web-test", TOM, 8000)
        inst = self.control.unexpose("web-test", TOM, "web-test")
        self.assertEqual(inst.exposures, ())
        self.assertIsNone(inst.url)
        self.assertFalse((self.ingress_dir / "web-test.yml").exists())

    def test_unexpose_unknown_name_is_a_404(self):
        with self.assertRaises(NotFound):
            self.control.unexpose("web-test", TOM, "nope")

    def test_expose_is_owner_only(self):
        self.control.share("web-test", TOM, "sue")
        with self.assertRaises(PermissionDenied):
            self.control.expose("web-test", SUE, 8000)
        self.control.expose("web-test", TOM, 8000)
        with self.assertRaises(PermissionDenied):
            self.control.unexpose("web-test", SUE, "web-test")

    def test_expose_never_touches_the_box(self):
        before = list(self.runtime.execs)
        self.control.expose("web-test", TOM, 8000, access=("sue@client.com",))
        self.control.unexpose("web-test", TOM, "web-test")
        self.assertEqual(self.runtime.execs, before)

    def test_bad_names_and_ports_are_rejected(self):
        with self.assertRaises(InvalidName):
            self.control.expose("web-test", TOM, 8000, name="my app")
        with self.assertRaises(CawlError):
            self.control.expose("web-test", TOM, 0)

    def test_exposing_a_stopped_env_persists_but_routes_wait_for_start(self):
        self.control.stop("web-test", TOM)
        inst = self.control.expose("web-test", TOM, 8000)
        self.assertEqual(inst.exposures, (Exposure("web-test", 8000),))
        self.assertFalse((self.ingress_dir / "web-test.yml").exists())
        self.control.start("web-test", TOM, blank())
        self.assertIn("Host(`web-test.sbx.example.com`)", self.route())

    def test_destroy_takes_exposures_with_it(self):
        self.control.expose("web-test", TOM, 8000)
        self.control.destroy("web-test", TOM)
        self.assertEqual(self.control.state.exposures("web-test"), [])
        self.assertFalse((self.ingress_dir / "web-test.yml").exists())


class TestSharing(unittest.TestCase):
    """Sharing is a row in the state store, never a write into the box — so it
    works on a stopped env, can't half-apply, and can't drift out of sync."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = FakeRuntime()
        self.control = ControlPlane(
            state=FakeStateStore(),
            runtime=self.runtime,
            ingress=TraefikIngress(Path(self.tmp.name) / "dynamic", "review.example.com"),
            access=_ACCESS,
        )
        self.inst = self.control.up(blank(), TOM,
                                    name="web-test", now=NOW).instance

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stranger_cannot_see_or_use_the_env(self):
        for op in (lambda: self.control.status("web-test", SUE),
                   lambda: self.control.exec("web-test", SUE, ["ls"])):
            with self.assertRaises(PermissionDenied):
                op()

    def test_sharing_lets_them_see_and_use_it(self):
        self.control.share("web-test", TOM, "sue")
        self.assertEqual(self.control.status("web-test", SUE).id, "web-test")
        self.assertEqual(self.control.exec("web-test", SUE, ["ls"]).exit_code, 0)
        self.assertEqual(self.control.status("web-test", TOM).shared_with, ("sue",))

    def test_sharing_never_touches_the_box(self):
        before = list(self.runtime.execs)
        self.control.share("web-test", TOM, "sue")
        self.control.unshare("web-test", TOM, "sue")
        self.assertEqual(self.runtime.execs, before)

    def test_a_shared_env_shows_up_in_their_ls_but_isnt_double_counted(self):
        self.control.share("web-test", TOM, "sue")
        self.assertEqual([i.id for i in self.control.ls(SUE)], ["web-test"])
        # ...and it's still Tom's env: it counts against his quota, not hers.
        self.assertEqual(self.control.state.count_active("sue"), 0)
        self.assertEqual(self.control.state.count_active("tom"), 1)

    def test_a_grantee_cannot_destroy_it(self):
        """The point of two tiers: sharing an env is not handing it over."""
        self.control.share("web-test", TOM, "sue")
        with self.assertRaises(PermissionDenied):
            self.control.destroy("web-test", SUE)
        self.assertEqual(self.control.status("web-test", SUE).status, Status.ready)

    def test_a_grantee_cannot_re_share_it(self):
        self.control.share("web-test", TOM, "sue")
        with self.assertRaises(PermissionDenied):
            self.control.share("web-test", SUE, "mallory")

    def test_unsharing_takes_the_access_away(self):
        self.control.share("web-test", TOM, "sue")
        self.control.unshare("web-test", TOM, "sue")
        with self.assertRaises(PermissionDenied):
            self.control.status("web-test", SUE)

    def test_unsharing_someone_who_has_no_grant_is_a_404(self):
        with self.assertRaises(NotFound):
            self.control.unshare("web-test", TOM, "sue")

    def test_admins_still_reach_everything(self):
        self.assertEqual(self.control.status("web-test", ADMIN).id, "web-test")
        self.control.destroy("web-test", ADMIN)

    def test_destroying_an_env_takes_its_grants_with_it(self):
        self.control.share("web-test", TOM, "sue")
        self.control.destroy("web-test", TOM)
        self.assertEqual(self.control.state.grants("web-test"), [])


class TestSshCerts(unittest.TestCase):
    """The SSH authorization boundary: the daemon signs a cert for a sandbox only
    for someone `require_access` would let in. The box holds no list of people."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        from test_ssh_ca import cert_body, keygen  # same-dir test helper
        self.cert_body = cert_body
        self.ca = SshCertAuthority(keygen(d / "ca"))
        self.user_pub = keygen(d / "user").with_suffix(".pub").read_text()
        host_pub = keygen(d / "host").with_suffix(".pub").read_text().strip()

        self.runtime = FakeRuntime(host_pubkey=host_pub)
        self.control = ControlPlane(
            state=FakeStateStore(),
            runtime=self.runtime,
            ingress=TraefikIngress(d / "dynamic", "review.example.com"),
            access=_ACCESS,
            ca=self.ca,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def up(self, actor=TOM, **kw):
        return self.control.up(blank(), actor, now=NOW, **kw).instance

    def test_the_box_learns_the_ca_and_its_own_id_as_its_principal(self):
        inst = self.up(name="web-test")
        spec = self.runtime.specs["web-test"]
        self.assertEqual(spec.ssh_ca_pubkey, self.ca.pubkey)
        self.assertEqual(spec.ssh_user, "dev")
        self.assertEqual(inst.ssh, "dev@web-test.tail1.ts.net")

    def test_the_host_key_is_signed_at_create_so_there_is_no_tofu(self):
        self.up(name="web-test")
        installed = [c for _, c in self.runtime.execs
                     if c[0] == "sh" and "ssh_host_ed25519_key-cert.pub" in c[-1]]
        self.assertEqual(len(installed), 1)
        cert = re.search(r"ssh-ed25519-cert-v01@openssh\.com \S+",
                         installed[0][-1]).group(0)
        out = self.cert_body(cert)
        self.assertIn("host certificate", out)
        self.assertIn("web-test.tail1.ts.net", out)

    def test_the_owner_gets_a_cert_for_that_one_box(self):
        self.up(name="web-test")
        access = self.control.ssh("web-test", TOM, self.user_pub)
        self.assertEqual(access.host, "web-test.tail1.ts.net")
        self.assertEqual(access.user, "dev")
        self.assertEqual(access.ca_pubkey, self.ca.pubkey)
        out = self.cert_body(access.certificate)
        self.assertIn("web-test", out)              # principal = the sandbox
        self.assertIn('Key ID: "tom@web-test"', out)  # who, for the audit log

    def test_a_stranger_gets_no_cert(self):
        self.up(name="web-test")
        with self.assertRaises(PermissionDenied):
            self.control.ssh("web-test", SUE, self.user_pub)

    def test_a_grantee_gets_a_cert_and_a_revoked_one_stops_getting_them(self):
        """The whole sharing story, end to end: signing is the enforcement point,
        so revoking a grant closes the door without touching the sandbox."""
        self.up(name="web-test")
        self.control.share("web-test", TOM, "sue")
        access = self.control.ssh("web-test", SUE, self.user_pub)
        self.assertIn('Key ID: "sue@web-test"', self.cert_body(access.certificate))

        self.control.unshare("web-test", TOM, "sue")
        with self.assertRaises(PermissionDenied):
            self.control.ssh("web-test", SUE, self.user_pub)

    def test_a_cert_for_one_box_names_only_that_box(self):
        self.up(name="web-test")
        self.up(name="other-box")
        out = self.cert_body(
            self.control.ssh("web-test", TOM, self.user_pub).certificate)
        self.assertNotIn("other-box", out)

    def test_review_envs_are_ssh_able_like_any_other(self):
        # Boxes are multi-purpose: purpose picks defaults, never capabilities.
        # Access control lives in the signing decision, not in what kind of
        # box it is.
        inst = self.control.up(template(), TOM, now=NOW).instance
        self.assertEqual(inst.ssh, f"dev@{inst.id}.tail1.ts.net")
        self.assertIsNotNone(self.control.ssh(inst.id, TOM, self.user_pub).certificate)

    def test_without_a_ca_there_is_no_ssh_at_all(self):
        control = ControlPlane(
            state=FakeStateStore(), runtime=FakeRuntime(),
            ingress=TraefikIngress(Path(self.tmp.name) / "d2", "review.example.com"),
            access=_ACCESS,  # ca=None
        )
        inst = control.up(blank(), TOM, name="no-ca", now=NOW).instance
        self.assertEqual(control.backends["default"].specs[inst.id].ssh_ca_pubkey, "")
        with self.assertRaises(CawlError):
            control.ssh("no-ca", TOM, self.user_pub)


class TestAccessProviderBoundary(unittest.TestCase):
    """SSH transport is the access provider's call (cawl_core/access.py) — the
    control plane asks it for the boot script and the dial target, and bakes
    neither into itself."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        from test_ssh_ca import cert_body, keygen  # same-dir test helper
        self.cert_body = cert_body
        self.ca = SshCertAuthority(keygen(d / "ca"))
        self.user_pub = keygen(d / "user").with_suffix(".pub").read_text()
        self.host_pub = keygen(d / "host").with_suffix(".pub").read_text().strip()

    def tearDown(self):
        self.tmp.cleanup()

    def control(self, access):
        return ControlPlane(
            state=FakeStateStore(),
            runtime=FakeRuntime(host_pubkey=self.host_pub),
            ingress=TraefikIngress(Path(self.tmp.name) / "dynamic",
                                   "review.example.com"),
            access=access, ca=self.ca,
        )

    def test_the_providers_boot_script_reaches_the_runtime_spec(self):
        control = self.control(_ACCESS)
        control.up(blank(), TOM, name="web-test", now=NOW)
        script = control.backends["default"].specs["web-test"].network_boot
        self.assertIn("tailscale up", script)
        self.assertIn("--hostname=web-test", script)

    def test_bridge_access_dials_the_ip_and_runs_nothing_in_the_box(self):
        control = self.control(BridgeAccess())
        inst = control.up(blank(), TOM, name="web-test", now=NOW).instance
        self.assertEqual(control.backends["default"].specs["web-test"].network_boot, "")
        self.assertEqual(inst.ssh, f"dev@{inst.vm_ip}")
        access = control.ssh("web-test", TOM, self.user_pub)
        self.assertEqual(access.host, inst.vm_ip)
        # The host cert covers what clients dial, whatever form it takes.
        installed = next(c for _, c in control.backends["default"].execs
                         if c[0] == "sh" and "ssh_host_ed25519_key-cert.pub" in c[-1])
        cert = re.search(r"ssh-ed25519-cert-v01@openssh\.com \S+",
                         installed[-1]).group(0)
        self.assertIn(inst.vm_ip, self.cert_body(cert))

    def test_the_providers_jump_rides_along_with_the_cert(self):
        from cawl_core.access import JumpAccess
        control = self.control(JumpAccess("ops@jump.example.com"))
        inst = control.up(blank(), TOM, name="web-test", now=NOW).instance
        access = control.ssh("web-test", TOM, self.user_pub)
        self.assertEqual(access.host, inst.vm_ip)
        self.assertEqual(access.jump, "ops@jump.example.com")
        # Direct providers hand out no hop.
        direct = self.control(BridgeAccess())
        direct.up(blank(), TOM, name="web-2", now=NOW)
        self.assertIsNone(direct.ssh("web-2", TOM, self.user_pub).jump)

    def test_bridge_access_has_no_ssh_target_while_the_box_is_down(self):
        # A tailnet name outlives a shutdown; an IP doesn't — so say so
        # instead of handing out a dead address.
        control = self.control(BridgeAccess())
        control.up(blank(), TOM, name="web-test", now=NOW)
        control.stop("web-test", TOM)
        with self.assertRaises(CawlError):
            control.ssh("web-test", TOM, self.user_pub)

    def _installed_cert(self, control):
        """The most recently installed host certificate, decoded."""
        installed = [c for _, c in control.backends["default"].execs
                     if c[0] == "sh" and "ssh_host_ed25519_key-cert.pub" in c[-1]]
        cert = re.search(r"ssh-ed25519-cert-v01@openssh\.com \S+",
                         installed[-1][-1]).group(0)
        return self.cert_body(cert)

    def test_resume_re_signs_the_host_cert_for_the_new_address(self):
        """An IP-based provider hands out a fresh address on every boot; a host
        cert naming yesterday's address would be a verification failure — the
        key-warning noise the CA exists to eliminate."""
        control = self.control(BridgeAccess())
        old_ip = control.up(blank(), TOM, name="web-test", now=NOW).instance.vm_ip
        self.assertIn(old_ip, self._installed_cert(control))

        control.stop("web-test", TOM)
        inst = control.start("web-test", TOM, blank()).instance
        self.assertNotEqual(inst.vm_ip, old_ip)      # the fake re-leases
        body = self._installed_cert(control)
        self.assertIn(inst.vm_ip, body)              # signed for what's dialed now
        self.assertNotIn(old_ip, body)


class TestSshTargetIsComputedNotStored(unittest.TestCase):
    """`ssh` is presentation: derived from the current access provider on every
    read, never persisted — so it can't go stale when the deployment's provider
    (or a box's address) changes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = FakeStateStore()
        self.runtime = FakeRuntime()
        self.ingress = TraefikIngress(Path(self.tmp.name) / "d", "x.example.com")

    def tearDown(self):
        self.tmp.cleanup()

    def plane(self, access=None):
        return ControlPlane(state=self.state, runtime=self.runtime,
                            ingress=self.ingress, access=access)

    def test_a_provider_change_shows_on_the_next_read(self):
        bridge = self.plane()                        # default BridgeAccess
        inst = bridge.up(blank(), TOM, name="web-test", now=NOW).instance
        self.assertEqual(inst.ssh, f"dev@{inst.vm_ip}")
        # Same state, new deployment-wide provider: no migration, no restart of
        # the box — the next read simply answers with the new transport.
        tail = self.plane(access=_ACCESS)
        self.assertEqual(tail.status("web-test", TOM).ssh,
                         "dev@web-test.tail1.ts.net")
        self.assertEqual(tail.ls(TOM)[0].ssh, "dev@web-test.tail1.ts.net")

    def test_a_stopped_env_advertises_no_ssh_target(self):
        control = self.plane(access=_ACCESS)
        control.up(blank(), TOM, name="web-test", now=NOW)
        control.stop("web-test", TOM)
        # Even under a name-based provider: the name may still resolve, but
        # nothing answers — don't hand out a dead address.
        self.assertIsNone(control.status("web-test", TOM).ssh)
        control.start("web-test", TOM, blank())
        self.assertIsNotNone(control.status("web-test", TOM).ssh)


if __name__ == "__main__":
    unittest.main()
