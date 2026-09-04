import { CheckoutJourney } from "@/features/checkout/checkout-journey";

/** Transactional page: dynamic, never cached (spec 21.5). */
export const dynamic = "force-dynamic";

export default async function CheckoutPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <CheckoutJourney checkoutId={decodeURIComponent(id)} />;
}
