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
    </main>
  );
}
