import { Suspense } from "react";

import { SearchPanel } from "@/features/storefront/search-panel";
import { VoicePanel } from "@/components/voice-panel";
import { Spinner } from "@/components/ui";
import { Wrench, ChevronDown } from "lucide-react";

export default function StorefrontPage() {
  return (
    <div className="space-y-10">
      <Suspense fallback={<Spinner label="Loading product discovery..." />}>
        <SearchPanel />
      </Suspense>

      {/* Developer architecture, proof chain, and voice placeholder kept accessible but unobtrusive */}
      <details className="group rounded-2xl border border-line bg-surface/60 p-4 text-sm text-muted shadow-xs transition">
        <summary className="flex cursor-pointer items-center justify-between font-medium text-foreground hover:text-accent select-none">
          <span className="flex items-center gap-2">
            <Wrench className="h-4 w-4 text-stone-500" aria-hidden="true" />
            <span>Architecture & Evidence Verification Details (Test Mode)</span>
          </span>
          <ChevronDown className="h-4 w-4 text-muted transition-transform duration-200 group-open:rotate-180" aria-hidden="true" />
        </summary>
        <div className="mt-4 space-y-6 border-t border-line/60 pt-4">
          <VoicePanel />
          <section aria-labelledby="how-heading" className="space-y-2 text-xs">
            <h2 id="how-heading" className="text-sm font-semibold text-foreground">
              What this storefront proves
            </h2>
            <ol className="list-decimal space-y-1.5 pl-5">
              <li>Grounded discovery: every result carries its source and catalogue revision; sold-out and delisted are shown as different facts.</li>
              <li>Deterministic quotes: the fee engine prices to the paisa; this page only formats.</li>
              <li>Trusted approval bound to a version and a content hash; a merchant change underneath it is refused with an exact delta and a new version.</li>
              <li>Razorpay Standard Checkout in test mode, admitted exactly once, verified server-side, with a Money Action Proof Chain you can inspect.</li>
            </ol>
          </section>
        </div>
      </details>
    </div>
  );
}
