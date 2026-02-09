from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import RepoInferenceConfig


def map_repo(
    jira_fields: Dict[str, object],
    repo_mapping: Dict[str, str],
) -> Optional[str]:
    for key, repo in repo_mapping.items():
        if ":" in key or "=" in key:
            sep = ":" if ":" in key else "="
            field, expected = key.split(sep, 1)
            value = jira_fields.get(field)
            if isinstance(value, list):
                if expected in [str(v) for v in value]:
                    return repo
            elif value is not None and str(value) == expected:
                return repo
        elif key in jira_fields:
            return repo
    return None


_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-./]{2,}")
_STOPWORDS = {
    "the",
    "and",
    "with",
    "from",
    "that",
    "this",
    "have",
    "has",
    "had",
    "are",
    "was",
    "were",
    "you",
    "your",
    "for",
    "not",
    "but",
    "all",
    "any",
    "can",
    "use",
    "using",
    "into",
    "over",
    "after",
    "before",
    "when",
    "then",
    "than",
    "via",
    "also",
    "its",
    "it",
    "our",
    "their",
    "they",
    "them",
    "more",
    "most",
    "less",
    "least",
    "some",
    "such",
    "should",
    "could",
    "would",
    "may",
    "might",
    "must",
    "shall",
    "etc",
    "n/a",
    "none",
    "todo",
}


def infer_repo_from_issue(
    jira_fields: Dict[str, object],
    root_dir: str,
    repo_allowlist: Sequence[str],
    inference: RepoInferenceConfig,
) -> Optional[str]:
    if not inference.enabled:
        return None
    ticket_text = _extract_ticket_text(jira_fields)
    tokens = _extract_tokens(ticket_text, inference.max_tokens)
    if not tokens:
        return None
    repo_paths = _discover_repos(root_dir, repo_allowlist, inference.max_repos)
    if not repo_paths:
        return None

    start = time.monotonic()
    scored: List[Tuple[float, str]] = []
    for repo_path in repo_paths:
        if _timed_out(start, inference.max_seconds):
            break
        name_score = _score_repo_name(tokens, repo_path.name)
        content_score = _score_repo_content(repo_path, tokens, inference, start)
        total = name_score + content_score
        if total > 0:
            scored.append((total, repo_path.name))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    top_score, top_repo = scored[0]
    if top_score < inference.min_score:
        return None
    if len(scored) > 1 and scored[1][0] == top_score:
        return None
    return top_repo


def _extract_ticket_text(jira_fields: Dict[str, object]) -> str:
    summary = str(jira_fields.get("summary") or "")
    description = jira_fields.get("description")
    if isinstance(description, dict) and "content" in description:
        desc = json.dumps(description)
    elif isinstance(description, str):
        desc = description
    else:
        desc = ""
    return f"{summary}\n{desc}".strip()


def _extract_tokens(text: str, max_tokens: int) -> List[str]:
    if not text:
        return []
    raw = [token.lower() for token in _TOKEN_RE.findall(text)]
    filtered = [token for token in raw if token not in _STOPWORDS and len(token) >= 3]
    counts = Counter(filtered)
    return [token for token, _ in counts.most_common(max_tokens)]


def _discover_repos(
    root_dir: str,
    repo_allowlist: Sequence[str],
    max_repos: int,
) -> List[Path]:
    root = Path(root_dir).expanduser()
    repos: List[Path] = []
    if repo_allowlist:
        for repo in repo_allowlist:
            repo_path = root / repo
            if repo_path.is_dir() and (repo_path / ".git").exists():
                repos.append(repo_path)
        return repos

    if not root.exists():
        return []
    for entry in root.iterdir():
        if entry.is_dir() and (entry / ".git").exists():
            repos.append(entry)
            if max_repos > 0 and len(repos) >= max_repos:
                break
    return repos


def _score_repo_name(tokens: Iterable[str], repo_name: str) -> float:
    name_tokens = {token for token in re.split(r"[^A-Za-z0-9]+", repo_name.lower()) if token}
    if not name_tokens:
        return 0.0
    score = 0.0
    for token in tokens:
        if token in name_tokens:
            score += 2.0
    return score


def _score_repo_content(
    repo_path: Path,
    tokens: Sequence[str],
    inference: RepoInferenceConfig,
    start: float,
) -> float:
    score = 0.0
    matched: Set[str] = set()
    files_checked = 0
    total_checked = 0

    for path in _iter_repo_files(repo_path, inference):
        if _timed_out(start, inference.max_seconds):
            break
        if files_checked >= inference.max_files_per_repo:
            break
        if total_checked >= inference.max_total_files:
            break
        total_checked += 1

        rel_path = str(path.relative_to(repo_path)).lower()
        for token in tokens:
            if token in matched:
                continue
            if token in rel_path:
                matched.add(token)
                score += 1.0

        try:
            content = _read_text_file(path, inference.max_bytes_per_file)
        except OSError:
            continue
        if content is None:
            continue

        content_lower = content.lower()
        for token in tokens:
            if token in matched:
                continue
            if token in content_lower:
                matched.add(token)
                score += 2.0
        files_checked += 1
    return score


def _iter_repo_files(repo_path: Path, inference: RepoInferenceConfig) -> Iterable[Path]:
    tracked = _git_ls_files(repo_path)
    if tracked:
        for rel in tracked:
            path = repo_path / rel
            if _skip_path(path, inference):
                continue
            yield path
        return

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in inference.ignore_dirs]
        for filename in files:
            path = Path(root) / filename
            if _skip_path(path, inference):
                continue
            yield path


def _git_ls_files(repo_path: Path) -> List[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "ls-files"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _skip_path(path: Path, inference: RepoInferenceConfig) -> bool:
    if not path.is_file():
        return True
    if path.suffix.lower() in {ext.lower() for ext in inference.ignore_extensions}:
        return True
    for part in path.parts:
        if part in inference.ignore_dirs:
            return True
    return False


def _read_text_file(path: Path, max_bytes: int) -> Optional[str]:
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if b"\x00" in raw:
        return None
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    return raw.decode("utf-8", errors="ignore")


def _timed_out(start: float, max_seconds: int) -> bool:
    if max_seconds <= 0:
        return False
    return (time.monotonic() - start) > max_seconds
