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
from .mapping import infer_repo_from_issue, map_repo
from .repo import (
    create_branch,
    detect_default_branch,
    detect_test_command,
    diff_patch,
    ensure_clean_worktree,
    fetch_and_checkout_base,
    remote_branch_exists,
)
from .session_capture import (
    list_sessions,
    read_session_events,
    read_session_output,
    session_or_noop,
)
from .state import (
    RunState,
    TicketState,
    add_run,
    clear_all_locks,
    clear_lock,
    dump_table,
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
    try:
        issues = search_issues(
            config.jira.base_url,
            config.jira.email,
            config.jira.api_token,
            config.jira.api_version,
            config.jira.jql,
            config.jira.fields,
            limit,
        )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=3)
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

    jira_key = jira_key.upper()

    ticket = get_ticket(jira_key)
    if ticket and ticket.status in {"PR_OPENED", "DONE"} and ticket.pr_url and not rerun:
        console.print(ticket.pr_url)
        raise typer.Exit(code=0)

    try:
        issues = search_issues(
            config.jira.base_url,
            config.jira.email,
            config.jira.api_token,
            config.jira.api_version,
            f"key = {jira_key}",
            config.jira.fields,
            1,
        )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=3)
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

    allow_all_repos = len(config.workspace.repo_allowlist) == 0
    repo = map_repo(issue.fields, config.workspace.repo_mapping)
    if repo is None:
        repo = infer_repo_from_issue(
            issue.fields,
            config.workspace.root_dir,
            config.workspace.repo_allowlist,
            config.workspace.repo_inference,
        )
    if repo is None and config.workspace.single_repo_only and len(config.workspace.repo_allowlist) == 1:
        repo = config.workspace.repo_allowlist[0]
    if not repo or (not allow_all_repos and repo not in config.workspace.repo_allowlist):
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

    # Resolve test command — auto-detect from repo contents when set to "auto".
    test_command = config.guardrails.test_command
    if test_command.lower() == "auto":
        detected = detect_test_command(repo_path)
        if detected:
            test_command = detected
        elif config.guardrails.require_tests:
            clear_lock(repo)
            console.print("[red]Could not auto-detect test command and require_tests is enabled[/red]")
            raise typer.Exit(code=2)
        else:
            test_command = ""

    run_state = RunState(run_id, jira_key, "RUNNING", repo, None, None, str(artifacts_root(jira_key, run_id)), None)
    add_run(run_state)
    upsert_ticket(TicketState(jira_key, "RUNNING", repo, None, None, run_id, None))
    log_event(logger, "run_started", {"ticket": jira_key, "repo": repo, "run_id": run_id})

    artifacts_dir = artifacts_root(jira_key, run_id)
    write_artifact_json(artifacts_dir, "ticket.json", issue.fields)

    title = str(issue.fields.get("summary", ""))
    description = _extract_description(issue.fields)
    acceptance = _acceptance_from_description(description)
    branch = f"j2pr/{jira_key}-{_slug(title)[:50]}".rstrip("-")

    commands = []

    with session_or_noop(config.session_capture, ticket=jira_key, run_id=run_id) as cap:
        cap.snapshot_config(config.model_dump())
        cap.event("run_initiated", {
            "ticket": jira_key,
            "repo": repo,
            "run_id": run_id,
            "title": title,
            "rerun": rerun,
            "force": force,
            "test_command": test_command,
            "test_command_source": "auto-detected" if config.guardrails.test_command.lower() == "auto" else "config",
        })

        try:
            if config.guardrails.require_clean_worktree:
                cap.event("worktree_check_started")
                clean, status = ensure_clean_worktree(repo_path)
                write_artifacts(artifacts_dir, {"pre_git_status.txt": status})
                cap.event("worktree_check_finished", {"clean": clean, "status": status})
                if not clean and not force:
                    raise RuntimeError("Worktree not clean")

            base_branch = config.github.default_base_branch
            if base_branch.lower() == "auto":
                detected = detect_default_branch(repo_path)
                if detected:
                    base_branch = detected
                    log_event(logger, "auto_detected_branch", {"repo": repo, "branch": base_branch})
                else:
                    base_branch = "main"
                    log_event(logger, "auto_detect_fallback", {"repo": repo, "branch": base_branch})

            cap.event("branch_setup_started", {"base_branch": base_branch, "branch": branch})
            fetch_and_checkout_base(repo_path, base_branch)
            create_branch(repo_path, branch)
            commands.extend(
                [
                    f"git fetch --all",
                    f"git checkout --force {base_branch}",
                    f"git reset --hard origin/{base_branch}",
                    f"git clean -fd",
                    f"git checkout -B {branch}",
                ]
            )
            cap.event("branch_setup_finished", {"branch": branch, "base_branch": base_branch})
            if not _denylist_ok(commands, config.guardrails.command_denylist):
                raise RuntimeError("Command denylist violation")

            prompt_vars = {
                "ticket_key": jira_key,
                "title": title,
                "description": description,
                "acceptance": acceptance,
                "repo_path": str(repo_path),
                "base_branch": base_branch,
                "deny_globs": ", ".join(config.guardrails.deny_globs),
                "max_files": str(config.guardrails.max_files_changed),
                "max_lines": str(config.guardrails.max_diff_lines),
                "test_command": test_command,
                "format_command": config.guardrails.format_command,
                "do_not_touch": ", ".join(config.guardrails.deny_globs),
                "notes_for_agent": "",
            }

            agent_result = None
            fix_attempts = 0
            while True:
                cap.event("agent_invocation_started", {"attempt": fix_attempts + 1})
                agent_result = run_agent(
                    config.cursor.command,
                    repo_path,
                    prompt_vars,
                    config.cursor.timeout_minutes,
                    artifacts_dir / "agent_transcript.log",
                    config.cursor.prompt_template_path or None,
                )
                cap.event("agent_invocation_finished", {
                    "exit_code": agent_result.exit_code,
                    "has_footer": agent_result.footer is not None,
                    "transcript_length": len(agent_result.transcript),
                })
                if not agent_result.footer:
                    raise RuntimeError("Agent contract missing footer")

                if config.guardrails.format_command:
                    cap.event("format_started", {"command": config.guardrails.format_command})
                    fmt = run_command(shlex.split(config.guardrails.format_command), cwd=repo_path)
                    commands.append(config.guardrails.format_command)
                    write_artifacts(artifacts_dir, {"format_output.log": fmt.stdout + fmt.stderr})
                    cap.event("format_finished", {"returncode": fmt.returncode})
                    if not _denylist_ok(commands, config.guardrails.command_denylist):
                        raise RuntimeError("Command denylist violation")

                if config.guardrails.require_tests:
                    cap.event("tests_started", {"command": test_command})
                    test = run_command(shlex.split(test_command), cwd=repo_path)
                    commands.append(test_command)
                    write_artifacts(artifacts_dir, {"test_output.log": test.stdout + test.stderr})
                    cap.event("tests_finished", {
                        "returncode": test.returncode,
                        "passed": test.returncode == 0,
                    })
                    if not _denylist_ok(commands, config.guardrails.command_denylist):
                        raise RuntimeError("Command denylist violation")
                    if test.returncode != 0:
                        fix_attempts += 1
                        cap.event("test_fix_cycle", {
                            "attempt": fix_attempts,
                            "max_attempts": config.guardrails.max_fix_attempts,
                        })
                        if fix_attempts > config.guardrails.max_fix_attempts:
                            raise RuntimeError("Tests failed")
                        prompt_vars["notes_for_agent"] = "Tests failed; please fix and re-run tests."
                        continue
                break

            cap.event("guardrails_check_started")
            ok, blocked = enforce_deny_globs(repo_path, config.guardrails.deny_globs)
            if not ok:
                cap.event("guardrails_deny_glob_violation", {"blocked_files": blocked})
                raise RuntimeError(f"Deny glob violation: {', '.join(blocked)}")
            ok, files_changed, lines_changed = enforce_diff_limits(
                repo_path, config.guardrails.max_files_changed, config.guardrails.max_diff_lines
            )
            cap.event("guardrails_check_finished", {
                "deny_globs_ok": True,
                "diff_ok": ok,
                "files_changed": files_changed,
                "lines_changed": lines_changed,
            })
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

            cap.event("pr_lookup_started")
            if remote_branch_exists(repo_path, branch):
                if config.github.use_gh_cli:
                    pr_url = find_pr_with_gh(branch, cwd=repo_path)
                else:
                    pr_url = find_pr_with_rest(
                        config.github.owner,
                        repo,
                        branch,
                        _require_github_token(config),
                    )
                if pr_url:
                    cap.event("existing_pr_found", {"pr_url": pr_url, "source": "branch"})
                    upsert_ticket(TicketState(jira_key, "PR_OPENED", repo, branch, pr_url, run_id, None))
                    finish_run(run_id, "PR_OPENED", pr_url, agent_result.exit_code)
                    clear_lock(repo)
                    console.print(pr_url)
                    raise typer.Exit(code=0)

            if config.github.use_gh_cli:
                pr_url = find_pr_by_jira_with_gh(jira_key, cwd=repo_path)
            else:
                pr_url = find_pr_by_jira_with_rest(
                    config.github.owner,
                    repo,
                    jira_key,
                    _require_github_token(config),
                )
            if pr_url:
                cap.event("existing_pr_found", {"pr_url": pr_url, "source": "jira_key"})
                upsert_ticket(TicketState(jira_key, "PR_OPENED", repo, branch, pr_url, run_id, None))
                finish_run(run_id, "PR_OPENED", pr_url, agent_result.exit_code)
                clear_lock(repo)
                console.print(pr_url)
                raise typer.Exit(code=0)

            cap.event("pr_creation_started", {"draft": config.github.draft_pr})
            pr_title = f"[{jira_key}] {title}"
            pr_body = _pr_body(agent_result.footer, test_command)

            run_command(["git", "push", "-u", "origin", branch], cwd=repo_path)

            if config.github.use_gh_cli:
                ensure_gh()
                pr_url = create_pr_with_gh(
                    pr_title,
                    pr_body,
                    base_branch,
                    branch,
                    config.github.draft_pr,
                    config.github.reviewers,
                    config.github.labels,
                    cwd=repo_path,
                )
            else:
                pr_url = create_pr_with_rest(
                    config.github.owner,
                    repo,
                    _require_github_token(config),
                    pr_title,
                    pr_body,
                    base_branch,
                    branch,
                    config.github.draft_pr,
                )
            cap.event("pr_creation_finished", {"pr_url": pr_url})

            if config.jira.comment_on_pr and not no_comment:
                try:
                    add_comment(
                        config.jira.base_url,
                        config.jira.email,
                        config.jira.api_token,
                        config.jira.api_version,
                        jira_key,
                        f"PR opened: {pr_url}",
                    )
                    cap.event("jira_comment_posted", {"pr_url": pr_url})
                except Exception as comment_exc:
                    cap.event("jira_comment_failed", {"error": str(comment_exc)})
                    console.print(f"[yellow]Warning: Jira comment failed (non-fatal): {comment_exc}[/yellow]")

            upsert_ticket(TicketState(jira_key, "PR_OPENED", repo, branch, pr_url, run_id, None))
            finish_run(run_id, "PR_OPENED", pr_url, agent_result.exit_code)
            write_artifact_json(artifacts_dir, "pr.json", {"pr_url": pr_url, "ticket": jira_key})
            write_artifact_json(artifacts_dir, "summary.json", {"pr_url": pr_url, "ticket": jira_key})
            clear_lock(repo)
            cap.event("run_succeeded", {"pr_url": pr_url})
            console.print(pr_url)
        except typer.Exit:
            # typer.Exit is used for normal control flow (e.g. existing PR
            # found) — let it propagate without marking the run as failed.
            raise
        except Exception as exc:
            cap.event("run_failed", {"error": str(exc), "error_type": type(exc).__name__})
            status = _classify_error(str(exc))
            finish_run(run_id, status, None, None)
            upsert_ticket(TicketState(jira_key, status, repo, None, None, run_id, str(exc)))
            clear_lock(repo)
            console.print(f"[red]Failed:[/red] {exc}")
            raise typer.Exit(code=2 if status == "NEEDS_HUMAN" else 3)


