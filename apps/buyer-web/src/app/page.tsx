/**
 * The front door is the copilot.
 *
 * This route used to be a storefront -- banners, aisles, a grid of twenty products -- with
 * the assistant as a box floating over it. That had the relationship backwards. What this
 * project is demonstrating is a shop you can talk to, where an agent proposes and a
 * deterministic kernel authorizes, and the interesting surface is the conversation. A
 * shelf is still the fastest way to point at something you cannot name, so the shelf is
 * one press away inside the copilot rather than underneath it.
 *
 * The storefront's own pages are untouched and still reachable -- an aisle, a product, the
 * cart, a checkout, an order -- because they are what a link from outside lands on, and
 * because the checkout page is the surface the kernel binds. Nothing about money moved.
 */
"use client";

import { CopilotApp } from "@/features/copilot/copilot-app";

export default function HomePage() {
  return <CopilotApp />;
}
