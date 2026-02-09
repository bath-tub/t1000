# Jira → Cursor Agent → PR Link (Local Utility) — Build Spec & Implementation Prompt

You are an advanced coding agent. Build a **specific, lightweight, daily-use CLI utility** that turns eligible Jira tickets into **a PR link** using a **Cursor headless agent** for all coding work. The orchestrator must be safe, observable, and idempotent.

This tool is intended to be used daily by me and potentially teammates.

---

## 0) Product Summary

**Goal:** A reliable loop:
1) discover eligible Jira issues,
2) run a constrained Cursor agent against the correct local repo,
3) validate basic quality gates (tests/format),
4) open a PR (draft by default),
5) persist run artifacts and show the PR link.

**Core principle:** The orchestrator enforces safety + auditability. The agent does the coding.

---

## 1) Non-Negotiables

- **All execution/coding is done by Cursor agent** (the agent edits code, writes tests, fixes issues).
- **Jira is the source of tickets and ticket data.**
- Tool is **observable** (logs + artifacts) and **safe** (guardrails enforced outside the agent).
- Tool is **idempotent**:
  - never opens duplicate PRs for the same ticket,
  - detects existing branches/PRs and returns links.
- Tool **never merges** PRs.

---

## 2) Scope & Non-Goals

### In scope (MVP)
- Local CLI utility with:
  - `scan` (list eligible tickets),
  - `run` (pick ticket and execute end-to-end),
  - `status` (show state + latest runs),
  - `open` (open/tail artifacts for a ticket/run),
  - `config-validate`.
- One-worker execution (with per-repo lock).
- PR creation to GitHub (via API or `gh` CLI).
- Jira read + comment (comment optional but supported).

### Out of scope (for MVP)
- Slack bot UI, server hosting, multi-user daemon (keep it local).
- Auto-merge, auto-deploy.
- Complex multi-repo orchestration (allow only if explicitly enabled; default single-repo).
- Heavy UI; keep to a clean CLI.

---

## 3) Platform & Tech Choices

Implement in **Python 3.11+** (preferred for portability), packaged as a CLI.

Use:
- `typer` for CLI
- `rich` for display
- `requests` for HTTP
- `sqlite` for state store (via `sqlite3` stdlib)
- optional: `pydantic` for config validation

Prefer invoking system tooling rather than re-implementing:
- Use `git` CLI for repo ops
- Use `gh` CLI for PR creation and finding existing PRs (fallback to GitHub REST if gh missing)

Cursor agent execution:
- Execute a local command specified in config (e.g., `cursor-agent` / `cursor` CLI wrapper).
- Assume headless mode is available. If not, create a thin abstraction that can be swapped.

---

## 4) Configuration (Team-Friendly)

Provide a single config file:
- Default path: `~/.j2pr/config.yaml`
- Also support `J2PR_CONFIG=/path/config.yaml`

### Required config fields
- Jira:
  - `jira.base_url`
  - `jira.email` (or username)
  - `jira.api_token` (or env var reference)
  - `jira.jql` (base eligibility query)
  - `jira.fields` (fields to request)
  - `jira.comment_on_pr` (bool)
  - `jira.label_running`, `jira.label_done`, `jira.label_failed` (strings, optional)
- GitHub:
  - `github.owner`
  - `github.default_base_branch` (e.g. `main`)
  - `github.use_gh_cli` (bool default true)
  - `github.draft_pr` (bool default true)
  - `github.reviewers` (optional list)
  - `github.labels` (optional list)
- Workspace:
  - `workspace.root_dir` (where repos live)
  - `workspace.repo_allowlist` (list of repo names)
  - `workspace.repo_mapping` (map from Jira component or project to repo name)
  - `workspace.single_repo_only` (bool default true)
- Guardrails:
  - `guardrails.deny_globs` (file patterns blocked)
  - `guardrails.command_denylist` (strings/regex)
  - `guardrails.max_files_changed`
  - `guardrails.max_diff_lines`
  - `guardrails.require_clean_worktree` (default true)
  - `guardrails.require_tests` (default true)
  - `guardrails.test_command` (e.g. `./gradlew test` or `dotnet test`)
  - `guardrails.format_command` (optional)
- Cursor agent:
  - `cursor.command` (the executable to run)
  - `cursor.model` (optional)
  - `cursor.timeout_minutes` (default 45)
  - `cursor.prompt_template_path` (optional; default internal template)

