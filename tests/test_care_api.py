import asyncio
import importlib
from pathlib import Path
import sys
import tempfile
import types
import unittest


PLUGIN_ROOT = Path(__file__).parents[1]
if "oppo_health_candidate" not in sys.modules:
    candidate = types.ModuleType("oppo_health_candidate")
    candidate.__path__ = [str(PLUGIN_ROOT)]
    sys.modules["oppo_health_candidate"] = candidate


class Response:
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code


class FakeRequest:
    username = None
    headers = {}
    payload = None

    async def json(self, default=None):
        return self.payload if self.payload is not None else default


class FakeContext:
    def __init__(self):
        self.registered_web_apis = []

    def get_config(self):
        return {"dashboard": {"username": "owner"}}

    def register_web_api(self, route, handler, methods, description):
        self.registered_web_apis.append((route, handler, methods, description))


class CareApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        previous = {name: sys.modules.get(name) for name in ("astrbot", "astrbot.api", "astrbot.api.web")}
        cls.previous_astrbot_modules = previous
        astrbot = types.ModuleType("astrbot")
        astrbot.__path__ = []
        api = types.ModuleType("astrbot.api")
        api.__path__ = []
        web = types.ModuleType("astrbot.api.web")
        cls.fake_request = FakeRequest()
        web.request = cls.fake_request
        web.json_response = lambda data=None, **kwargs: Response(data, kwargs.get("status_code", 200))
        web.error_response = lambda message, **kwargs: Response({"message": message}, kwargs.get("status_code", 400))
        sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.web": web})
        cls.module = importlib.import_module("oppo_health_candidate.care_api")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("oppo_health_candidate.care_api", None)
        for name, previous in cls.previous_astrbot_modules.items():
            sys.modules.pop(name, None)
            if previous is not None:
                sys.modules[name] = previous

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        store_module = importlib.import_module("oppo_health_candidate.care_store")
        self.repository = store_module.CareRepository(Path(self.temp_dir.name) / "care")
        self.context = FakeContext()
        self.status_reads = []
        def cached_status():
            self.status_reads.append("read")
            return {
                "source_state": "unavailable",
                "source_observed_at": None,
                "core_privacy_api_available": True,
                "supported_runner": False,
                "resolved_provider_host": None,
                "host_approved": False,
                "health_context_to_model_enabled": False,
                "last_route_error_code": "preflight_unavailable",
                "data_sources": [{
                    "source": "predicted_period", "status": "unavailable",
                    "data_date": None, "observed_at": None, "is_stale": None,
                    "reason": "本机安全预检尚未接通",
                }, {
                    "source": "confirmed_period_end", "status": "unsupported",
                    "data_date": None, "observed_at": "2026-10-09T08:15:00+08:00",
                    "is_stale": None, "reason": "当前来源未提供明确的经期结束事件",
                }, {
                    "source": "period_late_inquiry", "status": "unsupported",
                    "data_date": None, "observed_at": "2026-10-09T08:15:00+08:00",
                    "is_stale": None, "evaluation_state": "unsupported",
                    "reason": "来源没有同时提供明确经期开始和结束事件通道；后段询问保持暂停",
                }, {
                    "source": "wellness_low_state", "status": "ok",
                    "data_date": "2026-10-09", "observed_at": "2026-10-09T08:20:00+08:00",
                    "measured_at": "2026-10-09T08:15:00+08:00", "category": "Slow down",
                    "quality": "来源未提供质量标签",
                    "completeness": "解析成功，日期、分类、真实测量时刻和更新时间齐全；质量标签未独立核验",
                    "evaluation_state": "confirming_low_state", "is_stale": False,
                    "reason": "等待下一条新鲜独立测量样本",
                }],
            }
        self.api = self.module.CarePageAPI(
            self.context, {"daily_care": False}, self.repository, status_provider=cached_status
        )
        self.request = self.fake_request
        self.request.username = "owner"
        self.request.headers = {"origin": "http://localhost:6185", "host": "localhost:6185", "sec-fetch-site": "same-origin"}
        self.request.payload = None

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_route_registration_and_removal_uses_plugin_api_only(self):
        self.api.register()
        self.assertEqual([row[0] for row in self.context.registered_web_apis], [
            "/oppo_health/care/state", "/oppo_health/care/preview", "/oppo_health/care/config",
            "/oppo_health/care/privacy-status",
        ])
        self.api.unregister()
        self.assertEqual(self.context.registered_web_apis, [])

    def test_state_is_admin_only_and_has_no_synthetic_health_values(self):
        self.request.username = "api_key:readonly"
        response = asyncio.run(self.api.state())
        self.assertEqual(response.status_code, 403)
        self.request.username = "owner"
        response = asyncio.run(self.api.state())
        payload = response.data
        self.assertEqual(payload["demo"], False)
        self.assertEqual(payload["data_sources"][0]["status"], "unavailable")
        self.assertIsNone(payload["data_sources"][0]["data_date"])
        self.assertEqual(payload["model_access"]["current_host"], None)
        self.assertEqual(payload["privacy_status"]["last_route_error_code"], "preflight_unavailable")
        self.assertFalse(any("value" in source for source in payload["data_sources"]))
        self.assertTrue(all(not row["enabled"] for row in payload["rules"]))

    def test_rule_page_status_includes_unsupported_reason_and_source_times(self):
        config, revision = self.repository.load_config()
        config["rules"]["confirmed_period_end"]["enabled"] = True
        config["rules"]["period_late_inquiry"]["enabled"] = True
        config["rules"]["wellness_low_state"].update(
            enabled=True, mode="slow_down_category", timezone_confirmed=True,
            confirmation_minutes=15, minimum_independent_samples=2,
            recovery_debounce_minutes=15, maximum_sample_age_minutes=30,
            repeat_cooldown_minutes=240,
        )
        self.repository.save_config(config, revision)
        scheduler_state = self.repository.load_scheduler_state()
        scheduler_state["jobs"]["late-history"] = {
            "event_key": "f" * 64,
            "rule_id": "period_late_inquiry",
            "target_date": "2026-10-08",
            "basis": "后段询问机会",
            "expected_at": "2026-10-08T09:00:00+08:00",
            "window_end": "2026-10-08T21:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "source_observed_at": "2026-10-08T09:00:00+08:00",
            "is_actual_event": True,
            "state": "handed_off",
            "reason": "旧的候选交接状态",
        }
        self.repository.save_scheduler_state(scheduler_state)
        response = asyncio.run(self.api.state())
        rows = {row["rule_id"]: row for row in response.data["rules"]}
        self.assertEqual(rows["confirmed_period_end"]["state"], "unsupported")
        self.assertIn("未提供明确的经期结束事件", rows["confirmed_period_end"]["reason"])
        self.assertEqual(rows["confirmed_period_end"]["checked_at"], "2026-10-09T08:15:00+08:00")
        self.assertEqual(rows["period_late_inquiry"]["state"], "unsupported")
        self.assertIn("明确经期开始和结束事件通道", rows["period_late_inquiry"]["reason"])
        self.assertEqual(rows["period_late_inquiry"]["checked_at"], "2026-10-09T08:15:00+08:00")
        self.assertEqual(rows["wellness_low_state"]["state"], "confirming_low_state")
        self.assertEqual(rows["wellness_low_state"]["measured_at"], "2026-10-09T08:15:00+08:00")
        wellness = next(
            item for item in response.data["data_sources"] if item["source"] == "wellness_low_state"
        )
        self.assertIn("质量标签未独立核验", wellness["completeness"])

    def test_privacy_status_sanitizes_readiness(self):
        response = asyncio.run(self.api.privacy_status())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["source_state"], "unavailable")
        self.assertFalse(response.data["supported_runner"])
        self.assertEqual(self.status_reads, ["read"])
        self.assertNotIn("value", response.data)
        self.assertNotIn("metrics", response.data)

    def test_async_read_only_preflight_provider_is_awaited(self):
        async def route_status():
            await asyncio.sleep(0)
            return {
                "source_state": "unavailable",
                "route_observed_at": "2031-11-16T08:00:00+00:00",
                "resolved_provider_host": "approved.example",
                "host_approved": True,
                "supported_runner": True,
                "health_context_to_model_enabled": True,
                "data_sources": [],
            }

        self.api.status_provider = route_status
        response = asyncio.run(self.api.privacy_status())
        self.assertEqual(response.data["resolved_provider_host"], "approved.example")
        self.assertTrue(response.data["health_context_to_model_enabled"])
        self.assertEqual(response.data["route_observed_at"], "2031-11-16T08:00:00+00:00")

    def test_non_admin_privacy_status_does_not_call_preflight_provider(self):
        self.request.username = "api_key:readonly"
        response = asyncio.run(self.api.privacy_status())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.status_reads, [])

    def test_write_requires_admin_same_origin_and_optimistic_revision(self):
        default_config = importlib.import_module("oppo_health_candidate.care_rules").default_config()
        default_config["rules"]["weight_date_linked"]["enabled"] = True
        self.request.payload = {"expected_revision": 0, "config": default_config}

        self.request.headers = {"host": "localhost:6185"}
        response = asyncio.run(self.api.save_config())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.repository.load_config()[1], 0)

        self.request.headers = {"origin": "http://outside.example", "host": "localhost:6185"}
        response = asyncio.run(self.api.save_config())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.repository.load_config()[1], 0)

        self.request.headers = {"origin": "http://localhost:6185", "host": "localhost:6185", "sec-fetch-site": "same-origin"}
        response = asyncio.run(self.api.save_config())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["revision"], 1)
        self.assertTrue(response.data["config"]["rules"]["weight_date_linked"]["enabled"])

        reread = asyncio.run(self.api.state())
        self.assertEqual(reread.status_code, 200)
        self.assertEqual(reread.data["revision"], response.data["revision"])
        self.assertEqual(reread.data["config"], response.data["config"])
        self.assertEqual(len(reread.data["rules"]), 7)

        response = asyncio.run(self.api.save_config())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.repository.load_config()[1], 1)


if __name__ == "__main__":
    unittest.main()
