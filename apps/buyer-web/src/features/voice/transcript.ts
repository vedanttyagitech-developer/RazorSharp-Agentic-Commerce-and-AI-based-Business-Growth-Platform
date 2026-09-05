/**
 * The conversation as a value: every server frame folded into one immutable state.
 *
 * Pure on purpose. The socket, the microphone and the speakers all live in `session.ts`
 * and are impossible to reason about in a test without a browser; the rule that decides
 * whether a revised hypothesis replaces or concatenates is the rule that produced
 * `TumjoMainejoMaineTuMeriTuMainu`, and it is worth being able to test on its own, with
 * no audio hardware anywhere near it.
 *
 * Two invariants this module exists to hold:
 *
 *  - **An interim transcript is never confirmed intent.** It is held separately from the
 *    settled turns and is drawn differently, so no consumer can accidentally treat a
 *    half-heard sentence as something the buyer said.
 *  - **A transcript is intent evidence, never authority evidence** (19.11). Nothing in
 *    this state, and nothing downstream of it, approves, pays, refunds or cancels. A
 *    spoken "yes" lands here as a settled turn and stops.
 */
import {
  applyStreamText,
  type AgentReply,
  type CardRead,
  type ConsentClosedReason,
  type ConsentRecognised,
  type ConsentUnrecognised,
  type Degradation,
  type DegradationKind,
  type ServerFrame,
  type SessionReady,
} from "./wire";
import type { Offer } from "./wire";

/* ------------------------------------------------------------- spoken consent (19.11) */

/**
 * Where the last reading of an approval card stands.
 *
 * `read`: the gateway read a card and is speaking it. `listening`: the reading has been
 * fully sent and a yes or no now counts. `recognised` and `declined` are the two words
 * the window can end with; `closed` is every other way it ends, with the reason. The
 * `recognised` frame is kept whole because the consent component compares its five
 * binding fields to the card on screen before it presses anything -- this state holds
 * what the gateway HEARD; it never holds what this page decided to do about it.
 */
export type ConsentStatus = "idle" | "read" | "listening" | "recognised" | "declined" | "closed";

export interface ConsentState {
  status: ConsentStatus;
  /** The card the gateway read from the trusted server, as it reported it. */
  card: CardRead | null;
  consentId: string | null;
  closesInS: number | null;
  recognised: ConsentRecognised | null;
  /** The settled transcript that ended the window, for a recognised or declined one. */
  heard: string | null;
  /** The last thing heard inside the window that was neither a yes nor a no. */
  nearMiss: { text: string; reason: ConsentUnrecognised["reason"] } | null;
  closedReason: ConsentClosedReason | null;
}

export const idleConsent: ConsentState = {
  status: "idle",
  card: null,
  consentId: null,
  closesInS: null,
  recognised: null,
  heard: null,
  nearMiss: null,
  closedReason: null,
};

/** The turn being spoken right now: revisable, unconfirmed, not yet intent. */
export interface HeldTurn {
  turnId: number;
  text: string;
}

/**
 * One line of the conversation.
 *
 * `seq` is arrival order, assigned here. Turn ids are not an ordering: the assistant's
 * reply to turn 4 and the buyer's turn 5 can both be in flight, and the transcript should
 * read in the order the buyer experienced it.
 */
export type TranscriptEntry =
  | {
      kind: "buyer";
      id: string;
      seq: number;
      turnId: number;
      text: string;
      /** Aged past the freshness window; heard, shown, and NOT given to the agent. */
      stale: boolean;
      source: "voice" | "text";
    }
  | {
      kind: "assistant";
      id: string;
      seq: number;
      turnId: number;
      text: string;
      /** Rendered from a versioned locale template out of server-confirmed fields. */
      deterministic: boolean;
      locale: string;
      speechGeneration: number;
      templateId: string | null;
      templateVersion: number | null;
      fields: Readonly<Record<string, string>> | null;
    };

/** A degraded path the buyer must be able to see (19.12). */
export interface DegradationNotice {
  id: string;
  seq: number;
  kind: DegradationKind;
  /** The server's own sentence. Shown verbatim under this app's plainer copy. */
  message: string;
  textInputAvailable: true;
  transactionStateChanged: false;
}

export interface VoiceTranscriptState {
  /** The tunables the client must honour. Null until `session_ready` lands. */
  ready: SessionReady | null;
  held: HeldTurn | null;
  entries: readonly TranscriptEntry[];
  degradations: readonly DegradationNotice[];
  /** True between `speech_start` and `speech_end`: the server's echo gate is engaged. */
  speaking: boolean;
  /** The generation the server is currently speaking, or last spoke. */
  speechGeneration: number;
  /** The last frame-level error, kept visible rather than swallowed. */
  error: { code: string; message: string } | null;
  /** The last reading of an approval card, and what the gateway heard against it. */
  consent: ConsentState;
  /** The product the last reply put forward, if any. Cleared by the next reply. */
  offer: Offer | null;
  /** The buyer said yes to `offer`: a counter, so one affirmation fires one action. */
  affirmed: { seq: number; offer: Offer } | null;
  seq: number;
}

