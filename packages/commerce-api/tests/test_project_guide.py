from commerce_api.services.project_guide import answer, retrieve
from voice_runtime.gateway.agent_client import HttpTurnHandler
from voice_runtime.wire.frames import ScreenContext


def test_tour_begins_with_question_and_follows_merchant_shopping_console():
    first = answer("start project tour")
    assert "overview" in first["reply"]
    assert first["step"] == "merchant"
    assert answer("technical walkthrough")["navigate"] == "merchant"
    assert answer("next", "merchant")["navigate"] == "shopping"
    assert answer("next", "shopping")["navigate"] == "console"
    assert answer("next", "console")["navigate"] == "console"


def test_explanations_retrieve_sources_and_do_not_execute_actions():
    result = answer("How does exact approval prevent a stale price?")
    assert result["sources"][0]["id"] == "approval"
    assert "proposal" not in result
    assert "navigate" not in result or result["navigate"] is None
    assert answer("add two milk") is None
    assert answer("pay now") is None


def test_unknown_claim_is_not_invented():
    assert retrieve("zyxwvu") == []
    assert "verified project source" in answer("zyxwvu")["reply"]


def test_voice_preserves_guide_without_cart_proposal():
    data = {"kind": "project_guide", **answer("technical walkthrough")}
    reply = HttpTurnHandler._to_reply(
        {"reply": data["reply"], "language": "en", "server_authored": True, "structured": data}
    )
    assert reply.project_guide == data
    assert not reply.offer_is_proposal
    assert reply.offer is None
    assert ScreenContext(scope="project").tour_step == "merchant"


def test_project_speech_uses_curated_text_not_generated_outcome_claims():
    reply = HttpTurnHandler._to_reply(
        {
            "reply": "Your payment succeeded",
            "server_authored": False,
            "structured": {
                "kind": "project_guide",
                "speech_text": "A callback alone is not capture evidence.",
            },
        }
    )
    assert reply.text == "A callback alone is not capture evidence."
    assert reply.server_authored
    ordinary = HttpTurnHandler._to_reply(
        {"reply": "Your payment succeeded", "server_authored": False}
    )
    assert not ordinary.server_authored


def test_followup_retains_topic_without_automatically_navigating():
    result = answer("explain that with an example", "console", ["How does exact approval work?"])
    assert "approval" in [source["id"] for source in result["sources"]]
    assert result["navigate"] is None
    assert answer("add milk", "console", ["Explain approval"]) is None


def test_project_greeting_does_not_offer_products():
    result = answer("hello")
    assert "Vedant" in result["reply"]
    assert "overview" in result["reply"]


def test_guarded_conversational_response_is_spoken_instead_of_fixed_paragraph():
    reply = HttpTurnHandler._to_reply(
        {
            "reply": "The layers are separated so each component has a clear responsibility.",
            "server_authored": False,
            "structured": {"kind": "project_guide", "speech_text": "Curated explanation."},
        }
    )
    assert reply.text.startswith("The layers")
    assert not reply.server_authored


def test_untrusted_conversation_context_is_bounded():
    import pytest
    from commerce_api.routers.agent import TurnRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TurnRequest(message="why?", project_questions=["x"] * 9)
    with pytest.raises(ValidationError):
        TurnRequest(message="why?", project_questions=["x" * 2001])


def test_generation_receives_followup_context_and_has_answer_budget(monkeypatch):
    import json
    from types import SimpleNamespace

    from commerce_api.services.project_guide import generate
    from google import genai

    captured = {}

    def content(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            text=json.dumps(
                {"reply": "Each layer has one responsibility.", "source_ids": ["architecture"]}
            )
        )

    class Client:
        models = SimpleNamespace(generate_content=content)

        def __init__(self, *args: object, **kwargs: object) -> None:
            # Mirrors the production Vertex constructor shape
            # (vertexai/project/location); the fake ignores them.
            ...

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(genai, "Client", Client)
    history = ["Explain the architecture"]
    result = generate("Say that more simply", answer("architecture"), history)
    assert result["generated"]
    assert json.loads(captured["contents"])["previous_questions"] == history
    assert captured["config"].max_output_tokens >= 2000
    assert captured["config"].automatic_function_calling.disable


