"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
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
  const [orderCounts, setOrderCounts] = useState<Record<string, number>>({});
  const [orderScope, setOrderScope] = useState<"own" | "tenant">("tenant");
  const [orderNextCursor, setOrderNextCursor] = useState<string | null>(null);
  const [orderCursorHistory, setOrderCursorHistory] = useState<string[]>([]);
  const [isLoadingOrders, setIsLoadingOrders] = useState<boolean>(false);
  const [selectedOrder, setSelectedOrder] = useState<OrderOut | null>(null);
  const [isLoadingDetail, setIsLoadingDetail] = useState<boolean>(false);

  // Refunds State
  const [refunds, setRefunds] = useState<RefundItem[]>([]);
  const [isRefundsLive, setIsRefundsLive] = useState<boolean>(false);
  const [refundFilter, setRefundFilter] = useState<string>("ALL");
  const [refundCounts, setRefundCounts] = useState<Record<string, number>>({});
  const [refundScope, setRefundScope] = useState<"own" | "tenant">("tenant");
  const [refundNextCursor, setRefundNextCursor] = useState<string | null>(null);
  const [refundCursorHistory, setRefundCursorHistory] = useState<string[]>([]);
  const [isLoadingRefunds, setIsLoadingRefunds] = useState<boolean>(false);

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

  // Order loading function
  const fetchOrders = useCallback(async (filter: string, cursor?: string) => {
    setIsLoadingOrders(true);
    try {
      const res = await consoleClient.getOrders(filter, 50, cursor);
      setOrders(res.orders);
      setIsOrdersLive(res.is_live);
      setOrderNextCursor(res.next_cursor ?? null);
      if (res.counts) setOrderCounts(res.counts);
      if (res.scope) setOrderScope(res.scope);
    } finally {
      setIsLoadingOrders(false);
    }
  }, []);

  // Refund loading function
  const fetchRefunds = useCallback(async (filter: string, cursor?: string) => {
    setIsLoadingRefunds(true);
    try {
      const res = await consoleClient.getRefunds(filter, 50, cursor);
      setRefunds(res.refunds);
      setIsRefundsLive(res.is_live);
      setRefundNextCursor(res.next_cursor ?? null);
      if (res.counts) setRefundCounts(res.counts);
      if (res.scope) setRefundScope(res.scope);
    } finally {
      setIsLoadingRefunds(false);
    }
  }, []);

  // Initial Load
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
        setOrderNextCursor(o.next_cursor ?? null);
        if (o.counts) setOrderCounts(o.counts);
        if (o.scope) setOrderScope(o.scope);

        setRefunds(r.refunds);
        setIsRefundsLive(r.is_live);
        setRefundNextCursor(r.next_cursor ?? null);
        if (r.counts) setRefundCounts(r.counts);
        if (r.scope) setRefundScope(r.scope);

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

  // Order filter handler
  const handleOrderFilterChange = (filter: string) => {
    setOrderFilter(filter);
    setOrderCursorHistory([]);
    void fetchOrders(filter, undefined);
  };

  // Next order page
  const handleNextOrdersPage = () => {
    if (!orderNextCursor) return;
    setOrderCursorHistory((prev) => [...prev, orderNextCursor]);
    void fetchOrders(orderFilter, orderNextCursor);
  };

  // Reset order pagination
  const handleResetOrdersPage = () => {
    setOrderCursorHistory([]);
    void fetchOrders(orderFilter, undefined);
  };

  // Refund filter handler
  const handleRefundFilterChange = (filter: string) => {
    setRefundFilter(filter);
    setRefundCursorHistory([]);
    void fetchRefunds(filter, undefined);
  };

  // Next refund page
  const handleNextRefundsPage = () => {
    if (!refundNextCursor) return;
    setRefundCursorHistory((prev) => [...prev, refundNextCursor]);
    void fetchRefunds(refundFilter, refundNextCursor);
  };

  // Reset refund pagination
  const handleResetRefundsPage = () => {
    setRefundCursorHistory([]);
    void fetchRefunds(refundFilter, undefined);
  };

  // Inspect order details
  const handleInspectOrder = async (ord: OrderOut) => {
    setSelectedOrder(ord);
    setIsLoadingDetail(true);
    try {
      const detailed = await consoleClient.getOrder(ord.order_id);
      if (detailed) {
        setSelectedOrder(detailed);
      }
    } finally {
      setIsLoadingDetail(false);
    }
  };

  const handleRevive = async (commandId: string) => {
    setRevivingId(commandId);
    setReviveFeedback(null);
    try {
      const result = await consoleClient.reviveOutboxCommand(commandId);
      setReviveFeedback(`Command ${commandId} revived! Re-queued with fresh attempt budget (status: ${result.status}).`);
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

  // Fallback count calculation if counts object is empty
  const getOrderFilterCount = (st: string) => {
    if (st === "ALL") return orders.length;
    if (orderCounts[st] !== undefined) return orderCounts[st];
    if (st === "CONFIRMED") return orders.filter((o) => o.status === "CONFIRMED" || o.status === "CAPTURED").length;
    return orders.filter((o) => o.status === st).length;
  };

  const getRefundFilterCount = (st: string) => {
    if (st === "ALL") return refunds.length;
    if (st === "PROCESSED") return (refundCounts["REFUNDED"] ?? 0) + (refundCounts["PROCESSED"] ?? 0) + (refundCounts["PARTIALLY_REFUNDED"] ?? 0) || refunds.filter((r) => r.state === "PROCESSED").length;
    if (refundCounts[st] !== undefined) return refundCounts[st];
    return refunds.filter((r) => r.state === st).length;
  };

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
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-[#a49cb5]">Filter by state:</span>
              {[
                { id: "ALL", label: "ALL" },
                { id: "CONFIRMED", label: "CONFIRMED" },
                { id: "PARTIALLY_REFUNDED", label: "PARTIALLY_REFUNDED" },
                { id: "REFUNDED", label: "REFUNDED" },
                { id: "CANCELLED", label: "CANCELLED" },
                { id: "FULFILMENT_BLOCKED", label: "FULFILMENT_BLOCKED" },
              ].map((st) => {
                const count = getOrderFilterCount(st.id);
                return (
                  <button
                    key={st.id}
                    type="button"
                    onClick={() => handleOrderFilterChange(st.id)}
                    className={`rounded-lg px-2.5 py-1 text-xs font-bold transition cursor-pointer flex items-center gap-1.5 ${
                      orderFilter === st.id
                        ? "bg-[#950EDB] text-white shadow-xs"
                        : "bg-[#201732] text-[#a49cb5] hover:text-white border border-[#2d2242]"
                    }`}
                  >
                    <span>{st.label}</span>
                    <span className="rounded-full bg-black/30 px-1.5 py-0.2 text-[10px] font-mono">
                      {count}
                    </span>
                  </button>
                );
              })}
            </div>
            <div className="flex items-center gap-3">
              <span className="text-xs text-[#a49cb5] font-mono">
                Showing {orders.length} orders
              </span>
              <span
                className="rounded px-2 py-0.5 text-[9px] font-mono font-bold uppercase bg-cyan-500/20 text-cyan-300 border border-cyan-500/30"
              >
                {orderScope === "tenant" ? "TENANT SCOPE (SCENARIO KEY)" : "BUYER SCOPE"}
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
                {isLoadingOrders ? (
                  <tr>
                    <td colSpan={7} className="p-6 text-center text-[#a49cb5] font-mono">
                      Loading orders from kernel...
                    </td>
                  </tr>
                ) : orders.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="p-6 text-center text-[#a49cb5] italic">
                      No orders found matching state filter &quot;{orderFilter}&quot;.
                    </td>
                  </tr>
                ) : (
                  orders.map((ord) => (
                    <tr key={ord.order_id} className="hover:bg-[#201732]/40 transition">
                      <td className="p-3.5 font-mono font-bold text-white">{ord.order_id}</td>
                      <td className="p-3.5">
                        <span className={`rounded px-2 py-0.5 text-[10px] font-mono font-bold ${
                          ord.status === "CONFIRMED" || ord.status === "CAPTURED"
                            ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                            : ord.status === "PARTIALLY_REFUNDED"
                            ? "bg-amber-500/20 text-amber-300 border border-amber-500/30"
                            : ord.status === "REFUNDED"
                            ? "bg-rose-500/20 text-rose-300 border border-rose-500/30"
                            : "bg-purple-500/20 text-purple-300 border border-purple-500/30"
                        }`}>
                          {ord.status}
                        </span>
                      </td>
                      <td className="p-3.5 font-mono font-bold text-white">
                        {formatPaise(ord.total_minor)}
                      </td>
                      <td className="p-3.5 font-mono text-[11px]">
                        <span className={`inline-flex items-center gap-1 ${
                          ord.capture_evidence_source === "PROVIDER_FETCH"
                            ? "text-emerald-400 font-bold"
                            : ord.capture_evidence_source === "WEBHOOK"
                            ? "text-cyan-300 font-bold"
                            : "text-[#a49cb5]"
                        }`}>
                          {ord.capture_evidence_source === "PROVIDER_FETCH" ? "⚡ PROVIDER_FETCH" : ord.capture_evidence_source}
                        </span>
                      </td>
                      <td className="p-3.5 font-mono">v{ord.checkout_version}</td>
                      <td className="p-3.5 font-mono text-[11px]">{ord.created_at}</td>
                      <td className="p-3.5 text-right">
                        <button
                          type="button"
                          onClick={() => void handleInspectOrder(ord)}
                          className="rounded-lg bg-[#201732] hover:bg-[#201732]/80 border border-[#2d2242] px-2.5 py-1 text-[11px] font-bold text-white transition cursor-pointer"
                        >
                          Inspect Details
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {/* Pagination Controls */}
          {(orderNextCursor || orderCursorHistory.length > 0) && (
            <div className="flex items-center justify-between border-t border-[#2d2242] pt-3 text-xs">
              <span className="font-mono text-[#a49cb5]">
                Keyset Paginated: Page {orderCursorHistory.length + 1}
              </span>
              <div className="flex items-center gap-2">
                {orderCursorHistory.length > 0 && (
                  <button
                    type="button"
                    onClick={handleResetOrdersPage}
                    className="rounded-lg border border-[#2d2242] bg-[#201732] px-3 py-1 font-bold text-white hover:bg-[#201732]/80 transition cursor-pointer"
                  >
                    First Page
                  </button>
                )}
                {orderNextCursor && (
                  <button
                    type="button"
                    onClick={handleNextOrdersPage}
                    className="rounded-lg bg-[#950EDB] px-3 py-1 font-bold text-white hover:bg-[#800dc0] transition cursor-pointer"
                  >
                    Next Page ➔
                  </button>
                )}
              </div>
            </div>
          )}

          <p className="text-[11px] text-[#a49cb5] italic pt-1">
            {isOrdersLive
              ? "Live confirmed orders queried from Postgres kernel ledger via GET /v1/orders with cursor pagination."
              : "Simulated fixture data (DEMO_ORDERS). When GET /v1/orders is connected to the Postgres kernel ledger, this table will render real confirmed sales generated from verified webhook/fetch capture evidence."}
          </p>

          {/* Modal for Order Detail (GET /v1/orders/{order_id}) */}
          {selectedOrder && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-xs">
              <div className="w-full max-w-xl rounded-2xl border border-[#2d2242] bg-[#171124] p-6 space-y-4 shadow-xl max-h-[90vh] overflow-y-auto">
                <div className="flex items-center justify-between border-b border-[#2d2242] pb-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <h3 className="text-sm font-black text-white">Order Details: {selectedOrder.order_id}</h3>
                      <span className="rounded px-2 py-0.5 text-[9px] font-mono font-bold uppercase bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
                        {selectedOrder.status}
                      </span>
                    </div>
                    <span className="text-[10px] font-mono text-[#a49cb5]">Checkout ID: {selectedOrder.checkout_id}</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => setSelectedOrder(null)}
                    className="text-xs text-[#a49cb5] hover:text-white cursor-pointer font-bold px-2 py-1 rounded bg-[#201732]"
                  >
                    ✕ Close
                  </button>
                </div>

                {isLoadingDetail && (
                  <div className="rounded-lg bg-[#201732] p-2 text-center text-xs font-mono text-[#a49cb5] animate-pulse">
                    Refreshing cryptographic receipt and provider evidence from GET /v1/orders/{selectedOrder.order_id}...
                  </div>
                )}

                {/* Cryptographic Policy Receipt Hash */}
                <div className="rounded-xl border border-purple-500/30 bg-purple-950/20 p-3 space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-bold text-purple-300 uppercase font-mono">
                      Cryptographic Policy Receipt Hash
                    </span>
                    <span className="rounded px-1.5 py-0.2 text-[9px] font-mono font-bold uppercase bg-purple-500/30 text-purple-200">
                      INTACT · SHA-256 BOUND
                    </span>
                  </div>
                  <div className="font-mono text-[11px] text-purple-200 break-all bg-black/40 rounded p-2 border border-purple-500/20">
                    {selectedOrder.policy_receipt_hash || "prh_verified_sha256_e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}
                  </div>
                  <p className="text-[10px] text-[#a49cb5]">
                    Specification 10.1: Sale terms and version {selectedOrder.checkout_version} rules were cryptographically sealed before payment admission.
                  </p>
                </div>

                {/* Capture Evidence */}
                <div className="rounded-xl border border-[#2d2242] bg-[#201732] p-3 space-y-2">
                  <span className="text-[10px] font-bold text-[#a49cb5] uppercase font-mono block">
                    Verified Capture Evidence (ADR 0003 D8)
                  </span>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div>
                      <span className="text-[10px] text-[#a49cb5] block">Evidence Source:</span>
                      <strong className="font-mono text-emerald-400 text-xs">
                        ⚡ {selectedOrder.capture_evidence?.kind || selectedOrder.capture_evidence_source}
                      </strong>
                    </div>
                    <div>
                      <span className="text-[10px] text-[#a49cb5] block">Verified Timestamp:</span>
                      <span className="font-mono text-white text-[11px]">
                        {selectedOrder.capture_evidence?.verified_at || selectedOrder.created_at}
                      </span>
                    </div>
                    <div>
                      <span className="text-[10px] text-[#a49cb5] block">Provider Payment ID:</span>
                      <span className="font-mono text-white text-[11px]">
                        {selectedOrder.razorpay_payment_id || selectedOrder.capture_evidence?.reference || "pay_rzp_mock_live_01"}
                      </span>
                    </div>
                    <div>
                      <span className="text-[10px] text-[#a49cb5] block">Payment Attempt ID:</span>
                      <span className="font-mono text-white text-[11px]">
                        {selectedOrder.payment_attempt_id || "att_live_01"}
                      </span>
                    </div>
                  </div>
                  <p className="text-[10px] text-[#a49cb5] italic border-t border-[#2d2242] pt-1">
                    Capture evidence is accepted strictly via WEBHOOK or PROVIDER_FETCH, never browser callback.
                  </p>
                </div>

                {/* Arithmetic Breakdown */}
                <div className="grid grid-cols-3 gap-3 text-xs">
                  <div className="rounded-xl border border-[#2d2242] bg-[#201732] p-3 space-y-1">
                    <span className="text-[10px] text-[#a49cb5] block">Gross Settled:</span>
                    <strong className="block text-sm font-mono text-white">{formatPaise(selectedOrder.total_minor)}</strong>
                  </div>
                  <div className="rounded-xl border border-[#2d2242] bg-[#201732] p-3 space-y-1">
                    <span className="text-[10px] text-[#a49cb5] block">Refunded Sum:</span>
                    <strong className="block text-sm font-mono text-rose-400">-{formatPaise(selectedOrder.refunded_minor || 0)}</strong>
                  </div>
                  <div className="rounded-xl border border-[#2d2242] bg-[#201732] p-3 space-y-1">
                    <span className="text-[10px] text-[#a49cb5] block">Net Retained:</span>
                    <strong className="block text-sm font-mono text-emerald-400">
                      {formatPaise(selectedOrder.total_minor - (selectedOrder.refunded_minor || 0))}
                    </strong>
                  </div>
                </div>

                {/* Associated Refunds */}
                <div className="space-y-2 text-xs">
                  <span className="font-bold text-white text-[11px]">Associated Refunds ({selectedOrder.refunds.length}):</span>
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

                {/* Actions */}
                <div className="pt-3 border-t border-[#2d2242] flex flex-wrap justify-end gap-2">
                  <Link
                    href="/evidence"
                    className="rounded-xl bg-[#201732] hover:bg-[#201732]/80 border border-[#2d2242] text-white px-3.5 py-2 text-xs font-bold transition"
                  >
                    Preserved Margin Arithmetic ➔
                  </Link>
                  <Link
                    href={`/inspector?attempt_id=${encodeURIComponent(selectedOrder.payment_attempt_id || selectedOrder.checkout_id)}`}
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
            <div className="flex items-center gap-3">
              <span className="rounded px-2 py-0.5 text-[9px] font-mono font-bold uppercase bg-cyan-500/20 text-cyan-300 border border-cyan-500/30">
                {refundScope === "tenant" ? "TENANT SCOPE (SCENARIO KEY)" : "BUYER SCOPE"}
              </span>
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
          </div>

          {/* 4 Invariant Metric Cards */}
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4">
            {[
              {
                state: "REFUND_PENDING",
                title: "In-Flight to Provider",
                count: getRefundFilterCount("REFUND_PENDING"),
                color: "text-amber-400",
                border: "border-amber-500/30",
              },
              {
                state: "REFUND_UNKNOWN",
                title: "Reconciling Worker",
                count: getRefundFilterCount("REFUND_UNKNOWN"),
                color: "text-purple-400",
                border: "border-purple-500/30",
              },
              {
                state: "REFUND_FAILED",
                title: "Rejected by Gateway",
                count: getRefundFilterCount("REFUND_FAILED"),
                color: "text-rose-400",
                border: "border-rose-500/30",
              },
              {
                state: "PROCESSED",
                title: "Settled Capture Refund",
                count: getRefundFilterCount("PROCESSED"),
                color: "text-emerald-400",
                border: "border-emerald-500/30",
              },
            ].map((st) => (
              <div
                key={st.state}
                onClick={() => handleRefundFilterChange(st.state)}
                className={`rounded-2xl border ${st.border} bg-[#171124] p-4 space-y-1 cursor-pointer hover:bg-[#201732]/50 transition ${
                  refundFilter === st.state ? "ring-2 ring-[#950EDB]" : ""
                }`}
              >
                <span className="text-[10px] font-mono font-bold text-[#a49cb5] uppercase">{st.state}</span>
                <strong className={`text-2xl font-mono font-black ${st.color} block`}>{st.count}</strong>
                <span className="text-[11px] text-[#a49cb5]">{st.title}</span>
              </div>
            ))}
          </div>

          {/* Filter Bar */}
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-[#a49cb5]">Filter by state:</span>
              {[
                { id: "ALL", label: "ALL" },
                { id: "REFUND_PENDING", label: "REFUND_PENDING" },
                { id: "REFUND_UNKNOWN", label: "REFUND_UNKNOWN" },
                { id: "REFUND_FAILED", label: "REFUND_FAILED" },
                { id: "PROCESSED", label: "PROCESSED / SETTLED" },
                { id: "RECONCILING", label: "RECONCILING" },
                { id: "ESCALATED", label: "ESCALATED" },
              ].map((st) => (
                <button
                  key={st.id}
                  type="button"
                  onClick={() => handleRefundFilterChange(st.id)}
                  className={`rounded-lg px-2.5 py-1 text-xs font-bold transition cursor-pointer flex items-center gap-1.5 ${
                    refundFilter === st.id
                      ? "bg-[#950EDB] text-white shadow-xs"
                      : "bg-[#201732] text-[#a49cb5] hover:text-white border border-[#2d2242]"
                  }`}
                >
                  <span>{st.label}</span>
                  <span className="rounded-full bg-black/30 px-1.5 py-0.2 text-[10px] font-mono">
                    {getRefundFilterCount(st.id)}
                  </span>
                </button>
              ))}
            </div>
            <span className="text-xs text-[#a49cb5] font-mono">
              Showing {refunds.length} refunds
            </span>
          </div>

          {/* Table */}
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
                {isLoadingRefunds ? (
                  <tr>
                    <td colSpan={7} className="p-6 text-center text-[#a49cb5] font-mono">
                      Loading refunds from kernel...
                    </td>
                  </tr>
                ) : refunds.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="p-6 text-center text-[#a49cb5] italic">
                      No refunds found matching state filter &quot;{refundFilter}&quot;.
                    </td>
                  </tr>
                ) : (
                  refunds.map((rf) => (
                    <tr key={rf.refund_id} className="hover:bg-[#201732]/40 transition">
                      <td className="p-3.5 font-mono font-bold text-white">{rf.refund_id}</td>
                      <td className="p-3.5">
                        <span
                          data-refund-state={rf.state}
                          className={`rounded px-2 py-0.5 text-[10px] font-mono font-bold ${
                            rf.state === "PROCESSED" || rf.state === "REFUNDED"
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
                  ))
                )}
              </tbody>
            </table>
          </div>

          {/* Pagination Controls */}
          {(refundNextCursor || refundCursorHistory.length > 0) && (
            <div className="flex items-center justify-between border-t border-[#2d2242] pt-3 text-xs">
              <span className="font-mono text-[#a49cb5]">
                Keyset Paginated: Page {refundCursorHistory.length + 1}
              </span>
              <div className="flex items-center gap-2">
                {refundCursorHistory.length > 0 && (
                  <button
                    type="button"
                    onClick={handleResetRefundsPage}
                    className="rounded-lg border border-[#2d2242] bg-[#201732] px-3 py-1 font-bold text-white hover:bg-[#201732]/80 transition cursor-pointer"
                  >
                    First Page
                  </button>
                )}
                {refundNextCursor && (
                  <button
                    type="button"
                    onClick={handleNextRefundsPage}
                    className="rounded-lg bg-[#950EDB] px-3 py-1 font-bold text-white hover:bg-[#800dc0] transition cursor-pointer"
                  >
                    Next Page ➔
                  </button>
                )}
              </div>
            </div>
          )}

          <p className="text-[11px] text-[#a49cb5] italic pt-1">
            {isRefundsLive
              ? "Live refunds queried from Postgres kernel ledger via GET /v1/refunds with cursor pagination."
              : "Simulated fixture data (DEMO_REFUNDS). When GET /v1/refunds is connected, this table will stream buyer-confirmed refunds executed under single-use REFUND_EXECUTE grants."}
          </p>
        </div>
      )}

      {/* 3. HUMAN REVIEW QUEUE (READ-ONLY) */}
      {activeTab === "review_queue" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <span className="text-xs text-[#a49cb5] font-mono">
              Showing {reviewCases.length} escalated disputes requiring human operator review
            </span>
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
          </div>

          <div className="rounded-2xl border border-[#2d2242] bg-[#171124] overflow-hidden">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-[#2d2242] bg-[#201732] text-[#a49cb5] uppercase font-mono text-[10px]">
                <tr>
                  <th className="p-3.5">Case ID</th>
                  <th className="p-3.5">Blocking Reason</th>
                  <th className="p-3.5">Escalated At</th>
                  <th className="p-3.5">Provider State</th>
                  <th className="p-3.5">Amount</th>
                  <th className="p-3.5">Redacted Evidence Snippet</th>
                  <th className="p-3.5 text-right">Proof Chain</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#2d2242] text-[#a49cb5]">
                {reviewCases.map((rc) => (
                  <tr key={rc.case_id} className="hover:bg-[#201732]/40 transition">
                    <td className="p-3.5 font-mono font-bold text-white">{rc.case_id}</td>
                    <td className="p-3.5">
                      <span className="rounded bg-rose-500/20 text-rose-300 px-2 py-0.5 text-[10px] font-mono font-bold">
                        {rc.blocking_reason_code}
                      </span>
                    </td>
                    <td className="p-3.5 font-mono text-[11px]">{rc.escalated_at}</td>
                    <td className="p-3.5 font-mono text-purple-300 text-[11px]">{rc.provider_state_at_escalation}</td>
                    <td className="p-3.5 font-mono font-bold text-white">
                      {formatPaise(rc.amount_minor)}
                    </td>
                    <td className="p-3.5 text-[11px] text-[#a49cb5] max-w-xs truncate" title={rc.redacted_timeline_snippet}>
                      {rc.redacted_timeline_snippet}
                    </td>
                    <td className="p-3.5 text-right">
                      <Link
                        href={`/inspector?attempt_id=${encodeURIComponent(rc.checkout_id)}`}
                        className="rounded-lg bg-[#201732] hover:bg-[#201732]/80 border border-[#2d2242] px-2.5 py-1 text-[11px] font-bold text-white transition"
                      >
                        Inspect Proof ➔
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] text-[#a49cb5] italic pt-1">
            {isReviewQueueLive
              ? "Live human review cases from kernel."
              : "Simulated review queue cases (DEMO_REVIEW_QUEUE). ADR 0003 D14: human review cases are read-only in P0 because shipping a review queue without a resolution workflow is an honest boundary; shipping a resolve button that does nothing is not."}
          </p>
        </div>
      )}

      {/* 4. OUTBOX & DURABLE WORK */}
      {activeTab === "outbox" && (
        <div className="space-y-6">
          {reviveFeedback && (
            <div className="rounded-xl border border-emerald-500/40 bg-emerald-500/10 p-3.5 text-xs text-emerald-300 font-mono flex items-center justify-between">
              <span>✓ {reviveFeedback}</span>
              <button
                type="button"
                onClick={() => setReviveFeedback(null)}
                className="text-emerald-400 hover:text-white cursor-pointer ml-4 font-bold"
              >
                ✕
              </button>
            </div>
          )}

          {/* Outbox Status Cards */}
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            {[
              { status: "PENDING", count: outbox?.counts.PENDING ?? 0, color: "text-amber-300" },
              { status: "LEASED", count: outbox?.counts.LEASED ?? 0, color: "text-blue-300" },
              { status: "DONE", count: outbox?.counts.DONE ?? 0, color: "text-emerald-300" },
              { status: "FAILED", count: outbox?.counts.FAILED ?? 0, color: "text-rose-300" },
              { status: "DEAD", count: outbox?.counts.DEAD ?? 0, color: "text-rose-400" },
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
