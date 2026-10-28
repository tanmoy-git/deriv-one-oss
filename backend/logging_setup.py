"""One logging setup for the API and every worker.

LOG_FORMAT=json writes one JSON object per line (for log shippers);
anything else keeps the existing plain-text format. LOG_LEVEL sets the level.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Attributes every LogRecord has; anything else was passed through `extra=`.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, label: Optional[str] = None) -> None:
        super().__init__()
        self.label = label

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self.label:
            entry["worker"] = self.label
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        return json.dumps(entry, default=str)


def configure_logging(*, text_format: str = TEXT_FORMAT, label: Optional[str] = None) -> None:
    """Install one stdout handler on the root logger.

    In JSON mode uvicorn's loggers are routed through the root handler too,
    so access and error lines come out in the same format.
    """
    json_mode = os.environ.get("LOG_FORMAT", "").strip().lower() == "json"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(label) if json_mode else logging.Formatter(text_format))
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        handlers=[handler],
        force=True,
    )
    if json_mode:
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            uv = logging.getLogger(name)
            uv.handlers = []
            uv.propagate = True
