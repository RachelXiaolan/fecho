# Fecho Onboarding and Automation Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add one-command Fecho onboarding, a packaged cross-agent Skill, Beijing-time daily automation, and an always-available local Dashboard.

**Architecture:** Keep Fecho local-first. New onboarding code configures existing services and host integrations; a small scheduling module owns Beijing time, the daily pipeline, and macOS LaunchAgent lifecycle. Existing logging, scanning, digest, OAuth, and Dashboard APIs remain the execution engine.

**Tech Stack:** Python 3.9 standard library, SQLite, existing httpx/FastAPI stack, macOS launchd, unittest.

---

### Task 1: Beijing product clock and schedule validation

**Files:**
- Create: `fecho/clock.py`
- Modify: `fecho/store.py`
- Modify: `fecho/scan.py`
- Test: `tests/test_onboarding.py`

1. Write failing tests for Beijing dates across a host-timezone boundary and for valid/invalid daily times.
2. Run the focused tests and confirm failures are caused by missing clock behavior.
3. Implement an `Asia/Shanghai` clock, `today()`, and strict `HH:MM < 22:00` validation.
4. Route default log dates and transcript grouping through the product clock.
5. Run focused and existing core tests.
6. Commit the task.

### Task 2: Daily pipeline and once-per-day scheduler guard

**Files:**
- Create: `fecho/automation.py`
- Modify: `fecho/config.py`
- Test: `tests/test_onboarding.py`

1. Write failing tests for due/not-due ticks, once-per-day persistence, run-now, and stage failure behavior.
2. Confirm the focused tests fail correctly.
3. Implement `run_daily`, `tick`, schedule state, and redacted status.
4. Ensure Mobius sync failure is a warning while scan/digest failures remain visible failures.
5. Run focused and core tests.
6. Commit the task.

### Task 3: macOS LaunchAgent lifecycle

**Files:**
- Modify: `fecho/automation.py`
- Modify: `fecho/cli.py`
- Test: `tests/test_onboarding.py`

1. Write failing tests for daily and Dashboard plist contents, exact owned paths, CLI parsing, and idempotent install/status/uninstall behavior with a fake command runner.
2. Confirm failures.
3. Implement `fecho schedule install|status|run-now|tick|uninstall`.
4. Generate a once-per-minute Beijing guard LaunchAgent and a loopback-only, keep-alive Dashboard LaunchAgent.
5. Never remove files outside Fecho's two owned plist targets.
6. Run focused and full tests.
7. Commit the task.

### Task 4: Shared LLM provisioning and one-command onboarding

**Files:**
- Create: `fecho/onboarding.py`
- Modify: `fecho/cli.py`
- Modify: `fecho/service.py`
- Test: `tests/test_onboarding.py`

1. Write failing tests for existing/file/URL shared config precedence, allowed fields, missing config failure, 0600 persistence, and redacted output.
2. Write failing onboarding orchestration tests using fake host and automation adapters.
3. Confirm failures.
4. Implement `fecho onboard` with identity, repeated work/ignore paths, Mobius email, default/custom daily time, shared config sources, dry-run, and skip flags used by tests.
5. Keep OAuth user-driven and stop before automation when prerequisites fail.
6. Run focused and full tests.
7. Commit the task.

### Task 5: Cross-agent MCP and Skill installation

**Files:**
- Create: `fecho/presets/skill/SKILL.md`
- Create: `fecho/hosts.py`
- Modify: `fecho/onboarding.py`
- Modify: `pyproject.toml`
- Test: `tests/test_onboarding.py`

1. Write failing tests for host detection, exact Codex/Claude/Hermes commands, matching-registration skips, conflicts, and Skill destination paths.
2. Confirm failures.
3. Implement subprocess-safe adapters and packaged Skill copying.
4. Write the Skill so agents catch up at session start, log concrete outcomes automatically, avoid plans/secrets/user quotes, correct instead of duplicate, and rely on transcript scan as fallback.
5. Validate the Skill with the bundled skill validator.
6. Build a wheel and verify the Skill is included.
7. Run focused and full tests.
8. Commit the task.

### Task 6: Dashboard automation visibility and refresh

**Files:**
- Modify: `fecho/web.py`
- Modify: `fecho/presets/dashboard.html`
- Modify: `tests/test_dashboard.py`
- Test: `tests/test_onboarding.py`

1. Write failing tests for redacted automation status in the Dashboard payload and visible-page/focus refresh hooks.
2. Confirm failures.
3. Add schedule/last-run state to System and a conservative visible-page refresh interval.
4. Keep mutation refresh behavior unchanged.
5. Run Dashboard and full tests.
6. Commit the task.

### Task 7: Documentation and real local acceptance

**Files:**
- Modify: `README.md`
- Modify: `INSTALL.md`
- Modify: `docs/acceptance/2026-09-09.md`
- Modify: `fecho/__init__.py`
- Modify: `pyproject.toml`

1. Document the one-prompt installation request, shared-config provisioning, schedule commands, Beijing cutoff, Dashboard lifecycle, and current hosted-mode boundary.
2. Bump the release version.
3. Run all tests and `git diff --check`.
4. Build and install the wheel into the actual Fecho venv.
5. Run dry-run onboarding, then install real Codex/Claude registrations and LaunchAgents with the existing local LLM/OAuth configuration.
6. Verify MCP tools, `schedule status`, `schedule run-now`, and `http://127.0.0.1:8900/`.
7. Record results without credentials and commit.
