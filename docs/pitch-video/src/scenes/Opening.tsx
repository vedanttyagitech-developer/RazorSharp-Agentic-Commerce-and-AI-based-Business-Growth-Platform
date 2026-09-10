/**
 * 0:00 - 0:50. The hook, then the three failures that make the hook matter.
 *
 * The claim is stated before anything is explained, because a judge decides in the first ten
 * seconds whether this is another chatbot demo. "An AI can shop" is conceded immediately --
 * everyone has that -- so the sentence can spend its weight on the half nobody has.
 */

import React from 'react';
import {Sequence, useCurrentFrame} from 'remotion';
import {Kicker, Panel, Rise, Scene, Typed, fadeIn} from '../ui';
import {font, seconds, theme} from '../theme';

const Hook: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <Scene>
      <Kicker delay={6}>RazorSharp · Razorpay AI Buildathon · Track 1</Kicker>
      <div style={{height: 44}} />
      <Typed text="An AI can shop." delay={26} size={74} weight={700} cps={17} />
      <div style={{height: 18}} />
      <div style={{opacity: fadeIn(frame, 96)}}>
        <Rise delay={96} size={74} weight={700} color={theme.accent} lh={1.18}>
          The hard part is making it
          <br />
          unable to spend your money wrongly.
        </Rise>
      </div>
      <div style={{height: 46}} />
      <Rise delay={190} size={30} color={theme.dim} weight={400}>
        And being able to prove which of the two just happened.
      </Rise>
    </Scene>
  );
};

const FAILURES = [
  {
    head: 'Charged twice',
    body: 'A retry, a timeout, a double tap. The most expensive support ticket a payment company has.',
  },
  {
    head: '"Did it go through?"',
    body: 'A webhook lost, a provider silent. The single largest operational cost in payments.',
  },
  {
    head: '"I never agreed to that."',
    body: 'The price moved after the buyer said yes, and nothing recorded what they actually saw.',
  },
];

const Problem: React.FC = () => (
  <Scene>
    <Kicker delay={4} color={theme.refuse}>
      Three ways agentic payments break
    </Kicker>
    <div style={{height: 40}} />
    <div style={{display: 'flex', gap: 26}}>
      {FAILURES.map((f, i) => (
        <Panel key={f.head} delay={22 + i * 20} accent="#3A2028" style={{flex: 1, minHeight: 300}}>
          <div
            style={{
              fontFamily: font.mono,
              fontSize: 15,
              color: theme.refuse,
              letterSpacing: '0.14em',
            }}
          >
            {String(i + 1).padStart(2, '0')}
          </div>
          <div style={{height: 22}} />
          <div style={{fontSize: 37, fontWeight: 650, letterSpacing: '-0.02em', lineHeight: 1.18}}>
            {f.head}
          </div>
          <div style={{height: 20}} />
          <div style={{fontSize: 24, color: theme.dim, lineHeight: 1.55}}>{f.body}</div>
        </Panel>
      ))}
    </div>
    <div style={{height: 44}} />
    <Rise delay={112} size={29} color={theme.faint} weight={400}>
      Every one of them is a question about <span style={{color: theme.text}}>authority</span>,
      not about intelligence.
    </Rise>
  </Scene>
);

export const Opening: React.FC = () => (
  <>
    <Sequence durationInFrames={seconds(18)}>
      <Hook />
    </Sequence>
    <Sequence from={seconds(18)} durationInFrames={seconds(14)}>
      <Problem />
    </Sequence>
  </>
);
