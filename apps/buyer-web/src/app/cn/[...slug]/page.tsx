import { Suspense } from "react";
import { SearchPanel } from "@/features/storefront/search-panel";
import { Spinner } from "@/components/ui";

export default async function BlinkitCategoryPage({
  params,
}: {
  params: Promise<{ slug: string[] }>;
}) {
  const resolvedParams = await params;
  const slugStr = resolvedParams.slug.join("/").toLowerCase();

  let defaultCategory = "beverages";
  let defaultSubId = "bev_soft";

  if (
    slugStr.includes("skin") ||
    slugStr.includes("personal") ||
    slugStr.includes("derma") ||
    slugStr.includes("cetaphil") ||
    slugStr.includes("minimalist")
  ) {
    defaultCategory = "personal_care";
    defaultSubId = "pc_skin";
  } else if (slugStr.includes("dairy") || slugStr.includes("milk") || slugStr.includes("bread")) {
    defaultCategory = "dairy";
    defaultSubId = "dairy_all";
  } else if (slugStr.includes("produce") || slugStr.includes("fruit") || slugStr.includes("vegetable")) {
    defaultCategory = "produce";
    defaultSubId = "produce_all";
  } else if (slugStr.includes("snack") || slugStr.includes("munchies") || slugStr.includes("chip")) {
    defaultCategory = "snacks";
    defaultSubId = "snacks_all";
  } else if (slugStr.includes("staple") || slugStr.includes("atta") || slugStr.includes("rice") || slugStr.includes("dal")) {
    defaultCategory = "staples";
    defaultSubId = "staples_all";
  } else if (slugStr.includes("house") || slugStr.includes("clean")) {
    defaultCategory = "household";
    defaultSubId = "house_all";
  } else if (slugStr.includes("elec") || slugStr.includes("tech")) {
    defaultCategory = "electronics";
    defaultSubId = "elec_all";
  } else if (slugStr.includes("332") || slugStr.includes("cold") || slugStr.includes("beverage")) {
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
