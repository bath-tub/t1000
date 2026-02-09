from pathlib import Path

import j2pr.state as state


def test_ticket_idempotency(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state.sqlite"
    monkeypatch.setattr(state, "DB_PATH", db_path)
    state.init_db()

    ticket = state.TicketState("ABC-1", "PR_OPENED", "repo", "branch", "http://pr", "run1", None)
    state.upsert_ticket(ticket)

    fetched = state.get_ticket("ABC-1")
    assert fetched is not None
    assert fetched.pr_url == "http://pr"
