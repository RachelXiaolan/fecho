"""Onboarding and unattended automation contracts."""
import os
import json
import plistlib
import signal
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


TMP = tempfile.mkdtemp(prefix="fecho-onboarding-test-")
os.environ.setdefault("FECHO_HOME", os.path.join(TMP, "home"))
os.environ.setdefault("FECHO_DB", os.path.join(TMP, "fecho.db"))
os.environ.setdefault("FECHO_LOGS_DIR", os.path.join(TMP, "logs"))
os.environ.setdefault("FECHO_LLM_BASE_URL", "")
os.environ.setdefault("FECHO_LLM_API_KEY", "")
os.environ.setdefault("FECHO_MOBIUS_URL", "")
os.environ.setdefault("FECHO_MOBIUS_TOKEN", "")

from fecho import store  # noqa: E402


class TestBeijingClock(unittest.TestCase):
    def test_utc_evening_is_next_beijing_calendar_day(self):
        from fecho import clock

        instant = datetime(2030, 1, 1, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(clock.today(instant), "2030-01-02")
        self.assertEqual(clock.from_iso("2030-01-01T16:30:00Z").hour, 0)

    def test_store_today_uses_product_clock(self):
        with mock.patch("fecho.clock.today", return_value="2030-01-02"):
            self.assertEqual(store.today(), "2030-01-02")

    def test_daily_time_must_be_before_2200_beijing(self):
        from fecho import clock

        for value in ("00:00", "09:05", "21:00", "21:59"):
            self.assertEqual(clock.validate_daily_time(value), value)
        for value in ("22:00", "23:59", "9:00", "21:60", "tomorrow"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "22:00"):
                    clock.validate_daily_time(value)


class TestDailyAutomation(unittest.TestCase):
    def setUp(self):
        from fecho import automation

        self.automation = automation
        self.state = {"enabled": True, "daily_time": "21:00"}

    def test_tick_runs_once_during_beijing_grace_window(self):
        instant = datetime(2030, 1, 1, 13, 7, tzinfo=timezone.utc)  # 21:07 Beijing
        saved = []
        runner = mock.Mock(return_value={"ok": True, "date": "2030-01-01"})

        first = self.automation.tick(
            instant=instant, state=dict(self.state), runner=runner,
            save=lambda value: saved.append(value))
        second = self.automation.tick(
            instant=instant, state=saved[-1], runner=runner,
            save=lambda value: saved.append(value))

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "already-run")
        runner.assert_called_once_with("2030-01-01")

    def test_tick_does_not_run_before_or_long_after_selected_time(self):
        runner = mock.Mock()
        for instant in (
            datetime(2030, 1, 1, 12, 59, tzinfo=timezone.utc),
            datetime(2030, 1, 1, 13, 10, tzinfo=timezone.utc),
        ):
            result = self.automation.tick(
                instant=instant, state=dict(self.state), runner=runner,
                save=lambda value: None)
            self.assertEqual(result["status"], "not-due")
        runner.assert_not_called()

    def test_failed_tick_is_visible_and_does_not_mark_day_complete(self):
        instant = datetime(2030, 1, 1, 13, 0, tzinfo=timezone.utc)
        saved = []
        result = self.automation.tick(
            instant=instant, state=dict(self.state),
            runner=lambda date: {"ok": False, "date": date, "error": "scan failed"},
            save=lambda value: saved.append(value))

        self.assertEqual(result["status"], "failed")
        self.assertNotIn("last_run_date", saved[-1])
        self.assertEqual(saved[-1]["last_result"]["error"], "scan failed")

    def test_failure_schedules_one_retry_and_alerts(self):
        """21:00 那次挂了（比如在路上没网），正常窗口只有 10 分钟，当天就再也不会
        触发。所以失败要排一次补跑，并且要让人知道。"""
        instant = datetime(2030, 1, 1, 13, 0, tzinfo=timezone.utc)  # 21:00 北京
        saved, alerts = [], []

        self.automation.tick(
            instant=instant, state=dict(self.state),
            runner=lambda date: {"ok": False, "date": date, "error": "连不上 LLM 网关"},
            save=saved.append,
            notifier=lambda title, msg: alerts.append((title, msg)))

        retry = saved[-1]["retry"]
        self.assertEqual(retry["attempts"], 0, "排好但还没跑，已执行次数是 0")
        self.assertEqual(retry["date"], "2030-01-01")
        self.assertEqual(len(alerts), 1, "失败必须报警")
        self.assertIn("连不上 LLM 网关", alerts[0][1])
        self.assertIn("30", alerts[0][1], "要说清楚多久后重试")

    def test_retry_fires_after_the_delay_but_not_before(self):
        base = datetime(2030, 1, 1, 13, 0, tzinfo=timezone.utc)
        saved = []
        self.automation.tick(
            instant=base, state=dict(self.state),
            runner=lambda date: {"ok": False, "date": date, "error": "boom"},
            save=saved.append, notifier=lambda *a: None)
        after_fail = saved[-1]

        early = self.automation.tick(                    # 才过 20 分钟
            instant=base + timedelta(minutes=20), state=after_fail,
            runner=mock.Mock(), save=lambda v: None, notifier=lambda *a: None)
        self.assertEqual(early["status"], "not-due", "没到点不能提前补跑")

        runner = mock.Mock(return_value={"ok": True, "date": "2030-01-01"})
        late = self.automation.tick(                     # 过了 31 分钟
            instant=base + timedelta(minutes=31), state=after_fail,
            runner=runner, save=saved.append, notifier=lambda *a: None)
        self.assertEqual(late["status"], "succeeded")
        self.assertEqual(late["attempt"], "retry")
        self.assertEqual(saved[-1]["last_run_date"], "2030-01-01")
        self.assertNotIn("retry", saved[-1], "成功之后补跑记录要清掉")

    def test_retry_is_used_at_most_once_then_gives_up_loudly(self):
        """补跑无限重试会在真故障时刷屏。只补一次，然后停手并报警。"""
        base = datetime(2030, 1, 1, 13, 0, tzinfo=timezone.utc)
        saved, alerts = [], []
        fail = lambda date: {"ok": False, "date": date, "error": "还是没网"}

        self.automation.tick(instant=base, state=dict(self.state), runner=fail,
                             save=saved.append,
                             notifier=lambda t, m: alerts.append((t, m)))
        self.automation.tick(instant=base + timedelta(minutes=31), state=saved[-1],
                             runner=fail, save=saved.append,
                             notifier=lambda t, m: alerts.append((t, m)))

        third = self.automation.tick(
            instant=base + timedelta(minutes=90), state=saved[-1],
            runner=mock.Mock(), save=lambda v: None, notifier=lambda *a: None)
        self.assertEqual(third["status"], "not-due", "只补一次，不能没完没了")
        self.assertEqual(len(alerts), 2)
        self.assertIn("重试已用完", alerts[-1][1])

    def test_retry_from_a_previous_day_is_ignored(self):
        """昨天挂掉留下的补跑记录，不该在今天大清早突然跑起来。"""
        stale = dict(self.state)
        stale["retry"] = {"date": "2029-12-31", "attempts": 1,
                          "at": "2029-12-31T21:30:00+08:00"}
        runner = mock.Mock()
        result = self.automation.tick(
            instant=datetime(2030, 1, 1, 2, 0, tzinfo=timezone.utc),  # 10:00 北京
            state=stale, runner=runner, save=lambda v: None,
            notifier=lambda *a: None)
        self.assertEqual(result["status"], "not-due")
        runner.assert_not_called()

    def test_daily_pipeline_continues_when_mobius_sync_fails(self):
        scan = mock.Mock(return_value={"ok": True, "recorded": 2})
        digest = mock.Mock(return_value={"status": "generated", "update_count": 2})

        result = self.automation.run_daily(
            "2030-01-01",
            sync=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
            scan_func=scan,
            digest_func=digest,
        )

        self.assertTrue(result["ok"])
        self.assertIn("offline", result["warnings"][0])
        scan.assert_called_once_with(days=2)
        digest.assert_called_once_with("2030-01-01", force=False)

    def test_daily_pipeline_stops_when_scan_fails(self):
        digest = mock.Mock()
        result = self.automation.run_daily(
            "2030-01-01", sync=lambda: {"count": 1},
            scan_func=lambda **kw: {"ok": False, "error": "bad transcript"},
            digest_func=digest,
        )
        self.assertFalse(result["ok"])
        self.assertIn("bad transcript", result["error"])
        digest.assert_not_called()


