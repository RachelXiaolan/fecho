"""Dashboard 的静态交互契约；浏览器验收负责视觉和点击行为。"""
import _env  # noqa: F401  必须在 import fecho 之前
from pathlib import Path
import unittest


HTML = (Path(__file__).resolve().parents[1] / "fecho" / "presets" / "dashboard.html")
PRESETS = HTML.parent
MARK_URL = "/assets/fecho-mark.svg"


class TestDashboardContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")

    def test_reports_are_rendered_not_shown_as_markdown_source(self):
        """日报以渲染后的文档显示，不再把 # ** [](...) 这些源码原样吐给人看。"""
        body = self.html[self.html.index("function renderReports"):]
        body = body[:body.index("function renderSystem")]
        self.assertIn("md(report.daily)", body)
        self.assertIn("innerHTML", body)
        self.assertNotIn("textContent=content", body)

    def test_markdown_renderer_escapes_before_marking_up(self):
        """日报内容来自模型输出。先转义再加标记，链接只放行 http(s)。"""
        inline = self.html[self.html.index("function mdInline"):]
        inline = inline[:inline.index("function md(")]
        self.assertLess(inline.index("esc(text)"), inline.index("<a href"),
                        "必须先转义再生成标签")
        self.assertIn("https?:", inline, "链接只放行 http/https")
        self.assertNotIn("\x00", self.html, "不能夹原始控制字符")

    def test_report_renderer_supports_ordered_tasks_and_nested_bullets(self):
        """标准的 `1. [ ] 内容` 是主写法；旧的反序写法可继续兼容；编号项下的 - 必须有圆点。"""
        renderer = self.html[self.html.index("function mdBlocks"):self.html.index("function enhanceMd")]
        self.assertIn("const li=taskFirst?", renderer)
        self.assertIn("const taskFirst=line.match(/^(\\s*)\\[(.)\\]\\s+(\\d+)\\.\\s*(.*)$/);", renderer)
        self.assertIn("taskFirst?[taskFirst[0],taskFirst[1],taskFirst[3],`[${taskFirst[2]}] ${taskFirst[4]}`]", renderer)
        self.assertIn(".report-paper li>ul{list-style:disc", self.html)

    def test_parent_list_folds_its_direct_child_even_when_nested_items_are_folded(self):
        """子项自己的折叠状态不能阻止父编号项收起整组子列表。"""
        renderer = self.html[self.html.index("function enhanceMd"):self.html.index("// ---- 看日报时记住折叠状态")]
        self.assertIn("function setListFold(host,folded)", renderer)
        self.assertIn("child.hidden=folded", renderer)
        self.assertIn("setListFold(host,!host.classList.contains('folded'))", self.html)

    def test_editor_preview_renders_standard_ordered_task_before_legacy_variant(self):
        """编辑器主路径要把 `1. [ ]` 显示成编号、勾选框、内容；旧写法只作兼容。"""
        editor = (Path(__file__).resolve().parents[1] / "editor" / "entry.js").read_text(encoding="utf-8")
        self.assertIn("else if ((m = /^(\\s*)([-*+]|\\d+[.)])(\\s+)\\[(.)\\]\\s?/.exec(text)))", editor)
        self.assertIn("if (/^\\d/.test(m[2]))", editor)
        self.assertIn("widget('num' + label", editor)
        self.assertIn("const taskFirst=/^(\\s*)\\[(.)\\](\\s+)(\\d+[.)])(\\s+)(.*)$/", editor)

    def test_editor_nested_list_guides_follow_every_ancestor_branch(self):
        """模板规则：直接子项已有父项引导线；更深一层再叠加直接父项的线。"""
        editor = (Path(__file__).resolve().parents[1] / "editor" / "entry.js").read_text(encoding="utf-8")
        self.assertIn("function addIndentGuides(line, add)", editor)
        self.assertIn("if (depth < 1 || !line.text.trim()) return", editor)
        self.assertIn("className = 'md-indent-guides'", editor)
        self.assertIn("for (let level = 0; level < depth; level++)", editor)
        self.assertIn("'md-indent-guide'", editor)

    def test_editor_fold_arrow_tracks_its_list_indent(self):
        """折叠箭头锚在当前项标记前的 gutter，不能用固定页面坐标压住编号或正文。"""
        editor = (Path(__file__).resolve().parents[1] / "editor" / "entry.js").read_text(encoding="utf-8")
        self.assertIn("const markerFrom = line.from + (/^[\\t ]*/.exec(line.text) || [''])[0].length", editor)
        self.assertIn("add(markerFrom, markerFrom, Decoration.widget", editor)
        self.assertIn("marginLeft: '-20px'", editor)

    def test_dashboard_has_no_external_dependencies(self):
        """面板要能断网打开，所以不引任何 CDN。"""
        import re
        self.assertIsNone(re.search(r'<script[^>]+src="https?:', self.html))
        self.assertIsNone(re.search(r'<link[^>]+href="https?:', self.html))

    def test_company_mark_is_used_for_the_sidebar_and_browser_tab(self):
        """页面内的品牌按钮和浏览器紫色 favicon 要是两种各司其职的用法。"""
        self.assertTrue((PRESETS / "assets" / "fecho-mark.svg").is_file())
        self.assertIn('<button class="brand" type="button" aria-label="Fecho brand" data-i18n-aria="brand_aria">', self.html)
        self.assertIn('class="brand-button"', self.html)
        self.assertIn('FEED WORK. ECHO PROGRESS.', self.html)
        self.assertIn('background:var(--butter)', self.html)
        self.assertIn('border-radius:50%', self.html)
        for page in ("dashboard.html", "login.html", "onboard.html", "guide.html"):
            html = (PRESETS / page).read_text(encoding="utf-8")
            self.assertIn('<link rel="icon" href="%s"' % MARK_URL, html, page)

    def test_taro_milk_design_system_has_complete_light_and_dark_tokens(self):
        """四个入口页必须共享奶油浅色和深葡萄紫黑夜模式，不能只改 Dashboard。"""
        for page in ("dashboard.html", "login.html", "onboard.html", "guide.html"):
            html = (PRESETS / page).read_text(encoding="utf-8")
            self.assertIn('--canvas:#fff9ec', html, page)
            self.assertIn(':root[data-theme="dark"]', html, page)
            self.assertIn('--canvas:#18121e', html, page)
            self.assertIn('"SF Pro Rounded"', html, page)
        self.assertIn('--taro:#a99bb5', self.html)
        self.assertIn('--butter:#f0c75f', self.html)
        self.assertIn('--primary:#f0c75f', self.html)

    def test_has_five_product_views(self):
        for view in ("today", "review", "tasks", "reports", "system"):
            self.assertIn('data-view="%s"' % view, self.html)

    def test_has_date_and_filter_controls_with_accessible_labels(self):
        self.assertIn('id="work-date"', self.html)
        self.assertIn('for="work-date"', self.html)
        for ident in ("agent-filter", "source-filter", "status-filter"):
            self.assertIn('id="%s"' % ident, self.html)
            self.assertIn('for="%s-trigger"' % ident, self.html)

    def test_topbar_filters_have_a_scoped_visual_contract(self):
        """顶部筛选保持等高网格、暖黄焦点和次级刷新操作，不能影响页面上的其他表单。"""
        self.assertIn('.toolbar{display:grid;', self.html)
        self.assertIn('grid-template-columns:136px 108px 142px 96px 62px', self.html)
        self.assertIn('.toolbar input,.filter-select-trigger{', self.html)
        self.assertIn('accent-color:var(--butter)', self.html)
        self.assertIn('.filter-menu{position:absolute', self.html)
        self.assertIn('#reload-day{align-self:end', self.html)

    def test_dashboard_toolbar_reflows_before_mobile_breakpoint(self):
        """641–1100px 仍是桌面时，筛选器必须重排，不能挤出横向滚动条。"""
        self.assertIn('@media(max-width:1100px) and (min-width:641px){', self.html)
        self.assertIn('.topbar-actions{flex:1 1 520px;display:grid;grid-template-columns:minmax(0,1fr) auto auto;', self.html)
        self.assertIn('.toolbar,.shell.sidebar-expanded .toolbar{grid-column:1/-1;grid-template-columns:repeat(2,minmax(0,1fr));', self.html)
        self.assertIn('.toolbar .field:first-child{grid-column:1/-1}', self.html)

    def test_global_filters_use_custom_listboxes_but_keep_native_select_values(self):
        """菜单面板可控，筛选值仍由原有 select 和 change 事件提供。"""
        for ident in ("agent-filter", "source-filter", "status-filter"):
            self.assertIn('data-filter-select="%s"' % ident, self.html)
            self.assertIn('id="%s-trigger"' % ident, self.html)
            self.assertIn('id="%s-menu"' % ident, self.html)
        self.assertIn('role="listbox"', self.html)
        self.assertIn('aria-haspopup="listbox"', self.html)
        self.assertIn('function syncFilterMenu(select)', self.html)
        self.assertIn("select.dispatchEvent(new Event('change',{bubbles:true}))", self.html)
        self.assertIn("event.key==='ArrowDown'", self.html)
        self.assertIn("event.key==='Escape'", self.html)
        self.assertIn("!event.target.closest('.filter-select')", self.html)

    def test_filter_menus_wait_for_dashboard_options_and_align_to_their_field(self):
        """数据尚未回来时不能打开空菜单；菜单始终从触发器左边缘展开。"""
        for ident in ("agent-filter", "source-filter", "status-filter"):
            self.assertIn('data-filter-trigger="%s" disabled' % ident, self.html)
        self.assertIn('function setFilterControlsReady(ready)', self.html)
        self.assertIn('trigger.disabled=!ready', self.html)
        self.assertIn('setFilterControlsReady(false)', self.html)
        self.assertIn('setFilterControlsReady(true)', self.html)
        self.assertIn('.filter-menu{position:absolute;z-index:30;top:calc(100% + 7px);left:0;', self.html)

    def test_custom_filter_triggers_keep_the_date_field_shape(self):
        """自定义筛选按钮沿用日期控件的圆角与紫色描边，展开时不变成黄边。"""
        self.assertIn('.filter-select-trigger{position:relative;border-radius:7px;', self.html)
        self.assertIn('.filter-select-trigger:focus,.filter-select.open .filter-select-trigger{border-color:var(--taro)', self.html)
        self.assertIn('filter-select.open .filter-select-trigger::after{transform:translateY(3px) rotate(225deg);border-color:var(--muted)', self.html)

    def test_topbar_controls_share_the_purple_pressed_surface(self):
        """顶部控件采用主按钮的压边质感，但保持芋紫而非暖黄色。"""
        self.assertIn('--taro-deep:#796288', self.html)
        self.assertIn('--taro-deep:#8e7798', self.html)
        self.assertIn('.topbar .toolbar input,.topbar .filter-select-trigger,.topbar #reload-day,.topbar .lang-switch,.topbar .theme-toggle{', self.html)
        self.assertIn('background:var(--taro);color:var(--primary-ink);box-shadow:0 2px 0 var(--taro-deep)', self.html)
        self.assertIn('.topbar .toolbar input:active,.topbar .filter-select-trigger:active,.topbar #reload-day:active,.topbar .lang-switch:active,.topbar .theme-toggle:active{transform:translateY(2px);box-shadow:0 0 0 var(--taro-deep)}', self.html)

    def test_dashboard_language_switch_is_a_single_compact_toggle(self):
        """Dashboard 只保留一个与主题开关同尺寸的语言按钮。"""
        self.assertIn('<button class="lang-switch" id="lang-toggle" type="button"', self.html)
        self.assertIn("langToggle.textContent=lang==='en'?'EN':'中'", self.html)
        self.assertIn("event.target.closest('#lang-toggle')", self.html)
        self.assertNotIn('<div class="lang-switch" role="group"', self.html)

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
        # 任务列表和 Today 用同一个 matches 过滤。变量名不重要（t 现在是翻译函数，这里改叫 x）
        self.assertIn('.updates.some(matches)', self.html)

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
