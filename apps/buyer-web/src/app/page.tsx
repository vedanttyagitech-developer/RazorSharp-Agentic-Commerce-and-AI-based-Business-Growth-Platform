import { SearchPanel } from "@/features/storefront/search-panel";
import { VoicePanel } from "@/components/voice-panel";

export default function StorefrontPage() {
  return (
    <div className="space-y-8">
      <SearchPanel />
      <VoicePanel />
      <section aria-labelledby="how-heading" className="rounded-lg border border-line bg-surface p-4 text-sm">
        <h2 id="how-heading" className="mb-2 text-base font-semibold">What this storefront proves</h2>
        <ol className="list-decimal space-y-1 pl-5">
          <li>Grounded discovery: every result carries its source and catalogue revision; sold-out and delisted are shown as different facts.</li>
          <li>Deterministic quotes: the fee engine prices to the paisa; this page only formats.</li>
          <li>Trusted approval bound to a version and a content hash; a merchant change underneath it is refused with an exact delta and a new version.</li>
          <li>Razorpay Standard Checkout in test mode, admitted exactly once, verified server-side, with a Money Action Proof Chain you can inspect.</li>
        </ol>
      </section>
    </div>
  );
}
