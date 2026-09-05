/**
 * The voice WebSocket contract, in TypeScript.
 *
 * This file mirrors `packages/voice-runtime/src/voice_runtime/wire/frames.py` field for
 * field. That module is the authority; this one is its client-side copy, and when the two
 * disagree the Python is right. Parsed rather than cast, the same way `lib/api/types.ts`
 * parses REST responses: the socket is fed by a separate process that can be redeployed
 * while this tab is open, and a frame whose shape drifted should be legible here rather
 * than becoming `undefined` three components later.
 *
 * The semantics-carrying fields -- `cumulative`, `revisable`, `empty_means` -- are on the
 * wire as literals on purpose (19.5). They are not decoration: they are the contract
 * saying, in the frame itself, that `text` is the whole current hypothesis and not a
 * delta. They are kept here, and asserted, so that a server that ever changed its mind
 * fails at this boundary instead of silently corrupting a transcript.
 *
 * The tunables in `session_ready` are deliberately REQUIRED rather than defaulted. Every
 * one of them -- the echo tail, the barge-in threshold, the playback lead -- is measured
 * per deployment and is a property of the listener's speakers, not of this code. A client
 * that quietly substitutes its own guess for a missing `barge_in_level_rms` is the exact
 * hardcoding 19.15 forbids, so a `session_ready` without them fails to parse and the
 * panel says so.
 */
import { z } from "zod";

/* ------------------------------------------------------------------ degradation kinds */

/**
 * Every degraded path the server can announce (19.12).
 *
 * Exported as a tuple as well as a type so the notice component can be checked for
 * exhaustiveness at compile time and walked at test time. A kind added to `frames.py`
 * without copy to go with it should break the build, not ship as a blank card.
 */
export const DEGRADATION_KINDS = [
  "stt_connection_lost",
  "stt_rotation_failed",
  "stt_unavailable",
  "tts_failed",
  "reasoning_failed",
  "echo_gate_uncertain",
  "speech_guard_refused",
  "stale_turn_dropped",
  "card_unavailable",
] as const;

export const DegradationKindSchema = z.enum(DEGRADATION_KINDS);
export type DegradationKind = z.infer<typeof DegradationKindSchema>;

/* ------------------------------------------------------------------ server -> client */

export const AudioContractSchema = z.object({
  sample_rate_hz: z.number().int(),
  encoding: z.literal("pcm16le").default("pcm16le"),
  channels: z.literal(1).default(1),
});
export type AudioContract = z.infer<typeof AudioContractSchema>;

/**
 * The first frame on every stream: the constants the client must honour (19.15).
 *
 * `input.sample_rate_hz` is 16 kHz and `output.sample_rate_hz` is 24 kHz. That asymmetry
 * is the recognizer's contract meeting the synthesiser's output rate, and it is not a
 * defect to be tidied up: resampling either one to match the other costs quality and
 * fixes nothing.
 */
export const SessionReadySchema = z.object({
  type: z.literal("session_ready"),
  session_id: z.string(),
  input: AudioContractSchema,
  output: AudioContractSchema,
  mic_frame_ms: z.number().int(),
  echo_tail_s: z.number(),
  barge_in_level_rms: z.number(),
  barge_in_sustain_s: z.number(),
  playback_lead_s: z.number(),
  voice_is_authority: z.literal(false).default(false),
});
export type SessionReady = z.infer<typeof SessionReadySchema>;

/** `interim_input_transcription`: replace held text; never confirmed intent. */
export const TranscriptPartialSchema = z.object({
  type: z.literal("transcript_partial"),
  text: z.string(),
  turn_id: z.number().int(),
  stt_generation: z.number().int(),
  age_ms: z.number().int(),
  cumulative: z.literal(true).default(true),
  revisable: z.literal(true).default(true),
  empty_means: z.literal("keep_held").default("keep_held"),
});
export type TranscriptPartial = z.infer<typeof TranscriptPartialSchema>;

/**
 * `input_transcription`: replace, then close the turn.
 *
 * `stale` marks a final that aged past the freshness window and was NOT given to the
 * agent. It is still shown -- the buyer said it and deserves to see that it was heard --
 * but it is shown as heard-and-not-acted-on. Dropping it silently teaches a buyer that
 * the assistant is deaf at random; presenting it as if it had been acted on is worse.
 */
