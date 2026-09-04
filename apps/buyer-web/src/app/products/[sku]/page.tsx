import { ProductDetail } from "@/features/storefront/product-detail";
import { getClient } from "@/lib/api";
import type { Product } from "@/lib/api/types";

export const dynamic = "force-dynamic";

export default async function ProductPage({ params }: { params: Promise<{ sku: string }> }) {
  const { sku } = await params;
  const decodedSku = decodeURIComponent(sku);
  let initialProduct: Product | null = null;
  try {
    initialProduct = await getClient().getProduct(decodedSku);
  } catch {
    // Client-side fallback handles missing or dynamic SKU
  }
  return <ProductDetail sku={decodedSku} initialProduct={initialProduct} />;
}
