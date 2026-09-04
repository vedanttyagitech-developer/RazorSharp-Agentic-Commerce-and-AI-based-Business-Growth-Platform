"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import {
  consoleClient,
  formatBasisPoints,
  formatPaise,
  type CatalogueProduct,
} from "@/lib/api";

const PAGE_SIZE = 25;

const CATEGORIES = [
  { id: "ALL", label: "All Categories" },
  { id: "fruits-vegetables", label: "Fruits & Vegetables" },
  { id: "dairy-bread", label: "Dairy & Bread" },
  { id: "staples", label: "Staples" },
  { id: "snacks", label: "Snacks" },
  { id: "beverages", label: "Beverages" },
  { id: "personal-care", label: "Personal Care" },
  { id: "household", label: "Household" },
  { id: "baby", label: "Baby Care" },
];

export default function CataloguePage() {
  const [products, setProducts] = useState<CatalogueProduct[]>(() =>
    consoleClient.getCatalogueProducts()
  );

  const [search, setSearch] = useState("");
  const [selectedCategory, setSelectedCategory] = useState("ALL");
  const [sortBy, setSortBy] = useState<"name" | "price_asc" | "price_desc" | "stock_asc" | "stock_desc">("name");
  const [page, setPage] = useState(1);

  // Scenario Injection State
  const [editingPriceSku, setEditingPriceSku] = useState<string | null>(null);
  const [inputRupees, setInputRupees] = useState<number>(0);
  const [isSubmittingInjection, setIsSubmittingInjection] = useState(false);
  const [notification, setNotification] = useState<{ text: string; type: "success" | "info" } | null>(null);

  const filteredAndSorted = useMemo(() => {
    let result = products;

    // Filter by Category
    if (selectedCategory !== "ALL") {
      result = result.filter((p) => p.category === selectedCategory);
    }

    // Filter by Search Query (English, SKU, Brand, and Hindi synonyms)
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      result = result.filter(
        (p) =>
          p.sku.toLowerCase().includes(q) ||
          p.name.toLowerCase().includes(q) ||
          (p.brand && p.brand.toLowerCase().includes(q)) ||
          (p.synonyms && p.synonyms.some((s) => s.toLowerCase().includes(q)))
      );
    }

    // Sort
    return [...result].sort((a, b) => {
      const priceA = a.current_price_minor ?? a.list_price_minor;
      const priceB = b.current_price_minor ?? b.list_price_minor;
      if (sortBy === "name") return a.name.localeCompare(b.name);
      if (sortBy === "price_asc") return priceA - priceB;
      if (sortBy === "price_desc") return priceB - priceA;
      if (sortBy === "stock_asc") return a.stock_units - b.stock_units;
      if (sortBy === "stock_desc") return b.stock_units - a.stock_units;
      return 0;
    });
  }, [products, search, selectedCategory, sortBy]);

  const totalPages = Math.max(1, Math.ceil(filteredAndSorted.length / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const startIndex = (currentPage - 1) * PAGE_SIZE;
  const pageProducts = filteredAndSorted.slice(startIndex, startIndex + PAGE_SIZE);

  const handleApplyPriceSurge = async (sku: string) => {
    if (inputRupees <= 0) return;
    setIsSubmittingInjection(true);
    const newMinor = Math.round(inputRupees * 100);
    try {
      const injection = await consoleClient.injectScenario("PRICE_SET", sku, newMinor, "Merchant console operator price surge");
      setProducts((prev) =>
        prev.map((p) => (p.sku === sku ? { ...p, current_price_minor: newMinor } : p))
      );
      setEditingPriceSku(null);
      setNotification({
        text: `Scenario Price Surge applied (${injection?.injection_id ?? "live"}): ${sku} price set to ₹${inputRupees.toFixed(2)}. In-flight checkouts will be refused with STALE_APPROVAL_REFUSED.`,
        type: "success",
      });
    } finally {
      setIsSubmittingInjection(false);
      setTimeout(() => setNotification(null), 8000);
    }
  };

  const handleTriggerSellOut = async (sku: string) => {
    setIsSubmittingInjection(true);
    try {
      const injection = await consoleClient.injectScenario("SELL_OUT", sku, undefined, "Operator inventory stockout");
      setProducts((prev) =>
        prev.map((p) => (p.sku === sku ? { ...p, stock_units: 0 } : p))
      );
      setNotification({
        text: `Inventory depleted (${injection?.injection_id ?? "live"}): ${sku} marked Sold Out (0 units). Proposals will encounter STOCK_INSUFFICIENT.`,
        type: "info",
      });
    } finally {
      setIsSubmittingInjection(false);
      setTimeout(() => setNotification(null), 8000);
    }
  };

  const handleToggleDelist = async (sku: string, currentListed: boolean) => {
    setIsSubmittingInjection(true);
    const target = !currentListed;
    try {
      await consoleClient.injectScenario("AVAILABILITY_SET", sku, target, "Operator catalogue availability toggle");
      setProducts((prev) =>
        prev.map((p) => (p.sku === sku ? { ...p, is_listed: target } : p))
      );
      setNotification({
        text: `Catalogue listing updated: ${sku} is now ${target ? "Listed" : "Delisted"}.`,
        type: "info",
      });
    } finally {
      setIsSubmittingInjection(false);
      setTimeout(() => setNotification(null), 8000);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-[#2d2242] pb-6">
        <div>
          <div className="flex items-center gap-2 text-xs text-[#a49cb5]">
            <Link href="/" className="hover:underline">Dashboard</Link>
            <span>/</span>
            <span className="text-[#950EDB] font-bold">Catalogue Operations</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-black text-white tracking-tight mt-1">
            Catalogue Management ({products.length} Products)
          </h1>
          <p className="text-xs sm:text-sm text-[#a49cb5] mt-0.5">
            Grounded Indian quick-commerce catalogue in integer paise. Price and stock shifts execute via <code>POST /v1/scenario/injections</code>.
          </p>
        </div>

        <div className="flex items-center gap-2 rounded-2xl border border-[#2d2242] bg-[#171124] px-4 py-2">
          <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
          <span className="font-mono text-xs text-white font-bold">
            247 Grounded SKUs Active
          </span>
        </div>
      </div>

      {/* Side-by-Side Demonstration Notice Box */}
      <div className="rounded-2xl border border-purple-500/30 bg-purple-950/20 p-4 flex items-center justify-between gap-4">
        <div className="space-y-1 text-xs">
          <strong className="text-white flex items-center gap-1.5">
            <span>⚡</span>
            <span>Side-by-Side Demonstration Lever (Hero Refusal Moment):</span>
          </strong>
          <p className="text-[#a49cb5] leading-relaxed">
            Open the Storefront (<code>http://localhost:3000</code>) beside this console. Add <strong>Amul Taaza Toned Milk (GRO-DAIRY-001)</strong> to your basket and draft a proposal. While waiting on the approval card, click <strong>Surge Price</strong> below to raise the price to ₹38.00. The buyer&apos;s checkout will be refused with before-and-after version deltas.
          </p>
        </div>
      </div>

      {/* Notification Toast */}
      {notification && (
        <div className={`rounded-xl border p-3.5 text-xs font-bold flex items-center gap-2 ${
          notification.type === "success"
            ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
            : "border-purple-500/40 bg-purple-500/10 text-purple-300"
        }`}>
          <span>✓</span>
          <span>{notification.text}</span>
        </div>
      )}

      {/* Filters, Search & Sort Bar */}
      <div className="flex flex-col lg:flex-row gap-3 items-stretch lg:items-center justify-between bg-[#171124] p-4 rounded-2xl border border-[#2d2242]">
        <div className="flex-1 flex flex-col sm:flex-row gap-3">
          <div className="relative flex-1">
            <input
              type="text"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(1);
              }}
              placeholder="Search by SKU, English name, Hindi synonym (दूध, आटा)..."
              className="w-full rounded-xl border border-[#2d2242] bg-[#201732] px-3.5 py-2 text-xs text-white placeholder:text-[#a49cb5] focus:outline-none focus:ring-2 focus:ring-[#950EDB]"
            />
            {search && (
              <button
                type="button"
                onClick={() => setSearch("")}
                className="absolute right-3 top-2 text-xs text-[#a49cb5] hover:text-white"
              >
                ✕
              </button>
            )}
          </div>

          <select
            value={selectedCategory}
            onChange={(e) => {
              setSelectedCategory(e.target.value);
              setPage(1);
            }}
            className="rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-xs text-white focus:outline-none focus:ring-2 focus:ring-[#950EDB]"
          >
            {CATEGORIES.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </select>

          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value as "name" | "price_asc" | "price_desc" | "stock_asc" | "stock_desc")}
            className="rounded-xl border border-[#2d2242] bg-[#201732] px-3 py-2 text-xs text-white focus:outline-none focus:ring-2 focus:ring-[#950EDB]"
          >
            <option value="name">Sort: Name (A-Z)</option>
            <option value="price_asc">Sort: Price (Low ➔ High)</option>
            <option value="price_desc">Sort: Price (High ➔ Low)</option>
            <option value="stock_asc">Sort: Stock (Low ➔ High)</option>
            <option value="stock_desc">Sort: Stock (High ➔ Low)</option>
          </select>
        </div>

        <div className="text-right text-xs font-mono text-[#a49cb5] shrink-0">
          Showing {pageProducts.length} of {filteredAndSorted.length} matches
        </div>
      </div>

      {/* Products Table */}
      <div className="rounded-2xl border border-[#2d2242] bg-[#171124] overflow-hidden shadow-xs">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-[#2d2242] bg-[#201732] text-[#a49cb5] uppercase font-mono text-[10px]">
              <tr>
                <th className="p-3.5">SKU &amp; Brand</th>
                <th className="p-3.5">Product Title</th>
                <th className="p-3.5">Category</th>
                <th className="p-3.5">Pack Unit</th>
                <th className="p-3.5">GST Rate</th>
                <th className="p-3.5">Unit Price</th>
                <th className="p-3.5">Inventory State</th>
                <th className="p-3.5 text-right">Scenario Levers</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#2d2242] text-[#a49cb5]">
              {pageProducts.map((p) => {
                const isEditingPrice = editingPriceSku === p.sku;
                const activePriceMinor = p.current_price_minor ?? p.list_price_minor;
                const isPriceSurged = p.current_price_minor && p.current_price_minor !== p.list_price_minor;

                return (
                  <tr key={p.sku} className="hover:bg-[#201732]/40 transition">
                    <td className="p-3.5">
                      <span className="font-mono font-bold text-white block">{p.sku}</span>
                      <span className="text-[10px] text-[#a49cb5]">{p.brand || "Standard"}</span>
                    </td>
                    <td className="p-3.5 font-bold text-white max-w-xs">{p.name}</td>
                    <td className="p-3.5">
                      <span className="rounded bg-[#201732] border border-[#2d2242] px-2 py-0.5 text-[10px] font-mono text-[#a49cb5]">
                        {p.category}
                      </span>
                    </td>
                    <td className="p-3.5 text-[11px] font-mono">{p.unit}</td>
                    <td className="p-3.5 font-mono text-[11px]">{formatBasisPoints(p.tax_basis_points)}</td>
                    <td className="p-3.5 font-mono">
                      {isEditingPrice ? (
                        <div className="flex items-center gap-1">
                          <span className="text-white">₹</span>
                          <input
                            type="number"
                            step="1"
                            value={inputRupees}
                            onChange={(e) => setInputRupees(Number(e.target.value))}
                            className="w-20 rounded border border-[#950EDB] bg-[#0d0a14] px-1.5 py-0.5 text-white font-mono text-xs"
                          />
                        </div>
                      ) : (
                        <div>
                          <span className={`font-black text-sm block ${isPriceSurged ? "text-emerald-400" : "text-white"}`}>
                            {formatPaise(activePriceMinor)}
                          </span>
                          {isPriceSurged && (
                            <span className="text-[9px] text-[#a49cb5] line-through">
                              List: {formatPaise(p.list_price_minor)}
                            </span>
                          )}
                        </div>
                      )}
                    </td>
                    <td className="p-3.5">
                      <div className="flex items-center gap-2">
                        <span className={`rounded-full px-2 py-0.5 text-[10px] font-mono font-bold ${
                          p.stock_units > 0
                            ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/30"
                            : "bg-rose-500/10 text-rose-400 border border-rose-500/30"
                        }`}>
                          {p.stock_units > 0 ? `${p.stock_units} Units` : "Sold Out"}
                        </span>
                        {!p.is_listed && (
                          <span className="rounded bg-rose-500/20 text-rose-300 px-1.5 py-0.2 text-[9px] font-mono font-bold">
                            DELISTED
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="p-3.5 text-right">
                      {isEditingPrice ? (
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            disabled={isSubmittingInjection}
                            onClick={() => void handleApplyPriceSurge(p.sku)}
                            className="rounded bg-[#950EDB] hover:bg-[#800dc0] text-white px-2 py-1 text-[10px] font-bold cursor-pointer"
                          >
                            Apply
                          </button>
                          <button
                            type="button"
                            onClick={() => setEditingPriceSku(null)}
                            className="rounded bg-[#201732] text-[#a49cb5] hover:text-white px-2 py-1 text-[10px] font-bold cursor-pointer"
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            onClick={() => {
                              setEditingPriceSku(p.sku);
                              setInputRupees(activePriceMinor / 100);
                            }}
                            className="rounded border border-[#2d2242] bg-[#201732] hover:bg-[#201732]/80 text-[#a49cb5] hover:text-white px-2 py-1 text-[10px] font-bold transition cursor-pointer"
                          >
                            Surge
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleTriggerSellOut(p.sku)}
                            className="rounded border border-rose-500/30 bg-rose-500/10 hover:bg-rose-500/20 text-rose-300 px-2 py-1 text-[10px] font-bold transition cursor-pointer"
                            title="Simulate stockout for this item"
                          >
                            Sell Out
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleToggleDelist(p.sku, p.is_listed)}
                            className="rounded border border-[#2d2242] bg-[#201732] hover:bg-[#201732]/80 text-[#a49cb5] hover:text-white px-2 py-1 text-[10px] font-bold transition cursor-pointer"
                            title={p.is_listed ? "Delist this product" : "Relist this product"}
                          >
                            {p.is_listed ? "Delist" : "Relist"}
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* Pagination Bar */}
        <div className="p-4 border-t border-[#2d2242] flex items-center justify-between text-xs text-[#a49cb5]">
          <div>
            Page <strong>{currentPage}</strong> of <strong>{totalPages}</strong> ({filteredAndSorted.length} total products)
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              disabled={currentPage <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              className="rounded-lg border border-[#2d2242] bg-[#201732] px-3 py-1.5 font-bold text-white disabled:opacity-40 transition cursor-pointer"
            >
              Previous
            </button>
            <button
              type="button"
              disabled={currentPage >= totalPages}
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              className="rounded-lg border border-[#2d2242] bg-[#201732] px-3 py-1.5 font-bold text-white disabled:opacity-40 transition cursor-pointer"
            >
              Next
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
