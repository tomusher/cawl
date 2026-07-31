import unittest
from datetime import datetime, timedelta, timezone

from cawl_core.naming import (
    compute_expiry, default_exposure_label, exposure_host, exposure_label,
    new_environment_id, parse_ttl, sanitize, validate_expose_key,
    validate_exposure_label, validate_name,
)


class TestNaming(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize("Acme CMS!"), "acme-cms")
        self.assertEqual(sanitize("feature/FOO_bar"), "feature-foo-bar")
        self.assertEqual(sanitize("---"), "x")

    def test_validate_name_normalizes_case_and_space(self):
        self.assertEqual(validate_name("web-test"), "web-test")
        self.assertEqual(validate_name("  Web-Test "), "web-test")
        self.assertEqual(validate_name("a1"), "a1")
        self.assertEqual(validate_name("a" * 63), "a" * 63)

    def test_validate_name_rejects_illegal_labels(self):
        for bad in ("my env",        # space
                    "my_env",        # underscore
                    "-lead",         # leading dash
                    "trail-",        # trailing dash
                    "9lives",        # digit first: not a legal hostname
                    "x",             # too short
                    "a" * 64,        # too long
                    "café",
                    ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_name(bad)

    def test_parse_ttl(self):
        self.assertIsNone(parse_ttl(None))
        self.assertIsNone(parse_ttl("none"))
        self.assertIsNone(parse_ttl(""))
        self.assertEqual(parse_ttl("4h"), timedelta(hours=4))
        self.assertEqual(parse_ttl("7d"), timedelta(days=7))
        self.assertEqual(parse_ttl("30m"), timedelta(minutes=30))
        with self.assertRaises(ValueError):
            parse_ttl("banana")

    def test_compute_expiry(self):
        now = datetime(2026, 7, 8, tzinfo=timezone.utc)
        self.assertIsNone(compute_expiry(now, None))
        self.assertEqual(compute_expiry(now, timedelta(hours=1)),
                         now + timedelta(hours=1))

    def test_environment_id_shape(self):
        i = new_environment_id("acme-cms")
        self.assertTrue(i.startswith("acme-cms-"))
        self.assertRegex(i.removeprefix("acme-cms-"), r"^[0-9a-f]{32}$")
        self.assertNotEqual(i, new_environment_id("acme-cms"))

    def test_validate_name_rejects_double_dash(self):
        # Environment ids stay '--'-free so the default template labels they
        # generate (<key>--<id>) read unambiguously.
        with self.assertRaises(ValueError):
            validate_name("web--test")

    def test_validate_expose_key(self):
        self.assertEqual(validate_expose_key("Storybook "), "storybook")
        self.assertEqual(validate_expose_key("a"), "a")
        for bad in ("", "my app", "x--y", "9up", "a" * 21, "-x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_expose_key(bad)

    def test_validate_exposure_label_is_free_form(self):
        # Any hostname label, including the '--' the defaults generate — but
        # never a punycode prefix.
        self.assertEqual(validate_exposure_label(" Acme-Preview "), "acme-preview")
        self.assertEqual(validate_exposure_label("storybook--web-test"),
                         "storybook--web-test")
        for bad in ("", "x", "my app", "9up", "-x", "xn--foo", "a" * 64):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_exposure_label(bad)

    # -- exposure hostnames ------------------------------------------------
    def test_default_labels_are_scoped_to_the_env(self):
        # The template's `web` key is the front door (bare id); other keys are
        # env-scoped so template defaults can never collide across envs.
        self.assertEqual(default_exposure_label("web-test", "web"), "web-test")
        self.assertEqual(default_exposure_label("web-test", "storybook"),
                         "storybook--web-test")

    def test_exposure_host_is_one_label_deep(self):
        # Flat on purpose: one wildcard DNS record and one wildcard cert cover
        # every exposure of every env.
        self.assertEqual(exposure_host("acme-preview", "sbx.example.com"),
                         "acme-preview.sbx.example.com")

    def test_exposure_label_extracts_the_leftmost_label(self):
        base = "sbx.example.com"
        self.assertEqual(exposure_label("acme-preview.sbx.example.com", base),
                         "acme-preview")
        # Port, case and a trailing dot are forgiven; junk is not.
        self.assertEqual(exposure_label("Acme-Preview.SBX.example.com:443.", base),
                         "acme-preview")
        for bad in ("sbx.example.com",             # no label
                    "a.b.sbx.example.com",         # two labels deep
                    "web-test.other.dev",         # wrong domain
                    "web-test.sbx.example.com.evil.com"):
            with self.subTest(bad=bad):
                self.assertIsNone(exposure_label(bad, base))


if __name__ == "__main__":
    unittest.main()
