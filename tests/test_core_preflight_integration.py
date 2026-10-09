"""Exercise the plugin-to-core preflight with the actual AstrBot privacy API."""

from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.message.components import Plain
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.provider.local_health_privacy import clear_local_health_context


INTEGRATED_ROOT = Path(__file__).resolve().parents[4]
if str(INTEGRATED_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATED_ROOT))

from data.plugins.oppo_health import main as health_main


class CorePreflightIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_uses_real_core_preflight_before_local_page_capture(self):
        session = "synthetic_platform:FriendMessage:1234"

        class Context:
            def __init__(self, runner_type="local"):
                self.runner_type = runner_type
                self.provider_lookups = []

            async def get_using_provider_async(self, *, umo):
                self.provider_lookups.append(umo)
                return SimpleNamespace(
                    provider_config={"api_base": "https://approved-b.example.test/v1"}
                )

            def get_config(self, *, umo=None):
                self.config_umo = umo
                return {"agent_runner": {"runner_type": self.runner_type}}

        class Event:
            def __init__(self, text):
                self.message_str = text
                self.unified_msg_origin = session
                self.message_obj = SimpleNamespace(message=[Plain(text)])
                self.extras = {}
                self.stopped = False

            def get_extra(self, name):
                return self.extras.get(name)

            def set_extra(self, name, value):
                self.extras[name] = value

            def stop_event(self):
                self.stopped = True

        context = Context()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(health_main, "private_root", return_value=Path(temp_dir)):
                plugin = health_main.OPPOHealth(
                    context,
                    {
                        "private_session": session,
                        "enable_local_health_pages": True,
                        "approved_provider_hosts": [
                            "approved-b.example.test",
                            "approved-a.example.test",
                        ],
                    },
                )
                plugin._run = AsyncMock(return_value={
                    "pages": {
                        "weight_history": {
                            "status": "ok",
                            "data": {
                                "source": "synthetic",
                                "date": "2031-11-15",
                                "metrics": {
                                    "weight_history_records": [
                                        {"value": 60, "unit": "kg"}
                                    ]
                                },
                            },
                        }
                    }
                })
                event = Event("我今天体重是多少")
                request = ProviderRequest(
                    system_prompt="base prompt",
                    func_tool=SimpleNamespace(remove_tool=lambda _name: None),
                )
                request.resolved_provider_api_host = "approved-b.example.test"

                with patch.object(
                    health_main.SessionPluginManager,
                    "is_plugin_enabled_for_session",
                    new=AsyncMock(return_value=True),
                ):
                    await plugin.mark_local_health_request_intent(event)
                    await plugin.inject_health(event, request)

        self.assertEqual(context.provider_lookups, [session])
        self.assertEqual(context.config_umo, session)
        plugin._run.assert_awaited_once()
        self.assertTrue(request.local_health_sensitive)
        self.assertEqual(request.local_health_required_provider_host, "approved-b.example.test")
        self.assertFalse(event.stopped)
        self.assertEqual(request.system_prompt, "base prompt")
        clear_local_health_context(request)

    async def test_custom_runner_fails_closed_before_local_page_capture(self):
        session = "synthetic_platform:FriendMessage:1234"

        class Context:
            def __init__(self):
                self.provider_lookups = []

            async def get_using_provider_async(self, *, umo):
                self.provider_lookups.append(umo)
                return SimpleNamespace(
                    provider_config={"api_base": "https://approved-b.example.test/v1"}
                )

            def get_config(self, *, umo=None):
                return {"agent_runner": {"runner_type": "dify"}}

        class Event:
            message_str = "我今天体重是多少"
            unified_msg_origin = session
            message_obj = SimpleNamespace(message=[Plain(message_str)])
            stopped = False

            def __init__(self):
                self.extras = {}

            def get_extra(self, name):
                return self.extras.get(name)

            def set_extra(self, name, value):
                self.extras[name] = value

            def stop_event(self):
                self.stopped = True

        context = Context()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(health_main, "private_root", return_value=Path(temp_dir)):
                plugin = health_main.OPPOHealth(
                    context,
                    {
                        "private_session": session,
                        "enable_local_health_pages": True,
                        "approved_provider_hosts": ["approved-b.example.test"],
                    },
                )
                plugin._run = AsyncMock(
                    side_effect=AssertionError("custom runner cannot capture health pages")
                )
                event = Event()
                request = SimpleNamespace(
                    system_prompt="",
                    resolved_provider_api_host="approved-b.example.test",
                    func_tool=SimpleNamespace(remove_tool=lambda _name: None),
                )
                with patch.object(
                    health_main.SessionPluginManager,
                    "is_plugin_enabled_for_session",
                    new=AsyncMock(return_value=True),
                ):
                    await plugin.mark_local_health_request_intent(event)
                    await plugin.inject_health(event, request)

        self.assertTrue(event.stopped)
        plugin._run.assert_not_awaited()
        self.assertEqual(context.provider_lookups, [])


if __name__ == "__main__":
    unittest.main()
