import unittest
from pathlib import Path

from cawl_core.config import ConfigError, load_template_config, parse_template_config

_EX = Path(__file__).resolve().parents[2] / "examples"
EXAMPLE = _EX / "acme-cms" / "template.yaml"
SCRATCH = _EX / "scratch" / "template.yaml"


class TestConfig(unittest.TestCase):
    def test_load_example(self):
        cfg = load_template_config(EXAMPLE)
        self.assertEqual(cfg.name, "acme-cms")
        self.assertEqual(cfg.expose, {"web": 8000})
        self.assertEqual(cfg.ttl, "none")          # persistent by default

    def test_example_declares_its_params(self):
        cfg = load_template_config(EXAMPLE)
        self.assertEqual(list(cfg.params), ["branch", "mode"])
        self.assertEqual(cfg.params["branch"].default, "main")
        self.assertEqual(cfg.params["mode"].choices, ["dev", "review"])

    def test_example_hooks_carry_the_git_work(self):
        # What the daemon used to do is now the template's business.
        cfg = load_template_config(EXAMPLE)
        self.assertIn("git clone", cfg.hooks.build)
        self.assertIn("git checkout {{ branch }}", cfg.hooks.provision)
        self.assertIn("docker compose", cfg.hooks.provision)

    def test_defaults_parsing(self):
        cfg = parse_template_config({"name": "x", "defaults": {"ttl": "7d"}})
        self.assertEqual(cfg.ttl, "7d")

    def test_image_field(self):
        self.assertEqual(load_template_config(SCRATCH).image, "cawl/base")
        self.assertEqual(load_template_config(EXAMPLE).image, "cawl/acme-cms")

    def test_scratch_declares_nothing(self):
        cfg = load_template_config(SCRATCH)
        self.assertEqual(cfg.params, {})
        self.assertEqual(cfg.hooks.provision, "")
        self.assertEqual(cfg.hooks.build, "")
        self.assertEqual(cfg.expose, {})
        self.assertEqual(cfg.ttl, "none")

    def test_minimal_config(self):
        cfg = parse_template_config({"name": "scratch"})  # only a name required
        self.assertEqual(cfg.expose, {})
        self.assertEqual(cfg.params, {})

    # -- params ------------------------------------------------------------
    def test_param_defaults_are_validated(self):
        with self.assertRaises(ConfigError):
            parse_template_config({
                "name": "x",
                "params": {"branch": {"default": "no spaces allowed here!",
                                      "pattern": r"^[a-z]+$"}},
            })

    def test_param_cannot_shadow_a_builtin(self):
        with self.assertRaises(ConfigError) as e:
            parse_template_config({"name": "x", "params": {"template": {"default": "x"}}})
        self.assertIn("shadows a built-in", str(e.exception))

    def test_required_param_cannot_have_a_default(self):
        with self.assertRaises(ConfigError):
            parse_template_config({
                "name": "x",
                "params": {"tier": {"required": True, "default": "small"}},
            })

    # -- hooks -------------------------------------------------------------
    def test_hook_referencing_an_undeclared_param_is_rejected(self):
        # Caught on upload, not on the first `cawl up` that trips over it.
        with self.assertRaises(ConfigError) as e:
            parse_template_config({
                "name": "x",
                "hooks": {"provision": "git checkout {{ branch }}"},
            })
        self.assertIn("branch", str(e.exception))

    def test_hook_may_use_builtins_without_declaring_them(self):
        cfg = parse_template_config({
            "name": "x", "hooks": {"provision": "echo {{ template }} {{ id }}"}})
        self.assertIn("{{ template }}", cfg.hooks.provision)

    def test_unknown_hook_key(self):
        with self.assertRaises(ConfigError):
            parse_template_config({"name": "x", "hooks": {"postinstall": "echo hi"}})

    def test_expose_parses_names_and_ports(self):
        cfg = parse_template_config({
            "name": "x", "expose": {"web": 8000, "storybook": 6006}})
        self.assertEqual(cfg.expose, {"web": 8000, "storybook": 6006})

    # -- the rest ----------------------------------------------------------
    def test_backend_is_not_template_vocabulary(self):
        # Backend names are the deployment's, not the template's — a portable
        # template can't pin one.
        with self.assertRaises(ConfigError):
            parse_template_config({"name": "x", "defaults": {"backend": "vm"}})
        with self.assertRaises(ConfigError):
            parse_template_config({"name": "x", "defaults": {"isolation": "vm"}})

    def test_bad_expose(self):
        # A bogus port and a name that can't be a hostname label are both
        # template bugs, caught at upload.
        for bad in ({"web": "eight thousand"}, {"web": 0},
                    {"UP--DOWN": 8000}, {"-x": 8000}):
            with self.assertRaises(ConfigError):
                parse_template_config({"name": "x", "expose": bad})

    def test_unknown_defaults_key(self):
        with self.assertRaises(ConfigError):
            parse_template_config({"name": "x", "defaults": {"purpose": "dev"}})
        with self.assertRaises(ConfigError):
            parse_template_config({"name": "x", "defaults": {"ttl": "banana"}})


if __name__ == "__main__":
    unittest.main()
