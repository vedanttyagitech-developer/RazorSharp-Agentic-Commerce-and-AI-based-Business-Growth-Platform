/**
 * 0:32 - 1:07. The architecture, stated as three parties and one function.
 *
 * This is the load-bearing slide of the whole pitch. Everything after it is evidence for
 * the sentence "an agent proposes, a person approves, a kernel authorizes" -- so the scene
 * spends its first half establishing the three capability sets and its second half showing
 * that the boundary is a real function call inside a real transaction, not a diagram.
 */

import React from 'react';
import {Sequence, useCurrentFrame} from 'remotion';
import {Code, Kicker, Panel, Rise, Scene, fadeIn} from '../ui';
import {font, seconds, theme} from '../theme';

const PARTIES = [
  {
    role: 'The agent',
    verb: 'proposes',
    colour: theme.dim,
    can: ['search the catalogue', 'suggest a cart', 'draft a bill'],
    cannot: ['approve', 'pay', 'write a financial table'],
  },
  {
    role: 'The person',
    verb: 'approves',
    colour: theme.accent,
    can: ['read the exact bill', 'consent to a content hash'],
    cannot: ['be simulated by the model'],
  },
  {
    role: 'The kernel',
    verb: 'authorizes',
    colour: theme.prove,
    can: ['re-check every claim', 'mint one grant', 'move money'],
    cannot: ['be reached by a model'],
  },
];

const Three: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <Scene>
      <Kicker delay={4}>The idea, in one line</Kicker>
      <div style={{height: 34}} />
      <Rise delay={14} size={52} weight={680}>
        An agent proposes. A person approves. A kernel authorizes.
      </Rise>
      <div style={{height: 40}} />
      <div style={{display: 'flex', gap: 24}}>
        {PARTIES.map((p, i) => (
          <Panel key={p.role} delay={44 + i * 22} style={{flex: 1, minHeight: 330}}>
            <div style={{fontSize: 20, color: theme.faint}}>{p.role}</div>
            <div style={{height: 4}} />
            <div style={{fontSize: 42, fontWeight: 700, color: p.colour, letterSpacing: '-0.02em'}}>
              {p.verb}
            </div>
            <div style={{height: 24}} />
            {p.can.map((c, j) => (
              <div
                key={c}
                style={{
                  opacity: fadeIn(frame, 76 + i * 22 + j * 6, 8),
                  fontSize: 21,
                  color: theme.dim,
                  fontFamily: font.mono,
                  lineHeight: 1.9,
                }}
              >
                <span style={{color: theme.prove}}>+ </span>
                {c}
              </div>
            ))}
            {p.cannot.map((c, j) => (
              <div
                key={c}
                style={{
                  opacity: fadeIn(frame, 108 + i * 22 + j * 6, 8),
                  fontSize: 21,
                  color: theme.faint,
                  fontFamily: font.mono,
                  lineHeight: 1.9,
                  textDecoration: 'line-through',
                  textDecorationColor: theme.refuse,
                }}
              >
                <span style={{color: theme.refuse, textDecoration: 'none'}}>&minus; </span>
                {c}
              </div>
            ))}
          </Panel>
        ))}
      </div>
      <div style={{height: 36}} />
      <Rise delay={188} size={27} color={theme.faint} weight={400}>
        Three components. Three capability sets. The boundaries are enforced in code, not by
        convention.
      </Rise>
    </Scene>
  );
};

const ADMIT = [
  ['def admit(session, request, merchant_state) -> AdmissionDecision:', theme.text],
  ['    """Decide whether one money action may proceed."""', theme.faint],
  ['', undefined],
  ['    if not session.in_transaction():          # its guarantees come from locks', theme.dim],
  ['        raise AdmissionError(...)             # that only exist inside one', theme.dim],
  ['', undefined],
  ['    #  4  can this actor submit this operation at all?', theme.dim],
  ['    #  5  Safe Mode, before anything delegated is accepted', theme.dim],
  ['    #  6  lock the checkout, confirm the version is current', theme.dim],
  ['    # 6a  the buyer’s own approval, re-read from the database', theme.dim],
  ['    #  7  reservation, against the database clock', theme.dim],
  ['    # 8-10 re-read merchant truth, compare with what was approved', theme.dim],
  ['    # 10+  authority, locked in the same transaction', theme.dim],
  ['    # 12   exactly one winner', theme.dim],
] as const;

const OneDoor: React.FC = () => (
  <Scene>
    <Kicker delay={4}>One function. One transaction.</Kicker>
    <div style={{height: 26}} />
    <div style={{display: 'flex', gap: 34, alignItems: 'center'}}>
      <Code lines={ADMIT} delay={16} per={5} size={22} style={{flex: 1.35}} />
      <div style={{flex: 1}}>
        <Rise delay={44} size={40} weight={680} lh={1.22}>
          Every rupee in this system leaves through this one call.
        </Rise>
        <div style={{height: 26}} />
        <Rise delay={72} size={26} color={theme.dim} weight={400} lh={1.55}>
          Nothing the caller says is trusted. The version, the approval, the merchant&rsquo;s
          prices and the buyer&rsquo;s authority are all re-read from the database, under lock,
          inside the transaction that will write the payment attempt.
        </Rise>
        <div style={{height: 30}} />
        <Rise delay={116} size={26} color={theme.gold} weight={600} lh={1.45}>
          And a refusal is an answer, not a crash: HTTP 200,{' '}
          <span style={{fontFamily: font.mono}}>allowed: false</span>, with the reason and the
          way forward.
        </Rise>
      </div>
    </div>
    <div style={{height: 34}} />
    <Rise delay={160} size={22} color={theme.faint} mono weight={400}>
      packages/transaction-kernel/src/transaction_kernel/admission.py
    </Rise>
  </Scene>
);

export const Idea: React.FC = () => (
  <>
    <Sequence durationInFrames={seconds(16)}>
      <Three />
    </Sequence>
    <Sequence from={seconds(16)} durationInFrames={seconds(19)}>
      <OneDoor />
    </Sequence>
  </>
);
