# OPPO 健康关怀 · AstrBot 插件

让 AstrBot 在本人私聊中读取 OPPO 健康云端记录，并沿用当前人格自然回复。本插件不绑定特定人格。

**这是实验性接入，需要自行准备本人的 OPPO 健康凭证。仅安装插件不能自动连接手表。**

v0.2.0 增加 Windows 适配：[Windows 安装与授权说明](docs/WINDOWS.md)。Windows 完整端到端流程尚待实测。
## 能做什么

- 查询最近七天内云端已有的步数、最近一次心率、静息心率、血氧和主睡眠时长。
- 聊睡眠、心率、步数或运动时，可把健康摘要补充给当前人格；只有通过私聊、host allowlist 和兼容核心隐私 API 后才会把健康数据送入模型。
- 手表记录的起床时间约一小时后，主动关心昨晚睡眠。
- 每天北京时间 14:00–21:00 另随机关怀一次运动或心率。与睡眠关怀至少间隔两小时，最近 30 分钟在聊天则推迟。
- 限定一个本人 QQ 私聊会话，拒绝其他用户和群聊读取。缺失记录不按 0 处理。保留心率、血氧测量时间供判断新旧，通常不在回复中报精确时间；旧记录容易误认实时或用户追问时才自然说明。

不读取手表实时数据，不控制手表，也不自动做健康诊断。实际可用指标取决于设备测量能力和 OPPO 云端记录。

本目录另含独立的“关怀设置”页面和四种新规则。规则默认关闭。兼容核心必须在读取本机来源前，对准确本人私聊解析当前会话配置和实际 provider host；缺失、异常或不匹配时保持暂停。周期页面没有明确的经期开始/结束事件记录时会显示未知，不会根据缺失日标记推断。详见[本机页面读取说明](docs/LOCAL_HEALTH_PAGES.md)和[关怀规则说明](docs/CARE_RULES_PAGE.md)。

健康摘要、睡眠关怀、活动关怀和本机页面共用受限模型入口。`approved_provider_hosts` 默认空；缺少显式 HTTPS host 白名单或兼容核心隐私 API 时，所有健康数据到模型的路径都会关闭。名单只在插件私有配置中填写，当前主模型 host 必须已列入，备用模型只能使用名单里的 host；切换模型不会自动扩展名单。直接 `/oppohealth` 私聊命令仍在本人会话内返回云端摘要。模型工具 `get_oppo_health` 不再直接返回数据。详见 [本机页面读取说明](docs/LOCAL_HEALTH_PAGES.md)。
## 使用条件与验证范围

| 项目 | 条件 |
| --- | --- |
| AstrBot | 云端插件已验证 v4.27.3；本机页面候选仅配套 v4.28.2 核心补丁，未验证其他版本 |
| Python | 3.10+，推荐 3.12；需要 `zoneinfo` 和系统时区数据 |
| 系统 | macOS 已完整验证；Windows 已适配 DPAPI、路径和启动工具，完整流程待实测；Linux 未完成整套验证 |
| QQ 连接 | OneBot / NapCat 私聊平台，当前主动关怀依赖 aiocqhttp 事件处理流程 |
| 网络 | AstrBot 运行主机能访问 OPPO 云端，系统需有 `curl` |
| 手表与手机 | OPPO 健康中能看到本人的健康数据，并开启云同步 |
| 登录凭证 | 本人健康接口的有效 `token`、`token_auth_id` 和 `ssoid`，不是 QQ 密码，也不是模型 API Key |
| 模型 | 需要兼容的核心隐私 API；健康请求使用正常聊天模型，主备 host 都受显式 allowlist 限制。主动问候会消耗模型额度 |
已验证范围会随设备、地区、应用版本和运行环境变化；其他组合需自行验证。

其他 OPPO 手表可能复用相同接口，但尚未验证，不能保证兼容所有型号、地区或 App 版本。长期凭证续期和真实每日关怀到点后的效果仍需使用观察。
## 安装

1. 在 AstrBot 管理面板的插件页面，选择通过仓库链接安装：
   `https://github.com/Luxiaoqi-77/astrbot_plugin_oppo_health`
