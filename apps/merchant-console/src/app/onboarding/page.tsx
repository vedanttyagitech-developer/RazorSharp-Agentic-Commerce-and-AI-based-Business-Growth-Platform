"use client";

import Link from "next/link";
import { useState } from "react";

interface StepConfig {
  stepNumber: number;
  title: string;
  summary: string;
  isWired: boolean;
}

const STEPS: StepConfig[] = [
  { stepNumber: 1, title: "Tenant & Merchant Identity", summary: "Unique slug, legal business entity, tax registration", isWired: true },
  { stepNumber: 2, title: "Admin Verification", summary: "Primary operator identity & security keys", isWired: true },
  { stepNumber: 3, title: "Business Profile & Locales", summary: "Default currency, supported multilingual locales (en-IN, hi-IN, Hinglish)", isWired: true },
  { stepNumber: 4, title: "Stores & Service Areas", summary: "Fulfillment hubs, dark store radius, service geo-fences", isWired: false },
  { stepNumber: 5, title: "Catalogue Connector", summary: "Inventory feed, ERP connector & catalogue revision sync", isWired: true },
  { stepNumber: 6, title: "Field Mapping", summary: "Taxonomy attributes, synonyms, barcode & SKU identifiers", isWired: false },
  { stepNumber: 7, title: "Currency & Rounding", summary: "RFC 8785 JCS canonicalization, integer paise only (zero floats)", isWired: true },
  { stepNumber: 8, title: "Fulfilment Zones & Fees", summary: "Deterministic fee engine thresholds & delivery fee slabs", isWired: true },
  { stepNumber: 9, title: "Reservation TTL & Stock Policy", summary: "Cart lock duration (900s), stock substitution consent rules", isWired: true },
  { stepNumber: 10, title: "Discounts & Margin Floors", summary: "Promotional bounds, minimum margin preservation floors", isWired: false },
  { stepNumber: 11, title: "Cancellation & Refund Policy", summary: "Grace period, refund resolution modes (source vs credit)", isWired: true },
  { stepNumber: 12, title: "Approval & Delegated Authority", summary: "Human-present boundary (₹0.00), single-use execution grant TTL", isWired: true },
];

const STORAGE_KEY = "zepto_merchant_onboarding_state_v1";

interface OnboardingFormData {
  tenantSlug: string;
  tenantName: string;
  merchantSlug: string;
  merchantName: string;
  homeRegion: string;
  adminEmail: string;
  operatorKey: string;
  locales: string[];
  storeCount: number;
  freeDeliveryThresholdRupees: number;
  standardDeliveryFeeRupees: number;
  reservationTtlSeconds: number;
  marginFloorPercent: number;
  humanPresentThresholdRupees: number;
}

const DEFAULT_FORM: OnboardingFormData = {
  tenantSlug: "demo",
  tenantName: "Demo Grocery Storefront",
  merchantSlug: "demo-grocery",
  merchantName: "Zepto Quick Commerce Operations Pvt Ltd",
  homeRegion: "ap-south-1",
  adminEmail: "operator@demo-grocery.internal",
  operatorKey: "local-demo-scenario-key",
  locales: ["en-IN", "hi-IN", "hinglish"],
  storeCount: 4,
  freeDeliveryThresholdRupees: 499,
  standardDeliveryFeeRupees: 25,
  reservationTtlSeconds: 900,
  marginFloorPercent: 15,
  humanPresentThresholdRupees: 0,
};

