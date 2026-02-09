# J2PR

Local CLI that turns eligible Jira tickets into Draft PR links using a headless Cursor agent.

## Quick Start

1. Install:
   - `python -m venv .venv && source .venv/bin/activate`
   - `pip install -e ".[dev]"`
2. Copy config:
   - `cp config.example.yaml ~/.j2pr/config.yaml`
3. Set env vars for secrets referenced in config.
4. Run:
   - `j2pr config-validate`
   - `j2pr scan`
   - `j2pr run-next`

## Daily Workflow

- `j2pr scan` to list eligible tickets.
- `j2pr run <JIRAKEY>` to run a specific ticket.
- `j2pr status --ticket <JIRAKEY>` to inspect current status.
- `j2pr tail <JIRAKEY>` to stream the agent transcript.

## Repo Layout

- `src/j2pr/` core implementation
- `tests/` unit tests
- `config.example.yaml` starter config

## Notes

- Uses `gh` CLI by default for PR creation; falls back to GitHub REST if configured.
- Stores state in `~/.j2pr/state.sqlite` and run artifacts under `~/.j2pr/runs/`.
