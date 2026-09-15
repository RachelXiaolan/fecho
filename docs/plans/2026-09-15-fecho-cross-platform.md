# Fecho 本机采集适配 Windows / Linux

分支：`cross-platform`（从 `cloud` 拉出，**不直接合进 cloud**：线上网站和同事装机脚本都来自 cloud）

## 现状：只能 Mac 用的地方

按框架看，服务器那一侧（② 归属 ③ 存储 ④ 整理 ⑤ 交付）跟操作系统无关，lu2 本身就是 Linux。
卡住的全在 **① 采集**——同事电脑上的本机采集脚本 `fecho/presets/local/fecho_local.py`，
加上服务器对路径的判断。

| # | 问题 | 后果 |
|---|---|---|
| 1 | 定时任务只写了 macOS launchd | Windows / Linux 装完没有定时任务，每晚不会扫 |
| 2 | 路径必须以 `/` 开头（本机脚本上报文件夹、服务器收文件夹、白名单匹配、网页手动添加） | **Windows 的 `C:\Users\...` 全被当成无效丢掉：文件夹报不上来，永远不扫** |
| 3 | 找 agent 命令行只找 Mac 的位置 | Linux / Windows 上可能找不到 claude、codex |
| 4 | 调 agent 用系统默认编码 | Windows 中文系统默认 GBK，中文提示词传进去会报错或乱码 |
| 5 | 系统通知用 osascript | 别的系统弹不出日报失败的提醒 |
| 6 | 日志判断 `sys.stdout.isatty()` | Windows 用无窗口 Python 跑定时任务时 `sys.stdout` 是 None，直接崩 |
| 7 | 安装说明和指南只有 bash 命令、写着「只支持 Mac」 | 同事不知道怎么装 |

## 方案

### 定时任务（问题 1）

| 系统 | 用什么 | 为什么 |
|---|---|---|
| macOS | launchd（不变） | — |
| Linux | 优先 systemd 用户定时器（`OnCalendar=*:0/15` + `Persistent=true`）；没有就退回 cron | 主流桌面发行版都有 systemd；`Persistent` 让合盖错过的那次开机后补跑 |
| Windows | 任务计划程序（`schtasks`，每 15 分钟），用 `pythonw.exe` 跑 | 系统自带；`pythonw` 不弹黑色命令行窗口 |

三种都做成「先生成内容（纯函数）→ 再写入系统」，生成内容的部分能单测、也能在 lu2 的临时目录里真跑。
错过扫描时间（电脑合着）的补救不靠定时器：服务器的「该扫了吗」本来就会让第二天早上先补昨天。

### 路径（问题 2）

- 统一成正斜杠：`C:\Users\a\work` → `C:/Users/a/work`
- 「绝对路径」= 以 `/` 开头，或以盘符开头（`C:/`）
- Windows 路径（带盘符）比较时不分大小写；Mac / Linux 保持原样
- 本机脚本和服务器 `cloudscan.py` 用同一套规则，测试拿同一批样例对两边结果
- 已有的 Mac 路径没有反斜杠，规范化后不变，不用迁移数据

### 其余

- 找命令行：先 `shutil.which`（Windows 会带上 `.cmd` / `.exe`），再找 Linux 常见位置和 Windows 上 npm 全局目录
- 调 agent：`encoding="utf-8"`；Windows 上加 `CREATE_NO_WINDOW`
- 通知：Linux `notify-send`；Windows 托盘气泡。**通知文字一律通过参数 / 环境变量传，不拼进命令**
- 日志：`sys.stdout` 为 None 时不打印
- 安装说明（给 agent 看的 install.md）：按系统给命令；Windows 没装 Python 时停下来问用户，不替用户装软件
- 接入指南和 ONBOARDING.md：条件改成 macOS / Windows / Linux，标明后两者还没真机实测
- 网页手动添加文件夹：也接受 `C:\...`

## 不在这次范围

- 本机版 Fecho（`automation.py` 的 launchd 那套）：已经被云端版取代，不适配
- Hermes 后台扫描：和系统无关，另说

## 验证

- 单测：路径规则两边一致；三种系统的定时任务生成内容、安装、卸载、状态检查（模拟 `sys.platform` 和系统命令）；
  UTF-8、无 stdout、通知不拼命令
- SQLite / Postgres 全量测试
- 脚本仍然只用标准库、能在 Python 3.9 上跑
- **Linux 半真机**：在 lu2 的临时目录里跑判断定时方式、读对话记录、路径匹配、生成 systemd / cron 内容——不安装、不改 lu2 的系统配置
- **Windows：没有真机，只有模拟测试。** 合并前需要找一台 Windows 按接入指南实装一次
