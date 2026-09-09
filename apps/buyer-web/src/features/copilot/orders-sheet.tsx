/**
 * What this buyer has already bought, and the way back into a conversation about one.
 *
 * Order help is deliberately not always-on. An assistant that opens with "need help with
 * an order?" to someone who has never bought anything is offering to solve a problem
 * nobody has; and a copilot that treats every sentence as possible support noise answers
 * shopping questions badly. So the history lives here, behind its own icon, and choosing an
 * order is what turns the conversation to it -- the buyer asks, in their own words, through
 * the same composer as everything else.
 *
 * Every figure and every state on this screen is the server's. Nothing is inferred from a
 * date, and no status is softened: an order that failed says so.
 */

"use client";

import { useEffect, useState } from "react";

import { Amount, cx } from "@/components/ui";
import { formatDuration } from "@/features/orders/capture-evidence";
import { api } from "@/lib/api/client";
import type { OrderSummary } from "@/lib/api/types";

/** How the order's own state reads to a buyer. Never a guess, never softened. */
function stateTone(state: string): { label: string; className: string } {
  switch (state) {
    case "CAPTURED":
    case "PAID":
      return { label: "Paid", className: "bg-emerald-400/15 text-emerald-300" };
    case "AUTHORIZED":
      return { label: "Authorized", className: "bg-sky-400/15 text-sky-300" };
    case "FAILED":
    case "PAYMENT_FAILED":
      return { label: "Payment failed", className: "bg-rose-400/15 text-rose-300" };
    case "REFUNDED":
    case "PARTIALLY_REFUNDED":
      return { label: "Refunded", className: "bg-violet-400/15 text-violet-300" };
    case "REFUND_PENDING":
      return { label: "Refund on the way", className: "bg-violet-400/15 text-violet-300" };
    case "UNKNOWN":
    case "RECONCILING":
      // Deliberately not "failed". The money may have moved, and telling a buyer whose
      // card was charged that it did not is the one wrong answer available here.
      return { label: "Being confirmed", className: "bg-amber-400/15 text-amber-300" };
    default:
      return {
        label: state.replaceAll("_", " ").toLowerCase(),
        className: "bg-white/10 text-slate-300",
      };
  }
}

/** Order states with a capture behind them, which is what a refund is taken from. */
const REFUNDABLE: ReadonlySet<string> = new Set(["CAPTURED", "PAID", "PARTIALLY_REFUNDED"]);

