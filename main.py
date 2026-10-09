"""Private OPPO summaries, wake-up care and one optional daytime care."""
import asyncio
import datetime as dt
import inspect
import json
import os
import signal
from pathlib import Path
import time
import sys
import uuid
from zoneinfo import ZoneInfo
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import Plain
from astrbot.core.star.session_plugin_manager import SessionPluginManager
from .care_api import CarePageAPI
from .care_logic import (
    due_kind,
    new_activity_due,
    observe_sleep_candidate,
    preflight_cooldown_active,
    preflight_sleep_record,
    wake_due,
)
from .care_integration import enabled_capture_fields, facts_from_capture
from .care_scheduler import (
    DispatchRejected,
    dispatch_due_async,
    pause_for_privacy,
)
from .care_store import CareRepository, CareStorageError
from .collector.storage import private_root, read_private_json, write_private_json
from .local_request_policy import (
    format_local_health_context,
    health_query_mentions_known_metric,
    health_query_mentions_other_person,
    load_core_privacy_api,
    local_fields_for_query,
    local_request_allowed,
    normalize_approved_provider_hosts,
    provider_host_is_approved,
    remove_cloud_health_tool,
)
NAME = 'oppo_health'
TZ = ZoneInfo('Asia/Shanghai')
HEALTH_WORDS = ('健康', '心率', '血氧', '睡眠', '步数', '手表', '运动', '不舒服', '累', '困', '没睡', '熬夜', '睡得', '睡了', '走了', '跑步', '锻炼')

def _on_waiting_llm_request():
    register_hook = getattr(filter, 'on_waiting_llm_request', None)
    return register_hook() if callable(register_hook) else (lambda function: function)

def describe(snapshot):
    lines = [f"数据日期：{snapshot['date']}；读取时间：{snapshot['fetched_at']}"]
    m = snapshot.get('metrics', {})
    if 'steps' in m:
        lines.append(f"步数：{m['steps']['total']} 步")
    if 'heart_rate' in m:
        r = m['heart_rate']
        lines.append(f"最近一次心率：{r['latest']} 次/分（测于 {r['measured_at']}）")
        if 'resting' in r:
            lines.append(f"静息心率：{r['resting']} 次/分")
    if 'blood_oxygen' in m:
        r = m['blood_oxygen']
        lines.append(f"最近一次血氧：{r['latest']}%（测于 {r['measured_at']}）")
    if 'sleep' in m:
        minutes = int(m['sleep']['minutes'])
        lines.append(f"本日睡眠记录：{minutes // 60} 小时 {minutes % 60} 分钟")
        lines.append(f"入睡：{m['sleep'].get('bedtime')}；起床：{m['sleep'].get('wake_time')}（按起床日期归档）")
    if snapshot.get('errors'):
        lines.append('部分指标暂不可用。')
    if not m:
        lines.append('当前没有可用记录；不能将缺失数据当作 0。')
    return '\n'.join(lines)

