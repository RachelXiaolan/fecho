# Fecho Onboarding and Automation Design

## Goal

Turn Fecho from a set of working primitives into a one-time onboarding flow followed by an unattended daily loop: agents record work while they run, transcript scanning fills gaps, MiniMax produces the report before 22:00 Beijing time, and a local Dashboard is always available for review.

## Product flow

1. The user gives Codex, Claude Code, or Hermes one installation prompt.
2. The agent installs Fecho and runs `fecho onboard` with the user's identity, work allowlist, Mobius email, and chosen daily time.
3. Onboarding loads an owner-provided shared LLM configuration, registers the stdio MCP and Fecho Skill for installed hosts, completes Mobius OAuth, installs automation, and runs `doctor`.
4. During work, the Skill tells the active agent to call `catch_up` at the beginning and `log_progress` after a concrete result, pitfall, or decision.
5. A macOS LaunchAgent wakes once per minute. A Beijing-time guard runs the daily pipeline once on the configured calendar day: sync Mobius, scan recent allowlisted transcripts, then generate that Beijing day's report.
6. A second LaunchAgent keeps the local Dashboard available at `http://127.0.0.1:8900/`. The page refreshes when revisited and periodically while visible.
7. The user reviews and corrects entries after the report appears. Corrections mark the report stale; the user explicitly regenerates the final report.

Exact-date transcript replay is not part of this phase. It remains a later test helper; the production nightly path is the priority.

## Shared LLM configuration

The onboarding UI does not ask ordinary installers for MiniMax fields. It resolves the shared configuration in this order:

1. existing valid Fecho LLM configuration;
2. a JSON file named by `--shared-config` or `FECHO_SHARED_CONFIG`;
3. an HTTPS JSON endpoint named by `--shared-config-url` or `FECHO_SHARED_CONFIG_URL`.

Only `llm_base_url`, `llm_api_key`, `llm_model`, and optional reasoning/timeout values are accepted. The resolved key is stored in each user's `~/.fecho/config.json`, which is already restricted to mode 0600. No credential is committed to Git, built into the wheel, written to logs, or returned by `doctor`. The owner accepts that each installer can read the shared key on their own machine.

## Onboarding and host adapters

`fecho onboard` is idempotent and exposes non-interactive arguments so an agent can run it from one prompt. It installs a packaged `fecho` Skill into the user skill directory for each detected host, then registers the absolute `fecho-mcp` executable using the host's supported interface:

- Codex: `codex mcp add fecho -- <absolute-command>`;
- Claude Code: `claude mcp add --scope user fecho -- <absolute-command>`;
- Hermes: `hermes mcp add fecho --command <absolute-command>`.

Existing matching registrations are retained. A conflicting registration is reported and left unchanged unless a future explicit repair option is added. Missing hosts are reported as skipped, not failed. Changes that require an Agent restart are stated in the final result.

## Beijing time and daily pipeline

Fecho uses `Asia/Shanghai` as its product calendar regardless of the computer's timezone. User-facing dates, transcript day grouping, the default report date, and the scheduler guard use the same clock.

The configured time must match `HH:MM` and be earlier than 22:00. The default is 21:00. The LaunchAgent uses `StartInterval=60`; `fecho schedule tick` checks Beijing time and a persisted `last_run_date`, so it is safe across host timezones, daylight-saving changes, restarts, and repeated invocations. `run-now` bypasses the time guard for acceptance. A failed run is not marked complete and may retry during the configured minute; detailed stage results are persisted without secrets.

The daily pipeline attempts Mobius sync first. A sync failure becomes a warning and does not discard local work. Scan or digest failure makes the run fail visibly. Successful reports remain in the existing SQLite/report files and are immediately readable by the Dashboard.

## Dashboard service

The local-first architecture requires a local process only when serving the UI; MCP logging and scheduled generation continue to work without it. To make the fixed URL dependable, onboarding installs a per-user LaunchAgent with `RunAtLoad` and `KeepAlive`, binding only to `127.0.0.1:8900`.

The Dashboard polls only while the page is visible and refreshes on window focus/visibility return. System status shows the chosen Beijing schedule and last run outcome. Logs live under `~/.fecho/` and the automation CLI provides install, status, run-now, and uninstall operations.

## Failure and privacy boundaries

- Unregistered and ignored workspaces are filtered before transcript text reaches the LLM.
- Missing shared configuration stops onboarding before automation is installed.
- OAuth still requires the user to approve in the browser.
- MCP configuration conflicts are never silently overwritten.
- Automation is installed only after configuration and scope checks pass.
- LaunchAgents bind the Dashboard to loopback only.
- Removing automation removes only Fecho-owned plist files and unloads their labels; user data remains intact.

## Acceptance

- Unit tests cover shared config resolution/redaction, time validation, Beijing calendar boundaries, once-per-day ticks, pipeline failure behavior, plist generation, host registration, Skill installation, and Dashboard refresh hooks.
- A clean temporary home completes a dry-run onboarding without touching real host configuration.
- On macOS, real acceptance installs both LaunchAgents, verifies the fixed URL, runs `schedule run-now`, observes a report in the UI, and confirms `schedule status` reports success.
- Codex and Claude Code registrations are checked on this machine; Hermes behavior is validated against its official CLI/config contract and reported as untested when the binary is absent.
