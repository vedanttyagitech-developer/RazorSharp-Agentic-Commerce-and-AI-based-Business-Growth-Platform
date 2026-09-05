/**
 * The voice surface, embedded: one conversation and one composer for the RazorAI box.
 *
 * A mouth and an ear, and nothing else. This component can send four things: microphone
 * audio, typed text, an interruption, and a report that its speakers went quiet. It holds
 * no capability of its own, and there is deliberately no control in it that approves,
 * pays, cancels or refunds -- not even a confirming one, not even for a reply that asks for
 * confirmation. A transcript is intent evidence, never authority evidence (19.11), and a
 * spoken "yes" here records no consent. Approving happens on the store's own pages, where
 * the buyer is looking at the exact version and hash they are consenting to.
 *
 * That is a rule about the drawing as much as about the code. A confirm button inside a
 * surface the agent also writes into is the confusion this whole submission exists to
 * refuse, and it would be no less dangerous for being disabled or for saying "voice only".
 *
 * There is exactly one conversation and one composer here, whichever path is carrying
 * them. While the socket is open (or opening for the first time) the conversation is the
 * voice transcript and the composer sends `text_input` frames; when the socket is down --
 * stopped, refused, or in a reconnect cycle -- the conversation is whatever the host
 * passes as `children` (the written chat, with its proposal cards) and the composer sends
 * through `onSendText`. The buyer never sees two transcripts or two send buttons for one
 * assistant, and never types into a box that has nowhere to deliver.
 *
 * The typing box is not a fallback bolted on for demos. It is what makes the degradation
 * copy true: every notice on this surface promises the buyer they can still type, and a
 * promise that depends on a control that is not there is worse than no promise.
 *
 * The component reports its derived state upward (`onStateChange`) so the host can draw
 * the pill and the glow from the same facts the composer's edge is drawn from. The order
 * of that derivation is deliberate: what the assistant is doing (speaking) outranks what
 * the buyer is doing (an open microphone) only when a reply is not owed; a microphone that
 * stays open while the reply is computed would otherwise hide "thinking" entirely.
 *
 * "Listening" is claimed only while a microphone is actually live. The session keeps
 * `transmitting` true when the browser refuses the microphone, because nothing else turns
 * it off, and a pill that read "listening" from that flag alone would be the edge claiming
 * something the session is not doing -- over a mic button that says the microphone is
 * unavailable. A denied or failed mic is `idle`: the buyer can type, and the surface says
 * no more than that.
 */
"use client";

import { useEffect, useRef, useState, type FormEvent, type ReactNode, type Ref } from "react";

import { cx } from "@/components/ui";

import { ClientNoticeCard, DegradedNotice } from "./degraded-notice";
import { LiveTranscript } from "./live-transcript";
import type { ConnectionState, MicState } from "./session";
import type { TranscriptEntry, VoiceTranscriptState } from "./transcript";
import {
  useVoiceSession,
  type UseVoiceSessionOptions,
  type VoiceSessionController,
} from "./use-voice-session";
import type { Offer, ReplyItem } from "./wire";

/* --------------------------------------------------------------------------- state */

/** What the surface is doing, as one word. `text` means the socket is not carrying it. */
export type VoicePhase = "idle" | "connecting" | "listening" | "thinking" | "speaking" | "text";

export interface VoiceSurfaceState {
  /** True while the socket carries the conversation; false while the written chat does. */
  live: boolean;
  phase: VoicePhase;
}

/** The reference's state colours, one per word. */
export const PHASE_COLOUR: Readonly<Record<VoicePhase, string>> = {
  idle: "#7C8FF5",
  connecting: "#93A5FF",
  listening: "#4FD9F2",
  thinking: "#B08CFF",
  speaking: "#FFA14D",
  text: "#94A3B8",
};

export const PHASE_LABEL: Readonly<Record<VoicePhase, string>> = {
  idle: "idle",
  connecting: "connecting",
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
  text: "text mode",
};

/** The composer edge: a spinning sweep for two of the phases, a still hairline for one. */
export type GlowMode = "thinking" | "executing" | "listening";

export function glowFor(phase: VoicePhase): GlowMode | null {
  switch (phase) {
    case "thinking":
      return "thinking";
    case "speaking":
      return "executing";
    case "listening":
      return "listening";
    default:
      return null;
  }
}

