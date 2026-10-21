"""JSON logs on stdout (conventions §5): structlog formats, stdlib `logging` is the transport.

Code logs with the stdlib API and a constant message; values go into `extra`, for example
`logger.info("request completed", extra={"event.name": "http.server.request", ...})`.
The same records also reach the OTel log handler (see `otel.py`), so both sinks see the
same fields.
"""

import logging
import sys
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

import structlog
from opentelemetry import trace

from synthwerk_vision.platform.config import Settings

# Never written to a log line, whoever passes them (conventions §5).
FORBIDDEN_KEYS = frozenset(
    {
        "password",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "cookie",
        "set-cookie",
        "secret",
        "api_key",
        "prompt",
        "messages",
        "content",
        "email",
        "image",
        "body",
    }
)
# uvicorn passes an ANSI-coloured copy of each message; it only adds noise.
_DROPPED_KEYS = FORBIDDEN_KEYS | {"color_message"}
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}
_LEVEL_NAMES = {"WARNING": "WARN", "CRITICAL": "FATAL"}

_request_id: ContextVar[str | None] = ContextVar("synthwerk_request_id", default=None)


@contextmanager
def request_id_bound(request_id: str) -> Iterator[None]:
    """Add `synthwerk.request_id` to every log record written inside the block."""
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


class RecordContext(logging.Filter):
    """Per-record fields for every sink: drops forbidden keys, adds the request id and a
    default `event.name`. A handler filter, so library loggers get it too."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in _DROPPED_KEYS.intersection(record.__dict__):
            del record.__dict__[key]
        request_id = _request_id.get()
        if request_id is not None:
            record.__dict__.setdefault("synthwerk.request_id", request_id)
        if record.levelno >= logging.INFO:
            record.__dict__.setdefault("event.name", record.name)
        return True


def _timestamp(_: Any, __: str, event: MutableMapping[str, Any]) -> Mapping[str, Any]:
    record: logging.LogRecord = event["_record"]
    created = datetime.fromtimestamp(record.created, UTC)
    event["timestamp"] = created.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    event["level"] = _LEVEL_NAMES.get(record.levelname, record.levelname)
    return event


def _trace_context(_: Any, __: str, event: MutableMapping[str, Any]) -> Mapping[str, Any]:
    span = trace.get_current_span().get_span_context()
    if span.is_valid:
        event["trace_id"] = format(span.trace_id, "032x")
        event["span_id"] = format(span.span_id, "016x")
    return event


def _service_fields(fields: Mapping[str, str]) -> structlog.typing.Processor:
    def add(_: Any, __: str, event: MutableMapping[str, Any]) -> Mapping[str, Any]:
        event.update(fields)
        return event

    return add


def _tidy(_: Any, __: str, event: MutableMapping[str, Any]) -> Mapping[str, Any]:
    """Drop the formatter's copy of the message (it is `msg`) and put the core fields first."""
    event.pop("message", None)
    head = {key: event.pop(key) for key in ("timestamp", "level", "msg") if key in event}
    return {**head, **event}


def _is_ours(handler: logging.Handler) -> bool:
    return getattr(handler, "synthwerk", False) is True


def install_handler(handler: logging.Handler) -> None:
    """Attach a handler to the root logger with the shared record filter."""
    handler.addFilter(RecordContext())
    handler.synthwerk = True  # type: ignore[attr-defined]
    logging.getLogger().addHandler(handler)


def remove_handler(handler: logging.Handler) -> None:
    logging.getLogger().removeHandler(handler)
    handler.close()


def configure_logging(
    settings: Settings, service: Mapping[str, str], stream: TextIO | None = None
) -> logging.StreamHandler[TextIO]:
    """Route every logger (ours, uvicorn, libraries) through one stdout handler.

    Idempotent: a second call replaces the handlers a first call installed. Handlers that
    others put on the root logger (pytest's caplog, for one) stay.
    """
    console = settings.log_format == "console" and settings.platform_env == "local"
    renderer: structlog.typing.Processor = (
        structlog.dev.ConsoleRenderer() if console else structlog.processors.JSONRenderer()
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            _timestamp,
            structlog.stdlib.ExtraAdder(),
            _service_fields(service),
            _trace_context,
            structlog.processors.EventRenamer("msg"),
            structlog.processors.format_exc_info,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _tidy,
            renderer,
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for old in [h for h in root.handlers if _is_ours(h)]:
        remove_handler(old)
    install_handler(handler)
    root.setLevel(_LEVELS[settings.log_level])

    # uvicorn sets up its own handlers before it loads the app; send its records to ours.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    # The request middleware writes the request line (with the route, not the raw path).
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    return handler
