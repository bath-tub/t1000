from j2pr.config import RepoInferenceConfig
from j2pr.mapping import infer_repo_from_issue


def _make_repo(root, name: str, files: dict[str, str]) -> None:
    repo_path = root / name
    (repo_path / ".git").mkdir(parents=True)
    for rel, content in files.items():
        path = repo_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_infer_repo_from_content(tmp_path) -> None:
    _make_repo(
        tmp_path,
        "repo-payments",
        {"services/payments/handler.py": "payment gateway timeout retry logic"},
    )
    _make_repo(
        tmp_path,
        "repo-accounts",
        {"services/accounts/handler.py": "account sync reconciliation"},
    )

    inference = RepoInferenceConfig(enabled=True, min_score=2)
    fields = {"summary": "Payment gateway timeout", "description": "Retry logic needed"}
    repo = infer_repo_from_issue(fields, str(tmp_path), [], inference)
    assert repo == "repo-payments"


def test_infer_repo_respects_min_score(tmp_path) -> None:
    _make_repo(tmp_path, "repo-payments", {"README.md": "payment gateway timeout retry logic"})
    inference = RepoInferenceConfig(enabled=True, min_score=100)
    fields = {"summary": "Payment gateway timeout", "description": "Retry logic needed"}
    repo = infer_repo_from_issue(fields, str(tmp_path), [], inference)
    assert repo is None


def test_infer_repo_with_allowlist(tmp_path) -> None:
    _make_repo(tmp_path, "repo-payments", {"README.md": "payment gateway timeout retry logic"})
    _make_repo(tmp_path, "repo-accounts", {"README.md": "account sync reconciliation"})
    inference = RepoInferenceConfig(enabled=True, min_score=2)
    fields = {"summary": "Payment gateway timeout", "description": "Retry logic needed"}
    repo = infer_repo_from_issue(fields, str(tmp_path), ["repo-accounts"], inference)
    assert repo is None
