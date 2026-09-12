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

## 三、Vercel

（第 5 步做完界面后补，含自定义域名 `fecho.techmob.net` 的配置。）

---

## 出问题时

| 现象 | 先看哪里 |
|---|---|
| 日报没生成 | `sudo journalctl -u fecho-worker -n 100` |
| 所有人的 issue 都同步不上 | Vercel 和 lu2 的 `FECHO_SECRET_KEY` 是不是同一个 |
| 后台程序起不来 | `/etc/fecho/worker.env` 权限是不是 600、九项是不是齐 |
| 连不上数据库 | 在 lu2 上：`timeout 8 bash -c 'cat < /dev/null > /dev/tcp/<pooler主机>/6543'` |
| 调不通模型 | 在 lu2 上：`curl -s -o /dev/null -w "%{http_code}\n" http://api.feedmob.it.com/v1/models`，401 算通 |
