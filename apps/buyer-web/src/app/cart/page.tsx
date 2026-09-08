/**
 * `/cart` — the copilot, standing open on the cart.
 *
 * The route is unchanged and still matters: it is where the header's cart icon goes, where
 * `/checkout` redirects when there is nothing to check out, and what RazorAI's own proposal
 * cards hand off to. What changed is that it no longer answers with a second application.
 * The cart the buyer sees here is the same rail the conversation shows, because there is
 * one cart and there should be one drawing of it.
 */
"use client";

import { CopilotApp } from "@/features/copilot/copilot-app";

export default function CartPage() {
  return <CopilotApp at="cart" />;
}
