"""A merchant is a party the platform can authenticate, and the doors that stay shut.

Adding an actor type is the kind of change that looks like one line and is not. Every place
that refuses somebody had to be read, because a refusal written as "not the model" admits
every actor invented afterwards, silently, on the day it is invented. Four such places
existed, and this file is the record of what each of them decided.

The one that would have hurt: the Safe Mode kill switch and the outbox revive both refused
``AGENT`` and nothing else. A merchant session would have passed both -- a shopkeeper able
to stop delegated payments across the tenant, and to re-drive a buried money command. Both
are allowlists now, so the next actor is refused until somebody writes down why it should
not be.

The one that was subtler: ``api_sessions.buyer_ref`` was NOT NULL, and every session
without a real buyer got a minted placeholder. A merchant row carrying one would have
satisfied ``is_buyer_principal``, which asks only whether a reference is present, and
therefore failed ``is_merchant_principal`` -- routing a merchant to the buyer's copilot.
The column is nullable now, and a CHECK ties the two columns together in both directions,
so the rule lives where it cannot be forgotten rather than in whichever code writes the row.
"""

from __future__ import annotations

import uuid
from typing import Final

import pytest
from commerce_api.deps import (
    CAPABILITIES_BY_ACTOR,
    MERCHANT_CAPABILITIES,
    OPERATOR_CAPABILITIES,
)
from commerce_domain import ActorType
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from conftest import SeededTenant

pytestmark = pytest.mark.db


# ------------------------------------------------------------------------ the session


class TestMintingOne:
    def test_a_merchant_session_needs_the_operator_key(
        self, client: TestClient, seeded_tenant: SeededTenant
    ) -> None:
        """The same gate an operator session has, and for the same reason.

        Without it, "anyone who can reach the demo router" becomes "anyone who can approve
        a change to the catalogue".
        """
        refused = client.post(
            "/v1/demo/sessions",
            json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "MERCHANT"},
        )
        assert refused.status_code in (401, 404), refused.text

    def test_a_merchant_session_carries_no_buyer(
        self, client: TestClient, seeded_tenant: SeededTenant, scenario_headers: dict[str, str]
    ) -> None:
        minted = client.post(
            "/v1/demo/sessions",
            json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "MERCHANT"},
            headers=scenario_headers,
        )
        assert minted.status_code == 201, minted.text
        body = minted.json()
        assert body["buyer_ref"] is None
        assert body["actor_type"] == "MERCHANT"
        assert body["merchant_id"]

    def test_asking_for_a_buyer_reference_is_refused_rather_than_ignored(
        self, client: TestClient, seeded_tenant: SeededTenant, scenario_headers: dict[str, str]
    ) -> None:
        """Silently dropping it would leave the caller believing the session was scoped.

        A merchant is the shop, not one of its shoppers. A caller who sent a reference
        thought it narrowed the session, and a 201 with the field quietly nulled is the
        answer most likely to be misread.
        """
        refused = client.post(
            "/v1/demo/sessions",
            json={
                "tenant_slug": seeded_tenant.tenant_slug,
                "actor_type": "MERCHANT",
                "buyer_ref": "buyer-pretending",
            },
            headers=scenario_headers,
        )
        assert refused.status_code == 422, refused.text
        assert "shop" in refused.json()["detail"]

    def test_a_buyer_session_still_carries_one(
        self, client: TestClient, seeded_tenant: SeededTenant
    ) -> None:
        """The constraint runs both ways, so this is half of what it asserts."""
        minted = client.post(
            "/v1/demo/sessions",
            json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "BUYER"},
        )
        assert minted.status_code == 201, minted.text
        assert minted.json()["buyer_ref"]


