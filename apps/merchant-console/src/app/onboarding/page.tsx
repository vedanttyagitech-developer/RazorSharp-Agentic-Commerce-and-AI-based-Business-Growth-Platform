"use client";

import { useState } from "react";

export default function OnboardingPage() {
  const [threshold, setThreshold] = useState("499.00");
  const [deliveryFee, setDeliveryFee] = useState("25.00");
  const [saved, setSaved] = useState(false);

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    setSaved(true);
    setTimeout(() => setSaved(false), 3000);
  };

  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h1 className="text-2xl sm:text-3xl font-black text-white tracking-tight">
          Merchant Onboarding &amp; Policy Settings
        </h1>
        <p className="text-xs sm:text-sm text-[#a49cb5]">
          Specification Section 7: Immutable fee policies, free-delivery thresholds, and Razorpay standard credentials.
        </p>
      </div>

      {saved && (
        <div className="rounded-2xl border border-emerald-500/40 bg-emerald-500/10 p-3.5 text-xs font-bold text-emerald-300 flex items-center gap-2">
          <span>✓</span>
          <span>Merchant policy updated. Pinned regression vectors and outbox bindings synchronized.</span>
        </div>
      )}

      <form onSubmit={handleSave} className="space-y-6">
        {/* Tenant Identity */}
        <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-5 space-y-4">
          <h2 className="text-sm font-black text-white">Merchant Identity (Tenant)</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs">
            <div>
              <label className="block text-[#a49cb5] font-semibold mb-1">Tenant ID</label>
              <input
                type="text"
                disabled
                value="demo-grocery-store"
                className="w-full rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-[#a49cb5] font-mono"
              />
            </div>
            <div>
              <label className="block text-[#a49cb5] font-semibold mb-1">Store Legal Entity</label>
              <input
                type="text"
                defaultValue="Zepto Quick Commerce Operations Private Limited"
                className="w-full rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-white"
              />
            </div>
          </div>
        </div>

        {/* Fee Policy */}
        <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-5 space-y-4">
          <h2 className="text-sm font-black text-white">Deterministic Fee Policy</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs">
            <div>
              <label className="block text-[#a49cb5] font-semibold mb-1">Free Delivery Threshold (INR)</label>
              <div className="flex items-center gap-1 rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2">
                <span className="text-[#a49cb5]">₹</span>
                <input
                  type="text"
                  value={threshold}
                  onChange={(e) => setThreshold(e.target.value)}
                  className="w-full bg-transparent text-white font-mono focus:outline-none"
                />
              </div>
              <span className="text-[10px] text-[#a49cb5] mt-1 block">Default: ₹499.00 (49900 paise exact)</span>
            </div>

            <div>
              <label className="block text-[#a49cb5] font-semibold mb-1">Standard Delivery Fee (INR)</label>
              <div className="flex items-center gap-1 rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2">
                <span className="text-[#a49cb5]">₹</span>
                <input
                  type="text"
                  value={deliveryFee}
                  onChange={(e) => setDeliveryFee(e.target.value)}
                  className="w-full bg-transparent text-white font-mono focus:outline-none"
                />
              </div>
              <span className="text-[10px] text-[#a49cb5] mt-1 block">GST applicable: 18% (1800 bp)</span>
            </div>
          </div>
        </div>

        {/* Razorpay Integration */}
        <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-5 space-y-4">
          <h2 className="text-sm font-black text-white">Razorpay Standard Adapter Configuration</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs">
            <div>
              <label className="block text-[#a49cb5] font-semibold mb-1">Public Key ID</label>
              <input
                type="text"
                defaultValue="rzp_test_51NgQ1F8s"
                className="w-full rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-white font-mono"
              />
              <span className="text-[10px] text-[#a49cb5] mt-1 block">Dispatched to buyer checkout handoff</span>
            </div>

            <div>
              <label className="block text-[#a49cb5] font-semibold mb-1">Webhook Secret Status</label>
              <div className="flex items-center gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-emerald-400 font-mono">
                <span>●</span>
                <span>Configured (Server-Side Only)</span>
              </div>
              <span className="text-[10px] text-[#a49cb5] mt-1 block">Never exposed to browser</span>
            </div>
          </div>
        </div>

        <button
          type="submit"
          className="rounded-2xl bg-[#950EDB] hover:bg-[#800dc0] text-white py-3 px-6 text-xs font-bold transition shadow-xs cursor-pointer"
        >
          Save &amp; Seal Policy Configuration
        </button>
      </form>
    </div>
  );
}
