/**
 * One palette and one type scale for the whole pitch.
 *
 * Dark, because every frame after the opening shows either code, a ledger or a refusal, and
 * those read as instruments rather than marketing on a dark ground. The accent is Razorpay's
 * blue; the two signal colours are used for exactly one thing each and never decoratively --
 * `refuse` only ever marks the kernel declining something, `prove` only ever marks a check
 * that passed. A viewer learns that in the first thirty seconds and can then read the rest
 * of the video by colour alone.
 */

import {FONT_MONO, FONT_SANS} from './fonts';

export const theme = {
  bg: '#070B14',
  bgLift: '#0C1424',
  panel: '#111C31',
  line: '#1E2D48',
  text: '#E8EEF9',
  dim: '#8A9BB8',
  faint: '#4A5B78',

  accent: '#3395FF',
  accentDim: '#1B5FB0',

  /** The kernel saying no. Never used for anything else. */
  refuse: '#FF6B5A',
  /** A check that passed, a hash that verified. Never used for anything else. */
  prove: '#3FD9A4',
  /** Money that was at risk and was kept. */
  gold: '#F5C451',
} as const;

export const font = {
  sans: FONT_SANS,
  mono: FONT_MONO,
} as const;

/** 30fps everywhere; every scene length in this project is written in seconds × 30. */
export const FPS = 30;
export const seconds = (n: number) => Math.round(n * FPS);
