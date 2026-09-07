# 把 fecho 接到 ChatGPT

fecho 的 MCP 服务跑在你自己的电脑上，数据不出本机。ChatGPT 通过一条隧道连过来。

下面第二节整段复制给 ChatGPT（或任何能执行命令的 agent），它会自己把前置步骤做完。
最后一步「在 ChatGPT 界面里添加连接器」必须由人操作 —— agent 碰不到那个界面。

---

## 一、先决条件

- fecho 已装好并能跑（`fecho doctor` 全绿）
- **`cloudflared` 还没装**，先跑这句：

      brew install cloudflared

---

## 二、给 GPT 的 prompt（整段复制）

```
我要把本机的 fecho MCP 服务接到 ChatGPT 上。请按下面的步骤操作，每一步都把真实输出贴给我。

【背景】
fecho 是一个记录工作进展、自动出日报的 MCP 服务，跑在我这台 Mac 上，数据存在本地
SQLite。它同时提供两个 MCP 传输端点：
  /sse/   —— SSE，ChatGPT 用这个（URL 必须以 /sse/ 结尾）
  /mcp    —— Streamable HTTP，别的客户端用
鉴权规则：来自 127.0.0.1 的请求直接放行；非本机来源必须带
`Authorization: Bearer <token>`，token 从环境变量 FECHO_WEB_TOKEN 读。
如果没设 FECHO_WEB_TOKEN，非本机来源一律 403（防止隧道一开就裸奔）。

【第 1 步：生成 token 并起服务】
在一个终端里执行，然后保持这个终端不要关：

    export FECHO_WEB_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
    echo "TOKEN=$FECHO_WEB_TOKEN"
    fecho web --no-browser

把 TOKEN= 那一行的值记下来，后面要用。
确认输出里有 "MCP 端点 http://127.0.0.1:8900/mcp"。

【第 2 步：本机自测，确认服务是好的】
另开一个终端：

    curl -s http://127.0.0.1:8900/healthz

应该返回 {"ok":true,"author":"..."}。如果不是，先解决这一步再往下。

再测 MCP 协议本身：

    curl -s -X POST http://127.0.0.1:8900/mcp \
      -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

应该返回 9 个工具：log_progress / catch_up / my_tasks / get_my_log /
end_of_day / mobius_login / sync_issues / fecho_doctor / team_digest。

【第 3 步：开隧道】
再开一个终端，保持不关：

    cloudflared tunnel --url http://127.0.0.1:8900

它会打印一个形如 https://xxxx-yyyy.trycloudflare.com 的地址。记下来。

【第 4 步：验证隧道 + 鉴权都对】
把 <隧道地址> 和 <TOKEN> 换成真实值：

    # 不带 token 应该被拒（期望 401）
    curl -s -o /dev/null -w "%{http_code}\n" -X POST <隧道地址>/mcp \
      -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'

    # 带 token 应该成功（期望 200，返回 9 个工具）
    curl -s -X POST <隧道地址>/mcp \
      -H "Authorization: Bearer <TOKEN>" \
      -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

两个结果都符合预期，才算通了。

【第 5 步：把最终信息整理给我】
输出这三样，我要拿去填 ChatGPT 的界面：
  - 连接 URL：<隧道地址>/sse/     ← 注意结尾必须有 /sse/
  - Token：<TOKEN>
  - 上面第 4 步两条 curl 的实际返回

【注意】
- 不要尝试替我登录 ChatGPT 或修改 ChatGPT 的设置，那一步我自己在界面里做。
- 隧道地址每次重启 cloudflared 都会变；两个终端（fecho web 和 cloudflared）
  都要一直开着，关掉 ChatGPT 就连不上了。
```

---

## 三、最后一步：在 ChatGPT 界面里添加（这步只能人做）

1. ChatGPT → **Settings → Security and login** → 打开 **Developer mode**
2. 打开 [chatgpt.com/plugins](https://chatgpt.com/plugins) → 点 **+**
3. 填服务器 URL：`https://xxxx.trycloudflare.com/sse/`（结尾的 `/sse/` 不能少）
4. 鉴权那里填上面拿到的 token

**如果界面里没有填自定义 header / token 的地方**，说明这条路走不通，需要改成 OAuth。
OpenAI 的文档推荐 OAuth 配合 CIMD，ChatGPT 支持公开客户端（`none`）和
`private_key_jwt` 两种令牌交换方式。fecho 已经实现过一遍 Mobius 的 OAuth 2.1
（动态注册 + PKCE + 公开客户端），改造有现成的参照。

看到界面后告诉我有没有 token 输入框，我再决定要不要补 OAuth。

---

## 四、接之前该知道的两件事

**1. 提示注入的风险。** OpenAI 的文档专门警告：信任一个 MCP 的开发者，不等于用它安全
—— 你需要信任**这个 MCP 能读到的全部内容**。fecho 读的是你的对话记录和 Mobius issue，
这些内容里可能混进别人写的文字。

**2. 隧道是公开地址。** trycloudflare 的地址虽然随机，但它在公网上。token 是唯一的门。
不要把 token 贴进公开的地方；服务不用的时候把两个终端关掉。