export const TranscriptFinalSchema = z.object({
  type: z.literal("transcript_final"),
  text: z.string(),
  turn_id: z.number().int(),
  stt_generation: z.number().int(),
  age_ms: z.number().int(),
  stale: z.boolean(),
  source: z.enum(["voice", "text"]).default("voice"),
  cumulative: z.literal(true).default(true),
  revisable: z.literal(false).default(false),
  empty_means: z.literal("keep_held").default("keep_held"),
});
export type TranscriptFinal = z.infer<typeof TranscriptFinalSchema>;

/**
 * Text exists before speech.
 *
 * This frame is the whole point of the split pipeline (19.1): the reply is on screen the
 * moment it is authored, and audio is a second, slower rendering of the same words. A
 * client that waits for the first audio chunk before drawing the text has thrown that
 * away. `deterministic` marks a reply rendered from a versioned locale template out of
 * server-confirmed fields (19.10) -- the money facts -- and carries its own audit trail.
 */
/** The one product a reply put forward; what a spoken "yes" refers to. */
export const OfferSchema = z.object({
  sku: z.string(),
  name: z.string(),
  quantity: z.number().int().positive(),
  unit_price: z.unknown().nullable().default(null),
});
export type Offer = z.infer<typeof OfferSchema>;

/**
 * One product a reply named, for the shelf the conversation draws under the sentence.
 *
 * Mirrors `_item_of` in `gateway/agent_client.py`. `unit_price` is the API's own money
 * object or null and is `unknown` here for the same reason it is on `OfferSchema`: an
 * amount that arrived as a number would invite arithmetic on it in this browser, and
 * `<Amount/>` is the only thing that reads one.
 *
 * `stock_units` is a COUNT and may be null. The gateway is careful that a boolean never
 * arrives here -- a `bool` is an `int` in Python -- because "available: true" counted as
 * one unit in stock is a lie a shelf would render as "1 left".
 */
export const ReplyItemSchema = z.object({
  sku: z.string(),
  name: z.string(),
  unit_price: z.unknown().nullable().default(null),
  stock_units: z.number().int().nullable().default(null),
  available: z.boolean().default(true),
});
export type ReplyItem = z.infer<typeof ReplyItemSchema>;

export const AgentReplySchema = z.object({
  type: z.literal("agent_reply"),
  text: z.string(),
  deterministic: z.boolean(),
  locale: z.string(),
  turn_id: z.number().int(),
  speech_generation: z.number().int(),
  template_id: z.string().nullable().default(null),
  template_version: z.number().int().nullable().default(null),
  // Already rendered by the server. Values are strings, never numbers: an amount that
  // arrived as a number would invite arithmetic on it here, and no amount is ever
  // computed in this browser.
  fields: z.record(z.string(), z.string()).nullable().default(null),
  offer: OfferSchema.nullable().optional(),
  // Every product this reply put on the page, the offer first, up to five. The two absent
  // forms mean different things and the reducer keeps them apart: an EMPTY list is a
  // conversational reply that showed no product and so clears the shelf, while `null` (a
  // deterministic money utterance) shows nothing and clears nothing. Collapsing the two
  // would make a spoken price wipe the products the buyer was just offered.
  items: z.array(ReplyItemSchema).nullable().optional(),
});
export type AgentReply = z.infer<typeof AgentReplySchema>;

export const SpeechStartSchema = z.object({
  type: z.literal("speech_start"),
  speech_generation: z.number().int(),
});
export type SpeechStart = z.infer<typeof SpeechStartSchema>;

/**
 * Announces exactly one following binary frame of `byte_length` bytes.
 *
 * The pairing is positional, not keyed: the very next binary message on the socket is
 * this chunk's audio. A client that reorders them plays somebody else's sentence.
 */
export const SpeechChunkHeaderSchema = z.object({
  type: z.literal("speech_chunk"),
  seq: z.number().int(),
  speech_generation: z.number().int(),
  text: z.string(),
  sample_rate_hz: z.number().int(),
  byte_length: z.number().int(),
  deterministic: z.boolean(),
  encoding: z.literal("pcm16le").default("pcm16le"),
});
export type SpeechChunkHeader = z.infer<typeof SpeechChunkHeaderSchema>;

export const SpeechEndSchema = z.object({
  type: z.literal("speech_end"),
  speech_generation: z.number().int(),
  chunks: z.number().int(),
  cancelled: z.boolean(),
});
export type SpeechEnd = z.infer<typeof SpeechEndSchema>;

