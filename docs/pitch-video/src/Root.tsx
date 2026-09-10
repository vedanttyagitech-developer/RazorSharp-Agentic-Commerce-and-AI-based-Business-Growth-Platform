/**
 * Remotion's entry point. `npx remotion studio src/Root.tsx` opens this.
 *
 * The duration is derived from the chapter table rather than typed here, so a scene that is
 * re-cut cannot silently leave the video at 4:58 or push it past the 5:00 submission limit --
 * `Pitch.tsx` is the single source of truth for the running order.
 */

import React from 'react';
import {Composition} from 'remotion';
import {Pitch, TOTAL_FRAMES} from './Pitch';
import {FPS} from './theme';

export const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="Pitch"
      component={Pitch}
      durationInFrames={TOTAL_FRAMES}
      fps={FPS}
      width={1920}
      height={1080}
    />
  </>
);
