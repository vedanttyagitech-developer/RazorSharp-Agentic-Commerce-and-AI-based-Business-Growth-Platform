/**
 * Where the order is, drawn under the copilot's state pill.
 *
 * A buyer's turn walks a fixed spine — you discover something, it goes in the cart, you
 * approve the checkout, you pay, and an order comes back — and the rail is that spine made
 * visible so the session never has to say in prose which rung it is on. Five steps, always
 * in that order; a thin line fills up to the one that is live; the live step's dot breathes
 * in that stage's colour while the steps behind it dim and show a tick, because a passed
 * step is settled and should not compete for the eye with the one still in play.
 *
 * The sixth chip is not a sixth step. `Reserve Pay · simulator` shows ONLY while the
 * session is in the reserve stage, and it reads as an aside rather than a rung: it is the
 * demo's own payment simulator announcing itself, not another place the order travels
 * through, so it sits apart from the five and never joins the fill line.
 *
 * Nothing here computes state. `stage` is the session's own word; the component maps it to
 * an index and a colour and draws exactly that. The colour lives in one table, handed to
 * CSS as `--stage-colour` so the palette is stated once here and once in globals.css and
 * nowhere in between.
 */
"use client";

/*
 * The stage union is owned by `./use-order-stage`, which is the single source of truth for
 * it. This file previously mirrored the union locally because that module did not exist
 * when the rail was written; it exists now, so the mirror is gone and there is one
 * definition again. A second copy of a six-member union is a drift waiting to happen: a
 * seventh stage added there and forgotten here would fail as a missing rung rather than as
 * a type error.
 */
import type { OrderStage } from "./use-order-stage";

/** The five rungs, in the order the order walks them. */
const STEPS: readonly { stage: Exclude<OrderStage, "reserve">; label: string }[] = [
  { stage: "discover", label: "Discover" },
  { stage: "cart", label: "Cart" },
  { stage: "approve", label: "Approve" },
  { stage: "pay", label: "Pay" },
  { stage: "order", label: "Order" },
];

/**
 * Each stage's colour, stated once. `idle` is not a stage of its own — a session with no
 * stage yet reads as `discover`, and shares its indigo.
 */
const STAGE_COLOUR: Readonly<Record<OrderStage, string>> = {
  discover: "#7C8FF5",
  cart: "#4FD9F2",
  approve: "#B08CFF",
  pay: "#FFA14D",
  order: "#10b981",
  reserve: "#f59e0b",
};

/**
 * The rung the live stage lands on. `reserve` is not a rung, so while the simulator chip is
 * showing the fill sits at Pay — the step the simulator stands in for.
 */
function activeIndex(stage: OrderStage): number {
  if (stage === "reserve") return STEPS.findIndex((s) => s.stage === "pay");
  const i = STEPS.findIndex((s) => s.stage === stage);
  return i === -1 ? 0 : i;
}

function Tick() {
  return (
    <svg viewBox="0 0 24 24" width="10" height="10" aria-hidden="true" fill="none">
      <path
        d="M5 12.5 L10 17.5 L19 6.5"
        stroke="currentColor"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function StageRail({ stage, className }: { stage: OrderStage; className?: string }) {
  const active = activeIndex(stage);
  const colour = STAGE_COLOUR[stage] ?? STAGE_COLOUR.discover;
  const showReserve = stage === "reserve";
  // The fill spans from the first step's centre to the active step's centre: with N steps
  // laid out evenly, that is active/(N-1) of the track. A single active step leaves it at 0.
  const fillPct = STEPS.length > 1 ? (active / (STEPS.length - 1)) * 100 : 0;

  return (
    <div
      className={`stage-rail flex flex-col gap-2 rounded-lg border border-white/10 bg-white/[0.06] px-3 py-2.5 text-slate-300${
        className ? ` ${className}` : ""
      }`}
      style={{ ["--stage-colour" as string]: colour }}
      aria-label="Order stage"
    >
      <div className="relative">
        {/* the track and its fill, behind the steps */}
        <div
          className="pointer-events-none absolute left-0 right-0 top-[5px] h-px rounded bg-white/10"
          aria-hidden="true"
        />
        <div
          className="stage-fill pointer-events-none absolute left-0 top-[5px] h-px rounded"
          style={{ width: `${fillPct}%` }}
          aria-hidden="true"
        />

        <ol className="relative flex items-start justify-between gap-2">
          {STEPS.map((step, index) => {
            const isActive = index === active && stage !== "reserve";
            const isDone = index < active || (stage === "reserve" && index <= active);
            return (
              <li
                key={step.stage}
                className={`flex flex-1 flex-col items-center gap-1.5 text-center${
                  isDone ? " stage-step-done" : ""
                }`}
                aria-current={isActive ? "step" : undefined}
                data-stage={step.stage}
                data-state={isActive ? "active" : isDone ? "done" : "future"}
              >
                <span
                  className={`flex h-2.5 w-2.5 items-center justify-center rounded-full ${
                    isActive
                      ? "stage-dot-active breathing-dot"
                      : isDone
                        ? "bg-white/60 text-slate-900"
                        : "bg-white/20"
                  }`}
                  aria-hidden="true"
                >
                  {isDone ? <Tick /> : null}
                </span>
                <span
                  className={`font-mono text-[9px] uppercase tracking-[0.12em] ${
                    isActive ? "stage-label-active font-semibold" : "text-slate-400"
                  }`}
                >
                  {step.label}
                </span>
              </li>
            );
          })}
        </ol>
      </div>

      {showReserve ? (
        <div className="flex justify-center">
          <span
            className="rounded-full border border-white/10 bg-white/[0.06] px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.1em] text-amber-300"
            data-stage="reserve"
          >
            Reserve Pay · simulator
          </span>
        </div>
      ) : null}
    </div>
  );
}
