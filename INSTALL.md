# 给 Agent 的 Fecho Onboarding 说明

目标是让用户只表达一次安装意图，之后 Fecho 自动记录和补漏，并在用户选择的北京时间生成日报。浏览器里的 Mobius 授权必须由当前用户本人确认。

## 用户可以直接给 Agent 的 Prompt

> 安装并接入 Fecho。使用管理员提供的共享 LLM 配置；询问我的稳定标识、展示名、工作目录白名单、Mobius 邮箱和每天的总结时间。总结时间默认北京时间 21:00，必须早于 22:00。为本机已有的 Codex、Claude Code、Hermes 安装 Fecho MCP 和 Skill，并安装本地 Dashboard 与每日自动任务。Mobius 必须打开网页让我登录自己的账号。完成后运行 doctor，告诉我哪些 Agent 已接入、自动任务状态和 Dashboard 地址。不要显示任何 key。

## 管理员准备共享 LLM 配置

普通安装者不需要填写 MiniMax 参数。管理员通过私密 JSON 文件或 HTTPS 地址提供：

```json
{
  "llm_base_url": "https://example.internal/v1",
  "llm_api_key": "shared-key",
  "llm_model": "minimax-m3",
  "llm_reasoning_effort": "low"
}
```

可以预设其中一种环境变量：

```bash
export FECHO_SHARED_CONFIG=/private/path/fecho-shared.json
export FECHO_SHARED_CONFIG_URL=https://private.example/fecho.json
```

不要把共享 key 提交到 Git、wheel 或 Skill。它最终会写入每位用户权限为 0600 的 `~/.fecho/config.json`；安装者本人可以读取，这是当前共享 key 方案的已知边界。

## Agent 执行步骤

安装包含 Dashboard 的版本：

```bash
python3 -m pip install "fecho[server] @ git+https://github.com/RachelXiaolan/fecho.git"
```

收集用户信息后执行一次 onboarding：

```bash
fecho onboard \
  --author <稳定英文标识> \
  --display-name <展示名> \
  --work-prefix /绝对路径/to/work \
  --ignore /绝对路径/to/private-if-needed \
  --mobius-assignee <用户自己的邮箱> \
  --time 21:00
```

`--work-prefix` 和 `--ignore` 可以重复。未登记目录默认不读取、不发送给 LLM。时间固定按 `Asia/Shanghai` 解释，可选范围为 `00:00–21:59`。

onboarding 会依次：

1. 读取已有或管理员预置的共享 LLM 配置；
2. 写入身份和工作区白名单；
3. 为已安装的 Codex、Claude Code、Hermes 注册绝对路径 MCP 并复制 Fecho Skill；
4. 清除可能存在的旧 Mobius 身份，打开网页让当前用户重新 OAuth；
5. 同步当前用户的在办 issue；
6. 安装北京时间每日任务和本地 Dashboard LaunchAgent；
7. 返回脱敏 doctor 结果。

完成后让用户重启 Agent。不存在的宿主会显示 `not-installed`；已有冲突配置会显示 `conflict`，不会静默覆盖。

## 日常自动闭环

Fecho Skill 要求 Agent 在开工时调用一次 `catch_up`，完成一个可复述成果、确认踩坑或作出明确决策后自动调用 `log_progress`。Agent 遗漏的内容由每日 transcript 扫描补齐。

每天到点依次运行：

```text
Mobius sync → 白名单会话扫描 → 去重与匹配 → MiniMax 日报/口播稿 → Dashboard
```

管理命令：

```bash
fecho schedule status
fecho schedule run-now
fecho schedule install --time 20:30
fecho schedule uninstall
```

`run-now` 用于新安装后的真实验收。Mobius 临时失败会成为警告，不阻断本地日志；扫描或日报失败会显示为失败，并保留后续重试条件。

## Dashboard

macOS 登录后，LaunchAgent 会把 Dashboard 保持在：

<http://127.0.0.1:8900/>

它只监听本机。日志记录和每日整理直接访问 SQLite，即使 Dashboard 进程短暂退出也不会丢数据；系统会自动重新拉起。页面在重新聚焦、恢复可见及每分钟自动刷新。

用户在 21:00 后主要检查：

- Today：当天事实是否齐全；
- Review：正文和 Mobius 归属是否准确，确认后锁定；
- Reports：日报和口播稿是否符合事实；
- System：上次自动运行是否成功。

人工修改会把报告标成需要更新。用户确认完成后，在 Reports 点击“重新生成”。

## 验收与排查

```bash
fecho --version
fecho doctor
fecho schedule status
fecho schedule run-now
```

通过标准：

- 存储、Mobius、issue 缓存、LLM、工作范围和每日自动整理正常；
- 当前用户亲自完成了 Mobius 网页授权；
- 已安装宿主能看到 Fecho MCP 和 Skill；
- `run-now` 完成同步、扫描和日报；
- Dashboard 固定地址可打开，并能看到本次运行结果。

团队 collector、精确指定历史日期重放和云端 Dashboard 不属于本轮 onboarding 闭环。
