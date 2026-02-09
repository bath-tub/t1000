import os
from pathlib import Path

from j2pr.config import load_config


def test_env_interpolation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JIRA_TOKEN", "abc123")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
jira:
  base_url: "https://example.atlassian.net"
  email: "test@example.com"
  api_token: "${JIRA_TOKEN}"
  jql: "project = TEST"
  fields: ["summary", "description"]
  comment_on_pr: false
github:
  owner: "org"
  default_base_branch: "main"
  use_gh_cli: true
  draft_pr: true
  token: ""
workspace:
  root_dir: "/tmp"
  repo_allowlist: ["repo"]
  repo_mapping: {}
  single_repo_only: true
guardrails:
  deny_globs: []
  command_denylist: []
  max_files_changed: 10
  max_diff_lines: 100
  require_clean_worktree: true
  require_tests: true
  test_command: "pytest"
  format_command: ""
  max_fix_attempts: 1
cursor:
  command: "cursor-agent"
  model: ""
  timeout_minutes: 45
  prompt_template_path: ""
"""
    )
    result = load_config(str(config_path))
    assert result.config is not None
    assert result.config.jira.api_token == "abc123"
