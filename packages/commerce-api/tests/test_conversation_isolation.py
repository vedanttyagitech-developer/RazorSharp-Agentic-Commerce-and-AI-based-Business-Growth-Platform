"""Owner binding and provider continuation regressions, without external model calls."""

import uuid
from types import SimpleNamespace

import pytest
from commerce_api.errors import ProblemError
from commerce_api.services.conversation import ConversationService
from commerce_api.services.razor_main import VertexModelClient, _compact


def test_conversation_cannot_be_claimed_by_another_session_or_surface():
    store = ConversationService()
    conversation, tenant = uuid.uuid4(), uuid.uuid4()
    store.claim(conversation, tenant_id=tenant, owner="buyer-session:buyer")
    store.claim(conversation, tenant_id=tenant, owner="buyer-session:buyer")
    for owner in ("another-buyer:buyer", "buyer-session:console", "merchant:merchant"):
        with pytest.raises(ProblemError):
            store.claim(conversation, tenant_id=tenant, owner=owner)
    with pytest.raises(ProblemError):
        store.claim(conversation, tenant_id=uuid.uuid4(), owner="buyer-session:buyer")


def test_provider_follow_up_keeps_system_and_tools(monkeypatch):
    client = VertexModelClient(model="test", project="test", location="test")
    calls = []
    response = SimpleNamespace(candidates=[], text="Done", function_calls=[])
    monkeypatch.setattr(
        client,
        "_complete",
        lambda system, tools, contents: calls.append((system, tools, contents)) or response,
    )
    _, state = client.generate(
        system="Role and safety rules",
        history=[],
        message="hello",
        tools=[{"name": "catalog.search", "parameters": {}}],
    )
    client.follow_up(state=state, calls=[], results=[])
    assert calls[1][0] == calls[0][0] == "Role and safety rules"
    assert calls[1][1] == calls[0][1]


def test_read_evidence_survives_compaction():
    payload = {
        "services": {"worker": {"status": "unavailable"}},
        "sources": [{"path": "docs/DEMO.md"}],
    }
    result = _compact(SimpleNamespace(ok=True, payload=payload, reason_key=None))
    assert result["data"] == payload


@pytest.mark.db
def test_console_endpoint_is_operator_only(mint_client, client, api_app):
    client.headers["X-Scenario-Key"] = api_app.state.settings.scenario_key.get_secret_value()
    for role, expected in (("BUYER", 403), ("MERCHANT", 403), ("OPERATOR", 200)):
        client, _ = mint_client(actor_type=role)
        response = client.post("/v1/ops/agent/turn", json={"message": "Explain the kernel"})
        assert response.status_code == expected, response.text


@pytest.mark.db
def test_default_conversation_stable_and_cannot_be_stolen(mint_client):
    first, _ = mint_client(actor_type="BUYER")
    second, _ = mint_client(actor_type="BUYER")
    a = first.post("/v1/agent/turn", json={"message": "hello"})
    b = first.post("/v1/agent/turn", json={"message": "hello again"})
    assert a.status_code == b.status_code == 200
    conversation = a.json()["conversation_id"]
    assert b.json()["conversation_id"] == conversation
    stolen = second.post(
        "/v1/agent/turn", json={"message": "hello", "conversation_id": conversation}
    )
    assert stolen.status_code == 403


def test_voice_stream_uses_existing_turn_once_and_preserves_auth(mint_client, monkeypatch):
    from commerce_api.services import agent_service
    from commerce_api.services.speech_stream import speech_sink

    original = agent_service.run_turn
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["message"])
        sink = speech_sink.get()
        assert sink is not None
        sink({"type": "speech", "text": "Hello there.", "language": "en"})
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_service, "run_turn", run)
    buyer, _ = mint_client(actor_type="BUYER")
    response = buyer.post("/v1/voice/turn-stream", json={"message": "hello"})
    assert response.status_code == 200
    import json

    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]["type"] == "speech"
    assert events[-1]["type"] == "result"
    assert events[-1]["status"] == 200
    assert calls == ["hello"]
    assert speech_sink.get() is None
    buyer.headers.pop("Authorization", None)
    denied = buyer.post("/v1/voice/turn-stream", json={"message": "hello"})
    assert denied.status_code in {401, 403}
    assert calls == ["hello"]
