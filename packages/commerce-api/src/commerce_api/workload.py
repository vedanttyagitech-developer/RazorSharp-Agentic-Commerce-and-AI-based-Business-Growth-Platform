"""Admission budgets for the supported single API process.

Voice turns reach the same agent endpoint, sharing typed-turn user and tenant budgets.
Keys use authenticated identity, never caller-provided tenant headers.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from commerce_domain.workload import WorkloadExceededError
from fastapi import HTTPException, Request


@contextmanager
def admission(request: Request, limits: Sequence[tuple[str, int, int]]) -> Iterator[None]:
    try:
        with request.app.state.workload.admit(limits):
            yield
    except WorkloadExceededError as exc:
        raise HTTPException(
            429,
            "Demo is busy. Please wait a minute and try again.",
            headers={"Retry-After": "60"},
        ) from exc