/**
 * Whether the socket is carrying the conversation.
 *
 * Open, or the first attempt to open. A reconnect cycle is not live: the session never
 * gives up retrying, so "reconnecting" can last as long as the gateway is down, and a
 * composer whose only path is a socket that is not there is a composer that cannot send.
 * The written chat carries the conversation until the socket is back.
 */
export function isLive(connection: ConnectionState, reconnectAttempts: number): boolean {
  return connection === "open" || (connection === "connecting" && reconnectAttempts === 0);
}

/**
 * A reply is owed: the newest settled turn is the buyer's. A stale final does not count --
 * the agent never saw it, so nothing is coming back for it.
 */
export function awaitingReply(entries: readonly TranscriptEntry[]): boolean {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.kind === "assistant") return false;
    if (!entry.stale) return true;
  }
  return false;
}

export function deriveVoiceState(
  voice: {
    connection: ConnectionState;
    reconnectAttempts: number;
    transmitting: boolean;
    mic: MicState;
    transcript: VoiceTranscriptState;
  },
  textPending: boolean,
): VoiceSurfaceState {
  const live = isLive(voice.connection, voice.reconnectAttempts);
  if (!live) return { live, phase: textPending ? "thinking" : "text" };
  if (voice.connection === "connecting") return { live, phase: "connecting" };
  const { transcript } = voice;
  if (transcript.speaking) return { live, phase: "speaking" };
  // Hearing the buyer: the surface is sending, and there is a live microphone to send
  // from. `transmitting` alone is not enough -- it stays set when the browser refuses the
  // microphone, and a denied mic sends nothing.
  const hearing = voice.transmitting && voice.mic === "live";
  // Mid-sentence: the recogniser is still revising what the buyer is saying.
  const midSentence = transcript.held !== null && transcript.held.text.length > 0;
  if (hearing && midSentence) return { live, phase: "listening" };
  if (awaitingReply(transcript.entries) || textPending) return { live, phase: "thinking" };
  if (hearing) return { live, phase: "listening" };
  return { live, phase: "idle" };
}

/* ----------------------------------------------------------------------------- glyphs */

function MicGlyph({ pulsing, crossed }: { pulsing: boolean; crossed: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cx("h-[18px] w-[18px]", pulsing && "motion-safe:animate-pulse")}
    >
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0" />
      <path d="M12 18v3" />
      {crossed ? <path d="M4 4l16 16" /> : null}
    </svg>
  );
}

function SendGlyph() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-[18px] w-[18px]"
    >
      <path d="M12 19V5" />
      <path d="M5 12l7-7 7 7" />
    </svg>
  );
}

/* -------------------------------------------------------------------------- component */

export interface VoicePanelProps extends UseVoiceSessionOptions {
  className?: string;
  /** The buyer said yes to the product the last reply put forward. Fires once per yes. */
  onAffirmed?: (offer: Offer) => void;
  /**
   * The buyer said no, outside a consent window. Fires once per refusal.
   *
   * Carries nothing: what it refuses is whatever the host has pending. A permission slip
   * waiting on screen is the caller that needs this -- without it a spoken refusal is
   * silence, and the slip sits there as though the buyer had not answered.
   */
  onDenied?: () => void;
  /** The written conversation, drawn while the socket is not carrying one. */
  children?: ReactNode;
  /** Where a typed message goes while the socket is not carrying it. */
  onSendText?: (text: string) => void;
  /** A typed turn is in flight on that path; the composer's edge says so. */
  textPending?: boolean;
  /** The derived state, whenever it changes, so the host can draw the pill and the glow. */
  onStateChange?: (state: VoiceSurfaceState) => void;
  /**
   * The shelf's own basket write, for the product cards a spoken reply drew.
   *
   * The panel does not own a basket and never will: this is the host's `basket.add`, the
   * same request the shelf sends, threaded down to the card the buyer presses.
   */
  onAdd?: (sku: string, item?: ReplyItem) => void;
  /** The sku that write is in flight for, so a card cannot be pressed twice. */
  busySku?: string | null;
  /**
   * Rendered between the conversation and the composer, outside the scrolling region.
   *
   * A slot rather than a `CartStrip` import: this panel owns a socket and a transcript and
   * has no business knowing what a basket is. The host holds the basket and hands the strip
   * down already wired.
   */
  beforeComposer?: ReactNode;
  /**
   * Hands this panel's live session controller to the host, once.
   *
   * The host needs it so that anything it embeds -- an approval card with spoken consent,
   * today -- speaks and listens through THIS socket rather than opening a second one. Called
   * a single time per mounted session: the controller identity is stable for the life of the
   * session, so a host that stores it in state is not re-rendered by this.
   */
  onSession?: (controller: VoiceSessionController) => void;
  /** The composer's input, for a host that focuses it when it opens. */
  inputRef?: Ref<HTMLInputElement>;
}

