/**
 * The delivery strip: a quick-commerce progress track, and an honest one.
 *
 * A buyer who has just paid wants the shape every quick-commerce app gives them -- placed,
 * packed, on its way, at the door -- and this platform has no fulfilment domain to fill it
 * with. `OrderState` is a money lifecycle (CONFIRMED, FULFILMENT_BLOCKED, CANCELLED,
 * PARTIALLY_REFUNDED, REFUNDED); no courier, dispatch, or ETA exists anywhere in the API.
 *
 * WHY THIS IS NOT A MOCK
 * ----------------------
 * The obvious build is four stages with plausible timestamps and a moving dot. It is also
 * the one thing this application has promised not to do: nothing here ships a fixture, and
 * an unreachable API renders an error rather than invented data. A tracker that says "Out
 * for delivery, 8 minutes" on a platform with no dispatch signal is invented data with a
 * progress bar around it -- and it would be the single screen a reviewer could point at to
 * argue the rest of the evidence is decorative too.
 *
 * So the track is drawn in full, because the shape is the point, and each stage says which
 * kind of thing it is:
 *
 * * **Reached** -- backed by a real server field. "Placed" is `order.created_at`. "Payment
 *   confirmed" is the capture that `order.state` reaching CONFIRMED already asserts, which
 *   the evidence panel further down this same page proves independently.
 * * **Not reported** -- drawn, dimmed, and labelled. This merchant publishes no dispatch
 *   signal, so packing and travel are stages the platform cannot speak to. Saying so costs
 *   one line and buys the credibility of every figure above it.
 *
 * A cancelled or refunded order does not continue down the track; the strip says the sale
 * ended and points at the refunds, because a progress bar advancing under a refunded order
 * is worse than no progress bar.
 */
"use client";

import { cx } from "@/components/ui";
import type { Order } from "@/lib/api/types";

import { formatTimestamp } from "./capture-evidence";

/** Whether a stage is backed by a server field, merely not reported, or ended early. */
type StageStatus = "reached" | "unreported" | "ended";

interface Stage {
  key: string;
  label: string;
  status: StageStatus;
  /** Real server timestamp, when one backs this stage. Never synthesised. */
  at: string | null;
  /** What this stage means, or why the platform cannot speak to it. */
  note: string;
}

/** Order states that end the track rather than advancing it. */
const ENDED: Readonly<Record<string, string>> = {
  CANCELLED: "This sale was withdrawn, so nothing is on its way. Any money that moved is in the refunds below.",
  REFUNDED: "The full amount has settled back, so this sale is closed rather than in transit.",
  PARTIALLY_REFUNDED: "Part of this sale settled back. What remains is not tracked, because this merchant reports no dispatch.",
};

/**
 * The track for one order, derived only from fields the server sent.
 *
 * Exported for its own test: the interesting property is not how it looks but that no
 * stage is ever marked `reached` without a real field behind it.
 */
export function stagesFor(order: Order): Stage[] {
  const ended = ENDED[order.state];
  const paid = order.state !== "CANCELLED";
  const blocked = order.state === "FULFILMENT_BLOCKED";

  return [
    {
      key: "placed",
      label: "Order placed",
      status: "reached",
      at: order.created_at,
      note: "You approved a priced version and the platform bound this sale to it.",
    },
    {
      key: "paid",
      label: "Payment confirmed",
      status: paid ? "reached" : "ended",
      at: null,
      note: paid
        ? "Captured against provider evidence the platform verified. The proof is further down this page."
        : "No capture stands against this sale.",
    },
    {
      key: "packed",
      label: "Packed at the store",
      status: ended ? "ended" : "unreported",
      at: null,
      note: blocked
        ? "Fulfilment is held pending an operator decision, so the store has not been released to pack this."
        : "This merchant publishes no packing signal, so the platform cannot say whether this has happened.",
    },
    {
      key: "on-its-way",
      label: "On its way",
      status: ended ? "ended" : "unreported",
      at: null,
      note: "No courier or dispatch feed is connected, so there is no position and no arrival estimate to show.",
    },
    {
      key: "delivered",
      label: "Delivered",
      status: ended ? "ended" : "unreported",
      at: null,
      note: "Nothing reports arrival to this platform, so a delivered mark here would be a guess.",
    },
  ];
}