### Secrets
- Allow config values like `${ENV_VAR}` and resolve from environment.
- Never print secrets in logs.

---

## 5) Ticket Eligibility Rules (Enforced)

Eligibility is determined by:
- Jira JQL from config (source-of-truth)
- Additional guard checks:
  - ticket not already in state as `PR_OPENED`/`DONE` unless forced
  - ticket has sufficient data: title + description or acceptance criteria
  - if repo mapping ambiguous => mark `NEEDS_HUMAN`

---

## 6) Repo Mapping Strategy (Deterministic)

Implement repo mapping in this order:
1) explicit `workspace.repo_mapping` using Jira fields (component/project/custom)
2) if ticket contains a repo hint in description (optional regex; disabled by default)
3) if still ambiguous => `NEEDS_HUMAN`

Hard rule:
- If mapped repo not in `repo_allowlist` => refuse and mark `NEEDS_HUMAN`

---

## 7) State & Idempotency (SQLite)

Store DB at: `~/.j2pr/state.sqlite`

Tables (minimum):
- `tickets(ticket_key PRIMARY KEY, status, repo, branch, pr_url, last_run_id, updated_at, last_error)`
- `runs(run_id PRIMARY KEY, ticket_key, started_at, finished_at, status, repo, branch, pr_url, artifacts_dir, cursor_exit_code, summary_json)`
- `locks(repo PRIMARY KEY, locked_at, run_id)` (or use file lock mechanism)

Statuses:
- `DISCOVERED`
- `QUEUED`
- `RUNNING`
- `PR_OPENED`
- `DONE`
- `FAILED`
- `NEEDS_HUMAN`

Idempotency rules:
- If a ticket has `PR_OPENED` with a PR URL => return that URL immediately unless `--rerun`.
- Before creating a new PR:
  - check if branch exists remotely for same ticket branch name
  - check if an open PR exists referencing that branch or Jira key
  - if exists => record and return

---

## 8) Run Artifacts & Observability

For each run:
- artifacts root: `~/.j2pr/runs/<TICKET_KEY>/<RUN_ID>/`

Store:
- `ticket.json` (sanitized Jira fields)
- `plan.txt` (agent plan, if provided)
- `agent_transcript.log` (full stdout/stderr from agent call)
- `pre_git_status.txt`, `post_git_status.txt`
- `diff.patch` (final diff)
- `commands.json` (commands invoked by orchestrator, not agent)
- `test_output.log`
- `pr.json` (PR creation response)
- `summary.json` (parsed structured agent footer + orchestrator metadata)

CLI must be able to:
- tail transcript and test output
- print last PR URL
- show the reason for failure/needs-human

Logging:
- also write a global log file `~/.j2pr/j2pr.log`
- redact secrets

---

## 9) Guardrails (Enforced Outside Agent)

Before agent runs:
- ensure repo is present locally or clone it (optional; default: must exist)
- ensure worktree clean (default true)
- ensure base branch up to date:
  - `git fetch`
  - checkout base branch
  - pull/rebase depending on config

While preparing PR:
- block if changed files match deny globs
- block if max files/diff lines exceeded
- block if `command_denylist` is violated in orchestrator (and scrub in logs)

Validation step (orchestrator-owned):
- run format command (optional)
- run test command (default required)
- if tests fail:
  - allow agent one automated fix cycle (config: `guardrails.max_fix_attempts` default 1)
  - otherwise mark `FAILED` or `NEEDS_HUMAN` depending on error class

Absolutely never:
- modify `.github/workflows/*` (default deny glob)
- apply migrations / terraform / k8s by default deny globs
- run destructive commands from orchestrator

---

## 10) Cursor Agent Contract (Critical)

The agent must be invoked with a **single stable prompt** and must produce a **machine-parseable footer** at the end.

### Required footer format (JSON on a single line)
The agent MUST print a final line exactly:

`J2PR_RESULT: { ...json... }`

JSON schema:
- `decision`: `"proceed"` | `"needs_human"` | `"failed"`
- `summary`: string (short)
- `changes`: array of strings (bullets)
- `tests`: { `command`: string, `result`: "pass"|"fail"|"not_run", `notes`: string }
- `risk`: "low"|"medium"|"high"
- `repo`: string
- `branch`: string
- `commit_message`: string
- `notes_for_reviewer`: string
- `blocking_reason`: string (required if needs_human/failed)

