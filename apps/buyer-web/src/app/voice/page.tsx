/**
 * The route where a buyer talks to the shop.
 *
 * A page of its own rather than a section inside the RazorAI drawer, and that is a claim
 * about what this surface is allowed to be, not a layout preference. The drawer is a
 * written conversation with its own composer and its own proposal cards; putting a second
 * conversation inside it would give a buyer two transcripts and two send buttons for one
 * assistant, and the first time those disagreed the buyer would have no way to tell which
 * one the store had heard.
 *
 * Everything that could commit money is somewhere else, and deliberately so. This page
 * holds a microphone, a transcript and a typing box. Approving and paying happen on the
 * store's own pages, against the exact checkout version and content hash the buyer is
 * looking at -- so a spoken "yes" here records no consent, and there is no control on this
 * route that could turn one into consent.
 */
import type { Metadata } from "next";

import { VoicePanel } from "@/features/voice";

export const metadata: Metadata = {
  title: "Talk to RazorAI",
  description:
    "Speak to the storefront. RazorAI answers in writing first and speaks it afterwards; approving and paying stay on the store's own pages.",
};

export default function VoicePage() {
  return (
    <div className="column py-8">
      <header className="mb-5">
        <h1 className="text-[22px] font-bold tracking-[-0.01em] text-[var(--ink)]">
          Talk to RazorAI
        </h1>
        <p className="mt-1.5 max-w-2xl text-[13px] leading-[1.55] text-[var(--ink-4)]">
          Hold the talk button and ask for something the way you would ask a shopkeeper.
          Your words are transcribed as you speak, the reply is written first and spoken
          afterwards, and you can type instead at any time -- including when speech
          recognition is unavailable.
        </p>
      </header>

      <VoicePanel className="h-[68vh] min-h-[480px]" />

      <p className="mt-4 max-w-2xl text-[12px] leading-[1.55] text-[var(--ink-5)]">
        Speech is a way of asking, never a way of agreeing. Nothing said on this page
        approves an order, authorises a payment or cancels one; those happen on the
        store&rsquo;s own pages, where you can see the exact version you are consenting to.
      </p>
    </div>
  );
}
