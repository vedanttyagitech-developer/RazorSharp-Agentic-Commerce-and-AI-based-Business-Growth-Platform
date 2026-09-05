"""One JSON line per event, with the correlation scope on it, and nothing secret in it.

Two things emit through here and they need different treatment.

**This platform's own events.** :class:`EventLogger` takes a stable dotted event name and
typed fields. ``LogValue`` admits only scalars, so the attempt to log a webhook body or a
basket does not type-check, and :mod:`platform_observability.redaction` removes what
survives that. The event name is the thing a query groups by, so it is validated rather
than trusted.

**Everything else.** ``commerce_api.errors`` already calls ``_log.exception(...)``, the
worker logs a line per command, uvicorn logs a line per request, and none of them will ever
import this package. :class:`JsonFormatter` therefore does its work at *format* time,
where every record passes regardless of who emitted it: the message is rendered, scrubbed
and capped, the correlation scope is attached, and the result is one line of JSON. That is
the layer that catches ``_log.debug("headers=%s", request.headers)`` in code this package
does not own.

Shape of a line::

    {"ts":"2026-09-05T18:41:02.481913Z","level":"INFO","logger":"durable_worker.loop",
     "event":"worker.command.completed","message":"command ... -> OK",
     "correlation_id":"01a06f...","tenant_id":"...","actor_type":"WORKER",
     "fields":{"command_type":"REFUND_EXECUTE","attempt":2}}

Fields are **nested** under ``fields`` rather than spread at the top level, and that is a
redaction property rather than a style choice: a caller passing ``level="urgent"`` or
``ts=0`` cannot overwrite the envelope, so the meaning of every top-level key is fixed by
this module and not by whoever wrote the last call site.

Nothing here raises. A formatter that throws does not merely lose its record -- depending
on the handler it prints a traceback to stderr and, under a logging configuration that
routes stderr back into logging, does so repeatedly. :meth:`JsonFormatter.format` catches
everything and falls back to a minimal line saying so, because a log line is worth less
than the process.

On the specification's one explicit logging rule (19.13): callback exceptions inside a
receive loop are logged at ``warning`` or ``exception``, never ``debug``. This module gives
those levels the same treatment as every other and adds :meth:`EventLogger.exception`, so
there is no reason left to reach for ``debug`` to avoid noise.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from re import Pattern
from re import compile as re_compile
from typing import Any, Final

from .correlation import scope_fields
from .redaction import LogValue, cap_exception, cap_message, redact_fields

__all__ = [
    "EVENT_NAME_PATTERN",
    "FIELDS_ATTRIBUTE",
    "CorrelationFilter",
    "EventLogger",
    "JsonFormatter",
    "configure_logging",
]

#: The ``LogRecord`` attribute this package's fields travel on. Namespaced so it cannot
#: collide with an attribute some other library attaches via ``extra=``.
FIELDS_ATTRIBUTE: Final[str] = "platform_observability_fields"

#: A stable event name: lower snake segments, at least two, dot-separated --
#: ``checkout.submitted``, ``worker.command.dead_lettered``, ``voice.stream.rotated``.
#: Two segments minimum because a one-word event name ("failed") is unqueryable the moment
#: a second subsystem emits one too.
EVENT_NAME_PATTERN: Final[Pattern[str]] = re_compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")

#: What an invalid name becomes. A constant, so the mistake shows up as a spike on one
#: value rather than as a scattering of unqueryable names.
_INVALID_EVENT: Final[str] = "invalid_event_name"

#: ``LogRecord`` attributes that belong to the record itself. Anything else a caller
#: attached through ``extra=`` is treated as a field and redacted like one.
_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
        FIELDS_ATTRIBUTE,
    }
)


def _timestamp(created: float) -> str:
    """RFC 3339 in UTC with a ``Z``, matching the timestamp format of spec 24.1."""
    return datetime.fromtimestamp(created, UTC).isoformat().replace("+00:00", "Z")


def _json_default(value: object) -> str:
    """Anything the encoder cannot serialise becomes a type marker, never a ``repr``.

    A ``repr`` is how an ORM row's whole contents end up in a log store. The type name is
    enough to find the call site and carries nothing.
    """
    return f"[redacted:{type(value).__name__}]"


class JsonFormatter(logging.Formatter):
    """Render any ``LogRecord`` as one line of JSON. Never raises.

    Install it on the handler and every logger in the process is covered, including the
    ones in packages that have never heard of this one. That is the point: redaction that
    only applies to code which opted in is redaction by discipline again.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._render(record)
        except Exception as exc:  # pragma: no cover - defence; _render is total already
            # Deliberately hand-built rather than json.dumps: if the encoder is what broke,
            # calling it again in the fallback breaks the fallback too.
            return (
                '{"level":"ERROR","logger":"platform_observability.logs",'
                '"event":"observability.log.render_failed","message":'
                f'"{type(exc).__name__}"}}'
            )

    def _render(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "event": self._event_name(record),
            "message": cap_message(record.getMessage()),
        }
        payload.update(scope_fields())

        fields = self._fields(record)
        if fields:
            payload["fields"] = fields

        if record.exc_info:
            payload["exception"] = cap_exception(self.formatException(record.exc_info))
        elif record.exc_text:
            payload["exception"] = cap_exception(record.exc_text)
        if record.stack_info:
            payload["stack"] = cap_exception(self.formatStack(record.stack_info))

        # ensure_ascii=False keeps a Hindi product name readable; json.dumps escapes every
        # control character including newlines regardless, which is what guarantees the
        # "one line" in "one JSON line per event".
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default)

    def _event_name(self, record: logging.LogRecord) -> str:
        """The record's event name: validated if given, derived from the logger if not."""
        raw = getattr(record, "event", None)
        if raw is None:
            # A record from a package that does not know about events still gets one, so a
            # log query never has to special-case their absence.
            return f"{record.name.split('.')[0]}.log"
        name = str(raw)
        return name if EVENT_NAME_PATTERN.match(name) else _INVALID_EVENT

    def _fields(self, record: logging.LogRecord) -> dict[str, LogValue]:
        """This package's fields, plus anything a foreign caller attached via ``extra=``."""
        collected: dict[str, Any] = {}
        supplied = getattr(record, FIELDS_ATTRIBUTE, None)
        if isinstance(supplied, Mapping):
            collected.update(supplied)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in collected and key != "event":
                collected[key] = value
        raw_event = getattr(record, "event", None)
        if raw_event is not None and not EVENT_NAME_PATTERN.match(str(raw_event)):
            # Keep the offender, redacted, so the bad call site is findable.
            collected["requested_event"] = str(raw_event)
        return redact_fields(collected)


