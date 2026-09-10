/**
 * 1:37 - 2:37. The centrepiece: a refusal a judge can check, and the clock behind it.
 *
 * The scenario is the one the kernel was built for and the one a total-only comparison lets
 * through -- a merchant raises an item by exactly what they drop the delivery fee by, so the
 * bill the buyer approved and the bill they would now be charged are DIFFERENT DOCUMENTS
 * WITH THE SAME TOTAL. Two integers compare equal, and the money goes. Here it does not.
 *
 * The last beat is the one nothing else in payments shows: where the seconds went. Four
 * spans, and only the last of them is the one a payment provider can see at all.
 */

import React from 'react';
import {Sequence, useCurrentFrame} from 'remotion';
import {Field, Kicker, Panel, Rise, Scene, Tick, fadeIn} from '../ui';
import {font, seconds, theme} from '../theme';

const rupees = (minor: number) =>
  `₹${(minor / 100).toLocaleString('en-IN', {minimumFractionDigits: 2})}`;

/**
 * Real SKUs at their real catalogue prices (`merchant_sim/catalogue.py`), both zero-rated,
 * so the arithmetic on screen is the arithmetic a judge can reproduce. The rise on the 1 L
 * and the drop in delivery are ₹12.00 each and the quantity on the moved line is one --
 * which is the whole point: 23400 before, 23400 after.
 */
const LINES = [
  {sku: 'AMUL-DAIRY-002', name: 'Amul Gold Full Cream Milk 1 L', qty: 1, was: 7300, now: 8500},
  {sku: 'AMUL-DAIRY-001', name: 'Amul Taaza Toned Milk 500 ml', qty: 4, was: 2800, now: 2800},
];

const Setup: React.FC = () => {
  const frame = useCurrentFrame();
  const changed = frame > 150;
  return (
    <Scene>
      <Kicker delay={4}>A live refusal</Kicker>
      <div style={{height: 26}} />
      <Rise delay={12} size={44} weight={680}>
        The buyer approved this exact bill. Then the shop moved.
      </Rise>
      <div style={{height: 32}} />
      <Panel delay={34} style={{padding: 34}}>
        {LINES.map((l, i) => {
          const moved = changed && l.was !== l.now;
          return (
            <div
              key={l.sku}
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'baseline',
                opacity: fadeIn(frame, 46 + i * 12, 9),
                fontSize: 27,
                lineHeight: 2.1,
              }}
            >
              <span style={{color: theme.dim}}>
                <span style={{fontFamily: font.mono, color: theme.faint, fontSize: 21}}>
                  {l.qty}×{' '}
                </span>
                {l.name}
              </span>
              <span style={{fontFamily: font.mono, color: moved ? theme.refuse : theme.text}}>
                {moved ? (
                  <>
                    <span style={{color: theme.faint, textDecoration: 'line-through'}}>
                      {rupees(l.was * l.qty)}
                    </span>{' '}
                    {rupees(l.now * l.qty)}
                  </>
                ) : (
                  rupees(l.now * l.qty)
                )}
              </span>
            </div>
          );
        })}
        <div style={{height: 14, borderBottom: `1px solid ${theme.line}`, marginBottom: 14}} />
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontSize: 27,
            lineHeight: 2.1,
            opacity: fadeIn(frame, 74, 9),
          }}
        >
          <span style={{color: theme.dim}}>Delivery</span>
          <span style={{fontFamily: font.mono, color: changed ? theme.refuse : theme.text}}>
            {changed ? (
              <>
                <span style={{color: theme.faint, textDecoration: 'line-through'}}>
                  {rupees(4900)}
                </span>{' '}
                {rupees(3700)}
              </>
            ) : (
              rupees(4900)
            )}
          </span>
        </div>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontSize: 36,
            fontWeight: 700,
            marginTop: 14,
            opacity: fadeIn(frame, 88, 9),
          }}
        >
          <span>Total</span>
          <span style={{fontFamily: font.mono, color: theme.gold}}>{rupees(23400)}</span>
        </div>
      </Panel>
      <div style={{height: 28}} />
      <Rise delay={168} size={31} color={theme.refuse} weight={620} lh={1.4}>
        Two components moved by ₹12.00 in opposite directions. The total is byte-for-byte the
        one the buyer agreed to.
      </Rise>
      <div style={{height: 10}} />
      <Rise delay={196} size={26} color={theme.faint} weight={400}>
        A system that compares totals compares two equal integers, finds nothing, and pays.
      </Rise>
    </Scene>
  );
};

