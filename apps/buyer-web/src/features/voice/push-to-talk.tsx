/**
 * The talk control. Hold it down to speak; let go and you stop.
 *
 * Hold rather than toggle, and said so on the control itself. A toggle is easier to build
 * and worse to live with: a buyer who has forgotten which state it is in is a buyer with
 * an open microphone in their kitchen, and "did I leave it on?" is not a question a
 * shopping app should make anyone ask. Holding is unambiguous -- your finger is the state.
 *
 * Keyboard operable on the same terms. Space or Enter held down is the talk gesture, and
 * both are prevented from doing their default click so that a press-and-hold does not fire
 * a stream of activations. Releasing the key, blurring the control, or dragging off it all
 * stop transmission, because every one of those is a person who has stopped meaning to
 * talk.
 *
 * Two states are drawn that a buyer would otherwise have to guess at:
 *
 *  - **Sending.** The ring is live and the meter moves. If the meter does not move while
 *    you are speaking, your microphone is not the one the browser picked.
 *  - **The echo gate.** While the assistant is speaking, the server replaces this
 *    microphone's frames with digital silence so that she does not transcribe herself
 *    (19.6). That is a real mute and it is shown as one. Interrupting still works: this
 *    client watches the local level and cuts her off before the server has heard anything
 *    (19.7), which is the whole reason barge-in lives on the client.
 */
"use client";

import { useCallback, useEffect } from "react";

import { cx } from "@/components/ui";

import type { MicState } from "./session";

export interface PushToTalkProps {
  transmitting: boolean;
  onTransmitChange: (on: boolean) => void;
  /** 0..1, from the microphone frames themselves. */
  level: number;
  micState: MicState;
  /** True between `speech_start` and `speech_end`: the server's echo gate is engaged. */
  assistantSpeaking: boolean;
  /** The socket is not open, so nothing said now would reach anything. */
  disabled?: boolean;
}

const TALK_KEYS = new Set([" ", "Spacebar", "Enter"]);

/** What the live region says. One sentence, present tense, no jargon. */
function statusLine(props: {
  disabled: boolean;
  micState: MicState;
  transmitting: boolean;
  assistantSpeaking: boolean;
}): string {
  if (props.micState === "denied") {
    return "The microphone is blocked for this site. You can still type.";
  }
  if (props.micState === "failed") {
    return "The microphone could not be opened. You can still type.";
  }
  if (props.disabled) return "Not connected. You can still type.";
  if (props.micState === "starting") return "Opening the microphone.";
  if (props.transmitting) {
    return props.assistantSpeaking
      ? "Sending. The assistant is speaking, so keep talking to interrupt her."
      : "Sending your voice.";
  }
  if (props.assistantSpeaking) {
    return "The assistant is speaking. Your microphone is muted until she finishes.";
  }
  return "Not sending. Hold the button to talk.";
}

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none">
      <rect
        x="9"
        y="3"
        width="6"
        height="11"
        rx="3"
        stroke="currentColor"
        strokeWidth="1.8"
      />
      <path
        d="M5.5 11.5 C5.5 15.4 8.4 18.2 12 18.2 C15.6 18.2 18.5 15.4 18.5 11.5 M12 18.4 V21"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** The level meter: eleven bars, so a glance reads it as a level and not as a number. */
function LevelMeter({ level, active }: { level: number; active: boolean }) {
  const bars = 11;
  const lit = Math.round(Math.max(0, Math.min(1, level)) * bars);
  return (
    <div
      role="meter"
      aria-label="Microphone level"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(Math.max(0, Math.min(1, level)) * 100)}
      className="flex h-4 items-end gap-[3px]"
    >
      {Array.from({ length: bars }, (_, index) => (
        <span
          key={index}
          aria-hidden="true"
          className={cx(
            "w-[3px] rounded-[1px] transition-[height,background-color] duration-75",
            index < lit && active ? "bg-[var(--blue)]" : "bg-[var(--ink-6)]",
          )}
          style={{ height: `${6 + index * 1}px` }}
        />
      ))}
    </div>
  );
}

export function PushToTalk({
  transmitting,
  onTransmitChange,
  level,
  micState,
  assistantSpeaking,
  disabled = false,
}: PushToTalkProps) {
  const unusable = disabled || micState === "denied" || micState === "failed";

  const begin = useCallback(() => {
    if (unusable) return;
    onTransmitChange(true);
  }, [onTransmitChange, unusable]);

  const end = useCallback(() => {
    onTransmitChange(false);
  }, [onTransmitChange]);

  // A key held down while the window loses focus never fires its keyup, which would leave
  // the microphone live behind a switched tab. The window's own blur is the backstop.
  useEffect(() => {
    if (!transmitting) return;
    const release = () => end();
    window.addEventListener("blur", release);
    return () => window.removeEventListener("blur", release);
  }, [transmitting, end]);

  const status = statusLine({ disabled, micState, transmitting, assistantSpeaking });

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-3">
        <button
          type="button"
          // `aria-pressed` is the state of the control; `aria-disabled` rather than
          // `disabled` so the reason stays reachable to a screen reader instead of the
          // control vanishing from the tab order the moment the socket drops.
          aria-pressed={transmitting}
          aria-disabled={unusable}
          aria-describedby="voice-ptt-status"
          onPointerDown={(event) => {
            event.preventDefault();
            begin();
          }}
          onPointerUp={end}
          onPointerCancel={end}
          onPointerLeave={() => {
            if (transmitting) end();
          }}
          onBlur={() => {
            if (transmitting) end();
          }}
          onKeyDown={(event) => {
            if (!TALK_KEYS.has(event.key)) return;
            // Space activates a button on key UP and Enter on key DOWN; both would fire a
            // click in the middle of a hold. The hold is the gesture, so neither default
            // is wanted.
            event.preventDefault();
            if (event.repeat) return;
            begin();
          }}
          onKeyUp={(event) => {
            if (!TALK_KEYS.has(event.key)) return;
            event.preventDefault();
            end();
          }}
          className={cx(
            "flex h-11 flex-1 items-center justify-center gap-2 rounded-[var(--r-md)] text-[14px] font-bold transition",
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--blue)]",
            unusable
              ? "cursor-not-allowed bg-[var(--tint-1)] text-[var(--ink-5)]"
              : transmitting
                ? "bg-[var(--blue)] text-white"
                : "border border-[var(--card-line)] bg-white text-[var(--ink)] hover:bg-[var(--tint-2)]",
          )}
        >
          <MicIcon />
          {transmitting ? "Listening — release to stop" : "Hold to talk"}
        </button>
        <LevelMeter level={level} active={transmitting} />
      </div>

      {/* The one place the control's state is spoken. Polite: it must not interrupt the
          assistant's reply being announced in the transcript. */}
      <p
        id="voice-ptt-status"
        role="status"
        aria-live="polite"
        className="text-[12px] leading-[1.45] text-[var(--ink-4)]"
      >
        {status}
      </p>

      {assistantSpeaking ? (
        <p className="flex items-start gap-1.5 rounded-[var(--r-sm)] bg-[var(--tint-2)] px-2.5 py-1.5 text-[12px] leading-[1.45] text-[var(--ink-3)]">
          <span aria-hidden="true" className="mt-px font-bold text-[var(--ink-4)]">
            ⏸
          </span>
          <span>
            <span className="font-semibold text-[var(--ink-2)]">Microphone muted while she speaks.</span>{" "}
            Otherwise she hears herself through your speakers and reads it as you
            interrupting. Talk over her anyway and she stops — that is decided here, on your
            device, before the server hears a word.
          </span>
        </p>
      ) : null}
    </div>
  );
}
