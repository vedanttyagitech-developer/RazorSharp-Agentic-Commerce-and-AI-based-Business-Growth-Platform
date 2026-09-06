/**
 * Degradation, made visible. Silent degradation is a defect in this project (19.12).
 *
 * Every `degradation` frame produces something on screen. Not a console line, not a
 * retry that quietly papers over it, not a spinner that never resolves -- a card the buyer
 * can read. The reason is narrow and specific: a voice assistant that half works is
 * indistinguishable, from the outside, from a voice assistant that has stopped listening
 * to you, and a buyer who cannot tell which will keep talking to a dead microphone while
 * their basket sits there.
 *
 * The copy follows three rules.
 *
 *  - **Say what still works, not what broke.** Every one of these failures leaves the
 *    buyer with a working way to finish what they were doing, and that sentence is the
 *    useful half.
 *  - **Calm, not alarming.** None of these are errors the buyer caused or can fix, and an
 *    amber card that reads like a crash teaches people to distrust the parts that are fine.
 *  - **The money invariants come off the wire.** `text_input_available` and
 *    `transaction_state_changed` are literal fields on the frame, and the two closing
 *    sentences are rendered from them rather than asserted from memory here. If the
 *    contract ever changed, this copy would change with it.
 */
"use client";

import { cx } from "@/components/ui";

import type { ClientNotice, ConnectionState } from "./session";
import type { DegradationNotice } from "./transcript";
import type { DegradationKind } from "./wire";

interface Copy {
  title: string;
  body: string;
}

/**
 * One entry per kind in the union, checked by the type rather than by review.
 *
 * A kind added to `frames.py` and mirrored into `wire.ts` will not compile until it has
 * copy here, which is the only reliable way to stop a degraded path shipping as a blank
 * card.
 */
const DEGRADATION_COPY: Readonly<Record<DegradationKind, Copy>> = {
  speech_guard_refused: {
    title: "Part of that reply is on screen only",
    body:
      "Amounts and payment outcomes are read aloud only when the server has confirmed " +
      "them, so the rest of this reply was written but not spoken. The full text is above.",
  },
  stale_turn_dropped: {
    title: "That took too long to come back",
    body:
      "Your words were heard but the answer took long enough that acting on it could have " +
      "been wrong. Nothing was changed. Please say it again if you still want it.",
  },
  stt_connection_lost: {
    title: "Speech recognition dropped out",
    body:
      "The connection that turns your voice into words was lost and is being re-opened. " +
      "Anything you said in the last moment may not have been heard, so it is worth saying " +
      "again.",
  },
  stt_rotation_failed: {
    title: "Speech recognition is restarting",
    body:
      "These connections are replaced on a timer before the provider closes them, and this " +
      "hand-over did not take. A fresh one is being opened now. You may lose a second of " +
      "speech across the gap.",
  },
  stt_unavailable: {
    title: "Speech recognition is unavailable",
    body:
      "Nothing is listening at the moment, so speaking will not reach the assistant. " +
      "Everything else about this conversation works exactly as it did.",
  },
  tts_failed: {
    title: "The assistant's voice is unavailable",
    body:
      "Her reply is on screen in full and nothing is missing from it. The words are always " +
      "written before they are spoken, so a failure here costs the sound and nothing else.",
  },
  reasoning_failed: {
    title: "Answering the direct way",
    body:
      "The conversational model is unavailable, so search and the ordinary forms are being " +
      "used instead. Totals, fees and payment outcomes are unaffected: those never came from " +
      "the model in the first place.",
  },
  echo_gate_uncertain: {
    title: "Your microphone is being held closed",
    body:
      "It was not clear whether the assistant was hearing her own voice back through your " +
      "speakers, and the safer choice is to suppress the microphone rather than to act on " +
      "something she may have said to herself.",
  },
  card_unavailable: {
    title: "The approval card could not be read aloud",
    body:
      "The store would not give the voice service the card the screen asked for, so nothing " +
      "was read out and no spoken yes could count. The Approve button on this page works " +
      "exactly as it did.",
  },
};

/** The client's own bad news, in the same voice as the server's. */
const CLIENT_COPY: Readonly<Record<ClientNotice["kind"], Copy>> = {
  connection_lost: {
    title: "The voice connection dropped",
    body:
      "This tab is reconnecting on its own, waiting a little longer between each attempt. " +
      "Nothing you had already said was lost on the way to the store.",
  },
  microphone_denied: {
    title: "This site cannot use your microphone",
    body:
      "The browser refused the request, which is its job. You can allow it from the icon in " +
      "the address bar, or carry on typing.",
  },
  microphone_failed: {
    title: "The microphone could not be opened",
    body:
      "Something else may be holding it, or the device may have changed. Typing works either " +
      "way.",
  },
  frame_unreadable: {
    title: "A message from the store could not be read",
    body:
      "One frame did not match the contract this app understands, so it was ignored rather " +
      "than guessed at. If a reply seems to be missing, ask again.",
  },
  speech_chunk_mismatch: {
    title: "A piece of speech was discarded",
    body:
      "An audio chunk did not match the length announced for it, so it was dropped instead " +
      "of played. The text of that reply is unaffected and is on screen above.",
  },
};