class TestLaunchAgents(unittest.TestCase):
    def setUp(self):
        from fecho import automation

        self.automation = automation
        self.home = Path(tempfile.mkdtemp(prefix="fecho-launchagent-test-"))

    def test_daily_agent_ticks_each_minute_and_dashboard_is_loopback_keepalive(self):
        specs = self.automation.launch_agent_specs(
            python="/private/fecho/bin/python", home=self.home)
        daily = plistlib.loads(specs[self.automation.DAILY_LABEL])
        dashboard = plistlib.loads(specs[self.automation.DASHBOARD_LABEL])

        self.assertEqual(daily["StartInterval"], 60)
        self.assertTrue(daily["RunAtLoad"])
        self.assertEqual(daily["ProgramArguments"][:7], [
            "/usr/bin/env", "-u", "FECHO_LLM_API_KEY", "-u",
            "FECHO_MOBIUS_TOKEN", "-u", "MOBIUS_API_KEY",
        ])
        self.assertEqual(daily["ProgramArguments"][-4:], [
            "-m", "fecho.cli", "schedule", "tick"])
        self.assertTrue(dashboard["KeepAlive"])
        self.assertIn("127.0.0.1", dashboard["ProgramArguments"])
        self.assertIn("8900", dashboard["ProgramArguments"])
        self.assertIn("--no-browser", dashboard["ProgramArguments"])

    def test_free_dashboard_port_requires_no_migration(self):
        calls = []
        result = self.automation.release_dashboard_port(
            home=self.home, uid=501,
            runner=lambda args, **kw: calls.append(args) or _Result(1),
            killer=lambda *args: self.fail("free port must not kill a process"),
        )
        self.assertEqual(result["status"], "free")
        self.assertEqual(calls[0][:3], ["lsof", "-nP", "-tiTCP:8900"])

    def test_owned_legacy_dashboard_is_stopped(self):
        old = str(self.home / ".fecho" / "venv" / "bin" / "fecho")
        listener_checks = iter([_Result(0, "123\n"), _Result(1)])
        killed = []

        def runner(args, **kwargs):
            if args[0] == "lsof":
                return next(listener_checks)
            if args[0] == "ps":
                return _Result(0, "501 /usr/bin/python3 %s web --no-browser\n" % old)
            return _Result()

        result = self.automation.release_dashboard_port(
            home=self.home, uid=501, runner=runner,
            killer=lambda pid, sig: killed.append((pid, sig)), waiter=lambda _: None)
        self.assertEqual(result, {"status": "migrated", "pid": 123})
        self.assertEqual(killed, [(123, signal.SIGTERM)])

    def test_unknown_dashboard_port_owner_is_never_stopped(self):
        killed = []

        def runner(args, **kwargs):
            if args[0] == "lsof":
                return _Result(0, "456\n")
            return _Result(0, "501 /usr/local/bin/unrelated-server\n")

        with self.assertRaisesRegex(RuntimeError, "不是 Fecho"):
            self.automation.release_dashboard_port(
                home=self.home, uid=501, runner=runner,
                killer=lambda pid, sig: killed.append((pid, sig)))
        self.assertFalse(killed)

    def test_launchd_owned_dashboard_pid_survives_wrapper_exec_detection(self):
        listener_checks = iter([_Result(0, "789\n"), _Result(1)])
        killed = []

        def runner(args, **kwargs):
            if args[0] == "lsof":
                return next(listener_checks)
            return _Result(0, "501 /System/Python -m fecho.cli web --port 8900\n")

        result = self.automation.release_dashboard_port(
            home=self.home, uid=501, managed_pid=789, runner=runner,
            killer=lambda pid, sig: killed.append((pid, sig)), waiter=lambda _: None)
        self.assertEqual(result, {"status": "migrated", "pid": 789})
        self.assertEqual(killed, [(789, signal.SIGTERM)])

    def test_install_writes_only_owned_plists_and_loads_them(self):
        calls = []
        result = self.automation.install_launch_agents(
            home=self.home, python="/private/fecho/bin/python",
            runner=lambda args, **kw: calls.append(args) or 0,
            uid=501,
        )

        expected = {
            self.home / "Library" / "LaunchAgents" / "com.feedmob.fecho.daily.plist",
            self.home / "Library" / "LaunchAgents" / "com.feedmob.fecho.dashboard.plist",
        }
        self.assertEqual({Path(p) for p in result["files"]}, expected)
        self.assertTrue(all(p.exists() for p in expected))
        self.assertTrue(any(call[:2] == ["launchctl", "bootstrap"] for call in calls))

    def test_uninstall_removes_only_fecho_owned_plists(self):
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        owned = [agents / (label + ".plist") for label in (
            self.automation.DAILY_LABEL, self.automation.DASHBOARD_LABEL)]
        unrelated = agents / "com.example.keep.plist"
        for path in owned + [unrelated]:
            path.write_text("test", encoding="utf-8")

        self.automation.uninstall_launch_agents(
            home=self.home, runner=lambda args, **kw: 0, uid=501)

        self.assertFalse(any(path.exists() for path in owned))
        self.assertTrue(unrelated.exists())

    def test_install_schedule_rejects_2200_and_persists_valid_time(self):
        with self.assertRaisesRegex(ValueError, "22:00"):
            self.automation.install_schedule("22:00", installer=lambda: {})

        saved = []
        result = self.automation.install_schedule(
            "20:45", state={}, save=lambda state: saved.append(state),
            installer=lambda: {"files": ["daily", "dashboard"]})
        self.assertEqual(saved[-1]["daily_time"], "20:45")
        self.assertTrue(saved[-1]["enabled"])
        self.assertEqual(result["daily_time"], "20:45")

    def test_status_separates_configuration_from_live_health(self):
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        for label in self.automation.OWNED_LABELS:
            (agents / (label + ".plist")).write_text("plist", encoding="utf-8")

        def runner(args, **kwargs):
            label = args[-1]
            if label.endswith(self.automation.DAILY_LABEL):
                return _Result(0, "state = not running\nlast exit code = 0\n")
            return _Result(0, "state = running\npid = 123\nlast exit code = 0\n")

        with mock.patch.object(self.automation, "schedule_state", return_value={
                "enabled": True, "daily_time": "21:00"}):
            result = self.automation.status(
                home=self.home, uid=501, runner=runner,
                health=lambda url: {"ok": True, "version": "0.6.0"})
        self.assertTrue(all(result["configured_launch_agents"].values()))
        self.assertTrue(result["runtime"][self.automation.DAILY_LABEL]["ok"])
        self.assertTrue(result["runtime"][self.automation.DASHBOARD_LABEL]["ok"])
        self.assertTrue(result["ready"])

    def test_status_is_not_ready_when_dashboard_is_not_healthy(self):
        def runner(args, **kwargs):
            return _Result(0, "state = running\nlast exit code = 0\n")

        with mock.patch.object(self.automation, "schedule_state", return_value={
                "enabled": True, "daily_time": "21:00"}):
            result = self.automation.status(
                home=self.home, uid=501, runner=runner,
                health=lambda url: {"ok": False})
        self.assertFalse(result["runtime"][self.automation.DASHBOARD_LABEL]["ok"])
        self.assertFalse(result["ready"])


