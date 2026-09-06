/**
 * The RazorAI box after the AgentFlow live view: one dark scene, one conversation, one
 * composer, and a composer edge that says what the assistant is doing.
 *
 * Three groups. The pure derivation first, because the order of its rules is a decision
 * (a reply owed outranks an open microphone, or "thinking" would never show while the
 * mic stays live). Then the surface, driven by a fake socket, to show the edge follows
 * the session rather than a timer, and that the written chat takes over the moment the
 * socket cannot carry a message. Then the box itself: the scene, the pill, the ghost
 * controls, exactly one log and one text box at any moment, and the spoken yes still
 * reaching the basket and the checkout through the same client calls the shelf uses.
 */
import { StrictMode } from "react";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RazorAIPanel } from "@/features/agent/razorai-panel";

import { initialTranscriptState, type TranscriptEntry } from "../transcript";
import {
  VoicePanel,
  awaitingReply,
  deriveVoiceState,
  glowFor,
  type VoiceSurfaceState,
} from "../voice-panel";
import type { ServerFrame } from "../wire";

import { fakeAudio, fakeSocketFactory, Recorder, sessionReady, settle } from "./fakes";

const mocks = vi.hoisted(() => ({
  api: {
    createBasket: vi.fn(),
    setLine: vi.fn(),
    openCheckout: vi.fn(),
    agentTurn: vi.fn(),
  },
  context: {
    basketId: null as string | null,
    lineCount: 0,
    itemCount: 0,
    totalMinor: null as number | null,
    currency: "INR",
    setBasketId: vi.fn(),
    setLineCount: vi.fn(),
    refresh: vi.fn(async () => {}),
  },
}));

vi.mock("@/lib/api/client", () => ({
  api: mocks.api,
  newIdempotencyKey: () => "key-1",
}));
vi.mock("@/components/providers", () => ({
  useBasketContext: () => mocks.context,
}));

beforeEach(() => {
  // jsdom has no `matchMedia`; the box's phone scroll-lock reads it.
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: (query: string): MediaQueryList =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }) as MediaQueryList,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mocks.context.basketId = null;
});

/* ------------------------------------------------------------------ the derivation */

const buyer: TranscriptEntry = {
  kind: "buyer",
  id: "b1",
  seq: 1,
  turnId: 1,
  text: "two kilos of onions",
  stale: false,
  source: "voice",
};
const assistant: TranscriptEntry = {
  kind: "assistant",
  id: "a2",
  seq: 2,
  turnId: 1,
  text: "Red onions are cheaper today.",
  deterministic: false,
  locale: "en-IN",
  speechGeneration: 1,
  templateId: null,
  templateVersion: null,
  fields: null,
  items: null,
};

function session(
  overrides: Partial<Parameters<typeof deriveVoiceState>[0]> & {
    entries?: TranscriptEntry[];
    held?: string;
    speaking?: boolean;
  } = {},
) {
  const { entries = [], held, speaking = false, ...rest } = overrides;
  return {
    connection: "open" as const,
    reconnectAttempts: 0,
    transmitting: true,
    mic: "live" as const,
    transcript: {
      ...initialTranscriptState,
      entries,
      speaking,
      held: held === undefined ? null : { turnId: 9, text: held },
    },
    ...rest,
  };
}

