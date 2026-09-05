"""One JSON line, the scope on it, nothing secret in it, and never an exception out of it.

The formatter is the layer that has to hold for code this package does not own, so most of
what is asserted here is emitted through a plain ``logging.Logger`` rather than through
:class:`EventLogger` -- which is how ``commerce_api.errors`` and ``durable_worker.loop``
will reach it.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from platform_observability import (
    REDACTED,
    EventLogger,
    JsonFormatter,
    Secret,
    bind_scope,
    configure_logging,
)

TEST_PAN = "4111111111111111"


@pytest.fixture
def sink() -> Iterator[io.StringIO]:
    """A root logger writing JSON into a buffer, restored afterwards."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    stream = io.StringIO()
    configure_logging(stream=stream, level=logging.DEBUG)
    try:
        yield stream
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


class TestEnvelope:
    def test_one_event_is_one_line_of_json(self, sink: io.StringIO) -> None:
        EventLogger("test.emitter").info("checkout.submitted", checkout_version=3)
        assert len(sink.getvalue().splitlines()) == 1
        record = lines(sink)[0]
        assert record["event"] == "checkout.submitted"
        assert record["level"] == "INFO"
        assert record["logger"] == "test.emitter"
        assert record["fields"] == {"checkout_version": 3}
        assert record["ts"].endswith("Z")

    def test_a_newline_in_a_field_does_not_become_a_second_line(self, sink: io.StringIO) -> None:
        """The "one JSON line per event" guarantee, against the most obvious way of
        breaking a log pipeline: a value that contains a line break."""
        EventLogger("test.emitter").info("checkout.submitted", detail="first\nsecond")
        assert len(sink.getvalue().splitlines()) == 1
        assert lines(sink)[0]["fields"]["detail"] == "first\nsecond"

    def test_the_correlation_scope_is_on_the_line(self, sink: io.StringIO) -> None:
        with bind_scope("corr-1", tenant_id="t1", actor_type="WORKER"):
            EventLogger("test.emitter").info("worker.command.completed")
        record = lines(sink)[0]
        assert record["correlation_id"] == "corr-1"
        assert record["tenant_id"] == "t1"
        assert record["actor_type"] == "WORKER"

    def test_the_scope_is_absent_rather_than_null_when_unbound(self, sink: io.StringIO) -> None:
        EventLogger("test.emitter").info("checkout.submitted")
        assert "correlation_id" not in lines(sink)[0]

    def test_a_field_cannot_overwrite_the_envelope(self, sink: io.StringIO) -> None:
        """Fields are nested under ``fields`` for exactly this reason: the meaning of every
        top-level key is fixed by this module and not by the last call site."""
        EventLogger("test.emitter").info(
            "checkout.submitted", level="urgent", ts=0, event="something.else", logger="fake"
        )
        record = lines(sink)[0]
        assert record["level"] == "INFO"
        assert record["event"] == "checkout.submitted"
        assert record["logger"] == "test.emitter"
        assert record["ts"] != 0
        assert record["fields"]["level"] == "urgent"

    def test_a_foreign_log_record_gets_the_same_envelope(self, sink: io.StringIO) -> None:
        """``commerce_api.errors`` and ``durable_worker.loop`` will never import this
        package, and both have to come out as JSON with the correlation id on them."""
        with bind_scope("corr-1", tenant_id="t1"):
            logging.getLogger("commerce_api.errors").warning("checkout %s went stale", "abc")
        record = lines(sink)[0]
        assert record["message"] == "checkout abc went stale"
        assert record["correlation_id"] == "corr-1"
        assert record["event"] == "commerce_api.log"

    def test_an_extra_on_a_foreign_record_is_redacted_like_a_field(self, sink: io.StringIO) -> None:
        logging.getLogger("uvicorn.access").info(
            "served", extra={"route": "/v1/checkouts", "authorization": "Bearer abc123456789"}
        )
        fields = lines(sink)[0]["fields"]
        assert fields["route"] == "/v1/checkouts"
        assert fields["authorization"] == REDACTED