@app.command("run-next")
def run_next() -> None:
    config = _load_config_or_exit()
    try:
        issues = search_issues(
            config.jira.base_url,
            config.jira.email,
            config.jira.api_token,
            config.jira.api_version,
            config.jira.jql,
            config.jira.fields,
            1,
        )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=3)
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


@app.command()
def sessions(
    ticket: Optional[str] = None,
    limit: int = typer.Option(20, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List captured sessions, optionally filtered by ticket."""
    config = _load_config_or_exit()
    all_sessions = list_sessions(config.session_capture.output_dir)
    if ticket:
        all_sessions = [s for s in all_sessions if s.get("ticket") == ticket]
    all_sessions = all_sessions[:limit]
    if not all_sessions:
        console.print("[yellow]No captured sessions found[/yellow]")
        if not config.session_capture.enabled:
            console.print("[dim]Session capture is disabled. Enable it in config: session_capture.enabled: true[/dim]")
        raise typer.Exit(code=0)
    if json_output:
        console.print(json.dumps(all_sessions, indent=2))
    else:
        table = Table(title="Captured Sessions")
        table.add_column("Ticket", style="bold")
        table.add_column("Run ID", max_width=12)
        table.add_column("Finished")
        table.add_column("Elapsed")
        table.add_column("Events")
        table.add_column("Errors")
        for s in all_sessions:
            errors_count = len(s.get("errors", []))
            error_style = "red" if errors_count > 0 else "green"
            table.add_row(
                s.get("ticket", "?"),
                s.get("run_id", "?")[:12],
                s.get("finished_at", "?")[:19],
                f"{s.get('elapsed_s', 0):.1f}s",
                str(s.get("event_count", 0)),
                f"[{error_style}]{errors_count}[/{error_style}]",
            )
        console.print(table)


@app.command()
def session(
    ticket: str,
    run_id: Optional[str] = typer.Argument(None),
    events: bool = typer.Option(False, "--events", "-e", help="Show structured events"),
    output: bool = typer.Option(False, "--output", "-o", help="Show raw console output"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """View a captured session. Shows manifest by default; use --events or --output for details."""
    config = _load_config_or_exit()
    all_sessions = list_sessions(config.session_capture.output_dir)
    matching = [s for s in all_sessions if s.get("ticket") == ticket]
    if run_id:
        matching = [s for s in matching if s.get("run_id") == run_id or s.get("run_id", "").startswith(run_id)]
    if not matching:
        console.print("[yellow]No matching session found[/yellow]")
        raise typer.Exit(code=0)
    session_info = matching[0]
    session_path = Path(session_info["session_path"])

    if events:
        evt_list = read_session_events(session_path)
        if json_output:
            console.print(json.dumps(evt_list, indent=2))
        else:
            for evt in evt_list:
                elapsed = evt.get("elapsed_s", 0)
                name = evt.get("event", "?")
                data = evt.get("data", {})
                data_str = json.dumps(data, default=str) if data else ""
                console.print(f"[dim]{elapsed:>8.3f}s[/dim]  [bold]{name}[/bold]  {data_str}")
    elif output:
        raw = read_session_output(session_path)
        if raw:
            console.print(raw)
        else:
            console.print("[yellow]No output captured[/yellow]")
    else:
        if json_output:
            console.print(json.dumps(session_info, indent=2))
        else:
            console.print(f"[bold]Session:[/bold] {session_info.get('ticket')} / {session_info.get('run_id')}")
            console.print(f"[bold]Started:[/bold]  {session_info.get('started_at', '?')}")
            console.print(f"[bold]Finished:[/bold] {session_info.get('finished_at', '?')}")
            console.print(f"[bold]Elapsed:[/bold]  {session_info.get('elapsed_s', 0):.1f}s")
            console.print(f"[bold]Events:[/bold]   {session_info.get('event_count', 0)}")
            errors = session_info.get("errors", [])
            if errors:
                console.print(f"[bold red]Errors:[/bold red]   {len(errors)}")
                for err in errors:
                    console.print(f"  [red]- {err.get('error_type', '?')}: {err.get('error_message', '?')}[/red]")
            else:
                console.print("[bold green]Errors:[/bold green]   0")
            console.print(f"[bold]Path:[/bold]     {session_info.get('session_path')}")
            console.print()
            console.print("[dim]Use --events to see structured events, --output to see raw console output[/dim]")


@app.command("clean-locks")
def clean_locks() -> None:
    """Clear all stale repo locks."""
    init_db()
    removed = clear_all_locks()
    if removed:
        console.print(f"[green]Cleared {removed} lock(s)[/green]")
    else:
        console.print("[dim]No locks to clear[/dim]")


DB_TABLES = ("tickets", "runs", "locks")

# Column subsets per table for the Rich table display (keeps output readable).
_TABLE_COLUMNS: dict[str, list[str]] = {
    "tickets": ["ticket_key", "status", "repo", "branch", "pr_url", "last_run_id", "updated_at", "last_error"],
    "runs": ["run_id", "ticket_key", "status", "repo", "started_at", "finished_at", "pr_url", "cursor_exit_code"],
    "locks": ["repo", "run_id", "locked_at"],
}


@app.command("db")
def db_cmd(
    table: Optional[str] = typer.Argument(None, help="Table to show: tickets, runs, or locks. Omit for all."),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON instead of tables."),
) -> None:
    """Show the current j2pr database state."""
    init_db()
    tables = [table] if table else list(DB_TABLES)
    for tbl in tables:
        if tbl not in DB_TABLES:
            console.print(f"[red]Unknown table:[/red] {tbl}  (choose from {', '.join(DB_TABLES)})")
            raise typer.Exit(code=1)

    all_data: dict[str, list[dict]] = {}
    for tbl in tables:
        all_data[tbl] = dump_table(tbl)

    if as_json:
        console.print_json(json.dumps(all_data, default=str))
        raise typer.Exit(code=0)

    for tbl in tables:
        rows = all_data[tbl]
        cols = _TABLE_COLUMNS.get(tbl, list(rows[0].keys()) if rows else ["(empty)"])
        rt = Table(title=f"{tbl} ({len(rows)})", show_lines=False, pad_edge=True)
        for col in cols:
            rt.add_column(col, overflow="fold")
        for row in rows:
            rt.add_row(*(str(row.get(c, "")) for c in cols))
        console.print(rt)
        console.print()

    if not any(all_data.values()):
        console.print("[dim]All tables are empty.[/dim]")


@app.command("help")
def help_cmd() -> None:
    """Show a summary of j2pr and all available commands."""
    console.print()
    console.print("[bold cyan]j2pr[/bold cyan] — Turn eligible Jira issues into Draft Pull Requests using a Cursor headless agent.")
    console.print()

    table = Table(title="Available Commands", show_lines=False, pad_edge=True, expand=False)
    table.add_column("Command", style="bold green", min_width=20)
    table.add_column("Description")

    table.add_row("config-validate", "Validate the j2pr config file and report any errors.")
    table.add_row("scan", "Search Jira for eligible tickets and list them. Use --json for machine output.")
    table.add_row("run <JIRA_KEY>", "Run the full pipeline for a single ticket: branch, agent, guardrails, PR.")
    table.add_row("run-next", "Pick the next eligible ticket from Jira and run the pipeline automatically.")
    table.add_row("status --ticket <KEY>", "Show the current state of a ticket (status, PR URL, run ID, errors).")
    table.add_row("open <TICKET>", "Print the artifacts directory path for the latest run of a ticket.")
    table.add_row("tail <TICKET>", "Print the agent transcript log for the latest run of a ticket.")
    table.add_row("sessions [--ticket KEY]", "List captured sessions. Use --json for machine output.")
    table.add_row("session <TICKET> [RUN_ID]", "View a captured session manifest, --events or --output for details.")
    table.add_row("db [TABLE]", "Show database state (tickets, runs, locks). Use --json for machine output.")
    table.add_row("clean-locks", "Clear all stale repo locks.")
    table.add_row("help", "Show this summary.")

    console.print(table)
    console.print()
    console.print("[dim]Workflow:[/dim]  config-validate → scan → run <KEY>  (or run-next) → status --ticket <KEY>")
    console.print("[dim]Config:[/dim]    Set [bold]J2PR_CONFIG[/bold] env var or place config at [bold]~/.j2pr/config.yaml[/bold]")
    console.print("[dim]Artifacts:[/dim] Run logs and diffs are saved under [bold]~/.j2pr/runs/<ticket>/<run_id>/[/bold]")
    console.print()


if __name__ == "__main__":
    app()
