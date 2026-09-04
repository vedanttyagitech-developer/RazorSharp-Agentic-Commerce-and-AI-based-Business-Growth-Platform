import { ProductDetail } from "@/features/storefront/product-detail";

export const dynamic = "force-dynamic";

export default async function ProductPage({ params }: { params: Promise<{ sku: string }> }) {
  const { sku } = await params;
  return <ProductDetail sku={decodeURIComponent(sku)} />;
}