class TestRedactionAtFormatTime:
    """The value must not appear even when it is passed deliberately."""

    def test_a_denied_field_cannot_be_logged(self, sink: io.StringIO) -> None:
        EventLogger("test.emitter").info(
            "webhook.received",
            razorpay_signature="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            token="tok_live_abcdefghijklmnop",  # noqa: S106 - the point of the test
        )
        fields = lines(sink)[0]["fields"]
        assert fields["razorpay_signature"] == REDACTED
        assert fields["token"] == REDACTED

    def test_a_card_number_cannot_be_logged_under_any_field_name(self, sink: io.StringIO) -> None:
        EventLogger("test.emitter").info("checkout.submitted", note=f"card {TEST_PAN}")
        assert TEST_PAN not in sink.getvalue()

    def test_a_card_number_cannot_be_logged_in_a_message(self, sink: io.StringIO) -> None:
        """The layer that covers a package this one does not own."""
        logging.getLogger("some.other.package").info("charging %s", TEST_PAN)
        assert TEST_PAN not in sink.getvalue()

    def test_a_secret_interpolated_into_a_message_is_the_marker(self, sink: io.StringIO) -> None:
        secret = Secret("rzp_live_supersecret")
        logging.getLogger("some.other.package").info("using %s", secret)
        assert "supersecret" not in sink.getvalue()
        assert lines(sink)[0]["message"] == f"using {REDACTED}"

    def test_a_whole_body_passed_as_a_field_is_a_digest(self, sink: io.StringIO) -> None:
        """``LogValue`` rejects this at type-check time; this asserts what happens when
        somebody gets past that with a cast or from untyped code."""
        body = {"event": "payment.captured", "payload": {"payment": {"id": "pay_x"}}}
        logging.getLogger("some.other.package").info("applying", extra={"evidence": body})
        assert "payment.captured" not in sink.getvalue()
        assert lines(sink)[0]["fields"]["evidence"].startswith("[redacted:dict sha256=")

    def test_an_exception_message_is_scrubbed(self, sink: io.StringIO) -> None:
        try:
            raise ValueError(f"could not charge {TEST_PAN}")
        except ValueError:
            EventLogger("test.emitter").exception("payment.failed")
        assert TEST_PAN not in sink.getvalue()
        assert "ValueError" in lines(sink)[0]["exception"]


class TestEventNames:
    def test_a_valid_name_is_kept(self, sink: io.StringIO) -> None:
        EventLogger("test.emitter").info("worker.command.dead_lettered")
        assert lines(sink)[0]["event"] == "worker.command.dead_lettered"

    @pytest.mark.parametrize("name", ["failed", "Worker.Failed", "worker failed", "worker."])
    def test_an_invalid_name_is_replaced_rather_than_refused(
        self, sink: io.StringIO, name: str
    ) -> None:
        """Never raise over a log line. The bad name becomes one constant, so the mistake
        shows as a spike on a single value rather than as unqueryable scatter, and the
        offender is kept in a field so the call site is findable."""
        EventLogger("test.emitter").info(name)
        record = lines(sink)[0]
        assert record["event"] == "invalid_event_name"
        assert record["fields"]["requested_event"] == name


