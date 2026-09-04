"use client";

import { useState } from "react";

const PROOF_STEPS = [
  { step: 1, name: "Grounded Discovery", kind: "DISCOVERY_STAMPED", verdict: "OK", detail: "source: merchant-sim:demo-grocery/v1 (rev 12)" },
  { step: 2, name: "Deterministic Quote", kind: "JCS_HASHED", verdict: "OK", detail: "tax: half-up per line, delivery threshold: ₹499.00" },
  { step: 3, name: "Approval Card v1 Signed", kind: "BUYER_APPROVAL_SIGNED", verdict: "OK", detail: "hash: a4f102... signed by human buyer" },
  { step: 4, name: "Merchant Scenario Price Surge", kind: "INJECTION_STATE_MUTATED", verdict: "WARNING", detail: "GRO-DAIRY-001 raised +₹10.00; surge fee +₹20.00" },
  { step: 5, name: "Transaction Kernel Evaluation", kind: "REAPPROVAL_DECISION", verdict: "REFUSED", detail: "stale hash rejected; v1 invalidated; diff computed" },
  { step: 6, name: "Approval Card v2 Signed", kind: "BUYER_APPROVAL_SIGNED", verdict: "OK", detail: "hash: e3b0c4... signed for updated amount" },
  { step: 7, name: "Kernel Outbox Grant Issued", kind: "SINGLE_WINNER_ADMISSION", verdict: "OK", detail: "grant_id: grt_99182; attempt: att_001" },
  { step: 8, name: "Razorpay Standard Order Created", kind: "PROVIDER_ORDER_ISSUED", verdict: "OK", detail: "order_MOCK_8819 issued against grant" },
  { step: 9, name: "Razorpay Webhook Inbox Capture", kind: "WEBHOOK_VERIFIED", verdict: "OK", detail: "signature: hmac_sha256 verified; state: CAPTURED" },
  { step: 10, name: "Order Settled & Locked", kind: "ORDER_FINALIZED", verdict: "OK", detail: "ord_9921 finalized. Proof chain sealed." },
];

export default function InspectorPage() {
  const [activeStep, setActiveStep] = useState(PROOF_STEPS[4]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl sm:text-3xl font-black text-white tracking-tight">
          Protocol Inspector &amp; Proof Chain
        </h1>
        <p className="text-xs sm:text-sm text-[#a49cb5]">
          Full cryptographic verification trail across discovery, quotes, kernel admission, provider requests, and webhooks.
        </p>
      </div>

      {/* Proof Chain Sequence */}
      <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-5 shadow-xs space-y-4">
        <h2 className="text-sm font-black text-white">Ten-Link Cryptographic Proof Chain</h2>
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
          {PROOF_STEPS.map((s) => {
            const isSelected = activeStep.step === s.step;
            const isRefused = s.verdict === "REFUSED";
            return (
              <button
                key={s.step}
                type="button"
                onClick={() => setActiveStep(s)}
                className={`flex flex-col p-3 rounded-xl border text-left transition cursor-pointer ${
                  isSelected
                    ? "border-[#950EDB] bg-[#201732] shadow-xs"
                    : "border-[#2d2242] bg-[#171124] hover:bg-[#201732]/50"
                }`}
              >
                <div className="flex items-center justify-between text-[10px] font-mono mb-1">
                  <span className="font-bold text-[#a49cb5]">Link #{s.step}</span>
                  <span className={`font-bold ${isRefused ? "text-rose-400" : "text-emerald-400"}`}>
                    {s.verdict}
                  </span>
                </div>
                <span className="font-bold text-xs text-white line-clamp-1">{s.name}</span>
                <span className="text-[10px] font-mono text-[#a49cb5] mt-1 line-clamp-1">{s.kind}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Selected Step Deep Dive */}
      <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-5 shadow-xs space-y-4">
        <div className="flex items-center justify-between border-b border-[#2d2242] pb-3">
          <div className="flex items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-[#950EDB] text-white text-xs font-bold">
              #{activeStep.step}
            </span>
            <h3 className="text-base font-black text-white">{activeStep.name}</h3>
          </div>
          <span className="rounded bg-[#201732] border border-[#2d2242] px-2.5 py-1 text-[11px] font-mono text-[#a49cb5]">
            {activeStep.kind}
          </span>
        </div>

        <div className="rounded-xl border border-[#2d2242] bg-[#201732] p-4 space-y-2 font-mono text-xs">
          <div className="flex justify-between text-[#a49cb5]">
            <span>Verification Verdict:</span>
            <span className={activeStep.verdict === "REFUSED" ? "text-rose-400 font-bold" : "text-emerald-400 font-bold"}>
              {activeStep.verdict}
            </span>
          </div>
          <div className="flex justify-between text-[#a49cb5]">
            <span>Evidence Details:</span>
            <span className="text-white">{activeStep.detail}</span>
          </div>
          <div className="flex justify-between text-[#a49cb5]">
            <span>Cryptographic State:</span>
            <span className="text-white">HMAC &amp; JCS SHA-256 Validated</span>
          </div>
        </div>
      </div>
    </div>
  );
}
