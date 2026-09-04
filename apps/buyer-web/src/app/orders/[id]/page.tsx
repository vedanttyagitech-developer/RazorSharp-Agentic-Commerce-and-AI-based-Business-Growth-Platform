import { OrderView } from "@/features/order/order-view";

/** Transactional page: dynamic, never cached (spec 21.5). */
export const dynamic = "force-dynamic";

export default async function OrderPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <OrderView orderId={decodeURIComponent(id)} />;
}
