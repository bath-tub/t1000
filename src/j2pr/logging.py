from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

LOG_PATH = Path("~/.j2pr/j2pr.log").expanduser()


def setup_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("j2pr")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(LOG_PATH)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def redact_secrets(payload: Dict[str, Any]) -> Dict[str, Any]:
    redacted = {}
    for key, value in payload.items():
        if "token" in key.lower() or "password" in key.lower():
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def log_event(logger: logging.Logger, event: str, data: Dict[str, Any]) -> None:
    logger.info("%s %s", event, json.dumps(redact_secrets(data), default=str))
