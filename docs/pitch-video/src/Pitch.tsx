/**
 * The timeline. Exactly 9000 frames = 5:00 at 30fps, which is the Track 1 submission limit.
 *
 * The order is an argument, not a feature tour:
 *
 *   the claim  ->  why it is hard  ->  the shape of the answer  ->  why a model cannot pay
 *   ->  WATCH IT REFUSE  ->  why nobody pays twice  ->  PROVE ALL OF IT  ->  scale  ->  close
 *
 * The two beats in capitals get 60 and 55 seconds -- more than a third of the whole video --
 * because they are the only two a competing submission cannot also claim. Breadth is
 * deliberately late and deliberately short: it answers "is anything behind this?", which is
 * a question, not the pitch.
 */

import React from 'react';
import {AbsoluteFill, Sequence, useCurrentFrame, useVideoConfig} from 'remotion';
import {Opening} from './scenes/Opening';
import {Idea} from './scenes/Idea';
import {Absence} from './scenes/Absence';
import {Refusal} from './scenes/Refusal';
import {ExactlyOnce} from './scenes/ExactlyOnce';
import {Proof} from './scenes/Proof';
import {Breadth} from './scenes/Breadth';
import {Close} from './scenes/Close';
import {font, seconds, theme} from './theme';

type Chapter = {title: string; at: number; len: number; Component: React.FC};

/** One place that owns the running order. `Root.tsx` derives the composition length from it. */
export const CHAPTERS: readonly Chapter[] = [
  {title: 'The claim', at: seconds(0), len: seconds(32), Component: Opening},
  {title: 'The idea', at: seconds(32), len: seconds(35), Component: Idea},
  {title: 'Capability absence', at: seconds(67), len: seconds(30), Component: Absence},
  {title: 'A live refusal', at: seconds(97), len: seconds(60), Component: Refusal},
  {title: 'Exactly once', at: seconds(157), len: seconds(45), Component: ExactlyOnce},
  {title: 'Proof', at: seconds(202), len: seconds(55), Component: Proof},
  {title: 'Breadth', at: seconds(257), len: seconds(25), Component: Breadth},
  {title: 'Close', at: seconds(282), len: seconds(18), Component: Close},
];

export const TOTAL_FRAMES = CHAPTERS.reduce((n, c) => Math.max(n, c.at + c.len), 0);

/**
 * A hairline rail along the bottom edge with the current chapter named.
 *
 * Five minutes is long enough that a viewer starts wondering how much is left, and a viewer
 * wondering that is not listening. The rail costs eight pixels and answers it continuously.
 */
const Rail: React.FC = () => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  const current = CHAPTERS.filter((c) => frame >= c.at).at(-1);
  return (
    <AbsoluteFill style={{justifyContent: 'flex-end', pointerEvents: 'none'}}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          padding: '0 130px 22px',
          fontFamily: font.mono,
          fontSize: 16,
          letterSpacing: '0.14em',
          color: theme.faint,
        }}
      >
        <span>{(current?.title ?? '').toUpperCase()}</span>
        <span>RAZORSHARP · TRANSACTION TRUST KERNEL</span>
      </div>
      <div style={{height: 3, background: theme.line}}>
        <div
          style={{
            width: `${(frame / durationInFrames) * 100}%`,
            height: 3,
            background: theme.accent,
          }}
        />
      </div>
    </AbsoluteFill>
  );
};

export const Pitch: React.FC = () => (
  <AbsoluteFill style={{backgroundColor: theme.bg}}>
    {CHAPTERS.map(({title, at, len, Component}) => (
      <Sequence key={title} from={at} durationInFrames={len} name={title}>
        <Component />
      </Sequence>
    ))}
    <Rail />
  </AbsoluteFill>
);
