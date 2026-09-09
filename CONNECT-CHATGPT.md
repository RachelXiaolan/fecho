# 把 Fecho 接到 ChatGPT

Fecho 的数据在本机，ChatGPT 不能直接连接 `127.0.0.1`。当前首选方案是 [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)：它从本机发起出站 HTTPS，不开放公网入站端口。ChatGPT 自定义 MCP 应用的可用范围和管理权限仍可能随产品更新变化，操作前请核对 [ChatGPT Developer mode 文档](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt)。

## 适用前提

- Fecho 已安装并通过 `fecho doctor`；
- 你的 ChatGPT 账户/工作区允许 Developer mode 和自定义 MCP 应用；
- 你在 OpenAI Platform 有创建/使用 Tunnel 的权限；
- 本机可以向 OpenAI 发起 HTTPS 请求。

Fecho 暴露三种入口：stdio（本机 Agent）、`/mcp`（Streamable HTTP）、`/sse/`（兼容旧客户端）。Secure MCP Tunnel 可直接转发 stdio，通常无需启动 `fecho web`。

## 推荐：Secure MCP Tunnel + stdio

1. 在 OpenAI Platform 的 Tunnel 设置中创建 tunnel，关联正确的 Platform organization 和 ChatGPT workspace，记下 `tunnel_id`。
2. 从该页面下载最新 `tunnel-client`，不要把固定旧版本写死进安装脚本。
3. 在本机创建 Fecho profile：

```bash
export CONTROL_PLANE_API_KEY="<仅在当前终端设置>"

tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile fecho-local \
  --tunnel-id <tunnel_id> \
  --mcp-command "$(command -v fecho-mcp)"

tunnel-client doctor --profile fecho-local --explain
tunnel-client run --profile fecho-local
```

保持最后一个进程运行。Fecho 的 MCP 会话身份由连接上下文决定，工具参数不能伪造 Agent 名称。

4. 在 ChatGPT 的 Developer mode 应用创建界面选择 **Tunnel**，选择刚才关联的 tunnel 或填入 `tunnel_id`。
5. 扫描工具，确认出现 13 个 Fecho 工具；创建 draft 后在新对话中选择/提及该应用。
6. 先调用 `fecho_doctor`，再用一条可删除的测试进展验证 `log_progress → correct_progress → get_my_log`。

如果 tunnel 在 ChatGPT 中不可见，依次检查：

- tunnel 是否关联了目标 ChatGPT workspace，而不只是 Platform organization；
- 操作者是否有 Tunnels Read + Use；
- `tunnel-client run` 是否仍在运行；
- `tunnel-client doctor --profile fecho-local --explain` 是否全绿。

## 备选：私有网络内的 HTTP

如果团队已有受控私网或反向代理，可启动：

```bash
export FECHO_WEB_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
fecho web --host 127.0.0.1 --port 8900 --no-browser
```

本机验证：

```bash
curl -s http://127.0.0.1:8900/healthz

curl -s -X POST http://127.0.0.1:8900/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"chatgpt","version":"1"}}}'
```

`/healthz` 只返回 `{"ok":true}`，不会泄漏 author。非本机访问必须携带 `Authorization: Bearer <FECHO_WEB_TOKEN>`；未配置 token 时所有非本机请求直接拒绝。

若使用 Secure MCP Tunnel 转发 HTTP，把 profile 的 stdio command 换成 `--mcp-server-url http://127.0.0.1:8900/mcp`。

## 不再推荐：临时公网隧道

Cloudflare quick tunnel 之类的临时公网 URL 会把入口暴露到互联网，地址还会随重启变化。它只适合短时受控测试，必须设置高强度 `FECHO_WEB_TOKEN`，不得作为正式部署方案。Fecho 目前没有为自身远程入口实现完整的 OAuth provider；Mobius OAuth 只用于 Fecho 访问 Mobius，不能替代 ChatGPT → Fecho 的认证。

## 安全边界

- 只连接你信任的 Fecho 实例；MCP 能接触到的 issue 和工作日志可能包含提示注入文本；
- 工作目录必须先通过 `fecho scope` 加入白名单，扫描在调用 LLM 前过滤；
- Fecho 工具有写操作，ChatGPT 可能要求确认；发布前逐项审查工具权限；
- 不在聊天、仓库、截图或命令历史里暴露 Mobius、LLM、collector、OpenAI API 密钥；
- ChatGPT 对已发布工具可能使用冻结快照；工具 schema 更新后需要管理员刷新并重新审核。
