# OpencodeMonitor

> 一个开源的 OpenCode Go 套餐用量监控桌面磁贴 —— 无边框、置顶、可鼠标穿透的悬浮小部件，实时展示三个时间窗口的用量。

![Version](https://img.shields.io/badge/version-1.1.1-blue)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-blue)
![Tech](https://img.shields.io/badge/tech-Python%20%2F%20pywebview%20%2F%20pystray-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## 简介

OpencodeMonitor 是运行在 Windows 桌面上的一枚半透明磁贴，通过 OpenCode 官方用量接口
`GET https://opencode.ai/zen/go/v1/usage` 拉取 **OpenCode Go** 订阅计划的配额用量，
并用三个环形图实时展示：

- **5 小时（rolling）** 滚动窗口用量
- **本周（weekly）** 自然周用量
- **本月（monthly）** 订阅周期用量

每个环同时给出中心百分比、重置倒计时和按官方配额换算的已用美元金额（5 小时 = $12、每周 = $30、每月 = $60）。

## 功能特性

- 🎨 **三环形用量图**：5 小时 / 本周 / 本月百分比 + 重置倒计时 + 已用美元（`已用 $X.XX / $Y`）
- 🖱️ **鼠标穿透 / 置顶**：可一键切换，作为悬浮磁贴不挡操作
- ⚙️ **主题自定义**：背景透明度、背景色、强调色，实时预览并持久化
- 🔔 **托盘常驻**：窗口不进任务栏与 Alt+Tab，仅通过托盘图标操作（显示窗口 / 置顶 / 穿透 / 退出）
- 📍 **位置记忆**：退出时记住窗口位置，下次启动原位恢复
- ⏱️ **响应耗时**：底部实时显示每次接口请求的毫秒耗时
- 🔑 **内嵌 API Key 配置**：未配置或 Key 无效时自动弹出填写面板，保存后立即重连
- 📦 **一键安装 / 覆盖升级**：Inno Setup 安装包，重装保留 config 与 API Key

## 截图

![主界面](docs/screenshots/main.png)

## 安装

从 [Releases](../../releases) 下载最新安装包 `OpencodeMonitor-Setup-<版本号>.exe`，双击安装即可。

> 安装包会自动定位已安装的旧版本目录并原地覆盖升级，`config.json` 与 API Key 会被保留。

## 使用

1. 启动后，如果尚未配置 API Key，磁贴会弹出填写面板。
2. 填入你的 OpenCode Go API Key（`sk-...`），点击 **保存并连接**。
3. 磁贴开始每 60 秒刷新一次用量数据（可在 `config.json` 调整刷新间隔）。

托盘图标右键菜单：

| 菜单项 | 说明 |
| ------ | ---- |
| 显示窗口 | 重新显示磁贴 |
| 置顶 | 切换窗口始终置顶 |
| 鼠标穿透 | 切换点击穿透（穿透后可通过托盘菜单恢复） |
| 退出 | 退出程序 |

磁贴右上角 **✕** 按钮是最小化到托盘（并非退出）。

## 配置

程序在安装目录下生成 `config.json`，所有可选项：

| 键 | 类型 | 默认值 | 说明 |
| --- | ---- | ------ | ---- |
| `api_key` | string | `""` | OpenCode Go API Key |
| `refresh_seconds` | int | `60` | 用量刷新间隔（秒） |
| `bg_color` | string | `"#1e2330"` | 卡片背景色 |
| `bg_opacity` | number | `1.0` | 背景不透明度 0~1（低于 0.6 时自动为文字垫底色） |
| `accent_color` | string | `"#5b9bff"` | 强调色（环形图、状态圆点） |
| `window_x` / `window_y` | int\|null | `null` | 上次退出时窗口位置（物理像素） |
| `topmost` | bool | `true` | 启动时是否置顶 |
| `click_through` | bool | `true` | 启动时是否鼠标穿透 |

> 背景透明度为 0 时，卡片完全透明、只显示文字与图形；为了让文字依旧清晰，
> 程序会为所有带文字的元素自动垫上一层半透明深色底色（圆角矩形 / 圆形），不遮挡下层内容。

## 从源码构建

环境要求：Python 3.12、[PyInstaller](https://pyinstaller.org/)、[Inno Setup 7](https://jrsoftware.org/isinfo.php)。

```bash
# 1. 安装依赖
pip install pywebview pystray pillow

# 2. 源码运行（开发调试）
python main.py

# 3. 打包 exe（PyInstaller）
python -m PyInstaller --noconfirm --clean OpencodeMonitor.spec

# 4. 生成安装包（Inno Setup）
"/c/Program Files/Inno Setup 7/ISCC.exe" installer.iss
```

构建产物：
- `dist/OpencodeMonitor/` —— 绿色免安装目录（`OpencodeMonitor.exe` + `_internal/`）
- `installer/OpencodeMonitor-Setup-<版本号>.exe` —— 安装包

> 升级版本号：改 `installer.iss` 的 `#define MyAppVersion` 与 `index.html` 的 `VERSION` 常量即可，
> 安装包文件名会自动带上版本号。

## 技术栈

| 组件 | 用途 |
| ---- | ---- |
| [pywebview](https://pywebview.flowrl.com/)（WebView2 后端） | 无边框透明窗口 + 前端渲染 |
| [pystray](https://pystray.readthedocs.io/) | 系统托盘图标与菜单 |
| [PIL/Pillow](https://python-pillow.org/) | 托盘图标绘制（代码自绘，非网络素材） |
| PyInstaller | Python 打包为独立 exe |
| Inno Setup | 安装 / 卸载 / 覆盖升级 |

## 项目结构

```
OpencodeMonitor/
├── main.py                 # 主程序：数据抓取、窗口管理、托盘、JS 桥接
├── index.html              # 前端界面：三环图、配置面板、主题
├── OpencodeMonitor.spec    # PyInstaller 打包配置
├── installer.iss           # Inno Setup 安装脚本
├── icon.ico                # 应用/窗口图标（与托盘图标同款自绘）
├── config.json             # 用户配置（安装时生成，含 API Key，勿提交）
├── docs/                   # 文档与截图
├── dist/                   # 打包产物
└── installer/              # 安装包输出
```

## 数据来源与口径

用量数据来自 OpenCode 官方接口 `GET https://opencode.ai/zen/go/v1/usage`，认证需要同时携带
`Authorization: Bearer <key>` 与 `x-api-key: <key>` 两个请求头（详见 [docs/adr/0001-use-official-usage-api.md](docs/adr/0001-use-official-usage-api.md)）。
接口只返回 0–100 的整数百分比，因此"已用美元"按官方配额换算：**5 小时 = $12、每周 = $30、每月 = $60**。
这与官网仪表盘口径一致，不读取本地代理日志。

## 许可证

本项目基于 [MIT License](LICENSE) 开源。