class CorrelationFilter(logging.Filter):
    """Attach the bound correlation scope to records, for handlers that read attributes.

    :class:`JsonFormatter` reads the scope itself, so this is only needed when a record has
    to carry the scope past the formatter -- a third-party handler shipping structured
    attributes to a log API, say. Always returns ``True``: a filter that can drop records is
    a filter that will one day drop the record that mattered.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in scope_fields().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class EventLogger:
    """The typed front door for this platform's own events.

    ``**fields`` is typed :data:`~platform_observability.redaction.LogValue`, so under
    ``mypy --strict`` a mapping, a list or ``bytes`` is a type error at the call site --
    which is where "a full webhook body must be unrepresentable in a log line" is actually
    enforced. Everything that gets past the type checker meets the redactor.

    ::

        log = EventLogger("durable_worker.loop")
        log.info("worker.command.completed", command_type="REFUND_EXECUTE", attempt=2)
        log.exception("worker.command.failed", command_type="REFUND_EXECUTE")
    """

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger | str) -> None:
        self._logger = logging.getLogger(logger) if isinstance(logger, str) else logger

    @property
    def logger(self) -> logging.Logger:
        """The underlying stdlib logger, for level checks and handler wiring."""
        return self._logger

    def event(
        self,
        name: str,
        *,
        level: int = logging.INFO,
        message: str | None = None,
        exc_info: bool = False,
        **fields: LogValue,
    ) -> None:
        """Emit one event. Never raises.

        ``message`` is optional free text for a human; the *event name* is what a query
        groups by, so a dashboard never depends on the prose.

        ``level``, ``message`` and ``exc_info`` are keyword arguments of this method, so a
        field by one of those names is not reachable here. The level-named shortcuts below
        do not have that problem -- they take nothing but the name and fields -- which is
        why they are the ones to reach for.
        """
        self._emit(name, level, message, exc_info, fields)

    def _emit(
        self,
        name: str,
        level: int,
        message: str | None,
        exc_info: bool,
        fields: Mapping[str, LogValue],
    ) -> None:
        """The single emit path. Fields travel as a mapping rather than as ``**kwargs`` so
        no field name can be captured by a parameter of this method."""
        # Nothing is logged about a logging failure: that call is the one guaranteed to
        # fail the same way, and a handler raising into its own error path is how a log
        # outage becomes a request outage. Losing the line is the acceptable outcome here.
        with contextlib.suppress(Exception):
            self._logger.log(
                level,
                message if message is not None else name,
                exc_info=exc_info,
                extra={"event": name, FIELDS_ATTRIBUTE: dict(fields)},
            )

    def debug(self, name: str, **fields: LogValue) -> None:
        """Detail for a developer. Never the level for a swallowed exception: specification
        19.13 requires a callback failure at ``warning`` or ``exception``, because a lost
        turn that only appears at ``debug`` is a turn nobody ever finds out about."""
        self._emit(name, logging.DEBUG, None, False, fields)

    def info(self, name: str, **fields: LogValue) -> None:
        """A thing happened that a reader of the log would expect to see."""
        self._emit(name, logging.INFO, None, False, fields)

    def warning(self, name: str, **fields: LogValue) -> None:
        """Something went wrong and the platform handled it -- a retry, a degraded path."""
        self._emit(name, logging.WARNING, None, False, fields)

    def error(self, name: str, **fields: LogValue) -> None:
        """Something went wrong that a person should look at."""
        self._emit(name, logging.ERROR, None, False, fields)

    def exception(self, name: str, **fields: LogValue) -> None:
        """An error with the active traceback attached. Call from inside an ``except``."""
        self._emit(name, logging.ERROR, None, True, fields)


def configure_logging(
    *,
    level: int = logging.INFO,
    stream: Any | None = None,
    force: bool = True,
) -> logging.Handler:
    """Put :class:`JsonFormatter` on the root logger and return the handler.

    Called once by whoever owns the process -- the API's lifespan, the worker's ``main``.
    Not called at import: a library that reconfigures logging when it is imported is a
    library that fights the application over its own stderr.

    ``force`` replaces existing root handlers, which is what makes this deterministic under
    uvicorn (it installs its own) and under pytest (``caplog`` installs one too). Pass
    ``force=False`` to add alongside.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationFilter())
    root = logging.getLogger()
    if force:
        for existing in list(root.handlers):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    return handler
