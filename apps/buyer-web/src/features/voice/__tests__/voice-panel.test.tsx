/**
 * The panel, wired to a fake socket: the whole surface, end to end, in a jsdom.
 *
 * The point of this file is the two properties that only show up once the pieces are
 * assembled: an assistant reply is on screen with no audio having been played, and there
 * is nowhere on the finished panel to approve or pay.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { VoicePanel } from "../voice-panel";
import type { ServerFrame } from "../wire";

import { fakeAudio, fakeSocketFactory, Recorder, sessionReady, settle } from "./fakes";

afterEach(cleanup);

async function mountedPanel() {
  const recorder = new Recorder();
  const { connect, sockets } = fakeSocketFactory(recorder);
  const audio = fakeAudio(recorder);
  render(<VoicePanel url="wss://storefront.test/api/voice/stream" connect={connect} openAudio={async () => audio.io} />);

  fireEvent.click(screen.getByRole("button", { name: /start voice/i }));
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
  return { deliver, sockets, audio, recorder };
}

describe("VoicePanel", () => {
  it("connects and says so", async () => {
    await mountedPanel();
    expect(screen.getByText("Connected")).toBeTruthy();
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

  it("makes a degradation visible on the panel itself", async () => {
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
    // The promise the notice makes is one the panel can keep.
    expect(screen.getByLabelText(/type to razorai instead of speaking/i)).toBeTruthy();
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

  it("sends typed text and clears the box", async () => {
    const { recorder } = await mountedPanel();
    const input = screen.getByLabelText(/type to razorai instead of speaking/i) as HTMLInputElement;

    fireEvent.change(input, { target: { value: "two kilos of onions" } });
    fireEvent.click(screen.getByRole("button", { name: /send typed message/i }));

    expect(recorder.sentFrames()).toContainEqual({
      type: "text_input",
      text: "two kilos of onions",
    });
    expect(input.value).toBe("");
  });
});
