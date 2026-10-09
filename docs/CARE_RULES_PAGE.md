# 本机关怀规则说明

页面和规则 worker 沿用 AstrBot Plugin Pages，不新开 HTTP 服务，也不扩大 host 权限。查询前会调用核心的只读私聊路由预检；缺少核心接口或预检结果异常时，规则 worker 在本机来源读取前暂停。

## 页面与本地状态

- 页面：`pages/care/index.html`，由 AstrBot Plugin Pages 自动发现。
- API：`GET /oppo_health/care/state`、`GET /oppo_health/care/preview`、`GET /oppo_health/care/privacy-status`、`POST /oppo_health/care/config`。
- GET 只读规则/调度状态，并可请求核心执行只读 runner/host 路由预检；不会触发 ADB、本机健康来源读取、认证刷新、模型调用或回复。
- 配置存储在 `StarTools.get_data_dir("oppo_health") / "care"`；POSIX 目录/文件权限分别为 `0700`/`0600`，使用原子替换和配置 revision。
- 写接口要求插件 scope、精确 Dashboard 管理员身份和同源请求；host/管理员状态只读，页面没有扩大授权按钮。
- 四种新规则默认关闭。预测提前天数默认 3 天；时区、发送时段、每日上限、重复保护、免打扰、语气、过期阈值、去重和错过后的处理均可保存。

## 已连接的候选运行链

worker 在开启规则后按所需字段请求本机 `cycle_calendar` / `weight_history`，关闭云端 auth file。来源只在当前进程内最小化为日期事实；规则状态不保存体重值。调度会先持久化单次提交预留，再调用唯一插件入口 `submit_scheduled_care(metadata_plan)`。该入口复核规则状态、到期时间、事件去重、本机私聊、来源上下文、核心预检和 handler，然后沿用现有 token→private context→`inject_health`→core privacy staging 路径。

提交 body 只含通用提示和 token。`finally` 清除 token 与上下文；handler 异常只记录安全错误类型。状态 `handed_off` 仅表示 AstraBot handler 正常返回，页面会明确写“未确认平台送达”。

## 当前运行门槛

核心 `preflight_local_health_care` 按准确的私聊 UMO 读取有效会话 runner 配置，并查询该会话当前 provider host。插件要求预检 host 与请求实际 host 相同；缺少方法、返回不完整、runner 不支持、host 未批准或不匹配时，在任何本机来源读取前暂停。预检之外，核心请求入口还会对真实 `ToolLoopAgentRunner`、host allowlist、primary/fallback 与 Live 标记做运行时复核；配置中的 runner 名称不能替代实际 runner 检查。

现有周期 OCR 只提供实际/预测的日标记，不提供明确确认的开始/结束事件，所以这两类新规则显示为来源不支持并保持不发送。结束不会根据缺失标记推断。预测 ID 在一个可见日历月份内稳定；来源进一步需要提供 cycle ID 才能保证跨月预测变更原位重排。

详见 [`INTEGRATION_CONTRACT.md`](../INTEGRATION_CONTRACT.md) 和 [`DEPLOYMENT_CHECKLIST.md`](../DEPLOYMENT_CHECKLIST.md)。

## API 保存格式

写入只接收 `expected_revision` 与完整 `config`；schema 见 [`care_rules.schema.json`](../care_rules.schema.json)。读取/保存不会对来源、模型或平台产生副作用。

## 离线验证

在此目录运行：

```sh
PYTHONPATH=collector python3 -m unittest discover -s tests -v
node tests/test_care_ui.js
node --check pages/care/care.js
```

测试使用合成来源、core/模型 route mock 和 QQ handler mock；没有连接真实健康数据、模型、ADB 或 QQ。真实 AstrBot iframe 的视觉与保存验证需在插件安全启用后进行；handler 没有平台投递回执时，页面只显示已交处理。
