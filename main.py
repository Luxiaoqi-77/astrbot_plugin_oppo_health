"""Private OPPO summaries, wake-up care and one optional daytime care."""
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import time
import sys
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.session_plugin_manager import SessionPluginManager
from .care_logic import due_kind, new_activity_due, wake_due
from .collector.storage import private_root, read_private_json, write_private_json

NAME = 'oppo_health'
TZ = ZoneInfo('Asia/Shanghai')
HEALTH_WORDS = ('健康', '心率', '血氧', '睡眠', '步数', '手表', '运动', '不舒服', '累', '困', '没睡', '熬夜', '睡得', '睡了', '走了', '跑步', '锻炼')


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

    async def initialize(self):
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

    async def _allowed(self, event):
        if not self.umo or ':FriendMessage:' not in self.umo or event.unified_msg_origin != self.umo:
            return False
        return await SessionPluginManager.is_plugin_enabled_for_session(self.umo, NAME)

    async def _run(self, script, *args):
        python = self.config.get('collector_python') or sys.executable
        env = os.environ.copy()
        if self.config.get('auth_file'):
            env['OPPO_HEALTH_AUTH_FILE'] = str(Path(self.config['auth_file']).expanduser())
        process = await asyncio.create_subprocess_exec(
            python, str(script), *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=95)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return None
        if process.returncode != 0:
            return None
        try:
            return json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            return None

    async def _snapshot(self, date=None, force=False):
        async with self._lock:
            today = dt.datetime.now(TZ).date().isoformat()
            date = date or today
            if not force and self._cache and self._cache.get('date') == date and time.monotonic() - self._cache_time < 600:
                return self._cache
            script = Path(self.config.get('collector_script') or Path(__file__).parent / 'collector/snapshot.py').expanduser()
            result = await self._run(script, '--json', '--date', date)
            if self.config.get('emulator_auth_refresh', False) and result and any(isinstance(e, dict) and e.get('error_code') == 10101
                              for e in result.get('errors', {}).values()):
                # SDK refresh runs only in the isolated copy, never on the USB phone.
                await self._run(script.with_name('extract_emulator_auth.py'))
                result = await self._run(script, '--json', '--date', date)
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
        """查询本人 OPPO 手表健康数据，包括步数、心率、血氧、睡眠。保留测量时间，不能把旧测量称作当前值。

        Args:
            date(string): today 或最近七天内的 YYYY-MM-DD 日期。
        """
        if not await self._allowed(event):
            return '当前会话无权访问健康数据。'
        today = dt.datetime.now(TZ).date()
        try:
            requested = today if date == 'today' else dt.date.fromisoformat(date)
        except ValueError:
            return '日期格式应为 YYYY-MM-DD。'
        if not today - dt.timedelta(days=6) <= requested <= today:
            return '仅支持最近七天的健康记录。'
        snapshot = await self._snapshot(requested.isoformat(), force=True)
        return describe(snapshot) if snapshot else '暂时没有可用数据，不要推测数值。'

    @filter.on_llm_request()
    async def inject_health(self, event: AstrMessageEvent, req):
        if not await self._allowed(event):
            return
        text = event.message_str or ''
        if text.startswith('[OPPO每日关怀]'):
            return
        self._last_user_activity = dt.datetime.now(TZ)
        if not any(word in text for word in HEALTH_WORDS):
            return
        snapshot = await self._snapshot(force=True)
        if snapshot:
            req.system_prompt += '\n[本人健康记录，仅用于本次健康话题]\n' + describe(snapshot) + (
                '\n沿用当前人格的性格和语气自然回应，不要把读取时间当测量时间，旧记录不能说成实时值。通常不报精确测量时间；只有记录较旧、容易误认实时或用户追问时，才用“上午那次记录”等自然说法说明。不要诊断，不要罗列全部数值，'
                '不要声称实时监控或能控制手表。\n')

    def _state(self, now, snapshot):
        if self.state_path.exists():
            try:
                state = read_private_json(self.state_path)
                if state.get('date') == now.date().isoformat():
                    return state
            except (ValueError, OSError):
                raise RuntimeError('Daily health care state is unreadable')
        state = {'date': now.date().isoformat(),
                 'activity_due': new_activity_due(now, wake_due(snapshot, now))}
        self._save_state(state)
        return state

    def _save_state(self, state):
        write_private_json(state, self.state_path)

    async def _care(self, now):
        if not self.config.get('daily_care', False):
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
        kind = due_kind(state, snapshot, now, self._activated_at, self._last_user_activity)
        if kind == 'activity' and not self.config.get('random_activity_care', True):
            return
        if kind is None:
            return
        # Reserve before dispatch so a reload cannot cause duplicate messages.
        state[kind + '_attempted'] = True
        self._save_state(state)
        from aiocqhttp import Event
        topic = ('用户手表记录的起床时间已过去约一小时，关心昨晚睡眠。'
                 if kind == 'sleep' else '这是今天随机安排的一次活动关怀，关心运动、久坐休息或最近一次心率。')
        prompt = ('[OPPO每日关怀] ' + topic + ' 请沿用当前人格的性格和语气主动发一小段自然问候，'
                  '选一项有记录的情况轻轻关心即可，不要逐项报表，通常不报精确测量时间；旧记录需要说明时用“上午那次记录”等自然说法，不把旧测量当实时值。不诊断、不夸大，不说实时监控。'
                  '没有记录就温柔询问今天过得怎么样。\n' +
                  (describe(snapshot) if snapshot else '今天暂时无法读取记录。'))
        uid = int(parts[2])
        event = Event.from_payload({'post_type': 'message', 'message_type': 'private', 'sub_type': 'friend',
                                    'message_id': time.time_ns() % 2147483647, 'user_id': uid,
                                    'self_id': int(self.config['bot_qq_id']), 'time': int(time.time()),
                                    'message': [{'type': 'text', 'data': {'text': prompt}}],
                                    'raw_message': prompt, 'font': 0,
                                    'sender': {'user_id': uid, 'nickname': '每日健康关怀'}})
        if event is None:
            raise RuntimeError('Health care event could not be constructed')
        await handler(event)
        logger.info('[oppo_health] %s 关怀已交给私聊回复流程。', kind)

    async def _worker(self):
        next_poll = 0
        while True:
            try:
                if time.monotonic() >= next_poll:
                    await self._snapshot(force=True)
                    next_poll = time.monotonic() + 900
                await self._care(dt.datetime.now(TZ))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('[oppo_health] 本次健康刷新或关怀未完成；保留原数据，等待下一次检查。')
            await asyncio.sleep(20)
