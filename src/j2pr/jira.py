from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import requests


@dataclass
class JiraIssue:
    key: str
    fields: Dict[str, object]


def _auth(email: str, api_token: str) -> tuple:
    return (email, api_token)


def search_issues(
    base_url: str,
    email: str,
    api_token: str,
    jql: str,
    fields: List[str],
    limit: int = 20,
) -> List[JiraIssue]:
    url = f"{base_url.rstrip('/')}/rest/api/3/search"
    payload = {"jql": jql, "maxResults": limit, "fields": fields}
    resp = requests.post(url, json=payload, auth=_auth(email, api_token), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    issues = []
    for issue in data.get("issues", []):
        issues.append(JiraIssue(issue["key"], issue.get("fields", {})))
    return issues


def add_comment(
    base_url: str,
    email: str,
    api_token: str,
    issue_key: str,
    comment: str,
) -> None:
    url = f"{base_url.rstrip('/')}/rest/api/3/issue/{issue_key}/comment"
    payload = {"body": comment}
    resp = requests.post(url, json=payload, auth=_auth(email, api_token), timeout=30)
    resp.raise_for_status()
