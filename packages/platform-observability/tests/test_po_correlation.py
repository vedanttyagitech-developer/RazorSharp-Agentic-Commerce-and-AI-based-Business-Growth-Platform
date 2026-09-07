"""The correlation id has to survive an ``await``, a task, and a raised exception.

If it does not, every claim this package makes about joining a log line to an audit row
collapses -- and it collapses silently, producing lines with the wrong id rather than lines
with no id, which is worse.

``asyncio_mode`` is not set repository-wide, so async tests carry ``pytest.mark.asyncio``
explicitly, matching ``packages/agent-runtime/tests``.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterator
from contextvars import copy_context

import pytest
from platform_observability import (
    Scope,
    bind_scope,
    correlation_id,
    correlation_id_from_header,
    current_scope,
    current_tenant,
    new_correlation_id,
    scope_fields,
)


class TestBinding:
    def test_nothing_is_bound_by_default(self) -> None:
        """``None`` rather than a minted id: a line emitted outside any request should say
        so, and inventing an id that joins to nothing would hide it."""
        assert current_scope() is None
        assert correlation_id() is None
        assert scope_fields() == {}

    def test_a_bound_scope_is_visible_and_is_restored_on_exit(self) -> None:
        with bind_scope("abc", tenant_id="t1", actor_type="BUYER") as scope:
            assert scope == Scope(correlation_id="abc", tenant_id="t1", actor_type="BUYER")
            assert correlation_id() == "abc"
            assert current_tenant() == "t1"
        assert current_scope() is None

    def test_an_omitted_field_inherits_from_the_bound_scope(self) -> None:
        """A middleware establishes the id; a service deeper in adds the tenant without
        having to know it."""
        with bind_scope("abc", tenant_id="t1"), bind_scope(actor_type="WORKER"):
            assert correlation_id() == "abc"
            assert current_tenant() == "t1"
            scope = current_scope()
            assert scope is not None
            assert scope.actor_type == "WORKER"

    def test_nested_binds_unwind_in_order(self) -> None:
        with bind_scope("outer"):
            with bind_scope("inner"):
                assert correlation_id() == "inner"
            assert correlation_id() == "outer"
        assert correlation_id() is None

    def test_an_exception_still_restores_the_previous_scope(self) -> None:
        """Restoration is by token, so an exception skipping past several nested binds at
        once still leaves the context where it was found."""
        with bind_scope("outer"):
            with pytest.raises(RuntimeError), bind_scope("inner"):
                raise RuntimeError("boom")
            assert correlation_id() == "outer"

    def test_uuids_are_accepted_and_normalised_to_text(self) -> None:
        identifier = uuid.uuid7()
        with bind_scope(identifier, tenant_id=uuid.uuid7()) as scope:
            assert scope.correlation_id == str(identifier)
            assert isinstance(scope.tenant_id, str)

    def test_an_unbound_bind_mints_an_id(self) -> None:
        with bind_scope() as scope:
            assert uuid.UUID(scope.correlation_id).version == 7

    def test_scope_fields_omits_what_is_unset(self) -> None:
        with bind_scope("abc"):
            assert scope_fields() == {"correlation_id": "abc"}
        with bind_scope("abc", tenant_id="t1", actor_type="AGENT", causation_id="cause"):
            assert scope_fields() == {
                "correlation_id": "abc",
                "tenant_id": "t1",
                "actor_type": "AGENT",
                "causation_id": "cause",
            }


class TestHeaderResolution:
    def test_a_well_formed_header_is_kept(self) -> None:
        supplied = str(uuid.uuid7())
        assert correlation_id_from_header(supplied) == supplied

    @pytest.mark.parametrize("raw", [None, "", "not-a-uuid", "../../etc/passwd", "1" * 200])
    def test_anything_else_mints_one_rather_than_refusing(self, raw: str | None) -> None:
        """Mirrors ``commerce_api.deps._correlation_id``: a malformed correlation id is a
        client bug and is not grounds to refuse a payment. The header was never authority.
        """
        minted = correlation_id_from_header(raw)
        assert uuid.UUID(minted).version == 7

    def test_minted_ids_are_time_ordered(self) -> None:
        """UUIDv7, so ids sort by when their journey began -- which is what makes an index
        on this column in a log store worth having."""
        first, second = new_correlation_id(), new_correlation_id()
        assert first < second


class TestAcrossAnAwait:
    @pytest.mark.asyncio
    async def test_the_id_survives_an_await(self) -> None:
        async def inner() -> str | None:
            await asyncio.sleep(0)
            return correlation_id()

        with bind_scope("abc"):
            assert await inner() == "abc"
            await asyncio.sleep(0)
            assert correlation_id() == "abc"

    @pytest.mark.asyncio
    async def test_the_id_survives_several_frames_and_awaits(self) -> None:
        async def leaf() -> str | None:
            await asyncio.sleep(0)
            return correlation_id()

        async def middle() -> str | None:
            await asyncio.sleep(0)
            return await leaf()

        with bind_scope("abc", tenant_id="t1"):
            assert await middle() == "abc"

    @pytest.mark.asyncio
    async def test_a_spawned_task_inherits_the_scope(self) -> None:
        """``create_task`` copies the context, so a request fanning out to three calls gets
        three tasks reporting the same id."""

        async def leaf() -> str | None:
            await asyncio.sleep(0)
            return correlation_id()

        with bind_scope("abc"):
            results = await asyncio.gather(*(asyncio.create_task(leaf()) for _ in range(3)))
        assert results == ["abc", "abc", "abc"]

    @pytest.mark.asyncio
    async def test_a_task_cannot_corrupt_its_parent_or_its_siblings(self) -> None:
        """The context is *copied*, so a task that rebinds affects only itself. Two
        concurrent checkouts under one session cannot end up sharing an id."""

        async def rebind(new_id: str) -> str | None:
            with bind_scope(new_id):
                await asyncio.sleep(0)
                return correlation_id()

        with bind_scope("parent"):
            results = await asyncio.gather(
                asyncio.create_task(rebind("child-a")),
                asyncio.create_task(rebind("child-b")),
            )
            assert sorted(results) == ["child-a", "child-b"]  # type: ignore[list-item]
            assert correlation_id() == "parent"

    @pytest.mark.asyncio
    async def test_concurrent_tasks_keep_their_own_tenants(self) -> None:
        async def observe(tenant: str) -> str | None:
            with bind_scope(tenant_id=tenant):
                await asyncio.sleep(0)
                return current_tenant()

        with bind_scope("abc"):
            assert await asyncio.gather(
                asyncio.create_task(observe("acme")),
                asyncio.create_task(observe("globex")),
            ) == ["acme", "globex"]


class TestUnwindingInAForeignContext:
    """The failure that turns a 409 into a 500 if ``bind_scope`` trusts its token.

    ``ContextVar.reset`` refuses a token created in a different ``Context``. FastAPI
    produces exactly that arrangement for a *synchronous* ``yield`` dependency: it runs the
    enter and the exit through anyio's thread pool under two different copied contexts. If
    the ``ValueError`` escaped, it would escape from the dependency's teardown -- replacing
    a deliberate kernel refusal with an internal server error, which is the one thing this
    package must never be able to do.
    """

    def test_exiting_in_a_different_context_does_not_raise(self) -> None:
        manager = bind_scope("abc", tenant_id="t1")
        entering, leaving = copy_context(), copy_context()

        entering.run(manager.__enter__)
        leaving.run(manager.__exit__, None, None, None)  # must not raise

    def test_the_value_is_restored_in_the_context_being_unwound(self) -> None:
        manager = bind_scope("abc")
        entering, leaving = copy_context(), copy_context()
        entering.run(manager.__enter__)
        leaving.run(manager.__exit__, None, None, None)

        assert leaving.run(correlation_id) is None
        assert correlation_id() is None

    def test_a_sync_generator_teardown_survives_the_exception_thrown_into_it(self) -> None:
        """What FastAPI actually does to a ``yield`` dependency when the endpoint raises:
        it throws the exception into the generator. The caller's exception must come back
        out, and not be replaced by a ``ValueError`` about a token."""

        def dependency() -> Iterator[None]:
            with bind_scope(tenant_id="t1"):
                yield

        generator = dependency()
        entering, leaving = copy_context(), copy_context()
        entering.run(next, generator)

        def teardown() -> None:
            with pytest.raises(RouterRefusalError):
                generator.throw(RouterRefusalError())

        leaving.run(teardown)


class RouterRefusalError(Exception):
    """Stands in for the ``HTTPException`` a router raises to refuse a request."""


class TestAcrossAThread:
    def test_a_thread_does_not_inherit_the_scope(self) -> None:
        """Honest rather than convenient. The Action Executor binds its own scope per leased
        command; inheriting an HTTP request's id there would be a lie about causation.
        """
        seen: list[str | None] = []

        def work() -> None:
            seen.append(correlation_id())

        with bind_scope("abc"):
            thread = threading.Thread(target=work)
            thread.start()
            thread.join()

        assert seen == [None]
