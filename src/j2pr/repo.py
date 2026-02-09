from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from .util import run_command


def git_status(cwd: Path) -> str:
    result = run_command(["git", "status", "--porcelain"], cwd=cwd)
    return result.stdout.strip()


def ensure_clean_worktree(cwd: Path) -> Tuple[bool, str]:
    status = git_status(cwd)
    return (status == ""), status


def fetch_and_checkout_base(cwd: Path, base_branch: str) -> None:
    run_command(["git", "fetch", "--all"], cwd=cwd)
    run_command(["git", "checkout", base_branch], cwd=cwd)
    run_command(["git", "pull", "--rebase"], cwd=cwd)


def create_branch(cwd: Path, branch: str) -> None:
    run_command(["git", "checkout", "-B", branch], cwd=cwd)


def diff_name_only(cwd: Path) -> List[str]:
    result = run_command(["git", "diff", "--name-only"], cwd=cwd)
    return [line for line in result.stdout.splitlines() if line.strip()]


def diff_numstat(cwd: Path) -> List[Tuple[int, int, str]]:
    result = run_command(["git", "diff", "--numstat"], cwd=cwd)
    entries = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            added = int(parts[0]) if parts[0].isdigit() else 0
            removed = int(parts[1]) if parts[1].isdigit() else 0
            entries.append((added, removed, parts[2]))
    return entries


def diff_patch(cwd: Path) -> str:
    result = run_command(["git", "diff"], cwd=cwd)
    return result.stdout


def remote_branch_exists(cwd: Path, branch: str) -> bool:
    result = run_command(["git", "ls-remote", "--heads", "origin", branch], cwd=cwd)
    return bool(result.stdout.strip())