describe("deriveVoiceState", () => {
  it("is connecting on the first attempt, and text mode inside a reconnect cycle", () => {
    expect(deriveVoiceState(session({ connection: "connecting" }), false)).toEqual({
      live: true,
      phase: "connecting",
    });
    expect(
      deriveVoiceState(session({ connection: "reconnecting", reconnectAttempts: 1 }), false),
    ).toEqual({ live: false, phase: "text" });
    expect(
      deriveVoiceState(session({ connection: "connecting", reconnectAttempts: 2 }), false),
    ).toEqual({ live: false, phase: "text" });
    expect(deriveVoiceState(session({ connection: "closed" }), true)).toEqual({
      live: false,
      phase: "thinking",
    });
  });

  it("puts the assistant's own voice first, then a reply owed, then the open microphone", () => {
    expect(deriveVoiceState(session({ speaking: true, entries: [buyer] }), false).phase).toBe(
      "speaking",
    );
    expect(deriveVoiceState(session({ entries: [buyer] }), false).phase).toBe("thinking");
    expect(deriveVoiceState(session({ entries: [buyer, assistant] }), false).phase).toBe(
      "listening",
    );
    expect(
      deriveVoiceState(session({ entries: [buyer, assistant], transmitting: false }), false).phase,
    ).toBe("idle");
  });

  it("stays listening while the buyer is mid-sentence, even with a reply owed", () => {
    expect(deriveVoiceState(session({ entries: [buyer], held: "and also" }), false).phase).toBe(
      "listening",
    );
  });

  it("does not claim to listen through a microphone the browser refused", () => {
    // The session leaves `transmitting` set when the mic is denied or fails -- nothing
    // else turns it off -- so the derivation must read the mic itself. A refused mic is
    // idle: the buyer can type, and the pill says no more than that.
    for (const mic of ["denied", "failed"] as const) {
      expect(deriveVoiceState(session({ mic }), false)).toEqual({ live: true, phase: "idle" });
      expect(glowFor(deriveVoiceState(session({ mic }), false).phase)).toBeNull();
      // A reply owed still shows as thinking, and the assistant's own voice still wins.
      expect(deriveVoiceState(session({ mic, entries: [buyer] }), false).phase).toBe("thinking");
      expect(deriveVoiceState(session({ mic, speaking: true }), false).phase).toBe("speaking");
    }
    // Not yet live either: a mic that is still being opened has sent nothing.
    expect(deriveVoiceState(session({ mic: "starting" }), false).phase).toBe("idle");
  });

  it("does not wait on a stale final: the agent never saw it", () => {
    expect(awaitingReply([buyer, assistant, { ...buyer, id: "b3", seq: 3, stale: true }])).toBe(
      false,
    );
    expect(awaitingReply([assistant, buyer])).toBe(true);
    expect(awaitingReply([])).toBe(false);
  });

  it("maps the phases onto the reference's two sweeps and one hairline", () => {
    expect(glowFor("thinking")).toBe("thinking");
    expect(glowFor("speaking")).toBe("executing");
    expect(glowFor("listening")).toBe("listening");
    expect(glowFor("idle")).toBeNull();
    expect(glowFor("connecting")).toBeNull();
    expect(glowFor("text")).toBeNull();
  });
});

/* --------------------------------------------------------------------- the surface */

function fakes() {
  const recorder = new Recorder();
  const { connect, sockets } = fakeSocketFactory(recorder);
  const audio = fakeAudio(recorder);
  return { recorder, connect, sockets, openAudio: async () => audio.io };
}

async function opened(sockets: ReturnType<typeof fakes>["sockets"]) {
  await act(async () => {
    await settle();
  });
  await act(async () => {
    sockets[0].open();
    sockets[0].deliver(sessionReady());
    await settle();
  });
  return async (frame: ServerFrame) => {
    await act(async () => {
      sockets[0].deliver(frame);
      await settle();
    });
  };
}

const REPLY: ServerFrame = {
  type: "agent_reply",
  text: "Red onions are cheaper today.",
  deterministic: false,
  locale: "en-IN",
  turn_id: 1,
  speech_generation: 1,
  template_id: null,
  template_version: null,
  fields: null,
};

const FINAL: ServerFrame = {
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
};

function glowClasses(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll(".edge-glow, .edge-glow-ring")).map(
    (node) => node.className,
  );
}

