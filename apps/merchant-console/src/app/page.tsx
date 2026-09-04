"use client";

import Link from "next/link";
import { useState } from "react";

export default function DashboardPage() {
  const [surgeActive, setSurgeActive] = useState(false);

  return (
    <div className="space-y-6">
      {/* Top Banner: Retained Revenue Protection */}
      <div className="rounded-3xl border border-[#950EDB]/40 bg-[#1d1430] p-6 shadow-md">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="space-y-1 max-w-2xl">
            <div className="flex items-center gap-2">
              <span className="flex h-6 items-center rounded-full bg-[#950EDB] px-2.5 text-[10px] font-black uppercase tracking-wider text-white">
                Core Value Proposition
              </span>
              <span className="text-xs font-bold text-[#f6f5f9]">
                Zero Silent Price Leakage
              </span>
            </div>
            <h1 className="text-2xl sm:text-3xl font-black text-white tracking-tight">
              Governed Merchant Revenue Defense
            </h1>
            <p className="text-xs sm:text-sm text-[#a49cb5] leading-relaxed">
              When AI agents shop on quick-commerce surfaces, volatile prices and surge fees risk severe merchant under-billing. The transaction kernel rejects stale approvals with mathematical certainty, safeguarding merchant margins.
            </p>
          </div>

          <div className="flex flex-col gap-2 shrink-0">
            <button
              type="button"
              onClick={() => setSurgeActive(!surgeActive)}
              className={`flex items-center gap-2 rounded-2xl px-4 py-2.5 text-xs font-bold transition shadow-xs cursor-pointer ${
                surgeActive
                  ? "bg-rose-600 text-white"
                  : "border border-[#2d2242] bg-[#201732] text-white hover:bg-[#281c3e]"
              }`}
            >
              <span>⚡</span>
              <span>{surgeActive ? "Surge Pricing: SIMULATED (ACTIVE)" : "Trigger Scenario Price Surge"}</span>
            </button>
            <Link
              href="/evidence"
              className="flex items-center justify-center gap-1.5 rounded-2xl bg-[#950EDB] hover:bg-[#800dc0] text-white px-4 py-2.5 text-xs font-bold transition shadow-xs"
            >
              <span>View Retained Revenue Evidence</span>
              <span aria-hidden="true">➔</span>
            </Link>
          </div>
        </div>
      </div>

      {/* Hero Stats Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Metric 1 */}
        <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-5 shadow-xs space-y-1.5">
          <div className="flex items-center justify-between text-[#a49cb5] text-xs font-bold">
            <span>Retained Revenue Protected</span>
            <span className="text-emerald-400 font-mono">🛡️ HERO</span>
          </div>
          <p className="text-3xl font-black text-emerald-400 font-mono tracking-tight">
            ₹18,450.00
          </p>
          <p className="text-[11px] text-[#a49cb5]">
            Saved from stale under-priced orders
          </p>
        </div>

        {/* Metric 2 */}
        <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-5 shadow-xs space-y-1.5">
          <div className="flex items-center justify-between text-[#a49cb5] text-xs font-bold">
            <span>Stale Checkouts Refused</span>
            <span className="text-rose-400 font-mono">REAPPROVAL</span>
          </div>
          <p className="text-3xl font-black text-white font-mono tracking-tight">
            24 / 24
          </p>
          <p className="text-[11px] text-[#a49cb5]">
            100% prevented from leaking margin
          </p>
        </div>

        {/* Metric 3 */}
        <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-5 shadow-xs space-y-1.5">
          <div className="flex items-center justify-between text-[#a49cb5] text-xs font-bold">
            <span>Gross Merchant Volume</span>
            <span className="text-indigo-400 font-mono">INR</span>
          </div>
          <p className="text-3xl font-black text-white font-mono tracking-tight">
            ₹2,48,920.00
          </p>
          <p className="text-[11px] text-[#a49cb5]">
            142 settled authorized orders
          </p>
        </div>

        {/* Metric 4 */}
        <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-5 shadow-xs space-y-1.5">
          <div className="flex items-center justify-between text-[#a49cb5] text-xs font-bold">
            <span>Unverified Captures</span>
            <span className="text-emerald-400 font-mono">0 VIOLATIONS</span>
          </div>
          <p className="text-3xl font-black text-emerald-400 font-mono tracking-tight">
            0
          </p>
          <p className="text-[11px] text-[#a49cb5]">
            Browser callbacks rejected as capture
          </p>
        </div>
      </div>

      {/* Operational Highlights & Activity Feed */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* Left: Recent Refusal Decisions */}
        <div className="lg:col-span-7 rounded-2xl border border-[#2d2242] bg-[#171124] p-5 space-y-4 shadow-xs">
          <div className="flex items-center justify-between border-b border-[#2d2242] pb-3">
            <div>
              <h2 className="text-base font-black text-white">Recent Kernel Refusal Audit Log</h2>
              <p className="text-xs text-[#a49cb5]">Stale price checkouts halted at admission</p>
            </div>
            <Link href="/evidence" className="text-xs text-[#950EDB] hover:underline font-bold">
              Full Ledger ➔
            </Link>
          </div>

          <div className="space-y-2.5">
            {[
              {
                id: "chk_demo_refusal_01",
                time: "2 mins ago",
                sku: "GRO-DAIRY-001",
                name: "Amul Taaza Toned Milk (500 ml)",
                oldPrice: "₹28.00",
                newPrice: "₹38.00",
                retained: "₹20.00",
                outcome: "REAPPROVAL_REQUIRED",
              },
              {
                id: "chk_demo_refusal_02",
                time: "14 mins ago",
                sku: "OIL-MUS-001",
                name: "Fortune Kachi Ghani Mustard Oil 1 L",
                oldPrice: "₹207.00",
                newPrice: "₹247.00",
                retained: "₹40.00",
                outcome: "REAPPROVAL_REQUIRED",
              },
              {
                id: "chk_demo_refusal_03",
                time: "32 mins ago",
                sku: "GRO-STPL-001",
                name: "India Gate Classic Basmati Rice 5 kg",
                oldPrice: "₹499.00",
                newPrice: "₹539.00",
                retained: "₹40.00",
                outcome: "REAPPROVAL_REQUIRED",
              },
            ].map((item) => (
              <div key={item.id} className="rounded-xl border border-[#2d2242] bg-[#201732] p-3 flex flex-wrap items-center justify-between gap-2 text-xs">
                <div className="space-y-0.5">
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-white">{item.name}</span>
                    <span className="rounded bg-rose-500/20 text-rose-300 px-1.5 py-0.2 font-mono text-[10px]">
                      {item.outcome}
                    </span>
                  </div>
                  <p className="text-[11px] text-[#a49cb5]">
                    Order {item.id} · {item.time}
                  </p>
                </div>
                <div className="text-right">
                  <div className="font-mono tabular-nums">
                    <span className="line-through text-[#a49cb5] mr-1.5">{item.oldPrice}</span>
                    <span className="text-emerald-400 font-bold">➔ {item.newPrice}</span>
                  </div>
                  <span className="text-[11px] font-bold text-[#950EDB]">
                    Protected: +{item.retained}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Right: Architectural Evidence Checklist */}
        <div className="lg:col-span-5 rounded-2xl border border-[#2d2242] bg-[#171124] p-5 space-y-4 shadow-xs">
          <div className="border-b border-[#2d2242] pb-3">
            <h2 className="text-base font-black text-white">Track 1 Guarantee Checks</h2>
            <p className="text-xs text-[#a49cb5]">Verified invariants proven by test suite</p>
          </div>

          <div className="space-y-3 text-xs">
            {[
              {
                title: "Agents Propose; Systems Authorize",
                desc: "No autonomous agent can move money. Every grant requires human cryptographic approval.",
                status: "PASSED",
              },
              {
                title: "Stale Price Invalidation",
                desc: "Kernel checks revision locks; if price shifts underneath checkout, version N is revoked.",
                status: "PASSED",
              },
              {
                title: "Integer Minor Units Everywhere",
                desc: "Paise arithmetic avoids floating point rounding vulnerabilities.",
                status: "PASSED",
              },
              {
                title: "Webhook-Only Capture Evidence",
                desc: "Browser callbacks marked SUBMITTED; order confirmed only upon verified provider webhook.",
                status: "PASSED",
              },
              {
                title: "Single-Winner Concurrency",
                desc: "Contending submissions locked with exclusive row grants; duplicate gets DUPLICATE_OPERATION.",
                status: "PASSED",
              },
            ].map((check, idx) => (
              <div key={idx} className="flex items-start gap-2.5">
                <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-500/20 text-emerald-400 text-xs font-bold">
                  ✓
                </span>
                <div>
                  <strong className="block font-bold text-white leading-tight">
                    {check.title}
                  </strong>
                  <p className="text-[11px] text-[#a49cb5] mt-0.5 leading-snug">
                    {check.desc}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
