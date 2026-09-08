/**
 * What the shop promises, and the one path by which it changes.
 *
 * The write side of this existed before the screen did: a policy change is a merchant
 * action like every other, drafted, agreed to against an exact digest, and only then
 * carried out. What was missing was the read. A merchant could change terms they had no
 * way to display, which is a shop deciding blind.
 *
 * These are not the terms of any finished sale
 * --------------------------------------------
 * They are the shop's current position, and it governs sales not yet made. Every order
 * already placed carries its own copy, frozen onto it by the Policy-at-Sale Receipt, and
 * nothing changed here can reach one. That is the platform's largest claim, and this is
 * the screen that makes it demonstrable: withdraw returns, and orders sold this morning
 * still offer them while orders sold after do not.
 *
 * Nothing on this page moves when a proposal is drafted, and it should not: the terms
 * above are still what the shop promises. Re-reading them after a draft would remount the
 * form and take the confirmation with it, which is how the first version of this screen
 * managed to create an action and appear to do nothing.
 *
 * Proposing, not publishing
 * -------------------------
 * The button below drafts. It does not publish, and there is deliberately no control here
 * that does: approval happens on Changes to the shop, against the digest of the exact
 * document somebody read. A second approval path on this page would be a second way to
 * agree to terms, and the whole point of the first one is that there is only one.
 */
"use client";

import Link from "next/link";
import { useCallback, useState } from "react";

import { Button, Chip, Field, Loading, Panel, ProblemPanel, cx } from "@/components/ui";
import { api } from "@/lib/api/client";
import { useRead } from "@/lib/useRead";

/** How each family reads to a person, and what changing it actually means. */
const FAMILIES: Readonly<Record<string, { title: string; meaning: string }>> = {
  CANCELLATION: {
    title: "Cancellation",
    meaning: "Whether a buyer may call off an order, and up to what point.",
  },
  REFUND: {
    title: "Refund",
    meaning:
      "Whether money goes back, within how long, and whether part of it may. The kernel reads this window on every refund, from the receipt rather than from here.",
  },
  RETURN: {
    title: "Return",
    meaning:
      "Whether the goods themselves may be sent back, and in what condition. A separate promise from a refund: somebody has to receive and inspect them.",
  },
  SUBSTITUTION: {
    title: "Substitution",
    meaning: "Whether a picker may put a different product in the bag.",
  },
  FULFILMENT: {
    title: "Fulfilment",
    meaning: "How the shop delivers, and what it promises about timing.",
  },
};

function Terms({ terms }: { terms: Record<string, unknown> }) {
  const rows = Object.entries(terms);
  if (rows.length === 0) {
    return (
      <p className="px-4 py-3 text-[13px] text-[var(--faint)]">This family states nothing.</p>
    );
  }
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 px-4 py-3 sm:grid-cols-2">
      {rows.map(([key, value]) => (
        <div key={key} className="flex items-baseline justify-between gap-3">
          <dt className="mono text-[12px] text-[var(--muted)]">{key}</dt>
          <dd className="mono text-[13px] text-[var(--ink)]">{String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export default function PolicyPage() {
  const policy = useRead((signal) => api.merchantPolicy(signal), []);

  if (policy.error) {
    return (
      <ProblemPanel
        error={policy.error}
        what="reading what this shop promises"
        onRetry={policy.reload}
      />
    );
  }
  if (!policy.data) return <Loading label="Reading what the shop promises" />;

  const { version, chosen, published_by, terms, publishable } = policy.data;

  return (
    <main className="mx-auto flex min-h-dvh max-w-5xl flex-col gap-5 px-6 py-10">
      <header>
        <p className="mono text-[11px] uppercase tracking-[0.14em] text-[var(--faint)]">
          Merchant workspace
        </p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight text-[var(--ink)]">
          What the shop promises
        </h1>
        <p className="mt-2 max-w-[70ch] text-[13px] leading-relaxed text-[var(--muted)]">
          The terms this shop offers on sales not yet made. Changing one drafts a proposal;
          somebody approves it on Changes to the shop, and only then does the shop&rsquo;s
          position move.
        </p>
      </header>

      <Panel
        title="What this shop promises"
        subtitle={
          chosen
            ? `Version ${version}, published by ${published_by}.`
            : `Version ${version} — the opening position. Nobody in the shop has chosen these yet; they are what it started with.`
        }
      >
        <p className="max-w-[75ch] px-4 py-3 text-[13px] leading-relaxed text-[var(--muted)]">
          These govern sales not yet made. Every order already placed carries its own copy,
          frozen onto it when the buyer approved, and nothing changed here reaches one —
          which is why withdrawing returns today leaves this morning&rsquo;s orders still
          offering them.
        </p>
      </Panel>

      {publishable.map((family: string) => (
        <Panel
          key={family}
          title={FAMILIES[family]?.title ?? family}
          subtitle={FAMILIES[family]?.meaning}
          actions={
            <Chip tone={terms[family]?.allowed === false ? "muted" : "positive"}>{family}</Chip>
          }
        >
          <Terms terms={(terms[family] ?? {}) as Record<string, unknown>} />
          <Propose family={family} current={terms[family] ?? {}} />
        </Panel>
      ))}
    </main>
  );
}

/**
 * Draft a change to one family, as a merchant action.
 *
 * The textarea holds the whole family rather than one field, because that is what a
 * published version records: a family is replaced entire, and an editor that let somebody
 * change one key would be hiding which of the others they were also agreeing to.
 */
function Propose({ family, current }: { family: string; current: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(() => JSON.stringify(current, null, 2));
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);
  const [said, setSaid] = useState<string | null>(null);

  const propose = useCallback(async () => {
    setBusy(true);
    setProblem(null);
    setSaid(null);
    try {
      const parsed: unknown = JSON.parse(draft);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("A family's terms are an object of keys and values.");
      }
      const action = await api.proposeAction({
        kind: "POLICY_PUBLISH",
        target: family,
        proposal: parsed as Record<string, unknown>,
      });
      setSaid(
        `Drafted as ${action.action_id.slice(0, 8)}. Nothing has changed yet — approve it on Changes to the shop.`,
      );
      setOpen(false);
    } catch (cause) {
      setProblem(cause);
    } finally {
      setBusy(false);
    }
  }, [draft, family]);

  return (
    <div className="border-t border-[var(--line)] px-4 py-3">
      {said ? (
        <p className="mb-2 text-[13px] text-[var(--ink)]">
          {said}{" "}
          <Link href="/actions" className="underline underline-offset-2">
            Go there
          </Link>
          .
        </p>
      ) : null}
      {problem ? <ProblemPanel error={problem} what="drafting this change" /> : null}
      {open ? (
        <div className="flex flex-col gap-2">
          <Field label={`${family} terms, as they would be published`}>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              rows={Math.min(10, Math.max(4, draft.split("\n").length))}
              spellCheck={false}
              className={cx(
                "mono w-full rounded-[8px] border border-[var(--line)] bg-[var(--surface)] p-2 text-[13px] text-[var(--ink)]",
              )}
            />
          </Field>
          <div className="flex gap-2">
            <Button variant="primary" onClick={() => void propose()} disabled={busy}>
              {busy ? "Drafting…" : "Put it up for approval"}
            </Button>
            <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </Button>
          </div>
          <p className="text-[12px] text-[var(--faint)]">
            This drafts a change. It publishes nothing: somebody has to approve the exact
            document on Changes to the shop before the shop&rsquo;s position moves.
          </p>
        </div>
      ) : (
        <Button variant="ghost" onClick={() => setOpen(true)}>
          Propose a change
        </Button>
      )}
    </div>
  );
}
