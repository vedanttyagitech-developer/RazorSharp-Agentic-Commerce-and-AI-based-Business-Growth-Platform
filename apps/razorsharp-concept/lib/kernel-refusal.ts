// Reading a Transaction Trust Kernel refusal into something a person can act on.
//
// WHAT THIS REPLACES
// ------------------
// The checkout used to render a refusal as one line:
//
//     `${cause.code}: ${cause.decision.explanation}`
//
// which puts two machine strings in front of a buyer -- `STALE_CHECKOUT:
// approved_hash_does_not_match_stored` -- and answers none of the three questions somebody
// looking at a refused payment actually has: what went wrong, whose action caused it, and
// whether their money is now somewhere in flight.
//
// WHY IT LIVES HERE AND NOT IN A COMPONENT
// ----------------------------------------
// Three surfaces refuse: manual Razorpay checkout, Reserve Pay, and cancellation. A reading
// written inside one of them is a reading the other two do not have, and the second copy is
// the one that goes stale when the kernel gains a code.
//
// WHAT IT IS NOT
// --------------
// Not a translation layer with opinions. Every sentence below is a restatement of a check
// that exists in `transaction_kernel.admission`, in the order that module runs them. Nothing
// here decides anything, softens a refusal, or suggests a retry the kernel would refuse
// again; `RETRYABLE` mirrors `commerce_domain.recovery.RETRYABLE` and nothing else.

import type { Decision, Delta } from './commerce';

/** The settled name. Not "the Kernel", not "the engine". */
export const KERNEL_NAME = 'Transaction Trust Kernel';

/**
 * The check that refused, named as the kernel names it.
 *
 * These are the actual gates in `admit()`, in the order it runs them, not a five-verb
 * pipeline invented for a diagram. A buyer does not need the list; they need to be told
 * which one stopped their payment, because "the store changed the price" and "somebody is
 * already paying for this" are different situations with different next steps.
 */
export type KernelCheck =
  | 'capability'
  | 'operating mode'
  | 'version currency'
  | 'recorded consent'
  | 'policy binding'
  | 'stock hold'
  | 'merchant truth'
  | 'spending authority'
  | 'single attempt';

/**
 * Whose action the refusal is about.
 *
 * `store` and `nobody` are not softenings. A price that moved after approval is the shop
 * exercising its own pricing, and telling a buyer they did something wrong would be false.
 * `copilot` exists because an AI acting beyond what it was granted is a distinct failure
 * this platform is built to make visible, and hiding it inside "something went wrong" would
 * remove the one thing worth demonstrating.
 */
export type Actor = 'buyer' | 'copilot' | 'store' | 'platform' | 'nobody';

export type RefusalReading = {
  /** The kernel gate that refused. */
  check: KernelCheck;
  /** What happened, in the buyer's words. Never "error", never "failed" unless it did. */
  title: string;
  /** Why the kernel refused: the check, stated as a fact about this purchase. */
  why: string;
  /** Whose action this was about, and what that action was. */
  fault: { actor: Actor; action: string };
  /**
   * True when a payment for this checkout already exists.
   *
   * Two different codes reach it and they are NOT the same thing: `DUPLICATE_OPERATION` is
   * this buyer's own request arriving twice and being replayed, which cost nothing;
   * `CONCURRENT_OPERATION` is a second attempt that is live right now and whose outcome
   * nobody knows yet. `settled` separates them, because "you are fine" and "do not pay
   * again until we know" are opposite instructions.
   */
  duplicate: { started: boolean; settled: boolean };
  /** The one thing to do next. */
  next: string;
  /** Whether repeating the same request could succeed. Mirrors the domain's own set. */
  retryable: boolean;
};

/** Codes after which the same logical operation may be repeated. Mirrors `recovery.py`. */
const RETRYABLE = new Set([
  'CONCURRENT_OPERATION',
  'RESERVATION_EXPIRED',
  'PAYMENT_FAILED',
  'CONNECTOR_UNAVAILABLE',
]);

/**
 * The reason keys `admit()` emits, read one at a time.
 *
 * Keyed by `explanation` rather than by `code` because one code covers several situations a
 * buyer would describe completely differently: `STALE_CHECKOUT` is both "you approved an
 * older bill" and "these are not the bytes we stored", and collapsing them loses exactly the
 * fact that distinguishes an innocent stale tab from a mismatched approval.
 */