const COMPARISONS = [
  {
    name: 'total',
    q: 'Does the approval record’s amount still match merchant truth?',
    verdict: '23400 == 23400  →  no delta',
    fail: false,
  },
  {
    name: 'line_items',
    q: 'Can the merchant still supply everything?',
    verdict: 'all_available  →  no delta',
    fail: false,
  },
  {
    name: 'material_deltas',
    q: 'Is the document itself the same document?',
    verdict: '3 deltas',
    fail: true,
  },
];

const Compare: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <Scene>
      <Kicker delay={4}>Three comparisons, not one</Kicker>
      <div style={{height: 30}} />
      {COMPARISONS.map((c, i) => (
        <div key={c.name} style={{opacity: fadeIn(frame, 20 + i * 62, 12), marginBottom: 22}}>
          <div style={{display: 'flex', gap: 22, alignItems: 'center'}}>
            <div
              style={{
                fontFamily: font.mono,
                fontSize: 26,
                color: c.fail ? theme.refuse : theme.prove,
                width: 40,
              }}
            >
              {c.fail ? '✕' : '✓'}
            </div>
            <div style={{flex: 1}}>
              <div style={{fontFamily: font.mono, fontSize: 26, color: theme.text}}>{c.name}</div>
              <div style={{fontSize: 23, color: theme.dim, marginTop: 4}}>{c.q}</div>
            </div>
            <div
              style={{
                fontFamily: font.mono,
                fontSize: 24,
                color: c.fail ? theme.refuse : theme.faint,
              }}
            >
              {c.verdict}
            </div>
          </div>
        </div>
      ))}
      <div style={{height: 16}} />
      <Panel delay={224} accent="#3A2028">
        {[
          ['lines[AMUL-DAIRY-002].unit_minor', '7300', '8500', 'unit_price_changed'],
          ['lines[AMUL-DAIRY-002].line_minor', '7300', '8500', 'line_total_changed'],
          ['delivery_fee_minor', '4900', '3700', 'delivery_fee_changed'],
        ].map(([path, was, now, reason], i) => (
          <div
            key={path}
            style={{
              display: 'flex',
              gap: 18,
              opacity: fadeIn(frame, 238 + i * 14, 9),
              fontFamily: font.mono,
              fontSize: 24,
              lineHeight: 2,
            }}
          >
            <span style={{color: theme.text, flex: 1.5}}>{path}</span>
            <span style={{color: theme.faint}}>{was}</span>
            <span style={{color: theme.faint}}>→</span>
            <span style={{color: theme.refuse}}>{now}</span>
            <span style={{color: theme.dim, flex: 1, textAlign: 'right'}}>{reason}</span>
          </div>
        ))}
      </Panel>
      <div style={{height: 26}} />
      <Rise delay={310} size={26} color={theme.faint} weight={400}>
        The third comparison only ever <span style={{color: theme.text}}>adds</span> rows: an
        approval refused before is still refused, for at least the reasons it was refused for
        then.
      </Rise>
    </Scene>
  );
};

