from __future__ import annotations

from pathlib import Path
from typing import Dict

from .util import write_json


def artifacts_root(ticket_key: str, run_id: str) -> Path:
    return Path("~/.j2pr/runs").expanduser() / ticket_key / run_id


def write_artifacts(base: Path, files: Dict[str, str]) -> None:
    base.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (base / name).write_text(content)


def write_artifact_json(base: Path, name: str, payload: dict) -> None:
    base.mkdir(parents=True, exist_ok=True)
    write_json(base / name, payload)
