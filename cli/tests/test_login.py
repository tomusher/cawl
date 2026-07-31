import os
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request

from cawl import credentials, login as login_mod
from cawl.client import ConfigError, resolve_client


class TestCredentials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CAWL_CONFIG_DIR"] = self.tmp

    def tearDown(self):
        os.environ.pop("CAWL_CONFIG_DIR", None)

    def test_save_load_clear(self):
        self.assertEqual(credentials.load(), (None, None))
        credentials.save("https://d.example", "cawl_abc")
        self.assertEqual(credentials.load(), ("https://d.example", "cawl_abc"))
        # stored 0600
        self.assertEqual(oct(credentials.path().stat().st_mode)[-3:], "600")
        self.assertTrue(credentials.clear())
        self.assertEqual(credentials.load(), (None, None))
        self.assertFalse(credentials.clear())

    def test_resolve_prefers_env_then_file(self):
        credentials.save("https://file.example", "cawl_file")
        # env wins
        c = resolve_client(env={"CAWL_API_URL": "https://env.example",
                                "CAWL_TOKEN": "cawl_env"})
        self.assertEqual((c.base_url, c.token), ("https://env.example", "cawl_env"))
        # falls back to file
        c = resolve_client(env={})
        self.assertEqual((c.base_url, c.token), ("https://file.example", "cawl_file"))

    def test_resolve_errors_when_nothing(self):
        with self.assertRaises(ConfigError):
            resolve_client(env={})


class TestBrowserLogin(unittest.TestCase):
    def _simulate_browser(self, token, use_state=True):
        """Return a fake webbrowser.open that hits the loopback with `token`."""
        def fake_open(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            cb, st = q["callback"][0], q["state"][0]
            params = {"token": token, "state": st if use_state else "wrong"}
            hit = cb + "?" + urllib.parse.urlencode(params)
            threading.Thread(  # callback must run while browser_login serves
                target=lambda: urllib.request.urlopen(hit).read(), daemon=True).start()
        return fake_open

    def test_roundtrip_returns_token(self):
        login_mod.webbrowser.open = self._simulate_browser("cawl_TESTTOKEN")
        token = login_mod.browser_login("http://daemon.example", open_browser=True)
        self.assertEqual(token, "cawl_TESTTOKEN")

    def test_state_mismatch_raises(self):
        login_mod.webbrowser.open = self._simulate_browser("cawl_x", use_state=False)
        with self.assertRaises(RuntimeError):
            login_mod.browser_login("http://daemon.example", open_browser=True)


if __name__ == "__main__":
    unittest.main()
