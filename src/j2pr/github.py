from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests

from .util import run_command


@dataclass
class PRInfo:
    url: str
    number: int


def _gh_available() -> bool:
    result = run_command(["gh", "--version"])
    return result.returncode == 0


def find_pr_with_gh(branch: str, cwd: Optional[Path] = None) -> Optional[str]:
    result = run_command(
        ["gh", "pr", "list", "--state", "open", "--head", branch, "--json", "url"],
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout or "[]")
    if data:
        return data[0].get("url")
    return None


def find_pr_by_jira_with_gh(jira_key: str, cwd: Optional[Path] = None) -> Optional[str]:
    result = run_command(
        ["gh", "pr", "list", "--state", "open", "--search", jira_key, "--json", "url"],
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout or "[]")
    if data:
        return data[0].get("url")
    return None


def create_pr_with_gh(
    title: str,
    body: str,
    base: str,
    head: str,
    draft: bool,
    reviewers: List[str],
    labels: List[str],
    cwd: Optional[Path] = None,
) -> str:
    cmd = ["gh", "pr", "create", "--title", title, "--body", body, "--base", base, "--head", head]
    if draft:
        cmd.append("--draft")
    for reviewer in reviewers:
        cmd.extend(["--reviewer", reviewer])
    for label in labels:
        cmd.extend(["--label", label])
    result = run_command(cmd, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "gh pr create failed")
    return result.stdout.strip().splitlines()[-1]


def find_pr_with_rest(owner: str, repo: str, branch: str, token: str) -> Optional[str]:
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    query = f"repo:{owner}/{repo}+type:pr+head:{owner}:{branch}+state:open"
    url = f"https://api.github.com/search/issues?q={query}"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])
    if items:
        return items[0].get("html_url")
    return None


def find_pr_by_jira_with_rest(owner: str, repo: str, jira_key: str, token: str) -> Optional[str]:
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    query = f"repo:{owner}/{repo}+type:pr+state:open+{jira_key}"
    url = f"https://api.github.com/search/issues?q={query}"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])
    if items:
        return items[0].get("html_url")
    return None


def create_pr_with_rest(
    owner: str,
    repo: str,
    token: str,
    title: str,
    body: str,
    base: str,
    head: str,
    draft: bool,
) -> str:
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    payload = {"title": title, "body": body, "base": base, "head": head, "draft": draft}
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()["html_url"]


def ensure_gh() -> None:
    if not _gh_available():
        raise RuntimeError("gh CLI not found; disable use_gh_cli or install gh")
