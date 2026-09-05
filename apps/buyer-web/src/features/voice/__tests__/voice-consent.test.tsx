/**
 * The voice path presses the button, and only the button, and only for the card on screen.
 *
 * Driven with the fake socket, so every frame the gateway can send is delivered by hand
 * and every frame this component sends is recorded. The two properties that matter are
 * asserted in both directions: `onApprove` fires exactly once for a recognised yes whose
 * five fields match the card, and never for a mismatch, a busy button, a replayed frame,
 * a no, a near miss, or a window that closed.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApprovalCard } from "@/lib/api/types";

import { VoiceConsent, describeMismatch } from "../voice-consent";
import type { ConsentRecognised, ServerFrame } from "../wire";

import { fakeAudio, fakeSocketFactory, Recorder, sessionReady, settle } from "./fakes";

afterEach(cleanup);

const CARD: ApprovalCard = {
  checkout_id: "01a07169-ead6-7052-99d3-d017e36c0c93",
  version: 1,
  content_hash: "JT_-aFMjqUznsunI3AO60E4wbBpH3pDSH9s9qRCykyg",
  policy_receipt_id: "01a07169-eb07-743c-8df5-5fcd70e77802",
  policy_receipt_hash: "6KAVSh-tCcQUakdYCLGh-C2Ryo5kjmh08XEz9TnzT2s",
  amount_minor: 60863,
  currency: "INR",
  total: { minor: 60863, currency: "INR", display: "608.63" },
  expires_at: "2026-09-05T12:07:41.933575Z",
  reservation: null,
  quote: null,
  previous_version: null,
  deltas: [],
};

const READING =
  "Version 1, ₹608.63, six hundred eight rupees and sixty-three paise. Say yes to approve this exact version, or no to decline.";

function cardRead(overrides: Partial<Extract<ServerFrame, { type: "card_read" }>> = {}): ServerFrame {
  return {
    type: "card_read",
    checkout_id: CARD.checkout_id,
    version: CARD.version,
    content_hash: CARD.content_hash,
    amount_minor: CARD.amount_minor,
    currency: CARD.currency,
    locale: "en-IN",
    template_id: "consent.read_card",
    template_version: 1,
    speech_generation: 0,
    ...overrides,
  };
}

function reply(): ServerFrame {
  return {
    type: "agent_reply",
    text: READING,
    deterministic: true,
    locale: "en-IN",
    turn_id: -1,
    speech_generation: 0,
    template_id: "consent.read_card",
    template_version: 1,
    fields: { version: "1" },
  };
}

function listening(consentId = "c1"): ServerFrame {
  return { type: "consent_listening", consent_id: consentId, closes_in_s: 10, speech_generation: 0 };
}

function recognised(overrides: Partial<ConsentRecognised> = {}): ConsentRecognised {
  return {
    type: "consent_recognised",
    consent_id: "c1",
    checkout_id: CARD.checkout_id,
    version: CARD.version,
    content_hash: CARD.content_hash,
    amount_minor: CARD.amount_minor,
    currency: CARD.currency,
    heard: "yes",
    turn_id: 0,
    stt_generation: 1,
    offset_ms: 2400,
    recorded: false,
    voice_is_authority: false,
    ...overrides,
  };
}

async function mounted(props: { busy?: "approve" | "reject" | null; card?: ApprovalCard } = {}) {
  const recorder = new Recorder();
  const { connect, sockets } = fakeSocketFactory(recorder);
  const audio = fakeAudio(recorder);
  const onApprove = vi.fn();
  // The same three session props on every render, so the hook keeps one session.
  const openAudio = async () => audio.io;
  const url = "wss://storefront.test/api/voice/stream";
  const element = (busy: "approve" | "reject" | null) => (
    <VoiceConsent
      card={props.card ?? CARD}
      busy={busy}
      onApprove={onApprove}
      url={url}
      connect={connect}
      openAudio={openAudio}
    />
  );
  const view = render(element(props.busy ?? null));
  /** The parent's button went busy (a press in flight) after the reading started. */
  const setBusy = (busy: "approve" | "reject" | null) => {
    view.rerender(element(busy));
  };

  const deliver = async (frame: ServerFrame) => {
    await act(async () => {
      sockets[0].deliver(frame);
      await settle();
    });
  };

  /** Press the offer, open the socket, and let `read_card` go out. */
  const ask = async () => {
    fireEvent.click(screen.getByRole("button", { name: /say it aloud/i }));
    await act(async () => {
      sockets[0].open();
      sockets[0].deliver(sessionReady());
      await settle();
    });
  };

  /** A reading delivered in the gateway's order: text, binding fields, then the window. */
  const readAndListen = async (consentId = "c1") => {
    await deliver(reply());
    await deliver(cardRead());
    await deliver(listening(consentId));
  };

  return { recorder, sockets, audio, onApprove, deliver, ask, readAndListen, setBusy };
}

