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


def detect_default_branch(cwd: Path) -> str | None:
    """Ask the remote which branch HEAD points to (e.g. main, develop, master)."""
    result = run_command(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd)
    ref = result.stdout.strip()          # e.g. refs/remotes/origin/develop
    if ref:
        return ref.split("/")[-1]
    # symbolic-ref can be unset; fall back to `git remote show origin`
    result = run_command(["git", "remote", "show", "origin"], cwd=cwd)
    for line in result.stdout.splitlines():
        if "HEAD branch" in line:
            return line.split(":")[-1].strip()
    return None


def fetch_and_checkout_base(cwd: Path, base_branch: str) -> None:
    """Fetch from all remotes and reset to a pristine copy of the base branch.

    Any uncommitted changes or untracked files left over from a previous agent
    run are discarded so the new ticket starts from a clean slate.
    """
    run_command(["git", "fetch", "--all"], cwd=cwd)
    # Discard any in-progress changes so checkout cannot fail or carry over
    # stale work from a previous ticket.
    run_command(["git", "checkout", "--force", base_branch], cwd=cwd)
    run_command(["git", "reset", "--hard", f"origin/{base_branch}"], cwd=cwd)
    run_command(["git", "clean", "-fd"], cwd=cwd)


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


def detect_test_command(cwd: Path) -> str | None:
    """Auto-detect the test command for a repo by inspecting build files.

    Returns a shell command string, or ``None`` if nothing recognised is found.

    Detection order (first match wins):
      1. package.json  → ``npm test``
      2. build.gradle / build.gradle.kts  → ``./gradlew test``
      3. pom.xml  → ``mvn test``
    """
    if (cwd / "package.json").exists():
        return "npm test"
    if (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists():
        return "./gradlew test"
    if (cwd / "pom.xml").exists():
        return "mvn test"
    return None


def remote_branch_exists(cwd: Path, branch: str) -> bool:
    result = run_command(["git", "ls-remote", "--heads", "origin", branch], cwd=cwd)
    return bool(result.stdout.strip())
