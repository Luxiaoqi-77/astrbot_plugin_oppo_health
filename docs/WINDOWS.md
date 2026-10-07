# Windows 使用说明（v0.2.0）

已增加 Windows 原生路径、curl.exe、DPAPI 私有存储和模拟器助手脚本适配。**当前未完成真实 Windows 主机上的 OPPO App 登录与完整端到端验证**。有本人有效健康凭证时，可以按下文部署云端查询；没有凭证时，仍需先解决本人授权。

## 1. 基本环境

- Windows 10/11，推荐 64 位 Python 3.12。
- AstrBot 能正常使用 NapCat / OneBot 私聊和 LLM。
- `curl.exe` 在 PATH 中，Python 安装 `tzdata`。插件安装时会读取 requirements.txt。
- OPPO 健康开启云同步，准备本人健康接口的有效三个凭证字段。

在 **PowerShell** 中进入安装后的插件目录。下面的 Python 应与运行 AstrBot 的 Python 一致，必要时改成虚拟环境里的完整路径。

```powershell
python --version
curl.exe --version
python -m pip install -r requirements.txt
python collector\check_setup.py
```

若你的 Python 命令是 `py`，将示例中的 `python` 替换为 `py -3.12`。PowerShell 路径有空格时，用 `& "C:\路径\python.exe"` 调用，不要使用 macOS 的 `.venv/bin/python3` 路径。

## 2. 导入本人凭证

```powershell
python collector\configure_auth.py
python collector\check_setup.py
python collector\snapshot.py
```

输入不会回显，不要用命令行参数传 token。默认文件目录：

```text
%LOCALAPPDATA%\AstrBot\oppo-health\
  auth.json
  snapshot.json
  daily-care.json
```

这些文件使用 Windows 当前用户的 **DPAPI** 加密，磁盘上不是明文 token 或健康摘要。不要手工修改 auth.json，也不要复制 Mac 的明文凭证文件来替代导入步骤。

运行导入工具和 AstrBot 必须使用同一 Windows 用户。更换系统用户、机器或使用 SYSTEM 身份启动服务后，可能无法解密；应由目标运行用户重新导入本人凭证。实现不启用允许所有本机用户解密的 `CRYPTPROTECT_LOCAL_MACHINE` 标志。参见 [Microsoft DPAPI 说明](https://learn.microsoft.com/en-us/windows/win32/seccrypto/example-c-program-using-cryptprotectdata)。

如果设自定义路径：

```powershell
$env:OPPO_HEALTH_AUTH_FILE = "$env:LOCALAPPDATA\AstrBot\oppo-health\my-auth.json"
python collector\configure_auth.py
```

在插件 `auth_file` 配置中填写同一路径。不要把实际凭证粘贴到配置项或 Issue。

## 3. 配置插件

填写本人 `private_session`、机器人 `bot_qq_id`，在当前人格中启用 `get_oppo_health`。`collector_script` 留空即可使用插件内置程序；`collector_python` 留空使用 AstrBot 当前 Python。

保存后重载插件。先在本人 QQ 私聊发 `/oppohealth`，成功后开启 `daily_care`。普通健康话题和工具查询每次都会重新读取云端，不等待后台 15 分钟周期，但不能强制手表实时上传。

## 4. 可选：Windows 模拟器授权助手

已有凭证时不要求模拟器。模拟器用于尝试获取 / 重新读取凭证，仍属于高级实验性配置。

需先自行准备官方 Android SDK、可登录 OPPO 健康的普通用户镜像、允许 adb root 的专用副本镜像，以及本人登录后的独立 userdata / encryptionkey raw 副本。总体步骤见 [模拟器授权说明](EMULATOR_AUTH.md)。

Windows SDK 可使用自己的安装位置；环境变量只改变本插件助手，不修改系统其他 Android 配置：

```powershell
$env:OPPO_HEALTH_ANDROID_SDK = "$env:LOCALAPPDATA\Android\Sdk"
$env:OPPO_HEALTH_EMULATOR_ROOT = "$env:LOCALAPPDATA\AstrBot\oppo-health\emulator"
# 如果 AVD 放在 Android Studio 默认目录，明确填写：
$env:OPPO_HEALTH_AVD_HOME = "$env:USERPROFILE\.android\avd"
# 名称需与自己已经创建的 AVD 对应：
$env:OPPO_HEALTH_LOGIN_AVD = "AstrBot_OPPOHealth_user30"
$env:OPPO_HEALTH_AUTH_AVD = "AstrBot_OPPOHealth_authcheck30v2"
```

在该 emulator 根目录下准备 `auth-flat\userdata-flat.img`、`auth-flat\encryption-flat.img`、`downloads\frida-server-16.7.19-android-<架构>`。路径有空格也会作为独立参数传给 SDK，不经 shell 拼接。

**不要把 Mac 的 ARM64 镜像或 Frida server 直接搬到 Intel/AMD Windows。**Android 镜像应匹配主机虚拟化支持；助手会读取安卓 guest ABI，选择 `arm64`、`x86_64`、`arm` 或 `x86` 对应的 server。原始 OPPO APK 是否能在所选 x86_64 镜像安装、登录、同步，尚未验证；这里是程序适配，不是 APK 兼容保证。参见 [Android 模拟器加速要求](https://developer.android.com/studio/run/emulator-acceleration)。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r collector\requirements-emulator.txt
.\.venv\Scripts\python.exe collector\run_auth_emulator.py
```

助手显示 ready 后，在另一个终端设置相同环境变量，然后执行：

```powershell
.\.venv\Scripts\python.exe collector\extract_emulator_auth.py
.\.venv\Scripts\python.exe collector\snapshot.py
```

只在专用测试副本中操作，不需要真实手机 Root。准备后如果开启 `emulator_auth_refresh`，插件 `collector_python` 应指向此虚拟环境的 `Scripts\python.exe`，并保证后台 AstrBot 也获得相同的 SDK / AVD 环境变量。只在一个 PowerShell 窗口中设置变量，不会自动传给已经运行的 AstrBot。

本仓库不自动建立 Windows 计划任务。需要后台启动时，可以在任务计划程序中按自己的部署方式配置，务必用导入凭证的同一用户执行。长期 token 续期仍未验证，不保证永久免登录。

## 5. 故障排查与验证

- `auth: not_configured`：运行交互导入工具。
- `invalid_or_wrong_user`：检查运行用户是否一致，重新导入；Windows 不接受旧明文格式。
- `curl: missing`：安装 curl 并加入 PATH；PowerShell 的 `curl` 可能是别名，程序使用的是 `curl.exe`。
- `timezone: missing`：为实际使用的 Python 安装 `tzdata`。
- APK 安装失败、`adb root` 不可用、Frida server 缺失：先处理独立模拟器的系统 / APK / 架构问题，不等于健康云端查询故障。

离线测试（仅使用虚构凭证和健康记录，不登录、不联网、不发 QQ）：

```powershell
$env:PYTHONPATH = "collector;."
python -m unittest discover -s tests -p "test_*.py"
```

Windows 专属测试会真正调用 DPAPI，检查加密保存、同用户往返和损坏密文拒绝。在 Mac/Linux 上这项测试会跳过；不能将跳过当成 Windows 实测通过。
