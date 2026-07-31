import json
import tempfile
import threading
import unittest
from pathlib import Path

from cawl_core.egress import (
    EgressAttachment, JsonPolicyStore, NetworkEgress, ProxyEgress,
)


class TestEgressProvider(unittest.TestCase):
    def test_network_attachment_is_runtime_facing_not_guest_configuration(self):
        provider = NetworkEgress("cawl-agent")
        self.assertEqual(provider.attachment("env-1"), EgressAttachment("cawl-agent"))

    def test_network_name_is_required(self):
        for name in ("", "has spaces"):
            with self.assertRaises(ValueError):
                NetworkEgress(name)

    def test_proxy_exports_only_https_proxy_variables(self):
        script = ProxyEgress(
            "cawl-agent", "http://10.42.0.1:3128").boot_script("env-1")
        self.assertIn("export https_proxy=http://10.42.0.1:3128", script)
        self.assertIn("export HTTPS_PROXY=http://10.42.0.1:3128", script)
        self.assertNotIn("export http_proxy=", script)
        self.assertNotIn("export HTTP_PROXY=", script)

    def test_policy_mutations_do_not_lose_concurrent_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policies.json"
            store = JsonPolicyStore(path)
            start = threading.Barrier(12)

            def register(index):
                start.wait()
                store.register(f"env-{index}", f"10.0.0.{index + 1}", ())

            threads = [threading.Thread(target=register, args=(i,)) for i in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(len(json.loads(path.read_text())), 12)


if __name__ == "__main__":
    unittest.main()
