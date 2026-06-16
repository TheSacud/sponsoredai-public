from __future__ import annotations

import logging
import os
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import runtime_paths


LOGGER_NAME = "sai"
DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_BACKUPS = 3
DISABLED_LOG_DESTINATIONS = {"0", "false", "no", "off", "none", "null", "disabled"}
STDERR_LOG_DESTINATIONS = {"-", "stderr"}


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


class ServiceFilter(logging.Filter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service or "app"

    def filter(self, record: logging.LogRecord) -> bool:
        record.sai_service = self.service
        return True


class ParentCreatingRotatingFileHandler(RotatingFileHandler):
    def _open(self):  # type: ignore[override]
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()


def default_log_path() -> Path:
    return runtime_paths().home / "logs" / "sai.log"


def configure_logging(
    service: str | None = None,
    *,
    level: str | int | None = None,
    log_file: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Configure SAI application logging.

    Logs default to a rotating file under SAI_HOME so detached gateway/backend
    processes still leave an error trail. Request bodies are intentionally not
    logged by the HTTP handlers because they may contain prompts or API keys.
    """
    logger = logging.getLogger(LOGGER_NAME)
    level_no = _resolve_level(level)
    destination = _resolve_destination(log_file)
    signature = (level_no, str(destination), service or "")
    if getattr(logger, "_sai_logging_signature", None) == signature:
        return destination if isinstance(destination, Path) else None

    _remove_managed_handlers(logger)
    logger.setLevel(level_no)
    logger.propagate = False
    logger._sai_logging_signature = signature  # type: ignore[attr-defined]

    if destination is None:
        logger.addHandler(_managed_handler(logging.NullHandler()))
        return None

    formatter = UTCFormatter("%(asctime)sZ %(levelname)s %(name)s service=%(sai_service)s %(message)s")
    if destination == "stderr":
        handler: logging.Handler = logging.StreamHandler(sys.stderr)
    else:
        handler = ParentCreatingRotatingFileHandler(
            destination,
            maxBytes=_resolve_positive_int("SAI_LOG_MAX_BYTES", DEFAULT_MAX_BYTES),
            backupCount=_resolve_positive_int("SAI_LOG_BACKUPS", DEFAULT_BACKUPS),
            encoding="utf-8",
            delay=True,
        )
    handler.setFormatter(formatter)
    handler.setLevel(level_no)
    handler.addFilter(ServiceFilter(service or "app"))
    logger.addHandler(_managed_handler(handler))
    return destination if isinstance(destination, Path) else None


def log_destination_label(log_file: str | os.PathLike[str] | None = None) -> str:
    destination = _resolve_destination(log_file)
    if destination is None:
        return "disabled"
    if destination == "stderr":
        return "stderr"
    return str(destination)


def current_log_path(log_file: str | os.PathLike[str] | None = None) -> Path | None:
    destination = _resolve_destination(log_file)
    return destination if isinstance(destination, Path) else None


def tail_log_lines(lines: int = 80, log_file: str | os.PathLike[str] | None = None) -> list[str]:
    path = current_log_path(log_file)
    if path is None:
        raise ValueError("File logging is disabled for this process")
    if lines <= 0:
        return []
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return [line.rstrip("\n") for line in deque(fh, maxlen=lines)]


def reset_logging_for_tests() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    _remove_managed_handlers(logger)
    if hasattr(logger, "_sai_logging_signature"):
        delattr(logger, "_sai_logging_signature")
    logger.propagate = True


def _remove_managed_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_sai_managed", False):
            logger.removeHandler(handler)
            handler.close()


def _managed_handler(handler: logging.Handler) -> logging.Handler:
    handler._sai_managed = True  # type: ignore[attr-defined]
    return handler


def _resolve_destination(log_file: str | os.PathLike[str] | None) -> Path | str | None:
    raw = str(log_file) if log_file is not None else os.environ.get("SAI_LOG_FILE", "")
    value = raw.strip()
    if not value:
        return default_log_path()
    lowered = value.lower()
    if lowered in DISABLED_LOG_DESTINATIONS:
        return None
    if lowered in STDERR_LOG_DESTINATIONS:
        return "stderr"
    return Path(value).expanduser()


def _resolve_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    value = (level or os.environ.get("SAI_LOG_LEVEL") or "INFO").strip().upper()
    if value.isdigit():
        return int(value)
    resolved = getattr(logging, value, None)
    if isinstance(resolved, int):
        return int(resolved)
    raise ValueError(f"Invalid SAI_LOG_LEVEL: {value}")


def _resolve_positive_int(env_key: str, default: int) -> int:
    try:
        value = int(os.environ.get(env_key, ""))
    except ValueError:
        return default
    return value if value > 0 else default
