/**
 * The voice surface, assembled.
 *
 * A mouth and an ear, and nothing else. This panel can send four things: microphone audio,
 * typed text, an interruption, and a report that its speakers went quiet. It holds no
 * capability of its own, and there is deliberately no control in it that approves, pays,
 * cancels or refunds -- not even a confirming one, not even for a reply that asks for
 * confirmation. A transcript is intent evidence, never authority evidence (19.11), and a
 * spoken "yes" here records no consent. Approving happens on the store's own pages, where
 * the buyer is looking at the exact version and hash they are consenting to.
 *
 * That is a rule about the drawing as much as about the code. A confirm button inside a
 * surface the agent also writes into is the confusion this whole submission exists to
 * refuse, and it would be no less dangerous for being disabled or for saying "voice only".
 *
 * The typing box is not a fallback bolted on for demos. It is what makes the degradation
 * copy true: every notice on this panel promises the buyer they can still type, and a
 * promise that depends on a control that is not there is worse than no promise.
 */
"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { cx } from "@/components/ui";

import { ClientNoticeCard, DegradedNotice } from "./degraded-notice";
import { LiveTranscript } from "./live-transcript";
import { PushToTalk } from "./push-to-talk";
import type { ConnectionState } from "./session";
import { useVoiceSession, type UseVoiceSessionOptions } from "./use-voice-session";

const CONNECTION_COPY: Readonly<Record<ConnectionState, { label: string; tone: string }>> = {
  idle: { label: "Not connected", tone: "text-[var(--ink-4)] bg-[var(--tint-1)]" },
  connecting: { label: "Connecting", tone: "text-[var(--ink-3)] bg-[var(--tint-1)]" },
  open: { label: "Connected", tone: "text-[var(--green)] bg-[var(--green-add-bg)]" },
  reconnecting: { label: "Reconnecting", tone: "text-[var(--amber)] bg-amber-50" },
  closed: { label: "Stopped", tone: "text-[var(--ink-4)] bg-[var(--tint-1)]" },
};

function SendIcon() {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" fill="none">
      <path
        d="M10 16.5 V4 M5 9 L10 4 L15 9"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export interface VoicePanelProps extends UseVoiceSessionOptions {
  className?: string;
}

export function VoicePanel({ className, ...sessionOptions }: VoicePanelProps) {
  const voice = useVoiceSession(sessionOptions);
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  const { transcript, connection } = voice;
  const running = connection !== "idle" && connection !== "closed";

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [transcript.entries, transcript.held]);

  const submit = useCallback(
    (event: React.FormEvent) => {
      event.preventDefault();
      if (voice.sendText(draft)) setDraft("");
    },
    [draft, voice],
  );

  const status = CONNECTION_COPY[connection];

  return (
    <section
      aria-label="Talk to RazorAI"
      data-ai-state={
        transcript.speaking ? "speaking" : voice.transmitting ? "listening" : "idle"
      }
      className={cx(
        "ai-box flex min-h-0 flex-col gap-3 rounded-[var(--r-lg)] border-[0.5px] border-[var(--card-line)] bg-[var(--surface)] p-4",
        className,
      )}
    >
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-[16px] font-bold text-[var(--ink)]">Talk to RazorAI</h2>
          <p className="mt-0.5 text-[12px] leading-[1.4] text-[var(--ink-4)]">
            She answers in writing first and speaks it afterwards.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span
            className={cx(
              "rounded-full px-2 py-0.5 text-[12px] font-semibold",
              status.tone,
            )}
          >
            {status.label}
          </span>
          <button
            type="button"
            onClick={() => (running ? voice.stop() : voice.start())}
            className="rounded-[var(--r-sm)] border border-[var(--card-line)] bg-white px-2.5 py-1 text-[12px] font-semibold text-[var(--ink-2)] transition hover:bg-[var(--tint-2)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--blue)]"
          >
            {running ? "Stop" : "Start voice"}
          </button>
        </div>
      </header>

      <ClientNoticeCard
        notice={voice.notice}
        connection={connection}
        onDismiss={voice.dismissNotice}
      />
      <DegradedNotice
        notices={transcript.degradations}
        onDismiss={voice.dismissDegradation}
      />

      {transcript.error ? (
        <p
          role="alert"
          className="rounded-[var(--r-md)] border-[0.5px] border-[var(--red)] bg-red-50/60 px-3 py-2 text-[12px] leading-[1.45] text-[var(--ink-3)]"
        >
          <span className="font-semibold text-[var(--red)]">
            The store refused this voice session.
          </span>{" "}
          {transcript.error.message}
          <span className="tnum mt-1 block text-[9px] text-[var(--ink-5)]">
            {transcript.error.code}
          </span>
        </p>
      ) : null}

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
        <LiveTranscript
          entries={transcript.entries}
          held={transcript.held}
          speaking={transcript.speaking}
        />
      </div>

      <PushToTalk
        transmitting={voice.transmitting}
        onTransmitChange={voice.setTransmitting}
        level={voice.micLevel}
        micState={voice.mic}
        assistantSpeaking={transcript.speaking}
        disabled={connection !== "open"}
      />

      <form onSubmit={submit} className="flex items-center gap-2">
        <label htmlFor="voice-text-input" className="sr-only">
          Type to RazorAI instead of speaking
        </label>
        <input
          id="voice-text-input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Or type it"
          autoComplete="off"
          maxLength={4000}
          className="h-10 min-w-0 flex-1 rounded-[var(--r-md)] bg-[var(--tint-2)] px-3 text-[14px] text-[var(--ink)] placeholder:text-[var(--ink-5)]"
        />
        <button
          type="submit"
          disabled={draft.trim().length === 0 || connection !== "open"}
          aria-label="Send typed message"
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--r-md)] bg-[var(--blue)] text-white transition disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--blue)]"
        >
          <SendIcon />
        </button>
      </form>

      <p className="text-[12px] leading-[1.45] text-[var(--ink-5)]">
        Saying &ldquo;yes&rdquo; here approves nothing. Voice is how you ask; approving and
        paying happen on the store&rsquo;s own pages, on the exact order you are looking at.
        {voice.micPath ? (
          <span className="tnum mt-1 block text-[9px] text-[var(--ink-6)]">
            capture: {voice.micPath}
            {transcript.ready
              ? ` · in ${transcript.ready.input.sample_rate_hz} Hz · out ${transcript.ready.output.sample_rate_hz} Hz`
              : ""}
          </span>
        ) : null}
      </p>
    </section>
  );
}
