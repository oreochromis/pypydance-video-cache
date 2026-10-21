# VRChat 跳舞房助手 · Windows 版

<img src="assets/app-icon.png" width="96" alt="VRChat 跳舞房助手图标">

为 VRChat 的 PyPyDance / WannaDance 跳舞房提供本地视频缓存、队列预加载和永久曲库管理。**本软件仅支持 Windows x64。**

本 Fork 基于 [oreochromis/pypydance-video-cache](https://github.com/oreochromis/pypydance-video-cache) 继续开发，原作者为 **Bilibili @鲨鲨比亚Sharkspeare**。保留原有 Python 缓存核心与命令行入口，使用 Qt for Python 重构桌面界面，并增加日常使用功能。

**[下载 v3.0.1 正式版](https://github.com/Xiaoyu12-CPU/pypydance-video-cache/releases/tag/v3.0.1)** · **[直接下载 Windows x64 ZIP](https://github.com/Xiaoyu12-CPU/pypydance-video-cache/releases/download/v3.0.1/PyPyDanceCache-windows-x64.zip)**

## 下载与使用

1. 下载 `VRChatDanceAssistant-windows-x64.zip`，将整个 `VRChat跳舞房助手` 文件夹解压到当前账户可写的目录。
2. 双击 `VRChat跳舞房助手.exe`，确认 Windows 管理员授权。程序启动后会自动开启缓存服务，无需安装 Python。
3. 服务显示运行中后，进入 VRChat 的 PyPyDance 房间正常点歌。
4. 根据需要，在“存储”页设置永久曲库，在“设置”页调整预加载数量和 CDN，在“外观”页调整主题。
5. 使用结束后正常关闭窗口，程序会停止服务并尝试还原本程序添加的 hosts 设置。若提示恢复失败，请保留窗口和日志，按提示重试。

**必须保留 exe 旁的 `_internal` 等全部文件，不能只复制 exe。**默认放松视频、程序图标及运行依赖都在完整目录中。程序未签名，管理员授权窗口可能显示未知发布者。

如果升级后仍看到之前固定的 Python 图标，请退出旧版本并打开新版 exe；已固定的旧项可先取消固定，再从新版程序重新固定。

只想先看界面，可以双击 `打开演示.bat`。演示使用示例数据，不启动真实缓存服务、不修改 hosts，也不接入 VRChat。

## 相比 Fork 前更新了什么？

对比基线为原项目的 [V1.2 源码提交 `5efd712`](https://github.com/oreochromis/pypydance-video-cache/tree/5efd712073d696e180ffeedfbfe51afd293f702e)，当前版本为本 Fork 的 `v3.0.1`。

原版已经具备本地缓存、队列预加载、永久曲库、YouTube 源缓存、CDN 测速、曲库维护和 Cool Down。本 Fork 在这些能力上继续改进，主要变化如下：

| 项目 | Fork 前 | 当前版本 |
| --- | --- | --- |
| 桌面界面 | tkinter 单窗口，设置、队列、下载和日志集中展示 | Qt for Python 重构，分为“运行／存储／设置／外观”四页 |
| 外观设置 | 基础控件样式 | 默认浅蓝布局，支持深色、自定义主题、背景、透明度、缩放和密度，自动保存到本机 |
| CDN 优选 | 已有快速测速、扩大范围抽样测速及推荐 IP | 列表展示结果，可自行选择使用；新增本机常用 CDN 收藏，支持去重、保留和删除 |
| SteamVR 联动 | 手动打开程序 | 新增“跟随 SteamVR 启动”选项，保留管理员授权流程 |
| Cool Down | 已有功能，需要自行选择放松视频 | 随包提供默认视频，保留自选能力，并提供切回内置视频的入口 |
| 运行信息 | 已有歌曲队列、下载进度、速度和日志 | 分开呈现歌曲、下载及服务状态，补充失败、取消和恢复提示，支持复制、导出日志 |
| 设置保存 | 在原窗口中编辑并保存 | 草稿与应用分离，加强参数校验、保存失败提示及目录保护，避免测速操作顺带提交其他设置 |
| 程序图标 | 原有图标 | 新 PY 对话框图标，覆盖应用、窗口及 Windows exe，提供七种图标尺寸，并设置独立的 Windows 任务栏应用身份 |
| 构建与分发 | 原有单文件 exe 打包方式 | 完整目录 ZIP，附 SHA-256 与构建报告，发布前执行自动测试和实际打包程序检查 |

新版 GUI 使用 Qt、Pillow 和 OpenVR 等依赖，分发方式改为完整目录包；原有命令行缓存入口继续保留。

## 当前界面

下图使用程序的演示数据，默认采用浅蓝布局。

![运行页](docs/screenshots/run-light.png)

其他页面：[存储](docs/screenshots/storage.png) · [设置](docs/screenshots/settings.png) · [外观](docs/screenshots/appearance.png) · [深色界面](docs/screenshots/run-dark.png)

| 页面 | 主要用途 |
| --- | --- |
| 运行 | 查看歌曲队列、下载任务、缓存结果、服务状态、Cool Down 和日志 |
| 存储 | 设置临时缓存与永久曲库，管理保存规则，重建映射和检查视频大小 |
| 设置 | 调整预加载、YouTube 缓存、CDN 优选、默认放松视频和 SteamVR 联动 |
| 外观 | 调整浅色／深色主题、自定义配色、背景、透明度、缩放和密度 |

## 常用功能

### 本地缓存与永久曲库

视频请求命中本地缓存时，直接从硬盘提供；未命中时回源下载，并按配置尝试缓存。程序读取 VRChat 日志中的歌曲队列，提前准备可解析的 CDN 歌曲。

需要长期保留视频时，在“存储”页填写曲库目录，并开启对应来源的永久保存。符合条件的视频会按歌曲编号保存，例如 `3528.mp4`。改变曲库目录只影响后续保存位置，不会自动搬迁已有文件。

YouTube 源缓存依赖 VRChat 日志中已经解析出的直链，在播放时尝试后台保存；不提供独立的 YouTube 解析，也不会仅凭歌曲队列提前下载 YouTube 视频。

### CDN 测速与收藏

1. 打开“设置 → CDN 网络”，运行“快速测速”；需要更多候选时，可使用“扩大范围测速（抽样）”。
2. 在结果列表中选择地址，点击“使用选中的 IP”，再点击“应用更改”。
3. 点击“收藏选中的 IP”，即可加入本机“常用 CDN 收藏列表”。收藏重启后保留，可以选择使用或删除。

收藏操作独立保存，不会提交其他设置草稿。扩大范围测速采用抽样，不会穷举全部 CDN IP；收藏记录也不保证地址长期可用或始终最快，网络变慢时可重新测速。

### 跟随 SteamVR 启动

先打开 SteamVR，再在“设置 → 启动”中勾选“跟随 SteamVR 启动”，点击“应用启动设置”。之后启动 SteamVR 时会打开本程序，启动时仍需确认管理员授权。

取消勾选并应用即可关闭此功能。关闭 SteamVR 不会关闭本程序；移动程序目录后，请先关闭该选项并应用，再重新启用，以更新启动路径。

### Cool Down 放松视频

程序内置一段约 **3 分 16 秒**的默认放松视频，使用用户提供的原文件，未压缩或降低音频码率。

- 默认使用内置视频，也可以在“设置”页选择其他本地 MP4。
- 已有自选视频设置会保留。需要切回默认时，点击“使用内置视频”并应用更改。
- 在“运行”页手动启用 Cool Down 后进入待命，在符合条件的后续视频请求中替换本机画面。
- 等待替换时可以取消，正在替换时可以退出。恢复由后续请求判断，没有精确的播放结束倒计时。

Cool Down 只影响本机，不控制房间播放或其他玩家的画面。

### 设置与外观保存

“存储”和“设置”页需要点击“应用更改”；“外观”页即时预览并自动保存。修改保存目录、YouTube 缓存开关前，请先停止服务；修改目录还需等待活动下载和曲库任务结束。应用后再启动服务。

正式版内置浅色和深色主题，不附带个人主题或大幅背景图片。升级保留本机已有外观配置和导入图片；若旧主题仅引用测试包中的背景而本机没有副本，会回退到主题底色，可以重新导入背景。

## 升级与数据位置

升级前正常退出程序，备份 `config.json`、`mapping.json` 和视频目录。将新版完整解压到独立目录，不要把新旧 `_internal` 混在一起；迁移配置时注意旧相对路径所指向的位置。

| 数据 | 保存位置 |
| --- | --- |
| `config.json`、`mapping.json` | 与 exe 同级；源码运行时在项目目录 |
| 临时缓存、永久曲库 | 由业务配置中的目录决定 |
| 外观、个人背景、CDN 收藏、SteamVR 注册文件 | 当前 Windows 用户的应用数据目录 |
| 演示外观与收藏 | 默认使用临时目录；显式指定 `--state-dir` 时保存在该目录 |

更换程序目录不会自动搬迁曲库。只备份 exe 也不会包含配置、视频或个人外观数据。

## 使用注意

- 需要管理员权限，程序会接管相关视频域名，并使用本地 80／443 端口；443 端口不可用时，相关能力可能降级。
- 不要与 VRCDancePreloader 等接管相同域名的工具同时运行。
- 使用代理时，确保 `127.0.0.1` 的本地流量可以正常到达本机服务。
- 曲库大小检查用于报告差异，不是视频内容哈希验证，不会自动修复或重新下载。
- 强制结束进程、断电等异常退出可能需要额外检查 hosts；遇到问题请保留日志和构建报告。
- 网络测速和缓存无法保证所有来源、运营商及网络环境都达到相同速度。

## Windows 源码运行与打包

需要 **Python 3.10 或更新版本**。建议使用独立虚拟环境，在项目目录的 PowerShell 中执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe dance_cache_gui.py
```

`py -3.12` 是示例，可使用依赖支持的其他 Python 版本。正常入口会请求所需权限并自动启动缓存服务；如需先查看真实配置，使用 `--no-autostart`。安全查看示例界面则使用 `--demo`。

```powershell
# 演示界面，不启动真实服务
.\.venv\Scripts\python.exe dance_cache_gui.py --demo

# 原有命令行入口，仅使用 Python 标准库
.\.venv\Scripts\python.exe dance_cache.py

# 安装开发依赖、运行测试并生成 Windows 程序
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\build_windows.ps1 -Python .\.venv\Scripts\python.exe
```

构建产物位于 `dist\VRChat跳舞房助手\`，分发时保留整个目录。也可使用 `启动.bat`，它会优先打开已构建的 exe，否则使用 Python 运行源码。

## 验证与问题反馈

Windows 发布构建会执行完整自动化测试，并实际启动打包后的演示程序，检查界面、默认视频、exe 内嵌图标，以及 Windows 应用身份和原生窗口图标。实际测试数量及结果见下载附件和包内 `docs/build-info.json`，它们记录对应提交和验证结果。

用户已反馈此前版本在 Windows + VRChat 中正常工作。自动化检查与用户反馈分别记录，不代表所有网络、视频源、旧曲库迁移和 SteamVR 联动场景都已逐项验收。

反馈问题时，请附上版本号、操作步骤、错误提示、运行日志、网络或代理情况，以及是否使用旧配置或旧曲库。

## 来源与许可

- 原作者：Bilibili **@鲨鲨比亚Sharkspeare**。
- 上游项目：[oreochromis/pypydance-video-cache](https://github.com/oreochromis/pypydance-video-cache)。
- 本 Fork：[Xiaoyu12-CPU/pypydance-video-cache](https://github.com/Xiaoyu12-CPU/pypydance-video-cache)。
- 原理参考：[加载失败分析](https://github.com/kiyonya/why-my-dance-video-cannot-play)、[VRCDancePreloader](https://github.com/wzhqwq/VRCDancePreloader)。

项目代码沿用 [MIT License](LICENSE)，保留原作者版权与署名。Qt for Python、Pillow、OpenVR 等依赖使用各自许可，Windows 分发包内的 `THIRD_PARTY_LICENSES` 提供相应声明。
