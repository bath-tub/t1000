from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class CommandResult:
    command: List[str]
    returncode: int
    stdout: str
    stderr: str


def run_command(
    command: List[str],
    cwd: Optional[Path] = None,
    timeout: Optional[int] = None,
) -> CommandResult:
    proc = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return CommandResult(command, proc.returncode, proc.stdout, proc.stderr)


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))
