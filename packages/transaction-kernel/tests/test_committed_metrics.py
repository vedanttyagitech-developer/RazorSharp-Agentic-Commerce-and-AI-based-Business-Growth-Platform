"""Metrics reflect committed outcomes, including nested-savepoint semantics."""

import uuid

import pytest
from platform_observability.instruments import default_registry, reset_default_registry
from sqlalchemy.orm import Session
from transaction_kernel.metrics import increment

NAME = "commerce_execution_grants_issued_total"


def record(session):
    increment(session, uuid.UUID(int=1), NAME, operation="RESERVE_DEBIT")


def count():
    return [
        line for line in default_registry().render().splitlines() if line.startswith(NAME + "{")
    ]


@pytest.mark.parametrize("commit", [True, False])
def test_outer_transaction(commit):
    reset_default_registry()
    with Session() as session:
        session.begin()
        record(session)
        assert count() == []
        if commit:
            session.commit()
        else:
            session.rollback()
    assert bool(count()) == commit


def test_savepoint_commit_is_not_outer_commit():
    reset_default_registry()
    with Session() as session:
        session.begin()
        with session.begin_nested():
            record(session)
        assert count() == []
        session.rollback()
    assert count() == []


def test_savepoint_rollback_discards_only_its_events():
    reset_default_registry()
    with Session() as session, session.begin():
        record(session)
        nested = session.begin_nested()
        record(session)
        nested.rollback()
        with session.begin_nested():
            record(session)
        assert count() == []
    assert len(count()) == 1 and count()[0].endswith(" 2")


def test_closed_session_cannot_publish_abandoned_events_on_reuse():
    reset_default_registry()
    session = Session()
    session.begin()
    record(session)
    session.close()
    with session.begin():
        pass
    assert count() == []


@pytest.mark.parametrize("commit", [True, False])
def test_webhook_lag_observation_is_commit_aware(commit):
    from transaction_kernel.metrics import observe

    reset_default_registry()
    name = "commerce_webhook_apply_lag_seconds"
    with Session() as session:
        session.begin()
        observe(session, uuid.UUID(int=1), name, 2.5, event_type="payment.captured")
        assert not any(
            line.startswith(name + "_count{") for line in default_registry().render().splitlines()
        )
        if commit:
            session.commit()
        else:
            session.rollback()
    count = [
        line
        for line in default_registry().render().splitlines()
        if line.startswith(name + "_count{")
    ]
    assert bool(count) == commit
