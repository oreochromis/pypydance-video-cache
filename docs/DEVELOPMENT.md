# UI 重构开发说明

## 范围与来源

开发基线是 [oreochromis/pypydance-video-cache](https://github.com/oreochromis/pypydance-video-cache) 的提交 `5efd712073d696e180ffeedfbfe51afd293f702e`，原作者为 Bilibili @鲨鲨比亚Sharkspeare。当前 fork 为 [Xiaoyu12-CPU/pypydance-video-cache](https://github.com/Xiaoyu12-CPU/pypydance-video-cache)。

本轮把界面整理成「运行／存储／设置／外观」四页。`dance_cache.py` 继续负责缓存、网络、队列观察、维护与命令行流程；允许补充 UI 所需的状态与结果，但不重写后端。`config.json` 和 `mapping.json` 保持原有数据与路径兼容性。

## 开发环境

- GUI：Python 3.10+、`PySide6-Essentials==6.12.0`、`Pillow>=10.4,<13`。
- Windows 打包：PyInstaller 6.x，依赖见 `requirements-dev.txt`。
- 自动化测试：Python 内置 `unittest`，无需 pytest。
- 真实服务：Windows、管理员权限、可用的本地端口和 VRChat 日志。
- 非 Windows：用于隔离逻辑、Qt 演示和布局检查，不能代替真实服务验证。

`PySide6-Essentials` 包含本界面所需的 QtCore、QtGui、QtWidgets，不需要完整的 Qt Addons。使用独立虚拟环境，避免混入其他 Qt Python 绑定。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe dance_cache_gui.py --demo
```

`py -3.12` 是示例，可换为已安装、依赖支持的 Python 3.10+ 版本。依赖升级后重新检查界面、图片处理、配置保存和 Windows 打包。

## 启动模式

| 入口 | 数据与行为 | 用途 |
| --- | --- | --- |
| `dance_cache_gui.py --demo` | 示例业务数据；不启动服务、不修改 hosts、不接入 VRChat；默认使用临时外观状态 | 看界面、截图、交互检查 |
| `dance_cache_gui.py --demo --state-dir PATH` | 同上，但使用并保留指定目录的外观状态 | 检查主题跨启动保存 |
| `dance_cache_gui.py --no-autostart` | 使用真实业务配置，用户仍可手动启动服务 | 先检查真实配置 |
| `dance_cache_gui.py` | 正常模式，自动启动真实服务 | Windows 日常使用 |
| `dance_cache.py` | 原有标准库命令行入口 | CLI 兼容 |

`--state-dir` **只覆盖界面本机状态目录（外观与 CDN 收藏）**，不能沙箱化真实服务或重定向业务配置。隔离检查使用 `--demo` 或测试替身，不把 `--no-autostart` 当作演示模式。

## 配置与资源位置

### 业务数据

- 源码运行时，`config.json` 与 `mapping.json` 在源码旁。
- 打包运行时，两者在 exe 旁，不放进 `_internal`。
- 临时缓存和曲库从业务配置读取，沿用原有相对路径规则。

移动旧数据前保留配置、映射和视频目录的备份，确认相对路径在新位置仍有效。界面改版不会自动搬迁、合并或清理旧曲库。

### 外观数据

默认目录来自 Qt：

```text
QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation) / appearance
```

应用标识：应用 `VRChatDanceAssistant`（任务栏 AppUserModelID `Sharkspeare.VRChatDanceAssistant`）。以 Qt 在当前系统返回的实际路径为准，不硬编码用户名或盘符。

主题存储包含 `appearance.json` 与 `backgrounds/`。个人导入背景复制到此目录，原图不修改；随附背景从程序资源加载，所有副本均缺失时回退主题底色。可编辑主题不能写入 PyInstaller 的 `_internal` 或解包资源目录。

`--demo` 未指定状态目录时，使用临时目录并在正常退出后清理。需要复查保存结果时传入专用目录，例如 `.demo-state`，避免与真实用户数据共用。

### 跟随 SteamVR 启动

Windows 使用 `openvr==2.12.1401` 的公开应用接口；依赖只在 Windows 安装。通过 `VRApplication_Background` 短连接查询或注册，读取设置不会主动启动 SteamVR，因此首次设置和刷新时需先运行 SteamVR。固定应用标识为 `sharkspeare.vrchatdanceassistant`，manifest 放在本机状态目录下的 `steamvr/vrchatdance.vrmanifest`，不分发用户注册文件。

`AddApplicationManifest` 使用非临时注册，`SetApplicationAutoLaunch` 写入 SteamVR，回读确认后界面才报告成功；不在业务 config 中另存一个可能过时的开关。关闭只取消本应用的自动启动标志。注册失败尝试恢复原 manifest 和自动启动标志，恢复失败明确提示刷新检查。

SteamVR 的自动启动接口要求 manifest 标记 `is_dashboard_overlay=true`，本助手仍是桌面伴随程序，不提交 VR 场景。现有 exe 需要管理员权限，manifest 通过系统 Windows PowerShell 的 `Start-Process -Verb RunAs` 启动并保留 UAC 确认；Unicode 路径使用 UTF-16 EncodedCommand，路径与参数做独立引号处理，不修改执行策略。正常 Windows 窗口持有当前会话命名 mutex；自动入口先检查已有窗口进程，避免重复授权和重复服务。演示模式不注册、不连接 SteamVR、不占用正常实例锁。

“启动”卡片独立应用，不提交其他设置草稿；读取失败显示未知状态，保存失败保留草稿。SteamVR 关闭不退出助手。移动/升级到其他程序目录后，请先关闭该选项并应用，再重新启用以刷新启动路径。实际 SteamVR 启动、中文路径和 UAC 点击仍需在用户 Windows 机器验收。

API 依据：[Valve OpenVR 接口定义](https://github.com/ValveSoftware/openvr/blob/0924064316de3effbcd1acf1e309182a2deb1c05/headers/openvr.h)、[pyopenvr 固定版本](https://github.com/cmbruns/pyopenvr/tree/2.12.1401)。

### SteamVR 面板页

`dance_ui/vr_overlay.py` 以 `VRApplication_Overlay` 长连接注册 dashboard overlay（`sharkspeare.vrchatdanceassistant.panel`），出现在 SteamVR 菜单中。纹理用 Pillow 绘制后经 `SetOverlayRaw` 提交 RGBA 字节，不依赖 OpenGL 上下文；缩略图用 `SetOverlayFromFile` 指向 `assets/app-icon.png`。输入走 `VROverlayInputMethod_Mouse`，控制器激光映射为鼠标事件（坐标 0-1，y 原点在下），按钮命中测试在渲染坐标系完成，动作经 Qt 信号排队回 GUI 线程。SteamVR 未运行时 15 秒间隔静默重试，断连自动重连；overlay 会话存活期间“跟随 SteamVR 启动”的短连接复用同一会话。面板内容 1 秒刷新一次。实际显示效果与激光交互只能在 VR 环境验收。

### 常用 CDN 收藏

`cdn-favorites.json` 与 `appearance.json` 放在同一个本机状态目录，保存去重后的公网 IPv4 地址列表，不随程序发布。测速结果按速度排序并支持收藏；收藏可再次选择使用或删除。收藏与业务设置分开原子保存，收藏操作不提交其他草稿，使用收藏只填入优选 IP，需“应用更改”后生效。历史收藏不代表当前可用性或速度。演示默认使用临时目录；传入 `--state-dir` 可验证收藏跨启动保留。

### 程序图标（3.0.0）

`assets/app-icon.png` 是用户确认的透明背景黑白 PY 对话框图标，原 PNG 保持不变；`assets/app-icon.ico` 从该图导出 16、24、32、48、64、128、256 像素七种尺寸，用于 Windows exe 原生图标。Qt 全局应用图标和窗口图标使用同一 PNG，打包时同时保留 PNG 与 ICO 资源。

Windows 构建脚本与 spec 均将 ICO 嵌入 exe。冻结检查不仅核对包内文件，还读取 exe 的 PE 图标资源，比较其图像帧与批准的 ICO，防止只替换窗口图标却遗漏资源管理器图标。资源大小、SHA-256 和检查结果随构建报告发布。

### 内置 CoolDown 视频（2.0.1）

`assets/default_cooldown.mp4` 使用用户提供的原文件，未重新编码或降低音频码率。文件为 H.264/AAC、640×360、196.062 秒、5,073,920 字节（约 4.84 MiB）；SHA-256 为 `453650779b315339a1c68b29a4b486d9fe0c85b49b3c8e0a5af94ff770d7b135`。

源码资源按核心模块 `__file__` 的目录定位；Windows 包收集到 `_internal/assets/default_cooldown.mp4`。配置 `cooldown_file` 缺省或留空表示使用内置视频，非空则保留用户自选；自选失效时不静默替换。配置不写入安装绝对路径，资源只读，HTTP 提供内置视频时不更新其访问标记。恢复内置视频只修改设置草稿，应用后生效；仍需手动启用 CoolDown 才进入待命。

打包前、冻结演示、ZIP 校验及发布均核对原片大小、MP4 标记和 SHA-256，并写入构建报告。单文件发布上限按 5 MiB（5,242,880 字节）检查；本次用户明确要求使用原片。自动化检查不代替新版内置视频在 VRChat 中的实际播放验收。

### 正式版主题与升级

2.0.0 正式版只内置 `light` / `dark` 基础主题，新用户默认浅蓝布局。不分发测试版“小余12主题”的快照或原背景，因此不再把 `dance_ui/presets` 加入 Windows 构建。背景导入和自定义主题功能继续可用。

用户已有外观文件保留其主题、当前选择和阅读偏好；原随附主题若已写入个人配置，继续作为个人主题使用。原背景在本机已有导入副本时仍可加载；若仅引用旧包资源而本机没有副本，则回退主题底色。更新程序不会删除个人外观目录。

## 页面职责与状态约束

### 运行

来源、缓存状态与播放状态分别表达：

- 队列项不等于活动下载任务；真实任务开始后才能显示正在下载。
- 队列无法提供准确播放时长或剩余时间。
- 总量未知的下载不显示虚构百分比。
- 失败结果应保留，允许查看原因。
- 不显示核心尚不支持的单项暂停、拖拽改队列或远程播放控制。

Cool Down 表达已配置、待命、已触发等阶段，只替换本机画面。结束依赖后续视频请求，不能用模拟倒计时表示播放完成。

### 存储

临时缓存与永久曲库分别展示。容量标签对应实际统计范围，不能把已统计的 `.mp4` 文件大小称为整个磁盘的使用量。

永久保存需要曲库目录和对应来源开关。改变目录只影响后续保存位置。重建映射补齐编号关系；大小检查报告差异，不自动修复或批量重新下载。

### 设置

业务设置使用草稿与“应用更改”流程，校验全部值后再提交，避免只保存部分字段。修改 YouTube 缓存开关前先停止服务，应用后再次启动；修改保存目录还需等待活动下载和曲库任务结束。测速及采用推荐 IP 不应意外提交其他草稿字段；扩大范围测速保留“抽样”说明。

### 外观

外观即时预览并自动保存，不复用业务配置的应用状态。主题、背景、缩放和密度只触及外观数据。

背景支持 PNG/JPG/JPEG/WEBP；导入检查真实格式、文件大小和像素数，不只信任扩展名。当前上限为 20 MiB、40,000,000 像素、单边 16,384 像素；JPG/JPEG 的副本统一使用 `.jpg` 后缀。副本可由多个主题共享，删除主题不会删除原图或共享副本。当前不提供主题文件导入/导出。

新增控件需真实影响渲染、可保存并恢复，不能仅在表单中记住无视觉效果的值。

## Windows 打包

在原生 Windows Python 环境执行，不能使用 WSL Python：

```powershell
.\build_windows.ps1 -Python .\.venv\Scripts\python.exe -InstallDependencies
```

不指定 Python 时，脚本优先使用项目内 `.venv\Scripts\python.exe`，否则使用 PATH 中的 `python`。它验证平台和 Python 版本，使用如下打包形式：

```powershell
python -m PyInstaller --clean --noconfirm --onedir --windowed --name VRChat跳舞房助手 --uac-admin dance_cache_gui.py
```

实际脚本将生成物限定在项目 `build` 与 `dist`，并复制 README、MIT 许可、开发说明和可发现的第三方声明。仓库原有的 `VRChat PyPyDance助手.spec` 已更替为 `VRChat跳舞房助手` 的 onedir 产物；推荐使用脚本统一收集随包文档。

### 分发约束

- 启动文件：`dist\VRChat跳舞房助手\VRChat跳舞房助手.exe`。
- 分发完整目录，包括 `_internal`，不能单独复制 exe。
- `--uac-admin` 会让打包版每次启动请求提权，包括 `--demo`。从源码运行演示无需因为缓存服务申请提权。
- 不打包“小余12主题”、个人背景、配置或视频。发布验证会拒绝意外夹带的预置主题资源。
- 构建脚本不签名、不上传、不创建 Release 或 PR。
- `--clean --noconfirm` 会替换同名构建产物，不要把正式配置或个人视频保存在构建输出目录中。

构建完成后，需要真实启动产物，确认 Qt 平台插件、中文文字、文件对话框、图片解码及关闭流程。构建成功不代表真实缓存服务已经验收。

### Windows 构建、正式发布与冻结程序检查

`.github/workflows/windows-preview.yml` 在 `ui-redesign` 或 `main` 推送后使用原生 Windows x64 / Python 3.12 构建。完整隔离测试必须零失败、零错误、零跳过，随后运行打包后的 exe：

```powershell
.\dist\VRChat跳舞房助手\VRChat跳舞房助手.exe --demo --state-dir TEMP_PATH --smoke-test-report REPORT_PATH
```

该自动检查参数仅与 `--demo` 一起使用，检查四页导航、SVG 资源、默认浅蓝主题及使用临时合成图片的 Pillow 到 Qt 背景渲染，写 JSON 后自动退出。报告必须确认 `win32`、Qt `windows` 平台和 `frozen=true`，才能由 `tools/package_windows.py` 生成完整目录 ZIP、SHA-256 及构建信息。实际报告也保存在包内 `docs/build-info.json`。

普通推送只构建。只有本 Fork 的 `main` 分支 push 且提交消息包含 `[publish-release]` 时才发布正式版。`tools/publish_windows_preview.py` 保留历史文件名，在上传前后核对版本、测试、源码提交、ZIP 和远程附件 SHA，恢复同版本草稿后发布 `v3.0.1`，设为非预发行版及最新版本；不覆盖已发布 Release 或冲突标签。

冻结演示检查会加载打包的 Windows x64 OpenVR DLL，但不初始化或注册 SteamVR；只收集本机平台的 64 位 DLL 与许可。冻结演示检查仍使用示例数据，不验证 UAC 人工点击、真实服务、hosts 恢复、旧曲库兼容或 VRChat 请求。用户试用说明见 [WINDOWS_PREVIEW.md](WINDOWS_PREVIEW.md)。

## 验证范围

项目测试使用：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

有价值的检查包括：主题持久化、损坏数据回退、图片范围校验、状态转换、配置校验及演示隔离。涉及服务的测试替换网络、hosts、线程入口和系统操作，不能为验证按钮启动真实服务。

无显示服务的环境可用 `QT_QPA_PLATFORM=offscreen` 做 Qt 渲染检查。这不能代替 Windows 原生对话框、中文字体、125%/150% 缩放、UAC、服务启动/关闭及 VRChat 实际请求测试。

验证记录需区分隔离测试、Windows 打包/交互、VRChat 视频请求三个层次。**发布前用户已反馈在 Windows 运行 VRChat 实测正常；开发代理未在本机执行该实机测试**；示例画面或离屏测试不能描述为真实服务测试通过。

## 作者与许可

保留原作者署名、上游来源和项目 [MIT 许可](../LICENSE)。Qt for Python、Pillow 和打包工具采用各自的许可；构建脚本把已安装发行包中可发现的许可/声明文件放进 `THIRD_PARTY_LICENSES`，发布时保留与实际依赖版本一致的材料。

### Windows 任务栏身份（3.0.1）

入口在创建 QApplication 或任何窗口之前设置稳定的 AppUserModelID `Sharkspeare.VRChatDanceAssistant`，避免系统沿用 Python 宿主的任务栏关联；不改变 Qt 配置组织和应用名称，不自动修改用户固定项。

冻结检查读回进程身份，并通过 WM_GETICON 获取原生 BIG/SMALL HICON，分别在黑白底绘制，与同尺寸的 PY 图标逐像素比较。此检查不同于 Qt windowIcon 属性或 exe 内嵌资源校验，不表示已读取或刷新用户的 Explorer 缓存。已有旧固定项可由用户取消固定后重新固定新版 exe。
