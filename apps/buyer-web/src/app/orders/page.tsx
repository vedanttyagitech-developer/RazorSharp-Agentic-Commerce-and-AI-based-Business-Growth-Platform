/**
 * The orders route. A shell only: the list reads from the API in the browser, because
 * order state moves after a page is served and a server-rendered snapshot of it would go
 * stale in the reader's hands.
 */
import type { Metadata } from "next";

import { OrderList } from "@/features/orders/order-list";

export const metadata: Metadata = {
  title: "Orders",
  description: "Confirmed sales, each one backed by verified provider evidence.",
};

export default function OrdersPage() {
  return <OrderList />;
}
