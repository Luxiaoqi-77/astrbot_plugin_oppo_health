"""Private OPPO summaries, wake-up care and one optional daytime care."""
import asyncio
from copy import deepcopy
import datetime as dt
import inspect
import hashlib
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
from astrbot.core.message.components import Image, Plain
from astrbot.core.star.session_plugin_manager import SessionPluginManager
from .care_api import CarePageAPI
from .care_logic import (
    due_kind,
    new_activity_due,
    legacy_dispatch_skip_reason,
    sleep_record,
    wake_due,
    explicit_goodnight_message,
)
from .care_integration import enabled_capture_fields, event_key, facts_from_capture
from .period_flow import build_end_confirmation_request
from .period_runtime import (
    claim_confirmation,
    mark_inquiry_delivery,
    mark_inquiry_outcome,
    reserve_inquiry,
    settle_confirmation,
)
from .care_scheduler import (
    DispatchRejected,
    dispatch_due_async,
    dispatch_window_skip_reason,
    pause_for_privacy,
)
from .care_store import CareRepository, CareStorageError
from .delivery_runtime import PendingCareContexts
from .delivery_store import SleepDeliveryStore
from .outbound_ack import (
    ATTEMPT_EXTRA,
    OUTCOME_EXTRA,
    PERIOD_INQUIRY_EXTRA,
    PERIOD_OUTCOME_EXTRA,
    RANDOM_ACTIVITY_OUTCOME_EXTRA,
    attach_attempt,
    attach_period_inquiry,
    attach_random_activity_attempt,
    settle_after_send,
    settle_random_activity_after_send,
)
try:
    from data.plugins.proactive_delivery_coordination import (
        get_shared_proactive_delivery_coordinator,
    )
except ImportError:
    from proactive_delivery_coordination import (
        get_shared_proactive_delivery_coordinator,
    )

COORDINATION_TOKEN_EXTRA = "_oppo_proactive_coordination_token"

from .random_activity_freshness import current_random_activity_snapshot
from .health_freshness import (
    capture_is_fresh,
    capture_observation_receipt,
    measurement_receipts_are_fresh,
    current_cloud_snapshot,
    parse_aware,
    retry_fresh_context,
    source_receipts_are_fresh,
)
from .random_activity_dispatch import (
    eligible as random_activity_eligible,
    empty_ledger as empty_random_activity_ledger,
    finish as finish_random_activity_attempt,
    migrate_legacy_attempt as migrate_random_activity_legacy,
    prepare_ledger as prepare_random_activity_ledger,
    recover_in_flight as recover_random_activity_in_flight,
    reserve as reserve_random_activity_attempt,
    defer_unstarted_refresh as defer_random_refresh,
    suppress_day as suppress_random_activity_day,
)
from .sleep_dispatch import (
    POLICY_PRIVATE_MESSAGE,
    RETRYABLE_PRECHECK_REASONS,
    begin_attempt,
    finish_attempt,
    recover_in_flight,
    migrate_to_chat_reply,
    record_chat_wake,
    claim_chat_reply,
    recover_unselected_chat_claim,
    release_chat_reply_claim,
    reserve_chat_attempt,
)
from .care_wellness import (
    explicit_weight_mode_command,
    evaluate_capture as evaluate_wellness_capture,
    sunlight_candidate,
    weekly_candidate,
    wellness_source_summary,
)
from .low_steps_care import (
    aware as steps_aware,
    check_once as check_low_steps_once,
    dispatch_skip_reason as low_steps_dispatch_skip_reason,
    prepare_ledger as prepare_other_care_ledger,
    reserve_other_attempt,
    valid_steps,
    window_end as low_steps_window_end,
)
from .collector.storage import private_root, read_private_json, write_private_json
from .local_request_policy import (
    format_local_health_context,
    explicit_owner_health_data_query,
    health_query_mentions_known_metric,
    health_query_mentions_other_person,
    load_core_privacy_api,
    local_fields_for_query,
    local_request_allowed,
    normalize_approved_provider_hosts,
    provider_host_is_approved,
    remove_cloud_health_tool,
)
from astrbot.core.utils.model_output_guard import is_consciousness_sync
NAME = 'oppo_health'
TZ = ZoneInfo('Asia/Shanghai')
SLEEP_CARE_TARGET_UMO = 'qq_napcat_100000002:FriendMessage:100000001'
CARE_CONTEXT_TTL_SECONDS = 900
MAX_PENDING_CARE_CONTEXTS = 32
CARE_TOKEN_EXTRA = '_oppo_care_context_token'
RANDOM_ACTIVITY_EVENT_EXTRA = '_oppo_random_activity_care'
# 2026-10-16（主人要求）：撞车时短暂等待共享协调器冷却，不直接把关怀丢掉。
# 上限 3 分钟；超时仍被挡则本不发（理由写入结果与日志）。
CARE_COORDINATION_MAX_WAIT_SECONDS = 180.0
# 合并话题句（不含任何具体数值，只表达“想关心的事”）。
CARE_MERGE_TOPICS = {
    "predicted_period_lead": "她这几天正是经期前后，人可能会不舒服",
    "confirmed_period_start": "她经期刚开始，人可能会不舒服",
    "post_period_care": "她经期刚结束，身体可能还没缓过来",
    "period_late_inquiry": "她这几天经期不太规律，可以轻轻问一句",
    "wellness_low_state": "她今天身心状态值偏低，看着有点疲惫",
    "sunlight_evening": "她今天几乎没晒太阳，可以拉她出去走走",
    "weight_date_linked": "她想记录体重这件事",
    "low_steps": "她今天步数很少，可能没怎么出门",
    "sleep": "她昨晚休息得怎么样",
}
CARE_MERGE_HINT = (
    "\n\n【合并提示】你正好也想关心她一件事：{topic}。"
    "请在同一条回复里自然地一并说出来（不要分成两条消息，不要提“系统/提醒/定时/健康关怀”这类字眼，"
    "不要报具体数值或测量时间）。"
)
# 2026-10-16（主人要求）：允许关怀在稍晚送达时用“我刚刚看你…”这类口径自然衔接，
# 不暴露具体延迟或不提“系统/定时/提醒”。
CARE_DELIVERY_WORDING_HINT = (
    "\n若这条关怀比原计划稍晚才送达（例如和另一条主动消息撞上后延后了一会儿），"
    "可以用“我刚看了你的记录”这类自然措辞衔接，不要说出具体延迟，"
    "不要说“定时/系统/提醒/延迟”这类字眼。"
)
RANDOM_ACTIVITY_ATTEMPT_ID_EXTRA = '_oppo_random_activity_attempt_id'
HEALTH_WORDS = ('健康', '心率', '血氧', '睡眠', '步数', '手表', '运动', '不舒服', '累', '困', '没睡', '熬夜', '睡得', '睡了', '走了', '跑步', '锻炼')

def _on_waiting_llm_request():
    register_hook = getattr(filter, 'on_waiting_llm_request', None)
    return register_hook() if callable(register_hook) else (lambda function: function)

def describe(snapshot, *, include_step_measurement_time=True):
    lines = [f"数据日期：{snapshot['date']}；读取时间：{snapshot['fetched_at']}"]
    m = snapshot.get('metrics', {})
    if 'steps' in m:
        step_line = f"步数：{m['steps']['total']} 步"
        if include_step_measurement_time:
            step_line += f"（累计样本截至：{m['steps']['measured_at']}；快照读取：{snapshot['fetched_at']}；这是该时点的累计记录）"
        lines.append(step_line)
    if 'heart_rate' in m:
        r = m['heart_rate']
        lines.append(f"最近一次心率：{r['latest']} 次/分（测于 {r['measured_at']}）")
        if 'resting' in r and r.get('resting_measured_at'):
            lines.append(f"最近一次静息心率记录：{r['resting']} 次/分（测于 {r['resting_measured_at']}）")
    if 'blood_oxygen' in m:
        r = m['blood_oxygen']
        lines.append(f"最近一次血氧：{r['latest']}%（测于 {r['measured_at']}）")
    if 'sleep' in m:
        minutes = int(m['sleep']['minutes'])
        lines.append(f"最近完成的睡眠记录：{minutes // 60} 小时 {minutes % 60} 分钟")
        lines.append(f"入睡：{m['sleep'].get('bedtime')}；起床：{m['sleep'].get('wake_time')}（按起床日期归档；不是实时生理指标）")
        if m['sleep'].get('record_modified_at'):
            lines.append(f"来源记录更新时间：{m['sleep']['record_modified_at']}")
    if snapshot.get('errors'):
        lines.append('部分指标暂不可用。')
    if not m:
        lines.append('当前没有可用记录；不能将缺失数据当作 0。')
    return '\n'.join(lines)


def current_health_snapshot(value, now):
    snapshot, _has_metrics = current_cloud_snapshot(value, now)
    if snapshot is None:
        return None
    metrics = snapshot.get('metrics')
    if isinstance(metrics, dict) and 'sleep' in metrics:
        errors = snapshot.get('errors')
        if (isinstance(errors, dict) and 'sleep' in errors) or sleep_record(snapshot, now) is None:
            metrics.pop('sleep', None)
    return snapshot