/** Server acknowledgement of a client barge-in: the generation that is now current. */
export const InterruptedSchema = z.object({
  type: z.literal("interrupted"),
  speech_generation: z.number().int(),
});
export type Interrupted = z.infer<typeof InterruptedSchema>;

/**
 * A degraded path, made visible (19.12).
 *
 * The two literal fields are the money invariants stated on the wire, and the notice
 * component reads its copy off them rather than asserting the same thing from memory:
 * text input is still available, and no transaction state changed.
 */
export const DegradationSchema = z.object({
  type: z.literal("degradation"),
  kind: DegradationKindSchema,
  message: z.string(),
  text_input_available: z.literal(true).default(true),
  transaction_state_changed: z.literal(false).default(false),
});
export type Degradation = z.infer<typeof DegradationSchema>;

export const ErrorFrameSchema = z.object({
  type: z.literal("error"),
  code: z.string(),
  message: z.string(),
});
export type ErrorFrame = z.infer<typeof ErrorFrameSchema>;

/* ------------------------------------------------------------- spoken consent (19.11) */

/**
 * Voice is a second way to press the Approve button, and these frames are how this
 * client sees which way fired. Every one is a report. The gateway reads a card from the
 * trusted server, speaks it, listens for a word inside a window it opened when it had
 * finished SENDING the audio, and says what it heard against which bytes. It records
 * nothing: `recorded: false` is a literal on `consent_recognised` for the same reason
 * `transaction_state_changed: false` is one on `degradation`. What records an approval
 * is this storefront comparing those bytes to the card on screen and sending the same
 * request its button sends.
 */
export const CardReadSchema = z.object({
  type: z.literal("card_read"),
  checkout_id: z.string(),
  version: z.number().int(),
  content_hash: z.string(),
  amount_minor: z.number().int(),
  currency: z.string(),
  locale: z.string(),
  template_id: z.string(),
  template_version: z.number().int(),
  speech_generation: z.number().int(),
});
export type CardRead = z.infer<typeof CardReadSchema>;

export const ConsentListeningSchema = z.object({
  type: z.literal("consent_listening"),
  consent_id: z.string(),
  closes_in_s: z.number(),
  speech_generation: z.number().int(),
});
export type ConsentListening = z.infer<typeof ConsentListeningSchema>;

export const ConsentRecognisedSchema = z.object({
  type: z.literal("consent_recognised"),
  consent_id: z.string(),
  checkout_id: z.string(),
  version: z.number().int(),
  content_hash: z.string(),
  amount_minor: z.number().int(),
  currency: z.string(),
  heard: z.string(),
  turn_id: z.number().int(),
  stt_generation: z.number().int(),
  offset_ms: z.number().int(),
  recorded: z.literal(false).default(false),
  voice_is_authority: z.literal(false).default(false),
});
export type ConsentRecognised = z.infer<typeof ConsentRecognisedSchema>;

export const ConsentDeclinedSchema = z.object({
  type: z.literal("consent_declined"),
  consent_id: z.string(),
  heard: z.string(),
});
export type ConsentDeclined = z.infer<typeof ConsentDeclinedSchema>;

export const ConsentUnrecognisedSchema = z.object({
  type: z.literal("consent_unrecognised"),
  consent_id: z.string(),
  text: z.string(),
  reason: z.enum(["not_in_lexicon", "began_before_reading_ended"]),
});
export type ConsentUnrecognised = z.infer<typeof ConsentUnrecognisedSchema>;

export const CONSENT_CLOSED_REASONS = [
  "recognised",
  "declined",
  "expired",
  "barge_in",
  "superseded",
] as const;
export const ConsentClosedSchema = z.object({
  type: z.literal("consent_closed"),
  consent_id: z.string(),
  reason: z.enum(CONSENT_CLOSED_REASONS),
});
export type ConsentClosed = z.infer<typeof ConsentClosedSchema>;
export type ConsentClosedReason = ConsentClosed["reason"];

