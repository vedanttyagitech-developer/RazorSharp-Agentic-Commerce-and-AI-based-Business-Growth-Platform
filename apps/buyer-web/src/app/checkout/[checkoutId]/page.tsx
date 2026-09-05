/**
 * The checkout route.
 *
 * A thin server component over a client journey. It renders nothing about the checkout
 * itself, deliberately: the state of a payment is not cacheable, not prefetchable and
 * not something a page should have baked into its HTML, and the browser's own session
 * cookie is what identifies the buyer to the API. `force-dynamic` and `revalidate = 0`
 * say so to the framework as well as to the reader.
 */
import type { Metadata } from "next";

import { CheckoutJourney } from "@/features/checkout/checkout-journey";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export const metadata: Metadata = {
  title: "Checkout",
  description: "Approve a priced version, then pay for it.",
  robots: { index: false, follow: false },
};

export default async function CheckoutPage({
  params,
}: {
  params: Promise<{ checkoutId: string }>;
}) {
  const { checkoutId } = await params;

  return (
    <main className="column py-6">
      <CheckoutJourney checkoutId={checkoutId} />
    </main>
  );
}
