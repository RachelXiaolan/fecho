import _env  # noqa: F401  必须在 import fecho 之前
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestPackageContents(unittest.TestCase):
    def test_dashboard_template_is_declared_as_package_data(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        package_data = pyproject.split("[tool.setuptools.package-data]", 1)[1]
        self.assertRegex(
            package_data,
            re.compile(r'fecho\s*=\s*\[[^\]]*"presets/dashboard\.html"', re.S),
        )

    def test_onboarding_skill_is_declared_as_package_data(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        package_data = pyproject.split("[tool.setuptools.package-data]", 1)[1]
        self.assertIn('"presets/skill/*.md"', package_data)

    def test_version_is_consistent_everywhere(self):
        """两处版本号必须一致。

        不写死具体数字——盯死数字的测试每次发版都得跟着改，改的时候顺手改对
        反而掩盖了真问题：pyproject 和 __init__ 不一致时，pip 装出来的版本
        和代码自报的版本对不上，没法判断同事装的到底是哪一版。
        """
        from fecho import __version__

        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        self.assertIn('version = "%s"' % __version__, pyproject)


if __name__ == "__main__":
    unittest.main(verbosity=2)
