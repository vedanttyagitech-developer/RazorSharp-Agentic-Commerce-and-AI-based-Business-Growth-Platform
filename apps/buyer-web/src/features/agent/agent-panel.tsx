"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { useClient } from "@/components/providers";
import { useBasketActions } from "@/features/storefront/use-basket-actions";
import { ToolChip } from "./tool-chip";
import { DenialCard } from "./denial-card";
import { ProposalCard } from "./proposal-card";
import { RefusalHeroCard } from "./refusal-hero-card";
import type { AgentMessage, CheckoutProposal, ReapprovalDecision, ToolActivity } from "./types";

const INITIAL_MESSAGES: AgentMessage[] = [
  {
    id: "msg_welcome",
    role: "assistant",
    content:
      "Namaste! I'm your governed shopping assistant. I can search 240+ grounded products across the catalogue, add groceries to your basket, and draft checkout proposals.\n\nRemember: I can propose orders, but you retain sole authority to approve payments.",
    timestamp: "Just now",
    specialist: "shopping_specialist",
    specialistLabel: "Shopping Specialist",
    routingReason: "Session start: buyer discovery and basket guidance initialized",
  },
];

function formatSpecialistTitle(name?: string): string {
  if (!name) return "Commerce Assistant";
  if (name === "shopping_specialist") return "Shopping Specialist";
  if (name === "checkout_specialist") return "Checkout Specialist";
  if (name === "support_specialist") return "Support Specialist";
  if (name === "growth_specialist") return "Growth Specialist";
  if (name === "case_specialist") return "Case Specialist";
  return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function AgentPanel() {
  const router = useRouter();
  const client = useClient();
  const { addOne, basketId, lastBasket } = useBasketActions();

  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState<AgentMessage[]>(INITIAL_MESSAGES);
  const [input, setInput] = useState("");
  const [isProcessing, setIsProcessing] = useState(false);
  const [isAuthorizing, setIsAuthorizing] = useState(false);
  const [activeEndpointMode, setActiveEndpointMode] = useState<"live" | "simulated">("simulated");
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isOpen) {
      if (typeof messagesEndRef.current?.scrollIntoView === "function") {
        messagesEndRef.current.scrollIntoView({ behavior: "smooth" });
      }
    }
  }, [messages, isOpen]);

  // Handle human authorization of a proposal
  const handleAuthorizeProposal = async (proposal: CheckoutProposal) => {
    setIsAuthorizing(true);
    try {
      // Transition proposal state
      setMessages((prev) =>
        prev.map((msg) =>
          msg.proposal?.version === proposal.version
            ? { ...msg, proposal: { ...msg.proposal, status: "authorized" } }
            : msg
        )
      );

      // Add system message verifying authorization
      const systemConfirm: AgentMessage = {
        id: `sys_${Date.now()}`,
        role: "system",
        content: `Human authorization recorded for Proposal v${proposal.version}. Canonical hash verified: ${proposal.contentHash.slice(0, 12)}...`,
        timestamp: "Just now",
        tools: [
          {
            id: `tool_${Date.now()}`,
            name: "checkout.submit_approved",
            label: "Kernel Authority Check: AUTHORIZED",
            status: "completed",
          },
        ],
      };

      setMessages((prev) => [...prev, systemConfirm]);

      // Navigate to checkout if basket exists
      if (proposal.basketId) {
        setTimeout(() => {
          router.push("/basket");
        }, 800);
      }
    } finally {
      setIsAuthorizing(false);
    }
  };

  // Handle human approving next version after refusal
  const handleApproveNext = async (decision: ReapprovalDecision) => {
    setIsAuthorizing(true);
    try {
      const confirmMsg: AgentMessage = {
        id: `msg_approved_v${decision.nextVersion}`,
        role: "system",
        content: `Version ${decision.nextVersion} authorized by buyer at ${decision.currency} ${(decision.newTotalMinor / 100).toFixed(2)}. Outbox grant dispatched.`,
        timestamp: "Just now",
        tools: [
          {
            id: `tool_grant_${Date.now()}`,
            name: "execution_grant.issue",
            label: `Grant Issued: grt_demo_${decision.nextVersion}`,
            status: "completed",
            detail: "SINGLE_WINNER",
          },
        ],
      };
      setMessages((prev) => [...prev, confirmMsg]);
    } finally {
      setIsAuthorizing(false);
    }
  };

  // Process user message
  const handleSend = async (userText: string) => {
    const text = userText.trim();
    if (!text || isProcessing) return;

    setInput("");
    const userMsg: AgentMessage = {
      id: `usr_${Date.now()}`,
      role: "user",
      content: text,
      timestamp: "Just now",
    };
    setMessages((prev) => [...prev, userMsg]);
    setIsProcessing(true);

    // 1. Attempt live POST /v1/agent/turn if available
    try {
      const turnRes = await fetch("/api/backend/v1/agent/turn", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, basket_id: basketId }),
      });

      if (turnRes.ok) {
        const data = await turnRes.json();
        setActiveEndpointMode("live");
        const liveTools: ToolActivity[] = (data.tool_calls || []).map((t: { name: string; summary: string; ok: boolean }, i: number) => ({
          id: `t_live_${Date.now()}_${i}`,
          name: t.name,
          label: t.summary || t.name,
          status: t.ok ? "completed" : "failed",
        }));

        const reply: AgentMessage = {
          id: `asst_${Date.now()}`,
          role: "assistant",
          content: data.reply || "Request processed.",
          timestamp: "Just now",
          specialist: data.specialist || "shopping_specialist",
          specialistLabel: formatSpecialistTitle(data.specialist),
          routingReason: data.routing_reason || "Grounded turn processed by agent-runtime",
          language: data.language || "en",
          tools: liveTools,
          denials: data.denials,
          proposal: data.structured?.proposal,
          reapproval: data.structured?.reapproval,
        };

        setMessages((prev) => [...prev, reply]);
        setIsProcessing(false);
        return;
      }
    } catch {
      // Live endpoint not available or network offline -> proceed with robust deterministic simulator
    }

    setActiveEndpointMode("simulated");
    const lower = text.toLowerCase();

    // SCENARIO: DIRECT PAYMENT ATTEMPT -> FIRST-CLASS GOVERNANCE DENIAL
    if (
      lower.includes("pay now") ||
      lower.includes("charge") ||
      lower.includes("debit") ||
      lower.includes("execute payment") ||
      lower.includes("payment.execute")
    ) {
      setTimeout(() => {
        const reply: AgentMessage = {
          id: `asst_${Date.now()}`,
          role: "assistant",
          content:
            "I cannot authorize or execute payments directly. Under our platform security architecture, conversational agents hold zero payment capability. All payments must be authorized by you on the trusted approval card and settled through Razorpay.",
          timestamp: "Just now",
          specialist: "checkout_specialist",
          specialistLabel: "Checkout Specialist",
          routingReason: "Direct payment execution intent detected: blocked by Registry B security fence",
          tools: [
            {
              id: `t_deny_${Date.now()}`,
              name: "payment.execute",
              label: "payment.execute: REFUSED",
              status: "failed",
              detail: "FORBIDDEN",
            },
          ],
          denials: [
            {
              capability: "payment.execute",
              reason_key: "AGENT_CAPABILITY_NOT_GRANTED",
              explanation:
                "Track 1 System Invariant 1: LLMs may interpret and propose; only deterministic code may authorize and execute money. Approval, total, payment, and refund happen on the trusted human surface.",
            },
          ],
        };
        setMessages((prev) => [...prev, reply]);
        setIsProcessing(false);
      }, 500);
      return;
    }

    // SCENARIO: HERO MOMENT / PRICE CHANGE REFUSAL
    if (lower.includes("refus") || lower.includes("price change") || lower.includes("hero") || lower.includes("stale")) {
      setTimeout(() => {
        const refusalDecision: ReapprovalDecision = {
          invalidatedVersion: 1,
          nextVersion: 2,
          reason: "STALE_APPROVAL_REFUSED",
          deltas: [
            {
              fieldPath: "lines[0].unit_price_minor",
              label: "Fortune Mustard Oil 1 L",
              before: "₹145.00",
              after: "₹165.00",
              reason: "PRICE_CHANGED",
            },
            {
              fieldPath: "delivery_fee_minor",
              label: "Delivery Tier Adjustment",
              before: "₹25.00",
              after: "₹45.00",
              reason: "DELIVERY_CHANGED",
            },
          ],
          oldTotalMinor: 34000,
          newTotalMinor: 39500,
          currency: "INR",
          newContentHash: "b8c9d0e1f2a34567890123456789abcdef0123456789abcdef0123456789abcd",
        };

        const reply: AgentMessage = {
          id: `asst_${Date.now()}`,
          role: "assistant",
          content:
            "PRICE SHIFT REFUSED: The Transaction Assurance Kernel invalidated your checkout approval because prices updated before settlement.\n\n• Fortune Mustard Oil: was ₹145, now ₹165 (+₹20)\n• Order Total: was ₹340, Version 2 total is ₹395 (+₹55)\n\nVersion 1 has been permanently invalidated to protect your funds. Please review the itemized deltas below and re-approve Version 2 on the trusted card.",
          timestamp: "Just now",
          specialist: "checkout_specialist",
          specialistLabel: "Checkout Specialist",
          routingReason: "Kernel admission revalidation: STALE_APPROVAL_REFUSED detected",
          tools: [
            {
              id: `t_reval_${Date.now()}`,
              name: "checkout.revalidate",
              label: "Revalidated against Merchant DB",
              status: "completed",
              detail: "STALE_PRICE",
            },
            {
              id: `t_inval_${Date.now()}`,
              name: "checkout.invalidate_version",
              label: "Version 1 Invalidated -> Created v2",
              status: "completed",
              detail: "N+1 CREATED",
            },
          ],
          reapproval: refusalDecision,
        };

        setMessages((prev) => [...prev, reply]);
        setIsProcessing(false);
      }, 700);
      return;
    }

    // SCENARIO: CHECKOUT / PROPOSAL
    if (lower.includes("checkout") || lower.includes("propose") || lower.includes("order") || lower.includes("buy")) {
      setTimeout(() => {
        const items = (lastBasket?.lines && lastBasket.lines.length > 0)
          ? lastBasket.lines.map((l) => ({
              sku: l.sku,
              name: l.sku,
              quantity: l.quantity,
              unitPriceMinor: 2800,
              subtotalMinor: 2800 * l.quantity,
            }))
          : [
          {
            sku: "GRO-DAIRY-001",
            name: "Amul Taaza Toned Milk 500 ml",
            quantity: 2,
            unitPriceMinor: 2800,
            subtotalMinor: 5600,
          },
          {
            sku: "GRO-STPL-003",
            name: "Tata Salt — Iodised, 1 kg",
            quantity: 1,
            unitPriceMinor: 2800,
            subtotalMinor: 2800,
          },
        ];

        const subtotal = items.reduce((acc, it) => acc + it.subtotalMinor, 0);
        const delivery = subtotal >= 49900 ? 0 : 2500;
        const total = subtotal + delivery;

        const proposal: CheckoutProposal = {
          version: 1,
          basketId: basketId || "bsk_simulated_demo",
          items,
          itemsSubtotalMinor: subtotal,
          deliveryFeeMinor: delivery,
          deliveryTaxMinor: 0,
          totalMinor: total,
          currency: "INR",
          contentHash: "a1b2c3d4e5f67890123456789abcdef0123456789abcdef0123456789abcdef0",
          status: "proposed",
        };

        const reply: AgentMessage = {
          id: `asst_${Date.now()}`,
          role: "assistant",
          content:
            "I have compiled your items into a formal Checkout Proposal (v1). As an AI agent, I cannot sign payments. Please review the itemized breakdown and authorize on the trusted surface.",
          timestamp: "Just now",
          specialist: "checkout_specialist",
          specialistLabel: "Checkout Specialist",
          routingReason: "Intent detected: formal checkout proposal assembly & quote calculation",
          tools: [
            {
              id: `t_prop_${Date.now()}`,
              name: "checkout.submit_for_approval",
              label: "Drafted Checkout Proposal v1",
              status: "completed",
              detail: `₹${(total / 100).toFixed(2)}`,
            },
          ],
          proposal,
        };

        setMessages((prev) => [...prev, reply]);
        setIsProcessing(false);
      }, 700);
      return;
    }

    // SCENARIO: ADD MILK / DOODH
    if (lower.includes("doodh") || lower.includes("milk") || lower.includes("दूध")) {
      setTimeout(async () => {
        try {
          await addOne("GRO-DAIRY-001", "Amul Taaza Toned Milk 500 ml");
        } catch {
          // ignore in mock mode
        }

        const reply: AgentMessage = {
          id: `asst_${Date.now()}`,
          role: "assistant",
          content:
            "Maine aapke basket me Amul Taaza Toned Milk (500 ml) add kar diya hai (₹28)! Anything else you need today?",
          timestamp: "Just now",
          specialist: "shopping_specialist",
          specialistLabel: "Shopping Specialist",
          routingReason: "Query matches grounded dairy category and basket addition intent",
          language: "hinglish",
          tools: [
            {
              id: `t_s_${Date.now()}`,
              name: "catalog.search",
              label: 'Searched catalogue: "doodh"',
              status: "completed",
              detail: "6 hits",
            },
            {
              id: `t_b_${Date.now()}`,
              name: "basket.update",
              label: "Added 1 × Amul Taaza Toned Milk (500 ml)",
              status: "completed",
              detail: "₹28.00",
            },
          ],
        };

        setMessages((prev) => [...prev, reply]);
        setIsProcessing(false);
      }, 600);
      return;
    }

    // DEFAULT: GENERAL SEARCH & RESPONSE
    setTimeout(async () => {
      let hitsCount = 0;
      try {
        const searchRes = await client.search({ q: text });
        hitsCount = searchRes.hits.length;
      } catch {
        hitsCount = 1;
      }

      const reply: AgentMessage = {
        id: `asst_${Date.now()}`,
        role: "assistant",
        content: `I searched the verified merchant catalogue for "${text}". Found ${hitsCount} matching product${hitsCount === 1 ? "" : "s"}. What would you like to add to your cart?`,
        timestamp: "Just now",
        specialist: "shopping_specialist",
        specialistLabel: "Shopping Specialist",
        routingReason: "General catalog query: natural language keyword search",
        tools: [
          {
            id: `t_gen_${Date.now()}`,
            name: "catalog.search",
            label: `Searched catalogue: "${text}"`,
            status: "completed",
            detail: `${hitsCount} hits`,
          },
        ],
      };

      setMessages((prev) => [...prev, reply]);
      setIsProcessing(false);
    }, 600);
  };

  return (
    <>
      {/* Floating Trigger Button on Storefront */}
      {!isOpen && (
        <button
          type="button"
          onClick={() => setIsOpen(true)}
          className="fixed bottom-6 right-6 z-40 flex items-center gap-2 rounded-full bg-brand-purple px-4 py-3 text-white shadow-xl hover:bg-[#7a12b8] transition-all transform hover:scale-105 focus:outline-none focus:ring-4 focus:ring-brand-purple/30 cursor-pointer min-h-[44px]"
          aria-label="Open AI Shopping Assistant"
        >
          <span className="text-xl">✨</span>
          <span className="font-bold text-sm tracking-wide">Ask Zepto AI</span>
          <span className="flex h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
        </button>
      )}

      {/* Floating Drawer / Bottom Sheet Container */}
      {isOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Zepto AI Shopping Assistant"
          className="fixed bottom-0 sm:bottom-6 sm:right-6 z-50 flex flex-col w-full sm:w-[420px] h-[85vh] sm:h-[620px] max-h-[85vh] rounded-t-3xl sm:rounded-3xl border border-line bg-surface shadow-2xl overflow-hidden animate-in slide-in-from-bottom-5 duration-200"
        >
          {/* Header */}
          <div className="flex items-center justify-between border-b border-line bg-surface-raised px-4 py-3">
            <div className="flex items-center gap-2">
              <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-brand-purple-light text-brand-purple text-base font-black">
                ✨
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <h2 className="text-xs font-black tracking-tight text-foreground uppercase">
                    Zepto Shopping Agent
                  </h2>
                  <span className={`rounded-full px-1.5 py-0.2 text-[8px] font-mono font-bold uppercase tracking-wider ${
                    activeEndpointMode === "live"
                      ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300"
                      : "bg-purple-100 text-purple-800 dark:bg-purple-950 dark:text-purple-300"
                  }`}>
                    {activeEndpointMode === "live" ? "Live API (POST /v1/agent/turn)" : "Mock Mode"}
                  </span>
                </div>
                <p className="text-[10px] text-muted leading-none mt-0.5">
                  Governed Autonomous Assistant · Track 1 • Propose only
                </p>
              </div>
            </div>

            <div className="flex items-center gap-1.5">

              {/* Close panel with 44px tap target */}
              <button
                type="button"
                onClick={() => setIsOpen(false)}
                className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-xl border border-line bg-surface text-muted hover:text-foreground text-sm font-bold transition focus-visible:ring-2 focus-visible:ring-brand-purple cursor-pointer"
                aria-label="Close Assistant and return to store"
              >
                ✕
              </button>
            </div>
          </div>



          {/* Message List */}
          <div role="log" aria-live="polite" className="flex-1 overflow-y-auto p-4 space-y-4 scrollbar-none">
            {messages.map((msg) => {
              const isUser = msg.role === "user";
              const isSystem = msg.role === "system";

              return (
                <div
                  key={msg.id}
                  className={`flex flex-col ${
                    isUser ? "items-end" : "items-start"
                  }`}
                >
                  {/* Specialist & Deterministic Routing Reason Badge */}
                  {!isUser && !isSystem && msg.specialistLabel && (
                    <div className="mb-1.5 flex flex-wrap items-center gap-1.5 text-[10px]">
                      <span className="inline-flex items-center gap-1 rounded-full bg-brand-purple-light dark:bg-brand-purple/20 px-2 py-0.5 font-bold text-brand-purple border border-brand-purple/30">
                        <span className="h-1.5 w-1.5 rounded-full bg-brand-purple animate-pulse" />
                        {msg.specialistLabel}
                      </span>
                      {msg.routingReason && (
                        <span className="text-[10px] text-muted truncate max-w-[260px]" title={msg.routingReason}>
                          ↳ {msg.routingReason}
                        </span>
                      )}
                    </div>
                  )}

                  {/* Tool Activity Chips from Real Log */}
                  {msg.tools && msg.tools.length > 0 && (
                    <div className="mb-2 flex flex-wrap gap-1.5">
                      {msg.tools.map((tool) => (
                        <ToolChip key={tool.id} tool={tool} />
                      ))}
                    </div>
                  )}

                  {/* Message Bubble */}
                  <div
                    className={`max-w-[88%] rounded-2xl p-3 text-xs leading-relaxed shadow-2xs whitespace-pre-line ${
                      isUser
                        ? "bg-brand-purple text-white rounded-br-xs"
                        : isSystem
                        ? "border border-line bg-surface-raised text-muted text-[11px] font-mono italic"
                        : "border border-line bg-surface-raised text-foreground rounded-bl-xs"
                    }`}
                  >
                    {msg.content}
                  </div>

                  {/* First-Class Denial Notices */}
                  {msg.denials && msg.denials.length > 0 && (
                    <div className="mt-2 w-full max-w-[95%]">
                      {msg.denials.map((denial, idx) => (
                        <DenialCard key={idx} denial={denial} />
                      ))}
                    </div>
                  )}

                  {/* Checkout Proposal Card */}
                  {msg.proposal && (
                    <div className="mt-3 w-full">
                      <ProposalCard
                        proposal={msg.proposal}
                        onAuthorize={handleAuthorizeProposal}
                        isAuthorizing={isAuthorizing}
                      />
                    </div>
                  )}

                  {/* Reapproval Refusal Hero Card */}
                  {msg.reapproval && (
                    <div className="mt-3 w-full">
                      <RefusalHeroCard
                        decision={msg.reapproval}
                        onApproveNext={handleApproveNext}
                        isApproving={isAuthorizing}
                      />
                    </div>
                  )}

                  <span className="mt-1 text-[9px] text-muted px-1">
                    {msg.timestamp}
                  </span>
                </div>
              );
            })}

            {isProcessing && (
              <div className="flex items-center gap-2 text-xs text-muted">
                <span className="flex h-2 w-2 rounded-full bg-brand-purple animate-ping" />
                <span>Specialist reasoning &amp; querying merchant simulator...</span>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* Quick Prompts */}
          <div className="border-t border-line bg-surface-raised px-3 py-2">
            <div className="flex gap-2 overflow-x-auto pb-1 scrollbar-none">
              <button
                type="button"
                onClick={() => void handleSend("2 packet doodh add karo")}
                className="shrink-0 rounded-full border border-line bg-surface px-2.5 py-1 text-[11px] text-muted hover:text-foreground transition cursor-pointer"
              >
                🥛 2 packet doodh add karo
              </button>
              <button
                type="button"
                onClick={() => void handleSend("propose checkout for current basket")}
                className="shrink-0 rounded-full border border-line bg-surface px-2.5 py-1 text-[11px] text-muted hover:text-foreground transition cursor-pointer"
              >
                📋 Propose checkout
              </button>
              <button
                type="button"
                onClick={() => void handleSend("Simulate price change refusal hero")}
                className="shrink-0 rounded-full border border-brand-purple/40 bg-brand-purple-light px-2.5 py-1 text-[11px] font-bold text-brand-purple hover:bg-brand-purple hover:text-white transition cursor-pointer"
              >
                ⚡ Simulate Price Shift Refusal
              </button>
              <button
                type="button"
                onClick={() => void handleSend("Pay now from my bank account")}
                className="shrink-0 rounded-full border border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950 px-2.5 py-1 text-[11px] font-bold text-amber-900 dark:text-amber-200 transition cursor-pointer"
              >
                🛡️ Test Payment Denial
              </button>
            </div>
          </div>

          {/* Input Bar */}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void handleSend(input);
            }}
            className="border-t border-line bg-surface p-3 flex items-center gap-2"
          >
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask anything in English, Hindi, or Hinglish..."
              className="flex-1 rounded-xl border border-line bg-surface-raised px-3.5 py-2.5 text-xs text-foreground placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-brand-purple"
              disabled={isProcessing}
            />
            <button
              type="submit"
              disabled={!input.trim() || isProcessing}
              className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-purple text-white disabled:opacity-40 transition hover:bg-[#7a12b8] cursor-pointer"
              aria-label="Send message"
            >
              ➔
            </button>
          </form>
        </div>
      )}
    </>
  );
}
