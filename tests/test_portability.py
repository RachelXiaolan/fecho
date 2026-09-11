"""SQL 可移植性：本机版 SQLite 和云端版 Postgres 共用同一套 SQL。

真跑 Postgres 要起数据库（见 scripts/test_postgres.sh），这里做两件不依赖数据库的事：
盯住只有 SQLite 认的写法别再混进来，以及占位符转换本身。
"""
import _env  # noqa: F401  必须在 import fecho 之前
import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "fecho"

# 这些写法 Postgres 不认。每一条都是真踩过的：搬到 Postgres 时 13 个测试就是挂在这。
SQLITE_ONLY = {
    r"INSERT\s+OR\s+(REPLACE|IGNORE)": "改用 INSERT ... ON CONFLICT",
    r"\bdate\(\s*'now'": "日期在 Python 里按北京时间算好再传",
    r"\bdate\(\s*\?": "日期在 Python 里算好再传",
    r"ORDER BY[^\"']*\browid\b": "rowid 只有 SQLite 有，换成主键",
}


class TestNoSqliteOnlySql(unittest.TestCase):
    def test_business_code_uses_portable_sql(self):
        # collector.py 是本机团队汇总端，只跑 SQLite；db.py 里的 PRAGMA 在 SQLite 分支里
        skip = {"collector.py"}
        hits = []
        for path in sorted(SRC.glob("*.py")):
            if path.name in skip:
                continue
            code = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                             if not l.lstrip().startswith("#"))
            for pattern, fix in SQLITE_ONLY.items():
                for m in re.finditer(pattern, code, re.I):
                    hits.append("%s: %r → %s" % (path.name, m.group(0), fix))
        self.assertEqual(hits, [], "\n".join(hits))


class TestPlaceholderTranslation(unittest.TestCase):
    def test_question_marks_become_psycopg_placeholders(self):
        from fecho import db
        self.assertEqual(db._pg_sql("SELECT * FROM t WHERE a=? AND b=?"),
                         "SELECT * FROM t WHERE a=%s AND b=%s")

    def test_literal_percent_is_escaped(self):
        """LIKE '%x%' 里的 % 不转义会被 psycopg 当成占位符。"""
        from fecho import db
        self.assertEqual(db._pg_sql("SELECT * FROM t WHERE a LIKE '%x%' AND b=?"),
                         "SELECT * FROM t WHERE a LIKE '%%x%%' AND b=%s")

    def test_postgres_schema_swaps_only_autoincrement(self):
        from fecho import db
        pg = db._schema_for("postgres")
        self.assertNotIn("AUTOINCREMENT", pg)
        self.assertIn("BIGSERIAL PRIMARY KEY", pg)
        self.assertEqual(pg.count("CREATE TABLE"), db.SCHEMA.count("CREATE TABLE"))


if __name__ == "__main__":
    unittest.main()