const BY_EXPLANATION: Record<string, RefusalReading> = {
  // --- the actor was not entitled to submit at all --------------------------------------
  principal_lacks_submit_capability: {
    check: 'capability',
    title: 'The assistant was not allowed to submit this payment',
    why:
      `The ${KERNEL_NAME} checks what the acting party may do before it looks at the ` +
      'purchase at all. This request came from the AI copilot, and submitting an approved ' +
      'checkout is not among the things it holds.',
    fault: {
      actor: 'copilot',
      action:
        'The copilot tried to submit your approval for payment. Only you can do that, and ' +
        'the kernel refused it rather than accepting an action the copilot was never granted.',
    },
    duplicate: { started: false, settled: false },
    next: 'Approve and pay from this screen yourself. Nothing was charged.',
    retryable: false,
  },
  reserve_requires_authority: {
    check: 'spending authority',
    title: 'Reserve Pay had no permission to spend',
    why:
      'A Reserve Pay debit is only ever made against a saved spending permission. No ' +
      'permission was named on this request, so there was no authority to check it against.',
    fault: {
      actor: 'copilot',
      action:
        'A Reserve Pay payment was started without naming the permission it would spend ' +
        'from. The kernel will not infer one.',
    },
    duplicate: { started: false, settled: false },
    next: 'Choose a saved permission, or pay manually with Razorpay instead.',
    retryable: false,
  },
  authority_merchant_mismatch: {
    check: 'spending authority',
    title: 'That permission belongs to a different shop',
    why:
      'The saved permission names the merchant it may be spent at. This purchase is with ' +
      'another one, so it is not permission for this.',
    fault: {
      actor: 'copilot',
      action: 'A permission granted for a different shop was offered for this purchase.',
    },
    duplicate: { started: false, settled: false },
    next: 'Pay manually with Razorpay, or grant a permission for this shop first.',
    retryable: false,
  },

  // --- the platform stopped, on purpose or because it could not see -----------------------
  safe_mode_blocks_operation: {
    check: 'operating mode',
    title: 'The store has paused payments',
    why:
      'Safe Mode is on. While it is, the kernel refuses money movement deliberately rather ' +
      'than attempting it and finding out.',
    fault: {
      actor: 'platform',
      action:
        'Nobody did anything wrong. An operator put this store into Safe Mode, and it stays ' +
        'there until an operator ends it.',
    },
    duplicate: { started: false, settled: false },
    next: 'Nothing was charged. Try again once the store reports it is back.',
    retryable: false,
  },

  // --- the version is not the one being paid for ------------------------------------------
  checkout_version_not_found: {
    check: 'version currency',
    title: 'This bill no longer exists',
    why: 'The version this payment names is not in the checkout. There is nothing to pay.',
    fault: {
      actor: 'nobody',
      action: 'The bill was replaced or cleared before the payment reached the kernel.',
    },
    duplicate: { started: false, settled: false },
    next: 'Go back and build the order again.',
    retryable: false,
  },
  version_already_invalidated: {
    check: 'version currency',
    title: 'This bill was withdrawn',
    why:
      'The version was retired before this payment arrived, which is what happens when a ' +
      'newer one supersedes it. A retired version is never paid.',
    fault: {
      actor: 'store',
      action:
        'Something about the order changed, so the bill you approved was retired and a ' +
        'replacement was issued.',
    },
    duplicate: { started: false, settled: false },
    next: 'Go back and approve the current bill.',
    retryable: false,
  },
  approved_hash_does_not_match_stored: {
    check: 'version currency',
    title: 'The approved bill is not the stored one',
    why:
      'Your approval carries a fingerprint of the exact bill you saw. It does not match the ' +
      'bill this store has, so the kernel cannot tell which one you agreed to and will not ' +
      'guess.',
    fault: {
      actor: 'nobody',
      action:
        'This is the check that stops a purchase being paid against different contents from ' +
        'the ones you were shown. It fired, and nothing moved.',
    },
    duplicate: { started: false, settled: false },
    next: 'Go back, reload the order, and approve the bill on screen.',
    retryable: false,
  },
  a_newer_version_exists: {
    check: 'version currency',
    title: 'A newer bill has replaced this one',
    why:
      'A later version of this checkout exists, so the one you approved is no longer current. ' +
      'Your approval belongs to the version it was given for and does not carry forward.',
    fault: {
      actor: 'store',
      action: 'The order changed after you approved it, and a newer bill was issued.',
    },
    duplicate: { started: false, settled: false },
    next: 'Go back and approve the newer bill. Nothing was charged.',
    retryable: false,
  },

  // --- consent -----------------------------------------------------------------------------
  approval_not_found: {
    check: 'recorded consent',
    title: 'No approval was recorded for this bill',
    why:
      'The kernel reads your decision from its own record, never from the request. There is ' +
      'no recorded approval for this version.',
    fault: {
      actor: 'nobody',
      action: 'The approval did not reach the store before the payment was submitted.',
    },
    duplicate: { started: false, settled: false },
    next: 'Approve this bill again from the review screen.',
    retryable: false,
  },
  approval_already_consumed: {
    check: 'recorded consent',
    title: 'This approval has already been used',
    why:
      'An approval authorises one payment. This one was already spent, which means a payment ' +
      'for this bill has already been made.',
    fault: {
      actor: 'buyer',
      action:
        'A second payment was started for a bill that was already paid for. The kernel ' +
        'refused it rather than charging you twice.',
    },
    duplicate: { started: true, settled: true },
    next: 'Check your orders before trying again. Do not pay a second time.',
    retryable: false,
  },
  approval_expired: {
    check: 'recorded consent',
    title: 'Your approval has expired',
    why:
      'An approval is good for a limited time, because the prices and the stock it was given ' +
      'against are. This one ran out before the payment was submitted.',
    fault: {
      actor: 'nobody',
      action: 'Too much time passed between approving the bill and paying for it.',
    },
    duplicate: { started: false, settled: false },
    next: 'Go back and approve the order again. Nothing was charged.',
    retryable: false,
  },

  // --- the terms this sale is governed by ---------------------------------------------------
  reservation_not_valid: {
    check: 'stock hold',
    title: 'Your stock hold has ended',
    why:
      'The shop temporarily held these items while you reviewed the bill. That hold has ended, ' +
      'so availability and prices need checking again. This is a stock-hold timeout, not ' +
      'an expiry of your Reserve Pay permission.',
    fault: {
      actor: 'nobody',
      action: 'The hold ran out before the payment completed.',
    },
    duplicate: { started: false, settled: false },
    next: 'Refresh stock and review the latest bill. You will see any changes before paying.',
    retryable: true,
  },
  no_approved_line_can_still_be_sold: {
    check: 'merchant truth',
    title: 'Nothing on this order can still be sold',
    why:
      'The kernel re-read the shop before authorising. None of the items you approved is ' +
      'available any more, so there is no smaller version of this order to offer you.',
    fault: {
      actor: 'store',
      action: 'The items sold out between your approval and this payment.',
    },
    duplicate: { started: false, settled: false },
    next: 'Start a new order. There is nothing here to re-approve.',
    retryable: false,
  },
  merchant_state_changed_since_approval: {
    check: 'merchant truth',
    title: 'The store changed this order',
    why:
      'Before authorising, the kernel re-reads the shop and compares it with what you ' +
      'approved. Something moved, so your earlier yes no longer describes this purchase. The ' +
      'exact changes are listed below.',
    fault: {
      actor: 'store',
      action:
        'The shop changed a price, a fee or an availability after you approved. That is the ' +
        'shop’s to do; what the kernel will not do is charge you against the old figure.',
    },
    duplicate: { started: false, settled: false },
    next: 'Review the changes and approve the replacement bill.',
    retryable: false,
  },

  // --- two payments at once ------------------------------------------------------------------
  another_attempt_won: {
    check: 'single attempt',
    title: 'A payment for this order is already under way',
    why:
      'Two payments reached the kernel for this order at the same time. One went through to ' +
      'the provider; this one was stopped. Only one may exist at a time, which is what keeps a ' +
      'double tap from becoming a double charge.',
    fault: {
      actor: 'buyer',
      action:
        'A second payment was started while the first was still going. The first one is the ' +
        'live one — this one never reached a provider.',
    },
    duplicate: { started: true, settled: false },
    next:
      'Do not pay again. Wait for the payment already running to report its outcome.',
    retryable: true,
  },
  a_live_payment_attempt_already_exists_for_this_checkout: {
    check: 'single attempt',
    title: 'A payment for this order is already under way',
    why:
      'This checkout already has a live payment attempt. Its outcome is not known yet, so a ' +
      'second one is refused: starting it is how one purchase becomes two charges.',
    fault: {
      actor: 'buyer',
      action:
        'A second payment was started for an order that is already being paid for. No ' +
        'provider order was created for this one.',
    },
    duplicate: { started: true, settled: false },
    next:
      'Do not pay again. The payment already running will report whether it succeeded.',
    retryable: true,
  },
};

