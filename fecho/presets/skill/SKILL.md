---
name: fecho
description: Use Fecho to catch up on ongoing work and automatically record concrete outcomes, pitfalls, and decisions while working in an allowlisted workspace.
---

# Fecho work log

Use the Fecho MCP as the durable work log when its tools are available.

## At the start of a work session

Call `fecho_doctor` once. If Fecho is ready, call `catch_up` once before substantial work so you know the open tasks and recent report. Do not repeatedly call either tool during the same session.

If Fecho is missing or not ready, explain the failing doctor check. Do not silently claim logging is active.

## While working

After completing a concrete, independently understandable result, call `log_progress` without waiting for the user to request it. Also log a confirmed pitfall or a decision that materially changes the work.

- Record the result and its current state, not the plan or the conversation.
- Use `completion_status=done` only for completed outcomes; use `wip`, `blocked`, or `unknown` honestly.
- Use `kind=pitfall` for a failed approach with a useful conclusion and `kind=decision` only after a decision is actually made.
- Pass an issue key only when the current task context makes it certain. Otherwise allow a free task; never invent an issue.
- Never record credentials, private tokens, full prompts, full transcripts, or quotations of the user's wording.
- Keep one result per entry. Do not log routine tool calls, confirmations, or plans with no outcome.

Use the returned `update_id`. If the content or assignment is wrong, call `correct_progress` on that entry instead of creating a replacement. Transcript scanning is the scheduled fallback for missed entries; do not run it after every interaction.

The nightly Beijing-time automation generates the report. Call `end_of_day` manually only when the user asks to generate or regenerate a report now.
