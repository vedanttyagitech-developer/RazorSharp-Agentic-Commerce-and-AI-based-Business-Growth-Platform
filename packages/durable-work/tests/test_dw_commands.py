"""Outbox command vocabulary tests.

Pure tests pin the payload contract: every command survives ``to_payload`` ->
``from_payload`` unchanged, canonicalizes under the integer-only JCS profile, and refuses
anything that is not exactly its own shape. The database tests run as
``commerce_test_kernel`` (NOSUPERUSER, NOBYPASSRLS) and prove the two things a pure test
cannot: that :func:`enqueue_command` writes a row the worker parses back, and that the
grant binding rebuilt from a stored payload is the one the kernel's
:func:`~transaction_kernel.grants.consume_grant` actually accepts.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from commerce_domain import Money, canonicalize, uuid7
from durable_work import (
    COMMAND_VERSION,
    AnyCommand,
    ApplyWebhookEventCommand,
    CommandType,
    CreateOrderCommand,
    LeasedCommand,
    OutboxUsageError,
    ReconcilePaymentCommand,
    ReconcileRefundCommand,
    RefundExecuteCommand,
    enqueue_command,
    idempotency_key_of,
    lease,
    parse_command,
    parse_leased_command,
)
from platform_db import set_tenant
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from transaction_kernel.contracts import CheckoutRef, Operation
from transaction_kernel.grants import (
    GrantAlreadyConsumedError,
    GrantBinding,
    GrantBindingMismatchError,
    GrantStatus,
    consume_grant,
    issue_grant,
)

pytestmark = pytest.mark.db

# ------------------------------------------------------------------------ fixtures


def _ids() -> dict[str, str]:
    return {
        "tenant_id": str(uuid.uuid4()),
        "payment_attempt_id": str(uuid7()),
        "grant_id": str(uuid7()),
        "checkout_id": str(uuid7()),
        "refund_id": str(uuid7()),
        "inbox_id": str(uuid7()),
        "correlation_id": str(uuid7()),
    }


def make_create_order(**overrides: Any) -> CreateOrderCommand:
    ids = _ids()
    base: dict[str, Any] = {
        "tenant_id": ids["tenant_id"],
        "payment_attempt_id": ids["payment_attempt_id"],
        "grant_id": ids["grant_id"],
        "checkout_id": ids["checkout_id"],
        "checkout_version": 1,
        "content_hash": "hash-" + uuid.uuid4().hex[:16],
        "amount_minor": 149900,
        "currency": "INR",
        "receipt": "rcpt_" + uuid.uuid4().hex[:24],
        "correlation_id": ids["correlation_id"],
    }
    base.update(overrides)
    base.setdefault(
        "notes",
        {
            "tenant_id": base["tenant_id"],
            "checkout_id": base["checkout_id"],
            "payment_attempt_id": base["payment_attempt_id"],
            "checkout_version": str(base["checkout_version"]),
        },
    )
    return CreateOrderCommand(**base)


def make_refund_execute(**overrides: Any) -> RefundExecuteCommand:
    ids = _ids()
    base: dict[str, Any] = {
        "tenant_id": ids["tenant_id"],
        "refund_id": ids["refund_id"],
        "payment_attempt_id": ids["payment_attempt_id"],
        "grant_id": ids["grant_id"],
        "checkout_id": ids["checkout_id"],
        "checkout_version": 2,
        "content_hash": "hash-" + uuid.uuid4().hex[:16],
        "amount_minor": 50000,
        "currency": "INR",
        "idem_key": "rf_" + uuid.uuid4().hex,
        "correlation_id": ids["correlation_id"],
    }
    base.update(overrides)
    return RefundExecuteCommand(**base)


def make_webhook() -> ApplyWebhookEventCommand:
    ids = _ids()
    return ApplyWebhookEventCommand(
        tenant_id=ids["tenant_id"], inbox_id=ids["inbox_id"], correlation_id=ids["correlation_id"]
    )


def make_reconcile_payment() -> ReconcilePaymentCommand:
    ids = _ids()
    return ReconcilePaymentCommand(
        tenant_id=ids["tenant_id"],
        payment_attempt_id=ids["payment_attempt_id"],
        reason="create_order_unknown",
        attempt_number=1,
        correlation_id=ids["correlation_id"],
    )


def make_reconcile_refund() -> ReconcileRefundCommand:
    ids = _ids()
    return ReconcileRefundCommand(
        tenant_id=ids["tenant_id"],
        refund_id=ids["refund_id"],
        payment_attempt_id=ids["payment_attempt_id"],
        reason="refund_unknown",
        attempt_number=3,
        correlation_id=ids["correlation_id"],
    )


def every_command() -> list[AnyCommand]:
    return [
        make_create_order(),
        make_webhook(),
        make_reconcile_payment(),
        make_refund_execute(),
        make_reconcile_refund(),
    ]


# ------------------------------------------------------------------ pure: round trip


class TestRoundTrip:
    @pytest.mark.parametrize("command", every_command(), ids=lambda c: type(c).__name__)
    def test_to_payload_then_from_payload_is_identity(self, command: AnyCommand) -> None:
        payload = command.to_payload()
        assert payload["v"] == COMMAND_VERSION
        rebuilt = type(command).from_payload(payload)
        assert rebuilt == command
        assert parse_command(command.command_type, payload) == command

    @pytest.mark.parametrize("command", every_command(), ids=lambda c: type(c).__name__)
    def test_payload_canonicalizes_and_is_stable(self, command: AnyCommand) -> None:
        """The integer-only JCS profile refuses floats, UUID objects and datetimes, so a
        payload that canonicalizes is one the outbox will accept and hash reproducibly."""
        first = canonicalize(command.to_payload())
        second = canonicalize(type(command).from_payload(command.to_payload()).to_payload())
        assert first == second
        assert isinstance(first, bytes) and first

    @pytest.mark.parametrize("command", every_command(), ids=lambda c: type(c).__name__)
    def test_payload_holds_only_primitives(self, command: AnyCommand) -> None:
        for key, value in command.to_payload().items():
            if isinstance(value, dict):
                assert all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
            else:
                assert type(value) in (str, int), f"{key} is {type(value).__name__}"

    @pytest.mark.parametrize("command", every_command(), ids=lambda c: type(c).__name__)
    def test_command_is_frozen(self, command: AnyCommand) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            command.tenant_id = str(uuid.uuid4())  # type: ignore[misc]

    def test_command_type_values_match_the_kernel_operations(self) -> None:
        """A reviewer should see the same word on the outbox row and the grant row."""
        assert CommandType.PAYMENT_CREATE_ORDER.value == Operation.PAYMENT_CREATE_ORDER.value
        assert CommandType.REFUND_EXECUTE.value == Operation.REFUND_EXECUTE.value
        assert CreateOrderCommand.command_type is CommandType.PAYMENT_CREATE_ORDER
        assert RefundExecuteCommand.command_type is CommandType.REFUND_EXECUTE

    def test_notes_are_immutable_after_construction(self) -> None:
        command = make_create_order()
        with pytest.raises(TypeError):
            command.notes["x"] = "y"  # type: ignore[index]


# ------------------------------------------------------------------ pure: binding


class TestGrantBinding:
    def test_create_order_binding_equals_what_the_kernel_builds(self) -> None:
        command = make_create_order()
        rebuilt = CreateOrderCommand.from_payload(command.to_payload())
        expected = GrantBinding(
            tenant_id=uuid.UUID(command.tenant_id),
            checkout=CheckoutRef(
                checkout_id=uuid.UUID(command.checkout_id),
                version=command.checkout_version,
                content_hash=command.content_hash,
            ),
            payment_attempt_id=uuid.UUID(command.payment_attempt_id),
            operation=Operation.PAYMENT_CREATE_ORDER,
            amount=Money(command.amount_minor, command.currency),
        )
        assert rebuilt.grant_binding() == expected
        assert isinstance(rebuilt.grant_binding(), GrantBinding)

    def test_refund_binding_equals_what_the_kernel_builds(self) -> None:
        command = make_refund_execute()
        rebuilt = RefundExecuteCommand.from_payload(command.to_payload())
        expected = GrantBinding(
            tenant_id=uuid.UUID(command.tenant_id),
            checkout=CheckoutRef(
                checkout_id=uuid.UUID(command.checkout_id),
                version=command.checkout_version,
                content_hash=command.content_hash,
            ),
            payment_attempt_id=uuid.UUID(command.payment_attempt_id),
            operation=Operation.REFUND_EXECUTE,
            amount=Money(command.amount_minor, command.currency),
            refund_id=uuid.UUID(command.refund_id),
        )
        assert rebuilt.grant_binding() == expected

    def test_binding_covers_every_field_consume_grant_compares(self) -> None:
        """Changing any bound field in the payload changes the binding, so a substituted
        command can never bind to the grant of the admitted one."""
        command = make_create_order()
        baseline = command.grant_binding()
        variants = {
            "checkout_version": 2,
            "content_hash": "different",
            "amount_minor": command.amount_minor + 1,
            "currency": "USD",
            "checkout_id": str(uuid7()),
            "payment_attempt_id": str(uuid7()),
            "tenant_id": str(uuid.uuid4()),
        }
        for field, value in variants.items():
            payload = command.to_payload()
            payload[field] = value
            if field in ("tenant_id", "checkout_id", "payment_attempt_id"):
                payload["notes"][field] = value
            assert CreateOrderCommand.from_payload(payload).grant_binding() != baseline, field

    def test_refund_binding_carries_the_refund_id(self) -> None:
        """ADR D10: a refund command for refund B must not bind to refund A's grant, so
        the refund id travels on the payload and into the binding the kernel compares."""
        command = make_refund_execute()
        assert command.to_payload()["refund_id"] == command.refund_id
        assert command.grant_binding().refund_id == uuid.UUID(command.refund_id)
        payload = command.to_payload()
        payload["refund_id"] = str(uuid7())
        assert RefundExecuteCommand.from_payload(payload).grant_binding() != (
            command.grant_binding()
        )


# ------------------------------------------------------------------ pure: refusals


class TestRefusals:
    def test_unknown_command_type_is_refused(self) -> None:
        with pytest.raises(OutboxUsageError, match="unknown command_type"):
            parse_command("HOUSEKEEPING", make_webhook().to_payload())

    def test_command_type_that_is_not_a_string_is_refused(self) -> None:
        with pytest.raises(OutboxUsageError, match="command_type must be str"):
            parse_command(7, make_webhook().to_payload())  # type: ignore[arg-type]

    @pytest.mark.parametrize("version", [COMMAND_VERSION + 1, 0, "1", True, None])
    def test_version_mismatch_is_refused(self, version: object) -> None:
        payload = make_webhook().to_payload()
        payload["v"] = version
        with pytest.raises(OutboxUsageError, match="version"):
            ApplyWebhookEventCommand.from_payload(payload)

    def test_missing_version_is_refused(self) -> None:
        payload = make_webhook().to_payload()
        del payload["v"]
        with pytest.raises(OutboxUsageError, match="version"):
            ApplyWebhookEventCommand.from_payload(payload)

    @pytest.mark.parametrize("command", every_command(), ids=lambda c: type(c).__name__)
    def test_every_missing_key_is_refused(self, command: AnyCommand) -> None:
        for field in dataclasses.fields(command):
            payload = command.to_payload()
            del payload[field.name]
            with pytest.raises(OutboxUsageError, match=f"missing=\\['{field.name}'\\]"):
                type(command).from_payload(payload)

    @pytest.mark.parametrize("command", every_command(), ids=lambda c: type(c).__name__)
    def test_unknown_key_is_refused(self, command: AnyCommand) -> None:
        payload = command.to_payload()
        payload["merchant_override"] = "x"
        with pytest.raises(OutboxUsageError, match="unknown=\\['merchant_override'\\]"):
            type(command).from_payload(payload)

    def test_payload_that_is_not_a_mapping_is_refused(self) -> None:
        with pytest.raises(OutboxUsageError, match="must be a mapping"):
            parse_command(CommandType.APPLY_WEBHOOK_EVENT, ["v", 1])  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("amount_minor", 1499.0),
            ("amount_minor", "149900"),
            ("amount_minor", True),
            ("amount_minor", 0),
            ("amount_minor", -1),
            ("currency", "inr"),
            ("currency", "XXX"),
            ("currency", 356),
            ("checkout_version", 0),
            ("checkout_version", "1"),
            ("checkout_version", 1.0),
            ("tenant_id", "not-a-uuid"),
            ("tenant_id", 12345),
            ("grant_id", str(uuid7()).upper()),
            ("payment_attempt_id", uuid7().hex),
            ("content_hash", ""),
            ("content_hash", None),
            ("receipt", "r" * 41),
            ("notes", "tenant_id=x"),
            ("notes", {"tenant_id": 1}),
            ("correlation_id", uuid7()),
        ],
    )
    def test_wrong_types_and_shapes_are_refused(self, field: str, value: object) -> None:
        payload = make_create_order().to_payload()
        payload[field] = value
        with pytest.raises(OutboxUsageError):
            CreateOrderCommand.from_payload(payload)

    def test_direct_construction_is_held_to_the_same_rules(self) -> None:
        """A command built by the API, not parsed by the worker, must also be valid,
        otherwise to_payload could store something from_payload refuses."""
        with pytest.raises(OutboxUsageError, match="amount_minor"):
            make_create_order(amount_minor=1.5)
        with pytest.raises(OutboxUsageError, match="attempt_number"):
            ReconcilePaymentCommand(
                tenant_id=str(uuid.uuid4()),
                payment_attempt_id=str(uuid7()),
                reason="x",
                attempt_number=0,
                correlation_id=str(uuid7()),
            )

    def test_notes_must_agree_with_the_command(self) -> None:
        """The notes are what a Razorpay dashboard shows a reviewer; a note naming a
        different attempt than the command it rode on is evidence pointing the wrong way."""
        command = make_create_order()
        notes = dict(command.notes)
        notes["payment_attempt_id"] = str(uuid7())
        with pytest.raises(OutboxUsageError, match="notes\\['payment_attempt_id'\\]"):
            make_create_order(
                tenant_id=command.tenant_id,
                checkout_id=command.checkout_id,
                payment_attempt_id=command.payment_attempt_id,
                notes=notes,
            )
        del notes["payment_attempt_id"]
        with pytest.raises(OutboxUsageError, match="notes\\['payment_attempt_id'\\]"):
            make_create_order(
                tenant_id=command.tenant_id,
                checkout_id=command.checkout_id,
                payment_attempt_id=command.payment_attempt_id,
                notes=notes,
            )

    def test_notes_respect_razorpay_limits(self) -> None:
        command = make_create_order()
        too_many = dict(command.notes) | {f"k{i}": "v" for i in range(15)}
        with pytest.raises(OutboxUsageError, match="at most 15"):
            make_create_order(
                tenant_id=command.tenant_id,
                checkout_id=command.checkout_id,
                payment_attempt_id=command.payment_attempt_id,
                notes=too_many,
            )
        too_long = dict(command.notes) | {"memo": "m" * 257}
        with pytest.raises(OutboxUsageError, match="256"):
            make_create_order(
                tenant_id=command.tenant_id,
                checkout_id=command.checkout_id,
                payment_attempt_id=command.payment_attempt_id,
                notes=too_long,
            )

    def test_idempotency_key_in_the_envelope_is_tolerated_and_readable(self) -> None:
        payload = make_webhook().to_payload()
        assert idempotency_key_of(payload) is None
        payload["idempotency_key"] = "idem-1"
        ApplyWebhookEventCommand.from_payload(payload)
        assert idempotency_key_of(payload) == "idem-1"
        payload["idempotency_key"] = 1
        with pytest.raises(OutboxUsageError, match="idempotency_key"):
            ApplyWebhookEventCommand.from_payload(payload)

    def test_leased_row_whose_payload_names_another_tenant_is_refused(self) -> None:
        command = make_webhook()
        leased = LeasedCommand(
            command_id=uuid7(),
            tenant_id=uuid.uuid4(),
            command_type=command.command_type,
            payload=command.to_payload(),
            attempts=1,
            lease_token=None,  # type: ignore[arg-type]
            worker_id="w1",
            correlation_id=uuid.UUID(command.correlation_id),
        )
        with pytest.raises(OutboxUsageError, match="names tenant"):
            parse_leased_command(leased)


# ---------------------------------------------------------------------- database

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)
SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")


def _require_db(url: str) -> Engine:
    engine = create_engine(url, future=True, pool_size=2, max_overflow=0)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable for command tests: {exc}")
    return engine


@pytest.fixture(scope="module")
def kernel_engine() -> Engine:
    engine = _require_db(KERNEL_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert row.rolsuper is False, "command tests must not run as a superuser"
    assert row.rolbypassrls is False, "command tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="module")
def admin_engine() -> Engine:
    return _require_db(ADMIN_URL)


@pytest.fixture
def tenant(admin_engine: Engine) -> Iterator[uuid.UUID]:
    tenant_id = uuid.uuid4()
    slug = f"dwc-{tenant_id.hex[:8]}"
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, home_region) "
                "VALUES (:id, :slug, :name, 'asia-south1')"
            ),
            {"id": tenant_id, "slug": slug, "name": slug},
        )
    yield tenant_id
    with admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        for table in ("outbox_events", "execution_grants", "payment_attempts"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})  # noqa: S608
        conn.execute(SET_TENANT, {"t": None})
        conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})


@pytest.fixture
def session(kernel_engine: Engine) -> Iterator[Session]:
    s = sessionmaker(bind=kernel_engine, expire_on_commit=False, future=True)()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def seed_attempt(admin: Engine, tenant_id: uuid.UUID, command: CreateOrderCommand) -> None:
    """One CREATED payment attempt matching the command, seeded as the owner because the
    application roles cannot write outside a kernel transaction in this test."""
    with admin.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO payment_attempts (id, tenant_id, checkout_id, checkout_version,"
                " status, amount_minor, currency, receipt) "
                "VALUES (:id, :t, :c, :v, 'CREATED', :a, :cur, :r)"
            ),
            {
                "id": uuid.UUID(command.payment_attempt_id),
                "t": tenant_id,
                "c": uuid.UUID(command.checkout_id),
                "v": command.checkout_version,
                "a": command.amount_minor,
                "cur": command.currency,
                "r": command.receipt,
            },
        )


@pytest.mark.db
class TestEnqueueCommand:
    def test_enqueued_row_parses_back_into_the_same_command(
        self, session: Session, tenant: uuid.UUID, admin_engine: Engine
    ) -> None:
        command = make_create_order(tenant_id=str(tenant))
        with session.begin():
            set_tenant(session, tenant)
            row = enqueue_command(session, command, idempotency_key="idem-abc")
        assert row.command_type == CommandType.PAYMENT_CREATE_ORDER
        assert row.correlation_id == uuid.UUID(command.correlation_id)
        assert row.tenant_id == tenant

        with admin_engine.begin() as conn:
            stored = conn.execute(
                text("SELECT command_type, payload FROM outbox_events WHERE id = :i"),
                {"i": row.command_id},
            ).one()
        assert parse_command(stored.command_type, stored.payload) == command
        assert idempotency_key_of(stored.payload) == "idem-abc"

        with session.begin():
            set_tenant(session, tenant)
            (leased,) = lease(session, worker_id="w1", limit=1)
        assert leased.command_id == row.command_id
        assert parse_leased_command(leased) == command

    def test_every_command_type_round_trips_through_the_table(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        commands: list[AnyCommand] = [
            dataclasses.replace(c, tenant_id=str(tenant))
            for c in (make_webhook(), make_reconcile_payment(), make_reconcile_refund())
        ]
        refund = make_refund_execute(tenant_id=str(tenant))
        create = make_create_order(tenant_id=str(tenant))
        commands += [refund, create]
        with session.begin():
            set_tenant(session, tenant)
            for command in commands:
                enqueue_command(session, command, idempotency_key=None)
        with session.begin():
            set_tenant(session, tenant)
            leased = lease(session, worker_id="w1", limit=10)
        parsed = {type(parse_leased_command(row)) for row in leased}
        assert parsed == {type(c) for c in commands}
        for row in leased:
            assert idempotency_key_of(row.payload) is None

    def test_command_naming_another_tenant_is_refused_before_the_insert(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        command = make_webhook()  # random tenant, not the bound one
        with pytest.raises(OutboxUsageError, match="transaction is bound to"):
            with session.begin():
                set_tenant(session, tenant)
                enqueue_command(session, command, idempotency_key=None)

    def test_enqueue_requires_a_bound_tenant(self, session: Session, tenant: uuid.UUID) -> None:
        from platform_db import TenantContextError

        command = make_webhook()
        command = dataclasses.replace(command, tenant_id=str(tenant))
        with pytest.raises(TenantContextError):
            with session.begin():
                enqueue_command(session, command, idempotency_key=None)


@pytest.mark.db
class TestBindingConsumesTheKernelsGrant:
    def test_binding_from_the_stored_payload_consumes_the_grant_once(
        self, session: Session, tenant: uuid.UUID, admin_engine: Engine, kernel_engine: Engine
    ) -> None:
        """End to end: the kernel issues a grant for an attempt, the API enqueues the
        command, the worker parses the row and consumes with a binding built from the
        payload alone. Exactly that binding is accepted; the second delivery is refused."""
        draft = make_create_order(tenant_id=str(tenant))
        seed_attempt(admin_engine, tenant, draft)

        with session.begin():
            set_tenant(session, tenant)
            grant = issue_grant(
                session,
                tenant=tenant,
                checkout_ref=CheckoutRef(
                    checkout_id=uuid.UUID(draft.checkout_id),
                    version=draft.checkout_version,
                    content_hash=draft.content_hash,
                ),
                payment_attempt_id=uuid.UUID(draft.payment_attempt_id),
                operation=Operation.PAYMENT_CREATE_ORDER,
                amount=Money(draft.amount_minor, draft.currency),
                kernel_decision_id=uuid7(),
                ttl_seconds=300,
            )
            command = dataclasses.replace(draft, grant_id=str(grant.id))
            enqueue_command(session, command, idempotency_key=None)

        with session.begin():
            set_tenant(session, tenant)
            (leased,) = lease(session, worker_id="w1", limit=1)
        parsed = parse_leased_command(leased)
        assert isinstance(parsed, CreateOrderCommand)

        worker = sessionmaker(bind=kernel_engine, expire_on_commit=False, future=True)()
        try:
            with worker.begin():
                set_tenant(worker, tenant)
                consumed = consume_grant(worker, uuid.UUID(parsed.grant_id), parsed.grant_binding())
                assert consumed.status == GrantStatus.CONSUMED
            with pytest.raises(GrantAlreadyConsumedError):
                with worker.begin():
                    set_tenant(worker, tenant)
                    consume_grant(worker, uuid.UUID(parsed.grant_id), parsed.grant_binding())
        finally:
            worker.rollback()
            worker.close()

    def test_a_substituted_payload_does_not_bind(
        self, session: Session, tenant: uuid.UUID, admin_engine: Engine
    ) -> None:
        draft = make_create_order(tenant_id=str(tenant))
        seed_attempt(admin_engine, tenant, draft)
        with session.begin():
            set_tenant(session, tenant)
            grant = issue_grant(
                session,
                tenant=tenant,
                checkout_ref=CheckoutRef(
                    checkout_id=uuid.UUID(draft.checkout_id),
                    version=draft.checkout_version,
                    content_hash=draft.content_hash,
                ),
                payment_attempt_id=uuid.UUID(draft.payment_attempt_id),
                operation=Operation.PAYMENT_CREATE_ORDER,
                amount=Money(draft.amount_minor, draft.currency),
                kernel_decision_id=uuid7(),
                ttl_seconds=300,
            )
            grant_id = grant.id
        payload = dataclasses.replace(draft, grant_id=str(grant_id)).to_payload()
        payload["amount_minor"] = draft.amount_minor + 100
        tampered = CreateOrderCommand.from_payload(payload)
        with pytest.raises(GrantBindingMismatchError) as caught:
            with session.begin():
                set_tenant(session, tenant)
                consume_grant(session, uuid.UUID(tampered.grant_id), tampered.grant_binding())
        assert [d.field_path for d in caught.value.deltas] == ["amount_minor"]
        with admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant)})
            status = conn.execute(
                text("SELECT status FROM execution_grants WHERE id = :i"), {"i": grant_id}
            ).scalar_one()
        assert status == GrantStatus.ISSUED