export const initialTranscriptState: VoiceTranscriptState = {
  ready: null,
  held: null,
  entries: [],
  degradations: [],
  speaking: false,
  speechGeneration: 0,
  error: null,
  consent: idleConsent,
  offer: null,
  affirmed: null,
  seq: 0,
};

function assistantEntry(state: VoiceTranscriptState, frame: AgentReply): TranscriptEntry {
  const seq = state.seq + 1;
  return {
    kind: "assistant",
    id: `a${seq}`,
    seq,
    turnId: frame.turn_id,
    text: frame.text,
    deterministic: frame.deterministic,
    locale: frame.locale,
    speechGeneration: frame.speech_generation,
    templateId: frame.template_id,
    templateVersion: frame.template_version,
    fields: frame.fields,
  };
}

function degradationNotice(state: VoiceTranscriptState, frame: Degradation): DegradationNotice {
  const seq = state.seq + 1;
  return {
    id: `d${seq}`,
    seq,
    kind: frame.kind,
    message: frame.message,
    textInputAvailable: frame.text_input_available,
    transactionStateChanged: frame.transaction_state_changed,
  };
}

/**
 * The last settled buyer turn, for the consecutive-final deduplication rule.
 *
 * 19.5: identical consecutive finals are deduplicated by content and turn id. A recognizer
 * that re-sends the same settled utterance -- which happens across a stream rotation --
 * must not double the buyer's sentence in front of them.
 */
function lastBuyerEntry(entries: readonly TranscriptEntry[]): TranscriptEntry | null {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.kind === "buyer") return entry;
  }
  return null;
}

/**
 * Fold one server frame into the state.
 *
 * Returns the same object when a frame changes nothing, so a React consumer re-renders
 * only when the conversation actually moved.
 */
