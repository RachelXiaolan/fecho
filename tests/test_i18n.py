"""网页界面的中英文切换。

只切界面文字：服务器传回的内容（错误信息、检查说明、日报正文）不翻。
这里盯三件事：
1. 中文和英文两套 key 完全一样——漏一个，切到英文就会冒出一个 key 名
2. 页面里用到的每个 key 都在字典里
3. 界面上没有漏掉、没走字典的中文（写死在标签或脚本里的）
"""
import _env  # noqa: F401  必须在 import fecho 之前
import json
import re
import unittest
from pathlib import Path

PRESETS = Path(__file__).resolve().parents[1] / "fecho" / "presets"
PAGES = ("dashboard.html", "login.html", "onboard.html")
CJK = re.compile(r"[一-鿿]")


def load(name):
    html = (PRESETS / name).read_text(encoding="utf-8")
    raw = re.search(r'<script type="application/json" id="i18n">(.*?)</script>', html, re.S).group(1)
    return html, json.loads(raw)


def body_without_dictionary(html):
    body = html[html.index("<body>"):]
    body = re.sub(r'<script type="application/json" id="i18n">.*?</script>', "", body, flags=re.S)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    return body


def strip_js_comments(code):
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in code.splitlines())


class TestDictionaries(unittest.TestCase):
    def test_both_languages_have_the_same_keys(self):
        for name in PAGES:
            _, d = load(name)
            self.assertEqual(set(d), {"zh", "en"}, name)
            missing_en = sorted(set(d["zh"]) - set(d["en"]))
            missing_zh = sorted(set(d["en"]) - set(d["zh"]))
            self.assertEqual((missing_en, missing_zh), ([], []), name)

    def test_english_has_no_chinese(self):
        for name in PAGES:
            _, d = load(name)
            leaked = {k: v for k, v in d["en"].items() if CJK.search(v)}
            self.assertEqual(leaked, {}, name)

    def test_placeholders_match_between_languages(self):
        """{n}、{time} 这种占位符两边要一样，不然英文版会少数字、多出花括号。"""
        for name in PAGES:
            _, d = load(name)
            for key in d["zh"]:
                self.assertEqual(sorted(re.findall(r"\{(\w+)\}", d["zh"][key])),
                                 sorted(re.findall(r"\{(\w+)\}", d["en"][key])), "%s: %s" % (name, key))

    def test_every_key_used_exists(self):
        for name in PAGES:
            html, d = load(name)
            used = set(re.findall(r'data-i18n(?:-aria|-placeholder)?="(\w+)"', html))
            # 后面跟着 + 的是拼接出来的 key（t('confidence_'+级别)），下面单独查前缀
            used |= set(re.findall(r"\bt\('(\w+)'(?!\s*\+)", html))
            # 拼出来的 key：t('page_'+view) 这类
            dynamic_prefixes = ("page_", "status_", "confidence_", "task_status_")
            for key in used:
                self.assertIn(key, d["zh"], "%s 用了字典里没有的 %s" % (name, key))
            for prefix in dynamic_prefixes:
                if ("t('%s'+" % prefix) in html:
                    self.assertTrue(any(k.startswith(prefix) for k in d["zh"]), prefix)
        _, d = load("dashboard.html")
        for view in ("today", "review", "tasks", "reports", "system", "settings", "admin"):
            self.assertIn("page_%s" % view, d["zh"])
            self.assertIn("page_%s_sub" % view, d["zh"])


class TestNoHardcodedChinese(unittest.TestCase):
    def test_markup_and_scripts_go_through_the_dictionary(self):
        # 允许的例外：语言按钮自己的名字「中文」；服务器给的检查项名字（按中文名去找那一项）
        allowed = ("中文", "中", "每日自动整理")
        for name in PAGES:
            html, _ = load(name)
            body = body_without_dictionary(html)
            parts = re.split(r"(<script>.*?</script>)", body, flags=re.S)
            for part in parts:
                text = strip_js_comments(part) if part.startswith("<script>") else part
                for a in allowed:
                    text = text.replace(a, "")
                found = CJK.findall(text)
                self.assertEqual(found, [], "%s 里有写死的中文：%s" % (
                    name, text[max(0, text.find(found[0]) - 40):text.find(found[0]) + 40] if found else ""))

    def test_language_choice_is_shared_and_defaults_to_chinese(self):
        for name in PAGES:
            html, _ = load(name)
            self.assertIn("'fecho-lang'", html, name)
            self.assertIn("=== 'en' ? 'en' : 'zh'" if name != "dashboard.html" else "==='en'?'en':'zh'", html, name)
            if name == "dashboard.html":
                self.assertIn('id="lang-toggle"', html)
                self.assertNotIn('data-lang="zh"', html)
                self.assertNotIn('data-lang="en"', html)
            else:
                self.assertIn('data-lang="zh"', html)
                self.assertIn('data-lang="en"', html)


if __name__ == "__main__":
    unittest.main()