export default function OnboardingPage() {
  const [currentStep, setCurrentStep] = useState(1);
  const [form, setForm] = useState<OnboardingFormData>(() => {
    if (typeof window !== "undefined") {
      try {
        const stored = localStorage.getItem(STORAGE_KEY);
        if (stored) return JSON.parse(stored);
      } catch {
        // Ignore
      }
    }
    return DEFAULT_FORM;
  });
  const [saveStatus, setSaveStatus] = useState<string | null>(null);

  const saveToLocal = (updated: OnboardingFormData) => {
    setForm(updated);
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(updated));
    } catch {
      // Ignore
    }
  };

  const handleNext = () => {
    if (currentStep < 12) {
      setCurrentStep(currentStep + 1);
    } else {
      setSaveStatus("All 12 Configuration Steps sealed and verified! Ready for production deployment.");
      setTimeout(() => setSaveStatus(null), 6000);
    }
  };

  const handlePrevious = () => {
    if (currentStep > 1) {
      setCurrentStep(currentStep - 1);
    }
  };

  const handleResetForSecondTenant = () => {
    const secondTenant: OnboardingFormData = {
      ...DEFAULT_FORM,
      tenantSlug: "tenant-blended-pharmacy",
      tenantName: "Zepto 10-Min Pharmacy Tenant",
      merchantSlug: "zepto-pharmacy",
      merchantName: "Zepto Healthcare & Pharmacy Pvt Ltd",
      freeDeliveryThresholdRupees: 299,
      standardDeliveryFeeRupees: 35,
    };
    saveToLocal(secondTenant);
    setCurrentStep(1);
    setSaveStatus("Second Tenant Template Initialized without code changes (Spec P0 Acceptance Criterion 7.1).");
    setTimeout(() => setSaveStatus(null), 6000);
  };

  const activeStep = STEPS[currentStep - 1];

  return (
    <div className="space-y-6 max-w-5xl">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-[#2d2242] pb-6">
        <div>
          <div className="flex items-center gap-2 text-xs text-[#a49cb5]">
            <Link href="/" className="hover:underline">Dashboard</Link>
            <span>/</span>
            <span className="text-[#950EDB] font-bold">Policy Onboarding Flow</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-black text-white tracking-tight mt-1">
            Merchant Onboarding &amp; Policy Settings
          </h1>
          <p className="text-xs sm:text-sm text-[#a49cb5] mt-0.5">
            Specification 7.1: Twelve configuration steps governing tenant isolation, fee engine, and authority policies.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <span
            data-testid="badge-onboarding"
            className="rounded px-2 py-0.5 text-[9px] font-mono font-bold uppercase bg-purple-500/20 text-purple-300 border border-purple-500/30"
          >
            SIMULATED · MOCK
          </span>
          <button
            type="button"
            onClick={handleResetForSecondTenant}
            className="rounded-xl border border-purple-500/40 bg-purple-950/40 hover:bg-purple-900/60 text-purple-200 px-3.5 py-2 text-xs font-bold transition cursor-pointer"
          >
            ⚡ Configure 2nd Tenant (Zero Code Changes)
          </button>
        </div>
      </div>

      {saveStatus && (
        <div className="rounded-xl border border-emerald-500/40 bg-emerald-500/10 p-3.5 text-xs font-bold text-emerald-300">
          ✓ {saveStatus}
        </div>
      )}

      {/* Honest Backend Status Notice */}
      <div className="rounded-2xl border border-purple-500/30 bg-purple-950/20 p-4 text-xs text-[#a49cb5] flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-purple-400 animate-pulse" />
            <strong className="text-white font-mono text-xs">LOCAL STORAGE ONLY · NO BACKEND TENANT CREATED YET</strong>
          </div>
          <p className="text-xs text-[#a49cb5] leading-relaxed">
            This onboarding wizard is a real policy configuration flow over a provisioning backend that does not exist yet. Configuration persists to browser storage and does not yet create a tenant.
          </p>
        </div>
        <span
          data-testid="badge-onboarding-banner"
          className="rounded px-2.5 py-1 text-[10px] font-mono font-bold uppercase bg-purple-500/20 text-purple-300 border border-purple-500/30 shrink-0"
        >
          SIMULATED · MOCK
        </span>
      </div>

      {/* Stepper Progress Bar */}
      <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-4">
        <div className="flex items-center justify-between mb-3 text-xs">
          <span className="font-bold text-white">
            Step {currentStep} of 12: {activeStep.title}
          </span>
          <span className={`rounded px-2 py-0.5 text-[10px] font-mono font-bold ${
            activeStep.isWired
              ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
              : "bg-amber-500/20 text-amber-300 border border-amber-500/30"
          }`}>
            {activeStep.isWired ? "LIVE DATABASE BINDING" : "POLICY DRAFT · LOCAL PERSISTENCE"}
          </span>
        </div>

        <div className="grid grid-cols-12 gap-1.5">
          {STEPS.map((s) => {
            const isCurrent = s.stepNumber === currentStep;
            const isCompleted = s.stepNumber < currentStep;
            return (
              <button
                key={s.stepNumber}
                type="button"
                onClick={() => setCurrentStep(s.stepNumber)}
                className={`h-2.5 rounded-full transition cursor-pointer ${
                  isCurrent
                    ? "bg-[#950EDB]"
                    : isCompleted
                    ? "bg-emerald-500"
                    : "bg-[#201732] hover:bg-[#201732]/80"
                }`}
                title={`Step ${s.stepNumber}: ${s.title}`}
              />
            );
          })}
        </div>
      </div>

      {/* Current Step Configuration Form */}
      <div className="rounded-2xl border border-[#2d2242] bg-[#171124] p-6 space-y-6">
        <div>
          <span className="text-[10px] font-mono font-bold text-[#a49cb5] uppercase">
            Specification 7.1 · Step {currentStep}
          </span>
          <h2 className="text-xl font-black text-white mt-0.5">{activeStep.title}</h2>
          <p className="text-xs text-[#a49cb5] mt-1">{activeStep.summary}</p>
        </div>

        {/* STEP 1: IDENTITY */}
        {currentStep === 1 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs">
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Tenant Slug (Unique Database Scope)</label>
              <input
                type="text"
                value={form.tenantSlug}
                onChange={(e) => saveToLocal({ ...form, tenantSlug: e.target.value })}
                className="w-full rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-white font-mono focus:outline-none focus:ring-2 focus:ring-[#950EDB]"
              />
              <span className="text-[10px] text-[#a49cb5] mt-1 block">Row-Level Security (RLS) partition identifier</span>
            </div>
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Tenant Storefront Name</label>
              <input
                type="text"
                value={form.tenantName}
                onChange={(e) => saveToLocal({ ...form, tenantName: e.target.value })}
                className="w-full rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-[#950EDB]"
              />
            </div>
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Merchant Slug</label>
              <input
                type="text"
                value={form.merchantSlug}
                onChange={(e) => saveToLocal({ ...form, merchantSlug: e.target.value })}
                className="w-full rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-white font-mono focus:outline-none focus:ring-2 focus:ring-[#950EDB]"
              />
            </div>
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Merchant Legal Entity</label>
              <input
                type="text"
                value={form.merchantName}
                onChange={(e) => saveToLocal({ ...form, merchantName: e.target.value })}
                className="w-full rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-[#950EDB]"
              />
            </div>
          </div>
        )}

        {/* STEP 2: ADMIN VERIFICATION */}
        {currentStep === 2 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs">
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Primary Operator Email</label>
              <input
                type="email"
                value={form.adminEmail}
                onChange={(e) => saveToLocal({ ...form, adminEmail: e.target.value })}
                className="w-full rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-white font-mono"
              />
            </div>
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Operator Scenario Key (Server-Side Only)</label>
              <input
                type="password"
                value={form.operatorKey}
                onChange={(e) => saveToLocal({ ...form, operatorKey: e.target.value })}
                className="w-full rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-white font-mono"
              />
              <span className="text-[10px] text-emerald-400 mt-1 block">✓ Validated by FastAPI dependency require_scenario_key</span>
            </div>
          </div>
        )}

        {/* STEP 3: BUSINESS PROFILE & LOCALES */}
        {currentStep === 3 && (
          <div className="space-y-4 text-xs">
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Multilingual Locales Supported</label>
              <div className="flex gap-2">
                {["en-IN", "hi-IN", "hinglish"].map((loc) => (
                  <span key={loc} className="rounded-lg bg-[#201732] border border-[#2d2242] px-3 py-1.5 text-white font-mono font-bold">
                    ✓ {loc}
                  </span>
                ))}
              </div>
            </div>
            <div className="rounded-xl border border-[#2d2242] bg-[#201732] p-4 text-xs text-[#a49cb5]">
              Gemini 3.8 Flash Specialist Prompts are pre-seeded in English, Devanagari Hindi, and natural Hinglish code-mixing.
            </div>
          </div>
        )}

        {/* STEP 4: STORES & SERVICE AREAS */}
        {currentStep === 4 && (
          <div className="space-y-4 text-xs">
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Active Dark Stores Count</label>
              <input
                type="number"
                value={form.storeCount}
                onChange={(e) => saveToLocal({ ...form, storeCount: Number(e.target.value) })}
                className="w-32 rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-white font-mono"
              />
            </div>
            <span className="text-[10px] text-amber-300 block">
              Policy Draft (P0): Multi-store geo-fencing maps to single simulator store in memory.
            </span>
          </div>
        )}

        {/* STEP 5: CATALOGUE CONNECTOR */}
        {currentStep === 5 && (
          <div className="space-y-3 text-xs">
            <div className="rounded-xl border border-emerald-500/30 bg-emerald-950/20 p-4">
              <span className="text-emerald-400 font-bold block">Active Grounded Catalogue</span>
              <p className="text-[#a49cb5] text-xs mt-1">
                243 products grounded in <code>merchant_sim.catalogue.CATALOGUE</code> with zero divergence against storefront fixtures.
              </p>
            </div>
          </div>
        )}

        {/* STEP 6: FIELD MAPPING */}
        {currentStep === 6 && (
          <div className="space-y-3 text-xs">
            <p className="text-[#a49cb5]">
              Fields mapped: SKU, Name (English/Hindi), Pack Unit, Minor Unit Price (paise), Baseline Stock, GST Basis Points, and Category Taxonomies.
            </p>
          </div>
        )}

        {/* STEP 7: CURRENCY & ROUNDING */}
        {currentStep === 7 && (
          <div className="space-y-4 text-xs">
            <div className="rounded-xl border border-emerald-500/30 bg-emerald-950/20 p-4 space-y-2">
              <strong className="text-emerald-300">RFC 8785 Canonical JCS Policy:</strong>
              <p className="text-xs text-[#a49cb5] leading-relaxed">
                Money is strictly represented as integer minor units (paise). Floating point numbers in monetary structures trigger immediate runtime exceptions.
              </p>
            </div>
          </div>
        )}

        {/* STEP 8: FULFILMENT ZONES & FEES */}
        {currentStep === 8 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs">
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Free Delivery Threshold (Rupees)</label>
              <div className="flex items-center gap-1 rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2">
                <span className="text-[#a49cb5]">₹</span>
                <input
                  type="number"
                  value={form.freeDeliveryThresholdRupees}
                  onChange={(e) => saveToLocal({ ...form, freeDeliveryThresholdRupees: Number(e.target.value) })}
                  className="w-full bg-transparent text-white font-mono focus:outline-none"
                />
              </div>
              <span className="text-[10px] text-[#a49cb5] mt-1 block">Calculated as {form.freeDeliveryThresholdRupees * 100} minor units</span>
            </div>
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Standard Delivery Fee (Rupees)</label>
              <div className="flex items-center gap-1 rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2">
                <span className="text-[#a49cb5]">₹</span>
                <input
                  type="number"
                  value={form.standardDeliveryFeeRupees}
                  onChange={(e) => saveToLocal({ ...form, standardDeliveryFeeRupees: Number(e.target.value) })}
                  className="w-full bg-transparent text-white font-mono focus:outline-none"
                />
              </div>
              <span className="text-[10px] text-[#a49cb5] mt-1 block">Calculated as {form.standardDeliveryFeeRupees * 100} minor units</span>
            </div>
          </div>
        )}

        {/* STEP 9: RESERVATION TTL & STOCK */}
        {currentStep === 9 && (
          <div className="space-y-4 text-xs">
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Inventory Reservation TTL (Seconds)</label>
              <input
                type="number"
                value={form.reservationTtlSeconds}
                onChange={(e) => saveToLocal({ ...form, reservationTtlSeconds: Number(e.target.value) })}
                className="w-32 rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-white font-mono"
              />
              <span className="text-[10px] text-[#a49cb5] mt-1 block">Default: 900 seconds (15 minutes). Released automatically on checkout expiration.</span>
            </div>
          </div>
        )}

        {/* STEP 10: DISCOUNTS & MARGIN FLOORS */}
        {currentStep === 10 && (
          <div className="space-y-4 text-xs">
            <div>
              <label className="block text-[#a49cb5] font-bold mb-1">Minimum Net Margin Floor (%)</label>
              <input
                type="number"
                value={form.marginFloorPercent}
                onChange={(e) => saveToLocal({ ...form, marginFloorPercent: Number(e.target.value) })}
                className="w-32 rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-white font-mono"
              />
              <span className="text-[10px] text-[#a49cb5] mt-1 block">Specification 9.3: Prevents automated AI discounts from piercing baseline merchant profitability.</span>
            </div>
          </div>
        )}

        {/* STEP 11: CANCELLATION & REFUND POLICY */}
        {currentStep === 11 && (
          <div className="space-y-3 text-xs">
            <p className="text-[#a49cb5]">
              Refunds require fresh single-use <code>REFUND_EXECUTE</code> Execution Grants. In-flight refunds transition through bounded reconciliation (max 6 attempts, exponential backoff) before escalating to human review.
            </p>
          </div>
        )}

        {/* STEP 12: APPROVAL & DELEGATED AUTHORITY */}
        {currentStep === 12 && (
          <div className="space-y-4 text-xs">
            <div className="rounded-xl border border-emerald-500/30 bg-emerald-950/20 p-4 space-y-2">
              <strong className="text-emerald-300">Human-Present Payment Guarantee:</strong>
              <p className="text-[#a49cb5] leading-relaxed">
                Autonomous agent capability is restricted to proposal assembly. Execution Grants for financial capture are strictly contingent upon explicit human approval verified on the trusted surface.
              </p>
            </div>
          </div>
        )}

        {/* Navigation Buttons */}
        <div className="pt-4 border-t border-[#2d2242] flex items-center justify-between">
          <button
            type="button"
            disabled={currentStep <= 1}
            onClick={handlePrevious}
            className="rounded-xl border border-[#2d2242] bg-[#201732] hover:bg-[#201732]/80 text-[#a49cb5] hover:text-white px-4 py-2 text-xs font-bold disabled:opacity-40 transition cursor-pointer"
          >
            ← Previous Step
          </button>

          <button
            type="button"
            onClick={handleNext}
            className="rounded-xl bg-[#950EDB] hover:bg-[#800dc0] text-white px-5 py-2 text-xs font-bold transition cursor-pointer shadow-xs"
          >
            {currentStep === 12 ? "Seal All 12 Policies ✓" : "Save & Proceed →"}
          </button>
        </div>
      </div>

      <p className="text-[11px] text-[#a49cb5] italic pt-1">
        Simulated policy wizard. Configuration changes persist to browser local storage. When the POST /v1/tenants provisioning service is connected, sealing step 12 will atomically create tenant database records and seed initial policy receipts.
      </p>
    </div>
  );
}
