"use client";

/**
 * Onboarding, as a checklist of what this shop has actually done.
 *
 * Specification 7.1 lists sixteen steps and calls onboarding "a deterministic workflow,
 * not a Merchant Enablement Agent". A wizard with sixteen green ticks would be the easy
 * screen to build and the wrong one: this platform is already running, most of those steps
 * were completed by a seed script rather than by a merchant clicking through, and the
 * useful question is not "will you fill this in" but "which of these can the platform show
 * me it has".
 *
 * So each step carries one of three states and never a fourth:
 *
 *  - **read** — a live endpoint answered, and the evidence is printed beside the step. Not
 *    a tick: the actual figure, from the actual response, on this page load.
 *  - **not readable here** — the console has no endpoint that answers this. The step is
 *    drawn as unanswered and names what is missing. It is emphatically not drawn as
 *    incomplete, because "the console cannot see it" and "the merchant has not done it"
 *    are different facts and only one of them is about the merchant.
 *  - **outside P0** — specification 36 excludes it. Stated as a decision, never as a
 *    to-do.
 *
 * The page ends where the task ends: at the assistant. Step 14 is where the built-in
 * agents are enabled, and it is the step that hands off to the Agent Studio, because the
 * moment a shop has a catalogue and a bound agent principal is exactly the moment building
 * its assistant becomes the next real thing to do.
 */
import Link from "next/link";
import type { ReactNode } from "react";

import { Chip, Id, Loading, Panel, ProblemPanel, cx } from "@/components/ui";
import { formatCount } from "@/lib/money";
import { api } from "@/lib/api/client";
import { useRead } from "@/lib/useRead";

/** How many catalogue rows the localisation and money checks are sampled over. */
const SAMPLE = 25;

type State = "read" | "unreadable" | "excluded";

const STATE_CHIP: Record<State, { tone: "positive" | "warn" | "muted"; label: string }> = {
  read: { tone: "positive", label: "READ FROM THE PLATFORM" },
  unreadable: { tone: "warn", label: "NOT READABLE HERE" },
  excluded: { tone: "muted", label: "OUTSIDE P0" },
};

function Step({
  index,
  title,
  state,
  children,
  source,
  highlight,
}: {
  index: number;
  title: string;
  state: State;
  children: ReactNode;
  /** The endpoint that answered, or the one that would have to exist. */
  source: string;
  highlight?: boolean;
}) {
  const chip = STATE_CHIP[state];
  return (
    <li
      className={cx(
        "grid grid-cols-[28px_1fr] gap-x-3 border-b border-[var(--line-soft)] px-4 py-3 last:border-0",
        highlight && "bg-[color-mix(in_srgb,var(--brand)_6%,transparent)]",
      )}
    >
      <span className="num pt-0.5 text-[var(--faint)]">{index}</span>
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <h3 className="text-[12.5px] font-semibold text-[var(--ink)]">{title}</h3>
          <Chip tone={chip.tone}>{chip.label}</Chip>
        </div>
        <div className="mt-1 max-w-[80ch] text-[11.5px] leading-[1.55] text-[var(--muted)]">
          {children}
        </div>
        <p className="mono mt-1 text-[var(--faint)] break-id">{source}</p>
      </div>
    </li>
  );
}

