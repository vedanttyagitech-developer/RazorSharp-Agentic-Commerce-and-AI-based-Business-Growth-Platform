/**
 * What the transcript draws, and what it refuses to draw.
 *
 * The last test in this file is the one the project is actually about: there is no control
 * in this component that approves, pays, cancels or refunds, and a reply that asks the
 * buyer to confirm something gets a sentence pointing at the trusted surface rather than a
 * button that would become one.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { LiveTranscript } from "../live-transcript";
import { initialTranscriptState, reduceTranscript, type TranscriptEntry } from "../transcript";

afterEach(cleanup);

const buyerTurn: TranscriptEntry = {
  kind: "buyer",
  id: "b1",
  seq: 1,
  turnId: 1,
  text: "two kilos of onions",
  stale: false,
  source: "voice",
};

const staleTurn: TranscriptEntry = { ...buyerTurn, id: "b2", seq: 2, turnId: 2, stale: true };

const deterministicReply: TranscriptEntry = {
  kind: "assistant",
  id: "a1",
  seq: 3,
  turnId: 2,
  text: "Your current total is ₹395.00, including a ₹25.00 delivery fee.",
  deterministic: true,
  locale: "en-IN",
  speechGeneration: 1,
  templateId: "quote.total",
  templateVersion: 3,
  fields: { total: "₹395.00", delivery_fee: "₹25.00" },
};

const modelReply: TranscriptEntry = {
  kind: "assistant",
  id: "a2",
  seq: 4,
  turnId: 3,
  text: "Red onions are cheaper today than the white ones.",
  deterministic: false,
  locale: "en-IN",
  speechGeneration: 2,
  templateId: null,
  templateVersion: null,
  fields: null,
};

describe("LiveTranscript", () => {
  it("draws interim text apart from settled turns and marks it unconfirmed", () => {
    render(
      <LiveTranscript entries={[buyerTurn]} held={{ turnId: 2, text: "actually three" }} speaking={false} />,
    );
    const log = screen.getByRole("log");
    expect(within(log).getByText("two kilos of onions")).toBeTruthy();
    // The interim is outside the log, so a screen reader is not read a revising hypothesis.
    expect(within(log).queryByText(/actually three/)).toBeNull();
    expect(screen.getByText(/still hearing you/i)).toBeTruthy();
  });

  it("shows a stale final as heard and not acted on", () => {
    render(<LiveTranscript entries={[staleTurn]} held={null} speaking={false} />);
    expect(screen.getByText("two kilos of onions")).toBeTruthy();
    expect(screen.getByText(/heard, not acted on/i)).toBeTruthy();
    expect(screen.getByText(/say it again/i)).toBeTruthy();
  });

  it("renders a reply from the frame alone, with no audio having arrived", () => {
    // Exactly what the socket handler does: one `agent_reply`, no speech frames at all.
    const state = reduceTranscript(initialTranscriptState, {
      type: "agent_reply",
      text: "I found four kinds of onion.",
      deterministic: false,
      locale: "en-IN",
      turn_id: 1,
      speech_generation: 1,
      template_id: null,
      template_version: null,
      fields: null,
    });
    render(<LiveTranscript entries={state.entries} held={state.held} speaking={state.speaking} />);
    expect(screen.getByText("I found four kinds of onion.")).toBeTruthy();
  });

  it("marks a deterministic reply as the server's words and shows its audit trail", () => {
    render(<LiveTranscript entries={[deterministicReply]} held={null} speaking={false} />);
    expect(screen.getByText(/spoken from a fixed template/i)).toBeTruthy();
    expect(screen.getByText(/quote.total · v3 · en-IN/)).toBeTruthy();
    // The server's own strings, printed as they arrived.
    expect(screen.getByText("₹395.00")).toBeTruthy();
    expect(screen.getByText("₹25.00")).toBeTruthy();
  });

  it("does not dress a model reply up as a server-rendered fact", () => {
    render(<LiveTranscript entries={[modelReply]} held={null} speaking={false} />);
    expect(screen.queryByText(/spoken from a fixed template/i)).toBeNull();
  });

  it("offers no control that could approve, pay, cancel or refund", () => {
    render(
      <LiveTranscript
        entries={[buyerTurn, deterministicReply, modelReply]}
        held={{ turnId: 4, text: "yes, go ahead" }}
        speaking
      />,
    );
    // A spoken "yes" is a sentence in a transcript and nothing more (19.11).
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryAllByRole("link")).toHaveLength(0);
    expect(screen.getByText(/approving and paying still happen on the store/i)).toBeTruthy();
  });

  it("says the reply was on screen before it was spoken", () => {
    render(<LiveTranscript entries={[deterministicReply]} held={null} speaking />);
    expect(screen.getByText(/on screen before it was spoken/i)).toBeTruthy();
  });
});
