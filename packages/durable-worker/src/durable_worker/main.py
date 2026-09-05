"""The process entry point: read the environment, build the runtime, tick.

Two modes, and the difference is only how many ticks:

``python -m durable_worker`` runs until it is told to stop. ``--once`` runs a single pass
and exits, which is what a scheduled job, a smoke test and a demonstration script all
want, and what makes "did the worker do anything" answerable without tailing a log.

``SIGTERM`` and ``SIGINT`` set a flag rather than raising. Kubernetes sends ``SIGTERM``
and then waits; a worker that died where the signal found it could be halfway between
consuming a grant and recording what the provider said, which is recoverable -- the
redelivered command finds the grant consumed and reconciles -- but slower and noisier
than finishing the command in hand. The flag is read between commands, so shutdown is
prompt without being abrupt.

Nothing here logs a credential. The startup line names the profile, the worker id and
whether Razorpay is in test mode; the configuration object's ``repr`` is redacted, and
the Razorpay config's is too. From ``main`` onwards that is enforced rather than
remembered: :func:`platform_observability.configure_logging` puts a redacting JSON
formatter on the root logger, so a card number, a bearer credential or an ``rzp_live_``
key is removed from *any* record in this process -- including ones written by ``httpx``,
by SQLAlchemy, and by future code that has never heard of the redactor.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from types import FrameType
from typing import Final

from platform_observability import configure_logging

from .loop import TickReport, run_forever, run_once
from .settings import WorkerRuntime, WorkerSettings, build_runtime, get_settings

__all__ = ["build_parser", "main", "run"]

_LOG: Final = logging.getLogger("durable_worker.main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="durable-worker",
        description=("Execute admitted outbox commands. The only process that calls Razorpay."),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single tick (including housekeeping) and exit",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="logging level for the worker's own logger (default: INFO)",
    )
    return parser


def run(runtime: WorkerRuntime, *, once: bool) -> TickReport:
    """Run the loop under a stop flag that survives ``SIGTERM``."""
    stopping = False

    def stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True
        _LOG.info("signal %s received; stopping after the command in hand", signum)

    for received in (signal.SIGTERM, signal.SIGINT):
        signal.signal(received, stop)

    if once:
        return run_once(runtime, housekeeping=True)
    return run_forever(runtime, should_stop=lambda: stopping)


def main(argv: list[str] | None = None) -> int:
    """Start the worker. Returns a process exit code.

    A configuration fault is reported as a message and a non-zero exit rather than a
    traceback: the messages that matter here -- a live key outside production, a missing
    role URL -- name the exact rule that was broken, and a stack trace above them only
    buries the sentence an operator needs.
    """
    args = build_parser().parse_args(argv)
    # One JSON object per line, carrying whatever correlation scope is bound where the
    # line was written, with redaction applied at format time so it covers every logger in
    # the process rather than only the ones that opted in. Called here, in the process
    # entry point, and never at import: a library that reconfigures logging when it is
    # imported takes stderr away from whoever imported it.
    configure_logging(level=getattr(logging, str(args.log_level).upper(), logging.INFO))

    try:
        settings: WorkerSettings = get_settings()
        runtime = build_runtime(settings)
    except Exception as exc:
        _LOG.error("worker refused to start: %s: %s", type(exc).__name__, exc)
        return 2

    _LOG.info(
        "worker %s starting: profile=%s razorpay_test_mode=%s batch=%d lease=%ds",
        settings.worker_id,
        settings.profile.value,
        runtime.razorpay.is_test_mode,
        settings.batch_size,
        settings.lease_seconds,
    )
    report = run(runtime, once=bool(args.once))
    _LOG.info(
        "worker %s stopped: leased=%d completed=%d failed=%d dead=%d",
        settings.worker_id,
        report.leased,
        report.completed,
        report.failed,
        report.dead_letters,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