2. 按下文准备本人凭证，然后填写插件配置。
3. 保存配置并重载插件。健康数据进入模型前还须配置显式 provider host allowlist。
4. 先输入 `/oppohealth` 验证，再尝试“昨晚睡得怎么样”“今天走了多少步”。

默认不会启动后台查询，直到配置本人会话。主动关怀默认关闭，确认查询成功后再开启。
## 准备凭证

凭证必须来自本人已登录的 **OPPO 健康接口**；账号中心的通用 token 实测不能替代健康 token。

- 已有本人健康接口凭证：在插件目录执行下列交互工具，输入不会回显。不要将凭证发到聊天、Issue 或 GitHub。
- 尚无凭证：查看 [Mac 专用模拟器授权说明](docs/EMULATOR_AUTH.md)。此步骤需要额外配置，不是插件自动安装功能。

```sh
python3 collector/configure_auth.py
```

Mac/Linux 工具保存到 `~/.local/share/astrbot-oppo-health/auth.json`，目录权限 700、文件权限 600。Windows 保存到 `%LOCALAPPDATA%\AstrBot\oppo-health\auth.json`，采用当前用户 DPAPI 加密。AstrBot 与工具应使用同一操作系统用户。自定义路径可通过环境变量 `OPPO_HEALTH_AUTH_FILE` 导入，并在插件的 `auth_file` 配置中填写同一路径。
```sh
python3 collector/snapshot.py
```

成功时会列出已读取的指标名称和空错误表，不输出凭证或原始健康记录。
## 配置

| 配置项 | 填写方式 |
| --- | --- |
| `private_session` | 本人私聊 UMO，格式为 `平台ID:FriendMessage:本人QQ号`；必须使用 AstrBot 实际平台 ID |
| `bot_qq_id` | 机器人 QQ 号；开启关怀前填写 |
| `daily_care` | 开启睡眠关怀及每日关怀总开关，默认 `false` |
| `random_activity_care` | 是否另加一次随机运动心率关怀，需总开关已开启 |
| `auth_file` | 凭证文件绝对路径；留空使用默认位置，填写路径而不是凭证内容 |
| `collector_python` | 留空使用 AstrBot 当前 Python；也可填写独立 Python 绝对路径 |
| `collector_script` | 留空使用插件自带 `collector/snapshot.py` |
| `emulator_auth_refresh` | 凭证失效时尝试从专用模拟器重新读取；未搭建模拟器时保持关闭 |
| `approved_provider_hosts` | 仅供本人健康请求使用的 HTTPS host 白名单，默认空；只接受本机逐项填写的 host/origin，主模型和备用模型均须匹配。切换模型不会自动添加 host |
| `enable_local_health_pages` | 是否启用本机页面读取，默认 `false`；需要非空 host 白名单与兼容核心隐私 API |
会话 UMO 中的平台 ID 与用户 ID 由使用者在本机配置；README 不提供账号示例。
## 同步与关怀时间

插件启用时，后台每 15 分钟读取一次 OPPO 云端。健康话题聊天会先检查实际主模型 host，再读取摘要并进入核心私有暂存；`/oppohealth` 是本人私聊中的直接插件命令，不经过模型。无批准 host、provider host 不匹配或核心隔离 API 缺失时，健康模型请求停止。

手表 → 手机 → OPPO 云端的同步由 OPPO 健康负责，插件不保证其固定频率，也不能强制上传。因此“醒后一小时”从云端主睡眠的起床时间计算；云端上传延迟会影响实际发送时间。睡眠按起床日期归档，例如查询今天会得到昨晚到今早的主睡眠。

Mac 和 AstrBot 需要运行且联网。完成凭证准备后，手机不必一直连 USB，也不必与 Mac 在同一 Wi-Fi，但手机健康数据仍需正常同步云端。

