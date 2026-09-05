/**
 * The surface, wired to a fake socket: the whole thing, end to end, in a jsdom.
 *
 * The point of this file is the properties that only show up once the pieces are
 * assembled: an assistant reply is on screen with no audio having been played, the
 * session opens itself and says so, and there is nowhere on the finished surface to
 * approve or pay.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { VoicePanel, type VoiceSurfaceState } from "../voice-panel";
import type { ServerFrame } from "../wire";

import { fakeAudio, fakeSocketFactory, Recorder, sessionReady, settle } from "./fakes";

afterEach(cleanup);

async function mountedPanel() {
  const recorder = new Recorder();
  const { connect, sockets } = fakeSocketFactory(recorder);
  const audio = fakeAudio(recorder);
  const states: VoiceSurfaceState[] = [];
  render(
    <VoicePanel
      url="wss://storefront.test/api/voice/stream"
      connect={connect}
      openAudio={async () => audio.io}
      onStateChange={(state) => states.push(state)}
    />,
  );

  // The session opens itself on a zero-delay timer; let it, then let the socket open.
  await act(async () => {
    await settle();
  });
  await act(async () => {
    sockets[0].open();
    sockets[0].deliver(sessionReady());
    await settle();
  });

  const deliver = async (frame: ServerFrame) => {
    await act(async () => {
      sockets[0].deliver(frame);
      await settle();
    });
  };
  return { deliver, sockets, audio, recorder, states };
}

function surface(): HTMLElement {
  return screen.getByRole("region", { name: /talk to razorai/i });
}

describe("VoicePanel", () => {
  it("opens itself, and reports a live surface with the microphone open", async () => {
    const { states } = await mountedPanel();
    expect(states[states.length - 1]).toEqual({ live: true, phase: "listening" });
    expect(surface().getAttribute("data-voice-live")).toBe("true");
    expect(surface().getAttribute("data-voice-phase")).toBe("listening");
  });

  it("shows an assistant reply the moment its frame lands, with no audio at all", async () => {
    const { deliver, audio } = await mountedPanel();
    await deliver({
      type: "agent_reply",
      text: "Your current total is ₹395.00, including a ₹25.00 delivery fee.",
      deterministic: true,
      locale: "en-IN",
      turn_id: 1,
      speech_generation: 1,
      template_id: "quote.total",
      template_version: 2,
      fields: { total: "₹395.00" },
    });

    expect(
      screen.getByText("Your current total is ₹395.00, including a ₹25.00 delivery fee."),
    ).toBeTruthy();
    expect(audio.scheduledChunks()).toBe(0);
  });

  it("draws an interim turn while the buyer is still speaking", async () => {
    const { deliver } = await mountedPanel();
    await deliver({
      type: "transcript_partial",
      text: "two kilos of",
      turn_id: 1,
      stt_generation: 1,
      age_ms: 40,
      cumulative: true,
      revisable: true,
      empty_means: "keep_held",
    });
    expect(screen.getByText(/still hearing you/i)).toBeTruthy();

    await deliver({
      type: "transcript_final",
      text: "two kilos of onions",
      turn_id: 1,
      stt_generation: 1,
      age_ms: 300,
      stale: false,
      source: "voice",
      cumulative: true,
      revisable: false,
      empty_means: "keep_held",
    });
    expect(screen.queryByText(/still hearing you/i)).toBeNull();
    expect(screen.getByText("two kilos of onions")).toBeTruthy();
  });

  it("makes a degradation visible on the surface itself", async () => {
    const { deliver } = await mountedPanel();
    await deliver({
      type: "degradation",
      kind: "stt_unavailable",
      message: "recognizer returned 503",
      text_input_available: true,
      transaction_state_changed: false,
    });
    expect(screen.getByText(/speech recognition is unavailable/i)).toBeTruthy();
    expect(screen.getByText(/you can still type/i)).toBeTruthy();
    // The promise the notice makes is one the surface can keep.
    expect(screen.getByLabelText(/message razorai/i)).toBeTruthy();
  });

  it("has no control anywhere on it that approves, pays, cancels or refunds", async () => {
    const { deliver } = await mountedPanel();
    await deliver({
      type: "agent_reply",
      text: "Say yes and I will place the order.",
      deterministic: false,
      locale: "en-IN",
      turn_id: 1,
      speech_generation: 1,
      template_id: null,
      template_version: null,
      fields: null,
    });

    const names = screen
      .getAllByRole("button")
      .map((button) => (button.getAttribute("aria-label") ?? button.textContent ?? "").toLowerCase());
    for (const forbidden of ["approve", "pay", "confirm", "cancel", "refund", "place order"]) {
      expect(names.some((name) => name.includes(forbidden))).toBe(false);
    }
    expect(screen.getByText(/approving and paying happen on the store/i)).toBeTruthy();
  });

  it("sends typed text down the socket and clears the box", async () => {
    const { recorder } = await mountedPanel();
    const input = screen.getByLabelText(/message razorai/i) as HTMLInputElement;

    fireEvent.change(input, { target: { value: "two kilos of onions" } });
    fireEvent.click(screen.getByRole("button", { name: /send to razorai/i }));

    expect(recorder.sentFrames()).toContainEqual({
      type: "text_input",
      text: "two kilos of onions",
    });
    expect(input.value).toBe("");
  });

  it("mutes and unmutes the microphone from the composer, and says which it did", async () => {
    const { states } = await mountedPanel();

    fireEvent.click(screen.getByRole("button", { name: /mute the microphone/i }));
    expect(surface().getAttribute("data-voice-phase")).toBe("idle");
    expect(states[states.length - 1]).toEqual({ live: true, phase: "idle" });

    fireEvent.click(screen.getByRole("button", { name: /unmute the microphone/i }));
    expect(surface().getAttribute("data-voice-phase")).toBe("listening");
  });
});
