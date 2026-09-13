'use client';
import { useState } from 'react';
import { Badge } from './concept';
export type MerchantAction = {
  action_id: string;
  kind: string;
  target: string;
  state: string;
  proposal: Record<string, unknown>;
  content_hash: string;
  expected_revision: number;
  outcome_note: string;
  proposed_by: string;
  approved_by: string | null;
  created_at: string;
  updated_at: string;
  applied: { field: string; before: unknown; after: unknown }[];
};
type Change = (
  path: string,
  body: unknown,
  method?: string,
) => Promise<boolean>;
export function MerchantActionCard({
  action: a,
  busy,
  change,
}: {
  action: MerchantAction;
  busy: boolean;
  change: Change;
}) {
  const [mode, setMode] = useState<'edit' | 'reject' | 'cancel' | null>(null),
    [draft, setDraft] = useState(a.proposal),
    [note, setNote] = useState('');
  const cancellable = [
    'DRAFT',
    'AWAITING_APPROVAL',
    'APPROVED',
    'QUEUED',
  ].includes(a.state);
  const editValid = Object.values(draft).every(
    (v) => typeof v !== 'number' || Number.isSafeInteger(v),
  );
  const perform = async () => {
    if (!mode) return;
    const ok = await change(
      `merchant/actions/${a.action_id}${mode === 'edit' ? '' : '/' + mode}`,
      mode === 'edit' ? { proposal: draft } : { note },
      mode === 'edit' ? 'PUT' : 'POST',
    );
    if (ok) setMode(null);
  };
  return (
    <article className="evidence-event action-record">
      <header>
        <div>
          <span className="eyebrow">{a.kind.replaceAll('_', ' ')}</span>
          <h3>{a.target}</h3>
        </div>
        <Badge>{a.state}</Badge>
      </header>
      <p>Reviewed against catalogue revision {a.expected_revision}</p>
      <dl className="record-fields">
        {Object.entries(a.proposal).map(([field, value]) => (
          <div key={field}>
            <dt>{field.replaceAll('_', ' ')}</dt>
            <dd>{JSON.stringify(value)}</dd>
          </div>
        ))}
      </dl>
      <details>
        <summary>Approval and change evidence</summary>
        <dl className="record-fields">
          <div>
            <dt>Proposed by</dt>
            <dd>{a.proposed_by}</dd>
          </div>
          <div>
            <dt>Approved by</dt>
            <dd>{a.approved_by || 'Not approved'}</dd>
          </div>
          <div>
            <dt>Created</dt>
            <dd>{new Date(a.created_at).toLocaleString()}</dd>
          </div>
          <div>
            <dt>Updated</dt>
            <dd>{new Date(a.updated_at).toLocaleString()}</dd>
          </div>
          <div>
            <dt>Approval binding</dt>
            <dd>
              <code>{a.content_hash}</code>
            </dd>
          </div>
        </dl>
        {a.applied?.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Field</th>
                <th>Before</th>
                <th>After</th>
              </tr>
            </thead>
            <tbody>
              {a.applied.map((d, i) => (
                <tr key={i}>
                  <td>{d.field}</td>
                  <td>{JSON.stringify(d.before)}</td>
                  <td>{JSON.stringify(d.after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p>No applied changes recorded.</p>
        )}
      </details>
      <p>{a.outcome_note}</p>
      {mode ? (
        <section className="action-review">
          <h4>
            {mode === 'edit'
              ? 'Edit this draft'
              : mode === 'reject'
                ? 'Decline this proposal'
                : 'Withdraw this proposal'}
          </h4>
          {mode === 'edit' ? (
            <>
              <p>
                Saving creates a new approval binding. No change is executed.
              </p>
              {Object.entries(draft).map(([field, value]) => (
                <label className="form-field" key={field}>
                  {field.replaceAll('_', ' ')}
                  {typeof value === 'boolean' ? (
                    <select
                      value={String(value)}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          [field]: e.target.value === 'true',
                        })
                      }
                    >
                      <option value="true">Yes</option>
                      <option value="false">No</option>
                    </select>
                  ) : Array.isArray(value) ? (
                    <span>{JSON.stringify(value)} · preserved</span>
                  ) : (
                    <input
                      type={typeof value === 'number' ? 'number' : 'text'}
                      value={
                        typeof value === 'string' || typeof value === 'number'
                          ? value
                          : JSON.stringify(value)
                      }
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          [field]:
                            typeof value === 'number'
                              ? Number(e.target.value)
                              : e.target.value,
                        })
                      }
                    />
                  )}
                </label>
              ))}
            </>
          ) : (
            <label className="form-field">
              Reason
              <textarea
                maxLength={1000}
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
            </label>
          )}
          <div className="record-actions">
            <button
              className="primary"
              disabled={busy || (mode === 'edit' ? !editValid : !note.trim())}
              onClick={() => void perform()}
            >
              {mode === 'edit'
                ? 'Save draft'
                : mode === 'reject'
                  ? 'Confirm rejection'
                  : 'Confirm withdrawal'}
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={() => setMode(null)}
            >
              Back
            </button>
          </div>
        </section>
      ) : (
        <div className="record-actions">
          {a.state === 'DRAFT' && (
            <>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => {
                  setDraft(a.proposal);
                  setMode('edit');
                }}
              >
                Edit draft
              </button>
              <button
                className="primary"
                disabled={busy}
                onClick={() =>
                  void change(`merchant/actions/${a.action_id}/submit`, {})
                }
              >
                Request approval
              </button>
            </>
          )}
          {a.state === 'AWAITING_APPROVAL' && (
            <>
              <button
                className="primary"
                disabled={busy}
                onClick={() =>
                  void change(`merchant/actions/${a.action_id}/approve`, {
                    content_hash: a.content_hash,
                  })
                }
              >
                Approve this exact proposal
              </button>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => setMode('reject')}
              >
                Reject proposal
              </button>
            </>
          )}
          {a.state === 'APPROVED' && (
            <button
              className="primary"
              disabled={busy}
              onClick={() =>
                void change(`merchant/actions/${a.action_id}/execute`, {})
              }
            >
              Execute approved change
            </button>
          )}
          {cancellable && (
            <button
              className="secondary"
              disabled={busy}
              onClick={() => setMode('cancel')}
            >
              Withdraw
            </button>
          )}
          {['STALE', 'FAILED', 'REJECTED', 'CANCELLED', 'EXPIRED'].includes(
            a.state,
          ) && (
            <button
              className="secondary"
              disabled={busy}
              onClick={() =>
                void change('merchant/actions', {
                  kind: a.kind,
                  target: a.target,
                  proposal: a.proposal,
                })
              }
            >
              Create new draft for review
            </button>
          )}
          {a.state === 'UNKNOWN' && (
            <p>
              Outcome unconfirmed. Check evidence before proposing another
              change.
            </p>
          )}
        </div>
      )}
    </article>
  );
}
