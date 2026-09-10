/**
 * 1:07 - 1:37. Why the model cannot pay.
 *
 * Every other agentic-commerce demo answers this with a guardrail: a check, a prompt, a
 * confirmation step. All three are things that can be argued with. The answer here is that
 * the capability has no row -- there is nothing to grant by mistake and nothing to jailbreak
 * into. The scene has to *show* an absence, which is why the table is drawn with its rows
 * struck out of existence rather than merely marked "denied".
 */

import React from 'react';
import {Sequence, useCurrentFrame} from 'remotion';
import {Code, Kicker, Panel, Rise, Scene, fadeIn} from '../ui';
import {font, seconds, theme} from '../theme';

/**
 * Registry A, verbatim from `agent_runtime/capabilities/registry.py`. Six of seventeen, and
 * the six were chosen to trace a whole purchase: search, propose, quote, submit for a
 * decision, track, ask for a refund. Every one of them is a read or a proposal.
 *
 * `basket.` is not a stale name -- capability strings, tool names, the protocol wire and
 * idempotency operations keep it deliberately while every user-facing surface says cart.
 * A judge greps these and finds them; that is the point of putting real strings on screen.
 */
const GRANTED = [
  'catalog.search',
  'basket.propose_line',
  'quote.request',
  'checkout.submit_for_approval',
  'order.track',
  'refund.propose',
];

const ABSENT = [
  'checkout.approve',
  'payment.execute',
  'payment.capture',
  'refund.approve',
  'authority.revoke',
];

const NoRow: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <Scene>
      <Kicker delay={4} color={theme.refuse}>
        Not checked. Absent.
      </Kicker>
      <div style={{height: 30}} />
      <Rise delay={12} size={50} weight={680} lh={1.2}>
        A model cannot pay &mdash; not because it is stopped,
        <br />
        because the capability does not exist.
      </Rise>
      <div style={{height: 38}} />
      <div style={{display: 'flex', gap: 30}}>
        <Panel delay={52} style={{flex: 1}}>
          <div style={{fontSize: 20, color: theme.faint, fontFamily: font.mono}}>
            SPECIALIST ACTION TABLE
          </div>
          <div style={{height: 20}} />
          {GRANTED.map((g, i) => (
            <div
              key={g}
              style={{
                opacity: fadeIn(frame, 66 + i * 7, 8),
                fontFamily: font.mono,
                fontSize: 25,
                lineHeight: 2,
                color: theme.text,
              }}
            >
              <span style={{color: theme.prove}}>✓ </span>
              {g}
            </div>
          ))}
        </Panel>
        <Panel delay={120} accent="#3A2028" style={{flex: 1}}>
          <div style={{fontSize: 20, color: theme.faint, fontFamily: font.mono}}>
            NO ROW IN THE SPECIALIST ACTION TABLE
          </div>
          <div style={{height: 20}} />
          {ABSENT.map((a, i) => (
            <div
              key={a}
              style={{
                opacity: fadeIn(frame, 134 + i * 9, 8),
                fontFamily: font.mono,
                fontSize: 25,
                lineHeight: 2,
                color: theme.faint,
                textDecoration: 'line-through',
                textDecorationColor: theme.refuse,
                textDecorationThickness: 2,
              }}
            >
              <span style={{color: theme.refuse, textDecoration: 'none'}}>&mdash; </span>
              {a}
            </div>
          ))}
        </Panel>
      </div>
      <div style={{height: 30}} />
      <Rise delay={200} size={28} color={theme.dim} weight={400}>
        Nothing to grant by mistake. No flag to flip. No prompt to jailbreak into one.
      </Rise>
      <div style={{height: 12}} />
      <Rise delay={222} size={23} color={theme.faint} weight={400} lh={1.5}>
        One of the five does exist &mdash;{' '}
        <span style={{fontFamily: font.mono}}>authority.revoke</span>, on the trusted surface,
        where only a person acts. It has no row anywhere a model can reach.
      </Rise>
    </Scene>
  );
};

const GATE = [
  ['# The tool gate does not compare names. Names can be forged.', theme.faint],
  ['', undefined],
  ['fake = FunctionTool(func=pay, name="checkout_submit_approved")', theme.text],
  ['toolset.add(fake)', theme.text],
  ['', undefined],
  ['#  -> refused: the gate compares the callable itself, with `is`,', theme.dim],
  ['#     against exactly what the factory produced.', theme.dim],
  ['', undefined],
  ['# And an AST test walks every source file in both AI packages', theme.faint],
  ['# and fails if either so much as *imports* the kernel.', theme.faint],
] as const;

const ByIdentity: React.FC = () => (
  <Scene>
    <Kicker delay={4}>Two more locks on the same door</Kicker>
    <div style={{height: 28}} />
    <div style={{display: 'flex', gap: 34, alignItems: 'center'}}>
      <div style={{flex: 1}}>
        <Rise delay={14} size={40} weight={680} lh={1.24}>
          The gate refuses by closure identity, not by name.
        </Rise>
        <div style={{height: 24}} />
        <Rise delay={44} size={26} color={theme.dim} weight={400} lh={1.55}>
          Hand-build a tool. Name it exactly what the factory names a real one. Attach it to
          the toolset. Still refused &mdash; because the comparison is against the function
          object, and you cannot forge object identity with a string.
        </Rise>
        <div style={{height: 28}} />
        <Rise delay={106} size={26} color={theme.prove} weight={600} lh={1.45}>
          This is why prompt injection is not a category of risk here. There is no sentence
          that grants a capability that has no row.
        </Rise>
      </div>
      <Code lines={GATE} delay={20} per={7} size={21} style={{flex: 1.15}} />
    </div>
  </Scene>
);

export const Absence: React.FC = () => (
  <>
    <Sequence durationInFrames={seconds(16)}>
      <NoRow />
    </Sequence>
    <Sequence from={seconds(16)} durationInFrames={seconds(14)}>
      <ByIdentity />
    </Sequence>
  </>
);
