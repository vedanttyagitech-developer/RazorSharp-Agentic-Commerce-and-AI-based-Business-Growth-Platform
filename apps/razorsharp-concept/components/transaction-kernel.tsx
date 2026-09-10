'use client';
// The two things the Transaction Trust Kernel knows that no screen was showing.
//
// 1. WHY IT REFUSED. A refusal used to reach the buyer as `STALE_CHECKOUT:
//    approved_hash_does_not_match_stored` -- two machine strings and no answer to "what do I
//    do now". `KernelRefusal` renders the reading in `lib/kernel-refusal.ts`: the check that
//    stopped it, whose action it was about, whether a second payment is in flight, exactly
//    what moved, and the identifiers that let anyone follow the decision through the audit.
//
// 2. HOW LONG IT TOOK. The backend measures a sale in four spans and the checkout never
//    showed one of them. `KernelTiming` renders them, marking the single span Razorpay can
//    see, because the claim being demonstrated is that this platform can time the half of a
//    transaction a payment provider cannot.
//
// Both are named for the kernel on purpose. "The Kernel" appears in this codebase as a
// shorthand between people who already know what it is; a buyer reading a refusal does not,
// and neither does a judge.

import { AlertTriangle, Bot, Clock3, Copy, Store, User, Layers, ShieldAlert } from 'lucide-react';
import { money } from '@/lib/demo';
import type { Decision } from '@/lib/commerce';
import { type Order } from '@/lib/commerce';
import { KERNEL_NAME, readDelta, readRefusal, type Actor } from '@/lib/kernel-refusal';
import { seconds } from '@/lib/order-timing';

const ACTOR_LABEL: Record<Actor, string> = {
  buyer: 'Your action',
  copilot: 'The AI copilot’s action',
  store: 'The store’s action',
  platform: 'The platform',
  nobody: 'Nobody’s mistake',
};

const ACTOR_ICON: Record<Actor, typeof User> = {
  buyer: User,
  copilot: Bot,
  store: Store,
  platform: ShieldAlert,
  nobody: Layers,
};

/**
 * A refused payment, in full.
 *
 * `decision` is the kernel's own answer, which arrives with HTTP 200 and `allowed: false`
 * (ADR 0003 D15). It is rendered here unaltered: nothing on this panel is computed, softened
 * or inferred, and the identifiers at the bottom are the ones written into the audit stream.
 */
