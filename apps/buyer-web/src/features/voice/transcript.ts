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
  type Degradation,
  type DegradationKind,
  type ServerFrame,
  type SessionReady,
} from "./wire";

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
      return {
        ...state,
        seq,
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
      return { ...state, seq: entry.seq, entries: [...state.entries, entry] };
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
