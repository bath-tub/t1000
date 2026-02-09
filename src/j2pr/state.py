from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DB_PATH = Path("~/.j2pr/state.sqlite").expanduser()


@dataclass
class TicketState:
    ticket_key: str
    status: str
    repo: Optional[str]
    branch: Optional[str]
    pr_url: Optional[str]
    last_run_id: Optional[str]
    last_error: Optional[str]


@dataclass
class RunState:
    run_id: str
    ticket_key: str
    status: str
    repo: Optional[str]
    branch: Optional[str]
    pr_url: Optional[str]
    artifacts_dir: Optional[str]
    cursor_exit_code: Optional[int]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_key TEXT PRIMARY KEY,
            status TEXT,
            repo TEXT,
            branch TEXT,
            pr_url TEXT,
            last_run_id TEXT,
            updated_at TEXT,
            last_error TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            ticket_key TEXT,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            repo TEXT,
            branch TEXT,
            pr_url TEXT,
            artifacts_dir TEXT,
            cursor_exit_code INTEGER,
            summary_json TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS locks (
            repo TEXT PRIMARY KEY,
            locked_at TEXT,
            run_id TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_ticket(ticket_key: str) -> Optional[TicketState]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE ticket_key = ?", (ticket_key,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return TicketState(
        ticket_key=row["ticket_key"],
        status=row["status"],
        repo=row["repo"],
        branch=row["branch"],
        pr_url=row["pr_url"],
        last_run_id=row["last_run_id"],
        last_error=row["last_error"],
    )


def upsert_ticket(state: TicketState) -> None:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO tickets (ticket_key, status, repo, branch, pr_url, last_run_id, updated_at, last_error)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
        ON CONFLICT(ticket_key) DO UPDATE SET
            status=excluded.status,
            repo=excluded.repo,
            branch=excluded.branch,
            pr_url=excluded.pr_url,
            last_run_id=excluded.last_run_id,
            updated_at=datetime('now'),
            last_error=excluded.last_error
        """,
        (
            state.ticket_key,
            state.status,
            state.repo,
            state.branch,
            state.pr_url,
            state.last_run_id,
            state.last_error,
        ),
    )
    conn.commit()
    conn.close()


def add_run(run: RunState) -> None:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO runs (run_id, ticket_key, started_at, status, repo, branch, pr_url, artifacts_dir, cursor_exit_code)
        VALUES (?, ?, datetime('now'), ?, ?, ?, ?, ?, ?)
        """,
        (
            run.run_id,
            run.ticket_key,
            run.status,
            run.repo,
            run.branch,
            run.pr_url,
            run.artifacts_dir,
            run.cursor_exit_code,
        ),
    )
    conn.commit()
    conn.close()


def finish_run(run_id: str, status: str, pr_url: Optional[str], cursor_exit_code: Optional[int]) -> None:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE runs
        SET finished_at=datetime('now'), status=?, pr_url=?, cursor_exit_code=?
        WHERE run_id=?
        """,
        (status, pr_url, cursor_exit_code, run_id),
    )
    conn.commit()
    conn.close()


def set_lock(repo: str, run_id: str) -> None:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO locks (repo, locked_at, run_id)
        VALUES (?, datetime('now'), ?)
        ON CONFLICT(repo) DO UPDATE SET locked_at=datetime('now'), run_id=excluded.run_id
        """,
        (repo, run_id),
    )
    conn.commit()
    conn.close()


def clear_lock(repo: str) -> None:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM locks WHERE repo = ?", (repo,))
    conn.commit()
    conn.close()


def get_lock(repo: str) -> Optional[str]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT run_id FROM locks WHERE repo = ?", (repo,))
    row = cur.fetchone()
    conn.close()
    return row["run_id"] if row else None


def clear_all_locks() -> int:
    """Delete every row in the locks table. Returns count of rows removed."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM locks")
    count = cur.fetchone()[0]
    cur.execute("DELETE FROM locks")
    conn.commit()
    conn.close()
    return count


def dump_table(table_name: str) -> list[dict]:
    """Return all rows from *table_name* as a list of dicts."""
    allowed = {"tickets", "runs", "locks"}
    if table_name not in allowed:
        raise ValueError(f"Unknown table: {table_name}")
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table_name}")  # noqa: S608 – table name is allow-listed
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