class TestTheDatabaseStatesTheRule:
    """The constraint, not the code that usually writes the row.

    Both directions are checked because a one-way rule is the one that rots: a merchant
    with a buyer is the bug that routes a shop to the buyer's copilot, and a buyer without
    one is the bug that makes an order belong to nobody.
    """

    _INSERT: Final = (
        "INSERT INTO api_sessions (id, tenant_id, merchant_id, token_hash, buyer_ref, "
        "actor_type, capabilities, expires_at) VALUES "
        "(:id, :t, :m, :h, :b, :a, CAST('[]' AS jsonb), now() + interval '1 hour')"
    )

    def _insert(self, engine: Engine, tenant: SeededTenant, actor: str, buyer: str | None) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(self._INSERT),
                {
                    "id": uuid.uuid4(),
                    "t": tenant.tenant_id,
                    "m": tenant.merchant_id,
                    "h": uuid.uuid4().hex + uuid.uuid4().hex,
                    "b": buyer,
                    "a": actor,
                },
            )

    def test_a_merchant_row_with_a_buyer_is_refused(
        self, capi_admin_engine: Engine, seeded_tenant: SeededTenant
    ) -> None:
        with pytest.raises(IntegrityError, match="merchant_has_no_buyer"):
            self._insert(capi_admin_engine, seeded_tenant, "MERCHANT", "buyer-smuggled")

    def test_a_buyer_row_without_one_is_refused(
        self, capi_admin_engine: Engine, seeded_tenant: SeededTenant
    ) -> None:
        with pytest.raises(IntegrityError, match="merchant_has_no_buyer"):
            self._insert(capi_admin_engine, seeded_tenant, "BUYER", None)

    def test_an_actor_the_platform_does_not_have_is_refused(
        self, capi_admin_engine: Engine, seeded_tenant: SeededTenant
    ) -> None:
        with pytest.raises(IntegrityError, match="actor_type_enum"):
            self._insert(capi_admin_engine, seeded_tenant, "ADMIN", "buyer-1")


# --------------------------------------------------------------------- the doors it shuts


