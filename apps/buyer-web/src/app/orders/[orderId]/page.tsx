/**
 * One order's route. `params` is awaited because it is a promise in this router, and the
 * identifier is handed to a client component that does the read: refunds and payment
 * attempts move while the tab is open, and this screen is the one a reviewer refreshes.
 */
import type { Metadata } from "next";

import { OrderDetail } from "@/features/orders/order-detail";

export const metadata: Metadata = {
  title: "Order",
  description: "The amount, the approved version, the hashes it is bound to, and its refunds.",
};

export default async function OrderPage({
  params,
}: {
  params: Promise<{ orderId: string }>;
}) {
  const { orderId } = await params;
  return <OrderDetail orderId={orderId} />;
}