const Card: React.FC = () => {
  return (
    <Scene>
      <Kicker delay={4} color={theme.refuse}>
        HTTP 200 · allowed: false
      </Kicker>
      <div style={{height: 26}} />
      <Rise delay={12} size={46} weight={700} color={theme.refuse}>
        Payment refused. Nothing was charged.
      </Rise>
      <div style={{height: 34}} />
      <div style={{display: 'flex', gap: 24}}>
        <Panel delay={40} style={{flex: 1}}>
          <Field label="why" value="REAPPROVAL_REQUIRED" delay={52} colour={theme.refuse} />
          <div style={{height: 24}} />
          <Field
            label="detail"
            value={
              <span style={{fontFamily: font.mono, fontSize: 24}}>
                merchant_state_changed_since_approval
              </span>
            }
            delay={68}
            colour={theme.dim}
          />
          <div style={{height: 24}} />
          <Field label="whose action was wrong" value="The merchant’s, not yours" delay={84} />
        </Panel>
        <Panel delay={54} style={{flex: 1}}>
          <Field label="did a payment start?" value="No attempt row was written" delay={100} colour={theme.prove} />
          <div style={{height: 24}} />
          <Field label="did a duplicate start?" value="No grant was minted" delay={116} colour={theme.prove} />
          <div style={{height: 24}} />
          <Field label="what happens now" value="Version 3 retired · version 4 awaiting you" delay={132} colour={theme.accent} />
        </Panel>
      </div>
      <div style={{height: 34}} />
      <Rise delay={190} size={30} color={theme.dim} weight={400} lh={1.5}>
        A denial is a <span style={{color: theme.text, fontWeight: 600}}>business outcome</span>,
        not an exception. It names the reason, whose action caused it, whether any money moved,
        and the exact next step &mdash; all on the record, all re-checkable.
      </Rise>
      <div style={{height: 22}} />
      <Rise delay={230} size={22} color={theme.faint} mono weight={400}>
        the same refusal is what the buyer sees on screen — one payload, no second story
      </Rise>
    </Scene>
  );
};

const SPANS = [
  {label: 'You deciding', v: 41.2, note: 'Bill frozen → approval recorded.'},
  {label: 'The kernel admitting', v: 0.38, note: 'Re-read the shop, revalidate, mint authority.'},
  {label: 'Waiting for a worker', v: 1.1, note: 'The command in the outbox, before anything acted.'},
  {label: 'Paying at Razorpay', v: 22.7, note: 'Provider call, payment sheet, webhook back.', provider: true},
];

const Timing: React.FC = () => {
  const frame = useCurrentFrame();
  const max = Math.max(...SPANS.map((s) => s.v));
  return (
    <Scene>
      <Kicker delay={4}>The buyer re-approved version 4. This time it went through.</Kicker>
      <div style={{height: 24}} />
      <Rise delay={12} size={44} weight={680}>
        Four spans. A checkout shows you none of them.
      </Rise>
      <div style={{height: 34}} />
      {SPANS.map((s, i) => {
        const grow = fadeIn(frame, 42 + i * 26, 22);
        return (
          <div key={s.label} style={{marginBottom: 26, opacity: fadeIn(frame, 38 + i * 26, 10)}}>
            <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'baseline'}}>
              <span style={{fontSize: 28, color: theme.text, fontWeight: 600}}>{s.label}</span>
              <span style={{fontFamily: font.mono, fontSize: 26, color: theme.gold}}>
                {s.v.toFixed(2)}s
              </span>
            </div>
            <div style={{height: 10}} />
            <div style={{height: 12, background: theme.bgLift, borderRadius: 6}}>
              <div
                style={{
                  width: `${(s.v / max) * 100 * grow}%`,
                  height: 12,
                  borderRadius: 6,
                  background: s.provider ? theme.accent : theme.prove,
                }}
              />
            </div>
            <div style={{height: 8}} />
            <div style={{fontSize: 21, color: theme.faint}}>
              {s.note}
              {s.provider ? (
                <span style={{color: theme.accent}}> The only span Razorpay can see.</span>
              ) : null}
            </div>
          </div>
        );
      })}
      <div style={{height: 10}} />
      <Tick label="the four need not sum to the total — an unmeasurable span stays null, never folded into a neighbour" delay={200} />
    </Scene>
  );
};

export const Refusal: React.FC = () => (
  <>
    <Sequence durationInFrames={seconds(12)}>
      <Setup />
    </Sequence>
    <Sequence from={seconds(12)} durationInFrames={seconds(14)}>
      <Compare />
    </Sequence>
    <Sequence from={seconds(26)} durationInFrames={seconds(16)}>
      <Card />
    </Sequence>
    <Sequence from={seconds(42)} durationInFrames={seconds(18)}>
      <Timing />
    </Sequence>
  </>
);
