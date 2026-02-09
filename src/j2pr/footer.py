from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class AgentFooter:
    decision: str
    summary: str
    changes: list
    tests: dict
    risk: str
    repo: str
    branch: str
    commit_message: str
    notes_for_reviewer: str
    blocking_reason: str


def parse_footer(line: str) -> Optional[AgentFooter]:
    if not line.startswith("J2PR_RESULT:"):
        return None
    raw = line.replace("J2PR_RESULT:", "", 1).strip()
    data = json.loads(raw)
    return AgentFooter(
        decision=data.get("decision", ""),
        summary=data.get("summary", ""),
        changes=data.get("changes", []),
        tests=data.get("tests", {}),
        risk=data.get("risk", ""),
        repo=data.get("repo", ""),
        branch=data.get("branch", ""),
        commit_message=data.get("commit_message", ""),
        notes_for_reviewer=data.get("notes_for_reviewer", ""),
        blocking_reason=data.get("blocking_reason", ""),
    )
