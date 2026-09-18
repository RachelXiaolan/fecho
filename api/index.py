"""Vercel 的入口。

Vercel 跑 Python 的规矩：这个文件里叫 app 的那个变量就是网站本身，
每来一个请求它就在一个短命的小进程里被调用一次。所以这里只做两件事：
把仓库根目录加进 import 路径（fecho 包在那儿），然后把网站建出来。

这里不执行全量 `db.init()`：建表迁移通常由部署流程完成，避免每次冷启动都跑完整套改表语句。
需要自愈的轻量功能（例如快速 API）会在首次使用时只创建自己的表。
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