function whenOf(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function OrdersSheet({
  open,
  onClose,
  onAsk,
}: {
  open: boolean;
  onClose: () => void;
  /** Send a sentence to the copilot, as though the buyer had typed it. */
  onAsk: (text: string) => void;
}) {
  const [orders, setOrders] = useState<readonly OrderSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void (async () => {
      setLoading(true);
      setProblem(null);
      try {
        const page = await api.orders({ limit: 25, signal: controller.signal });
        setOrders(page.orders);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setProblem("Your orders could not be read just now.");
      } finally {
        setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [open]);

  if (!open) return null;

  return (
    <section
      aria-label="Your orders"
      className="absolute inset-0 z-20 flex flex-col bg-[var(--rzp-navy)]/95 backdrop-blur-xl"
    >
      <header className="flex shrink-0 items-center justify-between gap-2 border-b border-[var(--rzp-line)] px-4 py-3">
        <h2 className="text-sm font-semibold text-white">Your orders</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close your orders"
          className="flex size-9 items-center justify-center rounded-full border border-white/12 text-slate-300 transition-colors hover:bg-white/10 hover:text-white"
        >
          <svg viewBox="0 0 16 16" className="size-4" aria-hidden="true">
            <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {problem !== null ? (
          <p className="mt-8 text-center text-xs text-amber-300">{problem}</p>
        ) : loading && orders.length === 0 ? (
          <p className="mt-8 text-center text-xs text-slate-500">Reading your orders…</p>
        ) : orders.length === 0 ? (
          <p className="mt-8 text-center text-xs leading-relaxed text-slate-500">
            You have not placed an order yet.
            <br />
            When you do, it will be here with everything you can ask about it.
          </p>
        ) : (
          <ul role="list" className="mx-auto max-w-2xl space-y-2">
            {orders.map((order) => {
              const tone = stateTone(order.state);
              // The list endpoint carries no line items -- it is a ledger of orders, not of
              // carts -- so this names the order rather than inventing a summary of what was
              // in it. By its reference, which is the half of an order's identity a person
              // can read out; the id is what the platform joins on, and is what an API too
              // old to send a reference leaves this with.
              const summary = order.reference ?? order.order_id;
              return (
                <li
                  key={order.order_id}
                  className="rounded-2xl border border-[var(--rzp-line)] bg-white/[0.03] p-3"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate text-[13px] font-medium text-slate-100">{summary}</p>
                      <p className="mt-0.5 font-mono text-[10px] text-slate-500">
                        {whenOf(order.created_at)}
                        {/*
                          How long the sale took, on the surface this shop is actually
                          driven from. The figure was added to the order screen first,
                          which is a storefront page a link lands on -- not the place
                          anybody watching a demonstration is looking.

                          "Checkout", never "cart": the span starts when the kernel froze
                          version 1 and held the stock, and a cart has no expiry, no hold
                          and no price that stays put. Naming the cart would claim a
                          longer span than the one being reported.
                        */}
                        {formatDuration(order.duration_seconds)
                          ? ` · checkout to confirmed in ${formatDuration(order.duration_seconds)}`
                          : ""}
                        {order.refund_count > 0
                          ? ` · ${order.refund_count} refund${order.refund_count === 1 ? "" : "s"}`
                          : ""}
                      </p>
                    </div>
                    <div className="shrink-0 text-right">
                      <p className="font-mono text-[13px] font-semibold tabular-nums text-white">
                        <Amount money={order.amount} />
                      </p>
                      <span
                        className={cx(
                          "mt-1 inline-block rounded-full px-2 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-wider",
                          tone.className,
                        )}
                      >
                        {tone.label}
                      </span>
                    </div>
                  </div>
                  <div className="mt-2.5 flex gap-1.5">
                    <button
                      type="button"
                      onClick={() => onAsk(`where is my order ${order.reference ?? order.order_id}`)}
                      className="rounded-full border border-white/12 px-2.5 py-1 text-[11px] font-medium text-slate-300 transition-colors hover:bg-white/10 hover:text-white"
                    >
                      Track it
                    </button>
                    <button
                      type="button"
                      onClick={() => onAsk(`I need help with order ${order.reference ?? order.order_id}`)}
                      className="rounded-full border border-white/12 px-2.5 py-1 text-[11px] font-medium text-slate-300 transition-colors hover:bg-white/10 hover:text-white"
                    >
                      I need help
                    </button>
                    {/* Only where the merchant offered returns on THIS sale.
                        `return_offered` is resolved from the Policy-at-Sale Receipt, not
                        from what the shop publishes today, so a merchant who withdraws
                        returns tomorrow does not take the button off an order sold under
                        them -- and one who never offered returns never grows it. A shop
                        should not put a control in front of a buyer that it knows leads
                        to a refusal, and it should not hide one somebody was promised.

                        A door, not an instruction: it asks the copilot, which is where a
                        person decides what is owed. Nothing here starts a return. */}
                    {order.return_offered ? (
                      <button
                        type="button"
                        onClick={() =>
                          onAsk(`I want to return an item from order ${order.reference ?? order.order_id}`)
                        }
                        className="rounded-full border border-white/12 px-2.5 py-1 text-[11px] font-medium text-slate-300 transition-colors hover:bg-white/10 hover:text-white"
                      >
                        Return an item
                      </button>
                    ) : null}
                    {/* Only where money actually moved. Offering a refund on an order that
                        was never paid for, or one already fully refunded, is offering
                        something the kernel will decline -- and a shop should not put a
                        control in front of a buyer that it knows leads to a refusal.

                        The button is a door, not an instruction. It used to synthesise the
                        sentence "refund order RS-1234" and hand it to the chat, which
                        parsed it and moved money on the spot. Where it goes now is the
                        order's own screen, which states the kernel's figure and asks. */}
                    {REFUNDABLE.has(order.state) && order.refunded_minor < order.amount_minor ? (
                      <button
                        type="button"
                        onClick={() => {
                          window.location.href = `/orders/${order.order_id}`;
                        }}
                        className="rounded-full border border-white/12 px-2.5 py-1 text-[11px] font-medium text-slate-300 transition-colors hover:bg-white/10 hover:text-white"
                      >
                        Refund this order
                      </button>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
