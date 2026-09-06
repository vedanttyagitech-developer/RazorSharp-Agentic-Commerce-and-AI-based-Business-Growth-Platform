/**
 * The storefront's own furniture, hidden on the route that is not a storefront page.
 *
 * The home route is the copilot: a full-height application with its own header, its own
 * cart column and the shelf inside it. A page footer under that is not merely redundant,
 * it is the reason the whole app scrolled -- the copilot asks for the viewport's height
 * and the footer asked for four hundred pixels more, so the buyer arrived at a screen
 * scrolled halfway down its own composer.
 *
 * This exists as a client wrapper rather than a check inside the footer because the footer
 * is a server component with nothing to gain from becoming a client one. Children passed
 * through a client boundary are still rendered on the server; only the decision to show
 * them happens in the browser.
 */
"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

/** The routes that are an application rather than a document. */
const APP_ROUTES: ReadonlySet<string> = new Set(["/"]);

export function StoreChrome({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  if (APP_ROUTES.has(pathname)) return null;
  return <>{children}</>;
}
