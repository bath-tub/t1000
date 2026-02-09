from __future__ import annotations

import json
import os
import shlex
import uuid
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .agent import run_agent
from .artifacts import artifacts_root, write_artifact_json, write_artifacts
from .config import config_path_from_env, load_config
from .footer import AgentFooter
from .github import (
    create_pr_with_gh,
    create_pr_with_rest,
    ensure_gh,
    find_pr_with_gh,
    find_pr_by_jira_with_gh,
    find_pr_with_rest,
    find_pr_by_jira_with_rest,
)
from .guardrails import enforce_deny_globs, enforce_diff_limits
from .jira import add_comment, search_issues
from .logging import log_event, setup_logger
from .mapping import map_repo
from .repo import (
    create_branch,
    diff_patch,
    ensure_clean_worktree,
    fetch_and_checkout_base,
    remote_branch_exists,
)
from .state import (
    RunState,
    TicketState,
    add_run,
    clear_lock,
    get_lock,
    get_ticket,
    init_db,
    set_lock,
    upsert_ticket,
    finish_run,
)
from .util import run_command

app = typer.Typer(no_args_is_help=True)
console = Console()


def _load_config_or_exit() -> tuple:
    config_path = config_path_from_env()
    result = load_config(config_path)
    if not result.config:
        for err in result.errors:
            console.print(f"[red]Config error:[/red] {err}")
        raise typer.Exit(code=2)
    return result.config


def _slug(text: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in text).strip("-")


def _extract_description(fields: dict) -> str:
    desc = fields.get("description")
    if isinstance(desc, dict) and "content" in desc:
        return json.dumps(desc)
    if isinstance(desc, str):
        return desc
    return ""


def _acceptance_from_description(description: str) -> str:
    marker = "Acceptance Criteria"
    if marker in description:
        return description.split(marker, 1)[-1].strip()
    return ""


def _ticket_ok(fields: dict) -> bool:
    title = fields.get("summary")
    description = _extract_description(fields)
    return bool(title and description)


def _repo_path(root_dir: str, repo: str) -> Path:
    return Path(root_dir).expanduser() / repo


def _pr_body(footer: AgentFooter, test_command: str) -> str:
    changes = "\n".join(f"- {item}" for item in footer.changes) if footer.changes else "- n/a"
    return "\n".join(
        [
            "## Summary",
            footer.summary or "n/a",
            "",
            "## Changes",
            changes,
            "",
            "## How to Test",
            f"- {test_command}",
            "",
            "## Risk / Rollout Notes",
            footer.risk or "n/a",
            "",
            "## Notes for Reviewer",
            footer.notes_for_reviewer or "n/a",
        ]
    )


def _github_token(config) -> str:
    if config.github.token:
        return config.github.token
    return os.environ.get("GITHUB_TOKEN", "")


def _require_github_token(config) -> str:
    token = _github_token(config)
    if not token:
        raise RuntimeError("GitHub token required for REST operations")
    return token


def _denylist_ok(commands: list[str], denylist: list[str]) -> bool:
    if not denylist:
        return True
    joined = " ; ".join(commands)
    for denied in denylist:
        if denied in joined:
            return False
    return True


def _classify_error(message: str) -> str:
    needs_human_markers = [
        "Worktree not clean",
        "Deny glob violation",
        "Diff limits exceeded",
        "Agent contract missing footer",
        "Tests failed",
        "Repo mapping ambiguous",
    ]
    for marker in needs_human_markers:
        if marker in message:
            return "NEEDS_HUMAN"
    return "FAILED"


@app.command()
def config_validate() -> None:
    load_result = load_config(config_path_from_env())
    if load_result.config:
        console.print("[green]Config valid[/green]")
        raise typer.Exit(code=0)
    for err in load_result.errors:
        console.print(f"[red]Config error:[/red] {err}")
    raise typer.Exit(code=2)


