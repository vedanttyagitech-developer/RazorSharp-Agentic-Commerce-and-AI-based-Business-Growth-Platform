"""The single reasoning-model configuration source: identity and constraints.

The identifier ``gemini-3.8-flash`` is verified against the provider's model
documentation (GA September 2026): thinking levels LOW/MEDIUM/HIGH, no MINIMAL,
no temperature/top_p/top_k sampling controls, no Live API. These tests pin the
contract so a model swap cannot silently keep stale settings.
"""

from agent_runtime.runtime_adk.model_config import (
    DEFAULT_MODEL,
    generation_config,
    metadata,
    model_name,
    supports_live_api,
    thinking_level,
    use_temperature,
)


class TestIdentity:
    def test_default_is_the_verified_flash_model(self) -> None:
        assert model_name({}) == "gemini-3.8-flash"
        assert DEFAULT_MODEL == "gemini-3.8-flash"

    def test_explicit_env_wins_then_operator_var_then_default(self) -> None:
        assert model_name({"AGENT_RUNTIME_MODEL": "x", "GEMINI_MODEL_ID": "y"}) == "x"
        assert model_name({"GEMINI_MODEL_ID": "y"}) == "y"
        assert model_name({"AGENT_RUNTIME_MODEL": "  "}) == DEFAULT_MODEL


class TestConstraints:
    def test_flash_has_thinking_but_no_temperature_or_live_api(self) -> None:
        assert thinking_level("gemini-3.8-flash") == "LOW"
        assert use_temperature("gemini-3.8-flash") is False
        assert supports_live_api("gemini-3.8-flash") is False

    def test_generation_config_carries_only_supported_settings(self) -> None:
        assert generation_config("gemini-3.8-flash") == {"thinking_level": "LOW"}
        assert generation_config("gemini-2.5-flash") == {"temperature": 0.2}


class TestMetadata:
    def test_metadata_names_identity_and_limits_only(self) -> None:
        facts = metadata({})
        assert facts == {
            "model": "gemini-3.8-flash",
            "provider": "vertex_ai",
            "thinking_level": "LOW",
            "max_output_tokens": 65536,
            "live_api_supported": False,
            "source": "default",
        }

    def test_metadata_names_its_source(self) -> None:
        assert metadata({"AGENT_RUNTIME_MODEL": "m"})["source"] == "AGENT_RUNTIME_MODEL"
        assert metadata({"GEMINI_MODEL_ID": "m"})["source"] == "GEMINI_MODEL_ID"
