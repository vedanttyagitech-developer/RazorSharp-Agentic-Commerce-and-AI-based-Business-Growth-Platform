"use client";

/**
 * The copilot's dock: a composer that is always there, and the conversation above it.
 *
 * It is mounted once in the shell so the merchant can ask from any page without losing the
 * one they are on. That is the whole reason it is a dock rather than a route: a question
 * about catalogue health asked from the refunds screen should not navigate away from the
 * refunds screen.
 *
 * It is drawn in the console's language and not the storefront's -- flat, square, hairline
 * rules, tabular figures -- because the two surfaces do not share authority and should not
 * share a costume. The one colour it borrows is Razorpay blue, which in this console means
 * *information*, never *committed*.
 *
 * The panel holds no capability. It sends one message to `POST /v1/merchant/agent/turn` and
 * renders what comes back, and the only control in it that changes anything is the apply
 * press on a proposal card -- which is a person acting, through the audited scenario
 * endpoint, on data the agent could only describe.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { Chip, cx } from "@/components/ui";
import { api } from "@/lib/api/client";
import type { Turn } from "@/lib/api/types";

import { Opening, Transcript, specialistName, type Message } from "./transcript";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** The copilot's mark: a spark on a ledger rule, in the console's own geometry. */
function CopilotMark({ size = 16 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" fill="currentColor">
      <path d="M9.6 2.6 L11 7.6 L16 9 L11 10.4 L9.6 15.4 L8.2 10.4 L3.2 9 L8.2 7.6 Z" />
      <path d="M17.4 12.4 L18.1 14.9 L20.6 15.6 L18.1 16.3 L17.4 18.8 L16.7 16.3 L14.2 15.6 L16.7 14.9 Z" />
      <rect x="3.2" y="19.4" width="17.4" height="1.6" rx="0.2" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true" fill="none">
      <path d="M5 5 L15 15 M15 5 L5 15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true" fill="none">
      <path
        d="M10 16 V4.5 M5.5 9 L10 4.5 L14.5 9"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function CopilotDock() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [lastTurn, setLastTurn] = useState<Turn | null>(null);

  const dockRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const inFlight = useRef<AbortController | null>(null);
  // A counter rather than a random value, so server and client agree on every key through
  // hydration.
  const sequence = useRef(0);

  const nextId = useCallback(() => {
    sequence.current += 1;
    return `m${sequence.current}`;
  }, []);

  const close = useCallback(() => setOpen(false), []);

  // Escape closes, and Tab stays inside the dock while it is open. Captured on the
  // document so a keystroke in the composer reaches this before anything else.
  useEffect(() => {
    if (!open) return;
    const node = dockRef.current;
    if (!node) return;

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        // Focus goes to the disclosure button and not back to the composer: the composer
        // opens the panel when it takes focus, so returning focus there would reopen what
        // Escape had just closed and the key would appear to do nothing.
        toggleRef.current?.focus();
        return;
      }
      if (event.key !== "Tab" || node === null) return;
      const focusable = Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (element) => element.offsetParent !== null || element === document.activeElement,
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !node.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [open]);

  // On a phone the panel covers the console, so the page behind it must not scroll under
  // it. On a desktop it sits beside the dashboard, and locking the page would take the
  // screen away from an operator who opened an assistant to help them read it.
  useEffect(() => {
    if (!open) return;
    if (!window.matchMedia("(max-width: 639px)").matches) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  useEffect(() => {
    const node = transcriptRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, pending]);

  useEffect(() => () => inFlight.current?.abort(), []);

  const send = useCallback(
    async (raw: string) => {
      const message = raw.trim();
      if (!message || pending) return;

      setOpen(true);
      setMessages((previous) => [...previous, { id: nextId(), role: "merchant", text: message }]);
      setDraft("");
      setPending(true);

      const controller = new AbortController();
      inFlight.current = controller;
      try {
        const turn = await api.merchantTurn({ message }, controller.signal);
        setLastTurn(turn);
        setMessages((previous) => [
          ...previous,
          { id: nextId(), role: "copilot", text: turn.reply, turn },
        ]);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        // The failure is rendered as the problem document it was. Nothing is composed in
        // place of an answer the platform did not give.
        setMessages((previous) => [...previous, { id: nextId(), role: "problem", error }]);
      } finally {
        if (inFlight.current === controller) inFlight.current = null;
        setPending(false);
      }
    },
    [nextId, pending],
  );

  return (
    <>
      {/*
        The scrim exists because focus is trapped: a surface that takes the keyboard should
        say so on screen too. It is light rather than a blackout, since the console behind
        it is the thing the merchant is asking about.
      */}
      {open && (
        <div
          className="fixed inset-0 z-30 bg-[color-mix(in_srgb,var(--ink)_18%,transparent)]"
          onClick={close}
          aria-hidden="true"
        />
      )}

      {/*
        An <aside> rather than a <div>: closed, the dock is a launcher sitting in no
        landmark at all, which leaves it unreachable by landmark navigation and outside
        any region a reader can skip. It is complementary to the page rather than part of
        it -- the console's pages nest their own <main>, and a question asked from the
        refunds screen is not refunds content. That boundary is also what keeps the
        console's own specs honest: they enumerate controls within <main> to prove a
        screen offers no action it should not, and a chat composer inside that landmark
        would quietly falsify the claim.
      */}
      <aside
        ref={dockRef}
        aria-label="Merchant Copilot"
        role={open ? "dialog" : undefined}
        aria-modal={open ? true : undefined}
        className="fixed right-0 bottom-0 z-40 flex w-full max-w-[620px] flex-col p-2 sm:p-4"
      >
        {open && (
          <section
            id="copilot-panel"
            data-ai-state={pending ? "thinking" : messages.length ? "answered" : "idle"}
            className="ai-box mb-2 flex max-h-[min(74vh,680px)] flex-col overflow-hidden rounded-[var(--r-lg)] border border-[var(--line)] bg-[var(--bg)] shadow-[0_10px_30px_rgba(25,40,57,0.18)]"
          >
            <header className="flex shrink-0 flex-wrap items-start justify-between gap-2 border-b border-[var(--line)] bg-[var(--surface)] px-4 py-3">
              <div className="min-w-0">
                <p className="flex items-center gap-1.5 text-[13px] font-semibold text-[var(--ink)]">
                  <span className="text-[var(--brand)]">
                    <CopilotMark />
                  </span>
                  Merchant Copilot
                </p>
                {lastTurn ? (
                  <p className="mono mt-0.5 text-[var(--faint)] break-id">
                    {specialistName(lastTurn.specialist)} · {lastTurn.principal_id}
                  </p>
                ) : (
                  <p className="mt-0.5 text-[11.5px] text-[var(--muted)]">
                    Routing is a lexicon over your words, not a model&rsquo;s guess. Which
                    specialist answered, and why, appears on every reply.
                  </p>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Chip tone="info">proposes only</Chip>
                <button
                  type="button"
                  onClick={close}
                  aria-label="Close the Merchant Copilot"
                  className="flex h-7 w-7 items-center justify-center rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--raised)] text-[var(--muted)] transition-colors hover:text-[var(--ink)]"
                >
                  <CloseIcon />
                </button>
              </div>
            </header>

            <div ref={transcriptRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
              {messages.length === 0 && !pending ? (
                <Opening onPick={(question) => void send(question)} />
              ) : (
                <Transcript messages={messages} pending={pending} />
              )}
            </div>
          </section>
        )}

        <form
          onSubmit={(event) => {
            event.preventDefault();
            void send(draft);
          }}
          className="flex items-center gap-2 rounded-[var(--r-lg)] border border-[var(--line)] bg-[var(--surface)] p-2 shadow-[0_6px_18px_rgba(25,40,57,0.12)]"
        >
          {/*
            The mark is the disclosure control rather than an ornament, so the conversation
            can be opened and closed from the keyboard without typing anything into the
            composer -- and so the expanded state is announced by something that is allowed
            to carry it. A text box is not.
          */}
          <button
            type="button"
            ref={toggleRef}
            onClick={() => setOpen((was) => !was)}
            aria-expanded={open}
            aria-controls={open ? "copilot-panel" : undefined}
            aria-label={open ? "Hide the conversation" : "Show the conversation"}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--r-sm)] text-[var(--brand)] transition-colors hover:bg-[var(--raised)]"
          >
            <CopilotMark size={18} />
          </button>
          <label htmlFor="copilot-composer" className="sr-only">
            Ask the Merchant Copilot
          </label>
          <input
            id="copilot-composer"
            ref={inputRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onFocus={() => setOpen(true)}
            placeholder="Ask about the catalogue, inventory or checkouts"
            autoComplete="off"
            className="h-9 min-w-0 flex-1 rounded-[var(--r-sm)] bg-[var(--raised)] px-2.5 text-[12.5px] text-[var(--ink)] placeholder:text-[var(--faint)]"
          />
          <button
            type="submit"
            disabled={pending || draft.trim().length === 0}
            aria-label="Send to the Merchant Copilot"
            className={cx(
              "flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--r-sm)] border transition-colors",
              "border-[color-mix(in_srgb,var(--brand)_60%,transparent)] bg-[color-mix(in_srgb,var(--brand)_18%,transparent)] text-[var(--brand)]",
              "hover:bg-[color-mix(in_srgb,var(--brand)_26%,transparent)] disabled:cursor-not-allowed disabled:opacity-45",
            )}
          >
            <SendIcon />
          </button>
        </form>
      </aside>
    </>
  );
}
