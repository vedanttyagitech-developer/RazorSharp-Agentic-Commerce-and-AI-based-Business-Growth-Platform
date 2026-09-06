/**
 * `/basket`. A server component that renders nothing but the client view.
 *
 * There is no server-side read here on purpose. The basket belongs to a browser session
 * whose bearer token is minted by this app's own route handler, and pre-rendering a
 * total would mean caching a price that the merchant may already have moved past. The
 * quote the buyer sees is fetched live, in the client, every time.
 */
import type { Metadata } from "next";

import { BasketView } from "@/features/basket/basket-view";

export const metadata: Metadata = {
  title: "Your cart",
  description: "The items you have chosen, priced by the merchant.",
};

export default function BasketPage() {
  return (
    <div className="column py-6">
      <BasketView />
    </div>
  );
}
