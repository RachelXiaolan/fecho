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


if __name__ == "__main__":
    unittest.main(verbosity=2)
