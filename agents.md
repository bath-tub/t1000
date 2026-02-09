# J2PR Agent Guide

## Program Goals
- Turn eligible Jira issues into Draft PR links using a Cursor headless agent.
- Keep the orchestrator safe, observable, and idempotent.
- Treat Jira as source-of-truth; never merge PRs.

## High-Level Architecture
- CLI entrypoint: `j2pr` (`src/j2pr/cli.py`)
- Modules:
  - `config.py`: config parsing + env interpolation
  - `jira.py`: Jira search/comment
  - `github.py`: PR lookup/create (gh or REST)
  - `agent.py`: Cursor agent invocation + footer parsing
  - `guardrails.py`: deny-globs + diff limits
  - `state.py`: SQLite state + locks
  - `artifacts.py`: run artifacts in `~/.j2pr/runs/`
  - `session_capture.py`: full session recording for AI diagnosis

## Core Principles
- Orchestrator enforces guardrails outside the agent.
- Idempotent by default: reuse existing PRs when possible.
- Minimal change bias in agent prompts.
- Never change `.github/workflows/**`, infra, or migrations by default.

## Local Workflow
1. Validate config: `j2pr config-validate`
2. Scan: `j2pr scan`
3. Run: `j2pr run <JIRAKEY>` or `j2pr run-next`
4. Inspect: `j2pr status --ticket <JIRAKEY>` and `j2pr tail <JIRAKEY>`
5. Diagnose: `j2pr sessions` and `j2pr session <TICKET> --events`

## Session Capture
When `session_capture.enabled: true` in config, every `j2pr run` records a full
session to `~/.j2pr/sessions/<ticket>/<run_id>/` with three files:

- `session_output.log` — raw tee of all stdout/stderr (the complete agent
  conversation and orchestrator output, exactly as it appeared on the terminal)
- `session_events.jsonl` — structured timestamped events (config snapshot,
  branch creation, agent invocations, test results, guardrail checks, PR creation, errors)
- `session_manifest.json` — machine-readable summary (timing, event list, errors)

### Reading Sessions (CLI)
```
j2pr sessions                       # list all (newest first)
j2pr sessions --ticket PAYAD-1966   # filter by ticket
j2pr session PAYAD-1966             # show manifest for latest run
j2pr session PAYAD-1966 --events    # structured event timeline
j2pr session PAYAD-1966 --output    # raw console output
j2pr session PAYAD-1966 abc123      # specific run by ID prefix
```
All commands support `--json` for machine-readable output.

### Reading Sessions (direct file access)
Sessions live at `~/.j2pr/sessions/<ticket>/<run_id>/`. You can also read
the files directly:
- `session_manifest.json` — start here. Check `errors` array and `event_names`
  list to understand what happened at a glance.
- `session_events.jsonl` — one JSON object per line, each with `ts`, `elapsed_s`,
  `event`, and `data`. Walk these chronologically to reconstruct the decision flow.
- `session_output.log` — full raw output. Search this for agent reasoning,
  test output, git errors, or anything else the structured events don't capture.

### Diagnosis Playbook for AI Agents
When a run fails or behaves unexpectedly:
1. **Start with the manifest**: `j2pr session <TICKET> --json`. Look at `errors`
   and `event_names` to classify the failure (agent contract, tests, guardrails, git).
2. **Walk the event timeline**: `j2pr session <TICKET> --events`. Key events:
   - `agent_invocation_finished` — did the agent exit cleanly? Did it produce a footer?
   - `tests_finished` — did tests pass? What was the returncode?
   - `test_fix_cycle` — how many fix attempts? Did we exhaust `max_fix_attempts`?
   - `guardrails_check_finished` — were diff limits or deny globs violated?
   - `run_failed` — what was the terminal error and its type?
3. **Read the raw output** when events aren't enough: `j2pr session <TICKET> --output`.
   This is the full agent conversation — look for the agent's reasoning, the actual
   test output, and any mismatch between what the agent reported and what the
   orchestrator observed (e.g., agent says tests pass but orchestrator ran a
   different test command).
4. **Cross-reference with run artifacts** at `~/.j2pr/runs/<ticket>/<run_id>/`:
   `agent_transcript.log`, `test_output.log`, `diff.patch`, `commands.json`.

### Common Failure Patterns (captured by session events)
- **Wrong test command**: Agent uses project-native runner but orchestrator runs
  the configured `test_command`. Look for `tests_finished.returncode != 0` and
  compare `test_command` in the config snapshot against what the agent reported.
- **Duplicate footer**: Agent emits `J2PR_RESULT` twice; orchestrator picks the
  last one. Check `agent_invocation_finished.has_footer` and the raw output.
- **Dirty worktree**: `worktree_check_finished.clean == false`. Probably leftover
  files from a previous run. Use `--force` or clean manually.
- **Guardrail breach**: `guardrails_deny_glob_violation` or `diff_limits_exceeded`
  in events. Agent touched forbidden paths or made too large a change.

### Retention
Set `session_capture.retention_days` (default 0 = keep forever) to auto-prune
old sessions when new ones are created.

## Guardrails You Must Respect
- Deny globs and diff limits are enforced in `guardrails.py`.
- Tests are required by default; one fix cycle allowed.
- Worktree must be clean unless `--force`.

## Habits for Future Agents
- Keep changes small and isolated to the ticket.
- Update or add tests for any logic changes.
- If you learn a new workflow or sharp edge, add it here.
- Use Commitzen commit format: `<domain>(<goal>): <summary>`.
  - `domain` = area of intent (e.g., `core`, `tests`, `docs`, `config`, `rules`).
  - `goal` = short purpose phrase (e.g., `bootstrap`, `harden`, `document`).
  - `summary` = imperative, concise description of the change.
- **Session capture maintenance**: if you add or change a step in the `run()`
  pipeline in `cli.py`, emit a `cap.event()` call so the decision is recorded.
  If you add a public method to `SessionCapture`, add a matching no-op to
  `_NoOpCapture`.
- **Diagnosis first**: when debugging a failed run, always check session captures
  before reading source code. Run `j2pr session <TICKET> --events` — the event
  timeline usually pinpoints the failure faster than re-reading the pipeline.

## When You Get Stuck
- **Read the session capture first** if one exists: `j2pr session <TICKET> --events`
  and `j2pr session <TICKET> --output`. These contain the full context of what
  the orchestrator did and saw, including the agent's own reasoning.
- Check run artifacts: `~/.j2pr/runs/<ticket>/<run_id>/` for diffs, test logs,
  and the agent transcript.
- Document the blocker and resolution in this file.
- Include: what failed, where artifacts are, and the workaround.
