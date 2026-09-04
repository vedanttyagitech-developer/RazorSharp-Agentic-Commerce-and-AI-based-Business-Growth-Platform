"use client";

import Link from "next/link";
import { useState } from "react";

interface RefusalRecord {
  checkoutId: string;
  timestamp: string;
  buyerSession: string;
  sku: string;
  productName: string;
  approvedUnitPaise: number;
  liveUnitPaise: number;
  approvedDeliveryPaise: number;
  liveDeliveryPaise: number;
  approvedTotalPaise: number;
  liveTotalPaise: number;
  hashV1: string;
  hashV2: string;
  retainedPaise: number;
}

const SAMPLE_RECORDS: RefusalRecord[] = [
  {
    checkoutId: "chk_refusal_8819",
    timestamp: "2026-09-04 23:48:12 UTC",
    buyerSession: "sess_buyer_7721",
    sku: "GRO-DAIRY-001",
    productName: "Amul Taaza Toned Milk (500 ml)",
    approvedUnitPaise: 2800,
    liveUnitPaise: 3800,
    approvedDeliveryPaise: 2500,
    liveDeliveryPaise: 4500,
    approvedTotalPaise: 10825,
    liveTotalPaise: 15185,
    hashV1: "a4f1028e...93bf",
    hashV2: "e3b0c442...b855",
    retainedPaise: 4360,
  },
  {
    checkoutId: "chk_refusal_8814",
    timestamp: "2026-09-04 23:35:04 UTC",
    buyerSession: "sess_buyer_6619",
    sku: "OIL-MUS-001",
    productName: "Fortune Kachi Ghani Mustard Oil 1 L",
    approvedUnitPaise: 20700,
    liveUnitPaise: 24700,
    approvedDeliveryPaise: 0,
    liveDeliveryPaise: 2500,
    approvedTotalPaise: 21735,
    liveTotalPaise: 28885,
    hashV1: "88cf112a...77e1",
    hashV2: "1198acdf...aa32",
    retainedPaise: 7150,
  },
  {
    checkoutId: "chk_refusal_8790",
    timestamp: "2026-09-04 22:50:29 UTC",
    buyerSession: "sess_buyer_3301",
    sku: "GRO-STPL-001",
    productName: "India Gate Classic Basmati Rice 5 kg",
    approvedUnitPaise: 49900,
    liveUnitPaise: 54900,
    approvedDeliveryPaise: 0,
    liveDeliveryPaise: 0,
    approvedTotalPaise: 52395,
    liveTotalPaise: 57645,
    hashV1: "55bb3901...cc12",
    hashV2: "99aa8710...22ef",
    retainedPaise: 5250,
  },
  {
    checkoutId: "chk_refusal_8741",
    timestamp: "2026-09-04 21:12:10 UTC",
    buyerSession: "sess_buyer_1194",
    sku: "GRO-SNCK-001",
    productName: "Maggi 2-Minute Masala Noodles (Pack of 4)",
    approvedUnitPaise: 9600,
    liveUnitPaise: 11200,
    approvedDeliveryPaise: 2500,
    liveDeliveryPaise: 2500,
    approvedTotalPaise: 13252,
    liveTotalPaise: 15044,
    hashV1: "447192a0...bb11",
    hashV2: "3381900b...9900",
    retainedPaise: 1792,
  },
];

