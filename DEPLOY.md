# 云端版部署（管理员）

三个地方，各管一件事：

| 地方 | 管什么 | 有没有密钥 |
|---|---|---|
| **Supabase** | 存所有人的数据 | 数据库密码 |
| **Vercel** | 网页、登录、接收 agent 上传 | 数据库地址、加密密钥 |
| **lu2** | 后台慢活：出日报、交叉验证 | 数据库地址、加密密钥、**LLM key** |

**两样东西三处必须一致：**

- **数据库地址** —— Vercel 收进来的数据，lu2 要读出来出日报
- **加密密钥** —— 同事登录时 Vercel 把他的 Mobius 授权加密存起来，lu2 同步 issue 时要解开。
  两边不一致，lu2 就解不开，所有人的 issue 都同步不了

所以本机 `~/.fecho/cloud.env` 是唯一的源头，Vercel 和 lu2 都从它生成。

**LLM key 只在 lu2**，Vercel 和同事电脑上都没有。

---

## 一、Supabase

1. 新建项目，Region 选 **East US (Ohio)**（和 lu2 同一个地区）
2. 保存好 **Database Password**
3. **Connect → Connection string → Transaction pooler**（端口 6543）
   - 不要用默认那条直连地址：新项目的直连只走 IPv6，lu2 和 Vercel 多半连不上
4. 在本机跑，把地址和密码存起来（顺便生成共享的加密密钥）：

   ```bash
   python3 ~/.fecho/reset_cloud_env.py
   ```

5. 建表（每张表都会打开行级安全，把 Supabase 默认的 Data API 那扇门关上）：

   ```bash
   source ~/.fecho/cloud.env && FECHO_HOME=/tmp/nohome .venv/bin/python -c "
   from fecho import db; db.init(); print('建好', len(db.table_names()), '张表')"
   ```

---

## 二、lu2（后台程序）

### 1. 在本机生成配置

```bash
python3 scripts/make_worker_env.py
```

数据库地址和密钥取自 `~/.fecho/cloud.env`，LLM 三项取自 `~/.fecho/config.json`，
结果写到 `~/.fecho/worker.env`。**不要手抄，抄错一个字符就是几小时的排查。**

### 2. 传上 lu2

```bash
scp ~/.fecho/worker.env <你的lu2地址>:/tmp/worker.env
ssh <你的lu2地址> 'sudo install -D -m 600 /tmp/worker.env /etc/fecho/worker.env && rm /tmp/worker.env'
```

### 3. 装并启动

在 lu2 上：

```bash
curl -fsSL https://raw.githubusercontent.com/RachelXiaolan/fecho/cloud/scripts/install_worker.sh -o /tmp/i.sh && bash /tmp/i.sh
```

它会：拉代码到 `/opt/fecho` → 建 venv 装依赖 → 装成开机自启的 systemd 服务 → 启动 → 打印状态。

### 4. 确认

```bash
sudo systemctl status fecho-worker      # 应该是 active (running)
sudo journalctl -u fecho-worker -f      # 看日志
```

启动日志会写明连的哪个库、用的哪个模型、并发几个。

### 以后改了代码

```bash
bash /opt/fecho/scripts/install_worker.sh --update
```

### 手动跑一轮（不等定时）

```bash
sudo systemctl stop fecho-worker
sudo -E env $(sudo cat /etc/fecho/worker.env | xargs) /opt/fecho/venv/bin/fecho worker --once
sudo systemctl start fecho-worker
```

---

## 三、Vercel（网页、登录、接收 agent 上传）

仓库里和 Vercel 有关的文件，不用改，知道它们是干什么的就行：

| 文件 | 作用 |
|---|---|
| `api/index.py` | Vercel 的入口，把 fecho 的网站建出来 |
| `vercel.json` | 所有路径都交给上面那个入口 |
| `pyproject.toml` 里的 `[dependency-groups] vercel` | Vercel 装依赖**只读 pyproject**，requirements.txt 它不看 |
| `.vercelignore` | 测试、文档这些不传上去 |