class OPPOHealth(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.umo = str(config.get('private_session', ''))
        self.root = private_root()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_path = self.root / 'daily-care.json'
        self._other_care_path = self.root / 'other-care.json'
        self._other_care_lock = asyncio.Lock()
        self._random_activity_path = self.root / 'random-activity-delivery.json'
        self._random_activity_lock = asyncio.Lock()
        self._random_activity_ready = False
        self._sleep_delivery = SleepDeliveryStore(
            self.root / 'sleep-delivery.json',
            self.state_path,
            read_private_json,
            write_private_json,
        )
        self._sleep_delivery_ready = False
        self._wake_sleep_sync_task = None
        self._wake_sleep_sync_anchor = None
        self._cache = None
        self._cache_time = 0
        self._lock = asyncio.Lock()
        self._refresh_guard = asyncio.Lock()
        self._refresh_tasks = {}
        self._task = None
        self._care_refresh_retry_at = 0.0
        self._activated_at = dt.datetime.now(TZ)
        self._last_user_activity = None
        self._pending_care_contexts = PendingCareContexts(
            ttl_seconds=CARE_CONTEXT_TTL_SECONDS,
            max_entries=MAX_PENDING_CARE_CONTEXTS,
        )
        self._care_repository = None
        self._care_page_api = None
        self._care_facts = {}
        self._care_contexts = {}
        self._care_context_receipts = {}
        self._care_measurement_receipts = {}
        self._pending_legacy_care = None
        # 2026-10-16（主人要求）：撞车时若正好有可合并的主动回合，
        # 把“想关心她一件事”的意图交给那一轮一起说（不注入原始数值）。
        self._care_merge_intent = None
        self._care_dispatch_lock = asyncio.Lock()
        self.proactive_coordinator = get_shared_proactive_delivery_coordinator()
        self._care_inflight_event_ids = set()
        self._care_consumed_event_ids = set()
        self._period_runtime_lock = asyncio.Lock()
        self._period_sync_tasks = {}
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
    async def _initialize_sleep_delivery(self):
        """Migrate once and lock interrupted sends before starting the worker."""
        now = dt.datetime.now(TZ)
        try:
            async with self._sleep_delivery.lock:
                ledger = self._sleep_delivery.load(now)
                recovered = recover_in_flight(ledger, now)
                migrated = migrate_to_chat_reply(ledger, now)
                recovered_claim = recover_unselected_chat_claim(ledger, now)
                if recovered or migrated or recovered_claim:
                    self._sleep_delivery.save(ledger)
            self._sleep_delivery_ready = True
            return True
        except (OSError, ValueError, TypeError):
            self._sleep_delivery_ready = False
            logger.warning(
                '[oppo_health] Sleep-care dispatch state is unreadable; sleep care remains paused.'
            )
            return False

    async def _initialize_random_activity_delivery(self):
        """Initialize the separate metadata ledger and conservatively migrate old markers."""
        if (self.config.get('random_activity_care') is not True
                or self.config.get('daily_care') is not True):
            self._random_activity_ready = False
            return False
        now = dt.datetime.now(TZ)
        try:
            async with self._random_activity_lock:
                ledger, ledger_exists = self._load_random_activity_ledger(now, allow_missing=True)
                if not ledger_exists and self.state_path.exists():
                    if self.state_path.is_symlink():
                        raise ValueError('legacy daily-care state cannot be a symlink')
                    legacy = read_private_json(self.state_path)
                    if not isinstance(legacy, dict):
                        raise ValueError('legacy daily-care state is invalid')
                    migrate_random_activity_legacy(
                        ledger, now.date().isoformat(),
                        legacy.get('date') == now.date().isoformat()
                        and legacy.get('activity_attempted') is True,
                        now,
                    )
                if recover_random_activity_in_flight(ledger, now) or not ledger_exists:
                    write_private_json(ledger, self._random_activity_path)
            self._random_activity_ready = True
            return True
        except (OSError, ValueError, TypeError):
            self._random_activity_ready = False
            logger.warning('[oppo_health] Random-activity delivery state is unavailable; random care remains paused.')
            return False

    def _load_random_activity_ledger(self, now, *, allow_missing=False):
        """Read only a bounded regular ledger file and return it with existence metadata."""
        path = self._random_activity_path
        if path.is_symlink():
            raise ValueError('random-activity state cannot be a symlink')
        exists = path.exists()
        if not exists:
            if not allow_missing:
                raise ValueError('random-activity state is missing')
            return empty_random_activity_ledger(now), False
        if path.stat().st_size > 65536:
            raise ValueError('random-activity state is oversized')
        raw = read_private_json(path)
        return prepare_random_activity_ledger(raw, now), True

    async def _finish_random_activity(self, attempt_id, outcome, now, **metadata):
        if not self._random_activity_ready:
            return False
        try:
            async with self._random_activity_lock:
                ledger, _exists = self._load_random_activity_ledger(now)
                changed, _result = finish_random_activity_attempt(
                    ledger, attempt_id, outcome, now, **metadata
                )
                if changed:
                    write_private_json(ledger, self._random_activity_path)
                return changed
        except (OSError, ValueError, TypeError):
            self._random_activity_ready = False
            logger.warning('[oppo_health] Random-activity receipt could not be persisted; further random care is paused.')
            return False

    async def _recover_sleep_delivery_on_stop(self):
        """Persist uncertain outcomes for every send interrupted by shutdown."""
        if not self._sleep_delivery_ready:
            self._pending_care_contexts = PendingCareContexts(
                ttl_seconds=CARE_CONTEXT_TTL_SECONDS,
                max_entries=MAX_PENDING_CARE_CONTEXTS,
            )
            return
        now = dt.datetime.now(TZ)
        try:
            async with self._sleep_delivery.lock:
                ledger = self._sleep_delivery.load(now)
                if recover_in_flight(ledger, now):
                    self._sleep_delivery.save(ledger)
        except (OSError, ValueError, TypeError):
            logger.warning(
                '[oppo_health] Interrupted sleep-care outcomes could not be persisted; dispatch stays locked.'
            )
        self._pending_care_contexts = PendingCareContexts(
            ttl_seconds=CARE_CONTEXT_TTL_SECONDS,
            max_entries=MAX_PENDING_CARE_CONTEXTS,
        )

    async def initialize(self):
        self._register_care_page()
        await self._initialize_sleep_delivery()
        await self._initialize_random_activity_delivery()
        await self._recover_period_sync_state()
        if not self.umo or ':FriendMessage:' not in self.umo:
            logger.warning('[oppo_health] 请先配置本人 QQ 私聊会话；后台任务暂未启动。')
            return
        self._task = asyncio.create_task(self._worker())
        logger.info('[oppo_health] 已加载；仅限配置的 QQ 私聊，后台刷新间隔 15 分钟。')

    async def terminate(self):
        if self._wake_sleep_sync_task is not None:
            self._wake_sleep_sync_task.cancel()
            await asyncio.gather(self._wake_sleep_sync_task, return_exceptions=True)
            self._wake_sleep_sync_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for task in list(self._period_sync_tasks.values()):
            task.cancel()
        if self._period_sync_tasks:
            await asyncio.gather(*self._period_sync_tasks.values(), return_exceptions=True)
        self._period_sync_tasks.clear()
        await self._recover_sleep_delivery_on_stop()
        if self._random_activity_ready:
            now = dt.datetime.now(TZ)
            try:
                async with self._random_activity_lock:
                    ledger, _exists = self._load_random_activity_ledger(now)
                    if recover_random_activity_in_flight(ledger, now):
                        write_private_json(ledger, self._random_activity_path)
            except (OSError, ValueError, TypeError):
                self._random_activity_ready = False
                logger.warning('[oppo_health] Interrupted random-activity outcomes could not be reconciled; dispatch stays locked.')
        if self._care_page_api is not None:
            self._care_page_api.unregister()
            self._care_page_api = None

    async def _expire_pending_care_contexts(self):
        """Expire bounded prompt contexts and lock abandoned attempts unknown."""
        expired = self._pending_care_contexts.expire(time.monotonic())
        random_attempt_ids = [
            entry.get('random_activity_attempt_id')
            for _token, entry in expired
            if isinstance(entry, dict) and isinstance(entry.get('random_activity_attempt_id'), str)
        ]
        for attempt_id in random_attempt_ids:
            await self._finish_random_activity(
                attempt_id, 'unknown', dt.datetime.now(TZ), reason='pending_context_expired'
            )
        attempt_ids = [
            entry.get('attempt_id')
            for _token, entry in expired
            if isinstance(entry, dict) and isinstance(entry.get('attempt_id'), str)
        ]
        if not attempt_ids or not self._sleep_delivery_ready:
            return
        now = dt.datetime.now(TZ)
        try:
            async with self._sleep_delivery.lock:
                ledger = self._sleep_delivery.load(now)
                changed = False
                for attempt_id in attempt_ids:
                    attempt_day = next(
                        (
                            day for day in ledger['days'].values()
                            if day.get('active_attempt_id') == attempt_id
                        ),
                        None,
                    )
                    if not isinstance(attempt_day, dict) or attempt_day.get('delivery_state') != 'in_flight':
                        continue
                    done, _reason = finish_attempt(ledger, attempt_id, 'unknown', now)
                    changed = changed or done
                if changed:
                    self._sleep_delivery.save(ledger)
        except (OSError, ValueError, TypeError):
            self._sleep_delivery_ready = False
            logger.warning(
                '[oppo_health] Expired sleep-care context could not be reconciled; new sleep dispatch is paused.'
            )

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

    async def _recover_period_sync_state(self):
        """Fence interrupted confirmation syncs as unknown; never retry them."""
        if self._care_repository is None:
            return
        try:
            async with self._period_runtime_lock:
                state = self._care_repository.load_period_state()
                confirmation = state.get("confirmation")
                if isinstance(confirmation, dict) and confirmation.get("status") in {
                    "sync_claimed", "sync_running",
                }:
                    state = settle_confirmation(
                        state, confirmation["event_id"], dt.datetime.now(TZ)
                    )
                    self._care_repository.save_period_state(state)
        except (CareStorageError, ValueError, TypeError):
            logger.warning('[oppo_health] 经期确认同步状态无法安全恢复；不重试已开始的操作。')

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
        # Never let a failed poll leave an earlier capture eligible for dispatch.
        self._care_facts = {}
        self._care_contexts.clear()
        self._care_context_receipts.clear()
        self._care_measurement_receipts.clear()
        if self._care_repository is None:
            return
        try:
            config, _revision = self._care_repository.load_config()
            state = self._care_repository.load_scheduler_state()
            wellness_state = self._care_repository.load_wellness_state()
            period_state = self._care_repository.load_period_state()
        except CareStorageError:
            logger.warning('[oppo_health] 本机关怀规则状态无法安全读取；本轮保持暂停。')
            return
        if self._goodnight_quiet_for_today(now):
            reason = "本人已直接说晚安；本地日期剩余时间停止主动关怀"
            self._pending_legacy_care = None
            for job in state["jobs"].values():
                if job.get("state") == "pending":
                    job.update(state="skipped", reason=reason, updated_at=now.isoformat())
                    state["attempts"].append({
                        "job_id": job.get("event_key", ""),
                        "rule_id": job.get("rule_id", ""),
                        "at": now.isoformat(),
                        "outcome": "skipped",
                        "reason": reason,
                    })
                    del state["attempts"][:-5000]
            self._care_repository.save_scheduler_state(state)
            self._care_facts = {}
            self._care_contexts.clear()
            return
        source_config = deepcopy(config)
        if not wellness_state.get("weight_mode_enabled"):
            source_config["rules"]["weight_date_linked"]["enabled"] = False
        fields = enabled_capture_fields(source_config)
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

        try:
            capture = await retry_fresh_context(
                lambda: self._capture_local_fields(fields, timeout=240),
                lambda value: value if capture_is_fresh(value, fields, dt.datetime.now(TZ)) else None,
                lambda: not self._goodnight_quiet_for_today(dt.datetime.now(TZ)),
            )
        except (OSError, asyncio.TimeoutError):
            capture = None
        if capture is None:
            logger.info('[oppo_health] 本轮本机页面读取缺少新鲜时间凭据；跳过数据关怀。')
        capture_now = dt.datetime.now(TZ)
        try:
            facts, contexts, source_statuses = facts_from_capture(
                capture,
                source_config,
                capture_now,
                weight_mode_enabled_at=wellness_state.get("weight_mode_enabled_at")
                if wellness_state.get("weight_mode_enabled") else None,
                requested_fields=fields,
                period_inquiry_sent_dates={
                    item["date"] for item in period_state.get("inquiries", [])
                    if period_state.get("active_cycle") is not None
                    and item.get("cycle_id") == period_state["active_cycle"].get("cycle_id")
                },
                period_confirmed_cycle_ids={
                    period_state["confirmation"]["cycle_id"]
                } if isinstance(period_state.get("confirmation"), dict) else set(),
                period_postcare_cycle_ids=set(period_state.get("postcare_cycle_ids", [])),
            )
        except (TypeError, ValueError, KeyError):
            facts, contexts, source_statuses = {}, {}, []

        reserved_inquiries = []
        for inquiry in list(facts.get("period_late_inquiry", [])):
            try:
                period_state, reserved = await self._reserve_period_inquiry(inquiry, now)
            except (CareStorageError, ValueError, TypeError):
                reserved = False
            if reserved:
                reserved_inquiries.append(inquiry)
            else:
                facts.get("period_late_inquiry", []).remove(inquiry)
                contexts.pop(event_key("period_late_inquiry", inquiry.get("event_id", "")), None)
        if not facts.get("period_late_inquiry"):
            facts.pop("period_late_inquiry", None)
        wellness_rule = config["rules"]["wellness_low_state"]
        if wellness_rule["enabled"]:
            wellness_state = self._care_repository.load_wellness_state()
            source_row = wellness_source_summary(capture, wellness_rule["timezone"], now)
            wellness_state, evaluation, sample = evaluate_wellness_capture(
                capture, wellness_rule, wellness_state, now
            )
            source_row.update(
                evaluation_state=evaluation["status"],
                reason=evaluation["reason"],
            )
            source_page = capture.get("pages", {}).get("wellness_home") if isinstance(capture, dict) else None
            if sample is not None:
                source_row["collection_state"] = "usable"
            elif evaluation["status"] in {"paused_source", "duplicate_sample"}:
                if not isinstance(source_page, dict):
                    source_row["collection_state"] = "collection_failed"
                elif source_page.get("status") == "no_visible_value":
                    source_row["collection_state"] = "no_records"
                else:
                    source_row["collection_state"] = "filtered"
            elif evaluation["status"] in {"parameters_unconfirmed", "timezone_unconfirmed", "invalid_settings", "invalid_timezone"}:
                source_row["collection_state"] = "configuration_required"
            elif isinstance(source_page, dict) and source_page.get("status") == "ok":
                source_row["collection_state"] = "usable"
            elif isinstance(source_page, dict):
                source_row["collection_state"] = "filtered"
            else:
                source_row["collection_state"] = "collection_failed"
            if evaluation.get("data_date"):
                paused_for_source = evaluation["status"] == "paused_source"
                stale_reasons = {
                    "cross_day_data", "future_update_time", "future_source_time",
                    "stale_source_sample", "out_of_order_source_sample",
                }
                source_row.update(
                    data_date=evaluation["data_date"],
                    measured_at=evaluation.get("measured_at"),
                    observed_at=evaluation.get("observed_at"),
                    category=evaluation.get("category"),
                    quality=evaluation.get("quality"),
                    completeness=evaluation.get("completeness"),
                    status=(
                        "stale" if evaluation.get("reason_code") in stale_reasons
                        else ("unavailable" if paused_for_source else "ok")
                    ),
                    is_stale=evaluation.get("reason_code") in stale_reasons,
                )
            if evaluation["status"] not in {
                "disabled", "parameters_unconfirmed", "timezone_unconfirmed",
                "invalid_settings", "invalid_timezone",
            }:
                wellness_state, fact, slot_reason = weekly_candidate(
                    wellness_state, sample, evaluation, wellness_rule, now
                )
                if fact is not None:
                    source_row["reason"] = slot_reason
                    facts["wellness_low_state"] = [fact]
                    self._care_measurement_receipts[event_key(
                        "wellness_low_state", fact["event_id"]
                    )] = {
                        "wellness": {
                            "measured_at": sample.measured_at.isoformat(),
                            "max_age_minutes": wellness_rule["maximum_sample_age_minutes"],
                            "data_date": sample.data_date.isoformat(),
                            "timezone": wellness_rule["timezone"],
                        }
                    }
                    contexts[event_key("wellness_low_state", fact["event_id"])] = (
                        "本人连续的独立状态测量满足已设置的持续偏低条件。"
                        "请以温柔、不评判的方式关心本人，帮助放松和舒缓情绪；不要提分值、不要把设备状态等同真实心情、不要诊断。"
                    )
                elif evaluation["status"] == "paused_source":
                    source_row["reason"] = evaluation["reason"]
                else:
                    source_row["reason"] = slot_reason
            self._care_repository.save_wellness_state(wellness_state)
            source_statuses.append(source_row)
        sunlight_rule = config["rules"]["sunlight_evening"]
        if sunlight_rule["enabled"]:
            wellness_state = self._care_repository.load_wellness_state()
            wellness_state, fact, source_row = sunlight_candidate(
                capture, sunlight_rule, wellness_state, now
            )
            if fact is not None:
                facts["sunlight_evening"] = [fact]
                contexts[event_key("sunlight_evening", fact["event_id"])] = (
                    "本人 OPPO 健康记录明确显示今天日照记录在 0–5 分钟范围内。"
                    "请自然、温柔地问候；不责备、不诊断，也不把缺失记录说成零。"
                )
            self._care_repository.save_wellness_state(wellness_state)
            source_statuses.append(source_row)
        local_capture_receipt = capture_observation_receipt(capture, fields) if capture is not None else None
        self._care_facts = facts
        self._care_contexts = {
            event_id: (
                context
                + "\n本次关怀依据当前成功读取的本机页面。observed_at 只表示读取时间；"
                "周期、体重和睡眠是日期/测量记录，wellness 必须按样本时刻表达；"
                "没有可靠测量时刻的字段不得说成当前生理状态，缺项不得用旧缓存或历史对话补齐。"
            )
            for event_id, context in contexts.items()
        }
        if capture is not None:
            self._care_context_receipts = {
                event_id: local_capture_receipt for event_id in contexts
            }
        del capture
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
                source_config,
                facts,
                state,
                now,
                self,
                persist=self._care_repository.save_scheduler_state,
                not_before=self._activated_at,
            )
            self._care_repository.save_scheduler_state(state)
            for inquiry in reserved_inquiries:
                job_id = event_key("period_late_inquiry", inquiry["event_id"])
                job = state["jobs"].get(job_id)
                if not isinstance(job, dict):
                    continue
                outcome_map = {
                    "handed_off": "handed_off",
                    "failed": "failed",
                    "failed_uncertain": "failed_uncertain",
                    "skipped": "skipped",
                }
                outcome = outcome_map.get(job.get("state"))
                if outcome is None:
                    continue
                async with self._period_runtime_lock:
                    latest_period_state = self._care_repository.load_period_state()
                    latest_period_state = mark_inquiry_outcome(
                        latest_period_state, inquiry["event_id"], outcome, now
                    )
                    self._care_repository.save_period_state(latest_period_state)
        except CareStorageError:
            logger.warning('[oppo_health] 本机关怀调度状态无法安全保存；本轮保持暂停。')

    async def submit_scheduled_care(self, plan):
        """Submit one plan through the merged, private health-care ingress."""
        return await self.submit_scheduled_care_batch([plan])

    async def dispatch_batch(self, plans):
        """Merge simultaneous scheduled reasons into one private handoff."""
        return await self.submit_scheduled_care_batch(plans)

    async def submit_scheduled_care_batch(self, plans):
        """Validate, preflight, and hand off metadata-only care plans."""
        required = {
            "rule_id", "event_id", "target_at", "source_ref", "tone", "target_date",
            "basis", "is_actual_event", "timezone",
        }
        pending_legacy = self._pending_legacy_care
        if pending_legacy is not None and pending_legacy.get("kind") == "sleep":
            self._pending_legacy_care = None
            return {"status": "skipped", "reason": "sleep_chat_reply_only"}
        if not isinstance(plans, list) or len(plans) > 12 or (not plans and pending_legacy is None):
            raise DispatchRejected("dispatch_rejected")
        allowed_rule_ids = {
            "predicted_period_lead", "confirmed_period_start", "confirmed_period_end",
            "post_period_care", "period_late_inquiry",
            "weight_date_linked", "wellness_low_state", "sunlight_evening",
        }
        event_ids = []
        period_inquiry_ids = []
        for plan in plans:
            if not isinstance(plan, dict) or set(plan) != required:
                raise DispatchRejected("dispatch_rejected")
            if plan.get("rule_id") not in allowed_rule_ids:
                raise DispatchRejected("dispatch_rejected")
            event_id = plan.get("event_id")
            if not isinstance(event_id, str) or len(event_id) != 64 or any(c not in "0123456789abcdef" for c in event_id):
                raise DispatchRejected("dispatch_rejected")
            event_ids.append(event_id)
            if plan.get("rule_id") == "period_late_inquiry":
                period_inquiry_ids.append(event_id)
        if len(set(event_ids)) != len(event_ids):
            raise DispatchRejected("dispatch_rejected")
        if len(period_inquiry_ids) > 1:
            raise DispatchRejected("dispatch_rejected")
        if self._goodnight_quiet_for_today(dt.datetime.now(TZ)):
            if plans:
                raise DispatchRejected("goodnight_quiet")
            if isinstance(pending_legacy, dict) and isinstance(
                pending_legacy.get('random_activity_attempt_id'), str
            ):
                await self._finish_random_activity(
                    pending_legacy['random_activity_attempt_id'], 'suppressed',
                    dt.datetime.now(TZ), reason='goodnight_quiet',
                )
            self._pending_legacy_care = None
            return {
                "outcome": "skipped",
                "reason": "本人已直接说晚安；本地日期剩余时间停止主动关怀",
            }
        async with self._care_dispatch_lock:
            if any(event_id in self._care_consumed_event_ids or event_id in self._care_inflight_event_ids for event_id in event_ids):
                raise DispatchRejected("event_consumed")
            if pending_legacy is not None and pending_legacy.get('_dispatching') is True:
                raise DispatchRejected("legacy_event_inflight")
            if pending_legacy is not None:
                pending_legacy['_dispatching'] = True
            self._care_inflight_event_ids.update(event_ids)
        token = None
        attempt_started = False
        handler_invoked = False
        retained_context = False
        deferred_refresh = False
        random_attempt_id = (
            pending_legacy.get('random_activity_attempt_id')
            if isinstance(pending_legacy, dict) else None
        )
        random_pre_dispatch_outcome = 'failed_not_sent'
        coordination_token = None
        try:
            if self._care_repository is None:
                raise DispatchRejected("source_context_unavailable")
            config, _revision = self._care_repository.load_config()
            rules = []
            for plan in plans:
                rule_id = plan["rule_id"]
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
                rules.append(rule)
            state = self._care_repository.load_scheduler_state()
            dispatch_windows = []
            for plan, rule in zip(plans, rules):
                event_id = plan["event_id"]
                job = state["jobs"].get(event_id)
                if (
                    not isinstance(job, dict)
                    or job.get("rule_id") != plan["rule_id"]
                    or job.get("state") != "dispatching"
                    or job.get("expected_at") != plan.get("target_at")
                    or plan.get("source_ref") != "oppo_health_local_page"
                    or plan.get("tone") != rule.get("tone")
                    or plan.get("target_date") != job.get("target_date")
                    or plan.get("timezone") != job.get("timezone")
                ):
                    raise DispatchRejected("event_consumed")
                dispatch_windows.append((rule, job))
            skip_reason = self._submission_window_skip_reason(
                dispatch_windows, pending_legacy, dt.datetime.now(TZ),
            )
            if skip_reason is not None:
                random_pre_dispatch_outcome = 'suppressed'
                return {"outcome": "skipped", "reason": skip_reason}
            parts = self.umo.rsplit(':', 2)
            if (
                len(parts) != 3 or parts[1] != 'FriendMessage' or not parts[2].isdigit()
                or not str(self.config.get('bot_qq_id', '')).isdigit()
                or not await SessionPluginManager.is_plugin_enabled_for_session(self.umo, NAME)
            ):
                raise DispatchRejected("private_session_unavailable")
            await self._care_privacy_preflight()
            platform = self.context.get_platform_inst(parts[0])
            bot = getattr(platform, 'bot', None)
            handler = getattr(bot, '_handle_event', None) or getattr(bot, 'handle_event', None)
            if not callable(handler):
                raise DispatchRejected("handler_unavailable")
            if pending_legacy is not None:
                token = pending_legacy["token"]
                prompt = pending_legacy.get("prompt")
                if not isinstance(prompt, str) or not prompt:
                    prompt = f"[OPPO每日关怀 token={token}] 请依据本次新读取记录，用温和、不诊断的语气简短关心。"
                from aiocqhttp import Event
                event = Event.from_payload({
                    'post_type': 'message', 'message_type': 'private', 'sub_type': 'friend',
                    'message_id': time.time_ns() % 2147483647,
                    '_oppo_synthetic_care': True,
                    'user_id': int(parts[2]), 'self_id': int(self.config['bot_qq_id']),
                    'time': int(time.time()),
                    'message': [{'type': 'text', 'data': {'text': prompt}}],
                    'raw_message': prompt, 'font': 0,
                    'sender': {'user_id': int(parts[2]), 'nickname': '健康关怀'},
                })
            else:
                from aiocqhttp import Event
                token = uuid.uuid4().hex
                prompt = f"[OPPO每日关怀 token={token}] 请依据已核验的本人记录，用温和、不诊断的语气简短关心。"
                event = Event.from_payload({
                    'post_type': 'message',
                    'message_type': 'private',
                    'sub_type': 'friend',
                    'message_id': time.time_ns() % 2147483647,
                    '_oppo_synthetic_care': True,
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
            # 2026-10-16（主人要求）：让硬排程关怀也参与共享防撞车 ——
            # 不再“硬排程无条件优先”；与其他主动消息（wakeup / VL）互让。
            # 注意：调度器把“适配器正常返回”统一记为 handed_off（无重试），
            # 因此这里的让位 = 本次不发（理由写入结果与日志），不伪装成已发。
            coordination_key = (
                random_attempt_id
                if isinstance(random_attempt_id, str)
                else f"oppo-scheduled-{uuid.uuid4().hex}"
            )
            coordination = await self.proactive_coordinator.reserve(
                "oppo", coordination_key
            )
            if coordination.get("allowed") is not True:
                reason = coordination.get("reason") or "blocked"
                if reason in {"cooldown", "in_flight"}:
                    # 2026-10-16（主人要求）：不因为“别人刚发/正在发”就丢弃关怀。
                    # 短暂等（上限 3 分钟）到冷却结束，在关怀自己的有效时段内发出去；
                    # 若这期间正好有可合并的主动回合，就把关怀意图交给那一轮一起说。
                    retry_at = float(coordination.get("retry_at") or (time.time() + 60))
                    wait_seconds = max(
                        1.0,
                        min(CARE_COORDINATION_MAX_WAIT_SECONDS, retry_at - time.time()),
                    )
                    logger.info(
                        "[oppo_health] Care waits out another proactive delivery | "
                        "reason=%s | wait_seconds=%.1f",
                        reason,
                        wait_seconds,
                    )
                    merge_rule = plans[0].get("rule_id") if plans else None
                    merge_topic = CARE_MERGE_TOPICS.get(str(merge_rule)) if merge_rule else None
                    if merge_topic:
                        self._care_merge_intent = {
                            "rule": str(merge_rule),
                            "topic": merge_topic,
                            "deadline": time.monotonic() + wait_seconds,
                            "consumed": False,
                        }
                    await asyncio.sleep(wait_seconds)
                    intent = self._care_merge_intent
                    if isinstance(intent, dict) and intent.get("consumed") is True:
                        self._care_merge_intent = None
                        logger.info(
                            "[oppo_health] Care merged into a proactive turn | rule=%s",
                            intent.get("rule"),
                        )
                        return {"outcome": "skipped", "reason": "merged_into_proactive_turn"}
                    self._care_merge_intent = None
                    coordination = await self.proactive_coordinator.reserve(
                        "oppo", coordination_key
                    )
                    reason = coordination.get("reason") or "blocked"
            if coordination.get("allowed") is not True:
                if (
                    isinstance(random_attempt_id, str)
                    and not plans
                    and reason in {"cooldown", "in_flight"}
                    and pending_legacy is not None
                ):
                    retry_at = float(coordination.get("retry_at") or time.time() + 120)
                    pending_legacy["_coordination_retry_after"] = max(
                        time.monotonic(),
                        time.monotonic() + max(1.0, retry_at - time.time()),
                    )
                    pending_legacy["_refresh_retry_after"] = pending_legacy["_coordination_retry_after"]
                    deferred_refresh = True
                    return {"outcome": "deferred", "reason": f"proactive_coordination_{reason}"}
                if not isinstance(random_attempt_id, str):
                    logger.info(
                        "[oppo_health] Scheduled care yielded to another proactive delivery | reason=%s",
                        reason,
                    )
                random_pre_dispatch_outcome = "unknown" if reason == "unknown_prior" else "suppressed"
                return {"outcome": "skipped", "reason": f"proactive_coordination_{reason}"}
            coordination_token = coordination.get("token")
            if not isinstance(coordination_token, str) or not coordination_token:
                random_pre_dispatch_outcome = "unknown"
                return {"outcome": "skipped", "reason": "proactive_coordination_invalid_reservation"}
            event.set_extra(COORDINATION_TOKEN_EXTRA, coordination_token)
            skip_reason = self._submission_window_skip_reason(
                dispatch_windows, pending_legacy, dt.datetime.now(TZ),
            )
            if skip_reason is not None:
                return {"outcome": "skipped", "reason": skip_reason}
            await self._expire_pending_care_contexts()
            attempt_id = (
                pending_legacy.get('attempt_id', token)
                if pending_legacy is not None and pending_legacy.get('kind') == 'sleep'
                else None
            )
            if attempt_id is not None and self.umo != SLEEP_CARE_TARGET_UMO:
                raise DispatchRejected('private_session_unavailable')
            legacy_recipe = None
            if pending_legacy is not None:
                legacy_recipe = {
                    "kind": "low_steps" if pending_legacy.get("reason_id") == "low_steps" else "random_activity",
                    "target_at": pending_legacy.get("target_at"),
                    "low_steps_marker": pending_legacy.get("low_steps_marker"),
                    "random_activity_attempt_id": pending_legacy.get("random_activity_attempt_id"),
                }
            recipe = {
                "kind": "scheduled_care",
                "rules": [
                    {"rule_id": plan["rule_id"], "event_id": plan["event_id"]}
                    for plan in plans
                ],
                "legacy": legacy_recipe,
            }
            if not self._pending_care_contexts.put(
                token,
                recipe,
                attempt_id,
                time.monotonic(),
                period_inquiry_event_id=period_inquiry_ids[0] if period_inquiry_ids else None,
                low_steps_marker=pending_legacy.get('low_steps_marker')
                if pending_legacy is not None else None,
                random_activity_attempt_id=(pending_legacy.get('random_activity_attempt_id')
                                            if pending_legacy is not None else None),
                random_activity_target_at=(pending_legacy.get('target_at')
                                           if pending_legacy is not None and pending_legacy.get('random_activity_attempt_id') else None),
            ):
                raise DispatchRejected("care_context_capacity")
            if attempt_id is not None:
                if attempt_id != token or not self._sleep_delivery_ready:
                    raise DispatchRejected("sleep_delivery_unavailable")
                attempt_now = dt.datetime.now(TZ)
                async with self._sleep_delivery.lock:
                    if self._pending_care_contexts.get(token, time.monotonic()) is None:
                        raise DispatchRejected("care_context_expired")
                    ledger = self._sleep_delivery.load(attempt_now)
                    started, reason = begin_attempt(
                        ledger,
                        attempt_now,
                        attempt_id,
                        POLICY_PRIVATE_MESSAGE,
                        activated_at=self._activated_at,
                    )
                    if not started:
                        raise DispatchRejected(f"sleep_delivery_{reason}")
                    self._sleep_delivery.save(ledger)
                    attempt_started = True
            retained_context = True
            handler_invoked = True
            handler_result = await handler(event)
            if event.get_extra('_oppo_health_care_transport_outcome') == 'suppressed':
                return {"outcome": "skipped", "reason": "freshness_expired_before_send"}
            if event.get_extra('_oppo_health_refresh_deferred') is True:
                deferred_refresh = True
                retained_context = False
                self._pending_care_contexts.pop(token)
                if isinstance(random_attempt_id, str):
                    try:
                        async with self._random_activity_lock:
                            ledger, _exists = self._load_random_activity_ledger(
                                dt.datetime.now(TZ)
                            )
                            if defer_random_refresh(ledger, random_attempt_id):
                                write_private_json(ledger, self._random_activity_path)
                    except (OSError, ValueError, TypeError):
                        self._random_activity_ready = False
                        logger.warning('[oppo_health] Deferred random refresh could not be persisted; retry remains locked.')
                if pending_legacy is not None:
                    pending_legacy['_refresh_retry_after'] = time.monotonic() + 30
                return {"outcome": "deferred"}
            self._care_consumed_event_ids.update(event_ids)
            return handler_result
        finally:
            if token is not None and not retained_context:
                self._pending_care_contexts.pop(token)
            for event_id in event_ids:
                self._care_contexts.pop(event_id, None)
                self._care_context_receipts.pop(event_id, None)
                self._care_measurement_receipts.pop(event_id, None)
            async with self._care_dispatch_lock:
                for event_id in event_ids:
                    self._care_inflight_event_ids.discard(event_id)
                if pending_legacy is not None:
                    pending_legacy.pop('_dispatching', None)
                    if self._pending_legacy_care is pending_legacy and not deferred_refresh:
                        self._pending_legacy_care = None
            if attempt_started and not handler_invoked:
                try:
                    failed_at = dt.datetime.now(TZ)
                    async with self._sleep_delivery.lock:
                        ledger = self._sleep_delivery.load(failed_at)
                        finish_attempt(ledger, token, 'failed_not_sent', failed_at)
                        self._sleep_delivery.save(ledger)
                except (OSError, ValueError, TypeError):
                    self._sleep_delivery_ready = False
                    logger.warning(
                        '[oppo_health] Unqueued sleep-care attempt could not be reconciled; restart recovery will keep it locked.'
                    )
            if coordination_token is not None and not handler_invoked:
                await self.proactive_coordinator.settle(coordination_token, "failed_not_sent")
            if isinstance(random_attempt_id, str) and not handler_invoked:
                await self._finish_random_activity(
                    random_attempt_id, random_pre_dispatch_outcome,
                    dt.datetime.now(TZ), reason='ingress_not_started',
                )

    async def dispatch(self, plan):
        """Scheduler adapter that forwards only to the plugin-owned care ingress."""
        return await self.submit_scheduled_care(plan)

    async def _flush_pending_legacy_care(self):
        """Submit sleep/activity context after same-tick rule jobs can be merged."""
        if self._pending_legacy_care is None:
            return
        retry_after = max(
            self._pending_legacy_care.get('_refresh_retry_after', 0),
            self._pending_legacy_care.get('_coordination_retry_after', 0),
        )
        if time.monotonic() < retry_after:
            return
        try:
            outcome = await self.submit_scheduled_care_batch([])
        except DispatchRejected as exc:
            logger.info('[oppo_health] 原有关怀未提交：安全入口状态为 %s。', exc.code)
        else:
            if isinstance(outcome, dict) and outcome.get('outcome') == 'skipped':
                logger.info('[oppo_health] 原有关怀未提交：%s。', outcome.get('reason', '当前规则不满足'))

    async def _read_period_calendar_after_confirmation(self):
        """Perform one local, read-only Calendar capture with auth-file access off."""
        return await self._capture_local_fields(['cycle_calendar'], timeout=120)

    def _period_now(self):
        """Return the local clock; injectable only for deterministic isolated tests."""
        clock = getattr(self, '_period_clock', None)
        return clock() if callable(clock) else dt.datetime.now(TZ)

    async def _reserve_period_inquiry(self, fact, now):
        """Reload under the period lock so a stale poll cannot overwrite confirmation."""
        async with self._period_runtime_lock:
            latest_state = self._care_repository.load_period_state()
            latest_state, reserved = reserve_inquiry(latest_state, fact, now)
            if reserved:
                self._care_repository.save_period_state(latest_state)
            return latest_state, reserved

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=998)
    async def record_period_end_confirmation(self, event: AstrMessageEvent):
        """Claim one exact plain private reply and queue one local Calendar sync."""
        target = SLEEP_CARE_TARGET_UMO
        if (self.umo != target or event.unified_msg_origin != target
                or not await self._allowed(event) or self._care_repository is None):
            return
        message_obj = getattr(event, 'message_obj', None)
        components = getattr(message_obj, 'message', None)
        if not components or not all(isinstance(component, Plain) for component in components):
            return
        parts = target.rsplit(':', 2)
        try:
            sender_id = str(event.get_sender_id())
        except Exception:
            sender_id = str(getattr(getattr(message_obj, 'sender', None), 'user_id', ''))
        if len(parts) != 3 or sender_id != parts[2]:
            return
        raw_timestamp = getattr(message_obj, 'timestamp', None)
        if isinstance(raw_timestamp, bool) or not isinstance(raw_timestamp, (int, float)) or raw_timestamp <= 0:
            return
        try:
            confirmed_at = dt.datetime.fromtimestamp(raw_timestamp, dt.timezone.utc).astimezone(TZ)
        except (OverflowError, OSError, TypeError, ValueError):
            return
        try:
            config, _revision = self._care_repository.load_config()
            if config['rules']['period_late_inquiry'].get('enabled') is not True:
                return
            async with self._period_runtime_lock:
                state = self._care_repository.load_period_state()
                active = state.get('active_cycle')
                if not isinstance(active, dict):
                    return
                inquiry = next((row for row in reversed(state['inquiries'])
                                if row.get('cycle_id') == active['cycle_id']
                                and row.get('date') == confirmed_at.date().isoformat()
                                and row.get('delivery_status') == 'sent'), None)
                if inquiry is None:
                    return
                token = build_end_confirmation_request(
                    event.message_str or '',
                    event_umo=event.unified_msg_origin,
                    target_umo=target,
                    plain_private_message=True,
                    inquiry_open=True,
                    period_day=inquiry['period_day'],
                    period_start_date=inquiry['start_date'],
                    confirmed_at=confirmed_at.isoformat(),
                )
                if token is None:
                    return
                state, claim = claim_confirmation(state, inquiry['event_id'], confirmed_at)
                if claim is None:
                    return
                self._care_repository.save_period_state(state)
        except (CareStorageError, ValueError, TypeError, KeyError):
            logger.warning('[oppo_health] 经期确认状态无法安全保存；未启动同步。')
            return
        task = asyncio.create_task(self._run_period_confirmation_sync(claim))
        self._period_sync_tasks[claim['event_id']] = task
        task.add_done_callback(lambda _task, key=claim['event_id']: self._period_sync_tasks.pop(key, None))

    async def _run_period_confirmation_sync(self, claim):
        """Run the claimed read-only sync once, then consume the period-once rule."""
        if self._care_repository is None:
            return
        event_id = claim.get('event_id') if isinstance(claim, dict) else None
        if not isinstance(event_id, str):
            return
        now = self._period_now()
        try:
            async with self._period_runtime_lock:
                period_state = self._care_repository.load_period_state()
                confirmation = period_state.get('confirmation')
                if (not isinstance(confirmation, dict) or confirmation.get('event_id') != event_id
                        or confirmation.get('status') != 'sync_claimed'):
                    return
                confirmation['status'] = 'sync_running'
                confirmation['updated_at'] = now.isoformat()
                self._care_repository.save_period_state(period_state)
        except (CareStorageError, ValueError, TypeError):
            return

        captured = None
        end_record = None
        post_fact = None
        contexts = {}
        try:
            if (self.umo != SLEEP_CARE_TARGET_UMO
                    or not await SessionPluginManager.is_plugin_enabled_for_session(self.umo, NAME)):
                raise RuntimeError('private_session_unavailable')
            captured = await self._read_period_calendar_after_confirmation()
            config, _revision = self._care_repository.load_config()
            confirmed_at = claim['confirmed_at']
            token = {
                'confirmed': True,
                'period_start_date': claim['start_date'],
                'confirmed_at': confirmed_at,
                'confirmation_source': 'target_private_plain_reply',
            }
            facts, contexts, _statuses = facts_from_capture(
                captured, config, self._period_now(),
                requested_fields=['cycle_calendar'],
                period_end_confirmation=token,
                period_confirmed_cycle_ids={claim['cycle_id']},
            )
            end_rows = facts.get('confirmed_period_end', [])
            post_rows = facts.get('post_period_care', [])
            end_record = end_rows[0] if end_rows else None
            post_fact = post_rows[0] if post_rows else None
            if not isinstance(end_record, dict) or not isinstance(post_fact, dict):
                raise RuntimeError('period_end_evidence_unavailable')
        except asyncio.CancelledError:
            # The persisted running state is recovered as unknown on next startup.
            raise
        except Exception:
            captured = None

        now = self._period_now()
        try:
            async with self._period_runtime_lock:
                period_state = self._care_repository.load_period_state()
                period_state = settle_confirmation(
                    period_state,
                    event_id,
                    now,
                    end_date=end_record.get('event_date') if isinstance(end_record, dict) else None,
                    postcare_event_id=post_fact.get('event_id') if isinstance(post_fact, dict) else None,
                )
                self._care_repository.save_period_state(period_state)
        except (CareStorageError, ValueError, TypeError):
            logger.warning('[oppo_health] 经期确认同步结果无法保存；本地规则保持锁定。')
            return

        if not isinstance(post_fact, dict):
            return
        try:
            config, _revision = self._care_repository.load_config()
            if config['rules']['post_period_care'].get('enabled') is not True:
                return
            stable_id = post_fact['event_id']
            self._care_contexts[event_key('post_period_care', stable_id)] = contexts.get(
                event_key('post_period_care', stable_id),
                '本人已确认经期结束，且确认后的只读 Calendar 同步核实了末日。请温和、不诊断地简短关心。',
            )
            self._care_context_receipts[event_key('post_period_care', stable_id)] = (
                dt.datetime.now(TZ).isoformat()
            )
            scheduler_state = self._care_repository.load_scheduler_state()
            scheduler_state, _results = await dispatch_due_async(
                config,
                {'post_period_care': [post_fact]},
                scheduler_state,
                self._period_now(),
                self,
                persist=self._care_repository.save_scheduler_state,
                not_before=self._activated_at,
            )
            self._care_repository.save_scheduler_state(scheduler_state)
        except (CareStorageError, DispatchRejected, ValueError, TypeError):
            # Scheduler records failed/uncertain attempts before delivery; no resend.
            logger.warning('[oppo_health] 经期后单次关怀未完成；不会自动重试。')

    def _verified_sleep_chat_inbound(self, event):
        """Read adapter-issued metadata for the configured owner's real input.

        Args:
            event: Inbound event to check at wake, reply preparation and injection.

        Returns:
            The event hash and receipt time, or None for unverified input.
        """
        if self.umo != SLEEP_CARE_TARGET_UMO or event.unified_msg_origin != self.umo:
            return None
        try:
            platform = self.context.get_platform_inst(self.umo.rsplit(":", 2)[0])
            verify = getattr(platform, "verified_private_inbound", None)
            metadata = verify(event.message_obj) if callable(verify) else None
            if not isinstance(metadata, dict):
                return None
            owner = self.umo.rsplit(":", 1)[1]
            if str(event.get_sender_id()) != owner or metadata.get("user_id") != owner:
                return None
            now = dt.datetime.now(TZ)
            received_at = dt.datetime.fromtimestamp(metadata["received_at"], TZ)
            source_at = dt.datetime.fromtimestamp(metadata["source_at"], TZ)
            if (
                received_at.date() != now.date()
                or source_at.date() != now.date()
                or received_at > now
                or source_at > received_at
                or now - received_at > dt.timedelta(minutes=2)
                or received_at - source_at > dt.timedelta(minutes=2)
            ):
                return None
            correlation = "\x00".join(
                (self.umo, metadata["self_id"], metadata["message_id"])
            )
            return hashlib.sha256(correlation.encode()).hexdigest(), received_at
        except (
            AttributeError,
            KeyError,
            OverflowError,
            OSError,
            TypeError,
            ValueError,
        ):
            return None

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=999)
    async def record_sleep_wake_private_activity(self, event: AstrMessageEvent):
        """Start sleep sync after the first verified private message or command.

        Args:
            event: Actual owner input; synthetic care events carry no provenance.
        """
        if (
            not self.config.get("daily_care", False)
            or not self._sleep_delivery_ready
            or not await self._allowed(event)
        ):
            return
        inbound = self._verified_sleep_chat_inbound(event)
        if inbound is None:
            return
        event_hash, received_at = inbound
        try:
            async with self._sleep_delivery.lock:
                ledger = self._sleep_delivery.load(received_at)
                cached_sleep = sleep_record(self._cache, dt.datetime.now(TZ))
                start_sync = record_chat_wake(
                    ledger,
                    event_hash,
                    received_at,
                    device_wake_at=cached_sleep["wake_at"] if cached_sleep else None,
                )
                local_date = received_at.astimezone(TZ).date().isoformat()
                day = ledger.get("days", {}).get(local_date, {})
                flow = day.get("chat_flow", {})
                resume_sync = (
                    not start_sync
                    and self._wake_sleep_sync_task is None
                    and self._wake_sleep_sync_anchor is None
                    and flow.get("status") == "waiting"
                    and flow.get("reason")
                    in RETRYABLE_PRECHECK_REASONS | {"awaiting_next_chat"}
                    and day.get("selected") is None
                    and day.get("attempt_count") == 0
                    and day.get("delivery_state") == "ready"
                    and flow.get("wake_event_hash") is not None
                )
                if resume_sync:
                    start_sync = True
                    event_hash = flow["wake_event_hash"]
                if start_sync:
                    self._sleep_delivery.save(ledger)
            if start_sync:
                if self._wake_sleep_sync_task is not None:
                    self._wake_sleep_sync_task.cancel()
                    await asyncio.gather(
                        self._wake_sleep_sync_task, return_exceptions=True
                    )
                self._wake_sleep_sync_anchor = event_hash
                self._wake_sleep_sync_task = asyncio.create_task(
                    self._snapshot(
                        force=True, allow_auth_refresh=False, allow_auth_file=False
                    )
                )
                # Consume an exception even when there is no subsequent chat.
                self._wake_sleep_sync_task.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception()
                )
        except (OSError, ValueError, TypeError):
            self._sleep_delivery_ready = False
            logger.warning(
                "[oppo_health] Verified wake metadata could not be saved; sleep care stays paused."
            )

    async def _skip_inline_sleep_attempt(self, token):
        """Close an optional reply known to have stopped before any transport.

        Args:
            token: The already persisted inline attempt correlation ID.
        """
        self._pending_care_contexts.pop(token)
        now = dt.datetime.now(TZ)
        try:
            async with self._sleep_delivery.lock:
                ledger = self._sleep_delivery.load(now)
                finish_attempt(ledger, token, "failed_not_sent", now)
                self._sleep_delivery.save(ledger)
        except (OSError, ValueError, TypeError):
            self._sleep_delivery_ready = False
            logger.warning(
                "[oppo_health] Unsent inline care could not be reconciled; the reservation stays locked."
            )

    async def _release_sleep_chat_claim(self, event_hash, reason_code, now):
        """Release only a matching, unsent pre-draw claim with a fixed reason."""
        if not self._sleep_delivery_ready:
            return False
        try:
            async with self._sleep_delivery.lock:
                ledger = self._sleep_delivery.load(now)
                released, _reason = release_chat_reply_claim(
                    ledger, event_hash, reason_code, now
                )
                if released:
                    self._sleep_delivery.save(ledger)
                return released
        except (OSError, ValueError, TypeError):
            self._sleep_delivery_ready = False
            logger.warning(
                "[oppo_health] Sleep-care precheck state could not be saved; sleep care stays paused."
            )
            return False

    @_on_waiting_llm_request()
    async def prepare_sleep_chat_reply(self, event: AstrMessageEvent):
        """Check the one adjacent ordinary chat without waiting for collection.

        Args:
            event: The ordinary LLM turn; fixed command replies skip this hook.
        """
        if not await self._allowed(event):
            return
        inbound = self._verified_sleep_chat_inbound(event)
        components = getattr(event.message_obj, "message", None)
        text = event.message_str or ""
        plain_text = "".join(
            part.text for part in (components or ()) if isinstance(part, Plain)
        )
        image_text_input = (
            bool(plain_text.strip())
            and any(isinstance(part, Image) for part in (components or ()))
            and all(
                isinstance(part, (Plain, Image)) for part in (components or ())
            )
        )
        plain_text_input = (
            bool(plain_text.strip())
            and components
            and all(isinstance(part, Plain) for part in components)
            and plain_text == text
        )
        if (
            inbound is not None
            and event.get_extra("_wakeup_synthetic_contact") is not True
            and (plain_text_input or image_text_input)
        ):
            # Keep only the user's actual text component; media care remains ineligible.
            event.set_extra(
                "_oppo_verified_plain_input",
                {"turn": inbound[0], "text": plain_text},
            )
        if not self.config.get("daily_care", False) or not self._sleep_delivery_ready:
            return
        if inbound is None or event.get_extra("_oppo_sleep_chat_prepared"):
            return
        event.set_extra("_oppo_sleep_chat_prepared", True)
        event_hash, received_at = inbound
        now = dt.datetime.now(TZ)
        try:
            async with self._sleep_delivery.lock:
                ledger = self._sleep_delivery.load(now)
                claimed, _reason = claim_chat_reply(
                    ledger, event_hash, received_at, now
                )
                self._sleep_delivery.save(ledger)
                flow = (
                    ledger["days"].get(now.date().isoformat(), {}).get("chat_flow", {})
                )
                anchor = flow.get("wake_event_hash")
            if not claimed:
                return
            components = getattr(event.message_obj, "message", None)
            if not components or not all(isinstance(part, Plain) for part in components):
                await self._release_sleep_chat_claim(event_hash, "non_plain_input", now)
                return
            text = event.message_str or ""
            if self._goodnight_quiet_for_today(now):
                await self._release_sleep_chat_claim(event_hash, "goodnight_quiet", now)
                return
            if explicit_goodnight_message(text, own_private_plain_message=True):
                await self._release_sleep_chat_claim(event_hash, "explicit_goodnight", now)
                return
            if explicit_weight_mode_command(text, own_private_plain_message=True):
                await self._release_sleep_chat_claim(event_hash, "weight_mode", now)
                return
            if (
                health_query_mentions_other_person(text)
                and health_query_mentions_known_metric(text)
            ):
                await self._release_sleep_chat_claim(event_hash, "other_person_health", now)
                return
            task = self._wake_sleep_sync_task
            if (
                anchor != self._wake_sleep_sync_anchor
                or task is None
                or not task.done()
                or task.cancelled()
            ):
                await self._release_sleep_chat_claim(event_hash, "sync_unavailable", now)
                return
            if task.exception() is not None:
                await self._release_sleep_chat_claim(event_hash, "sync_failed", now)
                return
            snapshot = task.result()
            candidate = sleep_record(snapshot, now)
            if candidate is None:
                await self._release_sleep_chat_claim(
                    event_hash, "sleep_candidate_missing", now
                )
                return
            metadata = {
                "date": candidate["date"],
                "version_id": candidate["record_id"],
                "device_wake_at": candidate["wake_at"].isoformat(),
                "source_modified_at": candidate["source_modified_at"].isoformat(),
                "fetched_at": candidate["fetched_at"].isoformat(),
            }
            token = uuid.uuid4().hex
            async with self._sleep_delivery.lock:
                ledger = self._sleep_delivery.load(now)
                reserved, _reason = reserve_chat_attempt(
                    ledger, event_hash, metadata, now, token
                )
                self._sleep_delivery.save(ledger)
            if not reserved:
                if _reason == "snapshot_invalid":
                    await self._release_sleep_chat_claim(
                        event_hash, "candidate_invalid", now
                    )
                return
            # A route failure is explicitly not sent and cannot create late care.
            try:
                status = await asyncio.wait_for(
                    self._care_privacy_preflight(), timeout=0.5
                )
                route_ok = bool(status.get("health_context_to_model_enabled") is True)
            except (DispatchRejected, TimeoutError, OSError, ValueError, TypeError):
                route_ok = False
            if not route_ok:
                await self._skip_inline_sleep_attempt(token)
                return
            sleep_only = {
                "date": snapshot["date"],
                "fetched_at": snapshot["fetched_at"],
                "metrics": {"sleep": snapshot["metrics"]["sleep"]},
            }
            context = (
                "[本人刚确认醒来后的睡眠记录，仅用于这一条正常聊天回复]\n"
                + describe(sleep_only)
                + "\n先自然回应本人这条聊天，在同一次回复里顺势轻轻关心休息，沿用当前人格语气。"
                "设备睡眠结束不等同实际醒来；只使用这次有效记录，不编造或诊断，不逐项报表、"
                "不报精确时间，不另发一条关怀，不说之后会再提醒。"
            )
            if not self._pending_care_contexts.put(
                token,
                {
                    "kind": "sleep_chat", "event_hash": event_hash,
                    "record_id": candidate["record_id"],
                },
                token,
                time.monotonic(),
            ):
                await self._skip_inline_sleep_attempt(token)
                return
            event.set_extra("_oppo_sleep_chat_token", token)
            event.set_extra("_oppo_local_health_request", True)
        except (OSError, ValueError, TypeError):
            await self._release_sleep_chat_claim(event_hash, "evaluation_error", now)
            self._sleep_delivery_ready = False
            logger.warning(
                "[oppo_health] Inline sleep metadata could not be saved; sleep care stays paused."
            )

    @filter.after_message_sent()
    async def settle_sleep_care_delivery(self, event: AstrMessageEvent):
        """Settle only this configured private care event from its send receipt."""
        if not await self._allowed(event):
            return
        token = event.get_extra(CARE_TOKEN_EXTRA)
        if not isinstance(token, str) or not token:
            return
        attempt_id = event.get_extra(ATTEMPT_EXTRA)
        if isinstance(attempt_id, str) and attempt_id and self._sleep_delivery_ready:
            now = dt.datetime.now(TZ)
            try:
                async with self._sleep_delivery.lock:
                    ledger = self._sleep_delivery.load(now)
                    settle_after_send(ledger, event, now)
                    self._sleep_delivery.save(ledger)
            except (OSError, ValueError, TypeError):
                self._sleep_delivery_ready = False
                logger.warning(
                    '[oppo_health] Outbound sleep-care receipt could not be persisted; the attempt stays locked.'
                )
                return
        random_attempt_id = event.get_extra(RANDOM_ACTIVITY_ATTEMPT_ID_EXTRA)
        if isinstance(random_attempt_id, str) and self._random_activity_ready:
            now = dt.datetime.now(TZ)
            try:
                async with self._random_activity_lock:
                    ledger, _exists = self._load_random_activity_ledger(now)
                    settled, _outcome = settle_random_activity_after_send(
                        ledger, event, now, finish_random_activity_attempt
                    )
                    if settled:
                        write_private_json(ledger, self._random_activity_path)
            except (OSError, ValueError, TypeError):
                self._random_activity_ready = False
                logger.warning('[oppo_health] Random-activity outbound receipt could not be persisted; retry stays locked.')
                return
        coordination_token = event.get_extra(COORDINATION_TOKEN_EXTRA)
        if isinstance(coordination_token, str) and coordination_token:
            outcome = event.get_extra(RANDOM_ACTIVITY_OUTCOME_EXTRA) or "unknown"
            if outcome not in {"sent", "failed_not_sent", "unknown"}:
                outcome = "unknown"
            await self.proactive_coordinator.settle(coordination_token, outcome)
        inquiry_event_id = event.get_extra(PERIOD_INQUIRY_EXTRA)
        if isinstance(inquiry_event_id, str) and self._care_repository is not None:
            outcome = event.get_extra(PERIOD_OUTCOME_EXTRA) or 'unknown'
            if outcome not in {'sent', 'failed_not_sent', 'unknown'}:
                outcome = 'unknown'
            try:
                async with self._period_runtime_lock:
                    state = self._care_repository.load_period_state()
                    state = mark_inquiry_delivery(
                        state, inquiry_event_id, outcome, self._period_now()
                    )
                    self._care_repository.save_period_state(state)
            except (CareStorageError, ValueError, TypeError):
                logger.warning('[oppo_health] 经期问询送达回执无法安全保存；不接受普通肯定回复。')
                return
        self._pending_care_contexts.pop(token)

    async def _allowed(self, event):
        if event.get_extra("_wakeup_synthetic_contact") is True:
            return False
        if not self.umo or ':FriendMessage:' not in self.umo or event.unified_msg_origin != self.umo:
            return False
        return await SessionPluginManager.is_plugin_enabled_for_session(self.umo, NAME)

    def _apply_weight_mode_command(self, command):
        """Persist a direct opt-in/out without reading or sending health data."""
        if self._care_repository is None or command not in {"activate", "deactivate"}:
            return False
        try:
            config, revision = self._care_repository.load_config()
            runtime = self._care_repository.load_wellness_state()
            enabled = command == "activate"
            config["rules"]["weight_date_linked"]["enabled"] = enabled
            self._care_repository.save_config(config, revision)
            runtime["weight_mode_enabled"] = enabled
            runtime["weight_mode_enabled_at"] = (
                dt.datetime.now(dt.timezone.utc).isoformat() if enabled else None
            )
            self._care_repository.save_wellness_state(runtime)
            return True
        except (CareStorageError, ValueError, TypeError):
            logger.warning('[oppo_health] Weight-care mode change could not be saved; mode stays fail-closed.')
            return False
    @_on_waiting_llm_request()
    async def mark_local_health_request_intent(self, event: AstrMessageEvent):
        """Mark plain-text health intents before request hooks see history."""
        if not await self._allowed(event):
            return
        text = event.message_str or ''
        if (
            text.startswith('[OPPO每日关怀 token=')
            or local_fields_for_query(text)
            or explicit_owner_health_data_query(text)
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
    async def _singleflight(self, key, factory):
        """Share only an in-progress identical source read; never cache failures."""
        async with self._refresh_guard:
            task = self._refresh_tasks.get(key)
            if task is None or task.done():
                task = asyncio.create_task(factory())
                self._refresh_tasks[key] = task
        try:
            result = await asyncio.shield(task)
            return deepcopy(result)
        finally:
            if task.done():
                async with self._refresh_guard:
                    if self._refresh_tasks.get(key) is task:
                        self._refresh_tasks.pop(key, None)

    async def _snapshot(
        self, date=None, force=False, allow_auth_refresh=True, allow_auth_file=True
    ):
        today = dt.datetime.now(TZ).date().isoformat()
        date = date or today
        if not force and self._cache and self._cache.get('date') == date and time.monotonic() - self._cache_time < 600:
            return deepcopy(self._cache)
        key = ("cloud", date, allow_auth_refresh, allow_auth_file)
        return await self._singleflight(
            key,
            lambda: self._snapshot_uncached(
                date, force=True, allow_auth_refresh=allow_auth_refresh,
                allow_auth_file=allow_auth_file,
            ),
        )

    async def _snapshot_uncached(
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
            if date == today:
                # A failed current read invalidates the scheduling snapshot;
                # no subsequent path may mistake it for a current fallback.
                self._cache = None
                self._cache_time = 0
            return None
    async def _capture_local_fields(self, fields, *, timeout=240):
        ordered = tuple(sorted(set(fields)))
        key = ("local", ordered)

        async def capture():
            script = Path(__file__).parent / 'collector/local_capture.py'
            if not script.is_file():
                return None
            async with self._lock:
                result = await self._run(
                    script, '--json', '--fields', ','.join(ordered),
                    timeout=timeout, allow_auth_file=False,
                )
            if not capture_is_fresh(result, ordered, dt.datetime.now(TZ)):
                return None
            return result

        return await self._singleflight(key, capture)
    @filter.command('oppohealth')
    async def command_health(self, event: AstrMessageEvent):
        if not await self._allowed(event):
            return
        snapshot = await self._snapshot(force=True)
        snapshot = current_health_snapshot(snapshot, dt.datetime.now(TZ))
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

    async def _fresh_care_context_from_recipe(self, recipe, now):
        """Build prompt text only from a source read performed at consumption."""
        if not isinstance(recipe, dict) or recipe.get("kind") != "scheduled_care":
            return None
        rules = recipe.get("rules")
        legacy = recipe.get("legacy")
        if not isinstance(rules, list) or (legacy is not None and not isinstance(legacy, dict)):
            return None
        contexts = []
        source_receipts = {}
        measurement_receipts = {}

        if rules:
            if self._care_repository is None:
                return None
            try:
                config, _revision = self._care_repository.load_config()
                wellness_state = self._care_repository.load_wellness_state()
                period_state = self._care_repository.load_period_state()
            except CareStorageError:
                return None
            field_by_rule = {
                "predicted_period_lead": "cycle_calendar",
                "confirmed_period_start": "cycle_calendar",
                "confirmed_period_end": "cycle_calendar",
                "post_period_care": "cycle_calendar",
                "period_late_inquiry": "cycle_calendar",
                "weight_date_linked": "weight_history",
                "wellness_low_state": "wellness_home",
                "sunlight_evening": "sun_exposure",
            }
            fields = sorted({
                field_by_rule.get(item.get("rule_id"))
                for item in rules if isinstance(item, dict)
            } - {None})
            if not fields:
                return None

            async def capture_fresh():
                value = await self._capture_local_fields(fields, timeout=240)
                return value if capture_is_fresh(value, fields, dt.datetime.now(TZ)) else None

            capture = await retry_fresh_context(
                capture_fresh,
                lambda value: value,
                lambda: not self._goodnight_quiet_for_today(dt.datetime.now(TZ)),
            )
            if capture is None:
                return None
            source_receipts["local_capture"] = capture_observation_receipt(capture, fields)
            source_config = deepcopy(config)
            if not wellness_state.get("weight_mode_enabled"):
                source_config["rules"]["weight_date_linked"]["enabled"] = False
            try:
                facts, rule_contexts, _statuses = facts_from_capture(
                    capture,
                    source_config,
                    dt.datetime.now(TZ),
                    weight_mode_enabled_at=wellness_state.get("weight_mode_enabled_at")
                    if wellness_state.get("weight_mode_enabled") else None,
                    requested_fields=fields,
                    period_inquiry_sent_dates={
                        item["date"] for item in period_state.get("inquiries", [])
                        if item.get("status") == "sent"
                    },
                    period_confirmed_cycle_ids={
                        period_state["confirmation"]["cycle_id"]
                    } if isinstance(period_state.get("confirmation"), dict) else set(),
                    period_postcare_cycle_ids=set(),
                    period_end_confirmation=period_state.get("confirmation"),
                )
            except (TypeError, ValueError, KeyError):
                return None
            for descriptor in rules:
                event_id = descriptor["event_id"]
                rule_id = descriptor["rule_id"]
                context = rule_contexts.get(event_id)
                if context is None and rule_id == "wellness_low_state":
                    rule = source_config["rules"].get(rule_id)
                    if isinstance(rule, dict) and rule.get("enabled") is True:
                        trial_state = deepcopy(wellness_state)
                        trial_state, evaluation, sample = evaluate_wellness_capture(
                            capture, rule, trial_state, dt.datetime.now(TZ)
                        )
                        if sample is not None and trial_state.get("current_episode_id"):
                            candidate_id = hashlib.sha256(
                                f"{trial_state['current_episode_id']}\0{sample.sample_id}".encode()
                            ).hexdigest()
                            if event_key(rule_id, candidate_id) == event_id and evaluation.get("low_confirmed") is True:
                                context = (
                                    "本人连续的独立状态测量满足已设置的持续偏低条件。"
                                    "请以温柔、不评判的方式关心本人，帮助放松和舒缓情绪；不要提分值、不要把设备状态等同真实心情、不要诊断。"
                                )
                                measurement_receipts["wellness"] = {
                                    "measured_at": sample.measured_at.isoformat(),
                                    "max_age_minutes": rule["maximum_sample_age_minutes"],
                                    "data_date": sample.data_date.isoformat(),
                                    "timezone": rule["timezone"],
                                }
                if context is None and rule_id == "sunlight_evening":
                    page = capture.get("pages", {}).get("sun_exposure", {})
                    data = page.get("data", {}) if isinstance(page, dict) else {}
                    metrics = data.get("metrics", {}) if isinstance(data, dict) else {}
                    today = dt.datetime.now(TZ).date().isoformat()
                    duration = metrics.get("duration_min") if isinstance(metrics, dict) else None
                    if (event_id == event_key(rule_id, f"sunlight:{today}")
                            and data.get("date") == today and metrics.get("unit") == "min"
                            and type(duration) in (int, float) and 0 <= duration <= 5):
                        context = (
                            "本人 OPPO 健康记录明确显示今天日照记录在 0–5 分钟范围内。"
                            "请自然、温柔地问候；不责备、不诊断，也不把缺失记录说成零。"
                        )
                if not isinstance(context, str) or not context:
                    return None
                contexts.append(context)

        if legacy is not None:
            kind = legacy.get("kind")
            target_text = legacy.get("target_at")
            try:
                target = dt.datetime.fromisoformat(str(target_text).replace("Z", "+00:00"))
            except ValueError:
                return None

            async def fetch_cloud():
                value = current_health_snapshot(
                    await self._snapshot(force=True), dt.datetime.now(TZ)
                )
                if not isinstance(value, dict) or not value.get("metrics"):
                    return None
                if kind == "low_steps":
                    total, _reason = valid_steps(value, dt.datetime.now(TZ))
                    return value if total is not None and total < 500 else None
                return value

            snapshot = await retry_fresh_context(
                fetch_cloud,
                lambda value: value,
                lambda: (
                    dt.datetime.now(TZ) < dt.datetime.combine(
                        target.astimezone(TZ).date(), dt.time(21), tzinfo=TZ
                    )
                    and not self._goodnight_quiet_for_today(dt.datetime.now(TZ))
                    and not self._low_steps_mode_suppressed(target, dt.datetime.now(TZ))
                ),
            )
            if snapshot is None:
                return None
            source_receipts["cloud"] = snapshot["fetched_at"]
            if kind == "low_steps":
                total, _reason = valid_steps(snapshot, dt.datetime.now(TZ))
                if total is None or total >= 500:
                    return None
                measurement_receipts["steps"] = snapshot["metrics"]["steps"]["measured_at"]
                contexts.append(
                    "[OPPO低步数关怀，仅用于本次私聊回复] 当天有效步数记录偏少。"
                    "请沿用当前人格自然温柔地关心，轻轻问今天是不是没有出门、是否想休息。"
                    "不要断言一定没有出门，不责备、不调侃、不诊断，不编造原因，不逐项报表。"
                )
            else:
                filtered, step_data_available = current_random_activity_snapshot(
                    snapshot, dt.datetime.now(TZ)
                )
                if step_data_available:
                    measurement_receipts["steps"] = filtered["metrics"]["steps"]["measured_at"]
                contexts.append(
                    "[OPPO每日关怀，仅用于本次私聊回复]\n"
                    + describe(filtered, include_step_measurement_time=True)
                    + "\n请沿用当前人格温和问候，只提有记录的一项；不诊断、不夸大、不说实时监控。"
                    + ("\n步数样本不新鲜，不得提步数或从历史对话补值。" if not step_data_available else "")
                )
        context = "\n\n".join(contexts)
        if not context or any(value is None for value in source_receipts.values()):
            return None
        context += (
            "\n本次关怀依据消费时刚完成的成功读取。仅使用这次读取中仍有效的记录；"
            "缺项不得用旧缓存、旧 prompt 或历史对话补齐。"
        )
        context += CARE_DELIVERY_WORDING_HINT
        return context, source_receipts, measurement_receipts

    @filter.on_llm_request()
    async def inject_care_merge_intent(
        self, event: AstrMessageEvent, req
    ) -> None:
        """把“想关心她一件事”的意图交给正在发生的主动回合（2026-10-16）。

        只在以下条件全部满足时注入（任一不满足则安静退出，不影响该回合）：
        ① 存在未过期的合并意图；② 同一会话；③ 这是一个“可合并的主动回合”
        （wakeup 合成回合 / 提醒回合）；④ provider host 在批准名单内（fail-closed）。
        """
        intent = self._care_merge_intent
        if not isinstance(intent, dict) or intent.get("consumed") is True:
            return
        if time.monotonic() > float(intent.get("deadline") or 0.0):
            self._care_merge_intent = None
            return
        if str(event.unified_msg_origin) != self.umo:
            return
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        raw = raw if isinstance(raw, dict) else {}
        mergeable = event.get_extra("_wakeup_synthetic_contact") is True or isinstance(
            raw.get("_xavier_reminder_source"), dict
        )
        if not mergeable:
            return
        host = getattr(req, "resolved_provider_api_host", None)
        if not isinstance(host, str) or host not in self.approved_provider_hosts:
            return
        req.local_health_required_provider_host = host
        req.local_health_sensitive = True
        current = getattr(req, "system_prompt", "") or ""
        req.system_prompt = current + CARE_MERGE_HINT.format(topic=intent.get("topic", ""))
        intent["consumed"] = True
        logger.info(
            "[oppo_health] Care merge intent injected | rule=%s",
            intent.get("rule"),
        )

    @filter.on_llm_request()
    async def inject_health(self, event: AstrMessageEvent, req):
        if not await self._allowed(event):
            return
        text = event.message_str or ''
        inline_sleep_context = ""
        chat_token = event.get_extra("_oppo_sleep_chat_token")
        if isinstance(chat_token, str) and chat_token:
            pending = self._pending_care_contexts.get(chat_token, time.monotonic())
            host = getattr(req, "resolved_provider_api_host", None)
            if (
                not self.config.get("daily_care", False)
                or not self._sleep_delivery_ready
                or self._verified_sleep_chat_inbound(event) is None
                or not isinstance(pending, dict)
                or not isinstance(pending.get("recipe"), dict)
                or pending["recipe"].get("kind") != "sleep_chat"
                or self.health_ai_core_api is None
                or event.get_extra("action_type") == "live"
                or not remove_cloud_health_tool(getattr(req, "func_tool", None))
                or not local_request_allowed(
                    self.umo,
                    event.unified_msg_origin,
                    host,
                    self.approved_provider_hosts,
                    self.health_ai_core_api,
                )
            ):
                await self._skip_inline_sleep_attempt(chat_token)
                # This is an ordinary inbound message with an optional sleep
                # add-on. A failed add-on must not swallow the user's chat.
                inline_sleep_context = ""
                chat_token = None
            sleep_fetch_receipt = None
            try:
                route_ok = bool(chat_token) and await asyncio.wait_for(
                    self._request_route_preflight(host), timeout=0.5
                )
            except (TimeoutError, OSError, ValueError, TypeError):
                route_ok = False
            if chat_token and not route_ok:
                await self._skip_inline_sleep_attempt(chat_token)
                inline_sleep_context = ""
                chat_token = None
            if chat_token:
                async def fetch_sleep_context():
                    nonlocal sleep_fetch_receipt
                    snapshot = current_health_snapshot(
                        await self._snapshot(
                            force=True, allow_auth_refresh=False, allow_auth_file=False
                        ),
                        dt.datetime.now(TZ),
                    )
                    record = sleep_record(snapshot, dt.datetime.now(TZ))
                    if (record is None
                            or record.get("record_id") != pending["recipe"].get("record_id")):
                        return None
                    sleep_fetch_receipt = snapshot["fetched_at"]
                    sleep_only = {
                        "date": snapshot["date"],
                        "fetched_at": snapshot["fetched_at"],
                        "metrics": {"sleep": snapshot["metrics"]["sleep"]},
                    }
                    return (
                        "[本人刚确认醒来后的睡眠记录，仅用于这一条正常聊天回复]\n"
                        + describe(sleep_only)
                        + "\n先自然回应本人这条聊天，在同一次回复里顺势轻轻关心休息，沿用当前人格语气。"
                        "设备睡眠结束不等同实际醒来；只使用本次新读取的有效记录，不编造或诊断，不逐项报表、"
                        "不报精确时间，不另发一条关怀，不说之后会再提醒。"
                    )

                fresh_sleep_context = await retry_fresh_context(
                    fetch_sleep_context,
                    lambda value: value,
                    lambda: not self._goodnight_quiet_for_today(dt.datetime.now(TZ)),
                )
                if fresh_sleep_context is None:
                    await self._skip_inline_sleep_attempt(chat_token)
                    inline_sleep_context = ""
                    chat_token = None
            if chat_token:
                event.set_extra(CARE_TOKEN_EXTRA, chat_token)
                if not event.get_extra(ATTEMPT_EXTRA):
                    attach_attempt(event, chat_token)
                event.set_extra("_local_health_sensitive", True)
                event.set_extra("provider_request", None)
                req.local_health_required_provider_host = host
                req.local_health_sensitive = True
                inline_sleep_context = fresh_sleep_context
                event.set_extra(
                    "_oppo_health_live_source_receipts",
                    {"cloud": sleep_fetch_receipt},
                )
                if not local_fields_for_query(text) and not any(
                    word in text for word in HEALTH_WORDS
                ):
                    self.health_ai_core_api.stage_local_health_context(
                        req,
                        inline_sleep_context,
                        required_provider_host=host,
                        allowed_provider_hosts=self.approved_provider_hosts,
                        inline_sleep_chat=True,
                    )
                    return
        if text.startswith('[OPPO每日关怀 token='):
            token = text.split(']', 1)[0].removeprefix('[OPPO每日关怀 token=')
            pending = self._pending_care_contexts.get(token, time.monotonic())
            recipe = pending.get('recipe') if isinstance(pending, dict) else None
            attempt_id = pending.get('attempt_id') if isinstance(pending, dict) else None
            period_inquiry_event_id = (
                pending.get('period_inquiry_event_id') if isinstance(pending, dict) else None
            )
            random_activity_attempt_id = (
                pending.get('random_activity_attempt_id') if isinstance(pending, dict) else None
            )
            random_activity_target_at = (
                pending.get('random_activity_target_at') if isinstance(pending, dict) else None
            )
            is_random_activity = isinstance(random_activity_attempt_id, str)
            check_now = dt.datetime.now(TZ)
            if not isinstance(pending, dict) or not isinstance(recipe, dict):
                event.set_extra('_oppo_health_refresh_deferred', True)
                event.stop_event()
                return
            if isinstance(attempt_id, str) and attempt_id:
                event.stop_event()
                return
            if is_random_activity:
                try:
                    queued_target = dt.datetime.fromisoformat(
                        str(random_activity_target_at).replace('Z', '+00:00')
                    )
                except ValueError:
                    queued_target = None
                check_now = dt.datetime.now(TZ)
                if (
                    queued_target is None
                    or is_consciousness_sync(event)
                    or self._goodnight_quiet_for_today(check_now)
                    or self._low_steps_mode_suppressed(queued_target, check_now)
                    or check_now.date() != queued_target.astimezone(TZ).date()
                    or check_now.hour >= 21
                ):
                    await self._finish_random_activity(
                        random_activity_attempt_id, 'suppressed', check_now,
                        reason='pre_generation_gate',
                    )
                    self._pending_care_contexts.pop(token)
                    event.stop_event()
                    return
            steps_marker = pending.get('low_steps_marker') if isinstance(pending, dict) else None
            if steps_marker is not None and self._low_steps_dispatch_skip_reason(
                steps_marker, dt.datetime.now(TZ)
            ) is not None:
                event.stop_event()
                return
            provider_host = getattr(req, 'resolved_provider_api_host', None)
            if (
                self.health_ai_core_api is None
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
                if is_random_activity:
                    await self._finish_random_activity(
                        random_activity_attempt_id, 'failed_not_sent', dt.datetime.now(TZ),
                        reason='privacy_preflight_denied',
                    )
                    self._pending_care_contexts.pop(token)
                event.stop_event()
                return
            if not await self._request_route_preflight(provider_host):
                if is_random_activity:
                    await self._finish_random_activity(
                        random_activity_attempt_id, 'failed_not_sent', dt.datetime.now(TZ),
                        reason='route_preflight_denied',
                    )
                    self._pending_care_contexts.pop(token)
                event.stop_event()
                return
            fresh_result = await self._fresh_care_context_from_recipe(
                recipe, dt.datetime.now(TZ)
            )
            if fresh_result is None:
                event.set_extra('_oppo_health_refresh_deferred', True)
                event.stop_event()
                return
            care_context, live_sources, live_measurements = fresh_result
            event.set_extra('_oppo_health_live_source_receipts', live_sources)
            event.set_extra('_oppo_health_live_measurement_receipts', live_measurements)
            if is_random_activity:
                try:
                    async with self._random_activity_lock:
                        ledger, _exists = self._load_random_activity_ledger(check_now)
                        reserved, _reason = reserve_random_activity_attempt(
                            ledger, check_now,
                            dt.datetime.fromisoformat(
                                str(random_activity_target_at).replace('Z', '+00:00')
                            ),
                            random_activity_attempt_id,
                        )
                        if not reserved:
                            event.stop_event()
                            self._pending_care_contexts.pop(token)
                            return
                        write_private_json(ledger, self._random_activity_path)
                except (OSError, ValueError, TypeError):
                    self._random_activity_ready = False
                    event.stop_event()
                    return
            if isinstance(steps_marker, dict):
                try:
                    async with self._other_care_lock:
                        persisted = read_private_json(self._other_care_path)
                        step_state = prepare_other_care_ledger(persisted, check_now)
                        if (step_state['steps']['status'] != 'reserved'
                                or step_state['steps']['check_at'] != steps_marker['check_at']
                                or step_state['steps']['decided_at'] != steps_marker['reserved_at']):
                            event.stop_event()
                            self._pending_care_contexts.pop(token)
                            return
                        step_state['steps']['status'] = 'dispatching'
                        step_state['steps']['reason'] = 'fresh_context_ready'
                        write_private_json(step_state, self._other_care_path)
                except (OSError, ValueError, TypeError):
                    event.stop_event()
                    return
            event.set_extra(CARE_TOKEN_EXTRA, token)
            async def health_care_final_send_guard():
                check_at = dt.datetime.now(TZ)
                live_sources = event.get_extra('_oppo_health_live_source_receipts')
                live_measurements = event.get_extra('_oppo_health_live_measurement_receipts')
                if (
                    not source_receipts_are_fresh(live_sources, check_at)
                    or not measurement_receipts_are_fresh(live_measurements, check_at)
                    or self._goodnight_quiet_for_today(check_at)
                    or is_consciousness_sync(event)
                ):
                    return False
                if isinstance(steps_marker, dict) and self._low_steps_dispatch_skip_reason(
                    steps_marker, check_at
                ) is not None:
                    return False
                if event_ids:
                    try:
                        current_config, _revision = self._care_repository.load_config()
                        current_state = self._care_repository.load_scheduler_state()
                    except (CareStorageError, OSError, ValueError, TypeError):
                        return False
                    for event_id in event_ids:
                        job = current_state.get('jobs', {}).get(event_id)
                        rule = (
                            current_config.get('rules', {}).get(job.get('rule_id'))
                            if isinstance(job, dict) else None
                        )
                        if (
                            not isinstance(rule, dict)
                            or job.get('state') not in {'dispatching', 'handed_off'}
                            or dispatch_window_skip_reason(
                                rule, job, check_at, not_before=self._activated_at
                            ) is not None
                        ):
                            return False
                return True

            event.set_extra(
                '_oppo_health_care_before_send', health_care_final_send_guard
            )
            if is_random_activity:
                event.set_extra(RANDOM_ACTIVITY_EVENT_EXTRA, True)
                event.set_extra(RANDOM_ACTIVITY_ATTEMPT_ID_EXTRA, random_activity_attempt_id)
                event.set_extra('_oppo_random_activity_target_at', random_activity_target_at)
                attach_random_activity_attempt(event, random_activity_attempt_id)

                async def final_send_guard():
                    async def release_coordination_if_blocked():
                        token = event.get_extra(COORDINATION_TOKEN_EXTRA)
                        if isinstance(token, str) and token:
                            await self.proactive_coordinator.settle(token, "failed_not_sent")
                            event.set_extra(COORDINATION_TOKEN_EXTRA, None)

                    target_text = event.get_extra('_oppo_random_activity_target_at')
                    try:
                        target = dt.datetime.fromisoformat(str(target_text).replace('Z', '+00:00'))
                    except ValueError:
                        await release_coordination_if_blocked()
                        return False
                    now = dt.datetime.now(TZ)
                    live_sources = event.get_extra('_oppo_health_live_source_receipts')
                    live_measurements = event.get_extra('_oppo_health_live_measurement_receipts')
                    allowed = bool(
                        source_receipts_are_fresh(live_sources, now)
                        and measurement_receipts_are_fresh(live_measurements, now)
                        and not is_consciousness_sync(event)
                        and not self._goodnight_quiet_for_today(now)
                        and not self._low_steps_mode_suppressed(target, now)
                        and now.date().isoformat() == target.astimezone(TZ).date().isoformat()
                        and now.hour < 21
                    )
                    if not allowed:
                        await release_coordination_if_blocked()
                    return allowed

                event.set_extra('_oppo_random_activity_before_send', final_send_guard)
                req.func_tool = None
            if isinstance(attempt_id, str) and attempt_id:
                if not self._sleep_delivery_ready:
                    event.stop_event()
                    return
                attach_attempt(event, attempt_id)
            if isinstance(period_inquiry_event_id, str):
                attach_period_inquiry(event, period_inquiry_event_id)
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
        own_plain_private = False
        parts = self.umo.rsplit(':', 2)
        if bool(components) and all(isinstance(component, Plain) for component in components) and len(parts) == 3:
            sender_id = None
            try:
                sender_id = event.get_sender_id()
            except Exception:
                sender = getattr(message_obj, 'sender', None)
                sender_id = getattr(sender, 'user_id', None)
            own_plain_private = str(sender_id) == parts[2]
        weight_command = explicit_weight_mode_command(
            text, own_private_plain_message=own_plain_private
        )
        if weight_command is not None:
            self._apply_weight_mode_command(weight_command)
            return
        if explicit_goodnight_message(text, own_private_plain_message=own_plain_private):
            try:
                now = dt.datetime.now(TZ)
                state = self._state(now, self._cache)
                state['goodnight_date'] = now.date().isoformat()
                self._save_state(state)
            except (OSError, RuntimeError, ValueError):
                logger.warning('[oppo_health] 晚安静默状态无法安全保存；本轮主动关怀保持暂停。')
                self._last_goodnight_date = dt.datetime.now(TZ).date().isoformat()
        requested_local_fields = local_fields_for_query(text)
        health_words_in_query = any(word in text for word in HEALTH_WORDS)
        if not local_fields_for_query(text) and not explicit_owner_health_data_query(text):
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
            try:
                result = await self._capture_local_fields(local_fields, timeout=240)
            except (OSError, asyncio.TimeoutError):
                result = None
            self.health_ai_core_api.stage_local_health_context(
                req,
                format_local_health_context(
                    result, local_fields, now=dt.datetime.now(TZ)
                )
                + inline_sleep_context,
                required_provider_host=provider_host,
                allowed_provider_hosts=self.approved_provider_hosts,
            )
            return
        snapshot = await self._snapshot(force=True)
        snapshot = current_health_snapshot(snapshot, dt.datetime.now(TZ))
        health_summary = (
            describe(snapshot)
            if snapshot
            else '当前没有可用记录；不能将缺失数据当作 0。'
        )
        self.health_ai_core_api.stage_local_health_context(
            req,
            '[本人健康记录，仅用于本次健康话题]\n'
            + health_summary
            + "\n沿用当前人格的性格和语气自然回应，不要把读取时间当测量时间，旧记录不能说成实时值。通常不报精确测量时间；只有记录较旧、容易误认实时或用户追问时，才用“上午那次记录”等自然说法说明。不要诊断，不要罗列全部数值，不要声称实时监控或能控制手表。"
            + ("\n" + inline_sleep_context if inline_sleep_context else ""),
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
        for key in (
            'sleep_attempted_record_id', 'sleep_attempted_at', 'sleep_plan_record_id',
            'sleep_care_selected', 'sleep_care_due_at', 'sleep_care_delay_minutes',
        ):
            if previous.get(key):
                state[key] = previous[key]
        self._save_state(state)
        return state
    def _save_state(self, state):
        write_private_json(state, self.state_path)

    def _goodnight_quiet_for_today(self, now):
        today = now.astimezone(TZ).date().isoformat()
        if getattr(self, '_last_goodnight_date', None) == today:
            return True
        if not self.state_path.exists():
            return False
        try:
            state = read_private_json(self.state_path)
        except (ValueError, OSError):
            logger.warning('[oppo_health] 晚安静默状态无法安全读取；本轮主动关怀保持暂停。')
            return True
        return isinstance(state, dict) and state.get('goodnight_date') == today

    def _submission_window_skip_reason(self, windows, pending_legacy, now):
        if self._goodnight_quiet_for_today(now):
            return "本人已直接说晚安；本地日期剩余时间停止主动关怀"
        if pending_legacy is not None:
            marker = pending_legacy.get('low_steps_marker')
            if marker is not None:
                reason = self._low_steps_dispatch_skip_reason(marker, now)
                if reason is not None:
                    return reason
            code = legacy_dispatch_skip_reason(
                pending_legacy.get('kind'), pending_legacy.get('target_at'),
                now, self._activated_at,
            )
            if code is not None:
                return {
                    'before_activation': '计划时间早于本次插件启用时刻，不补发历史关怀',
                    'not_due': '关怀时间尚未到',
                    'expired': '已错过本次关怀时段，不补发',
                    'quiet_or_expired': '处于夜间静默或已错过关怀时段，已跳过',
                }.get(code, '关怀时段无效，已跳过')
        for rule, job in windows:
            reason = dispatch_window_skip_reason(rule, job, now, self._activated_at)
            if reason is not None:
                return reason
        return None

    def _low_steps_mode_suppressed(self, check_at, reserved_at=None):
        """Read the authoritative live mode; unavailable mode closes this rule."""
        if self.umo != SLEEP_CARE_TARGET_UMO:
            return True
        try:
            from astrbot.core.star import star_map
            metadata = next((item for item in star_map.values()
                             if item.name == 'astrbot_plugin_virtual_life'), None)
            plugin = metadata.star_cls if metadata and metadata.activated else None
            mode = getattr(plugin, 'conversation_mode', None)
            if mode is None or not mode.loaded or not mode.available:
                return True
            return mode.suppress_occurrence(
                self.umo, check_at.timestamp(),
                queued_ts=reserved_at.timestamp() if reserved_at is not None else None,
            )
        except (OSError, ValueError, TypeError, AttributeError):
            return True

    def _read_other_care(self, now):
        value = read_private_json(self._other_care_path) if self._other_care_path.exists() else None
        return prepare_other_care_ledger(value, now)

    async def _reserve_other_care_attempt(self, kind, event_id, now):
        async with self._other_care_lock:
            state = self._read_other_care(now)
            state, reserved, _reason = reserve_other_attempt(state, kind, event_id, now)
            write_private_json(state, self._other_care_path)
            return reserved

    def _low_steps_dispatch_skip_reason(self, marker, now):
        if (self.config.get('low_steps_care') is not True
                or self.config.get('daily_care') is not True):
            return 'low_steps_disabled'
        try:
            state = self._read_other_care(now)
            check_at = steps_aware(marker.get('check_at'))
            reserved_at = steps_aware(marker.get('reserved_at'))
            if check_at is None or reserved_at is None:
                return 'invalid_low_steps_marker'
            suppressed = (self._goodnight_quiet_for_today(now)
                          or self._low_steps_mode_suppressed(check_at, reserved_at))
            return low_steps_dispatch_skip_reason(
                state, marker, self._cache, now, self._activated_at,
                suppressed=suppressed,
            )
        except (OSError, ValueError, TypeError, AttributeError):
            return 'low_steps_state_unavailable'

    async def _low_steps_care_tick(self, now):
        """Consume one daily check before reusing the existing private ingress."""
        if (self.config.get('low_steps_care') is not True
                or self.config.get('daily_care') is not True
                or self.umo != SLEEP_CARE_TARGET_UMO):
            return
        try:
            async with self._other_care_lock:
                persisted = (
                    read_private_json(self._other_care_path)
                    if self._other_care_path.exists() else None
                )
                state = prepare_other_care_ledger(persisted, now)
                check_at = steps_aware(state['steps']['check_at'])
                if now < check_at:
                    # Save a new day's slot and entropy once, before its first check.
                    if persisted is None or persisted.get('date') != state['date']:
                        write_private_json(state, self._other_care_path)
                    return
                # Commit schedule and entropy first; a crash cannot reroll the draw.
                write_private_json(state, self._other_care_path)
                if state['steps']['status'] == 'reserved':
                    reserved_at = steps_aware(state['steps']['decided_at'])
                    end_at = low_steps_window_end(check_at)
                    if (reserved_at is None or now >= end_at
                            or self._goodnight_quiet_for_today(now)
                            or self._low_steps_mode_suppressed(check_at, reserved_at)):
                        return
                    if self._pending_legacy_care is None:
                        marker = {
                            'date': state['date'], 'check_at': check_at.isoformat(),
                            'window_end': end_at.isoformat(), 'reserved_at': reserved_at.isoformat(),
                        }
                        token = uuid.uuid4().hex
                        prompt = f'[OPPO每日关怀 token={token}] 请自然温柔地关心本人今天是否有出门。'
                        self._pending_legacy_care = {
                            'kind': 'activity', 'reason_id': 'low_steps',
                            'dispatch_id': 'low_steps:' + marker['date'], 'token': token,
                            'attempt_id': None, 'target_at': marker['check_at'],
                            'low_steps_marker': marker, 'prompt': prompt,
                        }
                    return
                if state['steps']['status'] == 'dispatching':
                    # A previous process crossed the model boundary. Never replay it.
                    return
                suppressed = (self._pending_legacy_care is not None
                              or self._goodnight_quiet_for_today(now)
                              or self._low_steps_mode_suppressed(check_at))
                snapshot = self._cache
                if not suppressed and state['steps']['status'] == 'planned' and now >= check_at:
                    # The worker cache is scheduling input only. The threshold
                    # decision must use a fresh read; a failed read leaves the
                    # planned opportunity pending for the next worker check.
                    async def fetch_current_steps():
                        fresh = await self._snapshot(force=True)
                        checked = current_health_snapshot(fresh, dt.datetime.now(TZ))
                        total, _reason = valid_steps(checked, dt.datetime.now(TZ))
                        return checked if total is not None else None

                    snapshot = await retry_fresh_context(
                        fetch_current_steps,
                        lambda value: value,
                        lambda: (
                            dt.datetime.now(TZ) < low_steps_window_end(check_at)
                            and not self._goodnight_quiet_for_today(dt.datetime.now(TZ))
                            and not self._low_steps_mode_suppressed(check_at)
                        ),
                    )
                    if snapshot is None:
                        logger.info('[oppo_health] Low-step fresh read unavailable; keeping the daily opportunity pending.')
                        return
                state, marker, _reason = check_low_steps_once(
                    state, snapshot, dt.datetime.now(TZ), self._activated_at,
                    suppressed=suppressed,
                )
                # Persist decisions and budget before crossing the ingress boundary.
                write_private_json(state, self._other_care_path)
                if marker is None:
                    return
                from aiocqhttp import Event
                token = uuid.uuid4().hex
                prompt = f'[OPPO每日关怀 token={token}] 请自然温柔地关心本人今天是否有出门。'
                parts = self.umo.rsplit(':', 2)
                event = Event.from_payload({
                    'post_type': 'message', 'message_type': 'private', 'sub_type': 'friend',
                    'message_id': time.time_ns() % 2147483647,
                    '_oppo_synthetic_care': True,
                    'user_id': int(parts[2]), 'self_id': int(self.config['bot_qq_id']),
                    'time': int(time.time()),
                    'message': [{'type': 'text', 'data': {'text': prompt}}],
                    'raw_message': prompt, 'font': 0,
                    'sender': {'user_id': int(parts[2]), 'nickname': '健康关怀'},
                })
                if event is None:
                    return
                self._pending_legacy_care = {
                    'kind': 'activity', 'reason_id': 'low_steps',
                    'dispatch_id': 'low_steps:' + marker['date'],
                    'token': token, 'attempt_id': None, 'target_at': marker['check_at'],
                    'low_steps_marker': marker,
                    'prompt': prompt,
                }
        except (OSError, ValueError, TypeError):
            logger.warning('[oppo_health] Low-step care remains paused because its state is unavailable.')

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
        if not self._random_activity_ready:
            return
        try:
            async with self._random_activity_lock:
                activity_ledger, _exists = self._load_random_activity_ledger(now)
                migrated_legacy = migrate_random_activity_legacy(
                    activity_ledger, state['date'], state.get('activity_attempted') is True, now
                )
                if migrated_legacy:
                    write_private_json(activity_ledger, self._random_activity_path)
                try:
                    target_at = dt.datetime.fromisoformat(
                        str(state.get('activity_due', '')).replace('Z', '+00:00')
                    )
                except ValueError:
                    target_at = None
                activity_ready, _activity_reason = (
                    random_activity_eligible(activity_ledger, now, target_at)
                    if target_at is not None else (False, 'no_scheduled_target')
                )
        except (OSError, ValueError, TypeError):
            self._random_activity_ready = False
            logger.warning('[oppo_health] Random-activity delivery ledger is unreadable; random care remains paused.')
            return
        due_state = deepcopy(state)
        if activity_ready:
            # The separate ledger owns random-care quota; legacy flags are left untouched.
            due_state['activity_attempted'] = False
        kind = due_kind(
            due_state,
            snapshot,
            now,
            self._activated_at,
            self._last_user_activity,
            include_sleep=False,
        )
        if kind == 'activity' and self.config.get('random_activity_care') is not True:
            return
        if kind is None:
            return

        if self._pending_legacy_care is not None:
            logger.info('[oppo_health] A prior private care request is still pending; skipped this candidate.')
            return
        if kind == 'activity' and self._low_steps_mode_suppressed(target_at, now):
            try:
                async with self._random_activity_lock:
                    ledger, _exists = self._load_random_activity_ledger(now)
                    suppress_random_activity_day(ledger, now, target_at, 'conversation_mode')
                    write_private_json(ledger, self._random_activity_path)
            except (OSError, ValueError, TypeError):
                self._random_activity_ready = False
            return
        step_data_available = True
        if kind == 'activity':
            # A random-care event must use a newly completed fetch. Never fall
            # back to the worker cache when this read fails or is too old.
            async def fetch_current_activity():
                fetched_snapshot = await self._snapshot(force=True)
                checked = current_health_snapshot(fetched_snapshot, dt.datetime.now(TZ))
                has_metrics = bool(
                    isinstance(checked, dict)
                    and isinstance(checked.get('metrics'), dict)
                    and checked['metrics']
                    and not checked.get('errors')
                )
                return checked if has_metrics else None

            snapshot = await retry_fresh_context(
                fetch_current_activity,
                lambda value: value,
                lambda: (
                    dt.datetime.now(TZ) < dt.datetime.combine(
                        target_at.astimezone(TZ).date(), dt.time(21), tzinfo=TZ
                    )
                    and not self._goodnight_quiet_for_today(dt.datetime.now(TZ))
                    and not self._low_steps_mode_suppressed(target_at, dt.datetime.now(TZ))
                ),
            )
            has_current_metrics = bool(
                isinstance(snapshot, dict)
                and isinstance(snapshot.get('metrics'), dict)
                and snapshot['metrics']
                and not snapshot.get('errors')
            )
            if not has_current_metrics:
                self._care_refresh_retry_at = time.monotonic() + 30
                logger.info('[oppo_health] Random-care refresh unavailable; keeping the opportunity pending without using cached health data.')
                return
            self._care_refresh_retry_at = 0.0
            snapshot, step_data_available = current_random_activity_snapshot(
                snapshot, dt.datetime.now(TZ)
            )
            if not step_data_available:
                logger.info('[oppo_health] Random-care step data is missing or stale; step numbers are excluded.')
        activity_attempt_id = None
        if kind == 'activity':
            activity_attempt_id = uuid.uuid4().hex
        from aiocqhttp import Event

        topic = "这是今天随机安排的一次活动关怀，关心运动、久坐休息或最近一次心率。"
        care_token = uuid.uuid4().hex
        care_context = (
            '[OPPO每日关怀，仅用于本次私聊回复]\n'
            + (describe(snapshot, include_step_measurement_time=kind == 'activity') if snapshot else "今天暂时无法读取记录。")
            + '\n请沿用当前人格的性格和语气主动发一小段自然问候，选一项有记录的情况轻轻关心即可，不要逐项报表，通常不报精确测量时间；旧记录需要说明时用“上午那次记录”等自然说法，不把旧测量当实时值。不诊断、不夸大，不说实时监控。'
            + ('\n本次没有足够新鲜的步数样本；不要提步数、任何步数数字，也不要从历史对话回忆旧步数。请只做不含数字的轻松问候。' if kind == 'activity' and not step_data_available else '')
        )
        prompt = ('[OPPO每日关怀 token=' + care_token + '] ' + topic + ' 请沿用当前人格的性格和语气主动发一小段自然问候，'
                  '选一项有记录的情况轻轻关心即可，不要逐项报表，通常不报精确测量时间；旧记录需要说明时用“上午那次记录”等自然说法，不把旧测量当实时值。不诊断、不夸大，不说实时监控。'
                  + ('没有新鲜步数样本时，不要提步数、任何步数数字，也不要从历史对话回忆旧步数；请只做不含数字的轻松问候。' if kind == 'activity' and not step_data_available else '')
                  + '没有记录就温柔询问今天过得怎么样。')
        uid = int(parts[2])
        try:
            event = Event.from_payload({'post_type': 'message', 'message_type': 'private', 'sub_type': 'friend',
                                        'message_id': time.time_ns() % 2147483647, '_oppo_synthetic_care': True,
                                        'user_id': uid,
                                        'self_id': int(self.config['bot_qq_id']), 'time': int(time.time()),
                                        'message': [{'type': 'text', 'data': {'text': prompt}}],
                                        'raw_message': prompt, 'font': 0,
                                        'sender': {'user_id': uid, 'nickname': '每日健康关怀'}})
        except Exception:
            if activity_attempt_id:
                await self._finish_random_activity(
                    activity_attempt_id, 'failed_not_sent', dt.datetime.now(TZ),
                    reason='event_construction_failed',
                )
            raise
        if event is None:
            if activity_attempt_id:
                await self._finish_random_activity(
                    activity_attempt_id, 'failed_not_sent', dt.datetime.now(TZ),
                    reason='event_construction_failed',
                )
            raise RuntimeError('Health care event could not be constructed')
        reason_id = "random_activity"
        event_suffix = state["date"]
        self._pending_legacy_care = {
            'kind': kind,
            'reason_id': reason_id,
            'dispatch_id': f'legacy-{kind}:{event_suffix}',
            'token': care_token,
            "attempt_id": None,
            'random_activity_attempt_id': activity_attempt_id,
            "target_at": state.get("activity_due"),
            'prompt': prompt,
        }
    async def _worker(self):
        next_poll = 0
        next_rules_poll = 0
        while True:
            try:
                poll_now = time.monotonic()
                if poll_now >= next_poll:
                    await self._snapshot(force=True)
                    next_poll = time.monotonic() + 900
                    await self._care(dt.datetime.now(TZ))
                elif self._care_refresh_retry_at and poll_now >= self._care_refresh_retry_at:
                    self._care_refresh_retry_at = 0.0
                    await self._care(dt.datetime.now(TZ))
                # Low-step opportunities are independently checked every loop.
                await self._low_steps_care_tick(dt.datetime.now(TZ))
                if time.monotonic() >= next_rules_poll:
                    await self._care_rules_tick(dt.datetime.now(TZ))
                    next_rules_poll = time.monotonic() + 900
                await self._flush_pending_legacy_care()
                await self._expire_pending_care_contexts()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('[oppo_health] 本次健康刷新或关怀未完成；保留原数据，等待下一次检查。')
            await asyncio.sleep(20)
