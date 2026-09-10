"""Dashboard 的静态交互契约；浏览器验收负责视觉和点击行为。"""
from pathlib import Path
import unittest


HTML = (Path(__file__).resolve().parents[1] / "fecho" / "presets" / "dashboard.html")


class TestDashboardContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")

    def test_has_five_product_views(self):
        for view in ("today", "review", "tasks", "reports", "system"):
            self.assertIn('data-view="%s"' % view, self.html)

    def test_has_date_and_filter_controls_with_accessible_labels(self):
        for ident in ("work-date", "agent-filter", "source-filter", "status-filter"):
            self.assertIn('id="%s"' % ident, self.html)
            self.assertIn('for="%s"' % ident, self.html)

    def test_has_loading_error_and_live_feedback_regions(self):
        self.assertIn('id="loading"', self.html)
        self.assertIn('id="error-state"', self.html)
        self.assertIn('role="status"', self.html)
        self.assertIn('aria-live="polite"', self.html)

    def test_uses_single_dashboard_payload_and_checks_http_errors(self):
        self.assertIn("/api/dashboard", self.html)
        self.assertIn("if (!response.ok)", self.html)

    def test_supports_report_dirty_state_and_undo_feedback(self):
        self.assertIn('id="report-dirty"', self.html)
        self.assertIn('id="undo-action"', self.html)

    def test_mobile_layout_has_a_bottom_navigation_mode(self):
        self.assertIn('@media(max-width:640px)', self.html)
        self.assertIn('bottom:0', self.html)

    def test_task_view_applies_the_same_global_filters(self):
        self.assertIn('t.updates.some(matches)', self.html)

    def test_visible_dashboard_refreshes_after_nightly_automation(self):
        self.assertIn("visibilitychange", self.html)
        self.assertIn("window.addEventListener('focus'", self.html)
        self.assertIn("setInterval", self.html)
        self.assertIn("document.visibilityState==='visible'", self.html)

    def test_system_view_renders_beijing_schedule_and_last_result(self):
        self.assertIn('id="automation-status"', self.html)
        self.assertIn("daily_time", self.html)
        self.assertIn("last_result", self.html)
        self.assertIn("configured_launch_agents", self.html)
        self.assertIn("automation.runtime", self.html)
        self.assertIn("运行正常", self.html)
        self.assertIn("服务异常", self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
