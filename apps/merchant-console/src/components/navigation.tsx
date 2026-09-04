"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { consoleClient, type SafeModeOut } from "@/lib/api";

const NAV_LINKS = [
  { href: "/", label: "Dashboard", icon: "📊" },
  { href: "/evidence", label: "Retained Revenue", icon: "🛡️" },
  { href: "/operations", label: "Operations & Outbox", icon: "⚙️" },
  { href: "/catalogue", label: "Catalogue & Prices", icon: "📦" },
  { href: "/inspector", label: "Protocol Inspector", icon: "🔍" },
  { href: "/onboarding", label: "Merchant Policy", icon: "🏢" },
];

export function Navigation() {
  const pathname = usePathname();
  const [safeMode, setSafeMode] = useState<SafeModeOut | null>(null);

  useEffect(() => {
    let active = true;
    consoleClient.getSafeMode().then((data) => {
      if (active) setSafeMode(data);
    });
    return () => {
      active = false;
    };
  }, [pathname]);

  return (
    <header className="sticky top-0 z-40 border-b border-[#2d2242] bg-[#171124]/95 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-4 sm:px-6 lg:px-8">
        <div className="flex items-center gap-6">
          <Link href="/" className="flex items-center gap-2.5 group">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-[#950EDB] text-white font-black text-base shadow-xs group-hover:scale-105 transition">
              ⚡
            </span>
            <div>
              <span className="block font-black text-sm text-white tracking-tight leading-tight">
                Zepto Merchant Console
              </span>
              <span className="block text-[10px] text-[#a49cb5] font-mono">
                Track 1 · Governed Commerce
              </span>
            </div>
          </Link>

          <nav className="hidden lg:flex items-center gap-1">
            {NAV_LINKS.map((link) => {
              const isActive = pathname === link.href;
              return (
                <Link
                  key={link.href}
                  href={link.href}
                  className={`flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-bold transition ${
                    isActive
                      ? "bg-[#950EDB] text-white shadow-xs"
                      : "text-[#a49cb5] hover:bg-[#201732] hover:text-white"
                  }`}
                >
                  <span>{link.icon}</span>
                  <span>{link.label}</span>
                </Link>
              );
            })}
          </nav>
        </div>

        <div className="flex items-center gap-3">
          {safeMode?.safe_mode ? (
            <Link
              href="/operations"
              className="flex items-center gap-2 rounded-full border border-rose-500/50 bg-rose-500/20 px-3 py-1 text-xs text-rose-300 animate-pulse hover:bg-rose-500/30 transition"
              title="Safe Mode is currently ACTIVE"
            >
              <span className="h-2 w-2 rounded-full bg-rose-400" />
              <span className="font-mono text-[11px] font-bold">SAFE MODE: ACTIVE</span>
            </Link>
          ) : (
            <div className="hidden sm:flex items-center gap-2 rounded-full border border-[#2d2242] bg-[#201732] px-3 py-1 text-xs">
              <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
              <span className="font-mono text-[11px] text-[#a49cb5]">Kernel Lock: NORMAL</span>
            </div>
          )}

          <span className="rounded-lg bg-emerald-500/10 border border-emerald-500/30 px-2.5 py-1 text-[11px] font-bold text-emerald-400">
            Tenant: demo-grocery
          </span>
        </div>
      </div>

      {/* Secondary mobile navigation bar for viewports below lg */}
      <div className="flex lg:hidden overflow-x-auto border-t border-[#2d2242] px-4 py-2 gap-1 scrollbar-none">
        {NAV_LINKS.map((link) => {
          const isActive = pathname === link.href;
          return (
            <Link
              key={link.href}
              href={link.href}
              className={`shrink-0 flex items-center gap-1 rounded-lg px-2.5 py-1 text-[11px] font-bold ${
                isActive ? "bg-[#950EDB] text-white" : "text-[#a49cb5]"
              }`}
            >
              <span>{link.icon}</span>
              <span>{link.label}</span>
            </Link>
          );
        })}
      </div>
    </header>
  );
}
