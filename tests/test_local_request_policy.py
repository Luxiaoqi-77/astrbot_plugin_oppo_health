import unittest
from types import SimpleNamespace

import local_request_policy as policy


class LocalRequestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.core = SimpleNamespace(
            stage_local_health_context=lambda request, context, **kwargs: None,
            clear_local_health_context=lambda request: None,
            local_health_request_context=lambda enabled: None,
            is_local_health_request=lambda: False,
            provider_allowed_for_request=lambda api_base, host: (
                api_base == f"https://{host}/" and host == "approved.example"
            ),
        )

    def test_query_selection_requires_self_reference_and_rejects_other_people(self):
        self.assertEqual(policy.local_fields_for_query("我今天体重是多少"), ["weight_history"])
        self.assertEqual(policy.local_fields_for_query("我朋友的体重是多少"), [])
        self.assertTrue(policy.health_query_mentions_other_person("我朋友的体重是多少"))
        self.assertTrue(policy.health_query_mentions_known_metric("体重是多少"))
        self.assertTrue(policy.health_query_mentions_other_person("his sleep record"))
        self.assertEqual(policy.local_fields_for_query("体重是多少"), [])
        self.assertEqual(
            policy.local_fields_for_query("我想看身心状态和步行距离"),
            ["wellness_home", "wellness_detail", "steps_daily_details"],
        )

    def test_missing_or_incomplete_core_api_is_unavailable(self):
        missing, reason = policy.load_core_privacy_api(
            lambda _name: (_ for _ in ()).throw(ModuleNotFoundError())
        )
        self.assertIsNone(missing)
        self.assertEqual(reason, "core_privacy_module_unavailable")
        modules = {
            policy.CORE_PRIVACY_MODULE: SimpleNamespace(
                stage_local_health_context=lambda *_args, **_kwargs: None,
                clear_local_health_context=lambda *_args: None,
                local_health_request_context=lambda *_args: None,
                is_local_health_request=lambda: False,
            ),
            policy.CORE_PROVIDER_ENTITIES_MODULE: SimpleNamespace(
                sanitize_provider_api_host=lambda _value: None,
            ),
        }
        incomplete, reason = policy.load_core_privacy_api(modules.__getitem__)
        self.assertIsNone(incomplete)
        self.assertEqual(reason, "core_provider_host_policy_unavailable")

        old_privacy_api = {
            policy.CORE_PRIVACY_MODULE: SimpleNamespace(
                stage_local_health_context=lambda request, context: None,
                clear_local_health_context=lambda *_args: None,
                local_health_request_context=lambda *_args: None,
                is_local_health_request=lambda: False,
            ),
            policy.CORE_PROVIDER_ENTITIES_MODULE: SimpleNamespace(
                provider_allowed_for_request=lambda _api_base, _host: True,
                sanitize_provider_api_host=lambda _value: "approved.example",
            ),
        }
        missing_staged_host, reason = policy.load_core_privacy_api(
            old_privacy_api.__getitem__
        )
        self.assertIsNone(missing_staged_host)
        self.assertEqual(reason, "core_privacy_api_incomplete")

    def test_existing_core_helpers_load_without_an_invented_version_marker(self):
        modules = {
            policy.CORE_PRIVACY_MODULE: SimpleNamespace(
                stage_local_health_context=lambda *_args, **_kwargs: None,
                clear_local_health_context=lambda *_args: None,
                local_health_request_context=lambda *_args: None,
                is_local_health_request=lambda: False,
            ),
            policy.CORE_PROVIDER_ENTITIES_MODULE: SimpleNamespace(
                provider_allowed_for_request=lambda _api_base, _host: True,
                sanitize_provider_api_host=lambda _value: "approved.example",
            ),
        }
        api, reason = policy.load_core_privacy_api(modules.__getitem__)
        self.assertIsNone(reason)
        self.assertIsNotNone(api)
        self.assertFalse(hasattr(modules[policy.CORE_PRIVACY_MODULE], "LOCAL_HEALTH_PRIVACY_API_VERSION"))

    def test_provider_host_validation_rejects_unapproved_and_url_paths(self):
        self.assertTrue(policy.provider_host_is_approved(self.core, "https://approved.example"))
        self.assertFalse(policy.provider_host_is_approved(self.core, "other.example"))
        self.assertIsNone(policy.normalize_provider_host("http://approved.example"))
        self.assertFalse(policy.provider_host_is_approved(self.core, "https://approved.example/v1"))
        self.assertFalse(policy.provider_host_is_approved(self.core, "https://user:pass@approved.example"))
        self.assertIsNone(policy.normalize_provider_host("https://[broken"))

    def test_provider_allowlist_is_explicit_and_invalid_entries_close_it(self):
        self.assertEqual(policy.normalize_approved_provider_hosts([]), ())
        self.assertEqual(
            policy.normalize_approved_provider_hosts(["HTTPS://Approved.Example/"]),
            ("approved.example",),
        )
        self.assertEqual(
            policy.normalize_approved_provider_hosts(
                ["approved.example", "https://unapproved.example/path"]
            ),
            (),
        )
        self.assertFalse(
            policy.local_request_allowed(
                "test_platform:FriendMessage:1234",
                "test_platform:FriendMessage:1234",
                "unapproved.example",
                ("approved.example",),
                self.core,
            )
        )

    def test_exact_private_session_and_approved_host_are_both_required(self):
        session = "test_platform:FriendMessage:1234"
        self.assertTrue(policy.local_request_allowed(
            session, session, "https://approved.example", ("approved.example",), self.core
        ))
        self.assertFalse(policy.local_request_allowed(
            session, "test_platform:FriendMessage:5678", "approved.example", ("approved.example",), self.core
        ))
        self.assertFalse(policy.local_request_allowed(
            session, session, "other.example", ("approved.example",), self.core
        ))
        self.assertFalse(policy.local_request_allowed(
            session, session, "approved.example", (), self.core
        ))

    def test_cloud_tool_removal_fails_closed_when_unsupported(self):
        self.assertFalse(policy.remove_cloud_health_tool(object()))
        removed = []
        self.assertTrue(policy.remove_cloud_health_tool(
            SimpleNamespace(remove_tool=removed.append)
        ))
        self.assertEqual(removed, ["get_oppo_health"])

    def test_context_formatter_keeps_only_requested_metrics(self):
        result = {
            "pages": {
                "weight_history": {
                    "status": "ok",
                    "data": {
                        "source": "synthetic",
                        "date": "2031-11-15",
                        "metrics": {"weight_history_records": [{"value": 60}], "private_debug": "drop"},
                    },
                },
                "active_calories": {
                    "status": "ok",
                    "data": {"metrics": {"active_kcal": 99}},
                },
            }
        }
        formatted = policy.format_local_health_context(result, ["weight_history"])
        self.assertIn('"weight_history_records"', formatted)
        self.assertNotIn("private_debug", formatted)
        self.assertNotIn("active_kcal", formatted)


if __name__ == "__main__":
    unittest.main()
