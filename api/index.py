"""Vercel 的入口。

Vercel 跑 Python 的规矩：这个文件里叫 app 的那个变量就是网站本身，
每来一个请求它就在一个短命的小进程里被调用一次。所以这里只做两件事：
把仓库根目录加进 import 路径（fecho 包在那儿），然后把网站建出来。

注意这里**不建表**。建表是一次性的事（本机 `db.init()` 已经做过了），
放在这儿等于每次冷启动都跑一遍改表语句，慢且没必要。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Vercel 的磁盘是只读的，只有 /tmp 能写。fecho 默认想往 ~/.fecho 写东西，
# 指到 /tmp 去，免得哪条不常走的代码路径突然去写盘就 500。
os.environ.setdefault("FECHO_HOME", "/tmp/fecho")

from fecho.web import build_app  # noqa: E402

app = build_app()