class TestWhatAMerchantMayNotDo:
    def _merchant(
        self, client: TestClient, tenant: SeededTenant, scenario_headers: dict[str, str]
    ) -> TestClient:
        minted = client.post(
            "/v1/demo/sessions",
            json={"tenant_slug": tenant.tenant_slug, "actor_type": "MERCHANT"},
            headers=scenario_headers,
        )
        assert minted.status_code == 201, minted.text
        return TestClient(client.app, headers={"Authorization": f"Bearer {minted.json()['token']}"})

    def test_a_merchant_may_not_throw_the_kill_switch(
        self, client: TestClient, seeded_tenant: SeededTenant, scenario_headers: dict[str, str]
    ) -> None:
        """The refusal that did not exist until the allowlist replaced the denylist.

        Safe Mode stops delegated payments across the tenant. That is the platform's
        posture, not one shop's, and a merchant who wants to stop selling has their own
        controls.
        """
        merchant = self._merchant(client, seeded_tenant, scenario_headers)
        refused = merchant.post(
            "/v1/ops/safe-mode", json={"enabled": True}, headers=scenario_headers
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["actor_type"] == "MERCHANT"

    def test_a_merchant_may_not_revive_a_buried_command(
        self, client: TestClient, seeded_tenant: SeededTenant, scenario_headers: dict[str, str]
    ) -> None:
        """Revive re-drives the money operation the command carries."""
        merchant = self._merchant(client, seeded_tenant, scenario_headers)
        refused = merchant.post(f"/v1/ops/outbox/{uuid.uuid4()}/revive", headers=scenario_headers)
        assert refused.status_code == 403, refused.text

    def test_a_merchant_holds_no_capability_that_moves_money(self) -> None:
        """Read from the set itself, so a later addition fails here rather than shipping.

        A merchant-initiated financial remedy crosses a narrow financial boundary with
        independently checked permissions. It is a different request, not a wider version
        of one of these.
        """
        money = {
            "refund.request",
            "checkout.approve",
            "checkout.submit_approved",
            "payment.verify",
            "basket.write",
            "checkout.create",
        }
        assert MERCHANT_CAPABILITIES.isdisjoint(money), MERCHANT_CAPABILITIES & money

    def test_a_merchant_is_not_an_operator_with_a_different_name(self) -> None:
        """Two sets, and each holds something the other does not.

        If they were equal the second actor would be decoration. An operator works the
        platform's apparatus and a merchant works one shop, and the capability sets are
        where that stops being a sentence and starts being enforced.
        """
        assert MERCHANT_CAPABILITIES != OPERATOR_CAPABILITIES
        assert MERCHANT_CAPABILITIES - OPERATOR_CAPABILITIES
        assert OPERATOR_CAPABILITIES - MERCHANT_CAPABILITIES

    def test_proposing_and_approving_are_separate_capabilities(self) -> None:
        """The two halves of the guarantee, and a model holds neither.

        A merchant session holds both today because one person does both. They are separate
        strings so that the copilot which drafts a change can be given the first alone --
        and so that a surface which merged them could not, because there would be nothing
        to merge them into.
        """
        assert "merchant.action.propose" in MERCHANT_CAPABILITIES
        assert "merchant.action.approve" in MERCHANT_CAPABILITIES
        agent = CAPABILITIES_BY_ACTOR[ActorType.AGENT]
        assert "merchant.action.propose" not in agent
        assert "merchant.action.approve" not in agent


class TestTheKernelRefusesItToo:
    """Defence in depth, at the layer that cannot be reached around.

    The HTTP route hands the kernel a hard-coded OPERATOR, so the kernel never sees a
    merchant from that path. This asserts what happens to a caller that does not go through
    the route -- which is the caller the kernel's own guard exists for.
    """

    def test_safe_mode_names_who_may_switch_it(self) -> None:
        from transaction_kernel.safe_mode import MAY_SWITCH_SAFE_MODE

        assert ActorType.MERCHANT not in MAY_SWITCH_SAFE_MODE
        assert ActorType.AGENT not in MAY_SWITCH_SAFE_MODE
        assert ActorType.OPERATOR in MAY_SWITCH_SAFE_MODE

    def test_the_kernel_refuses_a_merchant_by_name(self) -> None:
        from transaction_kernel.safe_mode import _validate_actor

        with pytest.raises(ValueError, match="MERCHANT may not"):
            _validate_actor("merchant:someone", ActorType.MERCHANT)


class TestTheBuyerCopilotDoesNotTakeThem:
    def test_a_merchant_principal_is_not_routed_to_the_buyers_copilot(self) -> None:
        """Where the placeholder buyer reference would have done its damage.

        `accepts` is an allowlist now, so this passes on the actor alone -- but the
        merchant-scope check behind it is asserted too, because that was the only thing
        standing between a merchant and the buyer's harness before.
        """
        from agent_runtime.harness.base import is_buyer_principal, is_merchant_principal
        from agent_runtime.harness.razorai import RazorAI
        from commerce_domain import AgentPrincipal

        principal = AgentPrincipal(
            principal_id="session:merchant",
            tenant_id=uuid.uuid4(),
            actor_type=ActorType.MERCHANT,
            merchant_id=uuid.uuid4(),
            buyer_ref=None,
            capabilities=frozenset(MERCHANT_CAPABILITIES),
        )
        assert is_merchant_principal(principal)
        assert not is_buyer_principal(principal)
        assert not RazorAI().accepts(principal)

    def test_even_a_merchant_wearing_a_buyer_reference_is_not_a_buyer(self) -> None:
        """The database refuses this row, and the code refuses it too.

        Belt and braces on purpose: the constraint is what makes it unreachable, and this
        is what makes it harmless if a future migration ever loosens the constraint.
        """
        from agent_runtime.harness.base import is_buyer_principal, is_merchant_principal
        from commerce_domain import AgentPrincipal

        impossible = AgentPrincipal(
            principal_id="session:merchant",
            tenant_id=uuid.uuid4(),
            actor_type=ActorType.MERCHANT,
            merchant_id=uuid.uuid4(),
            buyer_ref="buyer-placeholder",
            capabilities=frozenset(),
        )
        assert not is_buyer_principal(impossible)
        assert is_merchant_principal(impossible)
