"""Onboarding and unattended automation contracts."""
import os
import json
import plistlib
import tempfile
import unittest
from datetime import datetime, timezone
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
        self.assertEqual(
            daily["ProgramArguments"],
            ["/private/fecho/bin/python", "-m", "fecho.cli", "schedule", "tick"],
        )
        self.assertTrue(dashboard["KeepAlive"])
        self.assertIn("127.0.0.1", dashboard["ProgramArguments"])
        self.assertIn("8900", dashboard["ProgramArguments"])
        self.assertIn("--no-browser", dashboard["ProgramArguments"])

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
        )
        self.assertEqual([event[0] for event in events], [
            "configure", "scope", "scope", "hosts", "oauth", "schedule"])
        self.assertTrue(result["ok"])
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
