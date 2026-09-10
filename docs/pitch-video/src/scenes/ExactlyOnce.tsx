/**
 * 2:37 - 3:22. Why nobody pays twice, and why nobody is told a lie about it.
 *
 * Three beats, in the order the money actually moves: authority is spent before the provider
 * is called, only one attempt can be live at a time, and an outcome nobody can read is never
 * downgraded into "it failed". The third is the one that costs real money in production and
 * the one most demos have never had to think about.
 */

import React from 'react';
import {Sequence, useCurrentFrame} from 'remotion';
import {Code, Kicker, Panel, Rise, Scene, Tick, fadeIn} from '../ui';
import {font, seconds, theme} from '../theme';

const GRANT = [
  ['# The dangerous order — and the one almost everyone writes.', theme.faint],
  ['razorpay.orders.create(...)     # money side effect exists now', theme.refuse],
  ['grant.consume(); commit()       # ...and the crash lands HERE', theme.refuse],
  ['', undefined],
  ['# The order this kernel uses.', theme.faint],
  ['grant.consume()                 # single-use authority is spent', theme.prove],
  ['session.commit()                # and durable BEFORE the call', theme.prove],
  ['razorpay.orders.create(...)     # only now does the provider hear from us', theme.prove],
] as const;

const CommitBeforeSend: React.FC = () => (
  <Scene>
    <Kicker delay={4}>Commit before send</Kicker>
    <div style={{height: 26}} />
    <Rise delay={12} size={44} weight={680}>
      The grant is spent and durable <em>before</em> Razorpay is called.
    </Rise>
    <div style={{height: 32}} />
    <Code lines={GRANT} delay={30} per={8} size={24} />
    <div style={{height: 32}} />
    <div style={{display: 'flex', gap: 26}}>
      <Panel delay={126} style={{flex: 1}}>
        <div style={{fontSize: 27, color: theme.text, fontWeight: 600}}>Crash mid-flight?</div>
        <div style={{height: 12}} />
        <div style={{fontSize: 23, color: theme.dim, lineHeight: 1.6}}>
          The worker restarts, replays the command, and hits{' '}
          <span style={{fontFamily: font.mono, color: theme.gold}}>GrantAlreadyConsumedError</span>{' '}
          &mdash; which is not a failure but a{' '}
          <span style={{color: theme.prove}}>resume path</span>: go and find out what the
          provider already did.
        </div>
      </Panel>
      <Panel delay={144} style={{flex: 1}}>
        <div style={{fontSize: 27, color: theme.text, fontWeight: 600}}>The trade, named</div>
        <div style={{height: 12}} />
        <div style={{fontSize: 23, color: theme.dim, lineHeight: 1.6}}>
          This order can leave a grant spent with no provider order behind it. That is a{' '}
          <span style={{color: theme.gold}}>recoverable</span> state. The other order can leave
          a charge with no record &mdash; and that one nobody can recover from.
        </div>
      </Panel>
    </div>
  </Scene>
);

