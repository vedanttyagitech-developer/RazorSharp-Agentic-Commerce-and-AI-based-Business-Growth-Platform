/**
 * The Reserve Pay route. A shell only: the demo is a client component holding its own
 * React state, because "granting" the mandate is a state transition and nothing more —
 * there is nothing to render on the server and nothing to fetch.
 *
 * This is a labelled simulator (specification 12.3). It creates no payment and calls no
 * admission route.
 */
import type { Metadata } from "next";

import { ReservePayDemo } from "@/features/reserve-pay/reserve-pay-demo";

export const metadata: Metadata = {
  title: "Reserve Pay (simulated)",
  description:
    "A labelled simulator for the Reserve Pay mandate step, per specification 12.3. No mandate exists and no money can move.",
};

export default function ReservePayPage() {
  return <ReservePayDemo />;
}
