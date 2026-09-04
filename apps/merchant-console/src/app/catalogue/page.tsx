"use client";

import { useState } from "react";
import { injectPriceSurge } from "@/lib/api";

interface ProductRow {
  sku: string;
  name: string;
  category: string;
  unitLabel: string;
  pricePaise: number;
  stock: number;
  isAvailable: boolean;
}

const SAMPLE_PRODUCTS: ProductRow[] = [
  { sku: "GRO-DAIRY-001", name: "Amul Taaza Toned Milk 500 ml", category: "dairy", unitLabel: "500 ml", pricePaise: 2800, stock: 48, isAvailable: true },
  { sku: "GRO-DAIRY-002", name: "Amul Gold Full Cream Milk 1 L", category: "dairy", unitLabel: "1 L", pricePaise: 7300, stock: 30, isAvailable: true },
  { sku: "GRO-DAIRY-003", name: "Amul Masti Dahi 400 g", category: "dairy", unitLabel: "400 g", pricePaise: 4500, stock: 24, isAvailable: true },
  { sku: "GRO-DAIRY-004", name: "Amul Malai Paneer Block 200 g", category: "dairy", unitLabel: "200 g", pricePaise: 9500, stock: 0, isAvailable: false },
  { sku: "GRO-STPL-001", name: "India Gate Classic Basmati Rice 5 kg", category: "staples", unitLabel: "5 kg", pricePaise: 49900, stock: 10, isAvailable: true },
  { sku: "GRO-STPL-002", name: "Aashirvaad Shudh Chakki Atta 5 kg", category: "staples", unitLabel: "5 kg", pricePaise: 25500, stock: 18, isAvailable: true },
  { sku: "OIL-SUN-001", name: "Freedom Refined Sunflower Oil 1 L", category: "staples", unitLabel: "1 L", pricePaise: 17900, stock: 40, isAvailable: true },
  { sku: "OIL-MUS-001", name: "Fortune Kachi Ghani Mustard Oil 1 L", category: "staples", unitLabel: "1 L", pricePaise: 20700, stock: 35, isAvailable: true },
  { sku: "GRO-PROD-001", name: "Onion (Pyaz) 1 kg", category: "produce", unitLabel: "1 kg", pricePaise: 4200, stock: 35, isAvailable: true },
  { sku: "GRO-PROD-002", name: "Tomato (Tamatar) 1 kg", category: "produce", unitLabel: "1 kg", pricePaise: 3800, stock: 28, isAvailable: true },
  { sku: "GRO-SNCK-001", name: "Maggi 2-Minute Masala Noodles", category: "snacks", unitLabel: "4 x 70 g", pricePaise: 9600, stock: 26, isAvailable: true },
  { sku: "GRO-BEVG-001", name: "Tata Tea Gold 500 g", category: "beverages", unitLabel: "500 g", pricePaise: 28500, stock: 16, isAvailable: true },
  { sku: "ELEC-IPHONE-16", name: "Apple iPhone 17 Pro 256 GB", category: "electronics", unitLabel: "1 pc", pricePaise: 12689900, stock: 12, isAvailable: true },
];

