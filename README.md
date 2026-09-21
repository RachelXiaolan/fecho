# Fecho · Agent 优先的工作日志系统

> 把 Agent 做成的事沉淀成可复核的任务、日报和口播稿，而不是在下班时靠回忆补写。

Fecho 面向使用 Codex、Claude Code 等 coding agent 的团队。Agent 在推进工作时记录事实；Fecho 把这些事实归到持续生长的任务中，在每天设定的北京时间生成日报和口播稿。人只需要在网页里复核、修正和补充。

当前的主产品是云端版：**对话原文留在用户电脑上，云端只接收提炼后的进展**。仓库也保留本地/自托管模式，供高级部署或开发使用。

```text
用户电脑
Codex / Claude Code ──▶ 主动记录或本机扫描 ──▶ 提炼后的工作进展
                                                       │
                                                       ▼
Fecho 云端
按用户隔离的任务与进展 ──▶ 归属 / 去重 / 人工复核 ──▶ 日报 + 口播稿
```

## 用 Fecho 云端版

目前云端版面向可以登录 Mobius 的 Feedmob 成员。安装一次约十分钟；你不需要手动安装 Python 包，也不需要持有 LLM 密钥。

1. 在 Codex 或 Claude Code 中新开对话，发送：

   ```text
   读 https://fecho.techmob.net/install ，按里面的步骤帮我接入 Fecho。
   ```

2. 在网页中用自己的 Mobius 账号登录，生成个人 token 并交给 Agent。
3. 在 onboarding 页面勾选允许扫描的工作目录，然后重启 Agent 会话。

完整接入、验收、隐私说明和常见问题见 [ONBOARDING.md](ONBOARDING.md) 或网页版 [接入指南](https://fecho.techmob.net/guide)。

### 日常怎么用

- 做完一件事：对 Agent 说“记一下”。它会记录成果、踩坑或已确认的决策。
- 开工前：说“用 Fecho 看看昨天做到哪了”。
- 日报不对：在网页的“复核”中改归属，或在“日报”中直接修改正文。人工确认的归属不会再被模型覆盖。
- 需要非 MCP 的 Agent 接入：在“日报”页生成受限的快速 API key；它可以上传进展、成品日报和图片，但不能访问其他个人数据。

## Fecho 做什么

### 任务是主干，不是流水账

一项 Mobius issue 或自由任务可以横跨多天、多场对话和多个 Agent。每条进展都保留生产者、进入方式、完成状态、来源事件键、修订和归属审计；因此可以纠错，而不是把错误悄悄覆盖掉。

```text
任务（Mobius issue 或自由任务）
├─ 状态：open / done / merged
├─ 进展 × N
│  ├─ 来源：direct / transcript-scan / quick-api
│  ├─ 完成状态：done / wip / blocked / unknown
│  ├─ 类型：progress / pitfall / decision
│  └─ 会话、时间、修订号与来源事件键
└─ 审计：归属修正、完成、重开、合并
```

### 两条记录路径

**主动记录**适合 Agent 已知道任务归属的场景。它优先使用明确 issue、已有任务、项目绑定和同一会话上下文；不确定时宁可落入自由任务，也不会用关键词硬猜。

**本机扫描**是补漏。每 15 分钟，本机采集器向服务器询问是否该扫描；只读取用户勾选目录中的对话，过滤工具调用、工具输出和宿主样板后，再由同一种 Agent 提炼出“做成了什么”。失败时保留水位线，重跑不会漏掉未处理内容；稳定事件键使上传可安全重试。

### 日报与工作台

云端 worker 会先同步 Mobius issue，再按每个人的时间生成日报、口播稿和必要的归属复核。事实、归属或任务状态变化时，网页会提示日报需要更新；人工写过的日报不会被自动版本覆盖，旧版会保留在历史中。

工作台支持：

- 今天的任务与进展、归属复核、任务完成/重开/合并；
- 日报、口播稿、历史版本、图片和个人写作偏好；
- 工作文件夹的选择、分组、隐藏和恢复；
- Agent 接入状态、扫描诊断、反馈截图与管理员审批。

## 支持情况与隐私边界

| Agent / 入口 | 主动记录 | 自动扫描 |
|---|---:|---:|
| Codex | 支持 | 支持 |
| Claude Code | 支持 | 支持；需要命令行版已登录 |
| Cursor | 支持 | 暂不支持 |
| Grok / 网页 Actions 等 HTTP Agent | 通过快速 API | 暂不支持 |
| Hermes | 支持 | 暂不支持 |

- 只扫描用户明确勾选的目录；未勾选目录的对话不会被打开。
- 对话原文和工具输出不离开用户电脑；上传的是提炼后的工作进展。
- 每个人默认只能访问自己的数据；管理员权限需经过审批。
- Mobius 授权在云端加密保存，用于同步该用户自己的 issue；快速 API key 只允许访问受限上传接口。

Windows 和 Linux 的本机扫描已适配，但仍需要更多真实机器验收。Cursor、Grok 与 Hermes 的自动扫描限制和后续方向见 [技术债](docs/tech-debt.md)。

## 给开发者与管理员

### 云端架构

```text
Vercel                 Supabase                 lu2 worker
网页 / 登录 / API  ──▶ Postgres（用户隔离） ◀── 定时出稿、重试、写作偏好
       ▲                    ▲
       └──── 用户电脑上的 fecho_local.py ────┘
```

部署需要 Supabase、Vercel 和 lu2 三部分；完整的环境变量、建表、迁移和故障排查见 [DEPLOY.md](DEPLOY.md)。新增或修改 SQL 后，必须运行临时 Postgres 全量测试，而不仅是 SQLite 测试。

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python scripts/test_postgres.py -q
```

### 本地 / 自托管模式

本地模式保留 SQLite、`fecho-mcp`、本地 Dashboard、HTTP/SSE MCP 与只接收成品的 team collector，适合开发、离线个人使用或自行部署。

```bash
python3 -m pip install "fecho[server] @ git+https://github.com/RachelXiaolan/fecho.git"
fecho doctor
fecho setup --author <稳定英文标识>
fecho bind AI-2541                  # 在项目目录中执行
fecho scan --days 1
fecho digest
```

本地模式的 `onboard`、范围规则、自动任务、MCP 与 collector 命令请使用 `fecho --help` 查看。ChatGPT/Codex 的 MCP 连接说明见 [CONNECT-CHATGPT.md](CONNECT-CHATGPT.md)。

本地 MCP 固定提供 13 个通用工具；云端连接还会按需提供扫描提交、工作目录和每日时间设置等工具。两种模式都使用同一套任务、归属、审计与报告逻辑。

## 项目资料

- [版本记录](CHANGELOG.md)：每个版本的产品变化与升级注意事项
- [接入与验收](ONBOARDING.md)：给最终用户的操作说明
- [云端部署](DEPLOY.md)：Supabase、Vercel 与 lu2 worker 的管理员手册
- [ChatGPT / Codex 连接](CONNECT-CHATGPT.md)
- [可靠性与 UI 设计](docs/plans/2026-09-09-fecho-reliability-ui-design.md)
- [技术债](docs/tech-debt.md)

## 验证

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
./scripts/acceptance.sh  # 需要真实 Mobius 与 LLM 配置
```