/** Codes whose reading holds even when the reason key is one we have not met. */
const BY_CODE: Record<string, RefusalReading> = {
  DUPLICATE_OPERATION: {
    check: 'single attempt',
    title: 'This is the payment you already started',
    why:
      'The same request arrived twice — a retry, a reload, or a second tap. The kernel ' +
      'recognised it by the key it carries and replayed its original answer instead of ' +
      'executing again.',
    fault: {
      actor: 'nobody',
      action:
        'Nothing went wrong and nothing was charged twice. One request was made twice and ' +
        'counted once.',
    },
    duplicate: { started: true, settled: true },
    next: 'Carry on. This is your existing payment, not a second one.',
    retryable: false,
  },
  CONCURRENT_OPERATION: BY_EXPLANATION.a_live_payment_attempt_already_exists_for_this_checkout,
  REAPPROVAL_REQUIRED: BY_EXPLANATION.merchant_state_changed_since_approval,
  SOLD_OUT: BY_EXPLANATION.no_approved_line_can_still_be_sold,
  RESERVATION_EXPIRED: BY_EXPLANATION.reservation_not_valid,
  STALE_CHECKOUT: BY_EXPLANATION.a_newer_version_exists,
  AUTHORITY_REVOKED: {
    check: 'spending authority',
    title: 'That spending permission has been withdrawn',
    why:
      'The saved permission this payment would spend from is revoked. A revoked permission ' +
      'authorises nothing, including payments already in progress against it.',
    fault: {
      actor: 'buyer',
      action: 'The permission was revoked before this payment was authorised.',
    },
    duplicate: { started: false, settled: false },
    next: 'Pay manually with Razorpay, or grant a new permission.',
    retryable: false,
  },
  AUTHORITY_INSUFFICIENT: {
    check: 'spending authority',
    title: 'The saved permission does not cover this purchase',
    why:
      'The kernel checks the permission against this exact purchase: its amount, its ' +
      'products, and what is left of its balance. This purchase falls outside it.',
    fault: {
      actor: 'copilot',
      action:
        'Reserve Pay attempted a purchase larger than the permission allows, or outside the ' +
        'products it covers.',
    },
    duplicate: { started: false, settled: false },
    next: 'Pay manually with Razorpay, or raise the permission and try again.',
    retryable: false,
  },
  CONNECTOR_UNAVAILABLE: {
    check: 'merchant truth',
    title: 'The store could not be read',
    why:
      'Authorising means re-reading the shop’s own prices and stock. That read did not ' +
      'answer, so the kernel stopped before deciding anything.',
    fault: {
      actor: 'platform',
      action:
        'Nothing about your order was found wanting. A system the kernel depends on did not ' +
        'reply.',
    },
    duplicate: { started: false, settled: false },
    next: 'Nothing was charged. Try again in a moment.',
    retryable: true,
  },
  SAFE_MODE_ACTIVE: BY_EXPLANATION.safe_mode_blocks_operation,
  PAYMENT_FAILED: {
    check: 'single attempt',
    title: 'The payment did not go through',
    why: 'The provider reported a failed payment. Nothing was captured.',
    fault: { actor: 'nobody', action: 'The payment was attempted and declined.' },
    duplicate: { started: true, settled: true },
    next: 'You can try paying for the same order again.',
    retryable: true,
  },
};

