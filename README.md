# Fecho · Agent 优先的工作日志系统

> ticket: [AI-2541](https://mobius.feedmob.com/issue/AI-2541)
> 系统的用户是 **agent**，人是读者。

```
agent 边干边记 ──▶ 系统归到任务上 ──▶ 日终整理 ──▶ 日报 + 口播稿 ──▶ 人照读录语音
 log_progress      Mobius 自动配对      cron / LLM     按任务分组      （唯一的人工环节）
```

## 核心：任务，不是记录

系统里存的主干是**任务**，不是一条条平铺的记录。

```
任务
 ├─ 名字
 ├─ 来源：Mobius 上的 issue（AI-2541）  或者  Mobius 之外的自由任务
 ├─ 状态
 └─ 进展 × N
      ├─ 谁记的（codex / claude-code / hermes）
      ├─ 什么时候、哪个对话
      └─ 做成了什么
```

**记的是「我们做成了什么、进展到哪」，不是对话内容，也不是用户说过的 prompt。**

一个任务可以横跨很多天、很多个对话、很多个 agent；一个对话里也可能推进好几个任务。
所以任务是主干，会话只是审计线索和配对时的上下文先验。

## agent 说一句话之后发生什么

四级配对，先命中先赢：

| # | 方式 | 触发条件 | 例子 |
|---|---|---|---|
| 1 | `explicit` | 正文里写了 issue 号，或调用时点名 | 「顺手把 **AI-2541** 的配对引擎写了」 |
| 2 | `mobius-auto` | 和你名下在办的 Mobius issue 标题够像 | 「本地试了下 awesome-gpt-image-2」→ AI-2539 |
| 3 | `task-continue` | 和已有任务够像；或**接着同一个对话刚才那个任务** | 「接口和数据库跑通了」→ 接上文 |
| 4 | `new-task` | 都不是 | 「帮同事看了下爬虫为什么超时」→ 自由任务 |

**打分怎么算**：中文比 bigram 的*包含度*（不是 Jaccard——进展句通常比 issue 标题短得多，
Jaccard 会被长度差压死）；英文和带数字的 token 单独算，`awesome-gpt-image-2` / `lu3` 这种词
一旦对上就是强信号；再加一条「最长连续共现」——「工作日志」四个字连着出现，
比零散撞上三个双字词强得多。

拿真实的 Mobius issue 实测：命中的分数在 0.38–0.62，不该命中的在 0.00–0.16，中间有干净的空隙。

### 一个 V0 解决不了的问题，说在前面

「接口和数据库跑通了」这种话，靠关键词**永远**判不出属于哪个任务。唯一可用的信号是
"刚才在这个对话里推进的是哪个任务"，所以同一对话的任务是**默认归属**。

代价：同一个对话里换了话题、新话题又没有特征词时会错归。V0 不假装能解决，做的是两件事——

1. 配对方式对 agent **可见**，猜的就明说是猜的；
2. agent 判断错了可以带 `issue` 或 `task_id` 重记一次。

agent 知道自己在做什么，把最终判断权还给它，比让它盲信一个猜测更靠谱。

## 安装

```bash
pip install "git+https://github.com/RachelXiaolan/fecho.git"
claude mcp add fecho -- fecho-mcp
```

然后在 agent 会话里调 `fecho_doctor`，它会告诉你还缺什么、怎么补。
接 Mobius 调 `mobius_login`——**开浏览器授权，不用手贴 key**（Mobius 的 MCP 端点
支持 OAuth 2.1 动态客户端注册 + PKCE，所以整个流程零配置）。

单进程，装完不用起任何服务，数据落在 `~/.fecho/`。除 `httpx` 外无第三方依赖，
Python 3.9+。凭证只存在 `~/.fecho/config.json`（0600）。

**把仓库丢给 agent 让它自己装**：[INSTALL.md](INSTALL.md) 就是写给 agent 看的，
含安装、接 Mobius、验证，以及装好之后它该怎么用这套工具的行为约定。

```bash
fecho doctor          # 自检
fecho login           # 连 Mobius（浏览器授权）
fecho setup --llm-url … --llm-key … --llm-model …
fecho digest          # 日终整理（cron 入口）
fecho serve           # 只有团队共享部署才需要：起 REST 服务
python3 tests/test_core.py   # 22 个单测
```

## MCP 工具

| 工具 | 说明 |
|---|---|
| `log_progress` | 推进完一件事就调一次。只有 `content` 必填，归到哪个任务由系统判断 |
| `catch_up` | **开工时调**：拉回昨天的日报、口播稿，和现在还挂着的任务 |
| `my_tasks` | 我现在有哪些任务在推进，各自最近一条进展是什么 |
| `get_my_log` | 我今天推进了哪些任务、推到哪了 |
| `list_team_log` | 团队某天推进了哪些任务 |
| `sync_issues` | 从 Mobius 刷新配对用的 issue 缓存（cron 也会跑） |
| `end_of_day` | 收工出稿。输入没变时不重复生成 |

`catch_up` 是"反向"那一环。说明白：**MCP 协议里服务端没法主动往会话里塞东西**，
所以这不是系统推送，是 agent 开工时自己拉一次。配上宿主的会话启动钩子，效果等同于推。

## 整理层

日报按**任务**分组出，每个任务一段：这件事今天推到哪了。不是一条条记录排下来。

三条不变量：

1. **幂等** —— 输入指纹没变就不重复烧 token，`--force` 才重生成，旧稿进历史表；
2. **永远有产物** —— LLM 挂了走确定性兜底稿，日报文件不会缺；
3. **口播稿字数硬校验** —— 生成后代码数字数，超界调结构重写，仍超界按句号裁。

### 接推理模型（minimax-m3）踩到的三个坑

**别让推理模型数字数。** 提示词写「必须 200–280 字」，模型会在思维链里反复数中文字符，
实测烧掉 7216 个推理 token 后正文一个字没吐。改成给结构性目标（「讲 3 到 4 件事，每件一到两句」），
数字数和裁剪交给代码，一次就出稿。

**思维链长度不稳定，按可重试失败处理。** 同一提示词推理消耗在 1618 和 4000+ 之间跳。
撞到预算耗尽就把 `max_tokens` 翻倍重来（上限 16000），而不是把当天产物整个降级。
`reasoning_effort=low` 能把推理压到 155 token，但它是非标准参数，端点不认时客户端自动去掉重试。

**产物各自独立降级。** 日报和口播稿分开 catch，`generator` 可能是 `llm` / `fallback` / `llm+fallback`。

**端点形态**：`http://api.feedmob.it.com` 把思维链拆进 `reasoning_content`；带 `/v1` 的那层
内联在 `content` 的 `<think>` 标签里。客户端两种都吃得下，端点行为不泄漏到整理层。

## API

全部走 `Authorization: Bearer <token>`，token 即身份。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/progress` | 记一条进展。返回配到了哪个任务、怎么配的、置信度 |
| GET | `/progress?date=&author=&mine=` | 某天按任务分组的进展 |
| GET | `/tasks?mine=&status=` | 任务列表 |
| GET | `/tasks/{id}` | 单个任务及其全部进展 |
| POST | `/tasks/{id}/close` | 关掉一个任务 |
| POST | `/mobius/sync` | 刷新 Mobius issue 缓存 |
| GET | `/mobius/issues` | 看缓存里有什么 |
| GET | `/report/{date}?generate=&force=` | 取整理稿，可顺手触发生成 |
| POST | `/report/{date}/generate?all_authors=` | cron 入口 |
| GET | `/stats?since=&until=` | 按人 / 任务 / agent / 配对方式聚合 |
| GET | `/day/{date}/{author}.md` | 只读日页（纯文本） |

## Mobius 接入

Mobius 对外是一个 **HTTP MCP 端点**（JSON-RPC over HTTP），不是普通 REST。
fecho 自己说 JSON-RPC，不引 SDK。

配对发生在每一次 `log_progress` 上，不能每次都问 Mobius——所以定时同步一次到本地缓存，
配对全在本地做。Mobius 挂了不影响记日志，只是配不到新 issue。

> ⚠️ 现在 `.env` 里是 Rachel 的**个人**测试凭证。正式部署要换成服务账号。

## cron

```bash
0  9 * * 1-5 curl -sX POST -H "Authorization: Bearer $TOK" localhost:8899/mobius/sync
30 19 * * 1-5 /path/to/scripe/scripts/cron_digest.sh >> /path/to/scripe/logs/cron.log 2>&1
```

## 还没做

- **只读 UI** —— 该有，看任务/日报/翻历史。提交入口仍然不放网页上。数据结构已经撑得住
- **timeoff 真对接** —— PTO 的管道分支（豁免/降级）已经跑通，数据源还是本地表
- **任务状态回写 Mobius** —— 现在只读不写
- **Slack 推送** —— 语音永远人录，文字稿要不要自动发待定
