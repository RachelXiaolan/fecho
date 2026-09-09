# 给 Agent 的安装说明

用户把这个仓库交给你，表示要你安装 Fecho、接上 Mobius，并从现在开始记录真实工作进展。按顺序完成；浏览器授权必须让用户自己确认。

## 1. 安装

个人只用 stdio MCP：

```bash
python3 -m pip install "git+https://github.com/RachelXiaolan/fecho.git"
```

需要 Dashboard、HTTP 或 SSE：

```bash
python3 -m pip install "fecho[server] @ git+https://github.com/RachelXiaolan/fecho.git"
```

验证：

```bash
fecho --version
fecho doctor
```

应该同时存在 `fecho` 和 `fecho-mcp`。若仓库访问失败，先让用户处理 GitHub 权限；不要索要或回显访问 token。

## 2. 连接当前 Agent

Claude Code：

```bash
claude mcp add fecho -- fecho-mcp
```

Codex CLI / Desktop：

```bash
codex mcp add fecho -- fecho-mcp
```

也可在 `~/.codex/config.toml` 明确配置绝对路径：

```toml
[mcp_servers.fecho]
command = "/绝对路径/fecho-mcp"
```

其它宿主：

```json
{"mcpServers":{"fecho":{"command":"fecho-mcp","args":[],"env":{}}}}
```

重启会话后应看到 13 个工具，包括 `log_progress`、`correct_progress`、`catch_up`、`complete_task` 和 `fecho_doctor`。不需要额外启动服务。

## 3. 配身份并自检

```bash
fecho setup --author <稳定英文标识> --display-name <展示名>
fecho doctor
```

`doctor` 会检查存储、Mobius、issue 缓存、LLM、工作范围、项目绑定和可选 collector。首次运行只有存储为正常是合理状态。

## 4. 连接 Mobius（需要用户授权）

先询问用户在 Mobius 上的邮箱，然后调用：

```text
mobius_login(assignee="用户邮箱")
```

Fecho 会走 OAuth 2.1 动态客户端注册 + PKCE，并打开浏览器。请用户在浏览器完成授权；不要替用户点击同意，也不要打印凭证。成功后会自动同步在办 issue。

远程无浏览器时：

```bash
fecho login --no-browser --assignee <邮箱>
```

用户也可主动选择 token 模式：

```bash
fecho login --token <token> --assignee <邮箱>
```

验证：再次运行 `fecho doctor`，Mobius 与 issue 缓存应为正常；或调用 `sync_issues`。

## 5. 设置工作范围和项目绑定

扫描默认不读取任何目录。先显式加入工作仓库父目录：

```bash
fecho scope --work-prefix /绝对路径/to/work
```

在一个项目内工作时，建议绑定对应 issue：

```bash
cd /path/to/project
fecho bind AI-2541
```

项目绑定是强信号，但 `log_progress(..., freeform=true)` 可以明确覆盖它。

## 6. 配置 LLM

扫描抽取和日终整理都需要 LLM：

```bash
fecho setup \
  --llm-url <base_url> \
  --llm-key <api_key> \
  --llm-model <model> \
  --llm-reasoning-effort low
```

不配置仍可主动记进展，日终会生成兜底稿；`fecho scan` 会明确报错并保留水位线，不会假装扫描成功。

## 7. 验证一条完整链路

1. 调用 `log_progress(content="AI-2541 完成 Fecho 安装验收", issue="AI-2541", completion_status="done")`；
2. 保存返回的 `update_id`；
3. 调用 `correct_progress(update_id="...", content="AI-2541 完成 Fecho 安装与连接验收")`；
4. 调用 `my_tasks`，确认只有一个任务和一条修订后的进展；
5. 调用 `end_of_day`，确认日报与口播稿都有产物。

如需 Dashboard：

```bash
fecho web
```

打开 `http://127.0.0.1:8900/`，依次检查 Today、Review、Tasks、Reports、System。

## 8. 可选：团队 collector

```bash
fecho setup --collector-url <地址> --collector-token <个人 token>
```

只会推送日报和口播稿，不会推原始进展。`fecho doctor` 的“团队协作”项会显示状态。

## Agent 使用约定

- 开工先调一次 `catch_up`；
- 完成一个可复述的结果、踩坑或明确决策后调一次 `log_progress`；
- 知道 issue 就明确传入，不确定就用自由任务，不要硬猜；
- 归错或正文不准时调用 `correct_progress`，不要重记一条；
- 明确填写 `completion_status` 和 `kind`；
- 收工时调用 `end_of_day`；
- 不记录用户原话、密钥、完整对话或无结果的计划；
- 口播稿交给人照读录音，不使用 TTS 代替本人。
