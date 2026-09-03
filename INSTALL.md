# 给 agent 的安装说明

> 用户把这个仓库丢给你，意思是：**请你把 Fecho 装好并接上，然后开始用它记我们的工作进展。**
> 按下面的步骤做完，每一步都有验证方式。需要用户配合的地方我会写明——那几步你必须停下来问。

---

## Step 1 · 安装

```bash
pip install "git+https://github.com/RachelXiaolan/fecho.git"
```

装好之后有两个命令：`fecho`（人和你用来配置/排查）、`fecho-mcp`（MCP 服务本体）。

**验证**：`fecho --version` 有输出。

> 装不上就换 `pipx install git+…` 或 `uv tool install git+…`。
> 需要 Python 3.9 以上，除 `httpx` 外无第三方依赖。
> 仓库是私有的——如果报 403/认证失败，说明当前机器没有该仓库的 GitHub 访问权限，
> 让用户先 `gh auth login`，或者找仓库所有者要访问权。

---

## Step 2 · 把 MCP 挂到你自己身上

```bash
claude mcp add fecho -- fecho-mcp        # claude code
```

其它宿主手动加：

```json
{ "mcpServers": { "fecho": { "command": "fecho-mcp", "args": [], "env": {} } } }
```

codex 写进 `~/.codex/config.toml` 的 `[mcp_servers.fecho]`。

**验证**：重启会话后你应该能看到 `log_progress`、`catch_up`、`fecho_doctor` 等 8 个工具。

> 不需要另外起任何服务。Fecho 是单进程的，数据落在 `~/.fecho/`。

---

## Step 3 · 自检

调用 `fecho_doctor`。它会逐项告诉你什么配好了、什么还缺、缺的怎么补。
**后面几步就是把它标 ✗ 的项补齐。**

此刻应该是：存储 ✓，Mobius ✗，issue 缓存 ✗，LLM ✗。
这时已经可以记进展了，只是配不到 Mobius issue、日终出的是兜底稿。

---

## Step 4 · 连 Mobius（**需要用户操作**）

先问用户一句：**「你在 Mobius 上的邮箱是什么？」**（用来确定拉谁的 issue）

拿到之后调用：

```
mobius_login(assignee="用户的邮箱")
```

会自动弹出浏览器授权页。**你要告诉用户去浏览器点「同意」**，然后等它返回。
授权完成后 Fecho 会自动把用户名下在办的 issue 拉下来。

- 用不了浏览器（比如远程机器）→ 让用户跑 `fecho login --no-browser --assignee <邮箱>`，
  把打印出来的链接在自己电脑上打开。
- 用户更想直接给 token → `fecho login --token <token> --assignee <邮箱>`。

**不要替用户点授权，也不要把 token 写进任何文件或打印出来。**
凭证由 Fecho 自己存在 `~/.fecho/config.json`（权限 0600）。

**验证**：再调 `fecho_doctor`，Mobius 和 issue 缓存两项应该都变 ✓。

---

## Step 5 · 配 LLM（可选，但日终出稿要用）

问用户要三样：接口地址、API key、模型名。然后：

```bash
fecho setup --llm-url <base_url> --llm-key <key> --llm-model <model>
```

推理模型（minimax-m3、glm 之类）建议再加 `--llm-reasoning-effort low`，能省掉大半 token。

不配也能用——记进展、配对、任务归并都不需要 LLM，只是日终出的是确定性兜底稿。

**验证**：`fecho_doctor` 的 LLM 一项变 ✓。

---

## Step 5b · 接团队 collector（可选，只有团队已经部署了共享 collector 才做这步）

问用户：「团队的 fecho collector 地址是什么？有没有给你分配 token？」都没有就跳过这步，
不影响个人使用。有的话：

```bash
fecho setup --collector-url <地址> --collector-token <token>
```

**验证**：`fecho_doctor` 的「团队协作」一项变 ✓。之后每次 `end_of_day` / `fecho digest`
出完稿会自动把日报和口播稿推给 collector——只推成品，不推原始进展。

---

## Step 6 · 试一条

调用 `log_progress(content="装好了 fecho 并接上了 Mobius")`，看返回里的「配对方式」。
再调 `my_tasks` 应该能看到这条挂在某个任务下面。

至此安装完成。

---

# 装好之后你该怎么用

这部分比安装重要。Fecho 的价值全在于**你是否真的在记**。

### 什么时候调 `log_progress`

**推进完一件事就调一次。** 不要攒到用户说「帮我写日志」才补。判断标准：

- ✅ 修完一个 bug、跑通一条链路、得出一个结论、做完一次调研、定了一个方案
- ✅ 试了但失败了，并且知道了为什么——**负面结论也是进展**
- ❌ 用户刚说了句话、你刚读完几个文件、你正打算做某事

### 记什么

记「**我们做成了什么、进展到哪**」，不是对话内容，不是用户说过的 prompt，也不是你改了哪些文件。
一句能让人三个月后看懂的话：

- ✅ 「配对引擎写完了，拿真实 issue 测下来 11/12 命中，剩一条是纯上下文依赖的」
- ❌ 「改了几个文件」「按用户要求做了修改」「完成了任务」

### 归到哪个任务，你不用操心

系统会自己判断：正文里写了 issue 号就直接归过去；没写就拿内容和用户名下在办的
Mobius issue 配对；配不上就归到同一个对话里刚才那个任务；再不行才新立一个自由任务。

**但要看返回值。** 如果它说「⚠️ 这条是猜的」，而你知道这其实是另一件事，
带上 `issue="AI-xxxx"` 或 `task_id` 重记一次。你知道自己在做什么，系统不知道。

### 开工和收工

- 会话开场调一次 `catch_up`：昨天干了什么、现在还挂着哪些任务，一次看全。
- 用户说要下班/收工时调 `end_of_day`：出日报和口播稿。
  **口播稿是给用户照着念、录成语音的——语音永远由人来录，不要提议用 TTS。**

### 别做的事

- 不要把用户的原话整段塞进 `log_progress`
- 不要为了「有东西记」而记流水账
- 不要一件事记很多条（同一件事的多条进展会归到一个任务下，但内容重复没有价值）
