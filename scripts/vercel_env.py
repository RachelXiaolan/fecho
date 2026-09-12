#!/usr/bin/env python3
"""打印要填进 Vercel 的环境变量。

Vercel 那个添加环境变量的框支持整块粘贴 KEY=VALUE，所以这里直接输出成那个格式。
值全部取自 ~/.fecho/cloud.env —— 和 lu2 上用的是同一份，手抄会抄错。

    python3 scripts/vercel_env.py                      # 用默认的对外网址
    python3 scripts/vercel_env.py https://xxx.vercel.app   # 先用 Vercel 给的临时网址

注意：LLM key 不在这里。那东西只该在 lu2 上。
"""
import sys
from pathlib import Path

PUBLIC_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "https://fecho.techmob.net"
ADMIN = "rachel.lu@feedmob.com"

src = Path.home() / ".fecho" / "cloud.env"
if not src.exists():
    raise SystemExit("没有 %s，先跑 python3 ~/.fecho/reset_cloud_env.py" % src)

vals = {}
for line in src.read_text(encoding="utf-8").splitlines():
    line = line.strip().removeprefix("export ").strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    vals[k.strip()] = v.strip().strip("'\"")

missing = [k for k in ("FECHO_DATABASE_URL", "FECHO_SECRET_KEY") if not vals.get(k)]
if missing:
    raise SystemExit("%s 里缺 %s" % (src, "、".join(missing)))

for k, v in (
    ("FECHO_CLOUD", "1"),
    ("FECHO_DATABASE_URL", vals["FECHO_DATABASE_URL"]),
    ("FECHO_SECRET_KEY", vals["FECHO_SECRET_KEY"]),
    ("FECHO_PUBLIC_URL", PUBLIC_URL),
    ("FECHO_BOOTSTRAP_ADMINS", ADMIN),
):
    print("%s=%s" % (k, v))
