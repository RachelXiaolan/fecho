# Fecho Reliability and UI Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Fecho installable and safe for real multi-agent logging, then replace the demo Dashboard with a usable review and operations workspace.

**Architecture:** Keep the local SQLite and service layer, but replace process-global MCP identity with request contexts, make assignment and scan decisions auditable, and expose coherent dashboard APIs. Preserve backward compatibility through additive schema migrations and normalize legacy rows on read.

**Tech Stack:** Python 3.9, SQLite, JSON-RPC/MCP, FastAPI, vanilla HTML/CSS/JavaScript, unittest.

---

### Task 1: Installation and acceptance reliability

**Files:** Modify `pyproject.toml`, `tests/test_core.py`, `scripts/acceptance.sh`; create `tests/test_package.py`.

1. Write a failing package test that builds a wheel and asserts `fecho/presets/dashboard.html` exists.
2. Run the test and confirm it fails.
3. Add the dashboard asset to package data; make the test runner and acceptance script return non-zero on failure.
4. Run package and core tests; build and inspect the wheel.
5. Commit.

### Task 2: Per-session MCP identity

**Files:** Modify `fecho/mcp_server.py`, `fecho/web.py`, `scripts/mcp_probe.py`, `tests/test_core.py`.

1. Add failing tests showing two HTTP/SSE sessions retain different client names and task continuity.
2. Introduce `MCPContext` and pass it to `handle`/`call_tool`; retain one default context per stdio process.
3. Store a context per SSE session and Streamable HTTP session header; return the session header on initialize.
4. Remove the user-settable agent override from the public tool schema.
5. Run transport tests and commit.

### Task 3: Safe submission and correction

**Files:** Modify `fecho/db.py`, `fecho/store.py`, `fecho/service.py`, `fecho/mcp_server.py`, `fecho/web.py`, `tests/test_core.py`.

1. Add failing tests for invalid dates, unknown issues, explicit freeform, correction without duplicate rows, and human assignment locks.
2. Add assignment audit/lock columns through additive migrations.
3. Validate dates and issue references; support `freeform=true`.
4. Implement `correct_progress` and structured MCP result fields.
5. Ensure model verification cannot overwrite human locks; commit.

### Task 4: Scan integrity and source adapters

**Files:** Modify `fecho/db.py`, `fecho/scan.py`, `fecho/config.py`, `tests/test_core.py`.

1. Add failing tests for malformed model output, zero-result marker, source-event idempotency, and adapter discovery.
2. Add `scan_runs` and stable source event keys.
3. Treat malformed output as a failed group and keep the watermark before it.
4. Add Claude, Codex and Hermes transcript adapters with configuration-based paths.
5. Preserve producer agent separately from ingestion method; commit.

### Task 5: Assignment verification and report correctness

**Files:** Modify `fecho/digest.py`, `fecho/db.py`, `fecho/store.py`, `tests/test_core.py`.

1. Add failing tests for empty/stale issue cache, human locks, unchanged verification fingerprints, completion status, and fallback wording.
2. Skip verification when the issue cache is unavailable and exclude human-locked updates.
3. Cache verification fingerprints and include update revisions, assignments, issue titles and prompt version in report fingerprints.
4. Pass completion status and content kind to the model; render Done/In Progress/Blocked sections.
5. Make fallback output use neutral wording for unknown status; commit.

### Task 6: Task lifecycle

**Files:** Modify `fecho/store.py`, `fecho/service.py`, `fecho/mcp_server.py`, `fecho/web.py`, `tests/test_core.py`.

1. Add failing tests for complete, reopen and merge operations.
2. Expose task lifecycle operations through service, MCP and HTTP.
3. Keep auditability when merging tasks and ensure `my_tasks` excludes completed tasks.
4. Run tests and commit.

### Task 7: Dashboard APIs and coherent mutations

**Files:** Modify `fecho/web.py`, `tests/test_core.py`.

1. Add failing API tests for historical date selection, all review candidates, report dirty state, mutation error responses and complete dashboard refresh data.
2. Add a single dashboard payload endpoint and consistent mutation responses.
3. Include agent/session/time/method/confidence and system health data.
4. Add report daily/voice/history and task management endpoints.
5. Run API tests and commit.

### Task 8: Rebuild Dashboard UI

**Files:** Modify `fecho/presets/dashboard.html`; create `tests/test_dashboard.py`.

1. Add static contract tests for navigation, date/filter controls, accessible labels and error/status regions.
2. Build Today, Review, Tasks, Reports and System views using the new APIs.
3. Add loading, error, success and undo feedback; refresh all affected state after mutations.
4. Add collapsible timelines, responsive layout and report dirty indicators.
5. Run browser acceptance against a large fixture and commit.

### Task 9: End-to-end verification and documentation

**Files:** Modify `README.md`, `INSTALL.md`, `CONNECT-CHATGPT.md`, `scripts/acceptance.sh`.

1. Update documentation to the actual matching model, tool count, paths and local Codex connection.
2. Build a wheel and install into a clean virtual environment.
3. Run stdio, HTTP and SSE probes with two client identities.
4. Run all unit tests and browser acceptance.
5. Run real OAuth sync and synthetic LLM extraction/digest; record latency and output checks.
6. Commit final documentation and verification evidence.
