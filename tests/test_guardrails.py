from j2pr.guardrails import matches_deny_glob


def test_matches_deny_glob() -> None:
    deny = [".github/workflows/**", "migrations/**"]
    assert matches_deny_glob(".github/workflows/ci.yml", deny)
    assert matches_deny_glob("migrations/001.sql", deny)
    assert not matches_deny_glob("src/app.py", deny)
