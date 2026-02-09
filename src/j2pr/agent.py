from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .footer import AgentFooter, parse_footer


DEFAULT_PROMPT = """You are a Cursor headless coding agent.

Ticket: {ticket_key}
Title: {title}
Description:
{description}

Acceptance Criteria:
{acceptance}

Repo Path: {repo_path}
Base Branch: {base_branch}

Guardrails:
- deny globs: {deny_globs}
- max files changed: {max_files}
- max diff lines: {max_lines}
- test command: {test_command}
- format command: {format_command}

Do not touch:
{do_not_touch}

Instructions:
- Stay within repo.
- Minimal change bias.
- No dependency upgrades unless required for the ticket and small.
- Must add/update tests if change is logic.
- Must run the provided test command locally and report result in footer.
- Never open/merge PR yourself unless explicitly configured.
- If ambiguous requirements, choose safest interpretation and note it.

Required footer (single line):
J2PR_RESULT: {{...json...}}

Additional notes:
{notes_for_agent}
"""


@dataclass
class AgentResult:
    exit_code: int
    footer: Optional[AgentFooter]
    transcript: str


def run_agent(
    command: str,
    repo_path: Path,
    prompt_vars: Dict[str, str],
    timeout_minutes: int,
    transcript_path: Path,
    prompt_template_path: Optional[str] = None,
) -> AgentResult:
    if prompt_template_path:
        prompt_template = Path(prompt_template_path).read_text()
    else:
        prompt_template = DEFAULT_PROMPT
    prompt = prompt_template.format(**prompt_vars)

    # Use Popen + communicate so we can reliably kill the entire process
    # group on timeout.  subprocess.run's built-in timeout can hang when
    # grandchild processes hold stdout/stderr pipes open.
    proc = subprocess.Popen(
        [command, "--print", prompt],
        cwd=str(repo_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # new process group for clean kill
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_minutes * 60)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Kill entire process group so grandchild processes don't linger.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.kill()
        stdout, stderr = proc.communicate(timeout=30)

    transcript = (stdout or "") + "\n" + (stderr or "")
    transcript_path.write_text(transcript)

    if timed_out:
        return AgentResult(-1, None, transcript)

    footer = None
    for line in reversed(transcript.splitlines()):
        parsed = parse_footer(line.strip())
        if parsed:
            footer = parsed
            break
    return AgentResult(proc.returncode, footer, transcript)
