# Fecho 安装与使用指南

Fecho 让 **agent 边干活边记进展**，每晚自动整理成日报和口播稿。

你要做的只有两件事：装一次，然后每天晚上花两分钟看一眼日报对不对。

---

## 零、先确认你符合条件

| 条件 | 怎么确认 |
|---|---|
| **macOS** | 自动任务用的是 macOS 的 launchd，其他系统装不了后台服务 |
| **Python 3.9 以上** | 终端跑 `python3 --version` 看一下 |
| **能连公司内网** | 跑 `curl -s -o /dev/null -w "%{http_code}" http://api.feedmob.it.com`，有响应就行 |
| **用 Claude Code / Codex / Hermes 之一干活** | Fecho 靠读这些工具的会话记录来自动记进展 |
| **拿到配置文件** | 找 Rachel 要 `fecho-团队LLM配置.json`。**里面有密钥，别转发** |

---

## 一、安装

```bash
python3 -m venv ~/.fecho/venv
~/.fecho/venv/bin/pip install "fecho[server] @ git+https://github.com/RachelXiaolan/fecho@main"
echo 'export PATH="$HOME/.fecho/venv/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

**`[server]` 不能省** —— 少了它面板起不来。

验证：

```bash
fecho --version
```

应该输出 `0.6.1` 或更高。

**如果提示 `command not found: fecho`**，说明 PATH 没生效（新开的终端窗口、
或者你用的是 bash 不是 zsh）。两个办法：重开一个终端窗口，或者直接用全路径 ——
后面所有命令把 `fecho` 换成 `~/.fecho/venv/bin/fecho` 都能用。

---

## 二、初始化

把下面这行的三个地方换成你自己的，然后执行：

```bash
fecho onboard \
  --author 你的英文名 \
  --display-name "你的中文名" \
  --mobius-assignee 你的邮箱@feedmob.com \
  --work-prefix ~/Documents/work \
  --shared-config ~/Downloads/fecho-团队LLM配置.json
```

| 参数 | 填什么 | 举例 |
|---|---|---|
| `--author` | 稳定的英文标识，以后不要改 | `rachel` |
| `--display-name` | 日报上显示的名字 | `"Rachel Lu"` |
| `--mobius-assignee` | 你的 Mobius 邮箱 | `rachel.lu@feedmob.com` |
| `--work-prefix` | **你放工作仓库的父目录** | `~/Documents/work` |
| `--shared-config` | 刚拿到的配置文件路径 | `~/Downloads/fecho-团队LLM配置.json` |

**关于 `--work-prefix`：这是隐私边界。** Fecho 默认**不读任何目录**，只有这里登记过的才会被扫描。
个人项目、私人笔记不要放进来。可以写多次登记多个目录。

执行过程中会：

1. 读共享配置，配好 LLM
2. 给检测到的 agent（Claude Code / Codex / Hermes）注册 Fecho
3. **打开浏览器让你登录 Mobius** ← 这一步要你点授权，每个人必须自己登
4. 装两个后台服务（每日任务 + 面板）
5. 跑一次自检

想先看看会做什么而不真的执行，加 `--dry-run`。

---

## 三、确认装好了

```bash
fecho doctor
```

**这几项必须是 ✓：**

```
✓ 存储
✓ Mobius 连接
✓ issue 缓存
✓ LLM（出日报用）
✓ 工作范围
```

`项目绑定` 和 `团队协作` 是 ✗ 没关系，那是可选的。

再看一眼后台服务：

```bash
fecho schedule status
```

应该显示已启用、每天 21:00（北京时间）。

打开面板：

```bash
open http://127.0.0.1:8900
```

---

## 四、平时怎么用

### 平时：什么都不用做

在登记过的目录里正常干活就行。你的 agent 会在做完一件事时自己记一条，
晚上 21:00 系统会把当天的整理成日报。

### 想让 agent 立刻记一条

直接跟它说：

> 把刚才这个问题的解决办法记到工作日志里

### 想知道昨天干到哪了

开工时跟 agent 说：

> catch up

它会拉回昨天的日报和还挂着的任务。

### 晚上：花两分钟看一眼

打开 http://127.0.0.1:8900

| 视图 | 干什么 |
|---|---|
| **Today** | 今天记了什么，按任务分组 |
| **Review** | **改错的地方** —— 内容写错了、归到错的 issue 了，在这里改 |
| **Tasks** | 任务做完了点完成；一件事被拆成两个任务可以合并 |
| **Reports** | 日报和口播稿，可以重新生成 |
| **System** | 出问题时来这里看原因 |

**Review 这一步是关键。** 系统判断归属会出错，你改一次它就记住了，
之后模型不会再覆盖你的判断。

---

## 五、验收清单

装完之后，请按这几条试一遍，有问题告诉 Rachel：

- [ ] `fecho doctor` 五项核心检查都是 ✓
- [ ] 打开 http://127.0.0.1:8900 能看到面板
- [ ] 跟 agent 说"记一条工作日志"，Today 视图里能看到
- [ ] 在 Review 视图里改一条的归属，改完刷新还在
- [ ] 干一天活之后，第二天早上能看到前一天的日报
- [ ] 日报内容大致对得上你那天实际做的事

**最后一条是重点：** 请具体说哪里不对 —— 是漏记了、多记了、还是归错任务了。
这比"感觉不太准"有用得多。

---

## 六、出问题怎么办

### 日报没生成

```bash
fecho schedule status
```

看 `last_result`。如果是 `failed`，会显示原因。

**最常见的原因是 21:00 时电脑没网**（在路上、睡眠中）。这种情况系统会
**30 分钟后自动补跑一次**，并弹通知告诉你。补跑也失败的话，第二天手动补：

```bash
fecho digest --date 2026-09-11
```

### 有些活没被记上

多半是那个目录没登记：

```bash
fecho scope                      # 看当前登记了哪些
fecho scope --work .             # 把当前目录加进去
```

### 归属老是错

在做某个项目本身时，说的话和 issue 标题字面上常常一个词都不重合。
在项目目录里绑一次：

```bash
fecho bind AI-2541
```

之后这个目录的进展默认归它。

### 面板打不开

```bash
fecho schedule install --time 21:00
```

还是不行的话，多半是上一个进程占着端口没退干净：

```bash
lsof -ti:8900 | xargs kill -9
fecho schedule install --time 21:00
```

### 想看被系统判为重复而没进日报的内容

```bash
fecho hidden                     # 列出来
fecho hidden --restore <id>      # 捞回某一条
```

---

## 七、你的数据在哪

**全部在你自己电脑上**，路径 `~/.fecho/`：

```
~/.fecho/fecho.db      进展和任务
~/.fecho/logs/         日报和口播稿的 markdown
~/.fecho/config.json   配置（含密钥，权限 600）
```

面板只监听 `127.0.0.1`，外面连不进来。

**唯一会离开你电脑的**是日报和口播稿成品，前提是你配了团队汇总（默认没配）。
原始进展记录永远不会外发 —— 汇总端的数据库里根本没有存它们的表。

不想用了：

```bash
fecho schedule uninstall         # 卸掉后台服务
rm -rf ~/.fecho                  # 删掉所有数据
```
