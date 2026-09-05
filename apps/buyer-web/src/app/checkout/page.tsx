/**
 * `/checkout` with no checkout to show.
 *
 * A checkout is always a specific immutable thing, so this route never renders one. If a
 * link carried an identifier in the query string it is moved into the path where it
 * belongs; otherwise the buyer goes back to their basket, because inventing a checkout
 * to fill the page would be inventing an amount somebody could approve.
 */
import { redirect } from "next/navigation";

export const dynamic = "force-dynamic";

type Query = Record<string, string | string[] | undefined>;

/** The first value of a query parameter, ignoring a repeated key. */
function single(value: string | string[] | undefined): string | null {
  if (typeof value === "string") return value.trim() || null;
  if (Array.isArray(value)) return single(value[0]);
  return null;
}

export default async function CheckoutIndexPage({
  searchParams,
}: {
  searchParams: Promise<Query>;
}) {
  const query = await searchParams;
  const checkoutId =
    single(query.checkout_id) ?? single(query.checkoutId) ?? single(query.id);

  redirect(checkoutId ? `/checkout/${encodeURIComponent(checkoutId)}` : "/basket");
}
