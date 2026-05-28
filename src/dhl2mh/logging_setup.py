"""Structlog setup. Console renderer on a TTY (local dev), JSON otherwise (cron)."""

import logging
import sys

import structlog


def setup_logging(level: str = "INFO", *, json: bool | None = None) -> None:
    """Configure structlog. ``json=None`` auto-detects by stderr.isatty()."""
    if json is None:
        json = not sys.stderr.isatty()

    level_num = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(level=level_num, stream=sys.stderr, format="%(message)s")

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level_num),
        cache_logger_on_first_use=True,
    )
