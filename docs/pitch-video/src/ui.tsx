/**
 * The shared vocabulary every scene draws from.
 *
 * Deliberately small. A pitch that invents a new visual idiom every twenty seconds asks the
 * viewer to keep re-learning what they are looking at, and a judge watching twelve of these
 * in a row has no patience for that. Six primitives, reused: a scene frame, a rising line, a
 * typed line, a panel, a labelled row, and a tick.
 */

import React from 'react';
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {font, theme} from './theme';

/** Ease a value in over `frames`, starting at `delay`. Clamped, so it never overshoots. */
export const fadeIn = (frame: number, delay = 0, frames = 18) =>
  interpolate(frame - delay, [0, frames], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

/** A spring that settles, for anything that should feel physical rather than timed. */
export const useSettle = (delay = 0) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  return spring({frame: frame - delay, fps, config: {damping: 200, mass: 0.6}});
};

/** Every scene sits on this: one ground colour, one safe margin, one vertical rhythm. */
export const Scene: React.FC<{children: React.ReactNode; pad?: number}> = ({
  children,
  pad = 130,
}) => (
  <AbsoluteFill
    style={{
      backgroundColor: theme.bg,
      fontFamily: font.sans,
      color: theme.text,
      padding: pad,
      justifyContent: 'center',
    }}
  >
    {children}
  </AbsoluteFill>
);

/** A line that rises and fades in. The workhorse. */
export const Rise: React.FC<{
  children: React.ReactNode;
  delay?: number;
  size?: number;
  weight?: number;
  color?: string;
  mono?: boolean;
  lh?: number;
  style?: React.CSSProperties;
}> = ({children, delay = 0, size = 44, weight = 500, color = theme.text, mono, lh = 1.3, style}) => {
  const frame = useCurrentFrame();
  const o = fadeIn(frame, delay);
  const y = interpolate(o, [0, 1], [22, 0]);
  return (
    <div
      style={{
        opacity: o,
        transform: `translateY(${y}px)`,
        fontSize: size,
        fontWeight: weight,
        color,
        lineHeight: lh,
        fontFamily: mono ? font.mono : font.sans,
        letterSpacing: mono ? 0 : '-0.02em',
        ...style,
      }}
    >
      {children}
    </div>
  );
};

/** The small uppercase label that names what a frame is showing. */
export const Kicker: React.FC<{children: React.ReactNode; delay?: number; color?: string}> = ({
  children,
  delay = 0,
  color = theme.accent,
}) => (
  <Rise delay={delay} size={17} weight={600} color={color} style={{letterSpacing: '0.18em'}}>
    {String(children).toUpperCase()}
  </Rise>
);

/** Text revealed character by character, for anything that should read as being *typed*. */
export const Typed: React.FC<{
  text: string;
  delay?: number;
  cps?: number;
  size?: number;
  color?: string;
  mono?: boolean;
  weight?: number;
}> = ({text, delay = 0, cps = 38, size = 40, color = theme.text, mono, weight = 500}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const shown = Math.max(0, Math.floor(((frame - delay) / fps) * cps));
  const caret = frame > delay && shown < text.length;
  return (
    <div
      style={{
        fontSize: size,
        color,
        fontWeight: weight,
        fontFamily: mono ? font.mono : font.sans,
        lineHeight: 1.35,
        letterSpacing: mono ? 0 : '-0.02em',
      }}
    >
      {text.slice(0, shown)}
      {caret ? <span style={{color: theme.accent}}>▌</span> : null}
    </div>
  );
};

/** A bordered surface. Everything that represents *state* lives inside one of these. */
export const Panel: React.FC<{
  children: React.ReactNode;
  delay?: number;
  accent?: string;
  style?: React.CSSProperties;
}> = ({children, delay = 0, accent = theme.line, style}) => {
  const frame = useCurrentFrame();
  const o = fadeIn(frame, delay);
  return (
    <div
      style={{
        opacity: o,
        transform: `translateY(${interpolate(o, [0, 1], [16, 0])}px)`,
        background: theme.panel,
        border: `1px solid ${accent}`,
        borderRadius: 16,
        padding: 30,
        ...style,
      }}
    >
      {children}
    </div>
  );
};

/** A check that has passed. The only place `prove` green is allowed. */
export const Tick: React.FC<{label: string; delay: number; failed?: boolean}> = ({
  label,
  delay,
  failed,
}) => {
  const frame = useCurrentFrame();
  const o = fadeIn(frame, delay, 8);
  const colour = failed ? theme.refuse : theme.prove;
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        opacity: o,
        fontFamily: font.mono,
        fontSize: 25,
        color: theme.dim,
      }}
    >
      <span style={{color: colour, fontSize: 24, width: 22}}>{failed ? '✕' : '✓'}</span>
      <span>{label}</span>
    </div>
  );
};

/**
 * A monospace block. Lines arrive one at a time so the eye can follow a control flow rather
 * than being handed a wall of code -- a judge cannot read twelve lines in the two seconds a
 * static block usually gets.
 */
export const Code: React.FC<{
  lines: readonly (readonly [string, string?])[];
  delay?: number;
  per?: number;
  size?: number;
  style?: React.CSSProperties;
}> = ({lines, delay = 0, per = 5, size = 23, style}) => {
  const frame = useCurrentFrame();
  return (
    <div
      style={{
        fontFamily: font.mono,
        fontSize: size,
        lineHeight: 1.75,
        background: theme.bgLift,
        border: `1px solid ${theme.line}`,
        borderRadius: 14,
        padding: '26px 30px',
        whiteSpace: 'pre',
        ...style,
      }}
    >
      {lines.map(([txt, colour], i) => (
        <div
          key={`${i}-${txt}`}
          style={{opacity: fadeIn(frame, delay + i * per, 7), color: colour ?? theme.dim}}
        >
          {txt || ' '}
        </div>
      ))}
    </div>
  );
};

/** A label above a value. Used wherever the video shows a field rather than a sentence. */
export const Field: React.FC<{
  label: string;
  value: React.ReactNode;
  delay?: number;
  colour?: string;
  size?: number;
}> = ({label, value, delay = 0, colour = theme.text, size = 32}) => {
  const frame = useCurrentFrame();
  return (
    <div style={{opacity: fadeIn(frame, delay, 9)}}>
      <div
        style={{
          fontFamily: font.mono,
          fontSize: 15,
          letterSpacing: '0.15em',
          color: theme.faint,
        }}
      >
        {label.toUpperCase()}
      </div>
      <div style={{height: 8}} />
      <div style={{fontSize: size, color: colour, fontWeight: 600, letterSpacing: '-0.02em'}}>
        {value}
      </div>
    </div>
  );
};
