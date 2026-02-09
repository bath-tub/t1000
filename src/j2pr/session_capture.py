"""Session capture for j2pr runs.

When enabled, records the full context of every j2pr session so that
AI agents (or humans) can later read the captured output to diagnose
bugs, understand program behaviour, and capture context.

Captured artefacts per session:
  session_output.log   – raw tee of all stdout / stderr
  session_events.jsonl – structured timestamped events
  session_manifest.json – machine-readable summary written on close
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import SessionCaptureConfig


# ---------------------------------------------------------------------------
# Tee writers – mirror writes to both the original stream and a capture file
# ---------------------------------------------------------------------------

class _TeeWriter(io.TextIOBase):
    """Duplicates writes to an original stream and a log file handle."""

    def __init__(self, original: io.TextIOBase, capture_fh: io.TextIOBase) -> None:
        self._original = original
        self._capture_fh = capture_fh

    # --- delegated properties so Rich / Typer still think this is a tty ---

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._original, "encoding", "utf-8")

    def isatty(self) -> bool:  # type: ignore[override]
        return getattr(self._original, "isatty", lambda: False)()

    def fileno(self) -> int:
        return self._original.fileno()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        self._original.write(s)
        try:
            self._capture_fh.write(s)
            self._capture_fh.flush()
        except Exception:
            pass  # never let capture failures interrupt the real run
        return len(s)

    def flush(self) -> None:
        self._original.flush()
        try:
            self._capture_fh.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------

def _build_redaction_re(patterns: List[str]) -> re.Pattern[str]:
    """Build a single regex that matches any dict key containing a pattern."""
    escaped = [re.escape(p) for p in patterns]
    return re.compile("|".join(escaped), re.IGNORECASE) if escaped else re.compile(r"(?!)")


def _redact_dict(data: Dict[str, Any], redaction_re: re.Pattern[str]) -> Dict[str, Any]:
    """Recursively redact values whose keys match the redaction regex."""
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if redaction_re.search(key):
            out[key] = "***REDACTED***"
        elif isinstance(value, dict):
            out[key] = _redact_dict(value, redaction_re)
        elif isinstance(value, list):
            out[key] = [
                _redact_dict(v, redaction_re) if isinstance(v, dict) else v
                for v in value
            ]
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# SessionCapture
# ---------------------------------------------------------------------------

class SessionCapture:
    """Context-manager that captures a full j2pr session.

    Usage::

        with SessionCapture(config.session_capture, ticket="ABC-1", run_id="abc123") as cap:
            cap.event("step_started", {"step": "branch_creation"})
            ...
            cap.event("step_finished", {"step": "branch_creation", "branch": "j2pr/ABC-1-fix"})
    """

    def __init__(
        self,
        cfg: SessionCaptureConfig,
        *,
        ticket: str,
        run_id: str,
    ) -> None:
        self._cfg = cfg
        self._ticket = ticket
        self._run_id = run_id
        self._enabled = cfg.enabled
        self._events: List[Dict[str, Any]] = []
        self._redaction_re = _build_redaction_re(cfg.redact_patterns)
        self._start_ts: Optional[float] = None
        self._session_dir: Optional[Path] = None
        self._output_fh: Optional[io.TextIOBase] = None
        self._events_fh: Optional[io.TextIOBase] = None
        self._orig_stdout: Optional[io.TextIOBase] = None
        self._orig_stderr: Optional[io.TextIOBase] = None

    # -- public helpers --

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def session_dir(self) -> Optional[Path]:
        return self._session_dir

    def event(self, name: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Record a structured event with a monotonic + wall-clock timestamp."""
        if not self._enabled:
            return
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": round(time.monotonic() - self._start_ts, 3) if self._start_ts else 0,
            "event": name,
            "ticket": self._ticket,
            "run_id": self._run_id,
        }
        if data:
            entry["data"] = _redact_dict(data, self._redaction_re) if isinstance(data, dict) else data
        self._events.append(entry)
        if self._events_fh:
            try:
                self._events_fh.write(json.dumps(entry, default=str) + "\n")
                self._events_fh.flush()
            except Exception:
                pass

    # -- context manager --

    def __enter__(self) -> "SessionCapture":
        if not self._enabled:
            return self
        self._start_ts = time.monotonic()
        output_root = Path(self._cfg.output_dir).expanduser()
        self._session_dir = output_root / self._ticket / self._run_id
        self._session_dir.mkdir(parents=True, exist_ok=True)

        self._output_fh = open(self._session_dir / "session_output.log", "w")  # noqa: SIM115
        self._events_fh = open(self._session_dir / "session_events.jsonl", "w")  # noqa: SIM115

        # tee stdout / stderr
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = _TeeWriter(self._orig_stdout, self._output_fh)  # type: ignore[assignment]
        sys.stderr = _TeeWriter(self._orig_stderr, self._output_fh)  # type: ignore[assignment]

        # opening event
        self.event("session_started", self._env_snapshot())
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if not self._enabled:
            return

        # Record error if we're exiting due to an exception
        if exc_type is not None:
            self.event("session_error", {
                "error_type": exc_type.__name__,
                "error_message": str(exc_val),
            })

        self.event("session_finished", {
            "elapsed_s": round(time.monotonic() - self._start_ts, 3) if self._start_ts else 0,
            "exit_reason": "error" if exc_type else "normal",
        })

        self._write_manifest()

        # restore streams
        if self._orig_stdout:
            sys.stdout = self._orig_stdout
        if self._orig_stderr:
            sys.stderr = self._orig_stderr

        # close file handles
        for fh in (self._output_fh, self._events_fh):
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass

        # retention cleanup
        if self._cfg.retention_days > 0:
            _prune_old_sessions(
                Path(self._cfg.output_dir).expanduser(),
                self._cfg.retention_days,
            )

    # -- internal --

    def _env_snapshot(self) -> Dict[str, Any]:
        """Capture environment metadata at session start."""
        snapshot: Dict[str, Any] = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cwd": os.getcwd(),
            "pid": os.getpid(),
        }
        if self._cfg.include_env:
            safe_env = {
                k: v
                for k, v in os.environ.items()
                if not self._redaction_re.search(k)
            }
            # Only include j2pr-relevant env vars to keep size reasonable
            relevant_prefixes = ("J2PR_", "GITHUB_", "JIRA_", "CURSOR_", "PATH", "HOME", "USER", "SHELL")
            snapshot["env"] = {
                k: v for k, v in safe_env.items()
                if any(k.startswith(p) for p in relevant_prefixes)
            }
        return snapshot

    def snapshot_config(self, raw_config: Dict[str, Any]) -> None:
        """Record a redacted snapshot of the loaded config."""
        if not self._enabled or not self._cfg.include_config:
            return
        self.event("config_snapshot", _redact_dict(raw_config, self._redaction_re))

    def _write_manifest(self) -> None:
        """Write a machine-readable session summary for AI agents to parse."""
        if not self._session_dir:
            return
        elapsed = round(time.monotonic() - self._start_ts, 3) if self._start_ts else 0
        event_names = [e["event"] for e in self._events]

        # Collect all errors from events
        errors = [
            e["data"]
            for e in self._events
            if e["event"] == "session_error" and "data" in e
        ]

        manifest = {
            "version": 1,
            "ticket": self._ticket,
            "run_id": self._run_id,
            "started_at": self._events[0]["ts"] if self._events else None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": elapsed,
            "event_count": len(self._events),
            "event_names": event_names,
            "errors": errors,
            "files": [
                "session_output.log",
                "session_events.jsonl",
                "session_manifest.json",
            ],
            "purpose": (
                "This session capture is intended for AI agents and humans to "
                "diagnose bugs, understand program behaviour, and capture context "
                "from j2pr runs."
            ),
        }
        try:
            (self._session_dir / "session_manifest.json").write_text(
                json.dumps(manifest, indent=2, default=str) + "\n"
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Retention helpers
# ---------------------------------------------------------------------------

def _prune_old_sessions(root: Path, retention_days: int) -> None:
    """Remove session directories older than *retention_days*."""
    if not root.exists():
        return
    cutoff = time.time() - (retention_days * 86400)
    for ticket_dir in root.iterdir():
        if not ticket_dir.is_dir():
            continue
        for session_dir in ticket_dir.iterdir():
            if not session_dir.is_dir():
                continue
            manifest = session_dir / "session_manifest.json"
            if manifest.exists() and manifest.stat().st_mtime < cutoff:
                try:
                    import shutil
                    shutil.rmtree(session_dir)
                except Exception:
                    pass
        # Remove empty ticket dirs
        if ticket_dir.exists() and not any(ticket_dir.iterdir()):
            try:
                ticket_dir.rmdir()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Listing / reading helpers (used by CLI commands)
# ---------------------------------------------------------------------------

def list_sessions(output_dir: str) -> List[Dict[str, Any]]:
    """Return a list of session manifests, newest first."""
    root = Path(output_dir).expanduser()
    sessions: List[Dict[str, Any]] = []
    if not root.exists():
        return sessions
    for ticket_dir in sorted(root.iterdir()):
        if not ticket_dir.is_dir():
            continue
        for session_dir in sorted(ticket_dir.iterdir(), reverse=True):
            manifest_path = session_dir / "session_manifest.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                    manifest["session_path"] = str(session_dir)
                    sessions.append(manifest)
                except Exception:
                    pass
    sessions.sort(key=lambda s: s.get("finished_at", ""), reverse=True)
    return sessions


def read_session_events(session_dir: Path) -> List[Dict[str, Any]]:
    """Read all structured events from a session."""
    events_path = session_dir / "session_events.jsonl"
    if not events_path.exists():
        return []
    events = []
    for line in events_path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def read_session_output(session_dir: Path) -> str:
    """Read the raw console output from a session."""
    output_path = session_dir / "session_output.log"
    if not output_path.exists():
        return ""
    return output_path.read_text()


@contextmanager
def session_or_noop(
    cfg: Optional[SessionCaptureConfig],
    *,
    ticket: str,
    run_id: str,
):
    """Yield a SessionCapture if config is enabled, otherwise a no-op stub."""
    if cfg and cfg.enabled:
        cap = SessionCapture(cfg, ticket=ticket, run_id=run_id)
        with cap:
            yield cap
    else:
        yield _NoOpCapture()


class _NoOpCapture:
    """Stub used when session capture is disabled."""

    enabled = False
    session_dir = None

    def event(self, name: str, data: Optional[Dict[str, Any]] = None) -> None:
        pass

    def snapshot_config(self, raw_config: Dict[str, Any]) -> None:
        pass