export function DeliveryProgress({ order, className }: { order: Order; className?: string }) {
  const stages = stagesFor(order);
  const ended = ENDED[order.state];
  const reached = stages.filter((stage) => stage.status === "reached").length;

  return (
    <section
      aria-label="Delivery progress"
      className={cx(
        "rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] bg-white px-4 py-4",
        className,
      )}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-[15px] font-bold text-[var(--ink)]">Where this order is</h2>
        <p className="text-[12px] text-[var(--ink-4)]">
          <span className="tnum">{reached}</span> of{" "}
          <span className="tnum">{stages.length}</span> stages reported
        </p>
      </div>

      {/* The track. A list, so it reads in order without the visuals. */}
      <ol className="mt-4 grid gap-0 sm:grid-cols-5">
        {stages.map((stage, index) => (
          <li key={stage.key} className="relative flex gap-3 pb-4 sm:block sm:pb-0">
            {/* Connector. Horizontal between columns on wide screens, vertical on mobile. */}
            {index < stages.length - 1 ? (
              <span
                aria-hidden="true"
                className={cx(
                  "absolute bg-[var(--card-line)]",
                  "left-[7px] top-5 h-full w-[1.5px]",
                  "sm:left-auto sm:top-[7px] sm:h-[1.5px] sm:w-full",
                  stage.status === "reached" && stages[index + 1].status === "reached"
                    ? "sm:bg-[var(--green)]"
                    : "",
                )}
              />
            ) : null}

            <Dot status={stage.status} />

            <div className="min-w-0 flex-1 sm:mt-2 sm:pr-3">
              <p
                className={cx(
                  "text-[13px] font-semibold leading-tight",
                  stage.status === "reached" ? "text-[var(--ink)]" : "text-[var(--ink-4)]",
                )}
              >
                {stage.label}
              </p>
              {stage.at ? (
                <p className="tnum mt-0.5 text-[11px] text-[var(--ink-3)]" title={stage.at}>
                  {formatTimestamp(stage.at)}
                </p>
              ) : stage.status === "unreported" ? (
                <p className="mt-0.5 text-[11px] font-medium uppercase tracking-wide text-[var(--ink-4)]">
                  Not reported
                </p>
              ) : null}
              <p className="mt-1 hidden text-[11px] leading-[1.4] text-[var(--ink-4)] sm:block">
                {stage.note}
              </p>
            </div>
          </li>
        ))}
      </ol>

      <p className="mt-3 max-w-prose border-t border-[var(--card-line)] pt-3 text-[12px] leading-[1.5] text-[var(--ink-3)]">
        {ended ? (
          ended
        ) : (
          <>
            The first two stages are read from this order. This demonstration has no courier
            integration, so the stages after payment are shown to complete the picture and are
            marked <span className="font-semibold">Not reported</span> rather than estimated —
            the platform does not invent a position it was never told.
          </>
        )}
      </p>
    </section>
  );
}

function Dot({ status }: { status: StageStatus }) {
  if (status === "reached") {
    return (
      <span
        aria-hidden="true"
        className="relative z-[1] flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-[var(--green)]"
      >
        <svg viewBox="0 0 10 10" width="8" height="8" focusable="false">
          <path
            d="M1.5 5.2 4 7.5 8.5 2.5"
            fill="none"
            stroke="white"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </span>
    );
  }
  return (
    <span
      aria-hidden="true"
      className={cx(
        "relative z-[1] h-4 w-4 shrink-0 rounded-full border-[1.5px] bg-white",
        status === "ended" ? "border-[var(--card-line)]" : "border-[var(--ink-4)]/40",
      )}
    />
  );
}