class TestTotality:
    """Losing a log line is acceptable. Raising into commerce is not."""

    def test_a_formatter_that_cannot_render_still_returns_a_line(self) -> None:
        class Exploding(JsonFormatter):
            def _render(self, record: logging.LogRecord) -> str:
                raise RuntimeError("boom")

        record = logging.LogRecord("x", logging.INFO, "f", 1, "m", None, None)
        rendered = Exploding().format(record)
        assert json.loads(rendered)["event"] == "observability.log.render_failed"

    def test_a_record_whose_message_cannot_be_rendered_still_produces_a_line(self) -> None:
        """``getMessage()`` raises when a format string and its arguments disagree. Some
        handlers (pytest's included) let that escape; this formatter must not, because the
        call site it happens at is usually an error path already."""
        record = logging.LogRecord(
            "some.other.package", logging.INFO, "f", 1, "two args %s %s", ("only-one",), None
        )
        assert json.loads(JsonFormatter().format(record))["event"] in {
            "some.log",
            "observability.log.render_failed",
        }

    def test_a_handler_that_raises_does_not_reach_the_caller(self) -> None:
        class Hostile(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                raise RuntimeError("the log shipper is down")

            def handle(self, record: logging.LogRecord) -> bool:
                raise RuntimeError("the log shipper is down")

        logger = logging.getLogger("test.hostile")
        logger.propagate = False
        logger.addHandler(Hostile())
        try:
            EventLogger(logger).info("checkout.submitted")  # must not raise
        finally:
            logger.handlers.clear()
            logger.propagate = True

    def test_an_unserialisable_value_becomes_a_type_marker(self, sink: io.StringIO) -> None:
        class Opaque:
            __slots__ = ()

        logging.getLogger("some.other.package").info("x", extra={"thing": Opaque()})
        assert lines(sink)[0]["fields"]["thing"].startswith("[redacted:Opaque")


class TestConfiguration:
    def test_configure_logging_replaces_existing_handlers_by_default(self) -> None:
        root = logging.getLogger()
        saved, level = list(root.handlers), root.level
        try:
            root.addHandler(logging.NullHandler())
            handler = configure_logging(stream=io.StringIO())
            assert root.handlers == [handler]
            assert isinstance(handler.formatter, JsonFormatter)
        finally:
            for existing in list(root.handlers):
                root.removeHandler(existing)
            for existing in saved:
                root.addHandler(existing)
            root.setLevel(level)


class TestTheEnvelopeIsNotRepeatedInTheFields:
    """``configure_logging`` installs the filter *and* the formatter, so both see the scope.

    The filter copies the scope onto the record for handlers that read attributes; the
    formatter reads the scope itself. Left alone, the formatter would then collect the
    filter's attributes as if a caller had passed them, and every line in a mounted
    process would carry the correlation id, the tenant and the actor type twice. Found by
    mounting this package in ``commerce-api`` and ``durable-worker`` and reading the
    output of a real request.
    """

    def test_the_scope_appears_once_per_line(self, sink: io.StringIO) -> None:
        with bind_scope("01a0-corr", tenant_id="t-1", actor_type="WORKER"):
            EventLogger("test.emitter").info("checkout.submitted", checkout_version=3)

        line = lines(sink)[0]
        assert line["correlation_id"] == "01a0-corr"
        assert line["tenant_id"] == "t-1"
        assert line["actor_type"] == "WORKER"
        assert line["fields"] == {"checkout_version": 3}

    def test_a_foreign_record_carries_its_own_extras_and_not_the_scope_twice(
        self, sink: io.StringIO
    ) -> None:
        """A library that knows nothing about this package still contributes its ``extra=``.

        ``uvicorn`` does exactly this -- it attaches ``color_message`` -- so the filtering
        has to remove the envelope's own keys without removing anybody else's.
        """
        with bind_scope("01a0-corr", tenant_id="t-1"):
            logging.getLogger("some.foreign.library").warning("retrying", extra={"attempt": 2})

        line = lines(sink)[0]
        assert line["correlation_id"] == "01a0-corr"
        assert line["fields"] == {"attempt": 2}

    def test_a_field_a_caller_passed_deliberately_survives_its_name(
        self, sink: io.StringIO
    ) -> None:
        """Only record *attributes* are filtered. A field on the package's own carrier was
        meant to be recorded, so it is kept -- and it still cannot reach the envelope,
        which is the property nesting exists to guarantee."""
        with bind_scope("01a0-corr"):
            EventLogger("test.emitter").info("webhook.applied", correlation_id="upstream-id")

        line = lines(sink)[0]
        assert line["correlation_id"] == "01a0-corr"
        assert line["fields"] == {"correlation_id": "upstream-id"}