class TestSharedProvisioning(unittest.TestCase):
    def setUp(self):
        from fecho import onboarding

        self.onboarding = onboarding
        self.shared = {
            "llm_base_url": "https://llm.example.test/v1",
            "llm_api_key": "shared-secret",
            "llm_model": "minimax-m3",
            "llm_reasoning_effort": "low",
        }

    def test_existing_valid_config_wins_without_fetching(self):
        fetch = mock.Mock()
        result = self.onboarding.resolve_shared_llm(
            current=self.shared, shared_url="https://config.example.test/fecho.json",
            fetch=fetch)
        self.assertEqual(result["source"], "existing")
        self.assertEqual(result["values"]["llm_model"], "minimax-m3")
        fetch.assert_not_called()

    def test_private_json_file_accepts_only_llm_fields(self):
        path = self.home_file({**self.shared, "mobius_token": "must-not-copy", "author": "owner"})
        result = self.onboarding.resolve_shared_llm(current={}, shared_file=path)
        self.assertEqual(result["source"], "file")
        self.assertNotIn("mobius_token", result["values"])
        self.assertNotIn("author", result["values"])

    def test_https_shared_config_is_supported_but_plain_http_is_rejected(self):
        fetch = mock.Mock(return_value=self.shared)
        result = self.onboarding.resolve_shared_llm(
            current={}, shared_url="https://config.example.test/fecho.json", fetch=fetch)
        self.assertEqual(result["source"], "url")
        fetch.assert_called_once()
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            self.onboarding.resolve_shared_llm(
                current={}, shared_url="http://config.example.test/fecho.json", fetch=fetch)

    def test_missing_shared_config_stops_onboarding(self):
        with self.assertRaisesRegex(RuntimeError, "共享 LLM"):
            self.onboarding.resolve_shared_llm(current={})

    def test_onboarding_always_runs_mobius_browser_oauth_for_current_user(self):
        from fecho import config, oauth, service

        started = {"ctx": object()}
        with mock.patch.object(config, "mobius_configured", return_value=True), \
                mock.patch.object(config, "update"), \
                mock.patch.object(config, "reload_module"), \
                mock.patch.object(oauth, "login", return_value=started) as login, \
                mock.patch.object(oauth, "complete") as complete, \
                mock.patch.object(service, "sync_issues", return_value={"count": 3}):
            result = self.onboarding._connect_mobius("person@example.test")

        login.assert_called_once()
        complete.assert_called_once_with(started["ctx"])
        self.assertEqual(result["count"], 3)

    def test_persisted_shared_key_is_0600_and_result_is_redacted(self):
        home = Path(tempfile.mkdtemp(prefix="fecho-shared-config-test-"))
        config_file = home / "config.json"
        from fecho import config

        with mock.patch.object(config, "HOME", home), \
                mock.patch.object(config, "CONFIG_FILE", config_file), \
                mock.patch.object(config, "_cache", {}):
            result = self.onboarding.persist_shared_llm(self.shared)
        self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result, {"configured": True, "model": "minimax-m3"})
        self.assertNotIn("shared-secret", json.dumps(result))

    def test_onboard_orders_configuration_hosts_oauth_then_schedule(self):
        events = []
        result = self.onboarding.onboard(
            author="rachel", display_name="Rachel",
            work_prefixes=["/work"], ignores=["/work/private"],
            mobius_assignee="rachel@example.test", daily_time="21:00",
            shared_file=self.home_file(self.shared),
            configure=lambda values: events.append(("configure", values)),
            scope_add=lambda kind, value: events.append(("scope", kind, value)),
            install_hosts=lambda: events.append(("hosts",)) or {"codex": "installed"},
            connect_mobius=lambda email: events.append(("oauth", email)) or {"count": 2},
            install_schedule=lambda when: events.append(("schedule", when)) or {"installed": True},
            doctor=lambda: events.append(("doctor",)) or {"checks": [{"ok": True}]},
        )
        self.assertEqual([event[0] for event in events], [
            "configure", "scope", "scope", "hosts", "oauth", "schedule", "doctor"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["doctor"]["checks"][0]["ok"])
        self.assertNotIn("shared-secret", json.dumps(result))

    def home_file(self, content):
        path = Path(tempfile.mkdtemp(prefix="fecho-shared-source-")) / "shared.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        return path


class _Result:
    def __init__(self, code=0, stdout="", stderr=""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


class TestHostInstallation(unittest.TestCase):
    def setUp(self):
        from fecho import hosts

        self.hosts = hosts
        self.home = Path(tempfile.mkdtemp(prefix="fecho-hosts-test-"))
        self.command = "/private/fecho/bin/fecho-mcp"

    def test_host_specs_use_supported_user_level_commands(self):
        specs = self.hosts.host_specs(self.command, self.home)
        self.assertEqual(specs["codex"]["add"], [
            "codex", "mcp", "add", "fecho", "--", self.command])
        self.assertEqual(specs["claude-code"]["add"], [
            "claude", "mcp", "add", "--scope", "user", "fecho", "--", self.command])
        self.assertEqual(specs["hermes"]["add"], [
            "hermes", "mcp", "add", "fecho", "--command", self.command])
        self.assertEqual(specs["codex"]["skill"], self.home / ".codex" / "skills" / "fecho")

    def test_default_mcp_command_stays_beside_symlinked_venv_python(self):
        venv_bin = self.home / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.symlink_to("/usr/bin/python3")
        mcp = venv_bin / "fecho-mcp"
        mcp.write_text("#!/bin/sh\n", encoding="utf-8")

        with mock.patch.object(self.hosts.sys, "executable", str(python)), \
                mock.patch.object(self.hosts.shutil, "which", return_value=None):
            self.assertEqual(self.hosts._default_mcp_command(), str(mcp))

    def test_install_all_registers_detected_hosts_and_copies_skill(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            if args in (["codex", "mcp", "get", "fecho", "--json"],
                        ["claude", "mcp", "get", "fecho"],
                        ["hermes", "mcp", "list"]):
                return _Result(1, "", "not found")
            return _Result()

        result = self.hosts.install_all(
            home=self.home, mcp_command=self.command, runner=runner,
            which=lambda name: "/usr/bin/" + name,
        )

        self.assertEqual(set(result), {"codex", "claude-code", "hermes"})
        self.assertTrue(all(item["mcp"] == "installed" for item in result.values()))
        for item in result.values():
            skill = Path(item["skill"]) / "SKILL.md"
            self.assertTrue(skill.exists())
            self.assertIn("log_progress", skill.read_text(encoding="utf-8"))
        self.assertIn(["hermes", "mcp", "add", "fecho", "--command", self.command], calls)

    def test_matching_registration_is_kept_and_conflict_is_not_overwritten(self):
        adds = []

        def runner(args, **kwargs):
            if args == ["codex", "mcp", "get", "fecho", "--json"]:
                return _Result(0, json.dumps({"command": self.command}))
            if args == ["claude", "mcp", "get", "fecho"]:
                return _Result(0, "Command: /another/fecho-mcp")
            adds.append(args)
            return _Result()

        result = self.hosts.install_all(
            home=self.home, mcp_command=self.command, runner=runner,
            which=lambda name: "/usr/bin/" + name if name in ("codex", "claude") else None,
        )
        self.assertEqual(result["codex"]["mcp"], "existing")
        self.assertEqual(result["claude-code"]["mcp"], "conflict")
        self.assertFalse(adds)

    def test_missing_hosts_are_reported_without_creating_directories(self):
        result = self.hosts.install_all(
            home=self.home, mcp_command=self.command,
            runner=lambda *a, **k: _Result(), which=lambda name: None)
        self.assertTrue(all(item["mcp"] == "not-installed" for item in result.values()))
        self.assertFalse((self.home / ".codex").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
