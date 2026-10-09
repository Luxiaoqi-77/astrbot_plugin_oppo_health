# 本机关怀规则说明

页面和规则 worker 沿用 AstrBot Plugin Pages，不新开 HTTP 服务，也不扩大 host 权限。新规则读取本机来源前会做只读私聊路由预检；缺少兼容核心接口或预检异常时保持暂停。

## 用户规则

- 睡眠：每条新的有效醒来记录 50% 独立抽签，长期平均约两天一次，不按固定隔日；选中后按 OPPO 记录的醒来时间加随机 60–120 分钟安排。22:00–08:00 不主动发送；本人睡前直接说晚安后，本地当日不再主动打扰。
- 旧睡眠和活动各自最多一次/日，合计最多 2 次且不保证每天发送；各自按记录 ID/本地日期去重。它们与同轮到期的新类别合并成一条本人私聊回复；新类别仍按自己的来源事件/日期去重，不借用旧规则次数。七条新规则按各自开关执行，不共用每日 2 次预算或全局最短 4 小时间隔。主动查询任何健康数据不计主动关怀额度。
- 经期：预测仅在预计开始 D-3、D-2、D-1 每天一次；来源明确记录实际开始后 D1–D3 每天一次；D4 起到默认 D14 观察窗内，按每天默认 0.5 概率随机询问是否结束，默认两次询问至少间隔 1 天、每天最多一次；明确结束事件马上取消当天待发询问并停止后续询问。起始日、概率、冷却天数和观察窗都可配置。开始和结束必须来自 OPPO 明确事件通道；缺任一通道时暂停并显示原因。机器人不代替用户记录或按用户回答修改 OPPO 记录。
- 体重：只响应本人准确私聊的明确 opt-in/opt-out 语句。模式开启后，仅接受实际测量时间晚于开启时间的新测量；测量值不进入关怀提示、事件事实、历史或页面。来源没有独立记录创建时间，页面会说明此限制。
- 身心状态：分数越高越好。可选 `Slow down` 分类，或把数值阈值留空直到确认正式量纲；不能把来源标签当成独立测量验证。判定要求成功解析、真实日期/时间、有效分类和新鲜来源；缺少质量标签会显示限制而不自动禁用。每周默认 5 个随机候选时段，范围可配置为 1–7；没新鲜的独立样本就跳过，不补数。同一个持续 episode 在出现新样本并满足最短间隔后可再次关怀。
- 日照：每日随机检查时间在 20:00–21:00；同日 OPPO 分钟记录 0–5（含边界）可触发一次，超过 5 跳过。未知、缺失或单位不明不按 0 处理。

## 页面与 API

- 页面：`pages/care/index.html`，由 AstrBot Plugin Pages 自动发现。
- API：`GET /oppo_health/care/state`、`GET /oppo_health/care/preview`、`GET /oppo_health/care/privacy-status`、`POST /oppo_health/care/config`。
- 页面列出每条规则状态、开关、可调参数、预计时间、最近不触发/触发原因，以及可用的数据日期、测量时间、读取时间、完整性限制和最近执行历史。
- 保存使用配置 revision；页面 POST 后立即 GET 回读，核对 revision 与规则内容。写接口要求插件 scope、精确 Dashboard 管理员身份和同源请求；host/管理员状态只读。
- 私有配置写入 `StarTools.get_data_dir("oppo_health") / "care"`；POSIX 目录/文件权限分别为 `0700`/`0600`，采用原子替换。文件不包含认证凭证、体重值或状态分值。
- 配置页七条新规则默认关闭；状态规则未确认模式、时区和必要参数时不能保存为启用。体重模式独立默认关闭，只能由本人明确私聊 opt-in 开启。
- 错过的事件发送窗口始终跳过，不顺延。首次加载以本次插件启用时刻作发送下限；启用前到期的旧睡眠/活动或新类别计划不会补发。发送入口再次校验启用下限、窗口截止、免打扰与当日“晚安”状态；睡眠 22:00–08:00、旧活动 21:00–14:00 均静默。

## 来源与调度限制

当前周期 OCR 仅提供实际/预测日标记，不提供明确确认的开始/结束事件；D1–D3、后段询问、明确结束三条路径都会暂停并显示原因。后段询问要求开始与结束事件通道同时存在，明确结束只接受 OPPO 事实。预测规则只使用页面明确标为预测的日期。预测事件 ID 在一个可见日历月中稳定；如果来源后续增加 cycle ID，可进一步保证跨月更新稳定。

体重记录缺少独立创建时间和来源质量证明；使用明确 opt-in 时间与实际测量时刻比较，并仅对更新后的测量日期/时刻安排。这个过滤不能证明晚到同步的记录创建时刻。

身心状态采集器当前提供独立分类点的设备日期、分钟级测量时间和页面更新时间；没有质量标签不会阻断 `Slow down` 模式，但当前代码无法独立证明测量质量。过期、跨日、未来或乱序测量、缺失日期/时间/分类仍暂停解释。数值模式在正式量纲确认前保持未配置。

Worker 仅按已开启规则请求 `cycle_calendar`、`weight_history`、`wellness_home` 或 `sun_exposure`，并关闭云端 auth file。规则先记录保留，再通过 `submit_scheduled_care_batch(metadata_plans)` 做二次检查并合并同一轮同时到期的原因。入口会复核规则、到期时间、未补发下限、窗口截止、免打扰、晚安、事件去重、本人私聊、来源上下文、隐私预检与 handler；token 和上下文在 `finally` 清除。`handed_off` 仅表示 AstrBot handler 正常返回，不表示平台已送达。

## 离线验证

在本目录运行：

```sh
PYTHONPATH=collector:.:tests python3 -m unittest test_care_api test_care_integration test_care_rules test_care_scheduler test_care_store test_care_wellness test_care_web_security test_health test_local_capture test_local_health_adapter test_local_request_policy test_platform test_plugin_entry test_probe test_storage
node tests/test_care_ui.js
node --check pages/care/care.js
```

测试使用合成来源、受控时钟、确定性随机数、core route mock 和私聊 handler mock；没有连接真实健康数据、模型、ADB 或 QQ。`tests/test_core_preflight_integration.py` 依赖外围 AstrBot checkout 中已安装的插件，因此不包含在隔离测试命令中。页面 JavaScript/API 使用合成输入验证；真实 AstrBot Plugin Page iframe 与设备页面集成尚未端到端验证。