@app.command()
def scan(limit: int = 20, json_output: bool = typer.Option(False, "--json")) -> None:
    config = _load_config_or_exit()
    init_db()
    issues = search_issues(
        config.jira.base_url,
        config.jira.email,
        config.jira.api_token,
        config.jira.jql,
        config.jira.fields,
        limit,
    )
    rows = []
    for issue in issues:
        rows.append({"key": issue.key, "summary": issue.fields.get("summary", "")})
    if not json_output:
        table = Table(title="Eligible Tickets")
        table.add_column("Key")
        table.add_column("Summary")
        for row in rows:
            table.add_row(row["key"], str(row["summary"]))
        console.print(table)
    if json_output:
        console.print(json.dumps(rows, indent=2))


@app.command()
def run(
    jira_key: str,
    rerun: bool = False,
    no_comment: bool = False,
    force: bool = False,
) -> None:
    config = _load_config_or_exit()
    init_db()
    logger = setup_logger()

    ticket = get_ticket(jira_key)
    if ticket and ticket.status in {"PR_OPENED", "DONE"} and ticket.pr_url and not rerun:
        console.print(ticket.pr_url)
        raise typer.Exit(code=0)

    issues = search_issues(
        config.jira.base_url,
        config.jira.email,
        config.jira.api_token,
        f"key = {jira_key}",
        config.jira.fields,
        1,
    )
    if not issues:
        console.print(f"[red]Ticket {jira_key} not found[/red]")
        raise typer.Exit(code=3)
    issue = issues[0]

    if not _ticket_ok(issue.fields) and not force:
        upsert_ticket(
            TicketState(jira_key, "NEEDS_HUMAN", None, None, None, None, "Missing summary/description")
        )
        console.print("[yellow]Needs human: missing summary/description[/yellow]")
        raise typer.Exit(code=2)

    repo = map_repo(issue.fields, config.workspace.repo_mapping)
    if repo is None and config.workspace.single_repo_only and len(config.workspace.repo_allowlist) == 1:
        repo = config.workspace.repo_allowlist[0]
    if not repo or repo not in config.workspace.repo_allowlist:
        upsert_ticket(
            TicketState(jira_key, "NEEDS_HUMAN", None, None, None, None, "Repo mapping ambiguous or not allowed")
        )
        console.print("[yellow]Needs human: repo mapping ambiguous or not allowed[/yellow]")
        raise typer.Exit(code=2)

    run_id = uuid.uuid4().hex
    lock = get_lock(repo)
    if lock and lock != run_id:
        console.print(f"[yellow]Repo locked by run {lock}[/yellow]")
        raise typer.Exit(code=2)

    set_lock(repo, run_id)
    repo_path = _repo_path(config.workspace.root_dir, repo)
    if not repo_path.exists():
        clear_lock(repo)
        console.print("[red]Repo not found locally[/red]")
        raise typer.Exit(code=3)

    run_state = RunState(run_id, jira_key, "RUNNING", repo, None, None, str(artifacts_root(jira_key, run_id)), None)
    add_run(run_state)
    upsert_ticket(TicketState(jira_key, "RUNNING", repo, None, None, run_id, None))
    log_event(logger, "run_started", {"ticket": jira_key, "repo": repo, "run_id": run_id})

    artifacts_dir = artifacts_root(jira_key, run_id)
    write_artifact_json(artifacts_dir, "ticket.json", issue.fields)

    title = str(issue.fields.get("summary", ""))
    description = _extract_description(issue.fields)
    acceptance = _acceptance_from_description(description)
    branch = f"j2pr/{jira_key}-{_slug(title)[:50]}"

    commands = []

    try:
        if config.guardrails.require_clean_worktree:
            clean, status = ensure_clean_worktree(repo_path)
            write_artifacts(artifacts_dir, {"pre_git_status.txt": status})
            if not clean and not force:
                raise RuntimeError("Worktree not clean")

        fetch_and_checkout_base(repo_path, config.github.default_base_branch)
        create_branch(repo_path, branch)
        commands.extend(
            [
                f"git fetch --all",
                f"git checkout {config.github.default_base_branch}",
                f"git pull --rebase",
                f"git checkout -B {branch}",
            ]
        )
        if not _denylist_ok(commands, config.guardrails.command_denylist):
            raise RuntimeError("Command denylist violation")

        prompt_vars = {
            "ticket_key": jira_key,
            "title": title,
            "description": description,
            "acceptance": acceptance,
            "repo_path": str(repo_path),
            "base_branch": config.github.default_base_branch,
            "deny_globs": ", ".join(config.guardrails.deny_globs),
            "max_files": str(config.guardrails.max_files_changed),
            "max_lines": str(config.guardrails.max_diff_lines),
            "test_command": config.guardrails.test_command,
            "format_command": config.guardrails.format_command,
            "do_not_touch": ", ".join(config.guardrails.deny_globs),
            "notes_for_agent": "",
        }

        agent_result = None
        fix_attempts = 0
        while True:
            agent_result = run_agent(
                config.cursor.command,
                repo_path,
                prompt_vars,
                config.cursor.timeout_minutes,
                artifacts_dir / "agent_transcript.log",
                config.cursor.prompt_template_path or None,
            )
            if not agent_result.footer:
                raise RuntimeError("Agent contract missing footer")

            if config.guardrails.format_command:
                fmt = run_command(shlex.split(config.guardrails.format_command), cwd=repo_path)
                commands.append(config.guardrails.format_command)
                write_artifacts(artifacts_dir, {"format_output.log": fmt.stdout + fmt.stderr})
                if not _denylist_ok(commands, config.guardrails.command_denylist):
                    raise RuntimeError("Command denylist violation")

            if config.guardrails.require_tests:
                test = run_command(shlex.split(config.guardrails.test_command), cwd=repo_path)
                commands.append(config.guardrails.test_command)
                write_artifacts(artifacts_dir, {"test_output.log": test.stdout + test.stderr})
                if not _denylist_ok(commands, config.guardrails.command_denylist):
                    raise RuntimeError("Command denylist violation")
                if test.returncode != 0:
                    fix_attempts += 1
                    if fix_attempts > config.guardrails.max_fix_attempts:
                        raise RuntimeError("Tests failed")
                    prompt_vars["notes_for_agent"] = "Tests failed; please fix and re-run tests."
                    continue
            break

        ok, blocked = enforce_deny_globs(repo_path, config.guardrails.deny_globs)
        if not ok:
            raise RuntimeError(f"Deny glob violation: {', '.join(blocked)}")
        ok, files_changed, lines_changed = enforce_diff_limits(
            repo_path, config.guardrails.max_files_changed, config.guardrails.max_diff_lines
        )
        if not ok:
            raise RuntimeError(f"Diff limits exceeded: {files_changed} files, {lines_changed} lines")

        write_artifacts(
            artifacts_dir,
            {
                "post_git_status.txt": ensure_clean_worktree(repo_path)[1],
                "diff.patch": diff_patch(repo_path),
                "commands.json": json.dumps(commands, indent=2),
            },
        )

        if remote_branch_exists(repo_path, branch):
            pr_url = find_pr_with_gh(branch) if config.github.use_gh_cli else None
            if not pr_url:
                pr_url = find_pr_with_rest(
                    config.github.owner,
                    repo,
                    branch,
                    _require_github_token(config),
                )
            if pr_url:
                upsert_ticket(TicketState(jira_key, "PR_OPENED", repo, branch, pr_url, run_id, None))
                finish_run(run_id, "PR_OPENED", pr_url, agent_result.exit_code)
                clear_lock(repo)
                console.print(pr_url)
                raise typer.Exit(code=0)

        pr_url = find_pr_by_jira_with_gh(jira_key) if config.github.use_gh_cli else None
        if not pr_url:
            pr_url = find_pr_by_jira_with_rest(
                config.github.owner,
                repo,
                jira_key,
                _require_github_token(config),
            )
        if pr_url:
            upsert_ticket(TicketState(jira_key, "PR_OPENED", repo, branch, pr_url, run_id, None))
            finish_run(run_id, "PR_OPENED", pr_url, agent_result.exit_code)
            clear_lock(repo)
            console.print(pr_url)
            raise typer.Exit(code=0)

        pr_title = f"[{jira_key}] {title}"
        pr_body = _pr_body(agent_result.footer, config.guardrails.test_command)

        if config.github.use_gh_cli:
            ensure_gh()
            pr_url = create_pr_with_gh(
                pr_title,
                pr_body,
                config.github.default_base_branch,
                branch,
                config.github.draft_pr,
                config.github.reviewers,
                config.github.labels,
            )
        else:
            run_command(["git", "push", "-u", "origin", branch], cwd=repo_path)
            pr_url = create_pr_with_rest(
                config.github.owner,
                repo,
                _require_github_token(config),
                pr_title,
                pr_body,
                config.github.default_base_branch,
                branch,
                config.github.draft_pr,
            )

        if config.jira.comment_on_pr and not no_comment:
            add_comment(
                config.jira.base_url,
                config.jira.email,
                config.jira.api_token,
                jira_key,
                f"PR opened: {pr_url}",
            )

        upsert_ticket(TicketState(jira_key, "PR_OPENED", repo, branch, pr_url, run_id, None))
        finish_run(run_id, "PR_OPENED", pr_url, agent_result.exit_code)
        write_artifact_json(artifacts_dir, "pr.json", {"pr_url": pr_url, "ticket": jira_key})
        write_artifact_json(artifacts_dir, "summary.json", {"pr_url": pr_url, "ticket": jira_key})
        clear_lock(repo)
        console.print(pr_url)
    except Exception as exc:
        status = _classify_error(str(exc))
        finish_run(run_id, status, None, None)
        upsert_ticket(TicketState(jira_key, status, repo, None, None, run_id, str(exc)))
        clear_lock(repo)
        console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=2 if status == "NEEDS_HUMAN" else 3)


