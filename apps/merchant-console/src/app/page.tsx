/**
 * The console between two designs.
 *
 * The merchant copilot and the eight-route operations instrument that held it were removed
 * deliberately, not broken: the surface is being rebuilt as a workspace, and keeping the
 * old screens alive beside the new ones would have meant two consoles disagreeing about
 * what a merchant is looking at.
 *
 * This page says exactly that and claims nothing else. It reads no endpoint and renders no
 * figure, because the one thing worse than an empty console is one that fills the gap with
 * a plausible number nobody measured. Everything it names as intact -- the API, the kernel,
 * the agent runtime -- is intact: only `apps/merchant-console/src` lost files.
 *
 * One screen of the new workspace exists, and it is linked below rather than described:
 * the helpdesk, where a buyer's support case arrives and a person answers it. It is first
 * because the buyer's order screen already promises that somebody will, and a promise with
 * no screen behind it is the kind of gap this page exists to be honest about.
 */
export default function Page() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-2xl flex-col justify-center gap-6 px-6 py-16">
      <div>
        <p className="mono text-[11px] uppercase tracking-[0.14em] text-[var(--faint)]">
          Merchant workspace
        </p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight text-[var(--ink)]">
          Being rebuilt
        </h1>
      </div>

      <p className="text-[13px] leading-relaxed text-[var(--muted)]">
        The previous console — the merchant copilot, the agent studio and the operations,
        evidence, catalogue, review and inspector screens — has been removed. The workspace
        that replaces it is under construction.
      </p>

      <p className="text-[13px] leading-relaxed text-[var(--muted)]">
        Nothing behind this surface was touched. The commerce API, the transaction kernel,
        the agent runtime and the buyer storefront are unchanged and still running; every
        endpoint those screens read is still served. What was deleted was the user interface
        that read them.
      </p>

      <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--surface)] p-4">
        <p className="mono text-[11px] uppercase tracking-[0.14em] text-[var(--faint)]">
          Built so far
        </p>
        <a
          href="/helpdesk"
          className="mt-1.5 inline-block text-[15px] font-semibold text-[var(--info)] hover:underline"
        >
          Helpdesk &rarr;
        </a>
        <p className="mt-1 text-[12.5px] leading-relaxed text-[var(--muted)]">
          Where a buyer&rsquo;s support case arrives and a person answers it. It is the
          first screen of the new workspace because the storefront already tells buyers
          that somebody decides what they are owed, and until now nothing read the queue
          they were writing to.
        </p>

        <a
          href="/actions"
          className="mt-4 inline-block text-[15px] font-semibold text-[var(--info)] hover:underline"
        >
          Changes to the Store &rarr;
        </a>
        <p className="mt-1 text-[12.5px] leading-relaxed text-[var(--muted)]">
          Propose a price or a stock level, have somebody agree to it, and only then make
          it. Approving names the exact proposal that was read, so a change edited after
          it was put up cannot be approved by somebody who never saw the edit &mdash; the
          same binding the storefront gives a buyer&rsquo;s consent.
        </p>

        <a
          href="/policy"
          className="mt-4 inline-block text-[15px] font-semibold text-[var(--info)] hover:underline"
        >
          What the shop promises &rarr;
        </a>
        <p className="mt-1 text-[12.5px] leading-relaxed text-[var(--muted)]">
          Cancellation, refunds, returns, substitution and fulfilment &mdash; the terms
          this shop currently offers, and the one path by which they change. They govern
          sales not yet made: every order already placed carries its own frozen copy, so
          withdrawing returns today leaves this morning&rsquo;s orders still offering them.
        </p>
      </div>
    </main>
  );
}
