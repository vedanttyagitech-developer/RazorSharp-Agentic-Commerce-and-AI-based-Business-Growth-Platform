"""Every path that admits a checkout must take the sold units off the shelf.

WHY THIS FILE EXISTS
--------------------
The oversell guard changed hands. ``transaction_kernel.reservations`` defends units that
are *promised and not yet sold*, and it stops the instant admission consumes the hold --
because at that instant the sale becomes the caller's to record in ``inventory_movements``.
Counting the consumed hold as well would subtract the same unit twice.

That is the right division: inventory is the merchant's domain, and the kernel's is
transactions and consent. It leaves one obligation, and it is a real one: **a caller that
admits a checkout and does not record its sale will oversell, and nothing in the kernel can
stop it.** ``reserve``'s contract says so, and until now that was all it was -- a sentence,
and a check somebody did by hand while making the change.

So this file makes it a check the suite does. Two halves, because neither alone is enough.

**The census** reads the source and lists every call to the kernel's ``admit``. A new one
fails the test and the author has to come here, write down how their path records its sale,
and add it to the list. A tripwire, not a proof -- it cannot tell whether a listed path is
correct.

**The invariant** is the proof, and it is a property of the data rather than of the text:
after driving each admission path, no reservation is CONSUMED without a ``SOLD`` movement
for the same checkout and version. That cannot be fooled by a refactor, an alias or a
helper, because it never looks at the code.

The census catches the path nobody thought about. The invariant catches the path somebody
thought about and got wrong. Overselling needs only one of those to happen.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from conftest import MintedSession

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"
#: The `packages/` directory: this file is at `packages/commerce-api/tests/`.
PACKAGES = Path(__file__).resolve().parents[2]

#: Every call site of the kernel's ``admit``, and how that path records its sale.
#:
#: Adding a row here is not a formality. If the answer to "how does it record its sale" is
#: "it does not", the path oversells, and the fix is in the path rather than in this dict.
EXPECTED_ADMIT_CALLERS: dict[str, str] = {
    "commerce_api/services/admission_service.py": (
        "the buyer's own submit and approve-and-pay. `_spend_approval_and_enqueue` calls "
        "`inventory.record_sale` in the same transaction, right after the kernel consumes "
        "the hold."
    ),
    "commerce_api/services/scenario_service.py": (
        "the duplicate-submit demonstration, which races two real admissions in two real "
        "transactions. Exactly one wins and consumes a hold; `duplicate_submit` records "
        "that winner's sale in the request's own transaction, because the racing sessions "
        "have already closed theirs."
    ),
}


def _kernel_admit_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The names in this module that mean the kernel's ``admit``, and nothing else.

    Resolved from the imports rather than matched on the word, because ``admit`` is also
    the name of the ACP protocol's own admission -- a different function, on a different
    noun, that consumes no stock. A test that counted it would report a defect that is not
    there, and a test that excluded it by filename would stop working the day somebody
    moved the file.
    """
    bare: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "transaction_kernel"
        ):
            bare |= {alias.asname or alias.name for alias in node.names if alias.name == "admit"}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "transaction_kernel" or alias.name.startswith(
                    "transaction_kernel."
                ):
                    modules.add(alias.asname or alias.name.split(".")[0])
    return bare, modules


def _admit_call_sites() -> dict[str, list[int]]:
    """Every place outside the kernel that calls the kernel's ``admit``."""
    found: dict[str, list[int]] = {}
    for path in sorted(PACKAGES.glob("*/src/**/*.py")):
        # The kernel's own calls are the definition and its internals, not callers of it.
        if "transaction-kernel" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a file that does not parse fails elsewhere
            continue
        bare, modules = _kernel_admit_names(tree)
        if not bare and not modules:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            hit = (isinstance(func, ast.Name) and func.id in bare) or (
                isinstance(func, ast.Attribute)
                and func.attr == "admit"
                and isinstance(func.value, ast.Name)
                and func.value.id in modules
            )
            if hit:
                key = str(path).split("/src/", 1)[1]
                found.setdefault(key, []).append(node.lineno)
    return found


def _consumed_without_a_sale(engine: Engine, tenant_id: uuid.UUID) -> list[Any]:
    """Reservations the kernel has spent that no movement accounts for.

    This is the whole obligation, in one query. A CONSUMED hold has stopped defending its
    units -- so if nothing took them off the shelf, they are on it twice.
    """
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        return list(
            conn.execute(
                text(
                    "SELECT r.checkout_id, r.checkout_version FROM reservations r "
                    "WHERE r.tenant_id = :t AND r.status = 'CONSUMED' "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM inventory_movements m "
                    "     WHERE m.tenant_id = r.tenant_id "
                    "       AND m.checkout_id = r.checkout_id "
                    "       AND m.checkout_version = r.checkout_version "
                    "       AND m.kind = 'SOLD')"
                ),
                {"t": tenant_id},
            ).all()
        )