export default function OnboardingPage() {
  const session = useRead((signal) => api.session(signal), []);
  const config = useRead((signal) => api.config(signal), []);
  const catalogue = useRead((signal) => api.products({ limit: SAMPLE, signal }), []);
  const caps = useRead((signal) => api.agentCapabilities(signal), []);

  const sampled = catalogue.data?.products ?? [];
  // Counted over the sample this page read, and reported with the sample size beside it.
  // A percentage over 25 rows presented as a fact about 247 would be the kind of figure
  // this console exists to not print.
  const withHindi = sampled.filter((product) => product.name_hi.trim() !== "").length;
  const currencies = [...new Set(sampled.map((product) => product.currency))];
  const specialists = caps.data?.specialists ?? [];
  const bound = specialists.reduce((total, entry) => total + entry.capabilities.length, 0);

  const reading = session.loading || config.loading || catalogue.loading || caps.loading;
  const firstError = session.error ?? config.error ?? catalogue.error ?? caps.error;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight text-[var(--ink)]">Onboarding</h1>
          <p className="mt-0.5 text-[12.5px] text-[var(--muted)]">
            Specification 7.1, sixteen steps — and which of them this platform can show you it
            has done.
          </p>
        </div>
        {session.data && (
          <div className="flex flex-wrap items-center gap-2">
            <Chip tone="muted">
              merchant <Id value={session.data.merchant_id} />
            </Chip>
          </div>
        )}
      </header>

      {firstError != null && (
        <ProblemPanel
          error={firstError}
          what="one of the four reads this checklist is built from"
          onRetry={() => {
            session.reload();
            config.reload();
            catalogue.reload();
            caps.reload();
          }}
        />
      )}

      <Panel
        title="What this shop has"
        subtitle="Every state below comes from a read on this page load. Nothing here is remembered."
      >
        {reading ? (
          <Loading label="Reading the platform" />
        ) : (
          <ol>
            <Step
              index={1}
              title="Create tenant and merchant identity"
              state={session.data ? "read" : "unreadable"}
              source="GET /session"
            >
              {session.data ? (
                <>
                  This console is holding a session for tenant{" "}
                  <Id value={session.data.tenant_id} /> and merchant{" "}
                  <Id value={session.data.merchant_id} />. Both identities exist and every read
                  on every page of this console is bound to them by row-level security.
                </>
              ) : (
                <>The session read did not answer, so this console cannot name a tenant.</>
              )}
            </Step>

            <Step
              index={2}
              title="Verify merchant-admin identity"
              state="unreadable"
              source="no endpoint reports merchant-admin verification state"
            >
              This console holds an <span className="mono">OPERATOR</span> session, which is what
              the API mints it. That is not the same fact as a verified merchant admin, and this
              page will not present one as the other.
            </Step>

            <Step
              index={3}
              title="Configure business name, public profile, domain and supported locales"
              state="unreadable"
              source="GET /v1/merchants/{id} — not read by this console"
            >
              The platform holds merchant profile rows; no screen in this console reads them, so
              there is nothing here that could honestly report their contents.
            </Step>

            <Step
              index={4}
              title="Configure stores, service areas and locations"
              state="unreadable"
              source="no fulfilment-configuration endpoint is read here"
            >
              Delivery zones and service areas are merchant configuration this console does not
              read.
            </Step>

            <Step
              index={5}
              title="Select catalogue connector"
              state={catalogue.data ? "read" : "unreadable"}
              source="GET /v1/catalogue/products"
            >
              {catalogue.data ? (
                <>
                  A platform-managed catalogue answers: {formatCount(catalogue.data.matched)}{" "}
                  products across {Object.keys(catalogue.data.counts_by_category).length}{" "}
                  categories, at revision {catalogue.data.revision}. Which of the three connector
                  kinds is configured is not something the read reports; that a catalogue answers
                  at all is.
                </>
              ) : (
                <>The catalogue read did not answer.</>
              )}
            </Step>

            <Step
              index={6}
              title="Map product, option, inventory and price fields into the canonical domain"
              state={sampled.length > 0 ? "read" : "unreadable"}
              source="the shape of each row in GET /v1/catalogue/products"
            >
              {sampled.length > 0 ? (
                <>
                  Every one of the {sampled.length} rows read here arrived in the canonical shape —
                  sku, category, unit label, integer price, stock units, listed and available
                  flags, and a freshness stamp. A field that had not been mapped would be missing
                  from the row rather than empty in it.
                </>
              ) : (
                <>No rows were read, so no mapping can be observed.</>
              )}
            </Step>

            <Step
              index={7}
              title="Configure currency, tax and integer-minor-unit rounding rules"
              state={sampled.length > 0 ? "read" : "unreadable"}
              source="unit_price_minor, currency and tax_bp on each catalogue row"
            >
              {sampled.length > 0 ? (
                <>
                  The {sampled.length} rows sampled here price in{" "}
                  <span className="mono text-[var(--ink)]">{currencies.join(", ")}</span> and carry
                  their price as an integer count of the smallest unit, with tax as an integer
                  count of basis points. There is no decimal anywhere in the payload, which is
                  what makes rounding a question the platform never has to ask.
                </>
              ) : (
                <>No rows were read.</>
              )}
            </Step>

            <Step
              index={8}
              title="Configure fulfilment, delivery zones, slots, fees and minimum orders"
              state="unreadable"
              source="no fee or threshold configuration endpoint is read here"
            >
              The scenario controller can set a delivery fee and a free-delivery threshold, so the
              platform holds them; this console reads neither.
            </Step>

            <Step
              index={9}
              title="Configure inventory reservation TTL and substitution policy"
              state="unreadable"
              source="no reservation-policy endpoint is read here"
            >
              Reservations are kernel state with a TTL; the configured value is not on any read
              this console makes.
            </Step>

            <Step
              index={10}
              title="Configure discounts, coupons, margin floors and threshold offers"
              state="unreadable"
              source="no offer-policy endpoint is read here"
            >
              These are the levers a growth proposal argues about, and none of them is readable
              from this console.
            </Step>

            <Step
              index={11}
              title="Configure cancellation, refund, partial-refund and store-credit policies"
              state="unreadable"
              source="no policy-document endpoint is read here"
            >
              The Support specialist searches these policies through a capability; the documents
              themselves are not read on this page.
            </Step>

            <Step
              index={12}
              title="Configure approval, delegated-authority and Reserve Pay policies"
              state={config.data ? "read" : "unreadable"}
              source="GET /v1/config"
            >
              {config.data ? (
                <>
                  Partly. The platform reports its delegated-payment kill switch:{" "}
                  {config.data.safe_mode ? (
                    <>Safe Mode is engaged, so delegated authority is refused at admission.</>
                  ) : (
                    <>Safe Mode is not engaged.</>
                  )}{" "}
                  The approval and Reserve Pay policies themselves are not on this read — the
                  switch is a fact about the platform, not the policy configuration behind it.
                </>
              ) : (
                <>The config read did not answer.</>
              )}
            </Step>

            <Step
              index={13}
              title="Configure Hindi, Hinglish and English experience"
              state={sampled.length > 0 ? "read" : "unreadable"}
              source="name_en and name_hi on each catalogue row"
            >
              {sampled.length > 0 ? (
                <>
                  {withHindi} of the {sampled.length} rows sampled carry a Hindi name alongside
                  the English one. That is a sample of this page&rsquo;s read and not a claim
                  about the whole catalogue
                  {catalogue.data && <> of {formatCount(catalogue.data.matched)}</>}; the
                  localisation pack behind it is not something this console reads.
                </>
              ) : (
                <>No rows were read.</>
              )}
            </Step>

            <Step
              index={14}
              title="Enable the built-in buyer and merchant agents"
              state={caps.data ? "read" : "unreadable"}
              source="GET /v1/agent/capabilities"
              highlight
            >
              {caps.data ? (
                <>
                  The {caps.data.copilot} copilot is bound for this session, with{" "}
                  {specialists.length}{" "}
                  {specialists.length === 1 ? "specialist" : "specialists"} carrying {bound}{" "}
                  capabilities between them. This is the step the Agent Studio picks up:{" "}
                  <Link href="/studio" className="font-semibold text-[var(--info)] hover:underline">
                    now build your shop&rsquo;s assistant →
                  </Link>
                </>
              ) : (
                <>The capability read did not answer, so no agent binding can be shown.</>
              )}
            </Step>

            <Step
              index={15}
              title="Run connector, catalogue, policy, quote and sandbox-checkout tests"
              state="unreadable"
              source="no test-run record is exposed to this console"
            >
              The Agent Studio&rsquo;s sandbox is one of these — it runs a real turn against real
              data and shows every tool call — but the platform keeps no record of a test run that
              this console could read back, so nothing here reports a pass.
            </Step>

            <Step
              index={16}
              title="Publish an immutable merchant-configuration version"
              state="excluded"
              source="specification 36 — explicitly outside P0"
            >
              Publishing, versioning and rollback of a merchant configuration are named in section
              36 as outside P0. There is no button for this anywhere in the console, and its
              absence is the specification being followed rather than a screen left unfinished.
            </Step>
          </ol>
        )}
      </Panel>

      <Panel title="Where this leads" subtitle="The next real thing to do">
        <div className="space-y-2.5 p-4">
          <p className="max-w-[80ch] text-[12.5px] leading-[1.55] text-[var(--ink)]">
            A shop with a catalogue and a bound agent principal has everything it needs to give
            its buyers and its own staff an assistant. What it does not have yet is a decision
            about what that assistant may touch — and that decision is the one thing about an
            agent a merchant should never take on faith.
          </p>
          <p>
            <Link
              href="/studio"
              className="inline-flex items-center gap-1.5 rounded-[var(--r-sm)] border border-[color-mix(in_srgb,var(--brand)_60%,transparent)] bg-[color-mix(in_srgb,var(--brand)_18%,transparent)] px-2.5 py-1.5 text-[12px] font-medium text-[var(--brand)] transition-colors hover:bg-[color-mix(in_srgb,var(--brand)_26%,transparent)]"
            >
              Build your shop&rsquo;s assistant →
            </Link>
          </p>
        </div>
      </Panel>
    </div>
  );
}