class OPPOHealth(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.umo = str(config.get('private_session', ''))
        self.root = private_root()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_path = self.root / 'daily-care.json'
        self._cache = None
        self._cache_time = 0
        self._lock = asyncio.Lock()
        self._task = None
        self._activated_at = dt.datetime.now(TZ)
        self._last_user_activity = None
        self._pending_care_contexts = {}
        self._care_repository = None
        self._care_page_api = None
        self._care_facts = {}
        self._care_contexts = {}
        self._care_dispatch_lock = asyncio.Lock()
        self._care_inflight_event_ids = set()
        self._care_consumed_event_ids = set()
        self._care_privacy_status = {
            "source_state": "not_configured",
            "source_observed_at": None,
            "route_observed_at": None,
            "core_privacy_api_available": False,
            "supported_runner": False,
            "resolved_provider_host": None,
            "host_approved": False,
            "health_context_to_model_enabled": False,
            "last_route_error_code": "preflight_unavailable",
            "data_sources": [],
        }
        self.approved_provider_hosts = normalize_approved_provider_hosts(
            config.get('approved_provider_hosts', [])
        )
        self.health_ai_core_api = None
        if self.approved_provider_hosts:
            core_api, reason = load_core_privacy_api()
            if core_api is None:
                self.approved_provider_hosts = ()
                logger.error(
                    '[oppo_health] Health-to-model access disabled: AstrBot privacy API is unavailable (%s).',
                    reason,
                )
            elif not all(
                provider_host_is_approved(core_api, host)
                for host in self.approved_provider_hosts
            ):
                self.approved_provider_hosts = ()
                logger.error(
                    '[oppo_health] Health-to-model access disabled: a configured provider host was rejected by AstrBot core.'
                )
            else:
                self.health_ai_core_api = core_api
        if self.health_ai_core_api is not None and not callable(
            getattr(filter, 'on_waiting_llm_request', None)
        ):
            self.approved_provider_hosts = ()
            self.health_ai_core_api = None
            logger.error(
                '[oppo_health] Health-to-model access disabled: the request-intent hook is unavailable.'
            )
        self.local_pages_enabled = bool(
            config.get('enable_local_health_pages', False)
            and self.health_ai_core_api is not None
            and callable(getattr(filter, 'on_waiting_llm_request', None))
        )
        if config.get('enable_local_health_pages', False) and not self.local_pages_enabled:
            logger.error(
                '[oppo_health] Local page access disabled: the request-intent hook or privacy API is unavailable.'
            )
    async def initialize(self):
        self._register_care_page()
        if not self.umo or ':FriendMessage:' not in self.umo:
            logger.warning('[oppo_health] 请先配置本人 QQ 私聊会话；后台任务暂未启动。')
            return
        self._task = asyncio.create_task(self._worker())
        logger.info('[oppo_health] 已加载；仅限配置的 QQ 私聊，后台刷新间隔 15 分钟。')

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._care_page_api is not None:
            self._care_page_api.unregister()
            self._care_page_api = None

    def _register_care_page(self):
        """Register local plugin-page APIs without reading health data."""
        if self._care_page_api is not None:
            return
        try:
            directory = StarTools.get_data_dir(NAME) / "care"
            self._care_repository = CareRepository(directory)
            self._care_page_api = CarePageAPI(
                self.context,
                self.config,
                self._care_repository,
                status_provider=self.read_care_privacy_status,
            )
            self._care_page_api.register()
        except Exception:
            self._care_repository = None
            self._care_page_api = None
            logger.error('[oppo_health] 本机关怀页面无法安全初始化；新规则保持暂停。')

    def care_privacy_status(self):
        """Return only cached readiness; GET requests never refresh or collect."""
        return dict(self._care_privacy_status)

    async def read_care_privacy_status(self):
        """Refresh only the core's read-only route preflight, never the health source."""
        try:
            await self._care_privacy_preflight()
        except DispatchRejected:
            pass
        return self.care_privacy_status()

    async def _care_privacy_preflight(self):
        """Ask a core-owned read-only route/runner preflight before local capture.

        Missing or malformed core results keep scheduled care fail-closed.
        """
        core_available = self.health_ai_core_api is not None
        preflight = getattr(self.health_ai_core_api, "preflight_local_health_care", None)
        if not core_available or not callable(preflight):
            status = {
                "source_state": self._care_privacy_status.get("source_state", "unavailable"),
                "source_observed_at": self._care_privacy_status.get("source_observed_at"),
                "route_observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "core_privacy_api_available": core_available,
                "supported_runner": False,
                "resolved_provider_host": None,
                "host_approved": False,
                "health_context_to_model_enabled": False,
                "last_route_error_code": "preflight_unavailable",
                "data_sources": self._care_privacy_status.get("data_sources", []),
            }
            self._care_privacy_status = status
            raise DispatchRejected("privacy_preflight_unavailable")
        try:
            raw = preflight(
                context=self.context,
                private_session_umo=self.umo,
                approved_provider_hosts=tuple(self.approved_provider_hosts),
            )
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception:
            raw = None
        allowed_errors = {None, "unapproved_host", "runner_unsupported", "private_session_unavailable"}
        raw_error_code = raw.get("last_route_error_code") if isinstance(raw, dict) else None
        valid_shape = (
            isinstance(raw, dict)
            and type(raw.get("supported_runner")) is bool
            and (raw.get("resolved_provider_host") is None or isinstance(raw.get("resolved_provider_host"), str))
            and type(raw.get("host_approved")) is bool
            and type(raw.get("health_context_to_model_enabled")) is bool
            and (
                raw_error_code is None
                or (isinstance(raw_error_code, str) and raw_error_code in allowed_errors)
            )
        )
        if not valid_shape:
            self._care_privacy_status = {
                "source_state": self._care_privacy_status.get("source_state", "unavailable"),
                "source_observed_at": self._care_privacy_status.get("source_observed_at"),
                "route_observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "core_privacy_api_available": core_available,
                "supported_runner": False,
                "resolved_provider_host": None,
                "host_approved": False,
                "health_context_to_model_enabled": False,
                "last_route_error_code": "preflight_unavailable",
                "data_sources": self._care_privacy_status.get("data_sources", []),
            }
            raise DispatchRejected("privacy_preflight_unavailable")
        raw_host = raw.get("resolved_provider_host")
        host = None
        if isinstance(raw_host, str):
            from .local_request_policy import normalize_provider_host
            host = normalize_provider_host(raw_host)
        host_approved = bool(
            raw.get("host_approved") is True
            and host is not None
            and host in self.approved_provider_hosts
            and provider_host_is_approved(self.health_ai_core_api, host)
        )
        runner_ok = raw.get("supported_runner") is True
        enabled = bool(
            core_available
            and runner_ok
            and host_approved
            and raw.get("health_context_to_model_enabled") is True
            and raw.get("last_route_error_code") is None
        )
        error_code = raw.get("last_route_error_code")
        if not enabled and error_code is None:
            error_code = "unapproved_host" if runner_ok else "runner_unsupported"
        self._care_privacy_status = {
            "source_state": self._care_privacy_status.get("source_state", "unavailable"),
            "source_observed_at": self._care_privacy_status.get("source_observed_at"),
            "route_observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "core_privacy_api_available": core_available,
            "supported_runner": runner_ok,
            "resolved_provider_host": host,
            "host_approved": host_approved,
            "health_context_to_model_enabled": enabled,
            "last_route_error_code": error_code,
            "data_sources": self._care_privacy_status.get("data_sources", []),
        }
        if not enabled:
            if error_code == "unapproved_host":
                raise DispatchRejected("provider_unapproved")
            if error_code == "private_session_unavailable":
                raise DispatchRejected("private_session_unavailable")
            raise DispatchRejected("runner_unsupported")
        return dict(self._care_privacy_status)

    async def _request_route_preflight(self, request_provider_host):
        """Require the core's session-specific route to match the live request."""
        from .local_request_policy import normalize_provider_host

        request_host = normalize_provider_host(request_provider_host)
        if request_host is None:
            return False
        try:
            status = await self._care_privacy_preflight()
        except DispatchRejected:
            return False
        return bool(
            status.get("health_context_to_model_enabled") is True
            and status.get("resolved_provider_host") == request_host
        )

    async def _care_rules_tick(self, now):
        """Read the minimum selected local pages and dispatch metadata-only jobs."""
        if self._care_repository is None:
            return
        try:
            config, _revision = self._care_repository.load_config()
            state = self._care_repository.load_scheduler_state()
        except CareStorageError:
            logger.warning('[oppo_health] 本机关怀规则状态无法安全读取；本轮保持暂停。')
            return
        fields = enabled_capture_fields(config)
        if not fields:
            self._care_facts = {}
            self._care_contexts.clear()
            self._care_privacy_status.update(
                source_state="not_configured",
                source_observed_at=None,
                data_sources=[],
                last_route_error_code=None,
            )
            return
        parts = self.umo.rsplit(':', 2)
        if (
            len(parts) != 3 or parts[1] != 'FriendMessage' or not parts[2].isdigit()
            or not str(self.config.get('bot_qq_id', '')).isdigit()
            or not await SessionPluginManager.is_plugin_enabled_for_session(self.umo, NAME)
        ):
            self._care_contexts.clear()
            state = pause_for_privacy(state, now, "本人私聊会话当前不可用")
            self._care_repository.save_scheduler_state(state)
            self._care_privacy_status.update(
                source_state="unavailable",
                last_route_error_code="private_session_unavailable",
            )
            return
        try:
            await self._care_privacy_preflight()
        except DispatchRejected as exc:
            self._care_contexts.clear()
            reason = {
                "privacy_preflight_unavailable": "核心隐私预检不可用，已暂停提交",
                "runner_unsupported": "当前回复运行方式不受支持，已暂停提交",
                "provider_unapproved": "当前模型服务未获本机健康数据授权，已暂停提交",
                "private_session_unavailable": "本人私聊会话当前不可用",
            }.get(exc.code, "核心隐私预检不可用，已暂停提交")
            state = pause_for_privacy(state, now, reason)
            self._care_repository.save_scheduler_state(state)
            self._care_facts = {}
            self._care_contexts.clear()
            return

        script = Path(__file__).parent / 'collector/local_capture.py'
        if not script.is_file():
            capture = None
        else:
            try:
                async with self._lock:
                    capture = await self._run(
                        script,
                        '--json',
                        '--fields',
                        ','.join(fields),
                        timeout=240,
                        allow_auth_file=False,
                    )
            except (OSError, asyncio.TimeoutError):
                capture = None
        try:
            facts, contexts, source_statuses = facts_from_capture(capture, config, now)
        except (TypeError, ValueError, KeyError):
            facts, contexts, source_statuses = {}, {}, []
        del capture
        self._care_facts = facts
        self._care_contexts = contexts
        source_states = {item.get("status") for item in source_statuses}
        source_state = (
            "ready" if "ok" in source_states
            else ("stale" if "stale" in source_states else "unavailable")
        )
        observed = [
            item.get("observed_at") for item in source_statuses
            if isinstance(item.get("observed_at"), str)
        ]
        self._care_privacy_status.update(
            source_state=source_state,
            source_observed_at=max(observed) if observed else None,
            data_sources=source_statuses,
        )
        try:
            state, _results = await dispatch_due_async(
                config,
                facts,
                state,
                now,
                self,
                persist=self._care_repository.save_scheduler_state,
            )
            self._care_repository.save_scheduler_state(state)
        except CareStorageError:
            logger.warning('[oppo_health] 本机关怀调度状态无法安全保存；本轮保持暂停。')

    async def submit_scheduled_care(self, plan):
        """Submit one metadata-only plan through AstrBot's existing private handler."""
        required = {
            "rule_id", "event_id", "target_at", "source_ref", "tone", "target_date",
            "basis", "is_actual_event", "timezone",
        }
        if not isinstance(plan, dict) or set(plan) != required:
            raise DispatchRejected("dispatch_rejected")
        rule_id = plan.get("rule_id")
        event_id = plan.get("event_id")
        if rule_id not in {"predicted_period_lead", "confirmed_period_start", "confirmed_period_end", "weight_date_linked"}:
            raise DispatchRejected("dispatch_rejected")
        if not isinstance(event_id, str) or len(event_id) != 64 or any(c not in "0123456789abcdef" for c in event_id):
            raise DispatchRejected("dispatch_rejected")
        async with self._care_dispatch_lock:
            if event_id in self._care_consumed_event_ids or event_id in self._care_inflight_event_ids:
                raise DispatchRejected("event_consumed")
            self._care_inflight_event_ids.add(event_id)
        token = None
        try:
            if self._care_repository is None:
                raise DispatchRejected("source_context_unavailable")
            config, _revision = self._care_repository.load_config()
            rule = config["rules"].get(rule_id)
            if not isinstance(rule, dict) or rule.get("enabled") is not True:
                raise DispatchRejected("rule_disabled")
            now = dt.datetime.now(ZoneInfo(rule["timezone"]))
            try:
                target_at = dt.datetime.fromisoformat(str(plan.get("target_at", "")).replace("Z", "+00:00"))
            except ValueError:
                raise DispatchRejected("event_not_due")
            if target_at.tzinfo is None or target_at.utcoffset() is None or target_at > now:
                raise DispatchRejected("event_not_due")
            state = self._care_repository.load_scheduler_state()
            job = state["jobs"].get(event_id)
            if (
                not isinstance(job, dict)
                or job.get("rule_id") != rule_id
                or job.get("state") != "dispatching"
                or job.get("expected_at") != plan.get("target_at")
                or plan.get("source_ref") != "oppo_health_local_page"
                or plan.get("tone") != rule.get("tone")
                or plan.get("target_date") != job.get("target_date")
                or plan.get("timezone") != job.get("timezone")
            ):
                raise DispatchRejected("event_consumed")
            parts = self.umo.rsplit(':', 2)
            if (
                len(parts) != 3 or parts[1] != 'FriendMessage' or not parts[2].isdigit()
                or not str(self.config.get('bot_qq_id', '')).isdigit()
                or not await SessionPluginManager.is_plugin_enabled_for_session(self.umo, NAME)
            ):
                raise DispatchRejected("private_session_unavailable")
            await self._care_privacy_preflight()
            care_context = self._care_contexts.get(event_id)
            if not isinstance(care_context, str) or not care_context:
                raise DispatchRejected("source_context_unavailable")
            platform = self.context.get_platform_inst(parts[0])
            bot = getattr(platform, 'bot', None)
            handler = getattr(bot, '_handle_event', None) or getattr(bot, 'handle_event', None)
            if not callable(handler):
                raise DispatchRejected("handler_unavailable")
            from aiocqhttp import Event
            token = uuid.uuid4().hex
            prompt = f"[OPPO每日关怀 token={token}] 请依据已核验的本人记录，用温和、不诊断的语气简短关心。"
            event = Event.from_payload({
                'post_type': 'message',
                'message_type': 'private',
                'sub_type': 'friend',
                'message_id': time.time_ns() % 2147483647,
                'user_id': int(parts[2]),
                'self_id': int(self.config['bot_qq_id']),
                'time': int(time.time()),
                'message': [{'type': 'text', 'data': {'text': prompt}}],
                'raw_message': prompt,
                'font': 0,
                'sender': {'user_id': int(parts[2]), 'nickname': '健康关怀'},
            })
            if event is None:
                raise DispatchRejected("handler_unavailable")
            self._pending_care_contexts[token] = care_context
            self._care_consumed_event_ids.add(event_id)
            await handler(event)
        finally:
            if token is not None:
                self._pending_care_contexts.pop(token, None)
            self._care_contexts.pop(event_id, None)
            self._care_inflight_event_ids.discard(event_id)

    async def dispatch(self, plan):
        """Scheduler adapter that forwards only to the plugin-owned care ingress."""
        return await self.submit_scheduled_care(plan)
    async def _allowed(self, event):
        if not self.umo or ':FriendMessage:' not in self.umo or event.unified_msg_origin != self.umo:
            return False
        return await SessionPluginManager.is_plugin_enabled_for_session(self.umo, NAME)
    @_on_waiting_llm_request()
    async def mark_local_health_request_intent(self, event: AstrMessageEvent):
        """Mark plain-text health intents before request hooks see history."""
        if not await self._allowed(event):
            return
        text = event.message_str or ''
        if (
            text.startswith('[OPPO每日关怀 token=')
            or local_fields_for_query(text)
            or any(word in text for word in HEALTH_WORDS)
            or (
                health_query_mentions_other_person(text)
                and health_query_mentions_known_metric(text)
            )
        ):
            event.set_extra('_oppo_local_health_request', True)

    async def _run(self, script, *args, timeout=95, allow_auth_file=True):
        python = self.config.get('collector_python') or sys.executable
        env = os.environ.copy()
        if allow_auth_file and self.config.get('auth_file'):
            env['OPPO_HEALTH_AUTH_FILE'] = str(Path(self.config['auth_file']).expanduser())
        elif not allow_auth_file:
            env.pop('OPPO_HEALTH_AUTH_FILE', None)
        create_kwargs = {
            'env': env,
            'stdout': asyncio.subprocess.PIPE,
            'stderr': asyncio.subprocess.PIPE,
        }
        if os.name != 'nt':
            create_kwargs['start_new_session'] = True
        process = await asyncio.create_subprocess_exec(
            python, str(script), *args, **create_kwargs)
        communication_task = asyncio.create_task(process.communicate())
        try:
            stdout, _stderr = await asyncio.wait_for(
                asyncio.shield(communication_task), timeout=timeout)
        except asyncio.TimeoutError:
            await self._terminate_collector_process(process, communication_task)
            return None
        except asyncio.CancelledError:
            await self._terminate_collector_process(process, communication_task)
            raise
        if process.returncode != 0:
            return None
        try:
            return json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            return None

    async def _terminate_collector_process(self, process, communication_task):
        """Stop a timed-out collector and drain its subprocess pipes."""
        if os.name == 'nt':
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            signals = (None,)
        else:
            signals = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)
        for sig in signals:
            if communication_task.done():
                break
            if sig is not None:
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
            try:
                await asyncio.wait_for(
                    asyncio.shield(communication_task), timeout=1)
                break
            except asyncio.TimeoutError:
                if os.name == 'nt' and sig is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            except Exception:
                break
        if not communication_task.done():
            communication_task.cancel()
        try:
            await communication_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug('[oppo_health] Collector process communication stopped during cleanup.', exc_info=True)
    async def _snapshot(
        self, date=None, force=False, allow_auth_refresh=True, allow_auth_file=True
    ):
        async with self._lock:
            today = dt.datetime.now(TZ).date().isoformat()
            date = date or today
            if not force and self._cache and self._cache.get('date') == date and time.monotonic() - self._cache_time < 600:
                return self._cache
            script = Path(self.config.get('collector_script') or Path(__file__).parent / 'collector/snapshot.py').expanduser()
            result = await self._run(
                script, '--json', '--date', date, allow_auth_file=allow_auth_file
            )
            if allow_auth_refresh and self.config.get('emulator_auth_refresh', False) and result and any(isinstance(e, dict) and e.get('error_code') == 10101
                              for e in result.get('errors', {}).values()):
                # SDK refresh runs only in the isolated copy, never on the USB phone.
                await self._run(
                    script.with_name('extract_emulator_auth.py'),
                    allow_auth_file=allow_auth_file,
                )
                result = await self._run(
                    script, '--json', '--date', date, allow_auth_file=allow_auth_file
                )
            if result and result.get('date') == date:
                if date == today:
                    self._cache, self._cache_time = result, time.monotonic()
                return result
            return None
    @filter.command('oppohealth')
    async def command_health(self, event: AstrMessageEvent):
        if not await self._allowed(event):
            return
        snapshot = await self._snapshot(force=True)
        yield event.plain_result(describe(snapshot) if snapshot else '健康数据暂时读取不到，请稍后重试。')

    @filter.llm_tool(name='get_oppo_health')
    async def get_health(self, event: AstrMessageEvent, date: str = 'today'):
        """Refuse direct health tool results outside the protected request path.

        Args:
            date (string): Kept for compatibility with existing tool schemas.
        """
        if not await self._allowed(event):
            return '当前会话无权访问健康数据。'
        return '为保护隐私，此工具不直接返回健康数据；请直接询问本人健康记录。'

    @filter.on_llm_request()
    async def inject_health(self, event: AstrMessageEvent, req):
        if not await self._allowed(event):
            return
        text = event.message_str or ''
        if text.startswith('[OPPO每日关怀 token='):
            token = text.split(']', 1)[0].removeprefix('[OPPO每日关怀 token=')
            care_context = self._pending_care_contexts.get(token)
            provider_host = getattr(req, 'resolved_provider_api_host', None)
            if (
                care_context is None
                or self.health_ai_core_api is None
                or not self.approved_provider_hosts
                or event.get_extra('action_type') == 'live'
                or not remove_cloud_health_tool(getattr(req, 'func_tool', None))
                or not local_request_allowed(
                    self.umo,
                    event.unified_msg_origin,
                    provider_host,
                    self.approved_provider_hosts,
                    self.health_ai_core_api,
                )
            ):
                event.stop_event()
                return
            if not await self._request_route_preflight(provider_host):
                event.stop_event()
                return
            event.set_extra('_local_health_sensitive', True)
            event.set_extra('provider_request', None)
            req.local_health_required_provider_host = provider_host
            req.local_health_sensitive = True
            self.health_ai_core_api.stage_local_health_context(
                req,
                care_context,
                required_provider_host=provider_host,
                allowed_provider_hosts=self.approved_provider_hosts,
            )
            return
        self._last_user_activity = dt.datetime.now(TZ)
        message_obj = getattr(event, 'message_obj', None)
        components = getattr(message_obj, 'message', None)
        requested_local_fields = local_fields_for_query(text)
        health_words_in_query = any(word in text for word in HEALTH_WORDS)
        if (
            health_query_mentions_other_person(text)
            and health_query_mentions_known_metric(text)
        ):
            event.stop_event()
            return
        if not health_words_in_query and not requested_local_fields:
            return
        if (
            self.health_ai_core_api is None
            or not self.approved_provider_hosts
            or not remove_cloud_health_tool(getattr(req, 'func_tool', None))
        ):
            event.stop_event()
            return
        plain_text = bool(components) and all(
            isinstance(component, Plain) for component in components
        )
        if requested_local_fields and self.local_pages_enabled:
            if not event.get_extra('_oppo_local_health_request') or not plain_text:
                event.stop_event()
                return
            local_fields = requested_local_fields
        elif requested_local_fields and not health_words_in_query:
            event.stop_event()
            return
        else:
            local_fields = []
        if not plain_text:
            event.stop_event()
            return
        provider_host = getattr(req, 'resolved_provider_api_host', None)
        if (
            event.get_extra('action_type') == 'live'
            or not local_request_allowed(
                self.umo,
                event.unified_msg_origin,
                provider_host,
                self.approved_provider_hosts,
                self.health_ai_core_api,
            )
        ):
            logger.warning('[oppo_health] Health request refused by the private-session/provider gate.')
            event.stop_event()
            return
        if not await self._request_route_preflight(provider_host):
            logger.warning('[oppo_health] Health request refused by the read-only session route preflight.')
            event.stop_event()
            return
        event.set_extra('_local_health_sensitive', True)
        event.set_extra('provider_request', None)
        req.local_health_required_provider_host = provider_host
        req.local_health_sensitive = True
        if local_fields:
            capture_script = Path(__file__).parent / 'collector/local_capture.py'
            try:
                async with self._lock:
                    result = await self._run(
                        capture_script,
                        '--json',
                        '--fields',
                        ','.join(local_fields),
                        timeout=240,
                        allow_auth_file=False,
                    )
            except (OSError, asyncio.TimeoutError):
                result = None
            self.health_ai_core_api.stage_local_health_context(
                req,
                format_local_health_context(result, local_fields),
                required_provider_host=provider_host,
                allowed_provider_hosts=self.approved_provider_hosts,
            )
            return
        snapshot = await self._snapshot(force=True)
        health_summary = (
            describe(snapshot)
            if snapshot
            else '当前没有可用记录；不能将缺失数据当作 0。'
        )
        self.health_ai_core_api.stage_local_health_context(
            req,
            '[本人健康记录，仅用于本次健康话题]\n'
            + health_summary
            + '\n沿用当前人格的性格和语气自然回应，不要把读取时间当测量时间，旧记录不能说成实时值。通常不报精确测量时间；只有记录较旧、容易误认实时或用户追问时，才用“上午那次记录”等自然说法说明。不要诊断，不要罗列全部数值，不要声称实时监控或能控制手表。',
            required_provider_host=provider_host,
            allowed_provider_hosts=self.approved_provider_hosts,
        )
    def _state(self, now, snapshot):
        previous = {}
        if self.state_path.exists():
            try:
                state = read_private_json(self.state_path)
                if state.get('date') == now.date().isoformat():
                    return state
                previous = state
            except (ValueError, OSError):
                raise RuntimeError('Daily health care state is unreadable')
        state = {'date': now.date().isoformat(),
                 'activity_due': new_activity_due(now, wake_due(snapshot, now))}
        for key in ('sleep_attempted_record_id', 'sleep_attempted_at'):
            if previous.get(key):
                state[key] = previous[key]
        self._save_state(state)
        return state
    def _save_state(self, state):
        write_private_json(state, self.state_path)
    async def _care(self, now):
        if not self.config.get('daily_care', False):
            return
        if self.health_ai_core_api is None or not self.approved_provider_hosts:
            return
        if not await SessionPluginManager.is_plugin_enabled_for_session(self.umo, NAME):
            return
        if not str(self.config.get('bot_qq_id', '')).isdigit():
            return
        parts = self.umo.rsplit(':', 2)
        if len(parts) != 3 or parts[1] != 'FriendMessage' or not parts[2].isdigit():
            return
        platform = self.context.get_platform_inst(parts[0])
        bot = getattr(platform, 'bot', None)
        handler = getattr(bot, '_handle_event', None) or getattr(bot, 'handle_event', None)
        if not handler:
            return
        # Scheduling checks reuse the background snapshot rather than polling
        # the cloud every twenty seconds when the query cache expires.
        snapshot = self._cache
        state = self._state(now, snapshot)
        _, _, state_changed = observe_sleep_candidate(state, snapshot, now)
        if state_changed:
            self._save_state(state)
        kind = due_kind(state, snapshot, now, self._activated_at, self._last_user_activity)
        if kind == 'activity' and not self.config.get('random_activity_care', True):
            return
        if kind is None:
            return

        delivery_snapshot = snapshot
        sleep_record_id = None
        if kind == 'sleep':
            if preflight_cooldown_active(state, now):
                return
            state['sleep_preflight_at'] = now.isoformat()
            self._save_state(state)
            fresh_snapshot = await self._snapshot(
                snapshot.get('date'), force=True, allow_auth_refresh=False,
                allow_auth_file=False,
            )
            preflight_now = dt.datetime.now(TZ)
            candidate = preflight_sleep_record(snapshot, fresh_snapshot, preflight_now)
            _, stable, _ = observe_sleep_candidate(state, fresh_snapshot, preflight_now)
            state['sleep_preflight_at'] = preflight_now.isoformat()
            self._save_state(state)
            if candidate is None or not stable:
                logger.info('[oppo_health] Sleep-care preflight skipped after revalidation failed.')
                return
            if due_kind(state, fresh_snapshot, preflight_now, self._activated_at,
                        self._last_user_activity) != 'sleep':
                logger.info('[oppo_health] Sleep-care preflight skipped after timing recheck.')
                return
            delivery_snapshot = fresh_snapshot
            sleep_record_id = candidate['record_id']
            now = preflight_now

        from aiocqhttp import Event
        topic = ('用户手表记录的起床时间已过去约一小时，关心昨晚睡眠。'
                 if kind == 'sleep' else '这是今天随机安排的一次活动关怀，关心运动、久坐休息或最近一次心率。')
        care_token = uuid.uuid4().hex
        care_context = (
            '[OPPO每日关怀，仅用于本次私聊回复]\n'
            + (describe(delivery_snapshot) if delivery_snapshot else '今天暂时无法读取记录。')
            + '\n请沿用当前人格的性格和语气主动发一小段自然问候，选一项有记录的情况轻轻关心即可，不要逐项报表，通常不报精确测量时间；旧记录需要说明时用“上午那次记录”等自然说法，不把旧测量当实时值。不诊断、不夸大，不说实时监控。'
        )
        prompt = ('[OPPO每日关怀 token=' + care_token + '] ' + topic + ' 请沿用当前人格的性格和语气主动发一小段自然问候，'
                  '选一项有记录的情况轻轻关心即可，不要逐项报表，通常不报精确测量时间；旧记录需要说明时用“上午那次记录”等自然说法，不把旧测量当实时值。不诊断、不夸大，不说实时监控。'
                  '没有记录就温柔询问今天过得怎么样。')
        uid = int(parts[2])
        event = Event.from_payload({'post_type': 'message', 'message_type': 'private', 'sub_type': 'friend',
                                    'message_id': time.time_ns() % 2147483647, 'user_id': uid,
                                    'self_id': int(self.config['bot_qq_id']), 'time': int(time.time()),
                                    'message': [{'type': 'text', 'data': {'text': prompt}}],
                                    'raw_message': prompt, 'font': 0,
                                    'sender': {'user_id': uid, 'nickname': '每日健康关怀'}})
        if event is None:
            raise RuntimeError('Health care event could not be constructed')
        # Reserve only after the private event is fully constructed, but
        # before dispatch so a reload cannot duplicate a completed attempt.
        attempt_at = dt.datetime.now(TZ).isoformat()
        state[kind + '_attempted'] = True
        state[kind + '_attempted_at'] = attempt_at
        if kind == 'sleep':
            state['sleep_attempted_record_id'] = sleep_record_id
        self._save_state(state)
        self._pending_care_contexts[care_token] = care_context
        try:
            await handler(event)
        finally:
            self._pending_care_contexts.pop(care_token, None)
        logger.info('[oppo_health] %s 关怀已交给私聊回复流程。', kind)
    async def _worker(self):
        next_poll = 0
        next_rules_poll = 0
        while True:
            try:
                if time.monotonic() >= next_poll:
                    await self._snapshot(force=True)
                    next_poll = time.monotonic() + 900
                    await self._care(dt.datetime.now(TZ))
                if time.monotonic() >= next_rules_poll:
                    await self._care_rules_tick(dt.datetime.now(TZ))
                    next_rules_poll = time.monotonic() + 900
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('[oppo_health] 本次健康刷新或关怀未完成；保留原数据，等待下一次检查。')
            await asyncio.sleep(20)
