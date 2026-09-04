/**
 * Transcript wire contract, spec 19.5. No audio here; this module only encodes the
 * merge semantics so the placeholder voice panel renders the contract shape correctly.
 *
 * Field                              | Semantics                | Client behaviour
 * ---------------------------------- | ------------------------ | ---------------------------
 * interim_input_transcription.text   | cumulative, revisable    | replace held text
 * input_transcription.text           | settled utterance, turn  | replace, then close the turn
 *
 * Every streamed field is cumulative, revisable until final, and an empty value means
 * "no change" -- never "clear".
 */

export interface TranscriptFrame {
  turn_id: string;
  /** Cumulative and revisable. Replace the held interim text. */
  interim_input_transcription?: { text: string };
  /** The settled utterance. Replace, then close the turn. */
  input_transcription?: { text: string };
  /** Assistant speech follows the same rule in the other direction. */
  interim_output_transcription?: { text: string };
  output_transcription?: { text: string };
}

/** The whole contract in one expression: replace rather than append; empty never clears. */
export function applyStreamText(current: string, incoming: string | null | undefined): string {
  return incoming || current;
}

export interface TranscriptTurn {
  turn_id: string;
  interim: string;
  final: string | null;
  closed: boolean;
}

export interface TranscriptState {
  input: TranscriptTurn[];
  output: TranscriptTurn[];
  /** Only finals enter intent processing; an interim is never presented as confirmed. */
  confirmed_intents: { turn_id: string; text: string }[];
}

export const EMPTY_TRANSCRIPT: TranscriptState = { input: [], output: [], confirmed_intents: [] };

function upsertTurn(
  turns: TranscriptTurn[],
  turnId: string,
  interim: string | undefined,
  final: string | undefined,
): { turns: TranscriptTurn[]; closedNow: TranscriptTurn | null } {
  const index = turns.findIndex((turn) => turn.turn_id === turnId);
  const existing: TranscriptTurn = index >= 0 ? turns[index] : { turn_id: turnId, interim: "", final: null, closed: false };
  let next: TranscriptTurn = { ...existing, interim: applyStreamText(existing.interim, interim) };
  let closedNow: TranscriptTurn | null = null;
  if (final !== undefined) {
    const settled = applyStreamText(existing.final ?? existing.interim, final);
    // Identical consecutive finals are deduplicated by content and turn id.
    const duplicate = existing.closed && existing.final === settled;
    next = { ...next, final: settled, interim: settled, closed: true };
    if (!duplicate) closedNow = next;
  }
  const updated = index >= 0 ? turns.map((turn, i) => (i === index ? next : turn)) : [...turns, next];
  return { turns: updated, closedNow };
}

export function reduceTranscript(state: TranscriptState, frame: TranscriptFrame): TranscriptState {
  const input = upsertTurn(
    state.input,
    frame.turn_id,
    frame.interim_input_transcription?.text,
    frame.input_transcription?.text,
  );
  const output = upsertTurn(
    state.output,
    frame.turn_id,
    frame.interim_output_transcription?.text,
    frame.output_transcription?.text,
  );
  const confirmed = input.closedNow
    ? [...state.confirmed_intents, { turn_id: input.closedNow.turn_id, text: input.closedNow.final ?? "" }]
    : state.confirmed_intents;
  return { input: input.turns, output: output.turns, confirmed_intents: confirmed };
}
