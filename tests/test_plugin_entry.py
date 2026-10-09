import asyncio
import datetime as dt
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


class FakeLogger:
    def __init__(self):
        self.errors = []

    def error(self, message, *args, **kwargs):
        self.errors.append(message % args if args else message)

    def warning(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


class FakePlain:
    def __init__(self, text="synthetic"):
        self.text = text


class FakeEvent:
    def __init__(self, text, session):
        self.message_str = text
        self.unified_msg_origin = session
        self.message_obj = types.SimpleNamespace(message=[FakePlain(text)])
        self.extras = {}
        self.stopped = False

    def get_extra(self, name):
        return self.extras.get(name)

    def set_extra(self, name, value):
        self.extras[name] = value

    def stop_event(self):
        self.stopped = True


def _fixed_main_datetime(main):
    base = main.dt.datetime

    class FixedDateTime(base):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls(2031, 11, 16, 15, 0)
            return cls(2031, 11, 16, 15, 0, tzinfo=main.TZ).astimezone(tz)

    return FixedDateTime


def _identity_decorator(*_args, **_kwargs):
    return lambda function: function


def _module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class PluginEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.logger = FakeLogger()
        self.previous_astrbot_modules = {}
        for name in list(sys.modules):
            if name == "astrbot" or name.startswith("astrbot."):
                self.previous_astrbot_modules[name] = sys.modules.pop(name)
        self._install_stubs()
        root = Path(__file__).parents[1]
        package = _module("oppo_health_candidate")
        package.__path__ = [str(root)]
        collector = _module("oppo_health_candidate.collector")
        collector.__path__ = [str(root / "collector")]
        care = _module(
            "oppo_health_candidate.care_logic",
            choose_sleep_care=lambda *_args: {"record_id": "synthetic", "selected": False, "delay_minutes": 90, "due_at": None},
            due_kind=lambda *_args: None,
            explicit_goodnight_message=lambda *_args, **_kwargs: False,
            legacy_dispatch_skip_reason=lambda *_args: None,
            new_activity_due=lambda *_args: None,
            observe_sleep_candidate=lambda *_args: (None, False, False),
            preflight_cooldown_active=lambda *_args: False,
            preflight_sleep_record=lambda *_args: None,
            sleep_record=lambda *_args: None,
            wake_due=lambda *_args: None,
        )
        storage = _module(
            "oppo_health_candidate.collector.storage",
            private_root=lambda: Path(self.temp_dir.name),
            read_private_json=lambda _path: {},
            write_private_json=lambda *_args: None,
        )
        self.package = package
        self.main = importlib.import_module("oppo_health_candidate.main")

    async def asyncTearDown(self):
        task = getattr(getattr(self, "plugin", None), "_task", None)
        if task is not None:
            await self.plugin.terminate()
        for name in list(sys.modules):
            if name == "oppo_health_candidate" or name.startswith("oppo_health_candidate."):
                del sys.modules[name]
            elif name == "astrbot" or name.startswith("astrbot."):
                del sys.modules[name]
        sys.modules.update(self.previous_astrbot_modules)
        self.temp_dir.cleanup()

    def _install_stubs(self):
        class Star:
            def __init__(self, context):
                self.context = context

        class SessionPluginManager:
            @staticmethod
            async def is_plugin_enabled_for_session(_session, _name):
                return True

        class FakeFilter:
            command = _identity_decorator
            llm_tool = _identity_decorator
            on_llm_request = _identity_decorator
            on_waiting_llm_request = _identity_decorator

        class StarTools:
            @staticmethod
            def get_data_dir(_name=None):
                return Path(self.temp_dir.name) / "plugin-data"

        class Request:
            username = "owner"
            headers = {}

            async def json(self, default=None):
                return default

        astrbot = _module("astrbot")
        astrbot.__path__ = []
        api = _module("astrbot.api", logger=self.logger)
        api.__path__ = []
        event = _module(
            "astrbot.api.event",
            AstrMessageEvent=FakeEvent,
            filter=FakeFilter(),
        )
        star_api = _module("astrbot.api.star", Context=object, Star=Star, StarTools=StarTools)
        _module(
            "astrbot.api.web",
            request=Request(),
            json_response=lambda data=None, **_kwargs: types.SimpleNamespace(data=data),
            error_response=lambda message, **_kwargs: types.SimpleNamespace(data={"message": message}),
        )
        core = _module("astrbot.core")
        core.__path__ = []
        message = _module("astrbot.core.message")
        message.__path__ = []
        _module("astrbot.core.message.components", Plain=FakePlain)
        core_star = _module("astrbot.core.star")
        core_star.__path__ = []
        _module(
            "astrbot.core.star.session_plugin_manager",
            SessionPluginManager=SessionPluginManager,
        )

    def _new_plugin(self, enabled=True):
        session = "test_platform:FriendMessage:1234"
        config = {
            "private_session": session,
            "enable_local_health_pages": enabled,
            "approved_provider_hosts": ["approved.example"],
            "daily_care": False,
            "bot_qq_id": "9876",
        }
        self.plugin = self.main.OPPOHealth(object(), config)
        return session

    def _enable_weight_mode(self, enabled_at):
        runtime = self.plugin._care_repository.load_wellness_state()
        runtime.update(
            weight_mode_enabled=True,
            weight_mode_enabled_at=enabled_at,
        )
        self.plugin._care_repository.save_wellness_state(runtime)

    async def test_default_config_disables_local_pages_and_keeps_public_collector_defaults(self):
        schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
        self.assertFalse(schema["enable_local_health_pages"]["default"])
        self.assertEqual(schema["approved_provider_hosts"]["default"], [])
        self.assertEqual(schema["collector_script"]["default"], "")
        self.assertEqual(schema["auth_file"]["default"], "")
        self.assertEqual(schema["private_session"]["default"], "")

    async def test_missing_core_api_disables_all_health_to_model_paths(self):
        session = self._new_plugin(enabled=True)
        self.assertFalse(self.plugin.local_pages_enabled)
        self.assertIsNone(self.plugin.health_ai_core_api)
        self.assertTrue(any("Health-to-model access disabled" in item for item in self.logger.errors))

        async def idle_worker():
            await asyncio.Event().wait()

        self.plugin._worker = idle_worker
        await self.plugin.initialize()
        local_event = FakeEvent("我今天体重是多少", session)
        await self.plugin.mark_local_health_request_intent(local_event)
        self.assertTrue(local_event.get_extra("_oppo_local_health_request"))
        self.plugin._run = AsyncMock(side_effect=AssertionError("local capture must stay closed"))
        await self.plugin.inject_health(local_event, types.SimpleNamespace(system_prompt=""))
        self.plugin._run.assert_not_awaited()

        cloud_event = FakeEvent("我的步数是多少", session)
        synthetic_snapshot = {
            "date": "2031-11-15",
            "fetched_at": "2031-11-15T20:10:00+08:00",
            "metrics": {"steps": {"total": 42}},
        }
        self.plugin._snapshot = AsyncMock(return_value=synthetic_snapshot)
        request = types.SimpleNamespace(
            system_prompt="base prompt",
            resolved_provider_api_host="approved.example",
        )
        await self.plugin.inject_health(cloud_event, request)
        self.assertTrue(cloud_event.stopped)
        self.assertEqual(request.system_prompt, "base prompt")
        self.plugin._snapshot.assert_not_awaited()

    async def test_care_privacy_preflight_calls_core_api_with_session_and_allowlist(self):
        api = self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)

        result = await self.plugin._care_privacy_preflight()

        api.preflight_local_health_care.assert_called_once_with(
            context=self.plugin.context,
            private_session_umo=session,
            approved_provider_hosts=("approved.example",),
        )
        self.assertTrue(result["supported_runner"])
        self.assertTrue(result["host_approved"])
        self.assertTrue(result["health_context_to_model_enabled"])

    async def test_daily_state_keeps_previous_sleep_record_deduplication(self):
        self._new_plugin(enabled=False)
        self.plugin.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.plugin.state_path.write_text(
            json.dumps({
                "date": "2031-11-14",
                "sleep_attempted_record_id": "synthetic-record-id",
                "sleep_attempted_at": "2031-11-14T08:00:00+08:00",
            }),
            encoding="utf-8",
        )
        now = dt.datetime(2031, 11, 15, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))

        with patch.object(self.main, "read_private_json", return_value={
            "date": "2031-11-14",
            "sleep_attempted_record_id": "synthetic-record-id",
            "sleep_attempted_at": "2031-11-14T08:00:00+08:00",
        }):
            state = self.plugin._state(now, None)

        self.assertEqual(state["sleep_attempted_record_id"], "synthetic-record-id")
        self.assertEqual(state["sleep_attempted_at"], "2031-11-14T08:00:00+08:00")

    async def test_legacy_worker_checks_care_at_snapshot_cadence(self):
        self._new_plugin(enabled=False)
        clock = [0.0]
        sleeps = [0]

        async def advance(seconds):
            clock[0] += seconds
            sleeps[0] += 1
            if clock[0] >= 920:
                raise asyncio.CancelledError

        self.plugin._snapshot = AsyncMock(return_value=None)
        self.plugin._care = AsyncMock()
        self.plugin._care_rules_tick = AsyncMock()
        with patch.object(self.main.time, "monotonic", side_effect=lambda: clock[0]):
            with patch.object(self.main.asyncio, "sleep", side_effect=advance):
                with self.assertRaises(asyncio.CancelledError):
                    await self.plugin._worker()

        self.assertEqual(self.plugin._snapshot.await_count, 2)
        self.assertEqual(self.plugin._care.await_count, 2)
        self.assertEqual(self.plugin._care_rules_tick.await_count, 2)

    async def test_sleep_care_revalidates_the_same_record_before_handoff(self):
        self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=False)
        self.plugin.config.update(daily_care=True, bot_qq_id="9876")
        self.plugin._care_repository = self.main.CareRepository(Path(self.temp_dir.name) / "care")
        self.plugin._care_privacy_preflight = AsyncMock(return_value={
            "health_context_to_model_enabled": True,
        })
        self.plugin.health_ai_core_api = object()
        self.plugin.approved_provider_hosts = ("approved.example",)
        self.plugin._cache = {"date": "2031-11-15", "metrics": {}}
        state = {"date": "2031-11-15"}
        self.plugin._state = Mock(return_value=state)
        now = dt.datetime(2031, 11, 15, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        fresh = {
            "date": "2031-11-15",
            "fetched_at": "2031-11-15T09:00:00+08:00",
            "metrics": {},
        }
        self.plugin._snapshot = AsyncMock(return_value=fresh)
        handler = AsyncMock()
        platform = types.SimpleNamespace(bot=types.SimpleNamespace(_handle_event=handler))
        self.plugin.context = types.SimpleNamespace(get_platform_inst=Mock(return_value=platform))

        class SyntheticCQEvent:
            @staticmethod
            def from_payload(payload):
                return FakeEvent(payload["raw_message"], session)

        cq_module = types.ModuleType("aiocqhttp")
        cq_module.Event = SyntheticCQEvent
        previous = sys.modules.get("aiocqhttp")
        sys.modules["aiocqhttp"] = cq_module
        try:
            with patch.object(self.main, "preflight_cooldown_active", return_value=False):
                with patch.object(self.main, "observe_sleep_candidate", side_effect=[
                    (None, True, True), (None, True, True)
                ]):
                    with patch.object(
                        self.main, "preflight_sleep_record",
                        return_value={"record_id": "synthetic-record-id"},
                    ):
                        with patch.object(self.main, "due_kind", return_value="sleep"):
                            await self.plugin._care(now)
                            await self.plugin._flush_pending_legacy_care()
        finally:
            if previous is None:
                sys.modules.pop("aiocqhttp", None)
            else:
                sys.modules["aiocqhttp"] = previous

        self.plugin._snapshot.assert_awaited_once_with(
            "2031-11-15",
            force=True,
            allow_auth_refresh=False,
            allow_auth_file=False,
        )
        self.assertTrue(handler.await_count == 1)
        self.assertTrue(state["sleep_attempted"])
        self.assertEqual(state["sleep_attempted_record_id"], "synthetic-record-id")
        self.assertEqual(self.plugin._pending_care_contexts, {})

    async def test_sleep_care_stops_when_fresh_source_does_not_match(self):
        self._new_plugin(enabled=False)
        self.plugin.config.update(daily_care=True, bot_qq_id="9876")
        self.plugin.health_ai_core_api = object()
        self.plugin.approved_provider_hosts = ("approved.example",)
        self.plugin._cache = {"date": "2031-11-15", "metrics": {}}
        state = {"date": "2031-11-15"}
        self.plugin._state = Mock(return_value=state)
        self.plugin._snapshot = AsyncMock(return_value=None)
        handler = AsyncMock()
        self.plugin.context = types.SimpleNamespace(
            get_platform_inst=Mock(
                return_value=types.SimpleNamespace(
                    bot=types.SimpleNamespace(_handle_event=handler)
                )
            )
        )
        now = dt.datetime(2031, 11, 15, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with patch.object(self.main, "preflight_cooldown_active", return_value=False):
            with patch.object(self.main, "observe_sleep_candidate", return_value=(None, True, False)):
                with patch.object(self.main, "preflight_sleep_record", return_value=None):
                    with patch.object(self.main, "due_kind", return_value="sleep"):
                        await self.plugin._care(now)

        self.plugin._snapshot.assert_awaited_once_with(
            "2031-11-15",
            force=True,
            allow_auth_refresh=False,
            allow_auth_file=False,
        )
        self.assertNotIn("sleep_attempted", state)
        handler.assert_not_awaited()
        self.assertEqual(self.plugin._pending_care_contexts, {})

    async def test_existing_core_contract_gates_and_stages_a_local_page(self):
        api = self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        self.assertTrue(self.plugin.local_pages_enabled)
        event = FakeEvent("我今天体重是多少", session)
        await self.plugin.mark_local_health_request_intent(event)
        self.assertTrue(event.get_extra("_oppo_local_health_request"))
        request = types.SimpleNamespace(
            system_prompt="",
            resolved_provider_api_host="approved.example",
            func_tool=types.SimpleNamespace(remove_tool=Mock()),
        )
        self.plugin._run = AsyncMock(return_value={
            "pages": {
                "weight_history": {
                    "status": "ok",
                    "data": {
                        "source": "synthetic",
                        "date": "2031-11-15",
                        "metrics": {"weight_history_records": [{"value": 60, "unit": "kg"}]},
                    },
                }
            }
        })
        await self.plugin.inject_health(event, request)
        self.plugin._run.assert_awaited_once()
        capture_script = Path(self.plugin._run.call_args.args[0])
        self.assertEqual(capture_script.name, "local_capture.py")
        self.assertEqual(capture_script.parent.name, "collector")
        self.assertEqual(request.local_health_required_provider_host, "approved.example")
        self.assertTrue(request.local_health_sensitive)
        self.assertTrue(event.get_extra("_local_health_sensitive"))
        self.assertEqual(request.system_prompt, "")
        api.stage_local_health_context.assert_called_once()
        self.assertEqual(
            api.stage_local_health_context.call_args.kwargs["required_provider_host"],
            "approved.example",
        )
        self.assertEqual(
            api.stage_local_health_context.call_args.kwargs["allowed_provider_hosts"],
            ("approved.example",),
        )

    async def test_route_preflight_stops_cloud_and_local_reads_before_capture(self):
        self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        self.plugin._run = AsyncMock(
            side_effect=AssertionError("unsupported runner must not capture local pages")
        )
        self.plugin._snapshot = AsyncMock(
            side_effect=AssertionError("unsupported runner must not read cloud data")
        )
        self.plugin._care_privacy_preflight = AsyncMock(
            side_effect=[
                self.main.DispatchRejected("runner_unsupported"),
                self.main.DispatchRejected("runner_unsupported"),
            ]
        )

        for text in ("我今天体重是多少", "我昨晚睡得怎么样"):
            event = FakeEvent(text, session)
            await self.plugin.mark_local_health_request_intent(event)
            request = types.SimpleNamespace(
                system_prompt="",
                resolved_provider_api_host="approved.example",
                func_tool=types.SimpleNamespace(remove_tool=Mock()),
            )
            await self.plugin.inject_health(event, request)
            self.assertTrue(event.stopped)

        self.plugin._run.assert_not_awaited()
        self.plugin._snapshot.assert_not_awaited()
        self.assertEqual(self.plugin._care_privacy_preflight.await_count, 2)

    async def test_old_cloud_snapshot_is_staged_without_mutating_request_prompt(self):
        api = self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        event = FakeEvent("昨晚睡眠怎么样", session)
        request = types.SimpleNamespace(
            system_prompt="base prompt",
            resolved_provider_api_host="approved.example",
            func_tool=types.SimpleNamespace(remove_tool=Mock()),
        )
        snapshot = {
            "date": "2031-11-15",
            "fetched_at": "2031-11-16T06:20:00+08:00",
            "metrics": {"steps": {"total": 42}},
        }
        self.plugin._snapshot = AsyncMock(return_value=snapshot)

        await self.plugin.inject_health(event, request)

        self.plugin._snapshot.assert_awaited_once_with(force=True)
        self.assertEqual(request.system_prompt, "base prompt")
        self.assertTrue(request.local_health_sensitive)
        staged = api.stage_local_health_context.call_args.args[1]
        self.assertIn("42", staged)
        self.assertEqual(
            api.stage_local_health_context.call_args.kwargs["allowed_provider_hosts"],
            ("approved.example",),
        )

    async def test_waiting_hook_marks_cloud_and_worker_health_intents_early(self):
        self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=False)
        cloud_event = FakeEvent("昨晚睡眠怎么样", session)
        care_event = FakeEvent(
            "[OPPO每日关怀 token=synthetic-token] synthetic care topic", session
        )

        await self.plugin.mark_local_health_request_intent(cloud_event)
        await self.plugin.mark_local_health_request_intent(care_event)

        self.assertTrue(cloud_event.get_extra("_oppo_local_health_request"))
        self.assertTrue(care_event.get_extra("_oppo_local_health_request"))

    async def test_health_query_about_another_person_stops_without_reading_data(self):
        self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        event = FakeEvent("我朋友的步数是多少", session)
        request = types.SimpleNamespace(
            system_prompt="base prompt",
            resolved_provider_api_host="approved.example",
            func_tool=types.SimpleNamespace(remove_tool=Mock()),
        )
        self.plugin._snapshot = AsyncMock(
            side_effect=AssertionError("third-party query must not read health data")
        )

        await self.plugin.inject_health(event, request)

        self.assertTrue(event.stopped)
        self.plugin._snapshot.assert_not_awaited()
        self.assertEqual(request.system_prompt, "base prompt")

        weight_event = FakeEvent("我朋友的体重是多少", session)
        await self.plugin.mark_local_health_request_intent(weight_event)
        weight_request = types.SimpleNamespace(
            system_prompt="base prompt",
            resolved_provider_api_host="approved.example",
            func_tool=types.SimpleNamespace(remove_tool=Mock()),
        )
        await self.plugin.inject_health(weight_event, weight_request)
        self.assertTrue(weight_event.stopped)
        self.assertTrue(weight_event.get_extra("_oppo_local_health_request"))
        self.plugin._snapshot.assert_not_awaited()

    async def test_legacy_health_tool_does_not_read_or_return_snapshot_values(self):
        self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        event = FakeEvent("什么都可以", session)
        self.plugin._snapshot = AsyncMock(
            side_effect=AssertionError("direct tool must not read health data")
        )

        result = await self.plugin.get_health(event, "today")

        self.plugin._snapshot.assert_not_awaited()
        self.assertNotIn("42", result)
        self.assertNotIn("步数", result)
        self.assertIn("不直接返回健康数据", result)

    async def test_care_token_stages_private_context_only_on_the_approved_route(self):
        api = self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        self.plugin._pending_care_contexts["synthetic-token"] = (
            "synthetic private health value: 42"
        )
        event = FakeEvent(
            "[OPPO每日关怀 token=synthetic-token] synthetic care topic", session
        )
        request = types.SimpleNamespace(
            system_prompt="base prompt",
            resolved_provider_api_host="approved.example",
            func_tool=types.SimpleNamespace(remove_tool=Mock()),
        )

        await self.plugin.inject_health(event, request)

        self.assertEqual(request.system_prompt, "base prompt")
        self.assertTrue(request.local_health_sensitive)
        staged = api.stage_local_health_context.call_args.args[1]
        self.assertIn("42", staged)
        self.assertEqual(event.message_str.split("]", 1)[1].strip(), "synthetic care topic")

    async def test_live_care_event_is_stopped_before_health_context_staging(self):
        api = self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        self.plugin._pending_care_contexts["synthetic-token"] = "synthetic private health value"
        event = FakeEvent(
            "[OPPO每日关怀 token=synthetic-token] synthetic care topic", session
        )
        event.extras["action_type"] = "live"
        request = types.SimpleNamespace(
            system_prompt="base prompt",
            resolved_provider_api_host="approved.example",
            func_tool=types.SimpleNamespace(remove_tool=Mock()),
        )

        await self.plugin.inject_health(event, request)

        self.assertTrue(event.stopped)
        api.stage_local_health_context.assert_not_called()
        self.assertEqual(request.system_prompt, "base prompt")

    async def test_malformed_core_preflight_stops_before_source_capture(self):
        api = self._install_core_privacy_helpers()
        self._new_plugin(enabled=True)
        self.plugin._care_repository = self.main.CareRepository(Path(self.temp_dir.name) / "care")
        config, revision = self.plugin._care_repository.load_config()
        config["rules"]["weight_date_linked"]["enabled"] = True
        self.plugin._care_repository.save_config(config, revision)
        self._enable_weight_mode(dt.datetime.now(dt.timezone.utc).isoformat())
        api.preflight_local_health_care = Mock(return_value={"supported_runner": True})
        self.plugin._run = AsyncMock(side_effect=AssertionError("preflight must fail closed"))

        await self.plugin._care_rules_tick(
            self.main.dt.datetime(2031, 11, 16, 8, 0, tzinfo=self.main.TZ)
        )

        api.preflight_local_health_care.assert_called_once()
        self.plugin._run.assert_not_awaited()
        self.assertEqual(
            self.plugin.care_privacy_status()["last_route_error_code"],
            "preflight_unavailable",
        )

    async def test_new_scheduler_fails_closed_before_source_capture_without_core_preflight(self):
        api = self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        directory = Path(self.temp_dir.name) / "care"
        self.plugin._care_repository = self.main.CareRepository(directory)
        config, revision = self.plugin._care_repository.load_config()
        config["rules"]["weight_date_linked"]["enabled"] = True
        self.plugin._care_repository.save_config(config, revision)
        self._enable_weight_mode("2031-11-15T00:00:00+00:00")
        del api.preflight_local_health_care
        self.plugin._run = AsyncMock(side_effect=AssertionError("source capture must remain closed"))

        await self.plugin._care_rules_tick(self.main.dt.datetime(2031, 11, 16, 8, 0, tzinfo=self.main.TZ))

        self.plugin._run.assert_not_awaited()
        status = self.plugin.care_privacy_status()
        self.assertTrue(status["core_privacy_api_available"])
        self.assertFalse(status["supported_runner"])
        self.assertFalse(status["health_context_to_model_enabled"])
        self.assertEqual(status["last_route_error_code"], "preflight_unavailable")
        self.assertEqual(self.plugin._pending_care_contexts, {})

    async def test_goodnight_skips_pending_new_jobs_and_clears_legacy_candidate(self):
        self._new_plugin(enabled=True)
        self.plugin._care_repository = self.main.CareRepository(Path(self.temp_dir.name) / "care")
        config, revision = self.plugin._care_repository.load_config()
        config["rules"]["confirmed_period_start"]["enabled"] = True
        self.plugin._care_repository.save_config(config, revision)
        now = self.main.dt.datetime(2031, 11, 16, 8, 0, tzinfo=self.main.TZ)
        event_id = "d" * 64
        state = {
            "schema_version": 1,
            "jobs": {event_id: {
                "event_key": event_id, "rule_id": "confirmed_period_start",
                "target_date": now.date().isoformat(), "basis": "synthetic event",
                "expected_at": (now - self.main.dt.timedelta(minutes=1)).isoformat(),
                "window_end": (now + self.main.dt.timedelta(minutes=15)).isoformat(),
                "timezone": "Asia/Shanghai", "source_observed_at": now.isoformat(),
                "is_actual_event": True, "state": "pending", "reason": "test",
                "updated_at": now.isoformat(), "created_at": now.isoformat(),
            }},
            "attempts": [],
        }
        self.plugin._care_repository.save_scheduler_state(state)
        self.plugin._pending_legacy_care = {"kind": "activity", "context": "synthetic"}
        self.plugin._run = AsyncMock(side_effect=AssertionError("goodnight must stop before capture"))
        self.plugin._goodnight_quiet_for_today = Mock(return_value=True)

        await self.plugin._care_rules_tick(now)

        self.plugin._run.assert_not_awaited()
        self.assertIsNone(self.plugin._pending_legacy_care)
        persisted = self.plugin._care_repository.load_scheduler_state()
        self.assertEqual(persisted["jobs"][event_id]["state"], "skipped")
        self.assertEqual(persisted["attempts"][-1]["outcome"], "skipped")
        self.assertIn("晚安", persisted["attempts"][-1]["reason"])

    async def test_scheduled_ingress_requires_dispatching_job_and_one_use_event(self):
        api = self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        self.plugin._care_repository = self.main.CareRepository(Path(self.temp_dir.name) / "care")
        config, revision = self.plugin._care_repository.load_config()
        rule = config["rules"]["weight_date_linked"]
        rule["enabled"] = True
        self.plugin._care_repository.save_config(config, revision)
        self._enable_weight_mode("2031-11-15T00:00:00+00:00")
        event_id = "a" * 64
        frozen_datetime = _fixed_main_datetime(self.main)
        current = frozen_datetime.now(self.main.TZ)
        target_at = (current - self.main.dt.timedelta(minutes=2)).isoformat()
        self.plugin._activated_at = current - self.main.dt.timedelta(minutes=5)
        target_date = self.main.dt.datetime.fromisoformat(target_at).date().isoformat()
        state = {"schema_version": 1, "jobs": {}, "attempts": []}
        state["jobs"][event_id] = {
            "event_key": event_id,
            "rule_id": "weight_date_linked",
            "target_date": target_date,
            "basis": "体重记录日期",
            "expected_at": target_at,
            "window_end": self.main.dt.datetime.fromisoformat(
                target_date + "T21:00:00+08:00"
            ).isoformat(),
            "timezone": "Asia/Shanghai",
            "source_observed_at": current.isoformat(),
            "is_actual_event": True,
            "state": "dispatching",
            "reason": "test",
            "updated_at": current.isoformat(),
            "created_at": current.isoformat(),
        }
        self.plugin._care_repository.save_scheduler_state(state)
        self.plugin._care_contexts[event_id] = f"合成记录日期为 {target_date}；不包含体重数值。"
        api.preflight_local_health_care = AsyncMock(return_value={
            "supported_runner": True,
            "resolved_provider_host": "approved.example",
            "host_approved": True,
            "health_context_to_model_enabled": True,
            "last_route_error_code": None,
        })
        delivered = {}

        async def handle(event):
            delivered["event"] = event
            request = types.SimpleNamespace(
                system_prompt="base",
                resolved_provider_api_host="approved.example",
                func_tool=types.SimpleNamespace(remove_tool=Mock()),
            )
            await self.plugin.inject_health(event, request)
            delivered["request"] = request

        platform = types.SimpleNamespace(bot=types.SimpleNamespace(_handle_event=handle))
        self.plugin.context = types.SimpleNamespace(get_platform_inst=Mock(return_value=platform))
        class SyntheticCQEvent:
            @staticmethod
            def from_payload(payload):
                return FakeEvent(payload["message"][0]["data"]["text"], session)
        cq_module = types.ModuleType("aiocqhttp")
        cq_module.Event = SyntheticCQEvent
        previous = sys.modules.get("aiocqhttp")
        sys.modules["aiocqhttp"] = cq_module
        plan = {
            "rule_id": "weight_date_linked", "event_id": event_id, "target_at": target_at,
            "source_ref": "oppo_health_local_page", "tone": "gentle",
            "target_date": target_date, "basis": "体重记录日期",
            "is_actual_event": True, "timezone": "Asia/Shanghai",
        }
        try:
            with patch.object(self.main.dt, "datetime", frozen_datetime):
                first_outcome = await self.plugin.submit_scheduled_care(plan)
                self.assertIn(event_id, self.plugin._care_consumed_event_ids, first_outcome)
                with self.assertRaises(self.main.DispatchRejected):
                    await self.plugin.submit_scheduled_care(plan)
        finally:
            if previous is None:
                sys.modules.pop("aiocqhttp", None)
            else:
                sys.modules["aiocqhttp"] = previous

        event_text = delivered["event"].message_str
        self.assertIn("[OPPO每日关怀 token=", event_text)
        self.assertNotIn("2031-11-16", event_text)
        self.assertNotIn("体重", event_text)
        self.assertNotIn("60.0 kg", event_text)
        self.assertIn(target_date, api.stage_local_health_context.call_args.args[1])
        self.assertNotIn("60.0 kg", api.stage_local_health_context.call_args.args[1])
        self.assertEqual(self.plugin._pending_care_contexts, {})

    async def test_synthetic_worker_to_secure_care_ingress_updates_handoff_state(self):
        api = self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        self.plugin._care_repository = self.main.CareRepository(Path(self.temp_dir.name) / "care")
        config, revision = self.plugin._care_repository.load_config()
        rule = config["rules"]["weight_date_linked"]
        rule.update(
            enabled=True,
            send_window={"start": "00:00", "end": "23:59"},
            quiet_hours={"start": "23:59", "end": "00:00"},
        )
        self.plugin._care_repository.save_config(config, revision)
        api.preflight_local_health_care = AsyncMock(return_value={
            "supported_runner": True,
            "resolved_provider_host": "approved.example",
            "host_approved": True,
            "health_context_to_model_enabled": True,
            "last_route_error_code": None,
        })
        now = self.main.dt.datetime.now(self.main.TZ)
        self.plugin._activated_at = now - self.main.dt.timedelta(days=1)
        self._enable_weight_mode(
            (now - self.main.dt.timedelta(minutes=5)).astimezone(
                self.main.dt.timezone.utc
            ).isoformat()
        )
        record_date = now.date().isoformat()
        capture = {
            "status": "ok",
            "pages": {
                "weight_history": {
                    "status": "ok",
                    "data": {
                        "source": "oppo_health_ui_ocr",
                        "observed_at": now.isoformat(),
                        "metrics": {"weight_history_records": [{
                            "record_date": record_date,
                            "measured_at_local": now.strftime("%H:%M"),
                            "observed_at": now.isoformat(),
                            "value": 61.7,
                            "unit": "kg",
                        }]},
                    },
                },
            },
        }
        self.plugin._run = AsyncMock(return_value=capture)
        delivered = {}

        async def handle(event):
            delivered["event"] = event
            await self.plugin.mark_local_health_request_intent(event)
            request = types.SimpleNamespace(
                system_prompt="base prompt",
                resolved_provider_api_host="approved.example",
                func_tool=types.SimpleNamespace(remove_tool=Mock()),
            )
            await self.plugin.inject_health(event, request)
            delivered["request"] = request

        platform = types.SimpleNamespace(bot=types.SimpleNamespace(_handle_event=handle))
        self.plugin.context = types.SimpleNamespace(get_platform_inst=Mock(return_value=platform))
        class SyntheticCQEvent:
            @staticmethod
            def from_payload(payload):
                return FakeEvent(payload["message"][0]["data"]["text"], session)
        cq_module = types.ModuleType("aiocqhttp")
        cq_module.Event = SyntheticCQEvent
        previous = sys.modules.get("aiocqhttp")
        sys.modules["aiocqhttp"] = cq_module
        try:
            await self.plugin._care_rules_tick(now)
        finally:
            if previous is None:
                sys.modules.pop("aiocqhttp", None)
            else:
                sys.modules["aiocqhttp"] = previous

        self.assertIn("event", delivered)
        self.plugin._run.assert_awaited_once()
        self.assertEqual(self.plugin._run.call_args.args[1:], ("--json", "--fields", "weight_history"))
        self.assertFalse(self.plugin._run.call_args.kwargs["allow_auth_file"])
        prompt = delivered["event"].message_str
        self.assertIn("[OPPO每日关怀 token=", prompt)
        self.assertNotIn(record_date, prompt)
        self.assertNotIn("61.7", prompt)
        self.assertNotIn("61.7", api.stage_local_health_context.call_args.args[1])
        self.assertIn(record_date, api.stage_local_health_context.call_args.args[1])
        self.assertEqual(delivered["request"].system_prompt, "base prompt")
        self.assertEqual(self.plugin._pending_care_contexts, {})
        persisted = self.plugin._care_repository.load_scheduler_state()
        self.assertEqual(persisted["attempts"][-1]["outcome"], "handed_off")
        self.assertIn("平台送达状态未确认", persisted["attempts"][-1]["reason"])
        self.assertNotIn("61.7", json.dumps(persisted, ensure_ascii=False))

    async def test_merged_rules_use_one_private_handoff_without_global_daily_cap(self):
        self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        directory = Path(self.temp_dir.name) / "care"
        self.plugin._care_repository = self.main.CareRepository(directory)
        config, revision = self.plugin._care_repository.load_config()
        config["rules"]["confirmed_period_start"]["enabled"] = True
        config["rules"]["confirmed_period_end"]["enabled"] = True
        config["rules"]["weight_date_linked"]["enabled"] = True
        self.plugin._care_repository.save_config(config, revision)
        frozen_datetime = _fixed_main_datetime(self.main)
        now = frozen_datetime.now(self.main.TZ)
        self.plugin._activated_at = now - self.main.dt.timedelta(minutes=5)
        target = (now - self.main.dt.timedelta(minutes=1)).isoformat()
        first_id, second_id, third_id = "a" * 64, "b" * 64, "c" * 64
        scheduler_state = self.plugin._care_repository.load_scheduler_state()

        def add_job(event_id, rule_id):
            rule = config["rules"][rule_id]
            scheduler_state["jobs"][event_id] = {
                "event_key": event_id,
                "rule_id": rule_id,
                "target_date": now.date().isoformat(),
                "basis": "synthetic source reason",
                "expected_at": target,
                "window_end": (now + self.main.dt.timedelta(minutes=15)).isoformat(),
                "timezone": "Asia/Shanghai",
                "source_observed_at": now.isoformat(),
                "is_actual_event": True,
                "state": "dispatching",
                "reason": "test only",
                "updated_at": now.isoformat(),
                "created_at": now.isoformat(),
            }
            self.plugin._care_contexts[event_id] = f"Synthetic safe context for {rule_id}."
            return {
                "rule_id": rule_id,
                "event_id": event_id,
                "target_at": target,
                "source_ref": "oppo_health_local_page",
                "tone": rule["tone"],
                "target_date": now.date().isoformat(),
                "basis": "synthetic source reason",
                "is_actual_event": True,
                "timezone": "Asia/Shanghai",
            }

        plans = [
            add_job(first_id, "confirmed_period_start"),
            add_job(second_id, "confirmed_period_end"),
        ]
        self.plugin._pending_legacy_care = {
            "kind": "sleep",
            "reason_id": "sleep_wake",
            "dispatch_id": "legacy-sleep:synthetic-record",
            "token": "legacy-token",
            "target_at": target,
            "context": "Synthetic safe context for the existing sleep check-in.",
            "event": FakeEvent("[OPPO每日关怀 token=legacy-token] synthetic sleep care", session),
        }
        self.plugin._care_repository.save_scheduler_state(scheduler_state)
        api = self._install_core_privacy_helpers()
        self.plugin._care_privacy_preflight = AsyncMock(return_value={
            "health_context_to_model_enabled": True,
        })
        observed = []

        async def handle(event):
            observed.append((event, dict(self.plugin._pending_care_contexts)))

        self.plugin.context = types.SimpleNamespace(
            get_platform_inst=Mock(return_value=types.SimpleNamespace(
                bot=types.SimpleNamespace(_handle_event=handle)
            ))
        )

        class SyntheticCQEvent:
            @staticmethod
            def from_payload(payload):
                return FakeEvent(payload["message"][0]["data"]["text"], session)

        cq_module = types.ModuleType("aiocqhttp")
        cq_module.Event = SyntheticCQEvent
        previous = sys.modules.get("aiocqhttp")
        sys.modules["aiocqhttp"] = cq_module
        try:
            with patch.object(self.main.dt, "datetime", frozen_datetime):
                await self.plugin.submit_scheduled_care_batch(plans)
                self.assertEqual(len(observed), 1)
                self.assertEqual(len(observed[0][1]), 1)
                merged_context = next(iter(observed[0][1].values()))
                self.assertIn("confirmed_period_start", merged_context)
                self.assertIn("confirmed_period_end", merged_context)
                self.assertIn("existing sleep check-in", merged_context)

                third_plan = add_job(third_id, "weight_date_linked")
                self.plugin._care_repository.save_scheduler_state(scheduler_state)
                await self.plugin.submit_scheduled_care_batch([third_plan])
                self.assertEqual(len(observed), 2)
        finally:
            if previous is None:
                sys.modules.pop("aiocqhttp", None)
            else:
                sys.modules["aiocqhttp"] = previous

    async def test_care_worker_uses_tokenized_safe_ingress_and_cleans_pending_context(self):
        api = self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        now = self.main.dt.datetime(2031, 11, 16, 8, 0, tzinfo=self.main.TZ)
        snapshot = {
            "date": "2031-11-16",
            "fetched_at": "2031-11-16T08:00:00+08:00",
            "metrics": {"steps": {"total": 42}},
        }
        self.plugin.config.update(daily_care=True, random_activity_care=True)
        self.plugin._care_repository = self.main.CareRepository(Path(self.temp_dir.name) / "care")
        self.plugin._cache = snapshot
        self.plugin._activated_at = now
        self.plugin._last_user_activity = None
        self.plugin._state = Mock(return_value={"date": now.date().isoformat()})
        self.plugin._save_state = Mock()

        delivered = {}

        async def handle_synthetic_event(event):
            delivered["event"] = event
            await self.plugin.mark_local_health_request_intent(event)
            request = types.SimpleNamespace(
                system_prompt="base prompt",
                resolved_provider_api_host="approved.example",
                func_tool=types.SimpleNamespace(remove_tool=Mock()),
            )
            await self.plugin.inject_health(event, request)
            delivered["request"] = request

        platform = types.SimpleNamespace(
            bot=types.SimpleNamespace(_handle_event=handle_synthetic_event)
        )
        self.plugin.context = types.SimpleNamespace(
            get_platform_inst=Mock(return_value=platform)
        )

        class SyntheticCQEvent:
            @staticmethod
            def from_payload(payload):
                prompt = payload["message"][0]["data"]["text"]
                return FakeEvent(prompt, session)

        cq_module = types.ModuleType("aiocqhttp")
        cq_module.Event = SyntheticCQEvent
        previous = sys.modules.get("aiocqhttp")
        sys.modules["aiocqhttp"] = cq_module
        try:
            with patch.object(self.main, "due_kind", return_value="activity"):
                await self.plugin._care(now)
                await self.plugin._flush_pending_legacy_care()
        finally:
            if previous is None:
                sys.modules.pop("aiocqhttp", None)
            else:
                sys.modules["aiocqhttp"] = previous

        event_text = delivered["event"].message_str
        self.assertIn("[OPPO每日关怀 token=", event_text)
        self.assertNotIn("步数：42", event_text)
        self.assertTrue(
            delivered["event"].get_extra("_oppo_local_health_request")
        )
        self.assertIn("42", api.stage_local_health_context.call_args.args[1])
        self.assertEqual(delivered["request"].system_prompt, "base prompt")
        self.assertEqual(self.plugin._pending_care_contexts, {})

    async def test_non_health_multimodal_event_is_left_to_the_standard_pipeline(self):
        self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        event = FakeEvent("看一下这张图", session)
        event.message_obj.message = [FakePlain("看一下这张图"), object()]
        request = types.SimpleNamespace(system_prompt="base prompt")
        self.plugin._snapshot = AsyncMock(
            side_effect=AssertionError("ordinary image path must not read health data")
        )

        await self.plugin.inject_health(event, request)

        self.plugin._snapshot.assert_not_awaited()
        self.assertFalse(event.stopped)
        self.assertEqual(request.system_prompt, "base prompt")

    async def test_unapproved_request_host_never_starts_local_capture(self):
        self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        event = FakeEvent("我今天体重是多少", session)
        await self.plugin.mark_local_health_request_intent(event)
        request = types.SimpleNamespace(
            system_prompt="",
            resolved_provider_api_host="other.example",
            func_tool=types.SimpleNamespace(remove_tool=Mock()),
        )
        self.plugin._run = AsyncMock(side_effect=AssertionError("capture must not run"))
        await self.plugin.inject_health(event, request)
        self.plugin._run.assert_not_awaited()
        self.assertTrue(event.stopped)

    async def test_unavailable_page_stages_only_a_no_guess_context(self):
        api = self._install_core_privacy_helpers()
        session = self._new_plugin(enabled=True)
        event = FakeEvent("我今天体重是多少", session)
        await self.plugin.mark_local_health_request_intent(event)
        request = types.SimpleNamespace(
            system_prompt="",
            resolved_provider_api_host="approved.example",
            func_tool=types.SimpleNamespace(remove_tool=Mock()),
        )
        self.plugin._run = AsyncMock(return_value={"status": "unavailable", "pages": {}})
        await self.plugin.inject_health(event, request)
        staged_context = api.stage_local_health_context.call_args.args[1]
        self.assertIn("本机页面暂不可用；不得推测数值", staged_context)
        self.assertEqual(
            api.stage_local_health_context.call_args.kwargs["required_provider_host"]
            , "approved.example"
        )
        self.assertTrue(getattr(request, "local_health_sensitive", False))
        self.assertTrue(event.get_extra("_local_health_sensitive"))

    async def test_missing_intent_hook_disables_local_pages_without_breaking_plugin_entry(self):
        self._install_core_privacy_helpers()
        self.main.filter = types.SimpleNamespace()
        self._new_plugin(enabled=True)
        self.assertFalse(self.plugin.local_pages_enabled)
        self.assertIsNone(self.plugin.health_ai_core_api)
        self.assertEqual(self.plugin.approved_provider_hosts, ())
        self.assertTrue(any("request-intent hook or privacy API is unavailable" in item for item in self.logger.errors))
        decorator = self.main._on_waiting_llm_request()
        function = lambda: None
        self.assertIs(decorator(function), function)

    def _install_core_privacy_helpers(self):
        provider = _module("astrbot.core.provider")
        provider.__path__ = []
        privacy = _module(
            "astrbot.core.provider.local_health_privacy",
            stage_local_health_context=Mock(),
            clear_local_health_context=Mock(),
            local_health_request_context=Mock(),
            is_local_health_request=Mock(return_value=False),
            preflight_local_health_care=Mock(return_value={
                "supported_runner": True,
                "resolved_provider_host": "approved.example",
                "host_approved": True,
                "health_context_to_model_enabled": True,
                "last_route_error_code": None,
            }),
        )
        _module(
            "astrbot.core.provider.entities",
            provider_allowed_for_request=Mock(
                side_effect=lambda api_base, host: (
                    api_base == f"https://{host}/" and host == "approved.example"
                )
            ),
            sanitize_provider_api_host=Mock(return_value="approved.example"),
        )
        return privacy


if __name__ == "__main__":
    unittest.main()
