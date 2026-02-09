from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from .repo import diff_name_only, diff_numstat


def matches_deny_glob(path: str, deny_globs: List[str]) -> bool:
    path_obj = Path(path)
    for pattern in deny_globs:
        if path_obj.match(pattern):
            return True
    return False


def enforce_deny_globs(cwd: Path, deny_globs: List[str]) -> Tuple[bool, List[str]]:
    blocked = []
    for name in diff_name_only(cwd):
        if matches_deny_glob(name, deny_globs):
            blocked.append(name)
    return (len(blocked) == 0), blocked


def enforce_diff_limits(cwd: Path, max_files: int, max_lines: int) -> Tuple[bool, int, int]:
    files = diff_name_only(cwd)
    numstat = diff_numstat(cwd)
    lines = sum(added + removed for added, removed, _ in numstat)
    if len(files) > max_files or lines > max_lines:
        return False, len(files), lines
    return True, len(files), lines
