"use client";

/**
 * Operations: four views of the same running platform.
 *
 * The tab lives in the URL so an overview tile can link straight at the thing it counted
 * -- `/operations?tab=refunds&state=REFUND_UNKNOWN` opens the list already filtered -- and
 * so an operator can paste a link to what they are looking at into an incident channel.
 */
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Loading, cx } from "@/components/ui";
import { OrdersTab } from "@/components/ops/OrdersTab";
import { OutboxTab } from "@/components/ops/OutboxTab";
import { RefundsTab } from "@/components/ops/RefundsTab";
import { SafeModeTab } from "@/components/ops/SafeModeTab";

const TABS = [
  { id: "orders", label: "Orders" },
  { id: "refunds", label: "Refunds" },
  { id: "outbox", label: "Outbox" },
  { id: "safe-mode", label: "Safe mode" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function OperationsPage() {
  return (
    <Suspense fallback={<Loading label="Opening operations" />}>
      <Operations />
    </Suspense>
  );
}

function Operations() {
  const params = useSearchParams();
  const requested = params.get("tab");
  const tab: TabId = (TABS.find((entry) => entry.id === requested)?.id ?? "orders") as TabId;

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-[18px] font-semibold tracking-tight text-[var(--ink)]">Operations</h1>
        <p className="mt-0.5 text-[12.5px] text-[var(--muted)]">
          Orders, refunds, the durable outbox and the kill switch, on this tenant.
        </p>
      </header>

      <nav
        aria-label="Operations views"
        className="flex flex-wrap gap-1 rounded-[var(--r-lg)] border border-[var(--line)] bg-[var(--surface)] p-1"
      >
        {TABS.map((entry) => {
          const active = entry.id === tab;
          return (
            <Link
              key={entry.id}
              href={`/operations?tab=${entry.id}`}
              aria-current={active ? "page" : undefined}
              className={cx(
                "rounded-[var(--r-sm)] px-3 py-1.5 text-[12.5px] font-medium transition-colors",
                active
                  ? "bg-[var(--raised)] text-[var(--ink)] shadow-[inset_0_-2px_0_var(--info)]"
                  : "text-[var(--muted)] hover:text-[var(--ink)]",
              )}
            >
              {entry.label}
            </Link>
          );
        })}
      </nav>

      {/*
        Each tab is keyed by the filter it was linked with, so arriving from an overview
        tile with a different state remounts the list rather than leaving the previous
        filter's rows on screen under a new heading.
      */}
      {tab === "orders" && <OrdersTab key={params.get("status") ?? "all"} initialStatus={params.get("status")} />}
      {tab === "refunds" && <RefundsTab key={params.get("state") ?? "all"} initialState={params.get("state")} />}
      {tab === "outbox" && <OutboxTab key={params.get("status") ?? "all"} initialStatus={params.get("status")} />}
      {tab === "safe-mode" && <SafeModeTab />}
    </div>
  );
}