export function VoicePanel({
  className,
  onAffirmed,
  onDenied,
  children,
  onSendText,
  textPending = false,
  onStateChange,
  onAdd,
  busySku = null,
  beforeComposer,
  onSession,
  inputRef,
  ...sessionOptions
}: VoicePanelProps) {
  const voice = useVoiceSession(sessionOptions);
  const { transcript, connection } = voice;
  // Handed up once, guarded by a ref rather than by the dependency list. The controller
  // identity is stable for the session's life, so this fires on mount and not again -- and
  // the guard means a host that happens to pass a fresh closure each render still only
  // hears about the session one time.
  const announcedSession = useRef(false);
  useEffect(() => {
    if (announcedSession.current || !onSession) return;
    announcedSession.current = true;
    onSession(voice);
  }, [onSession, voice]);
  const [draft, setDraft] = useState("");
  const [micOn, setMicOn] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);

  const affirmedSeq = useRef(0);
  useEffect(() => {
    const affirmed = transcript.affirmed;
    if (!affirmed || affirmed.seq === affirmedSeq.current || !onAffirmed) return;
    affirmedSeq.current = affirmed.seq;
    onAffirmed(affirmed.offer);
  }, [transcript.affirmed, onAffirmed]);

  // The refusal, on the same once-per-utterance footing. Sequence-guarded the same way, so a
  // re-render cannot replay a "no" the host has already acted on.
  const deniedSeq = useRef(0);
  useEffect(() => {
    const denied = transcript.denied;
    if (!denied || denied.seq === deniedSeq.current || !onDenied) return;
    deniedSeq.current = denied.seq;
    onDenied();
  }, [transcript.denied, onDenied]);

  const { start, setTransmitting } = voice;
  // A conversation, not a walkie-talkie: the session opens itself and the microphone is
  // live from the first moment. It pauses on its own while RazorAI is speaking, so the
  // recogniser never hears the assistant's own voice, and the buyer can mute it.
  //
  // `closed` counts as well as `idle`. Nothing on this surface stops the session, so the
  // only way it is closed while still mounted is the hook's own unmount cleanup -- which
  // development StrictMode runs once, on purpose, before mounting for real. A surface
  // that only opened itself from `idle` sat stopped for the whole visit there.
  useEffect(() => {
    if (connection !== "idle" && connection !== "closed") return undefined;
    const timer = window.setTimeout(() => start(), 0);
    return () => window.clearTimeout(timer);
  }, [connection, start]);
  useEffect(() => {
    setTransmitting(connection === "open" && micOn && !transcript.speaking);
  }, [connection, micOn, transcript.speaking, setTransmitting]);

  const { live, phase } = deriveVoiceState(voice, textPending);
  // Reported through a ref so a host that passes a fresh arrow every render does not
  // re-fire the report every render -- which, when the host stores what it hears, is a loop.
  const report = useRef(onStateChange);
  useEffect(() => {
    report.current = onStateChange;
  }, [onStateChange]);
  useEffect(() => {
    report.current?.({ live, phase });
  }, [live, phase]);

  // Follow the conversation: keep the newest bubble in view, whichever path drew it.
  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [transcript.entries, transcript.held, children, textPending]);

  const running = connection !== "idle" && connection !== "closed";
  const blocked = voice.mic === "denied" || voice.mic === "failed";
  const canSend =
    draft.trim().length > 0 &&
    (live ? connection === "open" : onSendText !== undefined && !textPending);

  function submit(event: FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text) return;
    if (live) {
      if (voice.sendText(text)) setDraft("");
      return;
    }
    if (!onSendText || textPending) return;
    onSendText(text);
    setDraft("");
  }

  // One control for the microphone: it starts a stopped session, and mutes a running one.
  function pressMic() {
    if (!running) {
      setMicOn(true);
      voice.start();
      return;
    }
    setMicOn((on) => !on);
  }

  const micLabel = blocked
    ? "Microphone unavailable — type instead"
    : !running
      ? "Start voice"
      : micOn
        ? "Mute the microphone"
        : "Unmute the microphone";
  const micHot = running && micOn && !blocked;
  const glow = glowFor(phase);

  return (
    <section
      aria-label="Talk to RazorAI"
      data-voice-phase={phase}
      data-voice-live={live ? "true" : "false"}
      className={cx("flex h-full min-h-0 flex-col", className)}
    >
      <div
        ref={scrollRef}
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-1 py-3 [scrollbar-color:#334155_transparent] [scrollbar-width:thin]"
      >
        <ClientNoticeCard
          notice={voice.notice}
          connection={connection}
          onDismiss={voice.dismissNotice}
        />
        <DegradedNotice notices={transcript.degradations} onDismiss={voice.dismissDegradation} />

        {transcript.error ? (
          <p
            role="alert"
            className="rounded-lg border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs leading-relaxed text-slate-300"
          >
            <span className="font-semibold text-rose-300">
              The store refused this voice session.
            </span>{" "}
            {transcript.error.message}
            <span className="tnum mt-1 block font-mono text-[9px] text-slate-500">
              {transcript.error.code}
            </span>
          </p>
        ) : null}

        {live ? (
          <LiveTranscript
            entries={transcript.entries}
            held={transcript.held}
            speaking={transcript.speaking}
            onAdd={onAdd}
            busySku={busySku}
          />
        ) : (
          (children ?? (
            <p className="mt-6 text-center text-xs leading-relaxed text-slate-500">
              The voice connection is not carrying this conversation right now. Type below.
            </p>
          ))
        )}
      </div>

      {/* Whatever the host wants sitting between the conversation and the composer -- the
          live cart strip, today. It lives outside the scrolling region on purpose: a cart
          that scrolled away with the transcript would be a cart the buyer has to hunt for
          at the moment they want to check out. */}
      {beforeComposer ?? null}

      {/* The composer. Its edge animates while RazorAI thinks or speaks; a still cyan
          hairline says the microphone is open. */}
      <div className="shrink-0 pt-2">
        <div className="relative">
          {glow === "listening" ? (
            <div aria-hidden="true" className="edge-glow-ring edge-glow-listening" />
          ) : glow !== null ? (
            <>
              <div aria-hidden="true" className={`edge-glow edge-glow-${glow}`} />
              <div aria-hidden="true" className={`edge-glow-ring edge-glow-${glow}`} />
            </>
          ) : null}
          <form
            onSubmit={submit}
            className="relative flex items-center gap-2 rounded-2xl border border-white/12 bg-[#11141F] p-2 transition-colors focus-within:border-primary/60 focus-within:ring-2 focus-within:ring-primary/25"
          >
            <button
              type="button"
              onClick={pressMic}
              disabled={blocked}
              aria-pressed={running ? micOn : false}
              aria-label={micLabel}
              title={micLabel}
              className={cx(
                "flex h-11 w-11 shrink-0 items-center justify-center rounded-full transition-colors disabled:cursor-not-allowed",
                micHot
                  ? "bg-accent text-white"
                  : blocked
                    ? "border border-white/10 text-slate-600"
                    : "border border-white/15 text-slate-300 hover:border-white/30 hover:bg-white/[0.08] hover:text-white",
              )}
            >
              <MicGlyph pulsing={phase === "listening"} crossed={blocked || (running && !micOn)} />
            </button>

            <label htmlFor="razorai-composer" className="sr-only">
              Message RazorAI
            </label>
            <input
              id="razorai-composer"
              ref={inputRef}
              type="text"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Ask for something, or say what you need"
              autoComplete="off"
              maxLength={4000}
              className="h-11 min-w-0 flex-1 bg-transparent px-1 text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none"
            />

            <button
              type="submit"
              disabled={!canSend}
              aria-label="Send to RazorAI"
              title="Send (Enter)"
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-primary text-white transition-colors hover:bg-[#3730a3] disabled:cursor-not-allowed disabled:opacity-40"
            >
              <SendGlyph />
            </button>
          </form>
        </div>
        <p className="mt-2 px-1 text-center text-[11px] leading-relaxed text-slate-500">
          RazorAI proposes. Saying &ldquo;yes&rdquo; here approves nothing: approving and paying
          happen on the store&rsquo;s own pages, never in this panel.
        </p>
      </div>
    </section>
  );
}
