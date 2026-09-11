#!/usr/bin/env python3
"""在一个临时 Postgres 上跑全部测试，确认云端版（Supabase）的 SQL 没问题。

数据库和测试必须在同一个进程里：临时 Postgres 会在启动它的进程退出时关掉，
分成两个进程的话，测试一开跑数据库就已经没了。

需要开发环境：
    python3 -m venv .venv && .venv/bin/pip install -e ".[cloud]" pgserver
用法：
    .venv/bin/python scripts/test_postgres.py [-q]
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

import pgserver
import psycopg

ROOT = Path(__file__).resolve().parents[1]

srv = pgserver.get_server(Path(tempfile.mkdtemp()) / "pg", cleanup_mode="stop")
base = srv.get_uri()
with psycopg.connect(base, autocommit=True) as c:
    c.execute("DROP DATABASE IF EXISTS fecho_test")
    c.execute("CREATE DATABASE fecho_test")

# 必须在 import fecho 之前设好：config 在导入时读环境变量
os.environ["FECHO_DATABASE_URL"] = re.sub(r"/[^/?]+(\?|$)", r"/fecho_test\1", base, count=1)
print("临时 Postgres 已就绪", flush=True)

sys.path.insert(0, str(ROOT))
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))  # 同 -s tests
verbosity = 0 if "-q" in sys.argv else 1
result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
