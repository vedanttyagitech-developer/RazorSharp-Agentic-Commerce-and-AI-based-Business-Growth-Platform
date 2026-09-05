"use client";

/**
 * The console frame: identity on the left, navigation across, live platform facts on the
 * right.
 *
 * The header strip is itself a read. Profile, Razorpay mode, database reachability and
 * the effective operating mode all come from `GET /v1/config` on mount, and when that
 * read fails the strip says the platform is unreachable rather than rendering a
 * comfortable default. "development / test / NORMAL" printed over a failed request is
 * exactly the dishonesty this console was rebuilt to remove.
 *
 * The same rule applies one level down, which is why `degraded` is rendered rather than
 * merely parsed. A read that succeeded can still be reporting a fact it could not
 * establish -- `safe_mode: false` accompanied by a `safe_mode` degradation means the mode
 * was unreadable, not that the tenant is in NORMAL -- and a chip that says NORMAL over
 * that is the same lie told more quietly.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { CopilotDock } from "@/features/copilot/dock";
import { api } from "@/lib/api/client";
import { problemOf } from "@/lib/api/problem";
import { useRead } from "@/lib/useRead";
import { Chip, cx } from "./ui";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/evidence", label: "Evidence" },
  { href: "/operations", label: "Operations" },
  { href: "/catalogue", label: "Catalogue" },
  { href: "/review", label: "Review" },
  { href: "/inspector", label: "Inspector" },
] as const;

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const config = useRead((signal) => api.config(signal), []);
  const session = useRead((signal) => api.session(signal), []);

  // The API sends these when a fact it reports could not actually be established. They
  // were parsed and then dropped on the floor, which turned "we could not read this" into
  // whatever the fallback value happened to look like.
  const degradations = config.data?.degraded ?? [];
  const modeUnknown = degradations.some((degradation) => degradation.component === "safe_mode");

  return (
    <div className="min-h-dvh bg-[var(--bg)] pb-[84px]">
      <header className="sticky top-0 z-20 border-b border-[var(--line)] bg-[color-mix(in_srgb,var(--bg)_92%,transparent)] backdrop-blur">
        <div className="column flex flex-wrap items-center gap-x-6 gap-y-2 py-2.5">
          <Link href="/" className="flex shrink-0 items-center gap-2.5">
            <Mark />
            <span className="leading-tight">
              <span className="block text-[13px] font-semibold tracking-tight text-[var(--ink)]">
                Merchant Console
              </span>
              <span className="mono block text-[var(--faint)]">agentic commerce · operations</span>
            </span>
          </Link>

          <nav aria-label="Console sections" className="flex flex-wrap items-center gap-1">
            {NAV.map((item) => {
              const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cx(
                    "rounded-[var(--r-sm)] px-2.5 py-1.5 text-[12.5px] font-medium transition-colors",
                    active
                      ? "bg-[var(--raised)] text-[var(--ink)] shadow-[inset_0_-2px_0_var(--info)]"
                      : "text-[var(--muted)] hover:bg-[var(--surface)] hover:text-[var(--ink)]",
                  )}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          <div className="ml-auto flex flex-wrap items-center gap-1.5" aria-live="polite">
            {config.loading && <Chip tone="muted">reading /v1/config…</Chip>}
            {config.error != null && <Chip tone="danger">platform unreachable · {problemOf(config.error).status}</Chip>}
            {config.data && (
              <>
                <Chip tone="muted">{config.data.profile}</Chip>
                <Chip tone={config.data.razorpay.test_mode ? "info" : "danger"}>
                  razorpay {config.data.razorpay_mode}
                </Chip>
                <Chip tone={config.data.database.reachable ? "positive" : "danger"}>
                  db {config.data.database.reachable ? "reachable" : "unreachable"}
                </Chip>
                {/*
                  `safe_mode: false` means one of two different things, and the API is
                  careful to say which: it appends a `safe_mode` degradation when the mode
                  could not be read at all. Printing "mode NORMAL" over that would draw an
                  unreadable kill switch as a positive assurance about the tenant, which is
                  the single worst thing this strip could say.
                */}
                <Chip tone={config.data.safe_mode || modeUnknown ? "warn" : "muted"}>
                  {config.data.safe_mode ? "SAFE MODE" : modeUnknown ? "mode UNKNOWN" : "mode NORMAL"}
                </Chip>
              </>
            )}
            {session.data && <Chip tone="muted">{session.data.actor_type.toLowerCase()}</Chip>}
          </div>
        </div>

        {degradations.length > 0 && (
          <div className="column pb-2.5">
            <div
              role="status"
              className="rounded-[var(--r-sm)] border border-[var(--warn)] bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] px-3 py-2"
            >
              <p className="text-[11.5px] font-semibold text-[var(--ink)]">
                The platform is reporting {degradations.length === 1 ? "a degradation" : "degradations"}
              </p>
              <ul className="mt-1 space-y-0.5">
                {degradations.map((degradation) => (
                  <li key={degradation.component} className="text-[11.5px] text-[var(--muted)]">
                    <span className="mono text-[var(--ink)]">{degradation.component}</span> —{" "}
                    {degradation.notice}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </header>

      <main className="column py-5">{children}</main>

      <footer className="column border-t border-[var(--line)] py-4">
        <p className="text-[11.5px] text-[var(--faint)]">
          Every figure on every page in this console is read from the Commerce API during that page
          load. There are no fixtures in this application: a panel that cannot read its data renders
          the problem document instead of a number.
        </p>
        {session.data && (
          <p className="mono mt-1.5 text-[var(--faint)] break-id">
            tenant {session.data.tenant_id} · merchant {session.data.merchant_id} · session{" "}
            {session.data.session_id}
          </p>
        )}
      </footer>

      {/*
        The copilot is docked to the shell rather than given a route of its own, so a
        question asked from the refunds screen does not navigate away from the refunds
        screen. The padding above reserves the strip it occupies, because a composer
        floating over the last row of a table is a composer that hides the row an operator
        was reading.
      */}
      <CopilotDock />
    </div>
  );
}

/** The console's mark: a ledger rule with one refused row. Drawn, not imported. */
function Mark() {
  return (
    <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true" className="shrink-0">
      <rect x="0.75" y="0.75" width="24.5" height="24.5" rx="5" fill="var(--surface)" stroke="var(--line)" strokeWidth="1.5" />
      <rect x="6" y="7" width="14" height="1.6" rx="0.8" fill="var(--muted)" />
      <rect x="6" y="11.2" width="14" height="1.6" rx="0.8" fill="var(--muted)" />
      <rect x="6" y="15.4" width="9" height="1.6" rx="0.8" fill="var(--danger)" />
      <circle cx="18.6" cy="16.2" r="2.4" fill="none" stroke="var(--danger)" strokeWidth="1.5" />
      <path d="M17.1 17.7 20.1 14.7" stroke="var(--danger)" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}