后台循环以 20 秒为等待粒度；云端快照刷新和旧睡眠/活动关怀检查均约每 15 分钟一次。四条新规则也每 15 分钟检查一次，但仅在至少一条规则开启时才会预检并读取对应模拟器页面；规则默认全关。本人查询按请求读取，不等待后台周期。每类旧关怀每天最多尝试一次；加载前已到点的睡眠关怀不补发，23 点后不发睡眠关怀，随机关怀最晚在 21 点前。发送前写入去重状态，因此当天发送失败不会重试；这样可避免重启后的重复打扰。此处记录的是交给回复流程的尝试，不是 QQ 送达回执。

关怀时间目前固定使用北京时间，不提供其他时区配置。
## 隐私与数据存储

- 查询是本人账号的只读云端调用，不包含其他人的凭证。
- 配置的本人私聊之外，其他用户和群聊无法调用健康查询。
- 凭证通过 curl 标准输入传递，不放入进程命令行参数或日志。
- 本地保存凭证、当天健康摘要及去重状态；Mac/Linux 默认 `~/.local/share/astrbot-oppo-health/`，Windows 默认 `%LOCALAPPDATA%\AstrBot\oppo-health\`，采用 DPAPI 加密。
- **只有本人明确配置并批准的 HTTPS host 可以接收健康摘要和关怀上下文。**生成的回复会经 QQ 发送；启用前应了解所用模型服务的数据处理方式。
- 云端摘要、本机页面和 sleep/activity care 共用同一核心暂存与路由检查；请求钩子结束后才把数据写入模型 prompt。新主模型 host 不在白名单时会阻止该健康请求，不自动添加 host。
- 本机页面候选只采集本次明确请求的字段，不向采集子进程传入云端凭证路径；缺少兼容核心 API 时，云端健康对话和睡眠/活动模型关怀也 fail closed。
- 仓库不包含账号凭证、健康记录、手机 APK、Android 镜像或模拟器数据。发布时应只提交代码。
## 常见问题

**没有可用记录 / 部分指标不可用**：确认 OPPO 健康开启云同步、同一账号已记录数据、主机可联网。缺失不代表数值为 0。

**`error_code: 10101`**：可能是凭证无效、过期或用了账号中心 token。重新获取健康 TokenHelper 的 token，核对三个字段。模拟器刷新不保证永久免登录，OPPO 要求重新登录时仍需本人操作。

**凭证权限错误**：Mac/Linux 确认文件归运行 AstrBot 的用户所有，权限为 600、父目录为 700；Windows 确认导入工具与 AstrBot 使用同一系统用户，不能直接导入 Mac 的明文文件。

**普通聊天没用到数据**：健康摘要只对本人私聊中的健康话题启用；确认本人 UMO、`approved_provider_hosts` 和兼容核心 API 已配置。`get_oppo_health` 工具不会直接返回健康值。

**没主动关怀**：检查总开关、机器人 QQ 号、NapCat 私聊连接、host allowlist、兼容核心 API、LLM 是否可用，以及今天是否已经尝试过关怀。主模型或备用模型 host 不在名单时会停止健康关怀；若当天已尝试则不重发。随机关怀会避开最近聊天。
**接口变了 / 其他型号读不到**：这是基于当前 OPPO 云端接口的实验性实现，没有官方接口稳定性保证。可提交不含凭证和健康原始记录的错误状态。
## 开发验证

```sh
PYTHONPATH=collector:. python3 -m unittest discover -s tests -p 'test_*.py'
```

离线测试覆盖凭证权限、Windows 私有文件保护、SDK 路径与架构选择、请求边界、步数去重、日期筛选、特殊心率排除、缺失值、关怀时间和避让规则，不联网、不发 QQ 消息。发布验证另外在 AstrBot 环境使用模拟接收器检查私聊权限与持久化去重。 本机页面测试使用合成输入并 mock 采集，不访问 ADB、模型服务或 QQ。
## 来源与许可

本项目采用 MIT 许可。`collector/oppo_sign.py` 的签名实现来自 [foxlesbiao/oppo-health-cloud-mcp](https://github.com/foxlesbiao/oppo-health-cloud-mcp)，参考提交 `d6a1040e4e35ef7f2bd8fe447a991db0fe0b4272`；保留其许可于 [collector/LICENSE.upstream](collector/LICENSE.upstream)。该文件顶部的签名常量是上游协议实现的一部分，不是某个使用者的账号凭证。
