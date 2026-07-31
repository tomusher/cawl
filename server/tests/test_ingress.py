import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

import yaml

from cawl_core.ingress import TraefikIngress
from cawl_core.models import Exposure, Environment, Status


def inst(exposures=(Exposure("acme-review-a1", 8000),), ip="10.0.0.7"):
    return Environment(
        id="acme-review-a1", template="acme", args={"branch": "feature/x"},
        owner="tom", status=Status.ready, vm_ip=ip,
        created_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        exposures=tuple(exposures),
    )


def ingress(d, **kw):
    kw.setdefault("forward_auth_url", "http://127.0.0.1:8000/auth/forward")
    kw.setdefault("daemon_url", "http://127.0.0.1:8000")
    kw.setdefault("auth_host", "auth.sbx.example.com")
    return TraefikIngress(d, "sbx.example.com", **kw)


class TestIngress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ing = ingress(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, name):
        return yaml.safe_load((self.dir / name).read_text())

    def test_sync_writes_router_and_service_per_exposure(self):
        # Labels are free-form hostnames — the id-shaped one is just a default.
        self.ing.sync(inst([Exposure("acme-review-a1", 8000),
                            Exposure("acme-preview", 8025)]))
        http = self.read("acme-review-a1.yml")["http"]
        self.assertEqual(http["routers"]["acme-review-a1-acme-review-a1"]["rule"],
                         "Host(`acme-review-a1.sbx.example.com`)")
        self.assertEqual(http["routers"]["acme-review-a1-acme-preview"]["rule"],
                         "Host(`acme-preview.sbx.example.com`)")
        self.assertEqual(
            http["services"]["acme-review-a1-acme-review-a1"]["loadBalancer"]["servers"][0]["url"],
            "http://10.0.0.7:8000")
        self.assertEqual(
            http["services"]["acme-review-a1-acme-preview"]["loadBalancer"]["servers"][0]["url"],
            "http://10.0.0.7:8025")

    def test_every_router_is_behind_forward_auth(self):
        # Fail-closed: no exposure router without the auth middleware, ever.
        self.ing.sync(inst([Exposure("acme-review-a1", 8000),
                            Exposure("acme-preview", 8025)]))
        http = self.read("acme-review-a1.yml")["http"]
        for key, router in http["routers"].items():
            with self.subTest(router=key):
                if key.endswith("-cawl"):
                    continue
                self.assertEqual(router["middlewares"], ["cawl-auth"])

    def test_the_cawl_path_routes_to_the_daemon_not_the_vm(self):
        self.ing.sync(inst())
        http = self.read("acme-review-a1.yml")["http"]
        handoff = http["routers"]["acme-review-a1-acme-review-a1-cawl"]
        self.assertIn("PathPrefix(`/.cawl/`)", handoff["rule"])
        self.assertEqual(handoff["service"], "cawl-daemon")
        # ...and outranks the app router, whatever rule lengths say.
        self.assertGreater(handoff["priority"], 0)

    def test_shared_file_carries_auth_middleware_daemon_and_scoped_auth_host(self):
        self.ing.sync(inst())
        http = self.read("_cawl.yml")["http"]
        fa = http["middlewares"]["cawl-auth"]["forwardAuth"]
        self.assertEqual(fa["address"], "http://127.0.0.1:8000/auth/forward")
        self.assertEqual(
            http["services"]["cawl-daemon"]["loadBalancer"]["servers"][0]["url"],
            "http://127.0.0.1:8000")
        auth_router = http["routers"]["cawl-auth-host"]
        self.assertEqual(
            auth_router["rule"],
            "Host(`auth.sbx.example.com`) && PathPrefix(`/auth/`)")
        # The auth host must not publish unrelated daemon endpoints such as
        # /api/, /admin/, or /cli/login.
        self.assertNotEqual(auth_router["rule"], "Host(`auth.sbx.example.com`)")

    def test_no_exposures_or_no_ip_means_no_file(self):
        self.ing.sync(inst(exposures=()))
        self.assertFalse((self.dir / "acme-review-a1.yml").exists())
        self.ing.sync(inst())                     # write it...
        self.ing.sync(inst(ip=None))              # ...stopped: gone again
        self.assertFalse((self.dir / "acme-review-a1.yml").exists())

    def test_sync_publishes_complete_files_with_atomic_replace(self):
        published = []
        real_replace = os.replace

        def capture_replace(source, destination):
            source = Path(source)
            published.append((source, Path(destination), source.read_text()))
            real_replace(source, destination)

        with patch("cawl_core.ingress.os.replace", side_effect=capture_replace):
            self.ing.sync(inst())

        # Both the shared configuration and environment configuration were
        # complete before either became visible at their watched .yml paths.
        self.assertEqual({destination.name for _, destination, _ in published},
                         {"_cawl.yml", "acme-review-a1.yml"})
        for source, destination, contents in published:
            self.assertEqual(source.parent, self.dir)
            self.assertEqual(source.suffix, ".tmp")
            self.assertFalse(source.exists())
            self.assertEqual(yaml.safe_load(contents),
                             yaml.safe_load(destination.read_text()))

    def test_deregister_removes_the_file(self):
        self.ing.sync(inst())
        self.ing.deregister("acme-review-a1")
        self.assertFalse((self.dir / "acme-review-a1.yml").exists())

    def test_url_for(self):
        self.assertEqual(self.ing.url_for("acme-review-a1"),
                         "https://acme-review-a1.sbx.example.com")
        self.assertEqual(self.ing.url_for("acme-preview"),
                         "https://acme-preview.sbx.example.com")


if __name__ == "__main__":
    unittest.main()
