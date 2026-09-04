"use client";

import { useState } from "react";

import { EMPTY_TRANSCRIPT, reduceTranscript, type TranscriptFrame } from "@/lib/voice/transcript";

import { Button, StatusPill } from "./ui";

/**
 * Placeholder voice panel. Renders the transcript wire contract (spec 19.5) and nothing
 * else: no microphone, no audio, no model. The scripted frames show the two rules the
 * contract enforces -- replace rather than append, and an empty frame never clears.
 */
const SCRIPT: { label: string; frame: TranscriptFrame }[] = [
  { label: "Interim frame: “mujhe”", frame: { turn_id: "turn-1", interim_input_transcription: { text: "mujhe" } } },
  { label: "Interim frame: “mujhe doodh”", frame: { turn_id: "turn-1", interim_input_transcription: { text: "mujhe doodh" } } },
  { label: "Empty interim frame (must not clear)", frame: { turn_id: "turn-1", interim_input_transcription: { text: "" } } },
  { label: "Final: “mujhe doodh aur ande chahiye”", frame: { turn_id: "turn-1", input_transcription: { text: "mujhe doodh aur ande chahiye" } } },
  { label: "Duplicate final (deduplicated)", frame: { turn_id: "turn-1", input_transcription: { text: "mujhe doodh aur ande chahiye" } } },
  { label: "Assistant interim: “Milk and eggs —”", frame: { turn_id: "turn-1", interim_output_transcription: { text: "Milk and eggs —" } } },
  { label: "Assistant final", frame: { turn_id: "turn-1", output_transcription: { text: "Milk and eggs — adding Amul Taaza Toned Milk and Farm Eggs. Say ‘checkout’ when ready." } } },
];

export function VoicePanel() {
  const [transcript, setTranscript] = useState(EMPTY_TRANSCRIPT);
  const [cursor, setCursor] = useState(0);

  function feedNext() {
    const next = SCRIPT[cursor];
    if (!next) return;
    setTranscript((current) => reduceTranscript(current, next.frame));
    setCursor(cursor + 1);
  }

  function reset() {
    setTranscript(EMPTY_TRANSCRIPT);
    setCursor(0);
  }

  const input = transcript.input[0];
  const output = transcript.output[0];

  return (
    <section aria-labelledby="voice-heading" className="rounded-lg border border-line bg-surface p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 id="voice-heading" className="text-base font-semibold">Voice (placeholder)</h2>
        <StatusPill tone="warning" glyph="⚠" label="No audio in this build — text mode" />
      </div>
      <p className="text-sm text-muted">
        Renders the transcript wire contract only. <code className="font-mono text-xs">interim_input_transcription.text</code> is cumulative and revisable (replace held text);
        <code className="font-mono text-xs"> input_transcription.text</code> is the settled turn (replace, then close). Empty means “no change”. Voice is never payment authority (spec 19.11).
      </p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div className="rounded-md border border-line p-3 text-sm">
          <h3 className="font-medium">Buyer turn {input?.turn_id ?? "—"}</h3>
          <dl className="mt-1 space-y-1">
            <div><dt className="inline text-muted">interim (revisable): </dt><dd className="inline" aria-live="polite">{input?.interim || "—"}</dd></div>
            <div><dt className="inline text-muted">final (settled): </dt><dd className="inline font-medium" aria-live="polite">{input?.final ?? "—"}</dd></div>
            <div><dt className="inline text-muted">turn closed: </dt><dd className="inline">{input?.closed ? "yes" : "no"}</dd></div>
          </dl>
        </div>
        <div className="rounded-md border border-line p-3 text-sm">
          <h3 className="font-medium">Assistant turn</h3>
          <dl className="mt-1 space-y-1">
            <div><dt className="inline text-muted">interim: </dt><dd className="inline" aria-live="polite">{output?.interim || "—"}</dd></div>
            <div><dt className="inline text-muted">final: </dt><dd className="inline font-medium" aria-live="polite">{output?.final ?? "—"}</dd></div>
          </dl>
        </div>
      </div>
      <p className="mt-3 text-sm">
        <span className="text-muted">Confirmed intents (finals only): </span>
        {transcript.confirmed_intents.length === 0 ? "none" : transcript.confirmed_intents.map((intent) => `[${intent.turn_id}] ${intent.text}`).join("; ")}
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="secondary" onClick={feedNext} disabled={cursor >= SCRIPT.length}>
          {cursor < SCRIPT.length ? `Feed frame ${cursor + 1}/${SCRIPT.length}: ${SCRIPT[cursor].label}` : "Script complete"}
        </Button>
        <Button variant="ghost" onClick={reset}>Reset</Button>
      </div>
    </section>
  );
}