def _headers(**extra: str) -> dict[str, str]:
    return {"Idempotency-Key": f"k-{uuid.uuid4().hex}", **extra}


def _approved_checkout(client: TestClient, *, quantity: int = 2) -> dict[str, Any]:
    """A checkout with the buyer's approval recorded, ready to be submitted."""
    cart = client.post("/v1/carts", headers=_headers())
    assert cart.status_code == 201, cart.text
    cart_id = cart.json()["cart_id"]
    assert (
        client.put(
            f"/v1/carts/{cart_id}/lines/{MILK}", json={"quantity": quantity}, headers=_headers()
        ).status_code
        == 200
    )
    opened = client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers())
    assert opened.status_code == 201, opened.text
    card: dict[str, Any] = opened.json()
    approved = client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve",
        json={
            "content_hash": card["content_hash"],
            "amount_minor": card["amount_minor"],
            "currency": card["currency"],
        },
        headers=_headers(),
    )
    assert approved.status_code == 200, approved.text
    return card


class TestTheCensus:
    def test_every_admission_call_site_is_one_that_records_its_sale(self) -> None:
        found = _admit_call_sites()

        missing = sorted(set(EXPECTED_ADMIT_CALLERS) - set(found))
        surprising = sorted(set(found) - set(EXPECTED_ADMIT_CALLERS))

        assert not surprising, (
            "a new path admits checkouts: "
            f"{ {name: found[name] for name in surprising} }. Admission consumes the stock "
            "hold, and from that moment nothing but `inventory.record_sale` keeps those "
            "units off the shelf -- the kernel's guard counts ACTIVE holds only. Record the "
            "sale in the same transaction as the admission, then add the path to "
            "EXPECTED_ADMIT_CALLERS with how it does so."
        )
        assert not missing, (
            f"{missing} no longer calls the kernel's admit. If the path is gone, delete its "
            "row here; if it merely moved, this test has stopped watching it."
        )

    def test_the_acp_admission_is_not_mistaken_for_this_one(self) -> None:
        """`commerce_protocols.acp.admit` shares a name and nothing else.

        It admits a *request* against a client credential; it consumes no stock and opens
        no reservation. Pinned because the obvious implementation of the test above -- grep
        for `admit(` -- reports it, and a guard that cries wolf is one people switch off.
        """
        assert "commerce_api/routers/acp.py" not in _admit_call_sites()


class TestTheInvariant:
    def test_approve_and_pay_leaves_no_consumed_hold_unaccounted(
        self,
        auth_client: TestClient,
        demo_session: MintedSession,
        capi_admin_engine: Engine,
    ) -> None:
        card = _approved_checkout(auth_client)
        paid = auth_client.post(
            f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/submit",
            json={},
            headers=_headers(),
        )
        assert paid.status_code == 200, paid.text
        assert paid.json()["allowed"] is True, paid.text

        assert _consumed_without_a_sale(capi_admin_engine, demo_session.tenant_id) == []

    def test_the_one_shot_approve_and_pay_path_too(
        self,
        auth_client: TestClient,
        demo_session: MintedSession,
        capi_admin_engine: Engine,
    ) -> None:
        """The other buyer-facing entry point, which admits without a separate submit."""
        cart = auth_client.post("/v1/carts", headers=_headers())
        cart_id = cart.json()["cart_id"]
        auth_client.put(
            f"/v1/carts/{cart_id}/lines/{MILK}", json={"quantity": 1}, headers=_headers()
        )
        card = auth_client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers()).json()
        paid = auth_client.post(
            f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve-and-pay",
            json={
                "content_hash": card["content_hash"],
                "amount_minor": card["amount_minor"],
                "currency": card["currency"],
            },
            headers=_headers(),
        )
        assert paid.status_code == 200, paid.text
        assert paid.json()["allowed"] is True, paid.text

        assert _consumed_without_a_sale(capi_admin_engine, demo_session.tenant_id) == []

    def test_the_duplicate_submit_race_accounts_for_its_winner(
        self,
        auth_client: TestClient,
        demo_session: MintedSession,
        capi_admin_engine: Engine,
        scenario_headers: dict[str, str],
    ) -> None:
        """The path that was a real gap, and the reason the census exists.

        Two admissions race in two transactions of their own; exactly one wins and consumes
        the hold. It went through the kernel directly rather than through the buyer's
        service, so it recorded nothing -- and no test would have noticed, because until the
        guard stopped counting consumed holds the units were still defended.
        """
        card = _approved_checkout(auth_client)
        raced = auth_client.post(
            "/v1/scenario/duplicate-submit",
            json={"checkout_id": card["checkout_id"], "version": card["version"]},
            headers=_headers(**scenario_headers),
        )
        assert raced.status_code in {200, 201}, raced.text

        assert _consumed_without_a_sale(capi_admin_engine, demo_session.tenant_id) == []