def test_curated_evidence_matches_current_code():
    import runpy
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    tool = runpy.run_path(str(root / "scripts/refresh_project_knowledge.py"))
    assert tool["INDEX"].read_text() == tool["refreshed"]()


def test_retrieval_covers_judge_engineering_questions():
    cases = [
        ("How does a grant stop duplicate provider execution?", "grants"),
        ("How are raw webhook signatures checked?", "webhooks"),
        ("Why is UNKNOWN reconciled instead of paid again?", "reconciliation"),
        ("Can one merchant read another merchant support case?", "support-isolation"),
        ("What if two tabs open support simultaneously?", "support-race"),
        ("Can sale terms change after Merchant Policy publication?", "merchant-policy"),
        ("Does API health prove the voice worker is ready?", "runtime"),
        ("Why does the demo return 429 under load?", "overload"),
        ("Does a signed Reserve proof mean bank funds are blocked?", "reserve-proof"),
        ("Does mounting eliminate backend role checks?", "mounted-frontend"),
        ("How is project RAG connected to the agent?", "project-rag"),
    ]
    for question, expected in cases:
        assert expected in [d["id"] for d in retrieve(question)[:3]], question


def test_only_returned_source_ids_are_cited(monkeypatch):
    import json
    from types import SimpleNamespace

    from commerce_api.services.project_guide import generate
    from google import genai

    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            # Mirrors the production Vertex constructor shape
            # (vertexai/project/location); the fake ignores them.
            ...

        models = SimpleNamespace(
            generate_content=lambda **_kwargs: SimpleNamespace(
                text=json.dumps(
                    {"reply": "The grant is bound to its operation.", "source_ids": ["grants"]}
                )
            )
        )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(genai, "Client", Client)
    result = generate("How do execution grants work?", answer("execution grants"))
    assert result["generated"]
    assert [source["id"] for source in result["sources"]] == ["grants"]


def test_provider_429_is_observable_and_keeps_grounded_fallback(monkeypatch):
    from types import SimpleNamespace

    from commerce_api.services.project_guide import generate
    from google import genai

    class ThrottleError(Exception):
        code = 429

    def failed(**_kwargs):
        raise ThrottleError("private provider details must not be returned")

    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            # Mirrors the production Vertex constructor shape
            # (vertexai/project/location); the fake ignores them.
            ...

        models = SimpleNamespace(generate_content=failed)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(genai, "Client", Client)
    original = answer("webhook signatures")
    result = generate("webhook signatures", original)
    assert result["provider_status"] == 429
    assert result["generation_status"] == "provider_unavailable"
    assert result["reply"] == original["reply"]
    assert not result.get("generated")
    assert "private provider details" not in str(result)


def test_judge_challenges_retrieve_their_grounded_explanations():
    questions = {
        "Why should Razorpay care?": "judge-relevance",
        "Is this just a chatbot?": "judge-chatbot",
        "Explain this screen": "judge-screen",
        "Show me the proof": "judge-proof",
        "Can AI spend money by itself?": "judge-agent-authority",
        "What if the network fails or I click twice?": "judge-timeout",
        "Is this production ready and how does it scale?": "judge-production",
        "How is Reserve Pay different from normal checkout?": "judge-reserve",
        "Why use voice and what if Gemini goes down?": "judge-voice",
        "Why ACP UCP and MCP?": "judge-protocols",
        "Why a kernel instead of letting the model handle everything?": "judge-design",
    }
    for question, expected in questions.items():
        result = answer(question)
        assert result is not None, question
        assert expected in [source["id"] for source in result["sources"]], question
        assert result.get("navigate") is None
        assert "proposal" not in result
        assert all(source["source_sha256"] for source in result["sources"])


def test_proof_question_does_not_claim_a_fresh_test_run():
    result = answer("Show me the proof")
    assert "passed today" in result["reply"]
    assert answer("show me milk") is None
