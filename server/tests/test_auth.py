import unittest
from datetime import datetime, timezone

from cawl_core.auth import (
    Principal, Role, SYSTEM, can_view, require_access, require_admin,
    resolve_principal,
)
from cawl_core.errors import PermissionDenied
from cawl_core.models import Environment, Status


def inst(owner="tom"):
    return Environment(
        id="i1", template="acme", args={"branch": "main"},
        owner=owner, status=Status.ready,
        created_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )


class TestAuth(unittest.TestCase):
    def test_role_flags(self):
        self.assertFalse(Principal("tom").is_admin)
        self.assertTrue(Principal("root", Role.admin).is_admin)
        self.assertTrue(SYSTEM.is_admin)

    def test_can_view(self):
        self.assertTrue(can_view(Principal("tom"), inst("tom")))
        self.assertFalse(can_view(Principal("sue"), inst("tom")))
        self.assertTrue(can_view(Principal("root", Role.admin), inst("tom")))

    def test_require_access(self):
        require_access(Principal("tom"), inst("tom"))  # no raise
        with self.assertRaises(PermissionDenied):
            require_access(Principal("sue"), inst("tom"))

    def test_require_admin(self):
        require_admin(Principal("root", Role.admin))
        with self.assertRaises(PermissionDenied):
            require_admin(Principal("tom"))

    def test_resolve_principal_env_and_admins(self):
        p = resolve_principal(frozenset({"alice"}), env={"CAWL_ACTOR": "tom"})
        self.assertEqual(p, Principal("tom", Role.user))
        p = resolve_principal(frozenset({"alice"}), env={"CAWL_ACTOR": "alice"})
        self.assertEqual(p.role, Role.admin)

    def test_resolve_principal_falls_back_to_os_user(self):
        p = resolve_principal(frozenset(), env={})
        self.assertIsInstance(p.id, str)
        self.assertTrue(p.id)


if __name__ == "__main__":
    unittest.main()