@app.command("run-next")
def run_next() -> None:
    config = _load_config_or_exit()
    issues = search_issues(
        config.jira.base_url,
        config.jira.email,
        config.jira.api_token,
        config.jira.jql,
        config.jira.fields,
        1,
    )
    if not issues:
        console.print("[yellow]No eligible tickets[/yellow]")
        raise typer.Exit(code=0)
    run(issues[0].key)


@app.command()
def status(ticket: Optional[str] = None) -> None:
    init_db()
    if not ticket:
        console.print("[yellow]Provide --ticket to view status[/yellow]")
        raise typer.Exit(code=0)
    state = get_ticket(ticket)
    if not state:
        console.print("[yellow]No state found[/yellow]")
        raise typer.Exit(code=0)
    console.print(json.dumps(state.__dict__, indent=2))


@app.command()
def open(ticket: str, latest: bool = True) -> None:
    init_db()
    state = get_ticket(ticket)
    if not state or not state.last_run_id:
        console.print("[yellow]No runs found[/yellow]")
        raise typer.Exit(code=0)
    path = artifacts_root(ticket, state.last_run_id)
    console.print(str(path))


@app.command()
def tail(ticket: str, latest: bool = True) -> None:
    init_db()
    state = get_ticket(ticket)
    if not state or not state.last_run_id:
        console.print("[yellow]No runs found[/yellow]")
        raise typer.Exit(code=0)
    path = artifacts_root(ticket, state.last_run_id) / "agent_transcript.log"
    if not path.exists():
        console.print("[yellow]No transcript found[/yellow]")
        raise typer.Exit(code=0)
    console.print(path.read_text())


@app.command("clean-locks")
def clean_locks() -> None:
    init_db()
    console.print("[green]Locks cleanup is manual in this MVP[/green]")


if __name__ == "__main__":
    app()