/** The reading for a code the kernel has and this build has not met. Never invents a cause. */
function unmapped(decision: Decision): RefusalReading {
  return {
    check: 'merchant truth',
    title: `The ${KERNEL_NAME} refused this payment`,
    why:
      `The kernel answered ${decision.code}. This screen does not have a plain-language ` +
      'reading of that code, so it is shown as the kernel said it rather than guessed at.',
    fault: {
      actor: 'nobody',
      action:
        'No money moved. A refusal is the kernel working, not a fault — it declined ' +
        'before authorising anything.',
    },
    duplicate: { started: false, settled: false },
    next: 'Go back and try the order again. Nothing was charged.',
    retryable: RETRYABLE.has(decision.code),
  };
}

/**
 * What a refused decision means, for a person.
 *
 * `explanation` is consulted first because it is the more specific of the two: several
 * situations share one code and differ only here. `code` is the fallback, and an unmapped
 * code is reported as itself rather than described wrongly.
 */
export function readRefusal(decision: Decision): RefusalReading {
  const reading =
    BY_EXPLANATION[decision.explanation] ?? BY_CODE[decision.code] ?? unmapped(decision);
  // `retryable` follows the domain's own set rather than the table, so the two cannot drift.
  return { ...reading, retryable: RETRYABLE.has(decision.code) };
}