export const ServerFrameSchema = z.discriminatedUnion("type", [
  SessionReadySchema,
  TranscriptPartialSchema,
  TranscriptFinalSchema,
  AgentReplySchema,
  SpeechStartSchema,
  SpeechChunkHeaderSchema,
  SpeechEndSchema,
  InterruptedSchema,
  DegradationSchema,
  ErrorFrameSchema,
  CardReadSchema,
  ConsentListeningSchema,
  ConsentRecognisedSchema,
  ConsentDeclinedSchema,
  ConsentUnrecognisedSchema,
  ConsentClosedSchema,
]);
export type ServerFrame = z.infer<typeof ServerFrameSchema>;

/**
 * Parse one text frame off the socket.
 *
 * Returns a result rather than throwing. A REST call that returns a shape this app does
 * not understand is a failed call and `lib/api/client.ts` rightly throws it; one bad
 * frame on a live socket is not a reason to tear down a conversation that is otherwise
 * working, so the session records it and keeps listening.
 */
export type FrameParse =
  | { ok: true; frame: ServerFrame }
  | { ok: false; reason: string };

export function parseServerFrame(raw: string): FrameParse {
  let json: unknown;
  try {
    json = JSON.parse(raw);
  } catch {
    return { ok: false, reason: "frame was not JSON" };
  }
  const parsed = ServerFrameSchema.safeParse(json);
  if (!parsed.success) {
    const first = parsed.error.issues[0];
    const path = first?.path.join(".") ?? "";
    return { ok: false, reason: path ? `${path}: ${first?.message}` : (first?.message ?? "unparsable frame") };
  }
  return { ok: true, frame: parsed.data };
}

/* ------------------------------------------------------------------ client -> server */

/** Typed input. Always available, including while STT is degraded (19.12). */
export const TextInputSchema = z.object({
  type: z.literal("text_input"),
  text: z.string().min(1).max(4000),
});
export type TextInput = z.infer<typeof TextInputSchema>;

/** The client already flushed local playback (19.7); the server reconciles. */
export interface BargeIn {
  type: "barge_in";
}

/**
 * The client finished playing the speech of `speech_generation`; starts the echo tail
 * (19.6). The server never assumes this.
 *
 * Load-bearing, and easy to forget because nothing visibly breaks when it is missing. The
 * echo tail measures from the moment the buyer's speakers actually went quiet, which only
 * this client knows. Never send it, and the server holds its echo gate open for the full
 * `ECHO_GATE_MAX_HOLD_S`, during which the buyer is speaking into a microphone whose
 * frames are being replaced with silence -- which reads as "she stopped listening to me".
 */
export interface PlaybackEnded {
  type: "playback_ended";
  speech_generation: number;
}

export interface Ping {
  type: "ping";
}

/**
 * Ask the gateway to read an approval card aloud and listen for a yes or no.
 *
 * This NAMES a card; it does not describe one. There is no hash and no amount in it,
 * and the server rejects the frame if one is added: a hash this page could supply would
 * be a consent surface this page authored. The gateway reads the card from the trusted
 * server with the buyer's own bearer and speaks only what the server returned.
 */
export interface ReadCard {
  type: "read_card";
  checkout_id: string;
  version: number;
  locale: "en-IN" | "hi-IN";
}

export type ClientFrame = TextInput | BargeIn | PlaybackEnded | Ping | ReadCard;

export function encodeClientFrame(frame: ClientFrame): string {
  return JSON.stringify(frame);
}

/* ------------------------------------------------------------------- the merge rule */

/**
 * The one expression that keeps a transcript from eating itself (19.5, field guide 1.3).
 *
 * A streaming recognizer sends the WHOLE current hypothesis every frame and revises it
 * freely mid-utterance. A revision does not extend the previous string, so a client that
 * appends produces:
 *
 *     TumjoMainejoMaineTuMeriTuMainu
 *
 * which is what this project's predecessor shipped. That was a specification failure, not
 * a coding error: the contract described the SHAPE of a text frame and never said whether
 * `text` was cumulative or a delta, so the backend chose cumulative and the frontend
 * guessed. Hence `cumulative`, `revisable` and `empty_means` being fields on the wire.
 *
 * Two rules in one expression:
 *
 *   - REPLACE, never append. `incoming` wins whenever it has content.
 *   - An EMPTY frame never clears the held turn. Empty means "nothing new to say about
 *     this turn", not "the buyer un-said it", so `|| current` keeps what is held.
 *
 * The same rule governs both directions, buyer speech and assistant speech.
 */
export function applyStreamText(current: string, incoming: string): string {
  return incoming || current; // replace, never append; empty never clears
}
