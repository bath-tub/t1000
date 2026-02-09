from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError

DEFAULT_CONFIG_PATH = Path("~/.j2pr/config.yaml").expanduser()

ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class JiraConfig(BaseModel):
    base_url: str
    email: str
    api_token: str
    api_version: int = 3
    jql: str
    fields: List[str]
    comment_on_pr: bool = False
    label_running: Optional[str] = None
    label_done: Optional[str] = None
    label_failed: Optional[str] = None


class GitHubConfig(BaseModel):
    owner: str
    default_base_branch: str
    use_gh_cli: bool = True
    draft_pr: bool = True
    token: str = ""
    reviewers: List[str] = Field(default_factory=list)
    labels: List[str] = Field(default_factory=list)


class RepoInferenceConfig(BaseModel):
    enabled: bool = False
    min_score: float = 3.0
    max_repos: int = 0
    max_files_per_repo: int = 400
    max_total_files: int = 8000
    max_bytes_per_file: int = 200_000
    max_tokens: int = 80
    max_seconds: int = 60
    ignore_dirs: List[str] = Field(
        default_factory=lambda: [
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "dist",
            "build",
            ".tox",
            ".mypy_cache",
            ".pytest_cache",
        ]
    )
    ignore_extensions: List[str] = Field(
        default_factory=lambda: [
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".pdf",
            ".zip",
            ".gz",
            ".tgz",
            ".bz2",
            ".xz",
            ".tar",
            ".7z",
            ".mp3",
            ".mp4",
            ".mov",
            ".avi",
            ".wav",
            ".webm",
            ".woff",
            ".woff2",
            ".ttf",
            ".otf",
        ]
    )


class WorkspaceConfig(BaseModel):
    root_dir: str
    repo_allowlist: List[str]
    repo_mapping: Dict[str, str] = Field(default_factory=dict)
    single_repo_only: bool = True
    repo_inference: RepoInferenceConfig = Field(default_factory=RepoInferenceConfig)


class GuardrailsConfig(BaseModel):
    deny_globs: List[str] = Field(default_factory=list)
    command_denylist: List[str] = Field(default_factory=list)
    max_files_changed: int = 40
    max_diff_lines: int = 2000
    require_clean_worktree: bool = True
    require_tests: bool = True
    test_command: str = "auto"
    format_command: str = ""
    max_fix_attempts: int = 1


class CursorConfig(BaseModel):
    command: str
    model: str = ""
    timeout_minutes: int = 45
    prompt_template_path: str = ""


class SessionCaptureConfig(BaseModel):
    enabled: bool = False
    output_dir: str = "~/.j2pr/sessions"
    include_config: bool = True
    include_env: bool = True
    retention_days: int = 0
    redact_patterns: List[str] = Field(
        default_factory=lambda: ["token", "password", "secret", "api_key"]
    )


class AppConfig(BaseModel):
    jira: JiraConfig
    github: GitHubConfig
    workspace: WorkspaceConfig
    guardrails: GuardrailsConfig
    cursor: CursorConfig
    session_capture: SessionCaptureConfig = Field(default_factory=SessionCaptureConfig)


@dataclass
class ConfigResult:
    config: Optional[AppConfig]
    errors: List[str]


def _interpolate_env(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            env_key = match.group(1)
            return os.environ.get(env_key, "")

        return ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    return value


def load_config(path: Optional[str] = None) -> ConfigResult:
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return ConfigResult(None, [f"Config not found at {config_path}"])

    raw = yaml.safe_load(config_path.read_text()) or {}
    interpolated = _interpolate_env(raw)
    try:
        return ConfigResult(AppConfig.model_validate(interpolated), [])
    except ValidationError as exc:
        return ConfigResult(None, [str(err) for err in exc.errors()])


def config_path_from_env() -> Optional[str]:
    return os.environ.get("J2PR_CONFIG")