export function KernelRefusal({ decision, onBack, onRetry }: {
  decision: Decision;
  onBack?: () => void;
  onRetry?: () => void;
}) {
  const reading = readRefusal(decision);
  const ActorIcon = ACTOR_ICON[reading.fault.actor];
  const deltas = decision.deltas ?? [];

  return (
    <div className="kernel-refusal" role="alert">
      <header>
        <span className="kernel-mark">
          <ShieldAlert size={13} /> {KERNEL_NAME}
        </span>
        <span className="kernel-verdict">REFUSED AT · {reading.check.toUpperCase()}</span>
      </header>

      <h3>{reading.title}</h3>
      <p className="kernel-why">{reading.why}</p>

      {/* Whose action, said plainly. This is the question a refusal leaves behind. */}
      <div className={`kernel-fault kernel-fault-${reading.fault.actor}`}>
        <ActorIcon size={17} />
        <div>
          <strong>{ACTOR_LABEL[reading.fault.actor]}</strong>
          <p>{reading.fault.action}</p>
        </div>
      </div>

      {/* A second payment is the one thing a buyer must not do twice by accident. */}
      {reading.duplicate.started && (
        <div className={`kernel-duplicate ${reading.duplicate.settled ? 'is-settled' : 'is-live'}`}>
          <Copy size={16} />
          <div>
            <strong>
              {reading.duplicate.settled
                ? 'A duplicate payment was stopped'
                : 'A second payment was started while one was already running'}
            </strong>
            <p>
              {reading.duplicate.settled
                ? 'This request was recognised as one you had already made. It was counted ' +
                  'once, and you have not been charged twice.'
                : 'The kernel allows one live payment attempt per order. The first is still ' +
                  'going and its outcome is not known yet — do not start a third.'}
            </p>
          </div>
        </div>
      )}

      {/* What actually moved. The kernel found these by re-reading the shop. */}
      {deltas.length > 0 && (
        <div className="kernel-deltas">
          <span>WHAT CHANGED SINCE YOU APPROVED</span>
          <table>
            <thead>
              <tr>
                <th>Field</th>
                <th>You approved</th>
                <th>Now</th>
              </tr>
            </thead>
            <tbody>
              {deltas.map((delta) => {
                const read = readDelta(delta);
                const fmt = (v: string) =>
                  read.money && /^-?\d+$/.test(v) ? money(Number(v)) : v;
                return (
                  <tr key={delta.field_path}>
                    <td>
                      {read.label}
                      {read.sku && <small>{read.sku}</small>}
                    </td>
                    <td>{fmt(read.approved)}</td>
                    <td>
                      <strong>{fmt(read.current)}</strong>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="kernel-next">
        <AlertTriangle size={14} /> {reading.next}
      </p>

      {/* The decision's own identifiers. A refusal anyone can follow is the point. */}
      <dl className="kernel-trace">
        <div>
          <dt>Kernel answer</dt>
          <dd>{decision.code}</dd>
        </div>
        <div>
          <dt>Reason key</dt>
          <dd>{decision.explanation || '—'}</dd>
        </div>
        <div>
          <dt>Decision</dt>
          <dd>{decision.decision_id.slice(0, 8)}</dd>
        </div>
        {decision.checkout && (
          <div>
            <dt>Version refused</dt>
            <dd>
              {decision.checkout.version}
              {decision.next_version ? ` → ${decision.next_version}` : ''}
            </dd>
          </div>
        )}
        {decision.correlation_id && (
          <div>
            <dt>Correlation</dt>
            <dd>{decision.correlation_id.slice(0, 8)}</dd>
          </div>
        )}
      </dl>

      <div className="kernel-actions">
        {reading.retryable && onRetry && (
          <button className="secondary" onClick={onRetry}>
            Try the same order again
          </button>
        )}
        {onBack && (
          <button className={reading.retryable && onRetry ? 'subtle' : 'secondary'} onClick={onBack}>
            Back to payment options
          </button>
        )}
      </div>
    </div>
  );
}

type Span = { key: keyof Order['timing']; label: string; detail: string; provider?: boolean };

/**
 * The four spans, in the order they happen.
 *
 * The labels say what happened, not which subsystem did it: "you deciding" rather than
 * "approval latency". `provider` marks the only leg Razorpay can see, and the note under the
 * bars says why the four need not sum to the total — a span nobody could measure is `null`,
 * and folding it into a neighbour would invent the attribution this exists to establish.
 */
const SPANS: readonly Span[] = [
  {
    key: 'deciding_seconds',
    label: 'You deciding',
    detail: 'From the bill being frozen to your approval being recorded.',
  },
  {
    key: 'admitting_seconds',
    label: 'The kernel admitting',
    detail: 'Re-reading the shop, revalidating the price, minting single-use authority.',
  },
  {
    key: 'queued_seconds',
    label: 'Waiting for a worker',
    detail: 'The command sitting in the outbox before anything acted on it.',
  },
  {
    key: 'paying_seconds',
    label: 'Paying at Razorpay',
    detail: 'The provider call, you at the payment sheet, and the webhook coming back.',
    provider: true,
  },
];

/**
 * Where this sale's seconds went.
 *
 * Rendered from `GET /v1/orders/{id}` and nothing else. Bars are scaled against the largest
 * *measured* span rather than against the total, because the spans are not a decomposition
 * of it and drawing them as one would be a claim the backend explicitly refuses to make.
 */
export function KernelTiming({ order }: { order: Order }) {
  const measured = SPANS.map((s) => order.timing[s.key]).filter(
    (v): v is number => typeof v === 'number',
  );
  const widest = Math.max(1, ...measured);
  const unmeasured = SPANS.length - measured.length;

  return (
    <section className="kernel-timing">
      <header>
        <span className="kernel-mark">
          <Clock3 size={13} /> {KERNEL_NAME}
        </span>
        <span className="kernel-verdict">TRANSACTION TIME</span>
      </header>

      <div className="kernel-timing-total">
        {order.duration_seconds === null ? '—' : seconds(order.duration_seconds)}
        <span>
          {order.reference} · from the bill being frozen to the order being written from
          capture evidence
        </span>
      </div>

      <div className="kernel-spans">
        {SPANS.map((span) => {
          const value = order.timing[span.key];
          const width = typeof value === 'number' ? Math.max(2, (value / widest) * 100) : 0;
          return (
            <div className={value === null ? 'is-unmeasured' : ''} key={span.key}>
              <div className="kernel-span-head">
                <strong>{span.label}</strong>
                <span>{seconds(value)}</span>
              </div>
              <div className="kernel-span-bar">
                <i style={{ width: `${width}%` }} />
              </div>
              <p>
                {span.detail}
                {span.provider && ' The only span Razorpay can see.'}
              </p>
            </div>
          );
        })}
      </div>

      <p className="kernel-timing-note">
        Every span is the difference between two rows the database stamped, subtracted by the
        database. They are not a breakdown of the total and need not add up to it
        {unmeasured > 0
          ? `: ${unmeasured} of the four could not be measured on this sale and is reported as ` +
            'unmeasured rather than folded into its neighbour.'
          : ': a span whose ends cannot both be read is reported as unmeasured rather than ' +
            'folded into its neighbour.'}
      </p>
    </section>
  );
}
