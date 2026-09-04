"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  consoleClient,
  formatPaise,
  type OrderOut,
  type OutboxOut,
  type RefundItem,
  type ReviewQueueCase,
  type SafeModeOut,
} from "@/lib/api";

export default function OperationsPage() {
  const [activeTab, setActiveTab] = useState<"orders" | "refunds" | "review_queue" | "outbox" | "safe_mode">("orders");

  // Orders State
  const [orders, setOrders] = useState<OrderOut[]>([]);
  const [isOrdersLive, setIsOrdersLive] = useState<boolean>(false);
  const [orderFilter, setOrderFilter] = useState<string>("ALL");
  const [selectedOrder, setSelectedOrder] = useState<OrderOut | null>(null);

  // Refunds State
  const [refunds, setRefunds] = useState<RefundItem[]>([]);
  const [isRefundsLive, setIsRefundsLive] = useState<boolean>(false);

  // Review Queue State
  const [reviewCases, setReviewCases] = useState<ReviewQueueCase[]>([]);
  const [isReviewQueueLive, setIsReviewQueueLive] = useState<boolean>(false);

  // Outbox State
  const [outbox, setOutbox] = useState<OutboxOut | null>(null);
  const [revivingId, setRevivingId] = useState<string | null>(null);
  const [reviveFeedback, setReviveFeedback] = useState<string | null>(null);

  // Safe Mode State
  const [safeMode, setSafeMode] = useState<SafeModeOut | null>(null);
  const [safeModeReason, setSafeModeReason] = useState("");
  const [isTogglingSafeMode, setIsTogglingSafeMode] = useState(false);

  useEffect(() => {
    let active = true;
    async function loadData() {
      const [o, r, rq, ob, sm] = await Promise.all([
        consoleClient.getOrders(),
        consoleClient.getRefunds(),
        consoleClient.getReviewQueue(),
        consoleClient.getOutbox(),
        consoleClient.getSafeMode(),
      ]);
      if (active) {
        setOrders(o.orders);
        setIsOrdersLive(o.is_live);
        setRefunds(r.refunds);
        setIsRefundsLive(r.is_live);
        setReviewCases(rq.cases);
        setIsReviewQueueLive(rq.is_live);
        setOutbox(ob);
        setSafeMode(sm);
      }
    }
    loadData();
    return () => {
      active = false;
    };
  }, []);

  const handleRevive = async (commandId: string) => {
    setRevivingId(commandId);
    setReviveFeedback(null);
    try {
      const result = await consoleClient.reviveOutboxCommand(commandId);
      setReviveFeedback(`Command ${commandId} revived! Re-queued with fresh attempt budget (status: ${result.status}).`);
      // Refresh outbox
      const updatedOutbox = await consoleClient.getOutbox();
      setOutbox(updatedOutbox);
    } catch {
      setReviveFeedback("Failed to revive outbox command.");
    } finally {
      setRevivingId(null);
      setTimeout(() => setReviveFeedback(null), 5000);
    }
  };

  const handleToggleSafeMode = async () => {
    if (!safeMode) return;
    setIsTogglingSafeMode(true);
    const targetState = !safeMode.safe_mode;
    const reason = safeModeReason.trim() || (targetState ? "Manual operator emergency trigger" : "Operator stand-down");
    try {
      const updated = await consoleClient.setSafeMode(targetState, reason);
      setSafeMode(updated);
      setSafeModeReason("");
    } finally {
      setIsTogglingSafeMode(false);
    }
  };

  const filteredOrders = orders.filter((ord) => {
    if (orderFilter === "ALL") return true;
    return ord.status === orderFilter;
  });

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-[#2d2242] pb-6">
        <div>
          <div className="flex items-center gap-2 text-xs text-[#a49cb5]">
            <Link href="/" className="hover:underline">Dashboard</Link>
            <span>/</span>
            <span className="text-[#950EDB] font-bold">Operations Center</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-black text-white tracking-tight mt-1">
            Operations &amp; Governance Queue
          </h1>
          <p className="text-xs sm:text-sm text-[#a49cb5] mt-0.5">
            Real-time tracking for orders, refund states, outbox worker recovery, and safe-mode controls.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <span className={`rounded-xl px-3 py-1.5 text-xs font-mono font-black uppercase flex items-center gap-1.5 ${
            safeMode?.safe_mode
              ? "bg-rose-500/20 text-rose-300 border border-rose-500/40"
              : "bg-emerald-500/10 text-emerald-400 border border-emerald-500/30"
          }`}>
            <span className={`h-2 w-2 rounded-full ${safeMode?.safe_mode ? "bg-rose-400" : "bg-emerald-400"} animate-pulse`} />
            {safeMode?.safe_mode ? "SAFE MODE: ACTIVE" : "SYSTEM: NORMAL"}
          </span>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-[#2d2242] gap-6 text-xs font-bold overflow-x-auto scrollbar-none">
        <button
          type="button"
          onClick={() => setActiveTab("orders")}
          className={`pb-3 border-b-2 transition cursor-pointer whitespace-nowrap ${
            activeTab === "orders" ? "border-[#950EDB] text-white" : "border-transparent text-[#a49cb5] hover:text-white"
          }`}
        >
          Orders ({orders.length})
        </button>
        <button
          type="button"
          onClick={() => setActiveTab("refunds")}
          className={`pb-3 border-b-2 transition cursor-pointer whitespace-nowrap ${
            activeTab === "refunds" ? "border-[#950EDB] text-white" : "border-transparent text-[#a49cb5] hover:text-white"
          }`}
        >
          Refund State Tracker ({refunds.length})
        </button>
        <button
          type="button"
          onClick={() => setActiveTab("review_queue")}
          className={`pb-3 border-b-2 transition cursor-pointer whitespace-nowrap ${
            activeTab === "review_queue" ? "border-[#950EDB] text-white" : "border-transparent text-[#a49cb5] hover:text-white"
          }`}
        >
          Human Review Queue ({reviewCases.length})
        </button>
        <button
          type="button"
          onClick={() => setActiveTab("outbox")}
          className={`pb-3 border-b-2 transition cursor-pointer whitespace-nowrap ${
            activeTab === "outbox" ? "border-[#950EDB] text-white" : "border-transparent text-[#a49cb5] hover:text-white"
          }`}
        >
          Outbox &amp; Durable Work ({outbox?.counts.DEAD ?? 0} Dead)
        </button>
        <button
          type="button"
          onClick={() => setActiveTab("safe_mode")}
          className={`pb-3 border-b-2 transition cursor-pointer whitespace-nowrap ${
            activeTab === "safe_mode" ? "border-[#950EDB] text-white" : "border-transparent text-[#a49cb5] hover:text-white"
          }`}
        >
          Kill Switch (Safe Mode)
        </button>
      </div>

      {/* 1. ORDERS VIEW */}
      {activeTab === "orders" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="flex items-center gap-2">
              <span className="text-xs text-[#a49cb5]">Filter by state:</span>
              {["ALL", "CAPTURED", "PARTIALLY_REFUNDED", "PENDING_APPROVAL"].map((st) => (
                <button
                  key={st}
                  type="button"
                  onClick={() => setOrderFilter(st)}
                  className={`rounded-lg px-2.5 py-1 text-xs font-bold transition cursor-pointer ${
                    orderFilter === st
                      ? "bg-[#950EDB] text-white"
                      : "bg-[#201732] text-[#a49cb5] hover:text-white border border-[#2d2242]"
                  }`}
                >
                  {st}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-3">
              <span className="text-xs text-[#a49cb5] font-mono">
                Showing {filteredOrders.length} confirmed orders
              </span>
              <span
                data-testid="badge-orders"
                className={`rounded px-2 py-0.5 text-[9px] font-mono font-bold uppercase ${
                  isOrdersLive
                    ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                    : "bg-purple-500/20 text-purple-300 border border-purple-500/30"
                }`}
              >
                {isOrdersLive ? "LIVE · COMMITTED" : "SIMULATED · MOCK"}
              </span>
            </div>
          </div>

          <div className="rounded-2xl border border-[#2d2242] bg-[#171124] overflow-hidden">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-[#2d2242] bg-[#201732] text-[#a49cb5] uppercase font-mono text-[10px]">
                <tr>
                  <th className="p-3.5">Order ID</th>
                  <th className="p-3.5">State</th>
                  <th className="p-3.5">Amount</th>
                  <th className="p-3.5">Evidence Source</th>
                  <th className="p-3.5">Version</th>
                  <th className="p-3.5">Created At</th>
                  <th className="p-3.5 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#2d2242] text-[#a49cb5]">
                {filteredOrders.map((ord) => (
                  <tr key={ord.order_id} className="hover:bg-[#201732]/40 transition">
                    <td className="p-3.5 font-mono font-bold text-white">{ord.order_id}</td>
                    <td className="p-3.5">
                      <span className={`rounded px-2 py-0.5 text-[10px] font-mono font-bold ${
                        ord.status === "CAPTURED"
                          ? "bg-emerald-500/20 text-emerald-400"
                          : ord.status === "PARTIALLY_REFUNDED"
                          ? "bg-amber-500/20 text-amber-300"
                          : "bg-purple-500/20 text-purple-300"
                      }`}>
                        {ord.status}
                      </span>
                    </td>
                    <td className="p-3.5 font-mono font-bold text-white">
                      {formatPaise(ord.total_minor)}
                    </td>
                    <td className="p-3.5 font-mono text-[11px]">
                      <span className={`inline-flex items-center gap-1 ${
                        ord.capture_evidence_source === "PROVIDER_FETCH" ? "text-emerald-400" : "text-[#a49cb5]"
                      }`}>
                        {ord.capture_evidence_source === "PROVIDER_FETCH" ? "⚡ PROVIDER_FETCH" : ord.capture_evidence_source}
                      </span>
                    </td>
                    <td className="p-3.5 font-mono">v{ord.checkout_version}</td>
                    <td className="p-3.5 font-mono text-[11px]">{ord.created_at}</td>
                    <td className="p-3.5 text-right">
                      <button
                        type="button"
                        onClick={() => setSelectedOrder(ord)}
                        className="rounded-lg bg-[#201732] hover:bg-[#201732]/80 border border-[#2d2242] px-2.5 py-1 text-[11px] font-bold text-white transition cursor-pointer"
                      >
                        Inspect Details
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] text-[#a49cb5] italic pt-1">
            {isOrdersLive
              ? "Live confirmed orders queried from Postgres kernel ledger via GET /v1/orders."
              : "Simulated fixture data (DEMO_ORDERS). When GET /v1/orders is connected to the Postgres kernel ledger, this table will render real confirmed sales generated from verified webhook/fetch capture evidence."}
          </p>

          {/* Modal for Order Detail */}
          {selectedOrder && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-xs">
              <div className="w-full max-w-xl rounded-2xl border border-[#2d2242] bg-[#171124] p-6 space-y-4 shadow-xl">
                <div className="flex items-center justify-between border-b border-[#2d2242] pb-3">
                  <div>
                    <h3 className="text-sm font-black text-white">Order Details: {selectedOrder.order_id}</h3>
                    <span className="text-[10px] font-mono text-[#a49cb5]">Checkout ID: {selectedOrder.checkout_id}</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => setSelectedOrder(null)}
                    className="text-xs text-[#a49cb5] hover:text-white cursor-pointer font-bold"
                  >
                    ✕ Close
                  </button>
                </div>

                <div className="grid grid-cols-2 gap-4 text-xs">
                  <div className="rounded-xl border border-[#2d2242] bg-[#201732] p-3 space-y-1">
                    <span className="text-[10px] text-[#a49cb5]">Amount Settled:</span>
                    <strong className="block text-base font-mono text-white">{formatPaise(selectedOrder.total_minor)}</strong>
                  </div>
                  <div className="rounded-xl border border-[#2d2242] bg-[#201732] p-3 space-y-1">
                    <span className="text-[10px] text-[#a49cb5]">Capture Evidence Source:</span>
                    <strong className="block text-sm font-mono text-emerald-400">{selectedOrder.capture_evidence_source}</strong>
                  </div>
                </div>

                <div className="space-y-2 text-xs">
                  <span className="font-bold text-white text-[11px]">Associated Refunds:</span>
                  {selectedOrder.refunds.length === 0 ? (
                    <p className="text-[#a49cb5] text-[11px] italic">No refunds requested on this order.</p>
                  ) : (
                    selectedOrder.refunds.map((rf) => (
                      <div key={rf.refund_id} className="rounded-lg border border-[#2d2242] p-2 flex justify-between items-center text-[11px]">
                        <span className="font-mono text-white">{rf.refund_id}: {rf.reason}</span>
                        <strong className="font-mono text-rose-400">-{formatPaise(rf.amount_minor)}</strong>
                      </div>
                    ))
                  )}
                </div>

                <div className="pt-3 border-t border-[#2d2242] flex justify-end gap-2">
                  <Link
                    href={`/inspector?attempt_id=${encodeURIComponent(selectedOrder.checkout_id)}`}
                    className="rounded-xl bg-[#950EDB] hover:bg-[#800dc0] text-white px-3.5 py-2 text-xs font-bold transition"
                  >
                    Open Full Proof Chain ➔
                  </Link>
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 2. REFUND VIEW (Four States Strictly Distinguished) */}
      {activeTab === "refunds" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="rounded-2xl border border-amber-500/30 bg-amber-500/10 p-4 text-xs text-amber-200 leading-relaxed flex-1">
              <strong>CRITICAL RECONCILIATION INVARIANT:</strong> The states <code>REFUND_PENDING</code>, <code>REFUND_UNKNOWN</code>, and <code>REFUND_FAILED</code> are strictly segregated. Conflating in-flight pending status with unknown status causes duplicate buyer refunds.
            </div>
            <span
              data-testid="badge-refunds"
              className={`rounded px-2 py-0.5 text-[9px] font-mono font-bold uppercase shrink-0 ${
                isRefundsLive
                  ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                  : "bg-purple-500/20 text-purple-300 border border-purple-500/30"
              }`}
            >
              {isRefundsLive ? "LIVE · COMMITTED" : "SIMULATED · MOCK"}
            </span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            {[
              { state: "REFUND_PENDING", title: "In-Flight to Provider", count: refunds.filter((r) => r.state === "REFUND_PENDING").length, color: "text-amber-400", border: "border-amber-500/30" },
              { state: "REFUND_UNKNOWN", title: "Reconciling Worker", count: refunds.filter((r) => r.state === "REFUND_UNKNOWN").length, color: "text-purple-400", border: "border-purple-500/30" },
              { state: "REFUND_FAILED", title: "Rejected by Gateway", count: refunds.filter((r) => r.state === "REFUND_FAILED").length, color: "text-rose-400", border: "border-rose-500/30" },
              { state: "PROCESSED", title: "Settled Capture Refund", count: refunds.filter((r) => r.state === "PROCESSED").length, color: "text-emerald-400", border: "border-emerald-500/30" },
            ].map((st) => (
              <div key={st.state} className={`rounded-2xl border ${st.border} bg-[#171124] p-4 space-y-1`}>
                <span className="text-[10px] font-mono font-bold text-[#a49cb5] uppercase">{st.state}</span>
                <strong className={`text-2xl font-mono font-black ${st.color} block`}>{st.count}</strong>
                <span className="text-[11px] text-[#a49cb5]">{st.title}</span>
              </div>
            ))}
          </div>

          <div className="rounded-2xl border border-[#2d2242] bg-[#171124] overflow-hidden">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-[#2d2242] bg-[#201732] text-[#a49cb5] uppercase font-mono text-[10px]">
                <tr>
                  <th className="p-3.5">Refund ID</th>
                  <th className="p-3.5">State</th>
                  <th className="p-3.5">Amount</th>
                  <th className="p-3.5">Reason</th>
                  <th className="p-3.5">Reconciliation Cycles</th>
                  <th className="p-3.5">Provider Refund ID</th>
                  <th className="p-3.5">Created At</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#2d2242] text-[#a49cb5]">
                {refunds.map((rf) => (
                  <tr key={rf.refund_id} className="hover:bg-[#201732]/40 transition">
                    <td className="p-3.5 font-mono font-bold text-white">{rf.refund_id}</td>
                    <td className="p-3.5">
                      <span
                        data-refund-state={rf.state}
                        className={`rounded px-2 py-0.5 text-[10px] font-mono font-bold ${
                          rf.state === "PROCESSED"
                            ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                            : rf.state === "REFUND_PENDING"
                            ? "bg-amber-500/20 text-amber-300 border border-amber-500/30"
                            : rf.state === "REFUND_UNKNOWN"
                            ? "bg-purple-500/20 text-purple-300 border border-purple-500/40 font-black tracking-wide"
                            : "bg-rose-500/20 text-rose-300 border border-rose-500/30"
                        }`}
                      >
                        {rf.state}
                      </span>
                    </td>
                    <td className="p-3.5 font-mono font-bold text-white">
                      {formatPaise(rf.amount_minor)}
                    </td>
                    <td className="p-3.5 text-xs text-white">{rf.reason}</td>
                    <td className="p-3.5 font-mono text-center">{rf.reconciliation_attempts} / 6</td>
                    <td className="p-3.5 font-mono text-[11px] text-[#a49cb5]">
                      {rf.provider_refund_id || "— (Awaiting Ack)"}
                    </td>
                    <td className="p-3.5 font-mono text-[11px]">{rf.created_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] text-[#a49cb5] italic pt-1">
            {isRefundsLive
              ? "Live refunds queried from Postgres kernel ledger via GET /v1/refunds."
              : "Simulated fixture data (DEMO_REFUNDS). When GET /v1/refunds is connected, this table will stream buyer-confirmed refunds executed under single-use REFUND_EXECUTE grants."}
          </p>
        </div>
      )}

      {/* 3. HUMAN REVIEW QUEUE (READ-ONLY) */}
      {activeTab === "review_queue" && (
        <div className="space-y-4">
          <div className="rounded-2xl border border-purple-500/30 bg-purple-950/20 p-4 flex items-center justify-between gap-4">
            <div>
              <span className="text-xs font-black text-white block">Read-Only Governance Queue (Specification P0)</span>
              <p className="text-xs text-[#a49cb5] mt-0.5">
                Displays escalated cases, blocking reason codes, and verified provider state at escalation. <strong>Resolution happens outside this surface today.</strong>
              </p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <span
                data-testid="badge-review-queue"
                className={`rounded px-2 py-0.5 text-[9px] font-mono font-bold uppercase ${
                  isReviewQueueLive
                    ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                    : "bg-purple-500/20 text-purple-300 border border-purple-500/30"
                }`}
              >
                {isReviewQueueLive ? "LIVE · COMMITTED" : "SIMULATED · MOCK"}
              </span>
              <span className="rounded-lg bg-[#201732] border border-purple-500/40 text-purple-300 px-3 py-1 text-xs font-bold">
                Zero Arbitrary Modals
              </span>
            </div>
          </div>

          <div className="space-y-3">
            {reviewCases.map((cs) => (
              <div key={cs.case_id} className="rounded-2xl border border-[#2d2242] bg-[#171124] p-5 space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-black text-white text-sm">{cs.case_id}</span>
                    <span className="rounded bg-rose-500/20 text-rose-300 border border-rose-500/40 px-2 py-0.5 text-[10px] font-mono font-bold">
                      {cs.blocking_reason_code}
                    </span>
                  </div>
                  <span className="font-mono text-xs font-bold text-white">
                    Amount at Risk: {formatPaise(cs.amount_minor)}
                  </span>
                </div>

                <p className="text-xs text-[#a49cb5] leading-relaxed">
                  {cs.redacted_timeline_snippet}
                </p>

                <div className="flex flex-wrap items-center justify-between gap-2 pt-2 border-t border-[#2d2242] text-[11px] font-mono text-[#a49cb5]">
                  <div>
                    <span>Escalated: {cs.escalated_at}</span> · <span>Provider State: {cs.provider_state_at_escalation}</span>
                  </div>
                  <Link
                    href={`/inspector?attempt_id=${encodeURIComponent(cs.checkout_id)}`}
                    className="text-[#950EDB] hover:underline font-bold"
                  >
                    View Forensic Evidence Chain ➔
                  </Link>
                </div>
              </div>
            ))}
          </div>
          <p className="text-[11px] text-[#a49cb5] italic pt-1">
            Simulated review queue (DEMO_REVIEW_QUEUE). Human-review cases will populate here once the Reconciliation and Resolution background services are wired to flag price-surge exceptions and exhausted provider retry budgets.
          </p>
        </div>
      )}

      {/* 4. OUTBOX VIEW & REVIVE ACTION */}
      {activeTab === "outbox" && (
        <div className="space-y-4">
          {reviveFeedback && (
            <div className="rounded-xl border border-emerald-500/40 bg-emerald-500/10 p-3 text-xs text-emerald-300 font-bold">
              ✓ {reviveFeedback}
            </div>
          )}

          {/* Counts */}
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
            {[
              { status: "PENDING", count: outbox?.counts.PENDING ?? 0, color: "text-amber-400" },
              { status: "LEASED", count: outbox?.counts.LEASED ?? 0, color: "text-blue-400" },
              { status: "DONE", count: outbox?.counts.DONE ?? 0, color: "text-emerald-400" },
              { status: "FAILED", count: outbox?.counts.FAILED ?? 0, color: "text-rose-400" },
              { status: "DEAD", count: outbox?.counts.DEAD ?? 0, color: "text-rose-500" },
            ].map((c) => (
              <div key={c.status} className="rounded-xl border border-[#2d2242] bg-[#171124] p-3 text-center">
                <span className="text-[10px] font-mono text-[#a49cb5]">{c.status}</span>
                <strong className={`block text-xl font-mono font-black ${c.color}`}>{c.count}</strong>
              </div>
            ))}
          </div>

          {/* Outbox Badge & Header */}
          <div className="flex items-center justify-between">
            <span className="text-xs text-[#a49cb5] font-mono">
              Transactional Outbox Operations
            </span>
            <span
              data-testid="badge-outbox"
              className={`rounded px-2 py-0.5 text-[9px] font-mono font-bold uppercase ${
                outbox?.is_live
                  ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                  : "bg-purple-500/20 text-purple-300 border border-purple-500/30"
              }`}
            >
              {outbox?.is_live ? "LIVE · COMMITTED" : "SIMULATED · MOCK"}
            </span>
          </div>

          {/* Commands Table */}
          <div className="rounded-2xl border border-[#2d2242] bg-[#171124] overflow-hidden">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-[#2d2242] bg-[#201732] text-[#a49cb5] uppercase font-mono text-[10px]">
                <tr>
                  <th className="p-3.5">Command ID</th>
                  <th className="p-3.5">Type</th>
                  <th className="p-3.5">Status</th>
                  <th className="p-3.5">Attempts</th>
                  <th className="p-3.5">Error / Note</th>
                  <th className="p-3.5">Created At</th>
                  <th className="p-3.5 text-right">Recovery</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#2d2242] text-[#a49cb5]">
                {outbox?.commands.map((cmd) => (
                  <tr key={cmd.command_id} className="hover:bg-[#201732]/40 transition">
                    <td className="p-3.5 font-mono font-bold text-white">{cmd.command_id}</td>
                    <td className="p-3.5 font-mono text-white text-[11px]">{cmd.command_type}</td>
                    <td className="p-3.5">
                      <span className={`rounded px-2 py-0.5 text-[10px] font-mono font-bold ${
                        cmd.status === "DONE"
                          ? "bg-emerald-500/20 text-emerald-400"
                          : cmd.status === "DEAD"
                          ? "bg-rose-500/20 text-rose-400"
                          : "bg-amber-500/20 text-amber-300"
                      }`}>
                        {cmd.status}
                      </span>
                    </td>
                    <td className="p-3.5 font-mono text-center">{cmd.attempts}</td>
                    <td className="p-3.5 text-[11px] text-rose-300/80 max-w-xs truncate" title={cmd.last_error || ""}>
                      {cmd.last_error || "—"}
                    </td>
                    <td className="p-3.5 font-mono text-[11px]">{cmd.created_at}</td>
                    <td className="p-3.5 text-right">
                      {cmd.status === "DEAD" ? (
                        <button
                          type="button"
                          disabled={revivingId === cmd.command_id}
                          onClick={() => void handleRevive(cmd.command_id)}
                          className="rounded-lg bg-rose-600 hover:bg-rose-700 text-white px-2.5 py-1 text-[10px] font-bold transition disabled:opacity-50 cursor-pointer"
                        >
                          {revivingId === cmd.command_id ? "Reviving..." : "Revive Command"}
                        </button>
                      ) : (
                        <span className="text-[10px] text-[#a49cb5]">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] text-[#a49cb5] italic pt-1">
            {outbox?.is_live
              ? "Live transactional outbox commands read from Postgres ops schema via /v1/ops/outbox."
              : "Simulated outbox commands (DEMO_OUTBOX). When connected, displays live transactional worker lease states and retry budgets."}
          </p>
        </div>
      )}

      {/* 5. SAFE MODE KILL SWITCH */}
      {activeTab === "safe_mode" && (
        <div className="max-w-2xl space-y-6">
          <div className={`rounded-2xl border p-6 space-y-4 ${
            safeMode?.safe_mode
              ? "border-rose-500/50 bg-rose-950/20"
              : "border-emerald-500/30 bg-emerald-950/10"
          }`}>
            <div className="flex items-center justify-between">
              <h2 className="text-base font-black text-white">Tenant Safe Mode Control (Kill Switch)</h2>
              <span className={`rounded px-2.5 py-1 text-xs font-mono font-bold ${
                safeMode?.safe_mode
                  ? "bg-rose-500/30 text-rose-300"
                  : "bg-emerald-500/20 text-emerald-400"
              }`}>
                CURRENT: {safeMode?.mode ?? "NORMAL"}
              </span>
            </div>

            <p className="text-xs text-[#a49cb5] leading-relaxed">
              Specification 10.3.2 reserves the switch for an operator or an allowlisted deterministic incident rule. <strong>An AI agent may never enter or leave Safe Mode.</strong> When active, non-essential grants are blocked while refund processing and inspection remain available.
            </p>

            <div className="grid grid-cols-2 gap-3 text-xs pt-2">
              <div className="rounded-xl border border-[#2d2242] bg-[#171124] p-3 space-y-1">
                <span className="text-[10px] font-bold text-rose-400 uppercase block">Operations Blocked</span>
                <ul className="space-y-0.5 text-[11px] text-[#a49cb5]">
                  <li>• Dispatching new Execution Grants</li>
                  <li>• Admitting non-essential proposals</li>
                </ul>
              </div>
              <div className="rounded-xl border border-[#2d2242] bg-[#171124] p-3 space-y-1">
                <span className="text-[10px] font-bold text-emerald-400 uppercase block">Still Permitted</span>
                <ul className="space-y-0.5 text-[11px] text-[#a49cb5]">
                  <li>• Refund execution under fresh grants</li>
                  <li>• Forensic inspection and audit reads</li>
                </ul>
              </div>
            </div>

            <div className="pt-4 border-t border-[#2d2242] space-y-3">
              <label className="block text-xs font-bold text-white">
                Audited Operator Reason String:
                <input
                  type="text"
                  value={safeModeReason}
                  onChange={(e) => setSafeModeReason(e.target.value)}
                  placeholder="e.g. Upstream provider degraded latency anomaly"
                  className="mt-1 w-full rounded-xl border border-[#2d2242] bg-[#171124] px-3.5 py-2 text-xs text-white placeholder:text-[#a49cb5] focus:outline-none focus:ring-2 focus:ring-[#950EDB]"
                />
              </label>

              <button
                type="button"
                disabled={isTogglingSafeMode}
                onClick={() => void handleToggleSafeMode()}
                className={`w-full rounded-xl py-2.5 text-xs font-black transition cursor-pointer ${
                  safeMode?.safe_mode
                    ? "bg-emerald-600 hover:bg-emerald-700 text-white"
                    : "bg-rose-600 hover:bg-rose-700 text-white"
                }`}
              >
                {isTogglingSafeMode
                  ? "Transacting with Kernel..."
                  : safeMode?.safe_mode
                  ? "STAND DOWN SAFE MODE (RETURN TO NORMAL)"
                  : "ENGAGE SAFE MODE KILL SWITCH"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
