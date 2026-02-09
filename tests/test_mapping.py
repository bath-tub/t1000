from j2pr.mapping import map_repo


def test_map_repo_field_present() -> None:
    fields = {"project": "PAYAD"}
    mapping = {"project": "repo-a"}
    assert map_repo(fields, mapping) == "repo-a"


def test_map_repo_field_value() -> None:
    fields = {"component": "payments"}
    mapping = {"component:payments": "repo-pay"}
    assert map_repo(fields, mapping) == "repo-pay"
