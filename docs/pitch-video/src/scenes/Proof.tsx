/**
 * 3:22 - 4:17. The part that is genuinely rare: the platform proving itself, to a stranger.
 *
 * Everything before this was a claim about how the system behaves. This is the endpoint that
 * makes those claims checkable by someone who does not trust us and did not write any of it
 * -- which is exactly the position a Razorpay judge is in. It is the reason the pitch leads
 * with architecture rather than with a shopping demo.
 */

import React from 'react';
import {Sequence, useCurrentFrame} from 'remotion';
import {Kicker, Panel, Rise, Scene, Typed, fadeIn} from '../ui';
import {font, seconds, theme} from '../theme';

const Ask: React.FC = () => (
  <Scene>
    <Kicker delay={4}>Re-verify any sale, end to end, over HTTP</Kicker>
    <div style={{height: 40}} />
    <Typed
      text="GET /v1/checkouts/{id}/proof"
      delay={20}
      cps={19}
      size={54}
      mono
      color={theme.accent}
      weight={600}
    />
    <div style={{height: 44}} />
    <Rise delay={110} size={40} weight={680} lh={1.3}>
      Not a stored certificate. Not a log we curated.
    </Rise>
    <div style={{height: 14}} />
    <Rise delay={140} size={40} weight={680} color={theme.prove} lh={1.3}>
      The chain is rebuilt and re-verified on every request.
    </Rise>
    <div style={{height: 40}} />
    <Rise delay={190} size={27} color={theme.dim} weight={400} lh={1.55}>
      Ten links, walked in the order the money moved &mdash; and fifteen named checks that
      either pass or say precisely what did not.
    </Rise>
  </Scene>
);

const LINKS = [
  'intent',
  'merchant state',
  'checkout',
  'policy receipt',
  'approval',
  'kernel decision',
  'grant + command',
  'provider requests',
  'verified evidence',
  'final state',
];

const CHECKS = [
  'content_hash_recomputed',
  'receipt_bound_to_version',
  'approval_binds_content',
  'decision_names_version',
  'grant_binds_decision',
  'grant_consumed_once',
  'command_carries_grant',
  'every_mutation_consumed_a_grant',
  'capture_evidence_is_verified',
  'amounts_agree',
  'evidence_in_order',
  'final_state_consistent',
  'tenant_and_merchant_correlate',
  'retained_revenue',
  'audit_chain_checkout',
];

const Chain: React.FC = () => {
  const frame = useCurrentFrame();
  const passed = Math.min(CHECKS.length, Math.max(0, Math.floor((frame - 150) / 20)));
  return (
    <Scene pad={110}>
      <div style={{display: 'flex', gap: 34}}>
        <div style={{flex: 0.85}}>
          <Kicker delay={4}>The ten links</Kicker>
          <div style={{height: 22}} />
          {LINKS.map((l, i) => (
            <div
              key={l}
              style={{
                opacity: fadeIn(frame, 14 + i * 11, 9),
                display: 'flex',
                alignItems: 'center',
                gap: 16,
                marginBottom: 12,
              }}
            >
              <div
                style={{
                  width: 11,
                  height: 11,
                  borderRadius: 6,
                  background: theme.accent,
                  flexShrink: 0,
                }}
              />
              <div style={{fontSize: 26, color: theme.text}}>{l}</div>
              {i < LINKS.length - 1 ? (
                <div style={{flex: 1, height: 1, background: theme.line}} />
              ) : null}
            </div>
          ))}
          <div style={{height: 20}} />
          <Rise delay={150} size={22} color={theme.faint} weight={400} lh={1.5}>
            Each link names the one before it by hash. Edit any row and the link that points
            at it stops resolving.
          </Rise>
        </div>
        <div style={{flex: 1.15}}>
          <Kicker delay={140} color={theme.prove}>
            {`${passed} / 15 checks pass`}
          </Kicker>
          <div style={{height: 22}} />
          {CHECKS.map((c, i) => {
            const on = i < passed;
            return (
              <div
                key={c}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 14,
                  fontFamily: font.mono,
                  fontSize: 22,
                  lineHeight: 1.95,
                  color: on ? theme.text : theme.faint,
                  opacity: fadeIn(frame, 150 + i * 20, 6),
                }}
              >
                <span style={{color: on ? theme.prove : theme.faint, width: 20}}>
                  {on ? '✓' : '·'}
                </span>
                {c}
              </div>
            );
          })}
        </div>
      </div>
    </Scene>
  );
};

const Verdict: React.FC = () => (
  <Scene>
    <Kicker delay={4} color={theme.prove}>
      On a real order in this repository
    </Kicker>
    <div style={{height: 30}} />
    <Rise delay={12} size={50} weight={700}>
      All fifteen pass.
    </Rise>
    <div style={{height: 36}} />
    <div style={{display: 'flex', gap: 24}}>
      <Panel delay={40} accent="#14352C" style={{flex: 1}}>
        <div style={{fontSize: 21, color: theme.faint, fontFamily: font.mono}}>AUDIT STREAMS</div>
        <div style={{height: 16}} />
        <div style={{fontSize: 44, fontWeight: 700, color: theme.prove}}>19 events</div>
        <div style={{height: 10}} />
        <div style={{fontSize: 23, color: theme.dim, lineHeight: 1.55}}>
          Hash-chained and verified in the same answer, with the head hash returned so you can
          check it yourself later.
        </div>
      </Panel>
      <Panel delay={58} style={{flex: 1}}>
        <div style={{fontSize: 21, color: theme.faint, fontFamily: font.mono}}>REAL PROVIDER</div>
        <div style={{height: 16}} />
        <div style={{fontSize: 34, fontWeight: 700, color: theme.accent, fontFamily: font.mono}}>
          order_Ta6DZyvxhDtIS0
        </div>
        <div style={{height: 10}} />
        <div style={{fontSize: 23, color: theme.dim, lineHeight: 1.55}}>
          Razorpay&rsquo;s own hosted Checkout, on test-mode APIs. Not a mock, not a screenshot.
        </div>
      </Panel>
    </div>
    <div style={{height: 40}} />
    <Rise delay={130} size={38} weight={680} color={theme.gold} lh={1.35}>
      Nothing here is taken on trust &mdash; including by us.
    </Rise>
    <div style={{height: 18}} />
    <Rise delay={166} size={26} color={theme.dim} weight={400} lh={1.5}>
      A merchant can settle a dispute without our help. An auditor can check a sale without our
      cooperation. That is a different product from a chatbot that can buy things.
    </Rise>
  </Scene>
);

export const Proof: React.FC = () => (
  <>
    <Sequence durationInFrames={seconds(12)}>
      <Ask />
    </Sequence>
    <Sequence from={seconds(12)} durationInFrames={seconds(25)}>
      <Chain />
    </Sequence>
    <Sequence from={seconds(37)} durationInFrames={seconds(18)}>
      <Verdict />
    </Sequence>
  </>
);
