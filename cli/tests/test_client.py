import json
import unittest

from cawl.client import ApiClient, ApiError, ConfigError, client_from_env


def fake_transport(responses):
    """Return (transport, calls). `responses` is a list of (status, body)."""
    calls = []
    queue = list(responses)

    def t(method, url, headers, data):
        calls.append({"method": method, "url": url, "headers": headers,
                      "body": json.loads(data) if data else None})
        status, body = queue.pop(0)
        return status, (json.dumps(body).encode() if body is not None else b"")

    return t, calls


class TestClient(unittest.TestCase):
    def client(self, responses):
        t, calls = fake_transport(responses)
        return ApiClient("https://cawl.example/", "cawl_tok", transport=t), calls

    def test_up_posts_body_and_auth_header(self):
        c, calls = self.client([(200, {"id": "x", "owner": "tom"})])
        out = c.up(template="acme", args={"branch": "main"}, ttl="4h")
        self.assertEqual(out["id"], "x")
        call = calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://cawl.example/api/environments")
        self.assertEqual(call["headers"]["Authorization"], "Bearer cawl_tok")
        self.assertEqual(call["body"]["ttl"], "4h")
        self.assertEqual(call["body"]["args"], {"branch": "main"})

    def test_refresh_image_posts_args(self):
        c, calls = self.client([(200, {"image": "cawl/acme"})])
        c.refresh_image("acme", {"branch": "main"})
        self.assertEqual(calls[0]["url"], "https://cawl.example/api/images/refresh")
        self.assertEqual(calls[0]["body"], {"template": "acme", "args": {"branch": "main"},
                                            "backend": None})

    def test_ls_builds_query(self):
        c, calls = self.client([(200, [])])
        c.ls(template="acme")
        self.assertEqual(calls[0]["url"], "https://cawl.example/api/environments?template=acme")

    def test_exec_endpoint(self):
        c, calls = self.client([(200, {"exit_code": 0, "stdout": "hi", "stderr": ""})])
        res = c.exec("id1", ["echo", "hi"])
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(calls[0]["url"], "https://cawl.example/api/environments/id1/exec")
        self.assertEqual(calls[0]["body"], {"cmd": ["echo", "hi"]})

    def test_expose_posts_port_name_and_access(self):
        c, calls = self.client([(200, {"name": "storybook", "port": 6006,
                                       "url": "https://storybook--id1.sbx.x",
                                       "access": ["sue@client.com"], "links": {}})])
        out = c.expose("id1", 6006, name="storybook", access=["sue@client.com"])
        self.assertEqual(out["port"], 6006)
        self.assertEqual(calls[0]["url"],
                         "https://cawl.example/api/environments/id1/exposures")
        self.assertEqual(calls[0]["body"], {"port": 6006, "name": "storybook",
                                            "access": ["sue@client.com"]})

    def test_unexpose_deletes_by_name(self):
        c, calls = self.client([(200, {"id": "id1", "exposures": []})])
        c.unexpose("id1", "storybook")
        self.assertEqual(calls[0]["method"], "DELETE")
        self.assertEqual(calls[0]["url"],
                         "https://cawl.example/api/environments/id1/exposures/storybook")

    def test_error_status_raises_apierror(self):
        c, _ = self.client([(409, {"error": "over quota"})])
        with self.assertRaises(ApiError) as ctx:
            c.up(template="acme")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.message, "over quota")

    def test_error_without_body(self):
        c, _ = self.client([(500, None)])
        with self.assertRaises(ApiError) as ctx:
            c.whoami()
        self.assertEqual(ctx.exception.status, 500)

    def test_client_from_env_requires_both(self):
        with self.assertRaises(ConfigError):
            client_from_env(env={"CAWL_API_URL": "https://x"})
        with self.assertRaises(ConfigError):
            client_from_env(env={"CAWL_TOKEN": "t"})
        c = client_from_env(env={"CAWL_API_URL": "https://x", "CAWL_TOKEN": "t"})
        self.assertEqual(c.base_url, "https://x")


if __name__ == "__main__":
    unittest.main()
