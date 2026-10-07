# Mac 专用模拟器获取本人授权

这是高级配置说明。插件没有下载 Android SDK、创建虚拟机或替你登录的安装器；下面列出已验证的方法与附带工具的前置条件。没有凭证或不会部署 Android Emulator 时，请先完成授权环境，再安装插件，不要把“插件已安装”理解为“手表已连接”。

## 已验证的方法

在 Apple Silicon Mac 上，使用 Google 官方 Android Emulator、Android 30 arm64 镜像和手机导出的原版 OPPO 健康 APK：

1. 在普通用户版 `google_apis_playstore` 镜像登录本人 OPPO 账号，接受必要条款并开启云同步，确认可以看到真实记录。
2. 优雅关闭该虚拟机。使用 `qemu-img convert -O raw` 分别将其 **userdata 与 encryptionkey 的 QCOW2 覆盖层**转换成独立 raw 副本。不能只复制底层 `.img`，那可能丢掉已经登录的覆盖层内容；原虚拟机数据应保留备份。
3. 用独立 `default` 开发镜像和副本数据启动另一个专用虚拟机，端口与原虚拟机分离。开发镜像会触发健康 App 的 Root 提醒，不要点会退出进程的 OK。
4. 仅对这个专用副本启用 `adb root` 和 Frida 本地监听。通过 SDK getter 读取本人的三个健康凭证字段，不改 APK，不操作真手机的 Root。
5. 保存凭证后调用云端只读接口验证，原普通用户虚拟机可以恢复运行。

只在本人已经授权、登录的测试副本中操作。不要关闭真实手机的系统防护，也不要收集其他人的账号凭证。

## 凭证来源

附带的 `collector/extract_emulator_auth.py` 读取：

- `TokenHelper.getInstance().getToken()` → `token`
- `TokenHelper.getInstance().getDeviceId()` → `token_auth_id`
- `OppoAccountManager.getInstance().getSsoid()` → `ssoid`

账号中心的通用 OAuth token 实测不能替代上述健康 token。

## 附带工具所需目录

默认根目录为 `~/.local/share/astrbot-oppo-health/emulator`。工具不会替你创建下列 SDK、AVD、镜像或数据文件：

```text
emulator/
  sdk/platform-tools/adb
  sdk/emulator/emulator
  java-home.txt
  android-user/
  avd/
    AstrBot_OPPOHealth_user30.avd/
    AstrBot_OPPOHealth_user30.ini
    AstrBot_OPPOHealth_authcheck30v2.avd/
    AstrBot_OPPOHealth_authcheck30v2.ini
  auth-flat/userdata-flat.img
  auth-flat/encryption-flat.img
  downloads/frida-server-16.7.19-android-arm64
```

`java-home.txt` 填写 JDK 的绝对路径。AVD 的 `.ini` 和配置必须指向自己的实际目录、已安装的镜像，不能复制别人的 Mac 路径。原版 App 应安装到普通用户虚拟机，副本须包含本人登录后的数据。

官方工具和条款请从 [Android Studio](https://developer.android.com/studio) 获取；Frida server 从 [Frida 官方 releases](https://github.com/frida/frida/releases/tag/16.7.19) 获取并自行校验。此仓库不分发 APK、Android 镜像、SDK 或 Frida server。

## 运行助手

在插件目录创建独立环境并安装已测试的 Frida 客户端：

```sh
python3 -m venv .venv
.venv/bin/python3 -m pip install -r collector/requirements-emulator.txt
.venv/bin/python3 collector/run_auth_emulator.py
```

助手使用 ADB 服务端口 5038，专用虚拟机 console 端口 5578 / ADB 端口 5579，本地 Frida 转发端口 27742。端口已占用时会拒绝启动，不附着到未知设备。Frida server 仅监听虚拟机内 `127.0.0.1:27042`。

看到 `Dedicated health authentication assistant ready.` 后，在另一个终端运行：

```sh
.venv/bin/python3 collector/extract_emulator_auth.py
.venv/bin/python3 collector/snapshot.py
```

`status: saved` 表示提取并保存成功；摘要出现指标且没有错误才说明云端验证成功。如果要允许插件失效后重新读取，`collector_python` 填写此独立环境 Python 的绝对路径，再开启 `emulator_auth_refresh`。

助手需要持续运行才能支持失效后读取；本仓库不自动安装 macOS LaunchAgent。可用自己的进程管理器管理，日志只记录启动状态，不记录凭证。

## 仍然存在的限制

- 已测试 App 6.9.37 和 Android 30 arm64，getter 类名或登录流程可能改变。
- SDK 刷新调用不是永久免登录保证，真正长期过期后的续期尚未验证；需要重新登录时由本人完成，再重新制作/授权副本。
- 开发副本的数据与原虚拟机是分开的，原虚拟机后续登录状态更改不会自动复制到副本。
- 这是受限环境下的实验性方案，对不熟悉 Android Emulator 的使用者仍有部署门槛。
- 手机日常数据上传云端后，AstrBot 直接读云端；不依赖手机持续连接 Mac 的 USB 或同一 Wi-Fi。