If the footer is missing or invalid JSON => mark run `FAILED` with `NEEDS_HUMAN` suggested.

---

## 11) Agent Prompt Content (Provide This Template)

The orchestrator will provide:
- Jira ticket key, title, description, acceptance criteria
- repo path, base branch name
- guardrails (deny globs, max diff, test command, format command)
- explicit “do not touch” areas

### Agent Instructions (must be included)
- Stay within repo.
- Minimal change bias.
- No dependency upgrades unless required for the ticket and small.
- Must add/update tests if change is logic.
- Must run the provided test command locally and report result in footer.
- Never open/merge PR itself unless explicitly configured. (Default: orchestrator opens PR.)
- If ambiguous requirements, choose safest interpretation and note it.

---

## 12) PR Creation Requirements

PR conventions:
- Title: `[<JIRAKEY>] <ticket title>`
- Branch: `j2pr/<JIRAKEY>-<slug>`
- Body sections:
  - Summary
  - Changes
  - How to Test
  - Risk / Rollout Notes
  - Notes for Reviewer

Default: create **Draft PR**.

Linking:
- Ensure Jira key in branch + title so Jira autolinks.

Optional behaviors (config controlled):
- assign reviewers
- add labels
- comment on Jira with PR link

Never merge.

---

## 13) CLI Design

Command group: `j2pr`

### Commands
- `j2pr config-validate`
- `j2pr scan [--limit N] [--json]`
- `j2pr run <JIRAKEY> [--rerun] [--no-comment] [--force]`
- `j2pr run-next` (takes oldest eligible, runs it)
- `j2pr status [--ticket <JIRAKEY>]`
- `j2pr open <JIRAKEY> [--latest]` (prints paths + optionally opens folder)
- `j2pr tail <JIRAKEY> [--latest]` (tails transcript)
- `j2pr clean-locks` (safe cleanup)

Exit codes:
- 0 success (PR opened or already exists)
- 2 needs-human
- 3 failed

---

## 14) Error Handling Rules

Classify errors:
- **Transient** (network, rate limit): retry w/ exponential backoff
- **Auth**: fail fast with clear message
- **Mapping ambiguity**: needs-human
- **Guardrail violation**: needs-human (with reason + list of offending files)
- **Agent contract missing**: needs-human
- **Tests failing**: attempt fix cycle once; then needs-human or failed

Always include:
- what happened
- where artifacts are
- next action suggestion

---

## 15) Deliverables

Produce a working repo with:
- `src/j2pr/` python package
- `pyproject.toml` for packaging
- `README.md` with setup + daily workflow
- example config: `config.example.yaml`
- unit tests for:
  - config parsing + env interpolation
  - repo mapping
  - state/idempotency logic
  - footer parsing
  - deny glob enforcement
- minimal integration test stubs (mock Jira/GH)

---

## 16) Implementation Plan (Expected from you)

Before coding, output:
- a short architecture outline
- data model sketch (SQLite schema)
- command-by-command flow
- risk list + mitigations

Then implement MVP fully.

---

## 17) Acceptance Criteria (MVP is “done” when)

- Running `j2pr scan` lists tickets from Jira JQL.
- Running `j2pr run PAYAD-123` (example) does:
  - maps repo deterministically
  - checks clean worktree
  - creates branch
  - invokes Cursor agent with prompt
  - enforces guardrails
  - runs tests/format per config
  - opens Draft PR
  - prints PR URL
  - stores artifacts + state
- Running `j2pr run PAYAD-123` again returns the existing PR URL (idempotent).

---

## 18) Notes / Assumptions

- The user has local clones of relevant repos.
- Jira + GitHub auth are available as tokens.
- Cursor headless execution is available via configured command.

If any assumption fails, build a graceful fallback and clear messaging.

---

## 19) You Must Ask Zero Questions

Do not ask me clarifying questions. Make reasonable defaults:
- Single repo only by default.
- Draft PR by default.
- Clean worktree required by default.
- Deny globs include `.github/workflows/**`, `**/*.tf`, `k8s/**`, `migrations/**` as safe defaults.

---

## 20) Quick Start UX Target

I should be able to:
1) copy `config.example.yaml` to `~/.j2pr/config.yaml`
2) set env vars for Jira/GitHub tokens
3) run `j2pr scan`
4) run `j2pr run-next`
5) get a PR link + logs every time

Build for that.

