# 安装 Fecho（这份是写给 agent 看的）

你正在帮用户接入 Fecho：一个替用户记工作进展、每天自动出日报的服务。
按下面的步骤做，**每一步做完都告诉用户结果**。不需要安装任何软件包，也不需要任何 LLM 密钥——
唯一的凭证是用户登录后拿到的 token。

服务地址：__URL__

**先确认用户电脑是什么系统**（macOS、Linux 还是 Windows），下面有几步按系统给了不同的命令。
Windows 上的命令都在 **PowerShell** 里运行。

---

## 第 1 步：让用户登录

在用户电脑上打开浏览器：

| 系统 | 命令 |
|---|---|
| macOS | `open __URL__/onboard` |
| Linux | `xdg-open __URL__/onboard` |
| Windows | `Start-Process __URL__/onboard` |

告诉用户：

> 浏览器打开了 Fecho，请用 Mobius 账号登录（只接受 @__DOMAIN__ 邮箱）。
> 登录后页面上点「生成 token」，把那串 `fecho_` 开头的字符复制发给我。
> 页面上还可以顺便选每天几点出日报。

**等用户把 token 发过来再继续。** token 连续 7 天不用才会失效，天天在用就一直有效。

---

## 第 2 步：把 Fecho 接到你自己身上（随手记用）

按你是哪个 agent，选一段做。把 `<TOKEN>` 换成用户给你的那串。

### 你是 Claude Code

三个系统命令一样：

```bash
claude mcp add --transport http --scope user fecho __URL__/mcp --header "Authorization: Bearer <TOKEN>"
```

如果提示 `fecho` 已经存在，说明用户以前装过本机版 Fecho。**先停下来告诉用户**，
得到同意后再执行 `claude mcp remove fecho --scope user`，然后重新执行上面那条。

### 你是 Codex

在用户主目录下的 `.codex/config.toml` 末尾追加（macOS / Linux 是 `~/.codex/config.toml`，
Windows 是 `%USERPROFILE%\.codex\config.toml`）。已有 `[mcp_servers.fecho]` 先告诉用户，同意后再替换：

```toml
[mcp_servers.fecho]
type = "http"
url = "__URL__/mcp"

[mcp_servers.fecho.http_headers]
Authorization = "Bearer <TOKEN>"
```

### 你是 Hermes

远程接入的写法**还没实测过**。请告诉用户这一点，并请用户联系 Fecho 管理员。

---

## 第 3 步：装本机采集（每晚扫描用）

用户不说「记一下」的时候，agent 常常会漏记。本机采集就是补漏的：每天出日报前 15 分钟，
**每个 agent 扫自己的对话**——Claude Code 的交给 claude 命令提炼，Codex 的交给 codex 命令提炼——
只扫用户勾选的工作文件夹，提炼成进展后传上去，服务器再整理成日报。

只需要 Python 3.9 以上，不装任何包。

### macOS / Linux

```bash
mkdir -p ~/.fecho-cloud && curl -fsSL __URL__/local/fecho_local.py -o ~/.fecho-cloud/fecho_local.py && python3 ~/.fecho-cloud/fecho_local.py install --url __URL__ --token <TOKEN>
```

### Windows（PowerShell）

先确认有 Python：运行 `py -3 --version`，不行再试 `python --version`。
**两个都没有的话，停下来告诉用户**：需要先装 Python 3.9 以上（比如 `winget install -e --id Python.Python.3.12`，
或从 python.org 下载）。得到用户同意再装，不要自己替用户装软件。

有 Python 之后（下面用 `py -3`；如果只有 `python` 就换成 `python`）：

```powershell
New-Item -ItemType Directory -Force "$HOME\.fecho-cloud" | Out-Null; Invoke-WebRequest "__URL__/local/fecho_local.py" -OutFile "$HOME\.fecho-cloud\fecho_local.py"; py -3 "$HOME\.fecho-cloud\fecho_local.py" install --url __URL__ --token <TOKEN>
```

### 它会做什么

确认 token 能用 → 找出这台电脑上用过的 agent，**逐个在后台试叫醒一次** →
找出用户用 agent 干过活的文件夹（**只读路径，不读对话内容**）并上报 →
装一个每 15 分钟问一次服务器「该扫了吗」的定时任务：

| 系统 | 定时任务 |
|---|---|
| macOS | launchd |
| Linux | systemd 用户定时器；没有 systemd 就用 cron |
| Windows | 任务计划程序里的 `FechoCloudCheck`（用户登录时运行，不弹窗口） |

试叫醒这一步每个 agent 会花掉一次很小的调用。输出里每个 agent 一行：

- `✓` 能叫醒，以后每晚扫它的对话
- `✗` 叫不醒，暂时不扫它的对话，后面跟着原因和修法。**把原因和修法原样告诉用户。**
  最常见的是命令行版没登录（桌面版登录了，命令行不一定能用）。用户修好后，重新运行上面这条命令就会加上。

如果全部是 `✗`，命令会失败、不装定时任务，照样把原因告诉用户。

Hermes 还不支持在后台叫醒（没实测过），它的对话暂时不扫。

**Windows 和 Linux 上的本机采集还没在真机上完整验证过。** 装完请告诉用户这一点，
碰到问题请用户在网页左侧「反馈」里提，附上第 5 步 status 的输出。

装完告诉用户：

> 本机采集装好了：会扫 X 的对话（叫不醒的列出来并说怎么修）。找到了你用 agent 干过活的 N 个文件夹。
> 请回到刚才的网页（__URL__/onboard），勾选哪些算工作。**没勾的文件夹，对话连打开都不会打开。**

---

## 第 4 步：装 Fecho 的使用说明（skill）

### 你是 Claude Code

macOS / Linux：

```bash
mkdir -p ~/.claude/skills/fecho && curl -fsSL __URL__/local/SKILL.md -o ~/.claude/skills/fecho/SKILL.md
```

Windows（PowerShell）：

```powershell
New-Item -ItemType Directory -Force "$HOME\.claude\skills\fecho" | Out-Null; Invoke-WebRequest "__URL__/local/SKILL.md" -OutFile "$HOME\.claude\skills\fecho\SKILL.md"
```

如果 `SKILL.md` 已经存在，先告诉用户，同意后再覆盖。

### 你是 Codex

还没实测过，跳过这一步，告诉用户。

---

## 第 5 步：确认

接好 MCP 之后，新加的工具通常要**重启一次会话**才会出现。告诉用户重启。重启后：

1. 调用 `fecho_doctor`，确认显示的是用户的邮箱
2. 查本机采集状态，确认「定时任务：已装」，服务器那行没有报错：
   - macOS / Linux：`python3 ~/.fecho-cloud/fecho_local.py status`
   - Windows：`py -3 "$HOME\.fecho-cloud\fecho_local.py" status`

把两边的结果告诉用户，并告诉用户：接入与验收指南在 __URL__/guide ，照着里面的验收清单逐项确认。

---

## 平时怎么用

- **干完一件事就调 `log_progress` 记一条**：记「做成了什么」，不是对话内容
- 开工时调 `catch_up`，拉回昨天的日报和还挂着的任务
- 用户说「把出日报时间改到 X 点」→ `set_daily_time`
- 用户说「把某个目录加进/移出白名单」→ `set_work_folders`（整组替换，带上要保留的全部路径）
- 用户说「现在就扫一下今天」→ macOS / Linux 运行 `python3 ~/.fecho-cloud/fecho_local.py check --date <今天 YYYY-MM-DD>`；
  Windows 运行 `py -3 "$HOME\.fecho-cloud\fecho_local.py" check --date <今天 YYYY-MM-DD>`