function InfoIcon() {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" fill="none">
      <circle cx="10" cy="10" r="7.4" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M10 9 V14"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <circle cx="10" cy="6.4" r="0.95" fill="currentColor" />
    </svg>
  );
}

function Card({
  title,
  body,
  detail,
  invariants,
  onDismiss,
}: {
  title: string;
  body: string;
  /** The server's own sentence, or the client's diagnostic. Kept, never paraphrased away. */
  detail: string | null;
  invariants: readonly string[];
  onDismiss?: () => void;
}) {
  return (
    <section
      // `status`, not `alert`. These are conditions to be aware of, not interruptions, and
      // an assertive announcement would cut across the reply being read out.
      role="status"
      className="rounded-[var(--r-md)] border-[0.5px] border-[var(--amber)] bg-amber-50/60 p-3"
    >
      <div className="flex items-start gap-2">
        <span className="mt-px shrink-0 text-[var(--amber)]">
          <InfoIcon />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-[13px] font-semibold text-[var(--ink)]">{title}</p>
          <p className="mt-0.5 text-[12px] leading-[1.45] text-[var(--ink-3)]">{body}</p>
          <ul className="mt-1.5 flex flex-col gap-0.5">
            {invariants.map((line) => (
              <li key={line} className="text-[12px] leading-[1.45] text-[var(--ink-2)]">
                {line}
              </li>
            ))}
          </ul>
          {detail ? (
            <p className="mt-1.5 text-[9px] tracking-[0.04em] text-[var(--ink-5)]">{detail}</p>
          ) : null}
        </div>
        {onDismiss ? (
          <button
            type="button"
            onClick={onDismiss}
            aria-label={`Dismiss: ${title}`}
            className="-mt-1 -mr-1 shrink-0 rounded-full px-2 py-1 text-[12px] text-[var(--ink-4)] transition hover:bg-amber-100/70 hover:text-[var(--ink)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--blue)]"
          >
            Dismiss
          </button>
        ) : null}
      </div>
    </section>
  );
}

/**
 * Every degradation the server has announced.
 *
 * Renders nothing when there are none, so a panel can mount it unconditionally.
 */
export function DegradedNotice({
  notices,
  onDismiss,
  className,
}: {
  notices: readonly DegradationNotice[];
  onDismiss?: (id: string) => void;
  className?: string;
}) {
  if (notices.length === 0) return null;
  return (
    <div className={cx("flex flex-col gap-2", className)} aria-label="Voice service notices">
      {notices.map((notice) => {
        const copy = DEGRADATION_COPY[notice.kind];
        return (
          <Card
            key={notice.id}
            title={copy.title}
            body={copy.body}
            detail={`${notice.kind}${notice.message ? ` · ${notice.message}` : ""}`}
            invariants={[
              // Both sentences are rendered from the frame's own literal fields.
              notice.textInputAvailable
                ? "You can still type, and typing does everything speaking does."
                : "Typed input is unavailable.",
              notice.transactionStateChanged
                ? "Something about this transaction changed."
                : "Nothing about your cart, checkout, order or payment changed.",
            ]}
            onDismiss={onDismiss ? () => onDismiss(notice.id) : undefined}
          />
        );
      })}
    </div>
  );
}

/**
 * The client's own degradation: a dropped socket, a refused microphone, a frame this app
 * could not read. Same card, same promise, because the buyer does not care which side of
 * the socket noticed.
 */
export function ClientNoticeCard({
  notice,
  connection,
  onDismiss,
}: {
  notice: ClientNotice | null;
  connection: ConnectionState;
  onDismiss?: () => void;
}) {
  if (!notice) return null;
  const copy = CLIENT_COPY[notice.kind];
  const reconnecting = connection === "reconnecting" || connection === "connecting";
  return (
    <Card
      title={copy.title}
      body={copy.body}
      detail={`${notice.kind} · ${notice.detail}`}
      invariants={[
        "You can still type, and typing does everything speaking does.",
        "Nothing about your cart, checkout, order or payment changed.",
        ...(reconnecting ? ["The connection is being re-established."] : []),
      ]}
      onDismiss={onDismiss}
    />
  );
}
