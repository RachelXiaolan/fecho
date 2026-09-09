# Fecho · Agent 优先的工作日志系统

> [Mobius AI-2541](https://mobius.feedmob.com/issue/AI-2541) · 本地优先 · Python 3.9+

Fecho 让 Codex、Claude Code、Hermes 等 Agent 在推进工作时直接记录事实，再把这些事实归到持续生长的任务里，日终生成日报和口播稿。原始进展默认只保存在本机 SQLite；人通过 Dashboard 复核归属、修正文案、管理任务和查看系统健康。

```text
Agent 主动提交 ─┐
               ├─▶ 校验 / 归属 / 去重 ─▶ 任务 + 进展 ─▶ 人工复核 ─▶ 日报 + 口播稿
会话记录扫描 ──┘          SQLite                  Dashboard        LLM / 兜底稿
```

## 数据模型

系统的主干是任务，不是对话，也不是平铺日志。

```text
任务（Mobius issue 或自由任务）
├─ 状态：open / done / merged
├─ 进展 × N
│  ├─ 生产者：codex / claude-code / hermes / ...
│  ├─ 进入方式：direct / transcript-scan
│  ├─ 完成状态：done / wip / blocked / unknown
│  ├─ 类型：progress / pitfall / decision
│  └─ 会话、时间、修订号与来源事件键
└─ 审计事件：归属修正、完成、重开、合并
```

记的是“做成了什么、进展到哪、为什么没成”，不是用户 prompt 或完整对话。

## 两条记录路径

### 1. Agent 主动提交

`log_progress` 优先使用 Agent 已知的上下文：

1. 明确传 `issue="AI-2541"`：校验该 issue 确实存在后归入；
2. 明确传 `task_id`：归入已有本地任务；
3. 明确传 `freeform=true`：创建/续接自由任务，可覆盖项目绑定；
4. 当前项目已 `fecho bind AI-xxxx`：使用确定性的目录绑定；
5. 同一 MCP 会话刚推进过某任务：作为弱上下文续接；
6. 都没有：新建自由任务，不凭关键词硬猜 Mobius issue。

返回值包含 `update_id`、任务、归属方式和置信提示。发现归错时使用 `correct_progress` 原地修订；人工确认会锁定归属，日终模型不能覆盖。

### 2. 会话记录扫描

`fecho scan` 支持 Claude Code、Codex、Hermes，也可在 `scan_sources` 中覆盖路径。扫描先按工作目录白名单过滤，再读取增量并调用 LLM 抽取成果、踩坑和决策。LLM 同时做语义归属；没有把握就进入自由任务。

可靠性边界：

- 每个来源会话有独立水位线；
- 只有输出严格符合协议或明确返回 `NONE` 才推进水位线；
- 失败扫描写入 `scan_runs`，Dashboard 可见，并保留重试边界；
- 稳定 `source_event_key` 让同一片段重跑幂等；
- `source_agent` 与 `ingestion_method` 分开保存；
- 未加入工作范围的目录在调用 LLM 前就被排除。

## 安装与连接

```bash
pip install "git+https://github.com/RachelXiaolan/fecho.git"

# Claude Code
claude mcp add fecho -- fecho-mcp

# Codex CLI / Desktop
codex mcp add fecho -- fecho-mcp
```

随后重启 Agent，并调用 `fecho_doctor`。完整安装说明见 [INSTALL.md](INSTALL.md)；ChatGPT 远程连接见 [CONNECT-CHATGPT.md](CONNECT-CHATGPT.md)。

常用命令：

```bash
fecho doctor
fecho login                         # Mobius OAuth + PKCE
fecho scope --work-prefix ~/Documents/work
fecho bind AI-2541                  # 在当前项目目录执行
fecho scan --days 1
fecho digest
fecho web                           # Dashboard + HTTP/SSE MCP
```

默认数据目录为 `~/.fecho/`，配置文件权限为 0600。stdio 模式只依赖 `httpx`；Dashboard/HTTP/SSE 需要安装 `fecho[server]`。

## Dashboard

运行 `fecho web` 后打开 `http://127.0.0.1:8900/`：

- **Today**：按任务聚合当天进展，查看需要留意的归属、重复和报告状态；
- **Review**：编辑正文、改 issue、确认并锁定，恢复误挡的扫描结果；
- **Tasks**：完成、重开或合并误拆任务；
- **Reports**：日报、口播稿、历史版本、生成警告和“内容已变化”提示；
- **System**：Mobius、LLM、工作范围、项目绑定、来源识别和扫描失败诊断。

顶部日期、Agent、记录方式和完成状态筛选在 Today、Review、Tasks 间保持一致。每次修改后服务端返回完整 Dashboard 状态，避免局部数字和列表互相打架。

## MCP 工具（13 个）

| 工具 | 说明 |
|---|---|
| `log_progress` | 记录成果、踩坑或决策；支持 issue、任务、自由任务和完成状态 |
| `correct_progress` | 原地修正文案/归属并人工锁定 |
| `catch_up` | 会话开工时取回指定日成品和当前任务 |
| `my_tasks` | 查看仍在推进的任务 |
| `complete_task` | 完成本地任务；不回写 Mobius |
| `reopen_task` | 重开本地任务 |
| `merge_tasks` | 合并误拆任务并保留审计 |
| `get_my_log` | 查看某天任务、进展和已生成报告 |
| `end_of_day` | 生成日报和口播稿，输入不变时幂等 |
| `mobius_login` | Mobius OAuth 动态注册 + PKCE 登录 |
| `sync_issues` | 刷新当前用户的在办 issue 缓存 |
| `fecho_doctor` | 检查存储、Mobius、LLM、范围、绑定和团队协作 |
| `team_digest` | 读取团队 collector 中的成品 |

每个 stdio、HTTP 或 SSE 连接都有独立 MCP 会话身份。调用方不能通过工具参数伪造 `source_agent`。

## 日终整理

日报按任务分成 Done / In Progress / Blocked / Updates。整理层遵守以下规则：

1. issue 缓存为空或超过时效时跳过模型归属复核，避免用坏上下文改写事实；
2. 人工锁定的归属永不交给模型重判；
3. 相同事实、修订、归属、任务状态、persona、PTO 状态和 prompt 版本命中指纹缓存；
4. 日报与口播独立降级，LLM 故障时仍输出中性的确定性兜底稿；
5. 口播过短会进行结构性扩写重试，过长由代码按句裁剪；
6. 报告生成后事实、归属或任务状态变化时，Dashboard 明确标记需要重新生成。

## Mobius OAuth 与缓存

Fecho 使用 Mobius HTTP MCP 端点，支持 OAuth 2.1 动态客户端注册、PKCE、refresh token 和到期前刷新。`sync_issues` 登录后只拉当前用户在办 issue 到本机缓存；记录时不会每次请求 Mobius。凭证只写 `~/.fecho/config.json`，不会进入日志或 `doctor` 输出。

## 团队联邦模式

个人模式没有共享数据库。可选 collector 只接收日报和口播稿，不接收原始任务与进展：

```text
成员本机 SQLite ── POST /reports（仅成品）──▶ collector.team_reports
```

```bash
# collector 主机
fecho serve --host 0.0.0.0 --port 8899

# 成员本机
fecho setup --collector-url http://collector:8899 --collector-token <个人 token>
fecho team --date 2026-09-09
```

collector 的 author 只从 Bearer token 反查，请求体不能冒充他人。

## 自动运行

```cron
0  9 * * 1-5 fecho sync
*/30 * * * 1-5 fecho scan --days 2
30 19 * * 1-5 fecho digest
```

先配置 `fecho scope`，否则扫描默认不读取任何工作目录。

## 已知边界

- 本地任务状态暂不回写 Mobius；
- PTO 仍使用本地配置文件，未接 timeoff 服务；
- collector 是单机 SQLite，没有高可用；
- ChatGPT 远程连接取决于账户/工作区权限；优先使用 OpenAI Secure MCP Tunnel；
- transcript 格式由宿主控制，版本变化时适配器可能需要跟进。

## 验证

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
./scripts/acceptance.sh     # 需要已配置的真实 Mobius 与 LLM
```

设计与实施顺序见 [可靠性与 UI 设计](docs/plans/2026-09-09-fecho-reliability-ui-design.md) 和 [实施计划](docs/plans/2026-09-09-fecho-reliability-ui-implementation.md)。