describe("VoicePanel, the composer's edge", () => {
  it("holds a cyan hairline while listening, sweeps while a reply is owed, and again while it is spoken", async () => {
    const { connect, sockets, openAudio } = fakes();
    const { container } = render(
      <VoicePanel url="wss://storefront.test/api/voice/stream" connect={connect} openAudio={openAudio} />,
    );
    const deliver = await opened(sockets);

    // Listening: the ring only, cyan, no blurred sweep behind the bar.
    expect(glowClasses(container)).toEqual(["edge-glow-ring edge-glow-listening"]);
    expect(
      screen.getByRole("button", { name: /mute the microphone/i }).querySelector("svg")?.getAttribute("class"),
    ).toContain("animate-pulse");

    // A settled buyer turn with nothing back yet: thinking, the violet-cyan sweep.
    await deliver(FINAL);
    expect(glowClasses(container)).toEqual([
      "edge-glow edge-glow-thinking",
      "edge-glow-ring edge-glow-thinking",
    ]);

    // The reply lands: nothing owed, the mic is open again.
    await deliver(REPLY);
    expect(glowClasses(container)).toEqual(["edge-glow-ring edge-glow-listening"]);

    // She speaks it: the indigo-orange sweep, and the mic pauses itself.
    await deliver({ type: "speech_start", speech_generation: 1 });
    expect(glowClasses(container)).toEqual([
      "edge-glow edge-glow-executing",
      "edge-glow-ring edge-glow-executing",
    ]);
    await deliver({ type: "speech_end", speech_generation: 1, chunks: 0, cancelled: false });
    expect(glowClasses(container)).toEqual(["edge-glow-ring edge-glow-listening"]);
  });

  it("draws no listening ring, and no pulse, over a microphone the browser refused", async () => {
    const { connect, sockets, openAudio } = fakes();
    // The same fake sound card, except that asking for the microphone is refused the way
    // a browser refuses it: a `NotAllowedError`.
    const refused = async () => {
      const io = await openAudio();
      return {
        ...io,
        startMic: async () => {
          throw new DOMException("Permission denied", "NotAllowedError");
        },
      };
    };
    const states: VoiceSurfaceState[] = [];
    const { container } = render(
      <VoicePanel
        url="wss://storefront.test/api/voice/stream"
        connect={connect}
        openAudio={refused}
        onStateChange={(state) => states.push(state)}
      />,
    );
    await opened(sockets);

    // The socket is open and the session still counts itself as transmitting, but there
    // is no microphone: the surface is idle, not listening.
    expect(states[states.length - 1]).toEqual({ live: true, phase: "idle" });
    expect(container.querySelector("section")?.getAttribute("data-voice-phase")).toBe("idle");
    expect(glowClasses(container)).toEqual([]);
    const mic = screen.getByRole("button", { name: /microphone unavailable/i });
    expect(mic.hasAttribute("disabled")).toBe(true);
    expect(mic.className).not.toContain("bg-accent");
    expect(mic.querySelector("svg")?.getAttribute("class")).not.toContain("animate-pulse");
    // The refusal is said out loud, and the typing path is still there.
    expect(screen.getByText("This site cannot use your microphone")).toBeTruthy();
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
  });

  it("is one composer in the reference's shape: the round mic first, then the field, then send", async () => {
    const { connect, sockets, openAudio } = fakes();
    const { container } = render(
      <VoicePanel url="wss://storefront.test/api/voice/stream" connect={connect} openAudio={openAudio} />,
    );
    await opened(sockets);

    const forms = container.querySelectorAll("form");
    expect(forms).toHaveLength(1);
    const form = forms[0];
    expect(form.className).toContain("bg-[#11141F]");
    expect(form.className).toContain("rounded-2xl");
    const controls = Array.from(form.querySelectorAll("button, input"));
    expect(controls.map((node) => node.tagName)).toEqual(["BUTTON", "INPUT", "BUTTON"]);
    expect(controls[0].getAttribute("aria-label")).toMatch(/mute the microphone/i);
    expect(controls[0].className).toContain("h-11 w-11");
    expect(controls[0].className).toContain("rounded-full");
    expect(controls[0].className).toContain("bg-accent");
    expect(controls[2].getAttribute("aria-label")).toBe("Send to RazorAI");
    expect(controls[2].className).toContain("bg-primary");
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
  });

  it("starts a stopped session from the mic, and mutes a running one", async () => {
    const { connect, sockets, openAudio } = fakes();
    render(
      <VoicePanel url="wss://storefront.test/api/voice/stream" connect={connect} openAudio={openAudio} />,
    );
    // Before the zero-delay auto-start fires, nothing is running: the mic is the start.
    expect(sockets).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: /start voice/i }));
    expect(sockets).toHaveLength(1);

    await opened(sockets);
    const mic = screen.getByRole("button", { name: /mute the microphone/i });
    expect(mic.getAttribute("aria-pressed")).toBe("true");
    expect(mic.className).toContain("bg-accent");
    fireEvent.click(mic);
    expect(mic.getAttribute("aria-pressed")).toBe("false");
    expect(mic.getAttribute("aria-label")).toMatch(/unmute/i);
    expect(mic.className).not.toContain("bg-accent");
  });

  it("hands the conversation to the written chat while the socket is down, and sends there", async () => {
    const { connect, sockets, openAudio } = fakes();
    const onSendText = vi.fn();
    const states: VoiceSurfaceState[] = [];
    render(
      <VoicePanel
        url="wss://storefront.test/api/voice/stream"
        connect={connect}
        openAudio={openAudio}
        onSendText={onSendText}
        onStateChange={(state) => states.push(state)}
      >
        <p>the written chat</p>
      </VoicePanel>,
    );
    await opened(sockets);
    // Live: the voice log carries the conversation and the written chat is not drawn.
    expect(screen.getByRole("log", { name: /voice conversation/i })).toBeTruthy();
    expect(screen.queryByText("the written chat")).toBeNull();

    // The gateway drops: a reconnect cycle begins, and the written chat takes over.
    await act(async () => {
      sockets[0].serverClose();
      await settle();
    });
    expect(screen.queryByRole("log", { name: /voice conversation/i })).toBeNull();
    expect(screen.getByText("the written chat")).toBeTruthy();
    expect(states[states.length - 1]).toEqual({ live: false, phase: "text" });
    // The drop is said out loud, not swallowed.
    expect(screen.getByText(/the voice connection dropped/i)).toBeTruthy();

    const input = screen.getByLabelText(/message razorai/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "two kilos of onions" } });
    fireEvent.click(screen.getByRole("button", { name: /send to razorai/i }));
    expect(onSendText).toHaveBeenCalledWith("two kilos of onions");
    expect(input.value).toBe("");
  });

  it("glows as thinking while a typed turn is in flight on the written path", async () => {
    const { connect, openAudio } = fakes();
    const { container } = render(
      <VoicePanel
        url="wss://storefront.test/api/voice/stream"
        connect={connect}
        openAudio={openAudio}
        onSendText={() => {}}
        textPending
      >
        <p>the written chat</p>
      </VoicePanel>,
    );
    // Not yet started: the written chat carries it, and the pending turn is the glow.
    expect(glowClasses(container)).toEqual([
      "edge-glow edge-glow-thinking",
      "edge-glow-ring edge-glow-thinking",
    ]);
    expect(screen.getByRole("button", { name: "Send to RazorAI" }).hasAttribute("disabled")).toBe(
      true,
    );
  });
});

