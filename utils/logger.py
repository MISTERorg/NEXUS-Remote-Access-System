"""
utils/logger.py
---------------
Structured JSON/console logging with a stdlib fallback.
Includes an audit trail logger for security events.

Design notes (read before changing this file):

  * structlog is configured to render THROUGH stdlib `logging`
    (structlog.stdlib.LoggerFactory + ProcessorFormatter), not through
    structlog's standalone PrintLogger. This matters for two reasons:

      1. `structlog.stdlib.add_logger_name` reads `.name` off the
         underlying logger object. PrintLogger doesn't have one and
         raises AttributeError the moment anything logs. Only a real
         `logging.Logger` (which LoggerFactory gives us) has `.name`.

      2. PrintLogger writes straight to stdout and bypasses the
         RotatingFileHandlers set up below entirely. Routing through
         stdlib logging means `logs/nexus.log` and the audit file
         actually receive what's logged, instead of only the console.

  * `AuditLogger` may be constructed before `setup_logging()` runs
    (both core/auth.py and core/session.py create module-level
    AuditLogger() singletons at import time, which happens before
    ui/dashboard.py gets around to calling setup_logging()). Its file
    handler is therefore self-contained: it builds its own JSON
    formatter rather than borrowing whatever setup_logging() installed
    on the root logger, so it works correctly no matter which runs
    first.
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


# ---------------------------------------------------------------------------
# Shared processor chain applied to structlog-originated log records before
# they're handed to stdlib logging. Also reused (as foreign_pre_chain) to
# normalize log records that come from plain stdlib `logging.getLogger(...)`
# calls made by this app or by third-party libraries (uvicorn, fastapi, ...),
# so everything ends up formatted consistently through the same handlers.
# ---------------------------------------------------------------------------

def _shared_processors() -> list:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]


def _console_or_json_formatter(fmt: str) -> "structlog.stdlib.ProcessorFormatter":
    renderer = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    return structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=_shared_processors(),
    )


def _configure_stdlib(level: str, log_file: Path | None, fmt: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10_485_760, backupCount=5
            )
        )

    if _HAS_STRUCTLOG:
        formatter = _console_or_json_formatter(fmt)
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
    for h in handlers:
        h.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Overwrite (not append) so calling setup_logging() more than once in
    # the same process — e.g. a test harness, or a second entrypoint that
    # imports a module which already called it — can't leave stale/duplicate
    # handlers behind and double-print every line.
    root.handlers = handlers


def setup_logging(level: str = "INFO", fmt: str = "console", log_file: Path | None = None) -> None:
    _configure_stdlib(level, log_file, fmt)

    if _HAS_STRUCTLOG:
        structlog.configure(
            processors=[
                *_shared_processors(),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                # Hand off to stdlib logging. The record is rendered on the
                # way OUT, by the ProcessorFormatter attached to each
                # handler above — same renderer stdlib-originated log
                # records use, so output is consistent either way.
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
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

    Audit events propagate to the normal logging pipeline (console + the
    general log file, once setup_logging() has run) AND, when `audit_file`
    is given, to their own dedicated rotating file in JSON — always JSON,
    regardless of the app's console `fmt` setting, so the audit trail stays
    machine-parseable for security tooling even in "console" mode. This
    handler is self-contained and doesn't depend on setup_logging() having
    already run.
    """

    def __init__(self, audit_file: Path | None = None):
        self._log = get_logger("nexus.audit")
        if audit_file:
            audit_file.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                audit_file, maxBytes=50_485_760, backupCount=10
            )
            if _HAS_STRUCTLOG:
                handler.setFormatter(_console_or_json_formatter("json"))
            else:
                handler.setFormatter(
                    logging.Formatter("%(asctime)s %(name)s %(message)s")
                )
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