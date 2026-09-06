/**
 * Where the buyer says things, and the shortcuts for what they usually say next.
 *
 * Enter sends, explicitly. The browser will normally submit a form when Enter is pressed
 * in a text field, but it is a behaviour with conditions attached, and during development
 * it silently stopped firing here: a buyer typed a sentence, pressed Enter, and nothing
 * whatsoever happened -- indistinguishable, from their side, from an assistant ignoring
 * them. Sending from the key handler makes it part of this component rather than part of
 * the environment.
 *
 * The chips send ordinary sentences down the ordinary path. Nothing they do is unavailable
 * to someone who types, which is what keeps them a convenience rather than a second,
 * privileged way to drive the shop.
 */

"use client";

import { useRef, useState } from "react";

import { cx } from "@/components/ui";

import type { Chip } from "./flow";

export function Composer({
  chips,
  pending,
  onSend,
  listening,
  micBlocked,
  speaking,
  onToggleMic,
}: {
  chips: readonly Chip[];
  pending: boolean;
  onSend: (text: string) => void;
  /** The microphone is open and the socket is up: the buyer can just talk. */
  listening: boolean;
  /** The browser refused the microphone. Typing is unaffected and must keep working. */
  micBlocked: boolean;
  /** The assistant is speaking. Shown so the buyer knows why it is not listening. */
  speaking: boolean;
  onToggleMic: () => void;
}) {
  const [draft, setDraft] = useState("");
  const input = useRef<HTMLInputElement | null>(null);

  function send(text: string) {
    const trimmed = text.trim();
    if (trimmed.length === 0 || pending) return;
    onSend(trimmed);
    setDraft("");
    input.current?.focus();
  }

  return (
    <div className="shrink-0 border-t border-[var(--rzp-line)] px-4 py-3">
      <div className="mx-auto max-w-3xl space-y-2">
        {/* Named above the field, not only inside it: a placeholder disappears the moment
            anyone types, and this line is what tells a buyer standing in the store or in
            their orders that they can still just ask. */}
        <p className="px-1 text-[11px] font-medium text-slate-400">
          Ask RazorAI to see what you want
        </p>

        {chips.length > 0 ? (
          <div
            role="list"
            aria-label="Suggestions"
            className="flex gap-1.5 overflow-x-auto pb-0.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          >
            {chips.map((chip) => (
              <button
                key={chip.label}
                type="button"
                role="listitem"
                disabled={pending}
                onClick={() => send(chip.send)}
                className="shrink-0 rounded-full border border-white/12 bg-white/[0.04] px-3 py-1 text-[12px] font-medium text-slate-300 transition-colors hover:border-white/25 hover:bg-white/10 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
              >
                {chip.label}
              </button>
            ))}
          </div>
        ) : null}

        <form
          onSubmit={(event) => {
            event.preventDefault();
            send(draft);
          }}
          className="flex items-center gap-2 rounded-2xl border border-white/12 bg-white/[0.04] p-1.5 transition-colors focus-within:border-white/30"
        >
          {/* The microphone opens on a press because no browser will hand over a capture
              device without one, and then it stays open. Holding a button down to finish a
              sentence is a walkie-talkie, and this is meant to be a conversation. */}
          <button
            type="button"
            onClick={onToggleMic}
            disabled={micBlocked}
            aria-pressed={listening}
            aria-label={
              micBlocked
                ? "The browser refused the microphone; type instead"
                : listening
                  ? "Stop listening"
                  : "Start listening"
            }
            title={
              micBlocked
                ? "The browser refused the microphone. Typing does everything speaking does."
                : listening
                  ? "Listening. Press to stop."
                  : "Press to talk"
            }
            className={cx(
              "flex size-9 shrink-0 items-center justify-center rounded-full transition-colors",
              micBlocked
                ? "border border-white/10 text-slate-600"
                : speaking
                  ? "bg-[#B08CFF] text-white"
                  : listening
                    ? "bg-emerald-500 text-white"
                    : "border border-white/15 text-slate-300 hover:bg-white/10 hover:text-white",
            )}
          >
            <svg viewBox="0 0 20 20" className="size-4" fill="none" aria-hidden="true">
              <path
                d="M10 3a2 2 0 012 2v5a2 2 0 11-4 0V5a2 2 0 012-2zM5.5 9.5a4.5 4.5 0 009 0M10 14v3"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              {micBlocked ? (
                <path d="M4 4l12 12" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              ) : null}
            </svg>
          </button>

          <label htmlFor="copilot-composer" className="sr-only">
            Ask RazorAI to see what you want
          </label>
          <input
            id="copilot-composer"
            ref={input}
            type="text"
            value={draft}
            autoComplete="off"
            maxLength={4000}
            placeholder="Ask RazorAI to see what you want"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
              event.preventDefault();
              send(draft);
            }}
            className="h-10 min-w-0 flex-1 bg-transparent px-2.5 text-[13.5px] text-slate-100 placeholder:text-slate-500 focus:outline-none"
          />
          <button
            type="submit"
            disabled={pending || draft.trim().length === 0}
            aria-label="Send"
            className={cx(
              "flex size-9 shrink-0 items-center justify-center rounded-full transition-colors",
              pending || draft.trim().length === 0
                ? "bg-white/10 text-slate-500"
                : "rzp-action text-white",
            )}
          >
            <svg viewBox="0 0 20 20" className="size-4" fill="none" aria-hidden="true">
              <path
                d="M10 16V4M5 9l5-5 5 5"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </form>

        <p className="text-center text-[10px] leading-relaxed text-slate-500">
          {micBlocked
            ? "The browser refused the microphone. Typing does everything speaking does. "
            : listening
              ? "Listening — just talk. "
              : ""}
          RazorAI only proposes. It never approves and never pays — a yes answers the card in
          front of you.
        </p>
      </div>
    </div>
  );
}
