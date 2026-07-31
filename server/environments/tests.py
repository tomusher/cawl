import tempfile
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.test import Client, TestCase, override_settings
from django.utils import timezone

import environments.services as services
from environments.models import (ApiToken, Environment, EnvironmentEvent,
                              EnvironmentGrant, Exposure, LifecycleOperation,
                              Template, TemplateVersion)

SITE_YAML = __import__("pathlib").Path(
    services.settings.REPO_ROOT) / "examples" / "acme-cms" / "template.yaml"
SITE_BODY = SITE_YAML.read_text()

_INGRESS = tempfile.mkdtemp()


@override_settings(CAWL_RUNTIME="fake", CAWL_INGRESS_DIR=_INGRESS,
                   CAWL_PUBLIC_DOMAIN="review.example.com", CAWL_TAILNET="t.ts.net",
                   CAWL_TAILSCALE_AUTHKEY="tskey-test")
class ApiTestCase(TestCase):
    def setUp(self):
        services._backends = None  # reset the cached fakes between tests
        self.template = Template.objects.create(
            name="acme-cms", params="branch", raw_yaml=SITE_BODY)

    # -- helpers ----------------------------------------------------------
    def token(self, subject, role="user", quota=None):
        _, raw = ApiToken.mint(name=subject, subject=subject, role=role, quota=quota)
        return raw

    def call(self, method, path, raw, data=None):
        c = Client()
        kw = {"HTTP_AUTHORIZATION": f"Bearer {raw}",
              "content_type": "application/json"}
        fn = getattr(c, method)
        return fn(f"/api{path}", data=data, **kw) if data is not None \
            else fn(f"/api{path}", **kw)

    def up(self, raw, branch="main", **kw):
        # the acme-cms template declares `branch` (and `mode`, defaulted)
        return self.call("post", "/environments", raw,
                         {"template": "acme-cms", "args": {"branch": branch}, **kw})

    # -- tokens -----------------------------------------------------------
    def test_token_mint_and_verify(self):
        from environments.auth import authenticate_token
        raw = self.token("tom")
        self.assertEqual(len(ApiToken.objects.filter(subject="tom")), 1)
        ctx = authenticate_token(raw)
        self.assertEqual(ctx.principal.id, "tom")
        self.assertIsNone(authenticate_token("cawl_bogus"))

    def test_token_expiry_and_revoke(self):
        from environments.auth import authenticate_token
        tok, raw = ApiToken.mint(name="x", subject="x", ttl=timedelta(hours=1))
        self.assertIsNotNone(authenticate_token(raw))
        tok.revoked_at = timezone.now()
        tok.save()
        self.assertIsNone(authenticate_token(raw))

    # -- api lifecycle ----------------------------------------------------
    def test_up_creates_owned_environment(self):
        r = self.up(self.token("tom"))
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["owner"], "tom")
        self.assertEqual(body["status"], "ready")
        self.assertTrue(Environment.objects.filter(pk=body["id"]).exists())
        # history recorded: created + status change to ready
        kinds = list(EnvironmentEvent.objects.filter(environment_id=body["id"])
                     .values_list("kind", flat=True))
        self.assertIn("created", kinds)
        self.assertIn("status", kinds)

    def test_up_is_fenced_by_a_durable_lifecycle_operation(self):
        response = self.up(self.token("tom"))
        self.assertEqual(response.status_code, 200, response.content)
        environment = Environment.objects.get(pk=response.json()["id"])
        operation = LifecycleOperation.objects.get(environment=environment)
        self.assertEqual(operation.kind, "provision")
        self.assertEqual(operation.generation, environment.generation)
        self.assertEqual(operation.state, "succeeded")
        self.assertIsNone(environment.active_operation)

    def test_whoami(self):
        r = self.call("get", "/whoami", self.token("tom", role="admin"))
        self.assertEqual(r.json(), {"id": "tom", "role": "admin", "quota": None})

    def test_unauthenticated_rejected(self):
        r = Client().get("/api/environments")
        self.assertEqual(r.status_code, 401)

    def test_session_authenticated_post_requires_csrf(self):
        from django.contrib.auth.models import User

        user = User.objects.create_user("browser-user")
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        # Session auth still works for a safe request without a CSRF token.
        self.assertEqual(client.get("/api/whoami").status_code, 200)
        # But the session cookie alone cannot authorize a state-changing call.
        response = client.post(
            "/api/environments",
            {"template": "acme-cms", "args": {"branch": "main"}},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Environment.objects.filter(owner="browser-user").exists())

    def test_user_lists_only_own(self):
        self.up(self.token("tom"), "a")
        self.up(self.token("sue"), "b")
        tom_ids = {s["owner"] for s in self.call("get", "/environments", self.token("tom")).json()}
        self.assertEqual(tom_ids, {"tom"})
        all_ids = {s["owner"] for s in
                   self.call("get", "/environments", self.token("root", role="admin")).json()}
        self.assertEqual(all_ids, {"tom", "sue"})

    def test_user_cannot_control_others(self):
        sid = self.up(self.token("sue")).json()["id"]
        r = self.call("delete", f"/environments/{sid}", self.token("tom"))
        self.assertEqual(r.status_code, 403)
        # admin can
        r = self.call("delete", f"/environments/{sid}", self.token("root", role="admin"))
        self.assertEqual(r.status_code, 200)
        Environment.objects.get(pk=sid)  # still present (soft-deleted)
        self.assertEqual(Environment.objects.get(pk=sid).status, "destroyed")

    def test_exec_passthrough(self):
        sid = self.up(self.token("tom")).json()["id"]
        r = self.call("post", f"/environments/{sid}/exec", self.token("tom"),
                      {"cmd": ["echo", "hi"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["exit_code"], 0)

    def test_max_ttl_from_token(self):
        """A token minted for agent usage gives every env it creates a
        lifetime, whatever the request says."""
        _, raw = ApiToken.mint(name="agent", subject="agent-1", max_ttl="4h")
        r = self.up(raw, ttl="none")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNotNone(r.json()["expires_at"])

    def test_quota_from_token(self):
        raw = self.token("greedy", quota=1)
        self.assertEqual(self.up(raw, "a").status_code, 200)
        self.assertEqual(self.up(raw, "b").status_code, 409)  # over quota

    def test_on_behalf_of_admin_only(self):
        r = self.up(self.token("tom"), owner="sue")
        self.assertEqual(r.status_code, 403)
        r = self.up(self.token("root", role="admin"), owner="sue")
        self.assertEqual(r.json()["owner"], "sue")

    def test_soft_delete_excluded_from_reuse(self):
        a = self.up(self.token("tom"), "x", reuse=True).json()["id"]
        self.call("delete", f"/environments/{a}", self.token("tom"))
        b = self.up(self.token("tom"), "x", reuse=True).json()["id"]
        self.assertNotEqual(a, b)  # destroyed one not reused

    # -- custom names -----------------------------------------------------
    def test_up_with_custom_name(self):
        body = self.up(self.token("tom"), name="Web-Test").json()
        self.assertEqual(body["id"], "web-test")
        self.assertEqual(body["ssh"], "dev@web-test.t.ts.net")

    def test_invalid_name_is_400(self):
        r = self.up(self.token("tom"), name="my env")
        self.assertEqual(r.status_code, 400)
        self.assertIn("invalid name", r.json()["error"])

    def test_name_conflict_is_409(self):
        self.up(self.token("tom"), name="web-test")
        r = self.up(self.token("sue"), name="web-test")
        self.assertEqual(r.status_code, 409)
        self.assertIn("already in use", r.json()["error"])
        self.assertNotIn("tom", r.json()["error"])  # don't leak the holder

    def test_destroyed_name_can_be_reclaimed_without_inheriting_access(self):
        tom = self.token("tom")
        first = self.up(tom, "a", name="web-test").json()
        self.call("post", "/environments/web-test/grants", tom,
                  {"principal": "carol"})
        self.call("post", "/environments/web-test/exposures", tom,
                  {"port": 3000, "name": "preview",
                   "access": ["outsider@example.com"]})
        self.call("delete", "/environments/web-test", tom)

        # Grants and exposures are capabilities, so teardown revokes them even
        # though the environment and its event history are retained.
        self.assertFalse(EnvironmentGrant.objects.filter(
            environment_id="web-test").exists())
        self.assertFalse(Exposure.objects.filter(environment_id="web-test").exists())

        # A new template version lands between the two envs.
        self.template.version = 2
        self.template.save()

        second = self.up(self.token("sue"), "b", name="web-test").json()
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["status"], "ready")
        self.assertEqual(second["owner"], "sue")  # the row is the new env's, wholesale
        self.assertEqual(second["shared_with"], [])
        self.assertEqual(
            [(e["name"], e["access"]) for e in second["exposures"]],
            [("web-test", [])])
        self.assertEqual(
            self.call("get", "/environments/web-test", self.token("carol")).status_code,
            403)

        sb = Environment.objects.get(pk="web-test")
        self.assertEqual(sb.args, {"branch": "b", "mode": "dev"})
        self.assertIsNone(sb.destroyed_at)
        self.assertEqual(sb.template_version, 2)  # re-pinned, not inherited from v1

        # The row's history spans both envs: two creates, one destroy in between.
        kinds = list(EnvironmentEvent.objects.filter(environment_id="web-test")
                     .order_by("id").values_list("kind", flat=True))
        self.assertEqual(kinds.count("created"), 2)
        self.assertIn("destroyed", kinds)

    def test_reuse_with_name_returns_same_env(self):
        raw = self.token("tom")
        a = self.up(raw, "a", name="web-test", reuse=True).json()
        b = self.up(raw, "b", name="web-test", reuse=True).json()
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(b["args"], {"branch": "a", "mode": "dev"})  # the existing env, not a new one
        self.assertEqual(Environment.objects.count(), 1)

    def test_reap_command(self):
        from django.core.management import call_command
        from io import StringIO
        sid = self.up(self.token("tom"), ttl="1h").json()["id"]
        Environment.objects.filter(pk=sid).update(
            expires_at=timezone.now() - timedelta(hours=1))
        out = StringIO()
        call_command("reap", stdout=out)
        self.assertIn(sid, out.getvalue())
        self.assertEqual(Environment.objects.get(pk=sid).status, "destroyed")

    # -- templates --------------------------------------------------------
    def test_up_pins_template_version(self):
        sid = self.up(self.token("tom")).json()["id"]
        self.assertEqual(Environment.objects.get(pk=sid).template_version, 1)

    def test_template_create_is_personal_for_users_and_global_for_admins(self):
        body = {"yaml": "name: newsite\nparams:\n  branch:\n    default: main\n"}
        r = self.call("post", "/templates", self.token("tom"), body)
        self.assertEqual(r.status_code, 200, r.content)
        out = r.json()
        self.assertEqual(
            (out["name"], out["owner"], out["scope"], out["params"],
             out["version"], out["active"]),
            ("newsite", "tom", "personal", "branch", 1, True))

        global_body = {"yaml": "name: publicsite\n", "scope": "global"}
        r = self.call("post", "/templates", self.token("root", role="admin"),
                      global_body)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["owner"], r.json()["scope"]), ("", "global"))

    def test_personal_templates_are_private_and_cannot_shadow_globals(self):
        body = {"yaml": "name: private-site\n"}
        self.assertEqual(self.call("post", "/templates", self.token("tom"), body).status_code, 200)
        self.assertEqual(self.call("get", "/templates/private-site", self.token("jane")).status_code, 404)
        self.assertNotIn("private-site", [
            t["name"] for t in self.call("get", "/templates", self.token("jane")).json()])
        self.assertEqual(self.call("post", "/environments", self.token("jane"),
                                   {"template": "private-site"}).status_code, 404)
        self.assertEqual(self.call("post", "/templates", self.token("jane"), body).status_code, 409)

    def test_global_templates_require_admin(self):
        r = self.call("post", "/templates", self.token("tom"),
                      {"yaml": "name: no-public\n", "scope": "global"})
        self.assertEqual(r.status_code, 403)

    def test_template_create_validates_yaml(self):
        # missing required `name:` key => 400 from the config parser
        r = self.call("post", "/templates", self.token("root", role="admin"),
                      {"yaml": "image: cawl/base\n"})
        self.assertEqual(r.status_code, 400)

    def test_template_create_rejects_a_hook_with_an_undeclared_arg(self):
        r = self.call("post", "/templates", self.token("root", role="admin"),
                      {"yaml": "name: bad\nhooks:\n  provision: git checkout {{ nope }}\n"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("nope", r.json()["error"])

    def test_template_reupload_versions(self):
        admin = self.token("root", role="admin")
        r1 = self.call("post", "/templates", admin, {"yaml": "name: verst\n"})
        self.assertEqual(r1.json()["version"], 1)
        self.assertEqual(r1.json()["params"], "")
        r2 = self.call("post", "/templates", admin,
                       {"yaml": "name: verst\nparams:\n  tier:\n    default: small\n"})
        self.assertEqual(r2.json()["version"], 2)
        self.assertEqual(r2.json()["params"], "tier")
        self.assertEqual(Template.objects.get(name="verst").params, "tier")
        self.assertEqual(TemplateVersion.objects.filter(
            template__name="verst").count(), 2)

    def test_unknown_arg_is_400(self):
        r = self.call("post", "/environments", self.token("tom"),
                      {"template": "acme-cms", "args": {"bogus": "1"}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("unknown arg", r.json()["error"])

    def test_template_show_returns_body(self):
        r = self.call("get", "/templates/acme-cms", self.token("tom"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("acme-cms", r.json()["yaml"])

    def test_template_rm_deactivates_and_blocks_up(self):
        r = self.call("delete", "/templates/acme-cms", self.token("root", role="admin"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Template.objects.get(name="acme-cms").active)
        # up on an inactive template 404s
        self.assertEqual(self.up(self.token("tom")).status_code, 404)


class CliLoginTestCase(TestCase):
    CB = "http://127.0.0.1:54321/"

    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user("tom", password="x")
        self.staff = User.objects.create_user(
            "staff", password="x", is_staff=True)
        self.admin = User.objects.create_superuser("root", password="x")

    def test_unauthenticated_redirects_to_login(self):
        r = Client().get(f"/cli/login?callback={self.CB}&state=abc")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login", r["Location"])

    def test_get_shows_confirm_page(self):
        c = Client(); c.force_login(self.user)
        r = c.get(f"/cli/login?callback={self.CB}&state=abc")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Authorize")
        self.assertContains(r, "tom")

    def test_post_mints_token_and_redirects(self):
        from environments.auth import authenticate_token
        c = Client(); c.force_login(self.user)
        r = c.post("/cli/login", {"callback": self.CB, "state": "xyz"})
        self.assertEqual(r.status_code, 302)
        loc = urlsplit(r["Location"])
        q = parse_qs(loc.query)
        self.assertEqual(q["state"], ["xyz"])
        token = q["token"][0]
        ctx = authenticate_token(token)               # the token actually works
        self.assertEqual(ctx.principal.id, "tom")
        self.assertEqual(ctx.principal.role.value, "user")

    def test_admin_gets_admin_role(self):
        from environments.auth import authenticate_token
        c = Client(); c.force_login(self.admin)
        r = c.post("/cli/login", {"callback": self.CB, "state": "s"})
        token = parse_qs(urlsplit(r["Location"]).query)["token"][0]
        self.assertEqual(authenticate_token(token).principal.role.value, "admin")

    def test_staff_without_superuser_gets_user_role(self):
        from environments.auth import authenticate_token
        c = Client(); c.force_login(self.staff)
        r = c.post("/cli/login", {"callback": self.CB, "state": "s"})
        token = parse_qs(urlsplit(r["Location"]).query)["token"][0]
        self.assertEqual(authenticate_token(token).principal.role.value, "user")

    def test_non_loopback_callback_rejected(self):
        c = Client(); c.force_login(self.user)
        r = c.get("/cli/login?callback=https://evil.example/&state=s")
        self.assertEqual(r.status_code, 400)

    def test_headless_get_shows_confirm(self):
        c = Client(); c.force_login(self.user)
        r = c.get("/cli/login")  # no callback => headless
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Authorize")

    def test_headless_post_displays_working_token(self):
        from environments.auth import authenticate_token
        c = Client(); c.force_login(self.user)
        r = c.post("/cli/login")  # no callback
        self.assertEqual(r.status_code, 200)          # page, not a redirect
        self.assertContains(r, "Copy this code")
        # extract the token shown in the <code> block and confirm it works
        import re
        m = re.search(r"(cawl_[A-Za-z0-9_-]+)", r.content.decode())
        self.assertIsNotNone(m)
        self.assertEqual(authenticate_token(m.group(1)).principal.id, "tom")


# -- sharing and SSH certificates ------------------------------------------
_CA_DIR = tempfile.mkdtemp()


def _keygen(name):
    import subprocess
    p = __import__("pathlib").Path(_CA_DIR) / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(p)],
                   check=True, capture_output=True)
    return p


_CA_KEY = _keygen("ca")
_USER_PUB = _keygen("user").with_suffix(".pub").read_text()
_HOST_PUB = _keygen("host").with_suffix(".pub").read_text().strip()


@override_settings(CAWL_RUNTIME="fake", CAWL_INGRESS_DIR=_INGRESS,
                   CAWL_PUBLIC_DOMAIN="review.example.com", CAWL_TAILNET="t.ts.net",
                   CAWL_TAILSCALE_AUTHKEY="tskey-test",
                   CAWL_SSH_CA_KEY=str(_CA_KEY))
class SharingApiTestCase(ApiTestCase):
    """Ad-hoc sharing, end to end over the API.

    The through-line: a grant is a row here, and the *only* thing it changes is
    whether the daemon will sign an SSH certificate. No environment is ever touched.

    Subclassing ApiTestCase is deliberate — it re-runs the whole API suite with a
    CA configured, so the create-time host-cert signing has to keep working
    through every lifecycle path (reuse, name reclaim, teardown), not just the
    ones below.
    """

    def setUp(self):
        super().setUp()
        from cawl_core.runtime.fake import FakeRuntime
        # The fake box answers the host-key read, so create-time host-cert signing
        # runs for real.
        services._backends = {"container": FakeRuntime(host_pubkey=_HOST_PUB),
                              "vm": FakeRuntime(host_pubkey=_HOST_PUB, vm=True)}

    def own(self, subject="tom"):
        return self.up(self.token(subject), name="web-test").json()["id"]

    def test_owner_can_ssh_and_a_stranger_cannot(self):
        sid = self.own()
        r = self.call("post", f"/environments/{sid}/ssh-cert", self.token("tom"),
                      {"public_key": _USER_PUB})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["host"], "web-test.t.ts.net")
        self.assertEqual(body["user"], "dev")
        self.assertIn("ssh-ed25519-cert-v01@openssh.com", body["certificate"])
        self.assertIn("ssh-ed25519", body["ca_pubkey"])

        r = self.call("post", f"/environments/{sid}/ssh-cert", self.token("sue"),
                      {"public_key": _USER_PUB})
        self.assertEqual(r.status_code, 403)

    def test_sharing_lets_them_ssh_and_unsharing_stops_it(self):
        sid = self.own()
        tom, sue = self.token("tom"), self.token("sue")

        r = self.call("post", f"/environments/{sid}/grants", tom, {"principal": "sue"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["shared_with"], ["sue"])

        self.assertEqual(
            self.call("post", f"/environments/{sid}/ssh-cert", sue,
                      {"public_key": _USER_PUB}).status_code, 200)
        self.assertEqual(self.call("get", f"/environments/{sid}", sue).status_code, 200)
        self.assertEqual([s["id"] for s in self.call("get", "/environments", sue).json()],
                         [sid])

        self.assertEqual(
            self.call("delete", f"/environments/{sid}/grants/sue", tom).status_code, 200)
        self.assertEqual(
            self.call("post", f"/environments/{sid}/ssh-cert", sue,
                      {"public_key": _USER_PUB}).status_code, 403)

    def test_a_grantee_cannot_destroy_the_env(self):
        sid = self.own()
        self.call("post", f"/environments/{sid}/grants", self.token("tom"),
                  {"principal": "sue"})
        r = self.call("delete", f"/environments/{sid}", self.token("sue"))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Environment.objects.get(pk=sid).status, "ready")

    def test_only_the_owner_can_share(self):
        sid = self.own()
        r = self.call("post", f"/environments/{sid}/grants", self.token("sue"),
                      {"principal": "mallory"})
        self.assertEqual(r.status_code, 403)

    def test_sharing_is_recorded_in_the_history(self):
        sid = self.own()
        tom = self.token("tom")
        self.call("post", f"/environments/{sid}/grants", tom, {"principal": "sue"})
        self.call("post", f"/environments/{sid}/ssh-cert", tom, {"public_key": _USER_PUB})
        self.call("delete", f"/environments/{sid}/grants/sue", tom)
        kinds = list(EnvironmentEvent.objects.filter(environment_id=sid)
                     .values_list("kind", "actor", "detail"))
        self.assertIn(("shared", "tom", "sue"), kinds)
        self.assertIn(("unshared", "", "sue"), kinds)
        self.assertIn(("ssh-cert", "tom", ""), kinds)

    def test_a_bad_public_key_is_rejected(self):
        sid = self.own()
        r = self.call("post", f"/environments/{sid}/ssh-cert", self.token("tom"),
                      {"public_key": "definitely-not-a-key"})
        self.assertEqual(r.status_code, 400)


@override_settings(CAWL_RUNTIME="fake", CAWL_INGRESS_DIR=_INGRESS,
                   CAWL_PUBLIC_DOMAIN="review.example.com", CAWL_TAILNET="t.ts.net",
                   CAWL_TAILSCALE_AUTHKEY="tskey-test")
class StopStartApiTestCase(TestCase):
    """Pause without deleting, over the API."""

    def setUp(self):
        services._backends = None
        self.template = Template.objects.create(
            name="acme-cms", params="branch", raw_yaml=SITE_BODY)
        TemplateVersion.objects.create(
            template=self.template, version=1, raw_yaml=SITE_BODY, params="branch")

    token = ApiTestCase.token
    call = ApiTestCase.call
    up = ApiTestCase.up

    def test_stop_then_start_round_trip(self):
        tom = self.token("tom")
        sid = self.up(tom, name="web-test").json()["id"]

        r = self.call("post", f"/environments/{sid}/stop", tom)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "stopped")
        self.assertIsNone(r.json()["vm_ip"])          # no address while it's down
        self.assertEqual(Environment.objects.get(pk=sid).status, "stopped")  # not deleted

        r = self.call("post", f"/environments/{sid}/start", tom)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "ready")
        self.assertIsNotNone(r.json()["vm_ip"])
        self.assertEqual(r.json()["ssh"], "dev@web-test.t.ts.net")   # identity kept

    def test_a_stopped_env_still_lists(self):
        tom = self.token("tom")
        sid = self.up(tom).json()["id"]
        self.call("post", f"/environments/{sid}/stop", tom)
        listed = {s["id"]: s["status"] for s in self.call("get", "/environments", tom).json()}
        self.assertEqual(listed.get(sid), "stopped")

    def test_the_pause_is_recorded_in_the_history(self):
        tom = self.token("tom")
        sid = self.up(tom).json()["id"]
        self.call("post", f"/environments/{sid}/stop", tom)
        self.call("post", f"/environments/{sid}/start", tom)
        transitions = list(EnvironmentEvent.objects.filter(environment_id=sid, kind="status")
                           .values_list("from_status", "to_status"))
        self.assertIn(("ready", "stopped"), transitions)
        self.assertIn(("stopped", "ready"), transitions)

    def test_a_grantee_can_use_it_but_not_stop_it(self):
        tom = self.token("tom")
        sid = self.up(tom).json()["id"]
        self.call("post", f"/environments/{sid}/grants", tom, {"principal": "sue"})
        r = self.call("post", f"/environments/{sid}/stop", self.token("sue"))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Environment.objects.get(pk=sid).status, "ready")

    def test_a_stranger_cannot_stop_it(self):
        sid = self.up(self.token("tom")).json()["id"]
        r = self.call("post", f"/environments/{sid}/stop", self.token("mallory"))
        self.assertEqual(r.status_code, 403)

    def test_starting_a_running_env_is_a_no_op_not_an_error(self):
        tom = self.token("tom")
        sid = self.up(tom).json()["id"]
        r = self.call("post", f"/environments/{sid}/start", tom)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ready")

    def test_resume_uses_the_template_version_the_env_launched_from(self):
        """Stopping and starting must give you back the env you stopped — not
        silently migrate it onto a template someone has since rewritten."""
        tom = self.token("tom")
        sid = self.up(tom).json()["id"]
        self.call("post", f"/environments/{sid}/stop", tom)

        self.template.raw_yaml = SITE_BODY.replace("acme-cms", "acme-cms")
        self.template.version = 2
        self.template.save()
        TemplateVersion.objects.create(
            template=self.template, version=2,
            raw_yaml=SITE_BODY + "\n# v2 rewrite\n", params="branch")

        self.assertEqual(
            self.call("post", f"/environments/{sid}/start", tom).status_code, 200)
        backend = Environment.objects.get(pk=sid).backend
        spec = services.get_backends()[backend].specs[sid]
        self.assertNotIn("v2 rewrite", spec.provision)   # still on v1, as pinned


@override_settings(CAWL_RUNTIME="fake", CAWL_INGRESS_DIR=tempfile.mkdtemp(),
                   CAWL_PUBLIC_DOMAIN="review.example.com",
                   CAWL_AUTH_HOST="auth.review.example.com",
                   CAWL_TAILNET="t.ts.net")
class ExposureTestCase(TestCase):
    """The exposure API plus the browser flow: forward-auth -> magic link ->
    code-for-cookie handoff -> allowed."""

    def setUp(self):
        services._backends = None
        self.template = Template.objects.create(
            name="acme-cms", params="branch", raw_yaml=SITE_BODY)
        _, self.tom = ApiToken.mint(name="tom", subject="tom")
        r = self._call("post", "/environments", self.tom,
                       {"template": "acme-cms", "args": {}, "name": "web-test"})
        assert r.status_code == 200, r.content
        self.host = "web-test.review.example.com"

    def _call(self, method, path, raw, data=None):
        c = Client()
        kw = {"HTTP_AUTHORIZATION": f"Bearer {raw}",
              "content_type": "application/json"}
        fn = getattr(c, method)
        return fn(f"/api{path}", data=data, **kw) if data is not None \
            else fn(f"/api{path}", **kw)

    def forward(self, client, host=None, uri="/"):
        return client.get("/auth/forward",
                          HTTP_X_FORWARDED_HOST=host or self.host,
                          HTTP_X_FORWARDED_PROTO="https",
                          HTTP_X_FORWARDED_URI=uri)

    def sign_in(self, client, email, next_url=None):
        """Run the whole dance for `email`, as a browser would."""
        from environments.webauth import magic_link
        link = magic_link(email, next_url or f"https://{self.host}/")
        q = urlsplit(link)
        r = client.get(f"{q.path}?{q.query}")           # /auth/magic on auth host
        self.assertEqual(r.status_code, 302)
        cb = urlsplit(r["Location"])                    # https://<host>/.cawl/callback
        self.assertEqual(cb.hostname, urlsplit(next_url or f"https://{self.host}/").hostname)
        r = client.get(f"{cb.path}?{cb.query}", HTTP_HOST=cb.hostname)
        self.assertEqual(r.status_code, 302)            # -> the original path
        return r

    # -- the API ----------------------------------------------------------
    def test_template_default_exposure_is_live_with_a_url(self):
        body = self._call("get", "/environments/web-test", self.tom).json()
        self.assertEqual(body["url"], f"https://{self.host}")
        self.assertEqual(body["exposures"][0]["name"], "web-test")
        self.assertEqual(body["exposures"][0]["url"], f"https://{self.host}")

    def test_expose_builds_the_control_plane_once(self):
        from unittest.mock import patch

        with patch("environments.api.build_control",
                   wraps=services.build_control) as build:
            response = self._call(
                "post", "/environments/web-test/exposures", self.tom,
                {"port": 6006, "name": "single-control"})
        self.assertEqual(response.status_code, 200, response.content)
        build.assert_called_once_with()

    def test_expose_returns_url_and_a_link_per_access_email(self):
        # Any free label works as the name — it needn't mention the VM at all.
        r = self._call("post", "/environments/web-test/exposures", self.tom,
                       {"port": 6006, "name": "acme-storybook",
                        "access": ["Sue@Client.com"]})
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["url"], "https://acme-storybook.review.example.com")
        self.assertEqual(body["access"], ["sue@client.com"])
        link = body["links"]["sue@client.com"]
        self.assertIn("auth.review.example.com/auth/magic", link)

    def test_a_taken_name_is_a_409(self):
        self._call("post", "/environments", self.tom,
                   {"template": "acme-cms", "args": {}, "name": "other-box"})
        r = self._call("post", "/environments/other-box/exposures", self.tom,
                       {"port": 6006, "name": "web-test"})  # another env's id
        self.assertEqual(r.status_code, 409)
        self._call("post", "/environments/web-test/exposures", self.tom,
                   {"port": 6006, "name": "preview"})
        r = self._call("post", "/environments/other-box/exposures", self.tom,
                       {"port": 6006, "name": "preview"})
        self.assertEqual(r.status_code, 409)

    def test_exposure_name_has_a_database_backed_global_constraint(self):
        from django.db import IntegrityError, transaction
        from environments.models import Exposure

        self._call("post", "/environments", self.tom,
                   {"template": "acme-cms", "args": {}, "name": "other-box"})
        with self.assertRaises(IntegrityError), transaction.atomic():
            Exposure.objects.create(
                environment_id="other-box", name="web-test", port=9999)

    def test_expose_is_owner_only(self):
        _, sue = ApiToken.mint(name="sue", subject="sue")
        r = self._call("post", "/environments/web-test/exposures", sue,
                       {"port": 9999})
        self.assertEqual(r.status_code, 403)

    def test_unexpose(self):
        r = self._call("delete", "/environments/web-test/exposures/web-test", self.tom)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["exposures"], [])
        self.assertIsNone(r.json()["url"])

    # -- the browser flow --------------------------------------------------
    def test_unknown_host_is_404(self):
        r = self.forward(Client(), host="nope.review.example.com")
        self.assertEqual(r.status_code, 404)

    def test_no_cookie_redirects_to_the_auth_host(self):
        r = self.forward(Client(), uri="/admin/?q=1")
        self.assertEqual(r.status_code, 302)
        u = urlsplit(r["Location"])
        self.assertEqual(u.hostname, "auth.review.example.com")
        self.assertEqual(parse_qs(u.query)["next"],
                         [f"https://{self.host}/admin/?q=1"])

    def test_access_list_email_gets_in_without_creating_a_django_user(self):
        from django.contrib.auth.models import User

        self._call("post", "/environments/web-test/exposures", self.tom,
                   {"port": 8000, "access": ["sue@client.com"]})
        c = Client()
        self.sign_in(c, "sue@client.com")
        r = self.forward(c)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["X-Cawl-User"], "sue@client.com")
        self.assertFalse(User.objects.filter(email="sue@client.com").exists())
        self.assertFalse(User.objects.filter(username="sue@client.com").exists())

    def test_magic_link_is_one_time_and_scoped_to_its_host(self):
        from environments.webauth import magic_link

        link = magic_link("sue@client.com", f"https://{self.host}/")
        q = urlsplit(link)
        path = f"{q.path}?{q.query}"
        c = Client()
        self.assertEqual(c.get(path).status_code, 302)
        self.assertEqual(Client().get(path).status_code, 410)
        # The viewer SSO cookie inherits the token's host scope.
        r = c.get("/auth/sso", {
            "next": "https://other.review.example.com/",
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Sign in")

        other = magic_link("sue@client.com", f"https://{self.host}/")
        q = urlsplit(other)
        params = parse_qs(q.query)
        r = Client().get(q.path, {
            "token": params["token"][0],
            "next": "https://other.review.example.com/",
        })
        self.assertEqual(r.status_code, 410)

    def test_magic_link_for_a_superuser_does_not_create_a_django_session(self):
        from django.contrib.auth.models import User
        from environments.webauth import SSO_COOKIE, magic_link

        User.objects.create_superuser("root", email="boss@corp.com", password="x")
        link = magic_link("boss@corp.com", f"https://{self.host}/")
        q = urlsplit(link)
        c = Client()
        r = c.get(f"{q.path}?{q.query}")

        self.assertEqual(r.status_code, 302)
        self.assertIn(SSO_COOKIE, r.cookies)
        self.assertNotIn("_auth_user_id", c.session)
        self.assertEqual(c.get("/admin/").status_code, 302)

    def test_existing_django_session_short_circuits_viewer_login(self):
        from django.contrib.auth.models import User

        user = User.objects.create_user("tom", email="tom@example.com")
        c = Client()
        c.force_login(user)
        r = c.get("/auth/sso", {"next": f"https://{self.host}/"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"https://{self.host}/.cawl/callback", r["Location"])

    def test_env_access_admits_the_owner_without_an_access_list(self):
        from django.contrib.auth.models import User
        User.objects.create_user("tom", email="tom@example.com")
        c = Client()
        self.sign_in(c, "tom@example.com")
        self.assertEqual(self.forward(c).status_code, 200)

    def test_a_signed_in_stranger_is_403_not_302(self):
        # Authenticated (someone gave them a link to another env once) but not
        # authorized for this one.
        self._call("post", "/environments/web-test/exposures", self.tom,
                   {"port": 6006, "name": "storybook",
                    "access": ["sue@client.com"]})
        c = Client()
        self.sign_in(c, "sue@client.com",
                     next_url="https://storybook.review.example.com/")
        r = self.forward(c, host="storybook.review.example.com")
        self.assertEqual(r.status_code, 200)
        self._call("delete", "/environments/web-test/exposures/storybook", self.tom)
        self._call("post", "/environments/web-test/exposures", self.tom,
                   {"port": 6006, "name": "storybook", "access": []})
        r = self.forward(c, host="storybook.review.example.com")
        self.assertEqual(r.status_code, 403)

    def test_revoking_an_email_locks_them_out_on_the_next_request(self):
        self._call("post", "/environments/web-test/exposures", self.tom,
                   {"port": 8000, "access": ["sue@client.com"]})
        c = Client()
        self.sign_in(c, "sue@client.com")
        self.assertEqual(self.forward(c).status_code, 200)
        self._call("post", "/environments/web-test/exposures", self.tom,
                   {"port": 8000, "access": []})
        self.assertEqual(self.forward(c).status_code, 403)

    def test_a_cookie_for_one_host_cannot_be_replayed_against_another(self):
        """The cross-VM harvest scenario: an app inside a VM sees its visitors'
        host-scoped cookies; they must be useless anywhere else."""
        self._call("post", "/environments/web-test/exposures", self.tom,
                   {"port": 8000, "access": ["sue@client.com"]})
        self._call("post", "/environments/web-test/exposures", self.tom,
                   {"port": 6006, "name": "storybook",
                    "access": ["sue@client.com"]})
        c = Client()
        self.sign_in(c, "sue@client.com")                 # cookie names web-test host
        # Replay the same cookie against the storybook host: unauthenticated.
        r = self.forward(c, host="storybook.review.example.com")
        self.assertEqual(r.status_code, 302)

    def test_the_handoff_code_is_bound_to_the_host(self):
        from environments.webauth import magic_link
        link = magic_link("sue@client.com", f"https://{self.host}/")
        self._call("post", "/environments/web-test/exposures", self.tom,
                   {"port": 8000, "access": ["sue@client.com"]})
        c = Client()
        q = urlsplit(link)
        r = c.get(f"{q.path}?{q.query}")
        cb = urlsplit(r["Location"])
        # Redeem the code on a *different* exposed host: refused.
        r = c.get(f"{cb.path}?{cb.query}", HTTP_HOST="storybook.review.example.com")
        self.assertEqual(r.status_code, 400)

    def test_expired_magic_link_is_a_polite_410(self):
        c = Client()
        r = c.get("/auth/magic?token=garbage&next=https://x/")
        self.assertEqual(r.status_code, 410)

    def test_login_form_never_reveals_whether_an_email_has_access(self):
        c = Client()
        for email in ("sue@client.com", "stranger@nowhere.io"):
            r = c.post("/auth/login",
                       {"email": email, "next": f"https://{self.host}/"})
            self.assertEqual(r.status_code, 200)
            self.assertIn(b"Check your email", r.content)


class AccessSelectionTestCase(TestCase):
    """CAWL_ACCESS names the SSH access provider; unset, it's inferred from
    which settings are present. Naming a provider whose settings are missing
    is a startup error, never a silent fallback to a different transport."""

    def build(self, **overrides):
        base = dict(CAWL_ACCESS="", CAWL_TAILSCALE_AUTHKEY="", CAWL_SSH_JUMP="")
        base.update(overrides)
        with override_settings(**base):
            return services.build_access()

    def test_inference_authkey_then_jump_then_bridge(self):
        from cawl_core.access import BridgeAccess, JumpAccess, TailscaleAccess
        self.assertIsInstance(
            self.build(CAWL_TAILSCALE_AUTHKEY="tskey-x", CAWL_SSH_JUMP="j"),
            TailscaleAccess)  # authkey wins when both are set
        self.assertIsInstance(self.build(CAWL_SSH_JUMP="jump.example.com"),
                              JumpAccess)
        self.assertIsInstance(self.build(), BridgeAccess)

    def test_explicit_choice_overrides_inference(self):
        from cawl_core.access import BridgeAccess
        # An authkey lying around no longer drags in Tailscale.
        self.assertIsInstance(
            self.build(CAWL_ACCESS="bridge", CAWL_TAILSCALE_AUTHKEY="tskey-x"),
            BridgeAccess)

    def test_naming_a_provider_without_its_settings_is_a_config_error(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            self.build(CAWL_ACCESS="tailscale")
        with self.assertRaises(ImproperlyConfigured):
            self.build(CAWL_ACCESS="jump")
        with self.assertRaises(ImproperlyConfigured):
            self.build(CAWL_ACCESS="wireguard")  # not a provider

    def test_a_dotted_path_installs_a_third_party_provider(self):
        # "Installation" is Python packaging: any importable AccessProvider
        # subclass can be named. Stands in for an operator's own package.
        from cawl_core.access import BridgeAccess
        self.assertIsInstance(self.build(CAWL_ACCESS="cawl_core.access.BridgeAccess"),
                              BridgeAccess)

    def test_a_dotted_path_that_is_not_a_provider_is_a_config_error(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            self.build(CAWL_ACCESS="cawl_core.control.ControlPlane")  # wrong kind
        with self.assertRaises(ImproperlyConfigured):
            self.build(CAWL_ACCESS="no_such_module.Provider")


class BackendSelectionTestCase(TestCase):
    """The backend registry is open: CAWL_RUNTIME picks the built-in family,
    CAWL_EXTRA_BACKENDS merges in operator-installed Runtime subclasses by
    dotted path. Misconfiguration is a startup error, never a silent Incus."""

    def build(self, **overrides):
        base = dict(CAWL_RUNTIME="fake", CAWL_EXTRA_BACKENDS="")
        base.update(overrides)
        services._backends = None  # defeat the per-process cache
        try:
            with override_settings(**base):
                return services.get_backends()
        finally:
            services._backends = None

    def test_unknown_or_removed_family_is_a_config_error(self):
        from django.core.exceptions import ImproperlyConfigured
        for runtime in ("incus", "incus-api"):
            with self.subTest(runtime=runtime), self.assertRaises(ImproperlyConfigured):
                self.build(CAWL_RUNTIME=runtime)

    def test_incus_api_requires_a_pinned_server_certificate(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaisesRegex(ImproperlyConfigured,
                                    "requires CAWL_INCUS_SERVER_CERT"):
            self.build(CAWL_RUNTIME="incus_api", CAWL_INCUS_SERVER_CERT=None)

    def test_extra_backends_merge_over_the_builtins(self):
        from cawl_core.runtime.fake import FakeRuntime
        got = self.build(
            CAWL_EXTRA_BACKENDS="firecracker=cawl_core.runtime.fake.FakeRuntime")
        self.assertEqual(set(got), {"container", "vm", "firecracker"})
        self.assertIsInstance(got["firecracker"], FakeRuntime)

    def test_none_family_runs_only_installed_backends(self):
        got = self.build(
            CAWL_RUNTIME="none",
            CAWL_EXTRA_BACKENDS="mine=cawl_core.runtime.fake.FakeRuntime")
        self.assertEqual(set(got), {"mine"})

    def test_malformed_or_wrong_kind_entries_are_config_errors(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            self.build(CAWL_EXTRA_BACKENDS="not-a-pair")
        with self.assertRaises(ImproperlyConfigured):
            self.build(CAWL_EXTRA_BACKENDS="x=cawl_core.access.BridgeAccess")