describe("VoicePanel, opening itself", () => {
  it("opens the session on its own, even after StrictMode's simulated unmount has stopped it once", async () => {
    // Next runs the storefront with `reactStrictMode: true`. Development StrictMode mounts,
    // unmounts and remounts every effect once; the unmount runs the hook's cleanup, which
    // stops the session, and a surface that only opened itself from `idle` would then sit
    // stopped for the whole visit -- with a "Start voice" control the buyer was never
    // meant to need.
    const { connect, sockets, openAudio } = fakes();
    const states: VoiceSurfaceState[] = [];
    render(
      <StrictMode>
        <VoicePanel
          url="wss://storefront.test/api/voice/stream"
          connect={connect}
          openAudio={openAudio}
          onStateChange={(state) => states.push(state)}
        />
      </StrictMode>,
    );
    await act(async () => {
      await settle();
    });
    expect(sockets.length).toBeGreaterThanOrEqual(1);
    expect(sockets[sockets.length - 1].closed).toBe(false);
    expect(states[states.length - 1]).toEqual({ live: true, phase: "connecting" });
  });
});

/* ------------------------------------------------------------------------- the box */

function pill(): HTMLElement {
  return screen.getByRole("group", { name: "RazorAI status" });
}

describe("RazorAIPanel, the scene", () => {
  it("draws the dark scene: the ground glow, the mono pill cluster and the ghost controls", async () => {
    const { connect, sockets, openAudio } = fakes();
    render(
      <RazorAIPanel
        open
        onClose={() => {}}
        voiceOptions={{ url: "wss://storefront.test/api/voice/stream", connect, openAudio }}
      />,
    );
    await opened(sockets);

    const dialog = screen.getByRole("dialog", { name: /razorai/i });
    for (const cls of ["bg-[#05070E]/88", "backdrop-blur-xl", "rounded-[24px]", "text-slate-400"]) {
      expect(dialog.className).toContain(cls);
    }
    const ground = dialog.querySelector('[data-testid="razorai-ground"]');
    expect(ground?.getAttribute("aria-hidden")).toBe("true");
    expect(ground?.className).toContain("pointer-events-none absolute inset-0");

    expect(pill().className).toContain("font-mono");
    expect(pill().className).toContain("rounded-full border border-white/15");
    expect(pill().textContent).toContain("RazorAI");
    expect(pill().textContent).toContain("listening");
    expect(dialog.getAttribute("data-ai-state")).toBe("listening");

    for (const name of ["Dock RazorAI to the side", "Close RazorAI"]) {
      const button = screen.getByRole("button", { name });
      expect(button.className).toContain("rounded-full border border-white/15");
    }
  });

  it("shows one conversation and one composer: the voice log while live, the written chat when the socket is down", async () => {
    const { connect, sockets, openAudio } = fakes();
    render(
      <RazorAIPanel
        open
        onClose={() => {}}
        voiceOptions={{ url: "wss://storefront.test/api/voice/stream", connect, openAudio }}
      />,
    );
    await opened(sockets);

    expect(screen.getAllByRole("log")).toHaveLength(1);
    expect(screen.getByRole("log", { name: /voice conversation/i })).toBeTruthy();
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(screen.queryByText(/I am RazorAI/)).toBeNull();

    await act(async () => {
      sockets[0].serverClose();
      await settle();
    });
    expect(screen.getAllByRole("log")).toHaveLength(1);
    expect(screen.getByRole("log", { name: /conversation with razorai/i })).toBeTruthy();
    expect(screen.getByText(/I am RazorAI/)).toBeTruthy();
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(pill().textContent).toContain("text mode");
    expect(screen.getByRole("dialog").getAttribute("data-ai-state")).toBe("text");
  });

  it("a spoken yes adds it once, and the only question left is the checkout one", async () => {
    mocks.api.createBasket.mockResolvedValue({ basket_id: "01a07202-1ba8-7297-a54d-5116246acf0f" });
    mocks.api.setLine.mockResolvedValue({});
    mocks.api.openCheckout.mockResolvedValue({ checkout_id: "01a07300-9c2b-7bd1-a10e-77f0e0e0e0e0" });
    const { connect, sockets, openAudio } = fakes();
    render(
      <RazorAIPanel
        open
        onClose={() => {}}
        voiceOptions={{ url: "wss://storefront.test/api/voice/stream", connect, openAudio }}
      />,
    );
    const deliver = await opened(sockets);

    await deliver({
      ...REPLY,
      text: "Amul milk, one litre, is ₹68. Shall I add it?",
      offer: { sku: "AMUL-DAIRY-001", name: "Amul milk", quantity: 1, unit_price: null },
    });
    await deliver({ ...FINAL, text: "yes add two", turn_id: 2 });

    // The spoken yes is heard, carries its count, and DOES the add -- one question, one
    // answer. RazorAI had already named the item and its price and asked; a slip repeating
    // that with buttons was a second confirmation for a step that charges nothing and can be
    // undone from the cart. The confirmations that survive are the ones about money.
    await waitFor(() => expect(mocks.api.setLine).toHaveBeenCalledTimes(1));
    expect(mocks.api.setLine).toHaveBeenCalledWith(
      "01a07202-1ba8-7297-a54d-5116246acf0f",
      "AMUL-DAIRY-001",
      2,
    );
    // Allowing the add does not also open a checkout: that is the next question, not a
    // consequence of this answer.
    expect(mocks.api.openCheckout).not.toHaveBeenCalled();

    // ...and it is asked. Allowing it opens the checkout and keeps it in this box: nothing
    // navigates, which is why the approval can happen on this session's own microphone.
    const second = await waitFor(() => screen.getByRole("group", { name: "Permission request" }));
    fireEvent.click(within(second).getByRole("button", { name: "Confirm checkout" }));
    await waitFor(() => expect(mocks.api.openCheckout).toHaveBeenCalledTimes(1));
    expect(mocks.api.openCheckout).toHaveBeenCalledWith("01a07202-1ba8-7297-a54d-5116246acf0f");
    expect(mocks.context.setBasketId).toHaveBeenCalledWith(null);
  });

  it("a spoken no writes nothing, and takes the checkout question away with it", async () => {
    mocks.api.createBasket.mockResolvedValue({ basket_id: "01a07202-1ba8-7297-a54d-5116246acf0f" });
    const { connect, sockets, openAudio } = fakes();
    render(
      <RazorAIPanel
        open
        onClose={() => {}}
        voiceOptions={{ url: "wss://storefront.test/api/voice/stream", connect, openAudio }}
      />,
    );
    const deliver = await opened(sockets);

    await deliver({
      ...REPLY,
      text: "Amul milk, one litre, is ₹68. Shall I add it?",
      offer: { sku: "AMUL-DAIRY-001", name: "Amul milk", quantity: 1, unit_price: null },
    });
    // A no BEFORE any yes: nothing was ever offered to write, so nothing is written and no
    // question is left standing. The asymmetry `isNegative` documents is what makes this
    // safe -- a no is read eagerly, because the cost of over-reading one is a permission the
    // buyer asks for again, while the cost of missing one is a slip that ignores them.
    await deliver({ ...FINAL, text: "nahi", turn_id: 2 });

    await waitFor(() =>
      expect(screen.queryByRole("group", { name: "Permission request" })).toBeNull(),
    );
    expect(mocks.api.setLine).not.toHaveBeenCalled();
    expect(mocks.api.openCheckout).not.toHaveBeenCalled();
  });
});