describe("asking for the reading", () => {
  it("sends read_card naming the checkout and version on screen, and nothing about its bytes", async () => {
    const { ask, recorder } = await mounted();
    await ask();
    const sent = recorder.sentFrames().filter((frame) => frame.type === "read_card");
    expect(sent).toEqual([
      { type: "read_card", checkout_id: CARD.checkout_id, version: 1, locale: "en-IN" },
    ]);
    for (const frame of sent) {
      expect(frame).not.toHaveProperty("content_hash");
      expect(frame).not.toHaveProperty("amount_minor");
    }
  });

  it("prints the reading the moment its text arrives, before any window opens", async () => {
    const { ask, deliver } = await mounted();
    await ask();
    await deliver(reply());
    await deliver(cardRead());
    expect(screen.getByText(READING, { exact: false })).toBeTruthy();
    expect(screen.queryByText(/listening for yes or no/i)).toBeNull();
  });

  it("opens the microphone only while the window is open, and closes it after", async () => {
    const { ask, readAndListen, deliver, audio, recorder } = await mounted();
    await ask();
    await act(async () => {
      audio.frame();
    });
    expect(recorder.shape.filter((s) => s.startsWith("send:")).pop()).toBe("send:silence");

    await readAndListen();
    expect(screen.getByText(/listening for yes or no/i)).toBeTruthy();
    await act(async () => {
      audio.frame();
    });
    expect(recorder.shape.filter((s) => s.startsWith("send:")).pop()).toBe("send:audio");

    await deliver({ type: "consent_closed", consent_id: "c1", reason: "expired" });
    await act(async () => {
      audio.frame();
    });
    expect(recorder.shape.filter((s) => s.startsWith("send:")).pop()).toBe("send:silence");
    expect(screen.getByText(/no clear yes or no in time/i)).toBeTruthy();
  });
});