export function reduceTranscript(
  state: VoiceTranscriptState,
  frame: ServerFrame,
): VoiceTranscriptState {
  switch (frame.type) {
    case "session_ready":
      return { ...state, ready: frame, error: null };

    case "transcript_partial": {
      // A partial for a different turn abandons the held one rather than settling it: an
      // interim was never confirmed intent, so there is nothing to keep.
      const current = state.held && state.held.turnId === frame.turn_id ? state.held.text : "";
      const text = applyStreamText(current, frame.text);
      if (state.held && state.held.turnId === frame.turn_id && state.held.text === text) {
        return state;
      }
      return { ...state, held: { turnId: frame.turn_id, text } };
    }

    case "transcript_final": {
      const current = state.held && state.held.turnId === frame.turn_id ? state.held.text : "";
      const text = applyStreamText(current, frame.text);
      const previous = lastBuyerEntry(state.entries);
      if (previous && previous.turnId === frame.turn_id && previous.text === text) {
        // Same turn, same words: already settled. Close the held turn and stop.
        return state.held === null ? state : { ...state, held: null };
      }
      const seq = state.seq + 1;
      // "Yes" after an offer, outside a consent window, is the buyer taking the offer. The
      // consent window has its own, stricter lexicon on the gateway; this one belongs to
      // the shopping conversation and acts on a product, never on money.
      const affirmed =
        state.offer !== null && state.consent.status !== "listening" && isAffirmative(text)
          ? { seq, offer: state.offer }
          : state.affirmed;
      return {
        ...state,
        seq,
        affirmed,
        held: null, // the turn is closed; nothing is held after a final
        entries: [
          ...state.entries,
          {
            kind: "buyer",
            id: `b${seq}`,
            seq,
            turnId: frame.turn_id,
            text,
            stale: frame.stale,
            source: frame.source,
          },
        ],
      };
    }

    case "agent_reply": {
      // Drawn the instant it arrives, with no reference at all to audio. Speech is a
      // second, slower rendering of these same words (19.1); the text never waits for it.
      const entry = assistantEntry(state, frame);
      // A deterministic (money) utterance never carries an offer and never clears one:
      // the reading of a card follows the reply that offered, it does not replace it.
      const offer = frame.deterministic ? state.offer : (frame.offer ?? null);
      return { ...state, seq: entry.seq, entries: [...state.entries, entry], offer };
    }

    case "speech_start":
      return { ...state, speaking: true, speechGeneration: frame.speech_generation };

    case "speech_chunk":
      // Nothing to add to the conversation: the words in a chunk header are a sentence of
      // a reply that was already drawn in full by `agent_reply`. The header's job is to
      // announce the binary frame behind it, and that is the playback queue's business.
      return state;

    case "speech_end":
      // `speaking` goes false when the SERVER stops sending. The buyer's speakers are
      // still working through the queue, and the echo tail measures from the moment they
      // fall quiet -- which is why the client sends `playback_ended` (19.6).
      return frame.speech_generation < state.speechGeneration
        ? state
        : { ...state, speaking: false };

    case "interrupted":
      return { ...state, speaking: false, speechGeneration: frame.speech_generation };

    case "degradation": {
      const notice = degradationNotice(state, frame);
      // One card per kind. A flapping STT connection should not bury the transcript under
      // forty identical notices; the latest message for a kind replaces the previous one.
      const others = state.degradations.filter((existing) => existing.kind !== notice.kind);
      return { ...state, seq: notice.seq, degradations: [...others, notice] };
    }

    case "error":
      return { ...state, error: { code: frame.code, message: frame.message } };

    case "card_read":
      // A new reading starts a new consent from nothing: whatever the previous window
      // heard is not evidence about this card.
      return { ...state, consent: { ...idleConsent, status: "read", card: frame } };

    case "consent_listening":
      return {
        ...state,
        consent: {
          ...state.consent,
          status: "listening",
          consentId: frame.consent_id,
          closesInS: frame.closes_in_s,
          nearMiss: null,
        },
      };

    case "consent_recognised":
      // Only for the window this page is listening on. The gateway holds one window and
      // closes it before it opens the next, so it never sends a word for a window that is
      // not the open one; a frame that does is a replay or a forgery from page script,
      // and it is dropped here rather than compared. Held whole otherwise: which card it
      // names is compared to the card on screen by the component that would press the
      // button, not decided here.
      if (state.consent.status !== "listening" || frame.consent_id !== state.consent.consentId) {
        return state;
      }
      return {
        ...state,
        consent: { ...state.consent, status: "recognised", recognised: frame, heard: frame.heard },
      };

    case "consent_declined":
      if (state.consent.status !== "listening" || frame.consent_id !== state.consent.consentId) {
        return state;
      }
      return { ...state, consent: { ...state.consent, status: "declined", heard: frame.heard } };

    case "consent_unrecognised":
      if (frame.consent_id !== state.consent.consentId) return state;
      return {
        ...state,
        consent: { ...state.consent, nearMiss: { text: frame.text, reason: frame.reason } },
      };

    case "consent_closed": {
      if (frame.consent_id !== state.consent.consentId) return state;
      // Every window ends with exactly one of these. A window that ended on a word keeps
      // that word as its status; any other ending is `closed` with the reason.
      const ended = frame.reason === "recognised" || frame.reason === "declined";
      return {
        ...state,
        consent: {
          ...state.consent,
          status: ended ? state.consent.status : "closed",
          closedReason: frame.reason,
        },
      };
    }

    default: {
      // Exhaustive over `ServerFrame`. A frame type added to `frames.py` and mirrored into
      // `wire.ts` fails to compile here until this reducer decides what it means, which is
      // the only way a new server frame does not become a frame nobody handles.
      const unhandled: never = frame;
      void unhandled;
      return state;
    }
  }
}

/** Clear one notice, for a buyer who has read it. Unknown ids are a no-op. */
export function dismissDegradation(
  state: VoiceTranscriptState,
  id: string,
): VoiceTranscriptState {
  const remaining = state.degradations.filter((notice) => notice.id !== id);
  return remaining.length === state.degradations.length ? state : { ...state, degradations: remaining };
}

const AFFIRMATIVE = new Set([
  "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "add it", "add", "go ahead",
  "do it", "confirm", "haan", "han", "ha", "haa", "ji", "ji haan", "ji ha", "theek hai",
  "thik hai", "kar do", "karo", "add karo", "add kar do", "le lo", "lelo", "हाँ", "हां", "जी",
  "जी हाँ", "ठीक है",
]);
const YES_FIRST = new Set([
  "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "haan", "han", "ha", "haa", "ji", "theek",
  "thik", "हाँ", "हां", "जी", "ठीक",
]);
const NEGATIVE = new Set(["no", "nope", "not", "don't", "dont", "nahi", "nahin", "na", "mat", "नहीं", "मत"]);

/**
 * A spoken yes: a short whole-utterance yes, or a short utterance that opens with one and
 * says no "no" anywhere -- "yes add that", "haan add karo", "okay please". A sentence that
 * merely contains a yes is not one, and anything with a negative in it is not one either.
 */
export function isAffirmative(text: string): boolean {
  const words = text
    .toLowerCase()
    .replace(/[.,!?।]/g, " ")
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 0 || words.length > 6) return false;
  if (words.some((word) => NEGATIVE.has(word))) return false;
  if (AFFIRMATIVE.has(words.join(" "))) return true;
  return YES_FIRST.has(words[0]);
}