export default function CataloguePage() {
  const [products, setProducts] = useState<ProductRow[]>(SAMPLE_PRODUCTS);
  const [editingSku, setEditingSku] = useState<string | null>(null);
  const [newPrice, setNewPrice] = useState<number>(0);
  const [notification, setNotification] = useState<string | null>(null);

  const handleUpdatePrice = async (sku: string) => {
    setProducts((prev) =>
      prev.map((p) => (p.sku === sku ? { ...p, pricePaise: newPrice * 100 } : p))
    );
    setEditingSku(null);
    try {
      const injection = await injectPriceSurge(sku, newPrice * 100);
      if (injection) {
        setNotification(`Live scenario injection applied (${injection.injection_id}): SKU ${sku} surged to ₹${newPrice}.00. Catalogue rev #${injection.new_catalogue_revision ?? "live"}.`);
      } else {
        setNotification(`Updated price for ${sku} to ₹${newPrice}.00. (Local demo mode; connects live on http://localhost:8000).`);
      }
    } catch {
      setNotification(`Updated price for ${sku} to ₹${newPrice}.00 (local mode).`);
    }
    setTimeout(() => setNotification(null), 5000);
  };

  const toggleStock = (sku: string) => {
    setProducts((prev) =>
      prev.map((p) =>
        p.sku === sku
          ? { ...p, isAvailable: !p.isAvailable, stock: p.isAvailable ? 0 : 20 }
          : p
      )
    );
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl sm:text-3xl font-black text-white tracking-tight">
            Catalogue &amp; Real-time Pricing Engine
          </h1>
          <p className="text-xs sm:text-sm text-[#a49cb5]">
            Adjust prices or toggle inventory to simulate live quick-commerce operations and trigger kernel refusal decisions.
          </p>
        </div>

        <span className="rounded-xl border border-[#2d2242] bg-[#171124] px-3 py-1.5 font-mono text-xs text-[#a49cb5]">
          Catalogue Revision: #14 (LIVE)
        </span>
      </div>

      {notification && (
        <div className="rounded-2xl border border-emerald-500/40 bg-emerald-500/10 p-3.5 text-xs font-bold text-emerald-300 flex items-center gap-2">
          <span>✓</span>
          <span>{notification}</span>
        </div>
      )}

      {/* Catalogue Table */}
      <div className="rounded-2xl border border-[#2d2242] bg-[#171124] shadow-xs overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-[#2d2242] bg-[#201732] text-[11px] font-bold text-[#a49cb5] uppercase tracking-wider">
              <tr>
                <th className="py-3 px-4">SKU</th>
                <th className="py-3 px-4">Product Name</th>
                <th className="py-3 px-4">Category</th>
                <th className="py-3 px-4">Pack Size</th>
                <th className="py-3 px-4">Unit Price (INR)</th>
                <th className="py-3 px-4">Inventory</th>
                <th className="py-3 px-4 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#2d2242]">
              {products.map((p) => {
                const isEditing = editingSku === p.sku;
                return (
                  <tr key={p.sku} className="hover:bg-[#201732]/40 transition">
                    <td className="py-3 px-4 font-mono text-[#a49cb5] text-[11px]">{p.sku}</td>
                    <td className="py-3 px-4 font-bold text-white">{p.name}</td>
                    <td className="py-3 px-4">
                      <span className="rounded-md bg-[#201732] px-2 py-0.5 text-[10px] uppercase font-bold text-[#a49cb5]">
                        {p.category}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-[#a49cb5]">{p.unitLabel}</td>
                    <td className="py-3 px-4 font-mono font-bold text-white tabular-nums">
                      {isEditing ? (
                        <div className="flex items-center gap-1">
                          <span>₹</span>
                          <input
                            type="number"
                            value={newPrice}
                            onChange={(e) => setNewPrice(Number(e.target.value))}
                            className="w-20 rounded border border-[#950EDB] bg-[#0d0a14] px-1.5 py-0.5 text-white font-mono text-xs"
                          />
                        </div>
                      ) : (
                        `₹${(p.pricePaise / 100).toFixed(2)}`
                      )}
                    </td>
                    <td className="py-3 px-4">
                      <button
                        type="button"
                        onClick={() => toggleStock(p.sku)}
                        className={`rounded-full px-2.5 py-0.5 text-[10px] font-bold cursor-pointer transition ${
                          p.isAvailable
                            ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/30"
                            : "bg-rose-500/10 text-rose-400 border border-rose-500/30"
                        }`}
                      >
                        {p.isAvailable ? `In Stock (${p.stock})` : "Sold Out"}
                      </button>
                    </td>
                    <td className="py-3 px-4 text-right">
                      {isEditing ? (
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            onClick={() => handleUpdatePrice(p.sku)}
                            className="rounded bg-[#950EDB] hover:bg-[#800dc0] text-white px-2 py-1 text-[10px] font-bold cursor-pointer"
                          >
                            Save
                          </button>
                          <button
                            type="button"
                            onClick={() => setEditingSku(null)}
                            className="rounded bg-[#201732] text-[#a49cb5] hover:text-white px-2 py-1 text-[10px] font-bold cursor-pointer"
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <button
                          type="button"
                          onClick={() => {
                            setEditingSku(p.sku);
                            setNewPrice(p.pricePaise / 100);
                          }}
                          className="rounded border border-[#2d2242] bg-[#201732] hover:bg-[#2b1f44] text-[#a49cb5] hover:text-white px-2 py-1 text-[10px] font-bold transition cursor-pointer"
                        >
                          Surge Price
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
