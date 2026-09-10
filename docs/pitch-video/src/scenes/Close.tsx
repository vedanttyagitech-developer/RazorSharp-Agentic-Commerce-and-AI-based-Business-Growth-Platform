/**
 * 4:42 - 5:00. Eighteen seconds, one sentence, and where to go and check.
 *
 * The close does not summarise. A judge who has watched four and a half minutes does not need
 * the list again; they need the one sentence they will repeat to the other judges, and the
 * URL they will open afterwards.
 */

import React from 'react';
import {Sequence} from 'remotion';
import {Kicker, Rise, Scene} from '../ui';
import {font, seconds, theme} from '../theme';

const End: React.FC = () => (
  <Scene>
    <Kicker delay={4}>RazorSharp</Kicker>
    <div style={{height: 44}} />
    <Rise delay={16} size={64} weight={700} lh={1.2}>
      Anyone can build an agent that spends money.
    </Rise>
    <div style={{height: 22}} />
    <Rise delay={62} size={64} weight={700} color={theme.accent} lh={1.2}>
      The question is who is allowed to,
      <br />
      and who can prove it afterwards.
    </Rise>
    <div style={{height: 56}} />
    <Rise delay={140} size={30} color={theme.dim} weight={400} lh={1.55}>
      An agent proposes. A person approves. A kernel authorizes. Every rupee leaves through one
      function, and every sale can be re-verified by someone who trusts none of us.
    </Rise>
    <div style={{height: 46}} />
    <Rise delay={200} size={24} color={theme.faint} weight={400} style={{fontFamily: font.mono}}>
      github.com/vedanttyagitech-developer/
      <br />
      RazorSharp-Agentic-Commerce-and-AI-based-Business-Growth-Platform
    </Rise>
    <div style={{height: 18}} />
    <Rise delay={224} size={24} color={theme.accent} weight={400} style={{fontFamily: font.mono}}>
      GET /v1/checkouts/&#123;id&#125;/proof
    </Rise>
  </Scene>
);

export const Close: React.FC = () => (
  <Sequence durationInFrames={seconds(18)}>
    <End />
  </Sequence>
);
