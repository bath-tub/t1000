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
    api_version: int,
    jql: str,
    fields: List[str],
    limit: int = 20,
) -> List[JiraIssue]:
    base = base_url.rstrip("/")
    headers = {"Accept": "application/json"}
    auth = _auth(email, api_token)
    payload = {"jql": jql, "maxResults": limit, "fields": fields}

    # Some Jira instances expose /search/jql; fall back to /search if needed.
    new_url = f"{base}/rest/api/{api_version}/search/jql"
    resp = requests.post(new_url, json=payload, auth=auth, headers=headers, timeout=30)

    if resp.status_code in {404, 405, 410}:
        # Server / DC may not have /search/jql — fall back to legacy.
        legacy_url = f"{base}/rest/api/{api_version}/search"
        resp = requests.post(legacy_url, json=payload, auth=auth, headers=headers, timeout=30)
        if resp.status_code in {405, 410}:
            params = {"jql": jql, "maxResults": limit, "fields": ",".join(fields)}
            resp = requests.get(legacy_url, params=params, auth=auth, headers=headers, timeout=30)

    if resp.status_code >= 400:
        raise RuntimeError(_format_error("Jira search failed", resp))
    data = resp.json()
    issues = []
    for issue in data.get("issues", []):
        issues.append(JiraIssue(issue["key"], issue.get("fields", {})))
    return issues


def add_comment(
    base_url: str,
    email: str,
    api_token: str,
    api_version: int,
    issue_key: str,
    comment: str,
) -> None:
    url = f"{base_url.rstrip('/')}/rest/api/{api_version}/issue/{issue_key}/comment"
    payload = {"body": comment}
    resp = requests.post(url, json=payload, auth=_auth(email, api_token), timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(_format_error("Jira add comment failed", resp))


def _format_error(prefix: str, resp: requests.Response) -> str:
    text = resp.text.strip()
    if len(text) > 500:
        text = text[:500] + "..."
    return f"{prefix} ({resp.status_code}): {resp.url} {text}"
