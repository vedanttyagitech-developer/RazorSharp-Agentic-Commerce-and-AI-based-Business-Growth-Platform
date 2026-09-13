"""Default conversational reasoning; financial operations stay on the existing bridge."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..providers.live_conversation import LiveConversation
from ..tts.guard import SpeechGuard
from ..turn import TurnReply
from .agent_client import AGENT_TURN_PATH, AGENT_TURN_TIMEOUT_S, HttpTurnHandler


class RazorAIMainAgent(HttpTurnHandler):
    def __init__(self, *args: Any, project: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.live = LiveConversation(project, self.call_tool)

    def set_project_context(self, enabled: bool, step: str) -> None:
        self.presentation = enabled
        self.tour_step = step if step in {"merchant", "shopping", "console"} else "shopping"

    async def call_tool(self, name: str, question: str) -> dict[str, Any]:
        body: dict[str, Any] = {"message": question}
        if name == "project_knowledge":
            body.update(
                presentation=True,
                grounding_only=True,
                tour_step=self.tour_step,
                project_questions=self.project_questions[-8:],
            )
        response = await self._client.post(
            AGENT_TURN_PATH,
            json=body,
            headers={"Authorization": f"Bearer {self._bearer}"},
            timeout=AGENT_TURN_TIMEOUT_S,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        self._note_scenario_faults(response)
        return payload

    async def handle_turn(self, transcript: Any, identity: Any) -> TurnReply:
        if not transcript.is_final:
            raise ValueError("Only a settled request may enter Live reasoning")
        if identity.copilot != "buyer":
            raise ValueError("This conversation bridge requires an authenticated buyer session")
        message = transcript.text.strip()[:2000]
        if not message:
            return TurnReply()
        try:
            text, audio, evidence = await self.live.answer(message, self.tour_step)
        except Exception:
            # Never replay an uncertain tool action through another runner.
            return TurnReply(
                text=(
                    "The live assistant could not complete this answer. "
                    "Please check any action in the screen before retrying."
                ),
                server_authored=True,
                degraded=True,
            )
        if not text:
            return self._to_reply(evidence)
        guide = evidence.get("structured", {})
        sources = guide.get("sources", []) if isinstance(guide, dict) else []
        verdict = SpeechGuard().check(text, deterministic=False, project_narration=True)
        if verdict.refused_any:
            return self._to_reply(evidence)
        self.project_questions = [*self.project_questions, message][-8:]
        base = self._to_reply(
            {
                "reply": text,
                "language": evidence.get("language", "en"),
                "server_authored": False,
                "structured": {
                    "kind": "project_guide",
                    "reply": text,
                    "step": self.tour_step,
                    "sources": sources,
                    "generation_status": "gemini_live",
                },
            }
        )
        return replace(base, native_audio=audio)

    async def aclose(self) -> None:
        await self.live.aclose()
