# 本机健康页面读取说明

本目录中的 `main.py`、`local_request_policy.py`、`collector/local_capture.py` 与 `collector/local_health_adapter.py` 构成插件侧读取实现。`approved_provider_hosts` 默认空，因此健康数据到模型、睡眠关怀、活动关怀和本机页面都默认关闭。只有在本机显式填写 HTTPS host 白名单、核心 API 可用、会话精确匹配本人私聊且查询为纯文本时，健康值才会进入受限请求。

官方 AstrBot v4.28.2 基线不包含本功能需要的 `local_health_privacy` 模块、provider 请求字段或 host guard。核心补丁须与目标版本匹配并已加载；缺少兼容接口时，健康话题模型回复、云端 snapshot 注入、睡眠/活动关怀及本机页面均 fail closed。本人私聊中的 `/oppohealth` 插件命令仍可直接返回云端摘要。插件会检查 `stage_local_health_context(request, context, *, required_provider_host=..., allowed_provider_hosts=...)` 等能力；缺少接口的自定义核心不会收到健康数据。

## 页面范围

解析器只接受当前用户明确询问的页面：经期日历、体重记录、日晒、身心状态、活动卡路里、步数摘要和步行距离。OCR 页面日期、单位、标签或图例不明确时会返回不可用，不把缺失数据补成零。

采集器通过 ADB 连接已打开 OPPO 健康的 Android 模拟器，并在 macOS 上使用系统 Vision OCR。截图只经标准输入传给 OCR 进程；候选代码不把截图写入仓库。它要求明确配置模拟器 serial，且拒绝 `emulator-N` 以外的目标。

配置示例使用占位符，替换值只保存在使用者本机：

```sh
export OPPO_HEALTH_ADB=/path/to/adb
export OPPO_HEALTH_EMULATOR_SERIAL=emulator-<id>
# 仅在使用非默认 ADB server 端口时设置：
export OPPO_HEALTH_ADB_SERVER_PORT=<port>
```

该读取路径目前限 macOS；Windows 与 Linux 尚未完成 Vision / ADB 的整套验证。真实健康页面应仅在已授权的专用模拟器中验证，发布测试只使用合成输入。

## 必要的 AstrBot 核心隔离

页面读取结果会成为单次模型请求的上下文，因此插件代码不足以覆盖完整隔离边界。云端 snapshot 和 care worker 也必须走同一暂存入口；care 的模拟用户消息只携带临时 token，健康文本只保存在插件内存中。端到端支持需要与目标 AstrBot 版本匹配的核心补丁，保护请求标记与上下文生命周期、精确私聊校验、显式 host 白名单、primary/fallback 路由、工具循环、重试、日志、历史、安全检查和回复装饰。

插件适配器探测函数与参数，不依赖自行设想的 API 版本标记。`approved_provider_hosts` 是使用者私有配置，默认空；插件不从当前模型推导或扩展名单。核心 staging 在其他 `OnLLMRequestEvent` 插件运行期间私下保存实际 primary host 和显式 host 列表；钩子完成后恢复并校验 primary。内置 ToolLoop 只允许名单内 fallback。health-care 事件沿用当前聊天模型，模型调用前读取实际 host；不在名单时停止请求。缺少 staging 参数、隐私钩子或 host guard 时，插件关闭全部健康到模型路径。

配套核心差异以官方 AstrBot v4.28.2 为基线单独审阅，不包含任何使用者 host，且不覆盖插件仓库许可。模型侧健康功能只支持内置 ToolLoop agent；自定义/第三方 runner、实时语音、非 HTTPS provider、缺少明确 `api_base` 或主备 host 不在白名单的请求均关闭。`get_oppo_health` 保留兼容入口但不会直接返回健康值；健康摘要只通过受限 prompt 暂存路径提供。

## 离线测试

`tests/test_local_health_adapter.py` 使用合成日期、文字和数值。`tests/test_local_capture.py` mock 了采集与临时目录，并验证未配置 ADB 或指定实体设备时不执行命令。离线测试不会访问模拟器、云端或模型。
