"use client";

import type { ToolActivity } from "./types";

const ICONS: Record<ToolActivity["name"], string> = {
  search_catalogue: "🔍",
  modify_basket: "🛒",
  propose_checkout: "📋",
  verify_policy: "🛡️",
};

export function ToolChip({ tool }: { tool: ToolActivity }) {
  const icon = ICONS[tool.name] ?? "⚙️";

  return (
    <div
      role="status"
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold transition-all shadow-2xs ${
        tool.status === "running"
          ? "border-brand-purple/40 bg-brand-purple-light text-brand-purple animate-pulse"
          : tool.status === "completed"
          ? "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300"
          : "border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-800 dark:bg-rose-950/50 dark:text-rose-300"
      }`}
    >
      <span aria-hidden="true" className="text-xs">
        {icon}
      </span>
      <span>{tool.label}</span>
      {tool.status === "running" ? (
        <span className="flex h-1.5 w-1.5 rounded-full bg-brand-purple animate-ping" />
      ) : tool.status === "completed" ? (
        <span className="text-[10px] font-bold text-emerald-600 dark:text-emerald-400">✓</span>
      ) : (
        <span className="text-[10px] font-bold text-rose-600 dark:text-rose-400">✕</span>
      )}
      {tool.detail ? (
        <span className="rounded bg-black/5 dark:bg-white/10 px-1 py-0.2 text-[9px] font-mono text-muted">
          {tool.detail}
        </span>
      ) : null}
    </div>
  );
}
