/**
 * The one- or two-line caption between the copilot's stage rail and its conversation.
 *
 * It is a caption, not a control: it says where the buyer stands in the one journey a
 * checkout runs -- discover, cart, approve, pay, order, reserve -- and what the next word
 * to them is, and it presses nothing. Every write on this surface lives in the cards
 * below; this line only narrates. A single link out to the placed order is the one
 * exception, because reading an order is not a write.
 *
 * When a permission slip is pending its one-line summary replaces the stage line outright:
 * a slip awaiting the buyer's word is the most important thing on the screen, and the
 * stage the checkout happens to sit in is not worth saying over it.
 *
 * Dark surface only. Slate text, small type, a hairline rule in `border-white/10` where
 * one is wanted; no light tokens and no motion of its own. Money is always drawn by
 * `<Amount/>` from a `Money` the server sent -- this component computes and formats none.
 */
"use client";

import Link from "next/link";

import { Amount, cx } from "@/components/ui";
import type { Checkout } from "@/lib/api/types";

// `./use-order-stage` owns the stage union and is the single source of truth. This file
// mirrored it locally while that module was still being written; it exists now, so the
// mirror is gone. Two copies of one union drift silently -- a stage added there and missed
// here would render as a blank scene rather than fail to compile.
import type { OrderStage } from "./use-order-stage";

type StageSceneProps = {
  stage: OrderStage;
  /** The inline/current checkout when known -- carries the version, amount and attempt. */
  checkout: Checkout | null;
  orderId: string | null;
  /** A pending permission slip's one-line summary, if one is awaiting the buyer's word. */
  pendingNote: string | null;
  className?: string;
};

const LINE = "text-[13px] leading-[1.5] text-slate-300";
const STRONG = "text-slate-100";

/**
 * The caption for the buyer's place in the journey.
 *
 * A pending slip's summary, when present, is shown in place of the stage line: nothing the
 * stage would say outranks a decision the buyer is being asked for right now.
 */
export function StageScene({ stage, checkout, orderId, pendingNote, className }: StageSceneProps) {
  const body = pendingNote !== null ? <PendingLine note={pendingNote} /> : <StageLine stage={stage} checkout={checkout} orderId={orderId} />;

  return (
    <div className={cx("border-t border-white/10 px-3 py-2", className)}>
      {body}
    </div>
  );
}

function PendingLine({ note }: { note: string }) {
  return <p className={LINE}>{note}</p>;
}

function StageLine({
  stage,
  checkout,
  orderId,
}: {
  stage: OrderStage;
  checkout: Checkout | null;
  orderId: string | null;
}) {
  switch (stage) {
    case "discover":
      return <p className={LINE}>Ask for anything, or say what you need</p>;

    case "cart":
      return <p className={LINE}>Ready? Say checkout, or press Checkout</p>;

    case "approve": {
      // Version and amount are the approval card's when it exists -- the amount as a `Money`
      // the server sent, never one computed here -- falling back to the checkout's current
      // version so the line still names a version if the card has not landed yet.
      const version = checkout?.approval_card?.version ?? checkout?.current_version ?? null;
      const amount = checkout?.approval_card?.total ?? null;
      return (
        <p className={LINE}>
          {version !== null ? <>Version <span className={STRONG}>v{version}</span>, </> : null}
          <Amount money={amount} className={STRONG} />. Say yes to approve
        </p>
      );
    }

    case "pay": {
      const razorpayOrderId = checkout?.attempt?.razorpay_order_id ?? null;
      return (
        <p className={LINE}>
          Approved. Razorpay&rsquo;s sheet is next
          {razorpayOrderId !== null ? (
            <>
              {" "}
              &middot; <span className={cx("font-mono text-[11px]", STRONG)}>{razorpayOrderId}</span>
            </>
          ) : null}
        </p>
      );
    }

    case "order":
      return (
        <p className={LINE}>
          Paid.{" "}
          {orderId !== null ? (
            <Link href={`/orders/${orderId}`} className={cx("underline decoration-white/30 underline-offset-2", STRONG)}>
              Track it on the order page
            </Link>
          ) : (
            "Track it on the order page"
          )}
        </p>
      );

    case "reserve":
      return <p className={LINE}>Reserve Pay is a labelled simulator; nothing moves money</p>;
  }
}
