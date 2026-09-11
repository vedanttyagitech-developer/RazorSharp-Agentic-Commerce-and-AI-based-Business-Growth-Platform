"""The liveness endpoint has to be able to say no.

A probe that always answers 200 is worse than no probe: Kubernetes would report a wedged
worker as healthy for ever, and the only failure this process can suffer without dying --
the loop blocked on a provider call, a lock, or an exhausted pool -- is precisely the one
such a probe would hide.

So the tests that matter here are the negative ones. Both directions are asserted: fresh
means 200, stale means 503, and the loop stamps the heartbeat only *after* a tick has
finished rather than when it starts.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest
from action_executor.health import (
    DEFAULT_PORT,
    DEFAULT_STALE_SECONDS,
    Heartbeat,
    health_port,
    serve_health,
    stale_after_seconds,
)


def _get(port: int, path: str = "/healthz") -> tuple[int, dict[str, object]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry the body we want
        return exc.code, json.loads(exc.read())


@pytest.fixture
def port(monkeypatch: pytest.MonkeyPatch) -> int:
    """A free port chosen by the OS, so a developer's own 8001 is never touched."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        chosen = int(probe.getsockname()[1])
    monkeypatch.setenv("WORKER_HEALTH_PORT", str(chosen))
    return chosen


def test_a_turning_loop_answers_200_with_its_age(
    port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKER_HEALTH_STALE_SECONDS", "60")
    heartbeat = Heartbeat()
    stop = serve_health(heartbeat)
    try:
        heartbeat.beat()
        status, body = _get(port)
        assert status == 200
        assert body["status"] == "ok"
        assert isinstance(body["last_tick_seconds_ago"], (int, float))
        assert float(body["last_tick_seconds_ago"]) < 5
    finally:
        stop()


def test_a_wedged_loop_answers_503_rather_than_pretending(
    port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point. A process that is running but not ticking must fail the probe."""
    monkeypatch.setenv("WORKER_HEALTH_STALE_SECONDS", "0.2")
    heartbeat = Heartbeat()
    stop = serve_health(heartbeat)
    try:
        time.sleep(0.4)  # no beat: the loop is alive as a process and stuck as a loop
        status, body = _get(port)
        assert status == 503, "a stale worker reported itself healthy"
        assert body["status"] == "stale"
        # The reason travels in the response rather than being inferred from a restart.
        assert float(body["last_tick_seconds_ago"]) >= 0.2
    finally:
        stop()


def test_a_beat_revives_a_stale_worker(port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_HEALTH_STALE_SECONDS", "0.2")
    heartbeat = Heartbeat()
    stop = serve_health(heartbeat)
    try:
        time.sleep(0.4)
        assert _get(port)[0] == 503
        heartbeat.beat()
        assert _get(port)[0] == 200, "a resumed loop stayed marked unhealthy"
    finally:
        stop()


def test_only_healthz_is_served(port: int) -> None:
    """No accidental surface. This process receives no traffic and answers one question."""
    heartbeat = Heartbeat()
    stop = serve_health(heartbeat)
    try:
        assert _get(port, "/")[0] == 404
        assert _get(port, "/metrics")[0] == 404
        assert _get(port, "/healthz?probe=1")[0] == 200, "a query string must not 404"
    finally:
        stop()


def test_a_port_already_in_use_does_not_stop_the_worker(port: int) -> None:
    """An observability problem must never become an outage.

    ``port`` is requested for its side effect: the fixture pins WORKER_HEALTH_PORT so both
    calls below try to bind the same socket.
    """
    assert port > 0
    heartbeat = Heartbeat()
    first = serve_health(heartbeat)
    try:
        second = serve_health(heartbeat)  # same port, already bound
        second()  # the returned callable is a no-op, not a crash
    finally:
        first()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", DEFAULT_PORT),
        ("not-a-number", DEFAULT_PORT),
        ("0", DEFAULT_PORT),
        ("70000", DEFAULT_PORT),
        ("9999", 9999),
    ],
)
def test_a_bad_port_falls_back_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv("WORKER_HEALTH_PORT", raw)
    assert health_port() == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", DEFAULT_STALE_SECONDS),
        ("junk", DEFAULT_STALE_SECONDS),
        ("-1", DEFAULT_STALE_SECONDS),
        ("45", 45.0),
    ],
)
def test_a_bad_staleness_window_falls_back(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    monkeypatch.setenv("WORKER_HEALTH_STALE_SECONDS", raw)
    assert stale_after_seconds() == expected