describe("a recognised yes", () => {
  it("presses the button exactly once when every binding field matches the card on screen", async () => {
    const { ask, readAndListen, deliver, onApprove } = await mounted();
    await ask();
    await readAndListen();
    await deliver(recognised());
    await deliver({ type: "consent_closed", consent_id: "c1", reason: "recognised" });

    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/heard “yes” for version 1, ₹608.63/i)).toBeTruthy();
    expect(screen.getByText(/sending your approval exactly as the button would/i)).toBeTruthy();
  });

  it("does nothing for the same consent id delivered twice", async () => {
    const { ask, readAndListen, deliver, onApprove } = await mounted();
    await ask();
    await readAndListen();
    await deliver(recognised());
    await deliver(recognised());
    expect(onApprove).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["version", { version: 2 }, /you heard version 2; this screen shows version 1/i],
    ["hash", { content_hash: "different-bytes-entirely" }, /this screen shows hash JT_-aFMjqUzn/i],
    ["amount", { amount_minor: 60864 }, /you heard ₹608.64; this screen shows ₹608.63/i],
    ["checkout", { checkout_id: "01a07169-0000-7052-99d3-d017e36c0c93" }, /different checkout/i],
  ])("never presses the button when the %s the gateway read differs from the screen", async (_, delta, sentence) => {
    const { ask, readAndListen, deliver, onApprove } = await mounted();
    await ask();
    await readAndListen();
    await deliver(recognised(delta));
    expect(onApprove).not.toHaveBeenCalled();
    expect(screen.getByText(sentence)).toBeTruthy();
    expect(screen.getByText(/nothing was sent/i)).toBeTruthy();
  });

  it("never presses the button while another action is in flight", async () => {
    const { ask, readAndListen, deliver, onApprove, setBusy } = await mounted();
    await ask();
    await readAndListen();
    // The buyer pressed Reject with the window open; the press is in flight.
    setBusy("reject");
    await deliver(recognised());
    expect(onApprove).not.toHaveBeenCalled();
    expect(screen.getByText(/another action was already in flight/i)).toBeTruthy();
  });

  it("offers no reading at all while a press is in flight", async () => {
    await mounted({ busy: "approve" });
    expect((screen.getByRole("button", { name: /say it aloud/i }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("every other outcome is a sentence", () => {
  it("a no records nothing and leaves Reject as a press", async () => {
    const { ask, readAndListen, deliver, onApprove } = await mounted();
    await ask();
    await readAndListen();
    await deliver({ type: "consent_declined", consent_id: "c1", heard: "nahi" });
    await deliver({ type: "consent_closed", consent_id: "c1", reason: "declined" });
    expect(onApprove).not.toHaveBeenCalled();
    expect(screen.getByText(/you said “nahi”\. nothing was recorded/i)).toBeTruthy();
    expect(screen.getByText(/use reject to release it/i)).toBeTruthy();
  });

  it("a near miss is shown and the window stays open", async () => {
    const { ask, readAndListen, deliver, onApprove } = await mounted();
    await ask();
    await readAndListen();
    await deliver({
      type: "consent_unrecognised",
      consent_id: "c1",
      text: "yes please do it now",
      reason: "not_in_lexicon",
    });
    expect(screen.getByText(/heard “yes please do it now” — not a yes or a no/i)).toBeTruthy();
    expect(screen.getByText(/listening for yes or no/i)).toBeTruthy();
    await deliver({
      type: "consent_unrecognised",
      consent_id: "c1",
      text: "yes",
      reason: "began_before_reading_ended",
    });
    expect(screen.getByText(/started speaking before the amount was read/i)).toBeTruthy();
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("a window the buyer spoke over is said so", async () => {
    const { ask, readAndListen, deliver } = await mounted();
    await ask();
    await readAndListen();
    await deliver({ type: "consent_closed", consent_id: "c1", reason: "barge_in" });
    expect(screen.getByText(/you spoke over the reading/i)).toBeTruthy();
    expect(screen.queryByText(/listening for yes or no/i)).toBeNull();
  });

  it("a card the store would not give is a visible refusal, and the button still works", async () => {
    const { ask, deliver, onApprove } = await mounted();
    await ask();
    await deliver({
      type: "degradation",
      kind: "card_unavailable",
      message: "The approval card could not be read: the server holds version 2.",
      text_input_available: true,
      transaction_state_changed: false,
    });
    expect(screen.getByText(/voice approval is unavailable/i)).toBeTruthy();
    expect(screen.getByText(/the server holds version 2/i)).toBeTruthy();
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("a yes for a window that is not the open one is dropped before it is compared", async () => {
    // The gateway supersedes the old window before reading again and never sends a word
    // for a closed one, so a recognised frame naming the old id can only be a replay or
    // page script. The reducer folds consent frames for the window it is listening on
    // and no other. Before that rule existed, this test pressed the button twice.
    const { ask, readAndListen, deliver, onApprove } = await mounted();
    await ask();
    await readAndListen("c1");
    await deliver({ type: "consent_closed", consent_id: "c1", reason: "superseded" });
    await readAndListen("c2");
    await deliver(recognised({ consent_id: "c2" }));
    expect(onApprove).toHaveBeenCalledTimes(1);
    await deliver(recognised({ consent_id: "c1" }));
    expect(onApprove).toHaveBeenCalledTimes(1);
  });
});

describe("describeMismatch", () => {
  it("is null only when all five fields agree", () => {
    expect(describeMismatch(recognised(), CARD)).toBeNull();
    expect(describeMismatch(recognised({ currency: "USD" }), CARD)).toMatch(/you heard/i);
  });
});
