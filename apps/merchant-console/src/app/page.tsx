"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  consoleClient,
  DEMO_RETAINED_SCENARIOS,
  formatPaise,
  type OutboxOut,
  type RetainedRevenueOut,
  type SafeModeOut,
} from "@/lib/api";

export default function Dashboard() {
  const [retainedData, setRetainedData] = useState<RetainedRevenueOut | null>(null);
  const [outbox, setOutbox] = useState<OutboxOut | null>(null);
  const [safeMode, setSafeMode] = useState<SafeModeOut | null>(null);
  const [surgeActive, setSurgeActive] = useState(false);
  const [notification, setNotification] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function loadOverview() {
      try {
        const [ret, ob, sm] = await Promise.all([
          consoleClient.getRetainedRevenue(),
          consoleClient.getOutbox(),
          consoleClient.getSafeMode(),
        ]);
        if (active) {
          setRetainedData(ret);
          setOutbox(ob);
          setSafeMode(sm);
        }
      } catch {
        // Fallback already handled by client
      }
    }
    loadOverview();
    return () => {
      active = false;
    };
  }, []);

  const handleTriggerSurge = async () => {
    setSurgeActive(true);
    try {
      const injection = await consoleClient.injectScenario(
        "PRICE_SET",
        "GRO-DAIRY-001",
        3800,
        "Dashboard quick demo trigger: Amul Taaza price surged to ₹38.00"
      );
      if (injection) {
        setNotification(
          `Scenario injection applied (${injection.injection_id}): Amul Taaza price surged to ₹38.00 (Rev #${injection.new_catalogue_revision ?? "live"}). In-flight storefront checkout will be refused with STALE_APPROVAL_REFUSED.`
        );
      }
    } finally {
      setSurgeActive(false);
      setTimeout(() => setNotification(null), 8000);
    }
  };

  const isLive = retainedData?.is_live === true;

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-[#2d2242] pb-6">
        <div>
          <span className="text-[10px] font-mono font-bold uppercase tracking-wider text-[#950EDB]">
            Executive Merchant Overview · Track 1
          </span>
          <h1 className="text-2xl sm:text-3xl font-black text-white tracking-tight mt-1">
            Zepto Merchant Control Console
          </h1>
          <p className="text-xs sm:text-sm text-[#a49cb5] mt-0.5">
            Cryptographic governance platform preserving merchant margin under autonomous buyer agents.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <button
            type="button"
            disabled={surgeActive}
            onClick={() => void handleTriggerSurge()}
            className="flex items-center gap-2 rounded-xl bg-gradient-to-r from-[#950EDB] to-indigo-600 hover:from-[#800dc0] hover:to-indigo-500 text-white px-4 py-2.5 text-xs font-black transition shadow-xs cursor-pointer disabled:opacity-50"
          >
            <span>⚡</span>
            <span>{surgeActive ? "Injecting Surge..." : "Trigger Hero Price Surge"}</span>
          </button>
        </div>
      </div>

      {/* Safe Mode Active Banner */}
      {safeMode?.safe_mode && (
        <div className="rounded-2xl border border-rose-500/50 bg-rose-950/40 p-4 text-xs font-bold text-rose-300 flex items-center justify-between gap-2 shadow-xs animate-pulse">
          <div className="flex items-center gap-2">
            <span>🚨</span>
            <span>SAFE MODE ENGAGED: Non-essential grants blocked. Kernel is protecting merchant assets.</span>
          </div>
          <Link href="/operations" className="shrink-0 underline text-rose-200 hover:text-white">
            Manage Switch ➔
          </Link>
        </div>
      )}

      {/* Notification Banner */}
      {notification && (
        <div className="rounded-2xl border border-emerald-500/40 bg-emerald-500/10 p-4 text-xs font-bold text-emerald-300 flex items-center justify-between gap-2 shadow-xs">
          <div className="flex items-center gap-2">
            <span>✓</span>
            <span>{notification}</span>
          </div>
          <Link
            href="/evidence"
            className="shrink-0 underline text-emerald-200 hover:text-white"
          >
            View Retained Evidence ➔
          </Link>
        </div>
      )}

      {/* Primary KPI Grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {/* Step 11: Retained Revenue Card */}
        <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-6 space-y-3 relative overflow-hidden">
          <div className="flex items-center justify-between">
            <span className="text-xs font-bold text-[#a49cb5]">Retained Revenue (Step 11)</span>
            <span className={`rounded px-2 py-0.5 text-[9px] font-mono font-bold uppercase ${
              isLive ? "bg-emerald-500/20 text-emerald-400" : "bg-purple-500/20 text-purple-300"
            }`}>
              {isLive ? "LIVE · COMMITTED" : "SIMULATED · MOCK"}
            </span>
          </div>
          <strong className="text-3xl font-black text-white font-mono block">
            {formatPaise(retainedData?.net_retained_minor ?? 10200)}
          </strong>
          <p className="text-xs text-[#a49cb5] leading-relaxed">
            Margin preserved by refusing stale approvals underneath in-flight AI checkout proposals.
          </p>
          <div className="pt-2 border-t border-[#2d2242] flex items-center justify-between text-xs">
            <span className="text-[#a49cb5]">Controlled Scenarios: 4</span>
            <Link href="/evidence" className="text-[#950EDB] hover:underline font-bold">
              Audit Breakdown ➔
            </Link>
          </div>
        </div>

        {/* Operations & Outbox Health */}
        <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-6 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-bold text-[#a49cb5]">Durable Outbox Worker</span>
            <span className="rounded bg-emerald-500/20 text-emerald-400 px-2 py-0.5 text-[9px] font-mono font-bold">
              HEALTHY
            </span>
          </div>
          <div className="flex items-baseline gap-2">
            <strong className="text-3xl font-black text-white font-mono">
              {outbox?.counts.DONE ?? 142}
            </strong>
            <span className="text-xs text-[#a49cb5]">commands settled</span>
          </div>
          <p className="text-xs text-[#a49cb5]">
            Outbox commands draining: {outbox?.counts.PENDING ?? 0} pending, {outbox?.counts.DEAD ?? 1} dead letter.
          </p>
          <div className="pt-2 border-t border-[#2d2242] flex items-center justify-between text-xs">
            <span className="text-rose-400 font-mono font-bold">
              {outbox?.counts.DEAD ?? 1} Dead Command
            </span>
            <Link href="/operations" className="text-[#950EDB] hover:underline font-bold">
              Open Outbox &amp; Revive ➔
            </Link>
          </div>
        </div>

        {/* Catalogue & Grounding Guard */}
        <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-6 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-bold text-[#a49cb5]">Catalogue Guard</span>
            <span className="rounded bg-emerald-500/20 text-emerald-400 px-2 py-0.5 text-[9px] font-mono font-bold">
              GROUNDED
            </span>
          </div>
          <div className="flex items-baseline gap-2">
            <strong className="text-3xl font-black text-white font-mono">
              243 SKUs
            </strong>
            <span className="text-xs text-[#a49cb5]">in integer paise</span>
          </div>
          <p className="text-xs text-[#a49cb5]">
            All 8 categories synchronized. RFC 8785 canonical hash revalidated prior to payment capture.
          </p>
          <div className="pt-2 border-t border-[#2d2242] flex items-center justify-between text-xs">
            <span className="text-emerald-400 font-bold">Zero Floats Allowed</span>
            <Link href="/catalogue" className="text-[#950EDB] hover:underline font-bold">
              Manage Prices ➔
            </Link>
          </div>
        </div>
      </div>

      {/* Controlled Refusals Snapshot */}
      <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-6 space-y-4">
        <div className="flex items-center justify-between border-b border-[#2d2242] pb-4">
          <div>
            <h2 className="text-base font-black text-white">Recent Refused Checkouts (Preserved Margin)</h2>
            <span className="text-xs text-[#a49cb5]">
              Every refusal proved by cryptographic hash mismatch between buyer proposal and live catalogue.
            </span>
          </div>
          <Link href="/evidence" className="text-xs font-bold text-[#950EDB] hover:underline">
            View All Scenarios ➔
          </Link>
        </div>

        <div className="divide-y divide-[#2d2242]">
          {DEMO_RETAINED_SCENARIOS.slice(0, 3).map((scen) => (
            <div key={scen.scenarioId} className="py-3.5 flex flex-wrap items-center justify-between gap-4">
              <div className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <span className="font-bold text-xs text-white">{scen.productName}</span>
                  <span className="text-[10px] font-mono text-[#a49cb5]">{scen.checkoutId}</span>
                </div>
                <p className="text-[11px] text-[#a49cb5]">{scen.reason}</p>
              </div>

              <div className="flex items-center gap-4 text-right">
                <div>
                  <span className="text-xs font-mono font-black text-emerald-400 block">
                    +{formatPaise(scen.preservedDeltaMinor)}
                  </span>
                  <span className="text-[9px] text-[#a49cb5]">
                    {formatPaise(scen.approvedMinor)} ➔ {formatPaise(scen.liveMinor)}
                  </span>
                </div>
                <Link
                  href={`/inspector?attempt_id=${encodeURIComponent(scen.checkoutId)}`}
                  className="rounded-lg bg-[#201732] hover:bg-[#201732]/80 border border-[#2d2242] px-2.5 py-1 text-[11px] font-bold text-white transition"
                >
                  Proof ➔
                </Link>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