export default function EvidencePage() {
  const [selected, setSelected] = useState<RefusalRecord>(SAMPLE_RECORDS[0]);

  const totalProtected = SAMPLE_RECORDS.reduce((acc, r) => acc + r.retainedPaise, 0);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <Link href="/" className="text-xs text-[#a49cb5] hover:underline">Dashboard</Link>
            <span className="text-[#a49cb5]">/</span>
            <span className="text-xs text-[#950EDB] font-bold">Retained Revenue Evidence</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-black text-white tracking-tight mt-1">
            Retained Revenue &amp; Refusal Evidence
          </h1>
          <p className="text-xs sm:text-sm text-[#a49cb5]">
            Cryptographic proof of price-shift protection underneath in-flight AI checkout proposals.
          </p>
        </div>

        <div className="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-2 text-right">
          <span className="block text-[10px] font-bold uppercase tracking-wider text-emerald-400">
            Total Margin Protected
          </span>
          <strong className="text-xl font-black text-emerald-300 font-mono">
            ₹{(totalProtected / 100).toFixed(2)}
          </strong>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* Table List */}
        <div className="lg:col-span-7 rounded-2xl border border-[#2d2242] bg-[#171124] p-5 shadow-xs space-y-3">
          <h2 className="text-sm font-black text-white">Refused Checkouts Ledger</h2>
          <div className="divide-y divide-[#2d2242]">
            {SAMPLE_RECORDS.map((record) => {
              const isSelected = selected.checkoutId === record.checkoutId;
              return (
                <div
                  key={record.checkoutId}
                  onClick={() => setSelected(record)}
                  className={`py-3 px-3 rounded-xl transition cursor-pointer flex items-center justify-between ${
                    isSelected ? "bg-[#201732] border border-[#950EDB]" : "hover:bg-[#201732]/60"
                  }`}
                >
                  <div className="space-y-0.5">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-xs text-white">{record.productName}</span>
                      <span className="text-[10px] font-mono text-[#a49cb5]">{record.checkoutId}</span>
                    </div>
                    <p className="text-[10px] text-[#a49cb5] font-mono">
                      {record.timestamp} · Session: {record.buyerSession}
                    </p>
                  </div>
                  <div className="text-right">
                    <span className="block text-xs font-mono font-bold text-emerald-400">
                      +₹{(record.retainedPaise / 100).toFixed(2)}
                    </span>
                    <span className="text-[10px] text-rose-400 font-bold uppercase">
                      v1 Refused
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Audit Inspector Card */}
        <div className="lg:col-span-5 rounded-2xl border border-[#2d2242] bg-[#171124] p-5 shadow-xs space-y-4">
          <div className="border-b border-[#2d2242] pb-3 flex items-center justify-between">
            <h2 className="text-sm font-black text-white">Kernel Evidence Snapshot</h2>
            <span className="rounded bg-rose-500/20 text-rose-300 px-2 py-0.5 text-[10px] font-mono font-bold">
              OUTCOME: REFUSED
            </span>
          </div>

          <div className="space-y-3 text-xs">
            <div>
              <span className="text-[#a49cb5] block text-[11px]">Checkout Target</span>
              <p className="font-bold text-white text-sm">{selected.productName}</p>
              <span className="font-mono text-[#a49cb5] text-[10px]">SKU: {selected.sku}</span>
            </div>

            <div className="rounded-xl border border-[#2d2242] bg-[#201732] p-3 space-y-2">
              <span className="text-[10px] font-bold uppercase tracking-wider text-[#a49cb5] block">
                Field Diff Analysis
              </span>
              <div className="flex justify-between">
                <span className="text-[#a49cb5]">Approved Unit Price:</span>
                <span className="font-mono text-white line-through">₹{(selected.approvedUnitPaise / 100).toFixed(2)}</span>
              </div>
              <div className="flex justify-between font-bold">
                <span className="text-emerald-400">Live Merchant Unit Price:</span>
                <span className="font-mono text-emerald-400">₹{(selected.liveUnitPaise / 100).toFixed(2)}</span>
              </div>
              <div className="flex justify-between text-[#a49cb5] pt-1 border-t border-[#2d2242]">
                <span>Surge Fee Difference:</span>
                <span className="font-mono text-white">
                  +₹{((selected.liveDeliveryPaise - selected.approvedDeliveryPaise) / 100).toFixed(2)}
                </span>
              </div>
              <div className="flex justify-between font-black text-white pt-1 border-t border-[#2d2242]">
                <span>Retained Margin Protected:</span>
                <span className="font-mono text-[#950EDB] text-sm">
                  +₹{(selected.retainedPaise / 100).toFixed(2)}
                </span>
              </div>
            </div>

            <div className="space-y-1 font-mono text-[10px] text-[#a49cb5]">
              <p>v1 Canonical JCS Hash: <span className="text-white">{selected.hashV1}</span></p>
              <p>v2 Candidate JCS Hash: <span className="text-white">{selected.hashV2}</span></p>
            </div>

            <div className="p-3 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-200 text-[11px] leading-relaxed">
              <strong>Cryptographic Guarantee:</strong> The kernel refused version 1 because content hash {selected.hashV1} did not match live state. The merchant never suffered uncollected surge or price drift.
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
