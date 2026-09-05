import { Suspense } from "react";
import { SearchPanel } from "@/features/storefront/search-panel";
import { Spinner } from "@/components/ui";

export default async function BlinkitCategoryPage({
  params,
}: {
  params: Promise<{ slug: string[] }>;
}) {
  const resolvedParams = await params;
  const slugStr = resolvedParams.slug.join("/");

  // cid/332 is Cold Drinks & Juices, 1102 is Soft Drinks
  let defaultCategory = "beverages";
  let defaultSubId = "bev_soft";

  if (slugStr.includes("332") || slugStr.includes("cold") || slugStr.includes("beverage")) {
    defaultCategory = "beverages";
    defaultSubId = "bev_soft";
  }

  return (
    <div className="space-y-6">
      <Suspense fallback={<Spinner label="Loading Blinkit category..." />}>
        <SearchPanel defaultCategory={defaultCategory} defaultSubCategory={defaultSubId} />
      </Suspense>
    </div>
  );
}
