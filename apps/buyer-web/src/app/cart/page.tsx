/**
 * `/cart`. A server component that renders the client cart view.
 */
import type { Metadata } from "next";

import { CartView } from "@/features/cart/cart-view";

export const metadata: Metadata = {
  title: "Your cart",
  description: "The items you have chosen, priced by the merchant.",
};

export default function CartPage() {
  return (
    <div className="column py-6">
      <CartView />
    </div>
  );
}
