/**
 * Every degraded path produces something a buyer can read.
 *
 * The loop over `DEGRADATION_KINDS` is the test that matters: it walks the union from the
 * wire contract itself, so a kind added to `frames.py` and mirrored into `wire.ts` fails
 * here if nobody wrote copy for it. Silent degradation is a defect (19.12), and a card
 * that renders as an empty amber box is silent degradation with extra steps.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ClientNoticeCard, DegradedNotice } from "../degraded-notice";
import type { DegradationNotice } from "../transcript";
import { DEGRADATION_KINDS, type DegradationKind } from "../wire";

afterEach(cleanup);

function notice(kind: DegradationKind): DegradationNotice {
  return {
    id: `d-${kind}`,
    seq: 1,
    kind,
    message: `server said: ${kind}`,
    textInputAvailable: true,
    transactionStateChanged: false,
  };
}

describe("DegradedNotice", () => {
  it.each(DEGRADATION_KINDS)("renders visible copy for %s", (kind) => {
    render(<DegradedNotice notices={[notice(kind)]} />);
    const card = screen.getByRole("status");

    // A title and a body, both real sentences, not a kind echoed back at the buyer.
    const text = card.textContent ?? "";
    expect(text.length).toBeGreaterThan(80);
    expect(text).not.toBe(kind);

    // The two money invariants, rendered from the frame's own literal fields.
    expect(within(card).getByText(/you can still type/i)).toBeTruthy();
    expect(
      within(card).getByText(/nothing about your basket, checkout, order or payment changed/i),
    ).toBeTruthy();

    // The server's own words are kept for whoever is reading over the buyer's shoulder.
    expect(text).toContain(kind);
  });

  it("renders nothing when there is nothing wrong", () => {
    const { container } = render(<DegradedNotice notices={[]} />);
    expect(container.textContent).toBe("");
  });

  it("shows one card per degradation", () => {
    render(<DegradedNotice notices={[notice("tts_failed"), notice("stt_unavailable")]} />);
    expect(screen.getAllByRole("status")).toHaveLength(2);
  });

  it("tells the buyer the reply is still complete when only the voice failed", () => {
    render(<DegradedNotice notices={[notice("tts_failed")]} />);
    expect(screen.getByText(/on screen in full/i)).toBeTruthy();
  });

  it("says money facts are unaffected when the model is the thing that failed", () => {
    render(<DegradedNotice notices={[notice("reasoning_failed")]} />);
    expect(screen.getByText(/never came from the model/i)).toBeTruthy();
  });
});

describe("ClientNoticeCard", () => {
  it("renders a dropped connection in the same voice as a server degradation", () => {
    render(
      <ClientNoticeCard
        notice={{ kind: "connection_lost", detail: "attempt 2" }}
        connection="reconnecting"
      />,
    );
    const card = screen.getByRole("status");
    expect(within(card).getByText(/you can still type/i)).toBeTruthy();
    expect(within(card).getByText(/being re-established/i)).toBeTruthy();
  });

  it("renders a refused microphone without blaming the buyer for it", () => {
    render(
      <ClientNoticeCard
        notice={{ kind: "microphone_denied", detail: "NotAllowedError" }}
        connection="open"
      />,
    );
    expect(screen.getByText(/cannot use your microphone/i)).toBeTruthy();
  });

  it("renders nothing when the client has noticed nothing", () => {
    const { container } = render(<ClientNoticeCard notice={null} connection="open" />);
    expect(container.textContent).toBe("");
  });
});
