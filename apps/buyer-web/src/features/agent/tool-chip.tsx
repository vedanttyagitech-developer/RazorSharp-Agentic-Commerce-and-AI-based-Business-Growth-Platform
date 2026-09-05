"use client";

import {
  Search,
  Tag,
  Package,
  ShoppingCart,
  Calculator,
  Clock,
  FileText,
  Lock,
  ShieldCheck,
  Truck,
  Scale,
  AlertTriangle,
  Cog,
  Check,
  X,
  type LucideIcon,
} from "lucide-react";

import type { ToolActivity } from "./types";

function getToolIcon(name: string): LucideIcon {
  if (name.includes("search") || name.includes("catalog.search")) return Search;
  if (name.includes("get_product")) return Tag;
  if (name.includes("inventory")) return Package;
  if (name.includes("basket") || name.includes("modify_basket")) return ShoppingCart;
  if (name.includes("quote")) return Calculator;
  if (name.includes("reservation")) return Clock;
  if (name.includes("propose_checkout") || name.includes("submit_for_approval")) return FileText;
  if (name.includes("submit_approved")) return Lock;
  if (name.includes("policy") || name.includes("verify_policy")) return ShieldCheck;
  if (name.includes("track")) return Truck;
  if (name.includes("resolution")) return Scale;
  if (name.includes("escalate")) return AlertTriangle;
  return Cog;
}

export function ToolChip({ tool }: { tool: ToolActivity }) {
  const Icon = getToolIcon(tool.name);

  return (
    <div
      role="status"
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold transition-all shadow-2xs ${
        tool.status === "running"
          ? "border-[#0c831f]/40 bg-[#f7fff9] text-[#0c831f] animate-pulse"
          : tool.status === "completed"
          ? "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300"
          : "border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-800 dark:bg-rose-950/50 dark:text-rose-300"
      }`}
    >
      <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
      <span className="font-mono text-[10px] opacity-80">{tool.name}</span>
      <span className="text-foreground/80 font-normal">|</span>
      <span>{tool.label}</span>
      {tool.status === "running" ? (
        <span className="flex h-1.5 w-1.5 rounded-full bg-[#0c831f] animate-ping" />
      ) : tool.status === "completed" ? (
        <Check className="h-3 w-3 stroke-[2.5] text-emerald-600 dark:text-emerald-400 shrink-0" />
      ) : (
        <X className="h-3 w-3 stroke-[2.5] text-rose-600 dark:text-rose-400 shrink-0" />
      )}
      {tool.detail ? (
        <span className="rounded bg-black/5 dark:bg-white/10 px-1 py-0.2 text-[9px] font-mono text-muted">
          {tool.detail}
        </span>
      ) : null}
    </div>
  );
}
