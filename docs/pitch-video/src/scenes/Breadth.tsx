/**
 * 4:17 - 4:42. Scale, kept short and put late on purpose.
 *
 * Numbers do not win this. They answer the one question a judge is left with after four
 * minutes of architecture -- "is this a slide deck with one endpoint behind it?" -- and the
 * honest answer takes twenty-five seconds, not a minute. Two of these figures matter more
 * than the total, and the scene says which two and why.
 */

import React from 'react';
import {Sequence, useCurrentFrame} from 'remotion';
import {Kicker, Panel, Rise, Scene, fadeIn} from '../ui';
import {font, seconds, theme} from '../theme';

const NUMBERS = [
  {n: '5,944', l: 'tests passing', s: 'ruff clean · mypy --strict across 258 files'},
  {n: '1,753', l: 'hit real PostgreSQL', s: 'RLS, locks and constraints cannot be faked'},
  {n: '640', l: 'are adversarial', s: 'written to attack the thing they cover'},
  {n: '87', l: 'HTTP routes', s: 'across nine surfaces'},
  {n: '14', l: 'Python packages', s: 'one front end under apps/'},
  {n: '26 / 29', l: 'tables RLS FORCED', s: 'the owner cannot bypass it either'},
];

const Numbers: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <Scene>
      <Kicker delay={4}>Is there anything behind it?</Kicker>
      <div style={{height: 34}} />
      <div style={{display: 'flex', flexWrap: 'wrap', gap: 22}}>
        {NUMBERS.map((x, i) => (
          <Panel key={x.l} delay={16 + i * 12} style={{flex: '1 1 30%', minWidth: 300}}>
            <div
              style={{
                fontSize: 58,
                fontWeight: 750,
                letterSpacing: '-0.03em',
                color: i === 1 || i === 2 ? theme.prove : theme.text,
                fontFamily: font.mono,
              }}
            >
              {x.n}
            </div>
            <div style={{height: 6}} />
            <div style={{fontSize: 26, color: theme.text, fontWeight: 600}}>{x.l}</div>
            <div style={{height: 6}} />
            <div style={{fontSize: 20, color: theme.faint, lineHeight: 1.5}}>{x.s}</div>
          </Panel>
        ))}
      </div>
      <div style={{height: 36}} />
      <div style={{opacity: fadeIn(frame, 120, 14)}}>
        <Rise delay={120} size={28} color={theme.dim} weight={400} lh={1.5}>
          The two in <span style={{color: theme.prove}}>green</span> are worth more than the
          total. A guarantee about locking, isolation or uniqueness that is only tested against
          a mock is{' '}
          <span style={{color: theme.text}}>not tested</span>.
        </Rise>
      </div>
    </Scene>
  );
};

const SURFACES = [
  ['Buyer', '247 products · durable cart · exact-bill approval · order lookup by RS- number'],
  ['Voice', 'one socket, live STT + TTS · nine typed degradation frames · barge-in · echo gate'],
  ['Copilot', 'three specialists, closed action sets 8 / 7 / 5 · deterministic routing'],
  ['Reserve Pay', 'bounded, revocable spend authority · revocation outranks every other bound'],
  ['Merchant', 'seven action kinds · twelve states · five publishable policy families'],
  ['Evidence', 'proof chain · timeline · hash-chain verify · retained-revenue evidence'],
  ['Operations', 'Safe Mode · dead-letter revive · review and reconciliation queues'],
  ['Protocols', 'MCP: 13 tools behind OAuth · ACP: five signature-verified routes'],
];

const Surfaces: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <Scene>
      <Kicker delay={4}>Nine surfaces, one kernel</Kicker>
      <div style={{height: 30}} />
      {SURFACES.map(([name, detail], i) => (
        <div
          key={name}
          style={{
            display: 'flex',
            gap: 26,
            alignItems: 'baseline',
            opacity: fadeIn(frame, 14 + i * 12, 9),
            borderBottom: `1px solid ${theme.line}`,
            padding: '15px 0',
          }}
        >
          <div style={{width: 230, fontSize: 29, fontWeight: 650, color: theme.accent}}>
            {name}
          </div>
          <div style={{flex: 1, fontSize: 24, color: theme.dim}}>{detail}</div>
        </div>
      ))}
      <div style={{height: 28}} />
      <Rise delay={128} size={27} color={theme.faint} weight={400}>
        Every one of them proposes. Not one of them can authorize.
      </Rise>
    </Scene>
  );
};

export const Breadth: React.FC = () => (
  <>
    <Sequence durationInFrames={seconds(13)}>
      <Numbers />
    </Sequence>
    <Sequence from={seconds(13)} durationInFrames={seconds(12)}>
      <Surfaces />
    </Sequence>
  </>
);
