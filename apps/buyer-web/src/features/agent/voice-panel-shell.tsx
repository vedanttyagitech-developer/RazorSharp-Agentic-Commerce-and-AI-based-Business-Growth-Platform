"use client";

import { useState } from "react";
import { reduceTranscript, EMPTY_TRANSCRIPT, type TranscriptFrame, type TranscriptState } from "@/lib/voice/transcript";

export function VoicePanelShell({
  onTranscribeComplete,
  isDegraded,
}: {
  onTranscribeComplete?: (text: string) => void;
  isDegraded?: boolean;
}) {
  const [isListening, setIsListening] = useState(false);
  const [transcript, setTranscript] = useState<TranscriptState>(EMPTY_TRANSCRIPT);
  const [liveInterim, setLiveInterim] = useState("");

  const simulateSpeech = (sampleText: string) => {
    setIsListening(true);
    setTranscript(EMPTY_TRANSCRIPT);
    setLiveInterim("");

    const turnId = `turn_${Date.now()}`;
    const words = sampleText.split(" ");
    let current = "";

    words.forEach((word, idx) => {
      setTimeout(() => {
        current += (idx === 0 ? "" : " ") + word;
        setLiveInterim(current);

        const frame: TranscriptFrame = {
          turn_id: turnId,
          interim_input_transcription: { text: current },
        };
        setTranscript((prev) => reduceTranscript(prev, frame));

        // If last word, settle as final turn
        if (idx === words.length - 1) {
          setTimeout(() => {
            const finalFrame: TranscriptFrame = {
              turn_id: turnId,
              input_transcription: { text: current },
            };
            setTranscript((prev) => reduceTranscript(prev, finalFrame));
            setIsListening(false);
            setLiveInterim("");
            if (onTranscribeComplete) {
              onTranscribeComplete(current);
            }
          }, 300);
        }
      }, (idx + 1) * 220);
    });
  };

  return (
    <div
      role="region"
      aria-label="Voice Shopping Interface"
      className="rounded-2xl border border-line bg-surface-raised p-4 space-y-3"
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="flex h-5 items-center rounded-full bg-indigo-600 px-2 text-[10px] font-black uppercase tracking-wider text-white">
            Voice Shell · Spec 19.5
          </span>
          <span className="text-xs font-bold text-foreground">
            Realtime Transactional Speech
          </span>
        </div>
        {isDegraded ? (
          <span className="text-[10px] font-bold text-orange-600 dark:text-orange-400 bg-orange-100 dark:bg-orange-950 px-2 py-0.5 rounded-full">
            Speech Gateway Degraded
          </span>
        ) : (
          <span className="text-[10px] text-muted font-mono">
            {isListening ? "Streaming Audio..." : "Ready"}
          </span>
        )}
      </div>

      {/* Mic Animation & Visualizer */}
      <div className="flex items-center justify-center py-3">
        <div className="flex flex-col items-center gap-2">
          <div
            className={`relative flex h-14 w-14 items-center justify-center rounded-full transition-all ${
              isListening
                ? "bg-rose-500 text-white shadow-lg ring-4 ring-rose-300 dark:ring-rose-800 animate-pulse"
                : "bg-brand-purple text-white hover:bg-brand-purple/90 cursor-pointer shadow-xs"
            }`}
            onClick={() => {
              if (!isListening) simulateSpeech("2 packet amul taaza doodh add kar do");
            }}
          >
            <span className="text-2xl" aria-hidden="true">🎙️</span>
            {isListening && (
              <span className="absolute -inset-1 rounded-full border-2 border-rose-400 animate-ping" />
            )}
          </div>
          <span className="text-[11px] font-semibold text-muted">
            {isListening ? "Listening (Hindi / Hinglish / English)..." : "Tap to speak or use simulated voice prompt"}
          </span>
        </div>
      </div>

      {/* Live Interim Transcript */}
      {isListening && liveInterim && (
        <div className="rounded-xl border border-indigo-200 dark:border-indigo-900 bg-indigo-50/60 dark:bg-indigo-950/40 p-3">
          <div className="flex items-center justify-between text-[10px] font-bold text-indigo-700 dark:text-indigo-300 mb-1">
            <span>Live Interim Transcription</span>
            <span className="animate-pulse">● LIVE</span>
          </div>
          <p className="text-xs italic text-foreground font-medium">
            &ldquo;{liveInterim}&rdquo;
          </p>
        </div>
      )}

      {/* Settled Turns History */}
      {transcript.confirmed_intents.length > 0 && (
        <div className="space-y-1.5 pt-2 border-t border-line">
          <span className="text-[10px] font-bold uppercase tracking-wider text-muted block">
            Settled Voice Utterances
          </span>
          {transcript.confirmed_intents.map((intent, idx) => (
            <div key={idx} className="flex items-center gap-2 text-xs bg-surface p-2 rounded-lg border border-line">
              <span className="text-emerald-600 font-bold">✓</span>
              <span className="font-medium text-foreground">{intent.text}</span>
            </div>
          ))}
        </div>
      )}

      {/* Simulated voice test triggers for demo */}
      <div className="flex flex-wrap items-center gap-1.5 pt-1">
        <span className="text-[10px] text-muted">Quick speech test:</span>
        <button
          type="button"
          disabled={isListening}
          onClick={() => simulateSpeech("2 packet amul taaza doodh add kar do")}
          className="rounded-full border border-line bg-surface hover:bg-surface-raised px-2.5 py-0.5 text-[10px] font-semibold text-foreground transition cursor-pointer disabled:opacity-50"
        >
          &quot;2 packet amul doodh...&quot;
        </button>
        <button
          type="button"
          disabled={isListening}
          onClick={() => simulateSpeech("kachi ghani mustard oil dikhao")}
          className="rounded-full border border-line bg-surface hover:bg-surface-raised px-2.5 py-0.5 text-[10px] font-semibold text-foreground transition cursor-pointer disabled:opacity-50"
        >
          &quot;mustard oil dikhao...&quot;
        </button>
        <button
          type="button"
          disabled={isListening}
          onClick={() => simulateSpeech("propose checkout for current basket")}
          className="rounded-full border border-line bg-surface hover:bg-surface-raised px-2.5 py-0.5 text-[10px] font-semibold text-foreground transition cursor-pointer disabled:opacity-50"
        >
          &quot;propose checkout...&quot;
        </button>
      </div>
    </div>
  );
}
