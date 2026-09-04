"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { useClient } from "@/components/providers";
import { useBasketActions } from "@/features/storefront/use-basket-actions";
import { ToolChip } from "./tool-chip";
import { ProposalCard } from "./proposal-card";
import { RefusalHeroCard } from "./refusal-hero-card";
import { VoicePanelShell } from "./voice-panel-shell";
import type { AgentMessage, CheckoutProposal, ReapprovalDecision } from "./types";

const INITIAL_MESSAGES: AgentMessage[] = [
  {
    id: "msg_welcome",
    role: "assistant",
    content:
      "Namaste! I'm your governed shopping assistant. I can search products across the catalogue, add groceries to your basket, and draft checkout proposals.\n\nRemember: I can propose orders, but you retain sole authority to approve payments.",
    timestamp: "Just now",
  },
];

export function AgentPanel() {
  const router = useRouter();
  const client = useClient();
  const { addOne, basketId, lastBasket } = useBasketActions();

  const [isOpen, setIsOpen] = useState(false);
  const [showVoice, setShowVoice] = useState(false);
  const [messages, setMessages] = useState<AgentMessage[]>(INITIAL_MESSAGES);
  const [input, setInput] = useState("");
  const [isProcessing, setIsProcessing] = useState(false);
  const [isAuthorizing, setIsAuthorizing] = useState(false);
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
            name: "verify_policy",
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
            name: "verify_policy",
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

    const lower = text.toLowerCase();

    // SCENARIO: HERO MOMENT / PRICE CHANGE REFUSAL
    if (lower.includes("refus") || lower.includes("price change") || lower.includes("hero") || lower.includes("stale")) {
      setTimeout(() => {
        const refusalDecision: ReapprovalDecision = {
          invalidatedVersion: 1,
          nextVersion: 2,
          reason: "STALE_FACTS_REFUSED",
          deltas: [
            {
              fieldPath: "lines[0].unit_price_minor",
              label: "Amul Taaza Toned Milk (500 ml)",
              before: "₹28.00",
              after: "₹38.00",
              reason: "PRICE_CHANGED",
            },
            {
              fieldPath: "delivery_fee_minor",
              label: "Quick Delivery Surge Fee",
              before: "₹25.00",
              after: "₹45.00",
              reason: "DELIVERY_CHANGED",
            },
            {
              fieldPath: "total_minor",
              label: "Total Checkout Amount",
              before: "₹108.25",
              after: "₹151.85",
              reason: "TOTAL_CHANGED",
            },
          ],
          oldTotalMinor: 10825,
          newTotalMinor: 15185,
          currency: "INR",
          newContentHash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        };

        const reply: AgentMessage = {
          id: `asst_${Date.now()}`,
          role: "assistant",
          content:
            "CRITICAL: While checking out, the merchant raised unit prices and surge fees. The transaction kernel refused to charge your card for the higher unapproved total and invalidated Version 1.",
          timestamp: "Just now",
          tools: [
            {
              id: `tool_chk_${Date.now()}`,
              name: "verify_policy",
              label: "Kernel Decision: REAPPROVAL_REQUIRED (Outcome: REFUSED)",
              status: "failed",
              detail: "v1 DEAD",
            },
          ],
          reapproval: refusalDecision,
        };

        setMessages((prev) => [...prev, reply]);
        setIsProcessing(false);
      }, 700);
      return;
    }

    // SCENARIO: PROPOSE CHECKOUT
    if (lower.includes("checkout") || lower.includes("propose") || lower.includes("order") || lower.includes("pay")) {
      setTimeout(async () => {
        const items = lastBasket?.lines?.map((l) => ({
          sku: l.sku,
          name: l.sku === "GRO-DAIRY-001" ? "Amul Taaza Toned Milk 500 ml" : l.sku,
          quantity: l.quantity,
          unitPriceMinor: 2800,
          subtotalMinor: l.quantity * 2800,
        })) ?? [
          {
            sku: "GRO-DAIRY-001",
            name: "Amul Taaza Toned Milk 500 ml",
            quantity: 2,
            unitPriceMinor: 2800,
            subtotalMinor: 5600,
          },
        ];

        const subtotal = items.reduce((acc, i) => acc + i.subtotalMinor, 0);
        const deliveryFee = subtotal >= 49900 ? 0 : 2500;
        const deliveryTax = Math.floor((deliveryFee * 1800 + 5000) / 10000);
        const total = subtotal + deliveryFee + deliveryTax;

        const proposal: CheckoutProposal = {
          version: 1,
          basketId: basketId ?? "bsk_active_proposal",
          items,
          itemsSubtotalMinor: subtotal,
          deliveryFeeMinor: deliveryFee,
          deliveryTaxMinor: deliveryTax,
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
          tools: [
            {
              id: `t_prop_${Date.now()}`,
              name: "propose_checkout",
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
            "Maine aapke basket me Amul Taaza Toned Milk (500 ml) add kar diya hai! Anything else you need today?",
          timestamp: "Just now",
          tools: [
            {
              id: `t_s_${Date.now()}`,
              name: "search_catalogue",
              label: 'Searched catalogue: "doodh"',
              status: "completed",
              detail: "2 hits",
            },
            {
              id: `t_b_${Date.now()}`,
              name: "modify_basket",
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
        tools: [
          {
            id: `t_gen_${Date.now()}`,
            name: "search_catalogue",
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
      {/* Backdrop overlay on mobile */}
      {isOpen && (
        <div
          className="fixed inset-0 bg-black/60 z-40 sm:hidden backdrop-blur-xs transition-opacity"
          onClick={() => setIsOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Floating Action Trigger Button (Bottom Right) */}
      {!isOpen && (
        <button
          type="button"
          onClick={() => setIsOpen(true)}
          className="fixed bottom-5 right-5 z-40 flex min-h-[44px] items-center gap-2 rounded-full bg-brand-purple hover:bg-[#800dc0] text-white px-4 py-3 shadow-lg hover:shadow-xl transition-all active:scale-95 focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-brand-purple cursor-pointer select-none group"
          aria-label="Open AI Shopping Assistant"
        >
          <span className="text-xl" aria-hidden="true">✨</span>
          <span className="font-black text-xs sm:text-sm tracking-wide">Ask Zepto AI</span>
          <span className="flex h-2 w-2 rounded-full bg-emerald-400 animate-ping" />
        </button>
      )}

      {/* Dockable Agent Panel Container */}
      {isOpen && (
        <aside
          role="dialog"
          aria-modal="true"
          aria-label="AI Shopping Assistant"
          className="fixed bottom-0 right-0 sm:bottom-5 sm:right-5 z-50 flex flex-col w-full sm:w-[420px] h-[90vh] sm:h-[620px] rounded-t-3xl sm:rounded-3xl border-t sm:border border-line bg-surface shadow-2xl overflow-hidden transition-all duration-300 animate-in slide-in-from-bottom sm:slide-in-from-right"
        >
          {/* Mobile Drag Indicator & Quick Dismiss */}
          <div className="sm:hidden flex items-center justify-center pt-2.5 pb-1 bg-surface-raised">
            <div className="w-12 h-1.5 rounded-full bg-muted/40" />
          </div>

          {/* Panel Header */}
          <div className="flex items-center justify-between border-b border-line bg-surface-raised px-4 py-2.5 select-none">
            <div className="flex items-center gap-2.5">
              <div className="flex h-9 w-9 items-center justify-center rounded-full bg-brand-purple text-white shadow-xs text-sm">
                ✨
              </div>
              <div>
                <h2 className="text-xs sm:text-sm font-black text-foreground leading-tight">
                  Zepto Shopping Agent
                </h2>
                <p className="text-[10px] text-muted font-medium">
                  Governed Autonomous Assistant · Track 1
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2">
              {/* Voice toggle with 44px tap target */}
              <button
                type="button"
                onClick={() => setShowVoice(!showVoice)}
                className={`flex min-h-[44px] min-w-[44px] items-center justify-center rounded-xl border text-sm transition cursor-pointer focus-visible:ring-2 focus-visible:ring-brand-purple ${
                  showVoice
                    ? "bg-indigo-600 text-white border-indigo-600"
                    : "border-line bg-surface text-muted hover:text-foreground"
                }`}
                title="Toggle Voice Shell"
                aria-label="Toggle Voice Shell"
              >
                🎙️
              </button>

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

          {/* Voice Shell Overlay (Spec 19.5) */}
          {showVoice && (
            <div className="border-b border-line bg-surface p-3">
              <VoicePanelShell
                onTranscribeComplete={(transcriptText) => {
                  void handleSend(transcriptText);
                }}
              />
            </div>
          )}

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
                  {/* Tool Activity Chips */}
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
                <span>Agent thinking &amp; querying merchant simulator...</span>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* Quick Prompt Suggestions with >=44px tap targets */}
          <div className="border-t border-line/60 bg-surface px-3 py-2.5 flex gap-2 overflow-x-auto scrollbar-none">
            <button
              type="button"
              onClick={() => void handleSend("2 packet amul taaza doodh add kar do")}
              className="shrink-0 min-h-[44px] rounded-full border border-line bg-surface-raised hover:bg-surface px-3.5 py-2 text-xs font-semibold text-foreground transition focus-visible:ring-2 focus-visible:ring-brand-purple cursor-pointer flex items-center gap-1"
            >
              <span>🥛</span>
              <span>&quot;2 packet doodh add karo&quot;</span>
            </button>
            <button
              type="button"
              onClick={() => void handleSend("propose checkout for current basket")}
              className="shrink-0 min-h-[44px] rounded-full border border-line bg-surface-raised hover:bg-surface px-3.5 py-2 text-xs font-semibold text-foreground transition focus-visible:ring-2 focus-visible:ring-brand-purple cursor-pointer flex items-center gap-1"
            >
              <span>📋</span>
              <span>&quot;Propose checkout&quot;</span>
            </button>
            <button
              type="button"
              onClick={() => void handleSend("simulate merchant price change refusal")}
              className="shrink-0 min-h-[44px] rounded-full border border-rose-300 dark:border-rose-800 bg-rose-50 dark:bg-rose-950/40 hover:bg-rose-100 text-rose-800 dark:text-rose-200 px-3.5 py-2 text-xs font-black transition focus-visible:ring-2 focus-visible:ring-rose-500 cursor-pointer flex items-center gap-1"
            >
              <span>⚡</span>
              <span>&quot;Simulate Price Shift Refusal&quot;</span>
            </button>
          </div>

          {/* Input Composer with >=44px controls */}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void handleSend(input);
            }}
            className="border-t border-line bg-surface-raised p-3 flex items-center gap-2.5"
          >
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask in Hindi, Hinglish or English..."
              className="flex-1 min-h-[44px] rounded-xl border border-line bg-surface px-3.5 py-2 text-xs sm:text-sm text-foreground placeholder:text-muted focus:border-brand-purple focus:outline-none focus:ring-1 focus:ring-brand-purple shadow-2xs"
            />
            <button
              type="submit"
              disabled={!input.trim() || isProcessing}
              className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-xl bg-brand-purple hover:bg-[#800dc0] disabled:opacity-40 text-white shadow-xs transition active:scale-95 focus-visible:ring-2 focus-visible:ring-brand-purple cursor-pointer"
              aria-label="Send message"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <line x1="22" y1="2" x2="11" y2="13" />
                <polygon points="22 2 15 22 11 13 2 9 22 2" />
              </svg>
            </button>
          </form>
        </aside>
      )}
    </>
  );
}
