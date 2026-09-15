"""本机采集在 Windows / Linux 上：路径、定时任务、编码、通知。

开发机是 Mac，Windows / Linux 的部分靠模拟 sys.platform 和系统命令来测。
这些测试证明的是「在那些系统上会调哪些命令、生成什么内容」，
不能代替在真 Windows、真 Linux 桌面上装一次。
"""
import _env  # noqa: F401  必须在 import fecho 之前
import importlib.util
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fecho import cloudscan  # noqa: E402
from test_cloud_auth import CloudCase, A  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "fecho" / "presets" / "local" / "fecho_local.py"


def load_script(home):
    os.environ["FECHO_LOCAL_HOME"] = str(home)
    try:
        spec = importlib.util.spec_from_file_location("fecho_local_xplat", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.pop("FECHO_LOCAL_HOME", None)


def done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fecho-xplat-"))
        self.mod = load_script(self.tmp / "home")
        self.script = self.tmp / "home" / "fecho_local.py"

    def platform(self, mac=False, windows=False):
        return mock.patch.multiple(self.mod, IS_MAC=mac, IS_WINDOWS=windows)


# ---------- 路径规则：本机脚本和服务器必须一模一样 ----------

IN_SCOPE_CASES = [
    ("/Users/a/work/proj", ["/Users/a/work"], True),
    ("/Users/a/work", ["/Users/a/work/"], True),
    ("/Users/a/work-backup", ["/Users/a/work"], False),
    ("/Users/a/Work/proj", ["/Users/a/work"], False),        # macOS / Linux 路径保持区分大小写
    ("C:\\Users\\a\\work\\proj", ["C:\\Users\\a\\work"], True),
    ("C:\\Users\\a\\work\\proj", ["C:/Users/a/work"], True),
    ("c:\\users\\A\\WORK\\proj", ["C:\\Users\\a\\work"], True),  # Windows 不分大小写
    ("C:\\Users\\a\\work-backup", ["C:\\Users\\a\\work"], False),
    ("D:\\work", ["C:\\work"], False),
    ("C:\\Users\\a", ["C:\\"], True),
    ("C:\\", ["C:\\"], True),
    ("", ["/"], False),
    ("relative/work", ["/Users/a"], False),
]

ABSOLUTE_CASES = [("/tmp/x", True), ("C:\\x", True), ("c:/x", True), ("C:", True),
                  ("relative\\x", False), ("~/work", False), ("", False)]


class TestPathRulesMatchServer(ScriptCase):
    def test_in_scope(self):
        for path, allowed, expected in IN_SCOPE_CASES:
            with self.subTest(path=path, allowed=allowed):
                self.assertEqual(self.mod.in_scope(path, allowed), expected, "本机脚本")
                self.assertEqual(cloudscan.in_scope(path, allowed), expected, "服务器")

    def test_absolute(self):
        for path, expected in ABSOLUTE_CASES:
            with self.subTest(path=path):
                self.assertEqual(self.mod.is_absolute(path), expected)
                self.assertEqual(cloudscan.is_absolute(path), expected)

    def test_normalized_the_same(self):
        for path in ("C:\\Users\\a\\work\\", "/Users/a/work/", "C:\\", "/", "D:/x//"):
            with self.subTest(path=path):
                self.assertEqual(self.mod.norm_path(path), cloudscan._norm_path(path))


class TestServerAcceptsWindowsFolders(CloudCase):
    """真会出事的地方：服务器以前只收 / 开头的路径，Windows 同事的文件夹一个都报不上来。"""

    def setUp(self):
        super().setUp()
        from fecho import accounts, db
        accounts.upsert_user(A, "Alice")
        with db.cursor() as c:
            c.execute("DELETE FROM work_folders")

    def test_report_select_and_scan_scope(self):
        cloudscan.report_folders(A, [{"path": "C:\\Users\\a\\work", "last_used": "2030-01-10"}])
        self.assertEqual([f["path"] for f in cloudscan.folders_of(A)], ["C:/Users/a/work"])
        self.assertEqual(cloudscan.set_selected(A, ["C:\\Users\\a\\work"]), ["C:/Users/a/work"])
        r = cloudscan.submit(A, [{"content": "Windows 上做完的事", "project": "c:\\users\\a\\work\\proj",
                                  "agent": "codex", "source_event_key": "scan:win"}], date="2030-01-10")
        self.assertEqual(r["recorded"], 1)

    def test_mac_paths_are_unchanged(self):
        cloudscan.report_folders(A, [{"path": "/Users/a/work/"}])
        self.assertEqual([f["path"] for f in cloudscan.folders_of(A)], ["/Users/a/work"])


class TestWorkFoldersOnWindows(ScriptCase):
    def test_windows_paths_are_reported_and_temp_folders_are_not(self):
        d = self.tmp / "projects" / "p"
        d.mkdir(parents=True)
        lines = [json.dumps({"type": "user", "cwd": cwd, "timestamp": "2030-01-10T02:00:00Z",
                             "message": {"role": "user", "content": "x"}})
                 for cwd in ("C:\\Users\\a\\work", "C:\\Users\\a\\AppData\\Local\\Temp\\tmp1", "/tmp/x")]
        (d / "s.jsonl").write_text("\n".join(lines), encoding="utf-8")
        with mock.patch.dict(self.mod.TRANSCRIPTS, {"claude-code": str(self.tmp / "projects" / "*" / "*.jsonl"),
                                                    "codex": str(self.tmp / "none" / "*.jsonl"),
                                                    "hermes": str(self.tmp / "none" / "*.jsonl")}):
            folders = self.mod.work_folders()
        self.assertEqual([f["path"] for f in folders], ["C:/Users/a/work"])


# ---------- 定时任务 ----------

class TestSchedules(ScriptCase):
    def which(self, *available):
        return mock.patch.object(self.mod.shutil, "which",
                                 side_effect=lambda name: "/usr/bin/" + name if name in available else None)

    def test_macos_still_uses_launchd(self):
        plist = self.tmp / "LaunchAgents" / "com.feedmob.fecho.cloud.plist"
        with self.platform(mac=True), mock.patch.object(self.mod, "PLIST", plist), \
             mock.patch.object(self.mod.subprocess, "run", return_value=done()):
            self.assertEqual(self.mod.install_schedule(self.script), "launchd")
        spec = plistlib.loads(plist.read_bytes())
        self.assertEqual(spec["ProgramArguments"][-2:], [str(self.script), "check"])
        self.assertEqual(spec["StartInterval"], 900)

    def test_linux_prefers_a_systemd_user_timer(self):
        unit_dir = self.tmp / "systemd"
        with self.platform(), self.which("systemctl", "crontab"), \
             mock.patch.object(self.mod, "SYSTEMD_DIR", unit_dir), \
             mock.patch.object(self.mod.subprocess, "run", return_value=done()) as run:
            self.assertEqual(self.mod.install_schedule(self.script), "systemd")
            self.assertTrue(self.mod.schedule_installed())
            calls = [c.args[0] for c in run.call_args_list]
            self.assertIn(["systemctl", "--user", "enable", "--now", "fecho-cloud.timer"], calls)
            timer = (unit_dir / "fecho-cloud.timer").read_text(encoding="utf-8")
            service = (unit_dir / "fecho-cloud.service").read_text(encoding="utf-8")
            self.assertIn("OnCalendar=*:0/15", timer)
            self.assertIn("Persistent=true", timer, "合盖错过的那次，开机后要补跑")
            self.assertIn('"%s" check' % self.script, service)

            self.assertEqual(self.mod.uninstall_schedule(), "systemd")
            self.assertFalse((unit_dir / "fecho-cloud.timer").exists())
            self.assertIn(["systemctl", "--user", "disable", "--now", "fecho-cloud.timer"],
                          [c.args[0] for c in run.call_args_list])

    def test_linux_without_systemd_falls_back_to_cron_and_keeps_other_jobs(self):
        state = {"tab": "MAILTO=a@b.c\n0 9 * * * backup.sh\n*/15 * * * * old-fecho # fecho-cloud\n"}

        def fake(args, **kw):
            if args[:3] == ["systemctl", "--user", "show-environment"]:
                return done(returncode=1)
            if args == ["crontab", "-l"]:
                return done(stdout=state["tab"])
            if args == ["crontab", "-"]:
                state["tab"] = kw["input"]
                return done()
            return done()

        with self.platform(), self.which("systemctl", "crontab"), \
             mock.patch.object(self.mod.subprocess, "run", side_effect=fake):
            self.assertEqual(self.mod.install_schedule(self.script), "cron")
            lines = state["tab"].splitlines()
            self.assertIn("0 9 * * * backup.sh", lines, "别人的定时任务不能动")
            ours = [line for line in lines if "# fecho-cloud" in line]
            self.assertEqual(len(ours), 1, "重装不能叠出两条")
            self.assertIn(str(self.script), ours[0])
            self.assertTrue(self.mod.schedule_installed())

            self.assertEqual(self.mod.uninstall_schedule(), "cron")
            self.assertNotIn("# fecho-cloud", state["tab"])
            self.assertIn("backup.sh", state["tab"])

    def test_no_scheduler_says_how_to_set_one_up(self):
        with self.platform(), self.which(), mock.patch.object(self.mod.subprocess, "run", return_value=done()):
            with self.assertRaises(SystemExit) as caught:
                self.mod.install_schedule(self.script)
        self.assertIn("check", str(caught.exception))

    def test_windows_uses_task_scheduler_with_pythonw(self):
        with self.platform(windows=True), \
             mock.patch.object(self.mod.sys, "executable", "C:\\Python312\\python.exe"), \
             mock.patch.object(self.mod.os.path, "exists", side_effect=lambda p: p.endswith("pythonw.exe")), \
             mock.patch.object(self.mod.subprocess, "run", return_value=done()) as run:
            self.assertEqual(self.mod.install_schedule(self.script), "schtasks")
            create, first_run = [c.args[0] for c in run.call_args_list][:2]
            self.assertEqual(create[:9], ["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "15",
                                          "/TN", "FechoCloudCheck"])
            task = create[create.index("/TR") + 1]
            self.assertIn("C:\\Python312\\pythonw.exe", task, "用 pythonw，不弹命令行窗口")
            self.assertIn(str(self.script), task)
            self.assertEqual(first_run, ["schtasks", "/Run", "/TN", "FechoCloudCheck"])
            self.assertTrue(self.mod.schedule_installed())

            self.assertEqual(self.mod.uninstall_schedule(), "schtasks")
            self.assertIn(["schtasks", "/Delete", "/F", "/TN", "FechoCloudCheck"],
                          [c.args[0] for c in run.call_args_list])

    def test_windows_python_falls_back_when_pythonw_is_missing(self):
        with mock.patch.object(self.mod.os.path, "exists", return_value=False):
            self.assertEqual(self.mod.windows_python("C:\\Python312\\python.exe"), "C:\\Python312\\python.exe")


# ---------- 其他会在 Windows 上出事的地方 ----------

class TestRobustness(ScriptCase):
    def test_agent_gets_utf8_even_where_the_system_default_is_gbk(self):
        cfg = {"runners": {"claude-code": "claude"}}
        with mock.patch.object(self.mod.subprocess, "run", return_value=done(stdout="NONE")) as run:
            self.mod.run_agent(cfg, "claude-code", "把登录做完了")
        kw = run.call_args.kwargs
        self.assertEqual(kw["encoding"], "utf-8")
        self.assertIn("把登录做完了", kw["input"])

    def test_windows_does_not_flash_a_console_window(self):
        with self.platform(windows=True), \
             mock.patch.object(self.mod.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            self.assertEqual(self.mod._no_window(), 0x08000000)
        with self.platform(mac=True):
            self.assertEqual(self.mod._no_window(), 0)

    def test_logging_works_without_a_console(self):
        """Windows 用 pythonw 跑定时任务时 sys.stdout 是 None。"""
        with mock.patch.object(self.mod.sys, "stdout", None):
            self.mod.log("没有控制台也要能写日志")
        self.assertIn("没有控制台也要能写日志", self.mod.LOG.read_text(encoding="utf-8"))

    def test_notification_text_never_becomes_part_of_a_command(self):
        evil = '"; rm -rf ~; echo "'
        with self.platform(mac=True):
            args, env = self.mod.notify_command(evil)
            self.assertEqual(args[-1], evil, "只作为参数传")
            self.assertFalse(any(evil in a for a in args[:-1]))
        with self.platform(windows=True):
            args, env = self.mod.notify_command(evil)
            self.assertFalse(any(evil in a for a in args), "Windows 走环境变量")
            self.assertEqual(env["FECHO_NOTIFY_TEXT"], evil)
        with self.platform(), mock.patch.object(self.mod.shutil, "which", return_value="/usr/bin/notify-send"):
            args, _ = self.mod.notify_command(evil)
            self.assertEqual(args, ["notify-send", "Fecho", evil])
        with self.platform(), mock.patch.object(self.mod.shutil, "which", return_value=None):
            self.assertEqual(self.mod.notify_command(evil), (None, None), "没有通知工具就不弹，只记日志")

    def test_finds_an_npm_installed_cli_on_windows(self):
        cmd = self.tmp / "npm" / "claude.cmd"
        cmd.parent.mkdir(parents=True)
        cmd.write_text("@echo off\n")
        cmd.chmod(0o755)
        with mock.patch.dict(self.mod.CLI, {"claude-code": {"name": "claude", "also": [str(cmd)]}}), \
             mock.patch.object(self.mod.shutil, "which", return_value=None):
            self.assertEqual(self.mod.find_cli("claude-code"), str(cmd))


class TestDashboardAcceptsWindowsPaths(unittest.TestCase):
    def test_manual_folder_input(self):
        html = (Path(__file__).resolve().parents[1] / "fecho" / "presets" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("[A-Za-z]:[\\\\/]", html)
        self.assertNotIn("if(!path.startsWith('/'))", html)


if __name__ == "__main__":
    unittest.main()
