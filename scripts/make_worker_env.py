#!/usr/bin/env python3
"""在本机拼出 lu2 后台程序要的配置文件，然后你把它传上去。

数据库地址和加密密钥从 ~/.fecho/cloud.env 取，LLM 那三项从 ~/.fecho/config.json 取——
都从已有的地方读，避免手抄出错。尤其是加密密钥：Vercel 和 lu2 必须是同一个，
不一致的话 lu2 解不开同事的 Mobius 授权，所有人的 issue 都同步不了。

用法：python3 scripts/make_worker_env.py [--public-url https://fecho.techmob.net]
"""
import argparse
import json
import os
import pathlib
import re
import sys

HOME = pathlib.Path.home()
OUT = HOME / ".fecho" / "worker.env"

ap = argparse.ArgumentParser()
ap.add_argument("--public-url", default="https://fecho.techmob.net")
args = ap.parse_args()

cloud = (HOME / ".fecho" / "cloud.env")
if not cloud.exists():
    sys.exit("没找到 ~/.fecho/cloud.env，先跑 reset_cloud_env.py")
env = dict(re.findall(r"export (\w+)=['\"]?([^'\"\n]+)", cloud.read_text(encoding="utf-8")))

cfg = json.loads((HOME / ".fecho" / "config.json").read_text(encoding="utf-8"))
values = {
    "FECHO_CLOUD": "1",
    "FECHO_DATABASE_URL": env.get("FECHO_DATABASE_URL", ""),
    "FECHO_SECRET_KEY": env.get("FECHO_SECRET_KEY", ""),
    "FECHO_PUBLIC_URL": args.public_url,
    "FECHO_LLM_BASE_URL": cfg.get("llm_base_url", ""),
    "FECHO_LLM_API_KEY": cfg.get("llm_api_key", ""),
    "FECHO_LLM_MODEL": cfg.get("llm_model", ""),
    "FECHO_MOBIUS_URL": cfg.get("mobius_url", "https://mobius.feedmob.com/api/mcp"),
    "FECHO_WORKER_CONCURRENCY": "3",
}
missing = [k for k, v in values.items() if not v]
if missing:
    sys.exit("这几项没取到，检查一下来源文件：%s" % "、".join(missing))

OUT.write_text("".join("%s=%s\n" % kv for kv in values.items()), encoding="utf-8")
os.chmod(OUT, 0o600)
print("已生成 %s（权限 600）" % OUT)
for k, v in values.items():
    shown = "<%d 字符>" % len(v) if ("KEY" in k or "URL" in k and "@" in v) else v
    print("  %-26s %s" % (k, shown))