const OneWinner: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <Scene>
      <Kicker delay={4}>Exactly one winner, and the database decides</Kicker>
      <div style={{height: 26}} />
      <Rise delay={12} size={44} weight={680}>
        Twelve real threads. Twelve real sessions. One attempt row.
      </Rise>
      <div style={{height: 40}} />
      <div style={{display: 'flex', gap: 10, flexWrap: 'wrap'}}>
        {Array.from({length: 12}, (_, i) => {
          const won = i === 4;
          const o = fadeIn(frame, 46 + i * 4, 8);
          const settled = frame > 120;
          return (
            <div
              key={i}
              style={{
                opacity: o,
                flex: '1 1 120px',
                border: `1px solid ${settled ? (won ? theme.prove : theme.line) : theme.line}`,
                background: settled && won ? '#0F2A22' : theme.panel,
                borderRadius: 12,
                padding: '18px 14px',
                textAlign: 'center',
                fontFamily: font.mono,
              }}
            >
              <div style={{fontSize: 20, color: theme.faint}}>t{i + 1}</div>
              <div style={{height: 8}} />
              <div style={{fontSize: 22, color: settled ? (won ? theme.prove : theme.faint) : theme.dim}}>
                {settled ? (won ? 'won' : 'lost') : '…'}
              </div>
            </div>
          );
        })}
      </div>
      <div style={{height: 36}} />
      <Rise delay={150} size={28} color={theme.dim} weight={400} lh={1.5}>
        Not an application check &mdash; a partial unique index:{' '}
        <span style={{fontFamily: font.mono, color: theme.accent}}>
          uq_payment_attempts_one_non_terminal
        </span>
      </Rise>
      <div style={{height: 26}} />
      <Tick label="one attempt row" delay={186} />
      <Tick label="one Execution Grant" delay={196} />
      <Tick label="eleven losers, each told the truth about why" delay={206} />
      <Tick label="twelve audit events on an intact hash chain" delay={216} />
      <div style={{height: 24}} />
      <Rise delay={240} size={22} color={theme.faint} weight={400}>
        And the loser is identified by SQLSTATE and constraint name &mdash; never by matching
        message text, so a foreign-key failure is never mistaken for a survivable race.
      </Rise>
    </Scene>
  );
};

const Unknown: React.FC = () => (
  <Scene>
    <Kicker delay={4} color={theme.gold}>
      The expensive question in payments
    </Kicker>
    <div style={{height: 26}} />
    <Rise delay={12} size={46} weight={700} lh={1.2}>
      &ldquo;Did it go through?&rdquo;
    </Rise>
    <div style={{height: 30}} />
    <div style={{display: 'flex', gap: 26, alignItems: 'stretch'}}>
      <Panel delay={40} accent="#3A2028" style={{flex: 1}}>
        <div style={{fontSize: 25, color: theme.refuse, fontWeight: 600}}>
          What most systems do
        </div>
        <div style={{height: 14}} />
        <div style={{fontSize: 24, color: theme.dim, lineHeight: 1.65}}>
          Timeout, connection reset, 502 &mdash; call it a failure, show the buyer an error,
          let them try again. If the first call actually landed, they have now paid twice and
          nobody knows it yet.
        </div>
      </Panel>
      <Panel delay={58} accent="#14352C" style={{flex: 1}}>
        <div style={{fontSize: 25, color: theme.prove, fontWeight: 600}}>What this does</div>
        <div style={{height: 14}} />
        <div style={{fontSize: 24, color: theme.dim, lineHeight: 1.65}}>
          Exactly{' '}
          <span style={{color: theme.text, fontWeight: 600}}>five HTTP statuses</span> are on
          the &ldquo;this definitely did not happen&rdquo; list. Everything else becomes{' '}
          <span style={{color: theme.accent}}>reconciliation</span> &mdash; a queue a human
          owns, not a verdict a retry invents.
        </div>
      </Panel>
    </div>
    <div style={{height: 36}} />
    <Rise delay={120} size={32} color={theme.gold} weight={600} lh={1.45}>
      Telling someone a payment failed when it may have succeeded is how they pay twice.
    </Rise>
    <div style={{height: 16}} />
    <Rise delay={150} size={26} color={theme.faint} weight={400}>
      Unknown is a state this system is allowed to be in. It is not allowed to guess its way
      out of one.
    </Rise>
  </Scene>
);

export const ExactlyOnce: React.FC = () => (
  <>
    <Sequence durationInFrames={seconds(15)}>
      <CommitBeforeSend />
    </Sequence>
    <Sequence from={seconds(15)} durationInFrames={seconds(15)}>
      <OneWinner />
    </Sequence>
    <Sequence from={seconds(30)} durationInFrames={seconds(15)}>
      <Unknown />
    </Sequence>
  </>
);
