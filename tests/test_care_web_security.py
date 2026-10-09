import importlib
from pathlib import Path
import sys
import types
import unittest


PLUGIN_ROOT = Path(__file__).parents[1]
if "oppo_health_candidate" not in sys.modules:
    candidate = types.ModuleType("oppo_health_candidate")
    candidate.__path__ = [str(PLUGIN_ROOT)]
    sys.modules["oppo_health_candidate"] = candidate
security = importlib.import_module("oppo_health_candidate.care_web_security")


class CareWebSecurityTests(unittest.TestCase):
    def test_exact_dashboard_admin_identity_is_required(self):
        dashboard = {"dashboard": {"username": "owner"}}
        self.assertTrue(security.dashboard_admin_matches(dashboard, "owner"))
        self.assertFalse(security.dashboard_admin_matches(dashboard, "api_key:abc"))
        self.assertFalse(security.dashboard_admin_matches(dashboard, "other"))
        self.assertFalse(security.dashboard_admin_matches({"dashboard": {}}, "owner"))

    def test_write_requires_exact_same_origin_and_fetch_metadata(self):
        self.assertTrue(security.same_origin_write("https://localhost:6185", "localhost:6185", "same-origin"))
        self.assertTrue(security.same_origin_write("http://127.0.0.1:6185", "127.0.0.1:6185", "same-origin"))
        self.assertFalse(security.same_origin_write(None, "localhost:6185"))
        self.assertFalse(security.same_origin_write("https://evil.example", "localhost:6185"))
        self.assertFalse(security.same_origin_write("https://localhost:6185", "localhost:6185", "same-site"))
        self.assertFalse(security.same_origin_write("https://localhost:6185", "localhost:6185"))
        self.assertFalse(security.same_origin_write("https://localhost:6185/path", "localhost:6185"))


if __name__ == "__main__":
    unittest.main()
