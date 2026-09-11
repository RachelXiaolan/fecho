# 安装 Fecho（这份是写给 agent 看的）

你正在帮用户接入 Fecho：一个替用户记工作进展、每天自动出日报的服务。
按下面的步骤做，**每一步做完都告诉用户结果**。不需要安装任何软件包，也不需要任何密钥——
唯一的凭证是用户登录后拿到的 token。

服务地址：__URL__

---

## 第 1 步：让用户登录

在用户电脑上打开浏览器：

```bash
open __URL__/onboard
```

告诉用户：

> 浏览器打开了 Fecho，请用 Mobius 账号登录（只接受 @__DOMAIN__ 邮箱）。
> 登录后页面上点「生成 token」，把那串 `fecho_` 开头的字符复制发给我。
> 页面上还可以顺便选每天几点出日报。

**等用户把 token 发过来再继续。** token 连续 7 天不用才会失效，天天在用就一直有效。

---

## 第 2 步：把 Fecho 接到你自己身上

按你是哪个 agent，选一段做。把 `<TOKEN>` 换成用户给你的那串。

### 你是 Claude Code

```bash
claude mcp add --transport http --scope user fecho __URL__/mcp --header "Authorization: Bearer <TOKEN>"
```

### 你是 Codex

在 `~/.codex/config.toml` 末尾追加（已有 `[mcp_servers.fecho]` 就替换掉）：

```toml
[mcp_servers.fecho]
type = "http"
url = "__URL__/mcp"

[mcp_servers.fecho.http_headers]
Authorization = "Bearer <TOKEN>"
```

### 你是 Hermes

远程接入的写法**还没实测过**。请告诉用户这一点，并请他联系 Fecho 管理员。

---

接好之后，新加的工具通常要**重启一次会话**才会出现。告诉用户重启，重启后调用 `fecho_doctor`，
确认第一行显示「云端版」和用户的邮箱。

---

## 第 3 步：上报工作文件夹

Fecho 只扫描用户勾选为「工作」的文件夹里的对话，这是隐私边界。你要做的是找出**用户用 agent
干过活的所有文件夹**，交给服务器，让用户在网页上勾选。

**只读路径，不读对话内容。**

- **Claude Code**：`~/.claude/projects/*/*.jsonl`，每一行是一条 JSON，取 `cwd` 字段
- **Codex**：`~/.codex/sessions/**/*.jsonl`，取 `type` 为 `session_meta` 那一行的 `payload.cwd`

每个文件夹记下最近一次出现的时间，去重，只保留绝对路径，调用：

```
report_work_folders(folders=[{"path": "/Users/…/work/项目A", "last_used": "2026-09-11"}, …])
```

然后告诉用户：

> 我找到了你用 agent 干过活的 N 个文件夹，已经交给 Fecho。
> 请回到刚才的网页（__URL__/onboard），勾选哪些算工作。没勾的不会被扫描。

---

## 平时怎么用

- **干完一件事就调 `log_progress` 记一条**：记「做成了什么」，不是对话内容
- 开工时调 `catch_up`，拉回昨天的日报和还挂着的任务
- 用户说「把出日报时间改到 X 点」→ `set_daily_time`
- 用户说「把某个目录加进/移出白名单」→ `set_work_folders`（整组替换，带上要保留的全部路径）
