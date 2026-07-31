import unittest

from cawl_core.params import (
    Param, ParamError, args_hash, expand, placeholders, render, resolve,
)

BRANCH = Param("branch", default="main")
TIER = Param("tier", required=True, choices=["small", "big"])


class TestResolve(unittest.TestCase):
    def test_fills_defaults(self):
        self.assertEqual(resolve({"branch": BRANCH}, {}), {"branch": "main"})

    def test_supplied_wins(self):
        self.assertEqual(resolve({"branch": BRANCH}, {"branch": "feat"}),
                         {"branch": "feat"})

    def test_unknown_arg_is_rejected(self):
        with self.assertRaises(ParamError) as e:
            resolve({"branch": BRANCH}, {"bogus": "x"})
        self.assertIn("unknown arg 'bogus'", str(e.exception))

    def test_missing_required(self):
        with self.assertRaises(ParamError):
            resolve({"tier": TIER}, {})

    def test_choices_enforced(self):
        self.assertEqual(resolve({"tier": TIER}, {"tier": "big"}), {"tier": "big"})
        with self.assertRaises(ParamError):
            resolve({"tier": TIER}, {"tier": "huge"})

    def test_pattern_enforced(self):
        p = {"branch": Param("branch", default="main", pattern=r"^[a-z/]+$")}
        with self.assertRaises(ParamError):
            resolve(p, {"branch": "Feature X"})

    def test_value_must_be_printable_ascii(self):
        with self.assertRaises(ParamError):
            resolve({"branch": BRANCH}, {"branch": "a\nb"})
        with self.assertRaises(ParamError):
            resolve({"branch": BRANCH}, {"branch": "x" * 201})

    def test_bad_arg_name(self):
        with self.assertRaises(ParamError):
            resolve({"branch": BRANCH}, {"Branch": "x"})

    def test_template_with_no_params_takes_no_args(self):
        self.assertEqual(resolve({}, {}), {})
        with self.assertRaises(ParamError):
            resolve({}, {"branch": "x"})


class TestArgsHash(unittest.TestCase):
    def test_stable_regardless_of_key_order(self):
        self.assertEqual(args_hash("s", {"a": "1", "b": "2"}),
                         args_hash("s", {"b": "2", "a": "1"}))

    def test_differs_by_value_and_by_template(self):
        self.assertNotEqual(args_hash("s", {"a": "1"}), args_hash("s", {"a": "2"}))
        self.assertNotEqual(args_hash("s", {"a": "1"}), args_hash("t", {"a": "1"}))

    def test_default_and_explicit_same_value_are_one_env(self):
        # `--arg branch=main` and an omitted branch that defaults to main resolve
        # to the same args, so --reuse-if-exists must see them as the same env.
        params = {"branch": BRANCH}
        self.assertEqual(args_hash("s", resolve(params, {})),
                         args_hash("s", resolve(params, {"branch": "main"})))


class TestRender(unittest.TestCase):
    def test_substitutes_and_exports(self):
        out = render("git checkout {{ branch }}", {"branch": "feat"})
        self.assertIn("git checkout feat", out)
        self.assertIn("branch=feat; export branch", out)

    def test_value_is_shell_quoted(self):
        # The whole point: an arg cannot break out of the script it lands in.
        out = render("git checkout {{branch}}", {"branch": "x; rm -rf /"})
        self.assertIn("git checkout 'x; rm -rf /'", out)
        self.assertNotIn("checkout x; rm", out)

    def test_empty_hook_renders_empty(self):
        self.assertEqual(render("", {"a": "b"}), "")
        self.assertEqual(render("   \n ", {"a": "b"}), "")

    def test_undeclared_placeholder_raises(self):
        with self.assertRaises(ParamError):
            render("echo {{nope}}", {"branch": "main"})

    def test_expand_is_raw_for_hostnames(self):
        self.assertEqual(expand("{{template}}-{{branch}}", {"template": "acme", "branch": "x"}),
                         "acme-x")

    def test_placeholders_found(self):
        self.assertEqual(placeholders("{{ a }} and {{b}}"), {"a", "b"})


if __name__ == "__main__":
    unittest.main()