### 1. 导入仓库

vercel.com → **Add New → Project** → 选 `RachelXiaolan/fecho` → Application Preset 选 **Other** → Deploy。

第一次部署报 500 是正常的，环境变量还没填。

### 2. 生产分支改成 cloud

**Settings → Environments → Production → Branch Tracking** → 填 `cloud` → Save。

如果提示 "No deployments found for cloud"，说明这个分支上还没部署过：往 `cloud` 推一个提交，等它部署出来再点 Retry。

### 3. 填环境变量

在本机跑，打印出要填的 5 行：

```bash
python3 scripts/vercel_env.py https://fecho.techmob.net
```

**Settings → Environment Variables → Add** → 把 5 行整块粘进 **Key** 输入框（会自动拆开），Environments 三个都选 → Save。

| 变量 | 说明 |
|---|---|
| `FECHO_CLOUD` | `1` |
| `FECHO_DATABASE_URL` | 和 lu2 **必须一样** |
| `FECHO_SECRET_KEY` | 和 lu2 **必须一样** |
| `FECHO_PUBLIC_URL` | 对外网址，Mobius 登录完跳回这里 |
| `FECHO_BOOTSTRAP_ADMINS` | 第一批 admin 的邮箱，逗号分隔 |

改完任何环境变量都要 **Deployments → 最新一条 `···` → Redeploy** 才生效。

### 4. 挂域名 `fecho.techmob.net`

1. Vercel：**Settings → Domains → Add Domain** → `fecho.techmob.net` → 连到 Production。记下它给的 CNAME 值。
2. Cloudflare（techmob.net 的 DNS 在这里）：加一条记录
   - Type `CNAME`，Name `fecho`，Target 填上一步的值
   - **Proxy status 必须是 DNS only（灰色云朵）**。橙色代理会让 Vercel 签不下证书
3. 等一两分钟证书签下来，把 `FECHO_PUBLIC_URL` 改成 `https://fecho.techmob.net` → Redeploy。
4. 所有人重新登录一次。换了网址，Mobius 回调地址跟着变，代码会自动在 Mobius 重新注册，不用手动处理。

### 5. 验证

```bash
curl -s https://fecho.techmob.net/healthz
curl -s -o /dev/null -w "%{redirect_url}\n" https://fecho.techmob.net/auth/login
```

第一条应返回 `{"ok":true,...}`；第二条里的 `redirect_uri` 应该是 `https://fecho.techmob.net/auth/callback`。

---

## 出问题时

| 现象 | 先看哪里 |
|---|---|
| 日报没生成 | `sudo journalctl -u fecho-worker -n 100` |
| 所有人的 issue 都同步不上 | Vercel 和 lu2 的 `FECHO_SECRET_KEY` 是不是同一个 |
| 后台程序起不来 | `/etc/fecho/worker.env` 权限是不是 600、九项是不是齐 |
| 连不上数据库 | 在 lu2 上：`timeout 8 bash -c 'cat < /dev/null > /dev/tcp/<pooler主机>/6543'` |
| 调不通模型 | 在 lu2 上：`curl -s -o /dev/null -w "%{http_code}\n" http://api.feedmob.it.com/v1/models`，401 算通 |
| 网站所有路径都是 `FUNCTION_INVOCATION_FAILED` | 代码加载时就炸了。先看构建日志里依赖是不是从 pyproject 装的、有没有漏装 |
| 网站能打开，但面板报「请求失败（500）」 | Vercel 项目 → **Logs**，找那条 500 的 Python Traceback |
| 登录完跳回了旧网址 | `FECHO_PUBLIC_URL` 改了但没 Redeploy |
| 新域名证书一直签不下来 | Cloudflare 那条记录是不是开成了橙色代理 |
