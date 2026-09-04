"""A scripted stand-in for Gemini, for tests that drive a real ADK ``Runner``.

Each step is a function of the request the model would have seen and returns the parts
it answers with -- a function call, or text. The model records every request so a test
can read what the agent actually put in front of it: the system instruction, the tool
declarations, the function responses. No network, no key, no Vertex.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import ConfigDict, Field

Step = Callable[[LlmRequest], list[types.Part]]


class ScriptedModel(BaseLlm):
    """Answers each model call with the next scripted step; text when the script is out."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model: str = "scripted"
    steps: list[Any] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["scripted"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        del stream
        self.requests.append(llm_request)
        step: Step = self.steps.pop(0) if self.steps else say("(script exhausted)")
        yield LlmResponse(content=types.Content(role="model", parts=step(llm_request)))


def call(name: str, **args: Any) -> Step:
    """A step that calls one tool."""

    def step(_: LlmRequest) -> list[types.Part]:
        return [types.Part(function_call=types.FunctionCall(name=name, args=dict(args)))]

    return step


def say(text: str) -> Step:
    def step(_: LlmRequest) -> list[types.Part]:
        return [types.Part(text=text)]

    return step


def latest_function_response(request: LlmRequest) -> dict[str, Any] | None:
    """The newest tool result in the request, as the model would read it."""
    for content in reversed(request.contents):
        for part in reversed(content.parts or []):
            if part.function_response is not None and part.function_response.response:
                return dict(part.function_response.response)
    return None


def echo_field(field: str) -> Step:
    """A step that repeats one field of the newest tool result verbatim.

    This is the behaviour the prompt asks of the model on a kernel decision -- include
    ``rendered_for_buyer`` unchanged -- scripted so the test proves what reaches the buyer
    when the model does exactly as told.
    """

    def step(request: LlmRequest) -> list[types.Part]:
        response = latest_function_response(request) or {}
        return [types.Part(text=str(response.get(field, "")))]

    return step
