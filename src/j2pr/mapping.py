from __future__ import annotations

from typing import Dict, Optional


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
