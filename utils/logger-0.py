"""
utils/logger.py
---------------
Structured JSON logging with console fallback.
Includes an audit trail logger for security events.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

try:
    import structlog
    _HAS_STRUCTLOG = True
except ImportError:
    _HAS_STRUCTLOG = False


def _configure_stdlib(level: str, log_file: Path | None, fmt: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10_485_760, backupCount=5
            )
        )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def setup_logging(level: str = "INFO", fmt: str = "console", log_file: Path | None = None) -> None:
    _configure_stdlib(level, log_file, fmt)

    if _HAS_STRUCTLOG:
        processors = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
        ]
        if fmt == "json":
            processors.append(structlog.processors.JSONRenderer())
        else:
            processors.append(structlog.dev.ConsoleRenderer(colors=True))

        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(
                getattr(logging, level.upper(), logging.INFO)
            ),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )


def get_logger(name: str) -> Any:
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name)


class AuditLogger:
    """
    Security audit trail. Writes structured records for:
    - Authentication events (login, logout, MFA)
    - Session events (open, close, timeout)
    - Permission denials
    - File transfers
    - Command executions
    """

    def __init__(self, audit_file: Path | None = None):
        self._log = get_logger("nexus.audit")
        if audit_file:
            audit_file.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                audit_file, maxBytes=50_485_760, backupCount=10
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logging.getLogger("nexus.audit").addHandler(handler)

    def _emit(self, event: str, **kwargs: Any) -> None:
        self._log.info(event, **kwargs)

    def login_success(self, user_id: str, ip: str, mfa: bool = False) -> None:
        self._emit("auth.login_success", user_id=user_id, ip=ip, mfa_used=mfa)

    def login_failure(self, user_id: str, ip: str, reason: str) -> None:
        self._emit("auth.login_failure", user_id=user_id, ip=ip, reason=reason)

    def session_opened(self, session_id: str, user_id: str, device_id: str) -> None:
        self._emit("session.opened", session_id=session_id, user_id=user_id, device_id=device_id)

    def session_closed(self, session_id: str, duration_s: float, reason: str) -> None:
        self._emit("session.closed", session_id=session_id, duration_s=duration_s, reason=reason)

    def permission_denied(self, user_id: str, resource: str, action: str) -> None:
        self._emit("authz.denied", user_id=user_id, resource=resource, action=action)

    def file_transfer(self, session_id: str, direction: str, filename: str, size_bytes: int) -> None:
        self._emit(
            "transfer.file",
            session_id=session_id,
            direction=direction,
            filename=filename,
            size_bytes=size_bytes,
        )

    def command_executed(self, session_id: str, command: str, exit_code: int | None = None) -> None:
        self._emit(
            "terminal.command",
            session_id=session_id,
            command=command[:500],     # truncate for safety
            exit_code=exit_code,
        )
