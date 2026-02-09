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

## Core Principles
- Orchestrator enforces guardrails outside the agent.
- Idempotent by default: reuse existing PRs when possible.
- Minimal change bias in agent prompts.
- Never change `.github/workflows/**`, infra, or migrations by default.

## Local Workflow
1. Validate config: `j2pr config validate`
2. Scan: `j2pr scan`
3. Run: `j2pr run <JIRAKEY>` or `j2pr run-next`
4. Inspect: `j2pr status --ticket <JIRAKEY>` and `j2pr tail <JIRAKEY>`

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

## When You Get Stuck
- Document the blocker and resolution in this file.
- Include: what failed, where artifacts are, and the workaround.
