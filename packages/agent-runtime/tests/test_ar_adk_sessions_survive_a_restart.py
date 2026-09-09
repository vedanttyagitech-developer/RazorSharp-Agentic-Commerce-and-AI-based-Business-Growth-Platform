"""A conversation outlives the process that was having it.

THE DEFECT
----------
``AdkSpecialistRunner`` held an ``InMemorySessionService``: a plain nested dict with no
TTL, no cap and no eviction, and nothing in this codebase calls ``delete_session``. Two
consequences, and the second is the one nobody notices until production.

*The conversation dies with the process.* Restart the API and every buyer mid-conversation
loses the thread. What survives is only what the client re-sends or the database already
holds -- the cart id comes back on each request and the checkout is in Postgres -- so
"what is in my cart" still works and "add the milk we just talked about" does not. It is
the half that makes a copilot feel like a copilot.

*And it grows forever.* One entry per (bearer session x specialist), never released.

WHAT THIS ASSERTS
-----------------
Not that a particular backend is configured -- that is deployment. It asserts the seam:
when a session store is named, the runner uses it, and a **second runner over the same
store reads what the first one wrote**. A fresh runner instance is exactly what a restart
looks like from the store's side, so that is what the test builds.

The store here is SQLite in a temp file rather than Postgres, because what is under test is
the wiring, not the dialect. The production URL points at ``commerce_dev_adk``: the ADK's
own database, deliberately outside the commerce one. Its tables (``sessions``, ``events``,
``app_states``, ``user_states``) carry no ``tenant_id`` and no RLS, while every tenant table
in the commerce database carries both, and dropping un-tenanted tables into an RLS-governed
schema owned by alembic is the boundary this separation exists to keep.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_runtime.runtime_adk.adapter import (
    SESSION_DB_URL_ENV,
    AdkSpecialistRunner,
    build_session_service,
)
from google.adk.sessions import DatabaseSessionService, InMemorySessionService


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'adk.sqlite3'}"


def test_no_session_store_named_keeps_the_in_memory_one() -> None:
    """The default is unchanged, so an offline test and a laptop still run with nothing set."""
    assert isinstance(build_session_service(None), InMemorySessionService)
    assert isinstance(build_session_service(""), InMemorySessionService)
    assert isinstance(build_session_service("   "), InMemorySessionService)


def test_a_named_store_is_used(tmp_path: Path) -> None:
    assert isinstance(build_session_service(_url(tmp_path)), DatabaseSessionService)


def test_the_runner_honours_the_named_store(tmp_path: Path) -> None:
    """The seam is on the runner, not only on the helper. Wiring one and not the other is
    how a fix lands in a codebase and changes nothing that runs."""
    runner = AdkSpecialistRunner(session_db_url=_url(tmp_path))
    assert isinstance(runner.sessions, DatabaseSessionService)


@pytest.mark.asyncio
async def test_a_conversation_survives_the_process_that_started_it(tmp_path: Path) -> None:
    """The whole point: a second runner reads the first one's session.

    Two runner instances over one store is what a restart is. With the in-memory service
    the second one starts empty and the buyer is talking to a stranger.
    """
    url = _url(tmp_path)
    before = AdkSpecialistRunner(session_db_url=url)
    await before.sessions.create_session(
        app_name=before.app_name,
        user_id="buyer-16634f28f1a5",
        session_id="session:01a087ba/razorai/shopping",
        state={"cart_id": "01a087ba-0000-7000-8000-000000000000"},
    )

    after = AdkSpecialistRunner(session_db_url=url)
    recovered = await after.sessions.get_session(
        app_name=after.app_name,
        user_id="buyer-16634f28f1a5",
        session_id="session:01a087ba/razorai/shopping",
    )
    assert recovered is not None, "the conversation did not survive a restart"
    assert recovered.state["cart_id"] == "01a087ba-0000-7000-8000-000000000000"


@pytest.mark.asyncio
async def test_the_in_memory_store_is_what_loses_it() -> None:
    """The RED direction, kept: proof the test above is measuring something.

    A guard that would pass on the old code is not a guard. This one asserts the defect
    itself -- two in-memory runners share nothing -- so if the wiring above is ever undone,
    the pair disagrees instead of both quietly passing.
    """
    before = AdkSpecialistRunner()
    await before.sessions.create_session(
        app_name=before.app_name, user_id="u", session_id="s", state={"cart_id": "c"}
    )
    after = AdkSpecialistRunner()
    assert (
        await after.sessions.get_session(app_name=after.app_name, user_id="u", session_id="s")
    ) is None


def test_the_root_conftest_names_the_same_variable() -> None:
    """The repository-wide isolation fixture spells this variable rather than importing it.

    That is deliberate -- a policy file that depends on a package can be taken down by that
    package failing to import -- but a spelling nobody checks is a spelling that drifts, and
    the drift is silent: the fixture would clear a variable nothing reads while every suite
    quietly talked to a real database again.
    """
    root = Path(__file__).resolve().parents[3] / "conftest.py"
    assert root.is_file(), f"expected the repository conftest at {root}"
    assert f'SESSION_DB_URL_ENV = "{SESSION_DB_URL_ENV}"' in root.read_text(), (
        "conftest.py's SESSION_DB_URL_ENV no longer matches the adapter's"
    )