/**
 * What each ``reason`` the kernel emits means, in the buyer's words.
 *
 * Keyed by the kernel's own reason rather than by the field name, because the reason is
 * what ``transaction_kernel.material`` decided this difference *was* -- a quantity falling
 * to zero is reported as ``item_unavailable``, not as "quantity changed" -- and rewording it
 * from the path would lose that. The set is closed (``_TOP_LEVEL_REASON`` and
 * ``_LINE_REASON``), so an unknown reason means the kernel gained one and the path is used
 * verbatim rather than described wrongly.
 */
const REASON_LABEL: Record<string, string> = {
  currency_changed: 'Currency',
  subtotal_changed: 'Items subtotal',
  tax_changed: 'Tax',
  delivery_fee_changed: 'Delivery fee',
  discount_changed: 'Discount',
  total_changed: 'Total',
  quantity_changed: 'Quantity',
  item_unavailable: 'No longer available',
  item_added: 'Added by the store',
  unit_price_changed: 'Unit price',
  line_total_changed: 'Line total',
  line_tax_changed: 'Line tax',
};

/**
 * The field paths whose numbers are minor units.
 *
 * A closed set, not a `_minor` suffix test, because `total` carries no suffix and *is*
 * money: admission reports it separately from the document's own keys, and a suffix rule
 * printed a real ₹91.50 refusal as "9150".
 */
const MONEY_LEAVES = new Set([
  'total',
  'total_minor',
  'subtotal_minor',
  'tax_minor',
  'delivery_fee_minor',
  'discount_minor',
  'unit_minor',
  'line_minor',
]);

/** `lines[ENGL-BAKE-004].unit_minor` -> the SKU and the key. Brackets, not dots. */
const LINE_PATH = /^lines\[([^\]]+)\]\.(.+)$/;

export type DeltaReading = {
  label: string;
  /** The product this moved on, when it moved on one. */
  sku: string | null;
  approved: string;
  current: string;
  /** `approved`/`current` are minor units and want the caller's money formatter. */
  money: boolean;
};

/**
 * One change the kernel found, said in words.
 *
 * Formatting is left to the caller: this module has no money formatter, and a second one
 * is how two screens come to disagree about a rupee.
 */
export function readDelta(delta: Delta): DeltaReading {
  const line = LINE_PATH.exec(delta.field_path);
  const leaf = line ? line[2] : delta.field_path;
  const label =
    (delta.reason ? REASON_LABEL[delta.reason] : undefined) ??
    REASON_LABEL[`${leaf}_changed`] ??
    leaf.replace(/_/g, ' ');
  const money = MONEY_LEAVES.has(leaf);
  return {
    label,
    sku: line ? line[1] : null,
    approved: show(delta.approved),
    current: show(delta.current),
    money,
  };
}

/** A delta value as text. Minor-unit numbers pass through for the caller to format. */
function show(value: unknown): string {
  if (value === null || value === undefined) return '\u2014';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if(typeof value==='string')return value;
  if(typeof value==='number'||typeof value==='bigint')return value.toString();
  return JSON.stringify(value) ?? 'Unavailable';
}
