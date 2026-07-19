"""
Observability setup for signoz-mcp.

Structured logging is always on. OTEL is opt-in via env var.
"""

import contextlib
import logging
import os
import sys

import structlog


def configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file = os.getenv("LOG_FILE", "/opt/appdata/signoz-mcp/logs/signoz-mcp.log")

    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    stderr_handler: logging.Handler = logging.StreamHandler(sys.stderr)
    handlers: list[logging.Handler] = [stderr_handler]
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, mode=0o750, exist_ok=True)
            os.chmod(log_dir, 0o750)  # fix pre-existing dir if umask was wrong
        handlers.append(logging.FileHandler(log_file))
        with contextlib.suppress(OSError):
            os.chmod(log_file, 0o640)  # tighten: FileHandler creates with process umask (0664)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    for h in handlers:
        root_logger.addHandler(h)
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )
    for h in handlers:
        h.setFormatter(formatter)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# OTEL tracing + metrics now live in signoz_mcp/telemetry.py (opt-in via
# OTEL_EXPORTER_OTLP_ENDPOINT, wired into every tool call by server.instrument).
