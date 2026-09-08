/**
 * A quiet footer, and the place where the storefront says plainly what it is.
 *
 * The clone is faithful enough that someone could land on it and believe it sells them
 * groceries, so the last thing on every page is the sentence that says it does not. The
 * disclosure belongs here rather than in a banner because it should be findable without
 * being in the way of the demonstration.
 *
 * The aisle list is imported rather than retyped, so a label here can never drift away
 * from the one the category grid shows for the same slug.
 */
import Link from "next/link";

import { CATEGORIES, categoryLabel } from "@/lib/product-images";

export function Footer() {
  return (
    <footer className="mt-16 border-t border-[var(--header-line)] bg-[var(--tint-3)]">
      <div className="column py-10">
        <h2 className="text-[13px] font-semibold uppercase tracking-wide text-[var(--ink-4)]">
          Shop by category
        </h2>
        <ul className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-3 lg:grid-cols-5">
          {CATEGORIES.map((slug) => (
            <li key={slug}>
              <Link
                href={`/c/${slug}`}
                className="text-[13px] text-[var(--ink-3)] transition hover:text-[var(--green)]"
              >
                {categoryLabel(slug)}
              </Link>
            </li>
          ))}
        </ul>

        <div className="mt-10 border-t border-[var(--header-line)] pt-6">
          <p className="text-[13px] font-semibold text-[var(--ink-2)]">
            Built for the Razorpay AI Buildathon
          </p>
          <p className="mt-2 max-w-2xl text-[12px] leading-relaxed text-[var(--ink-4)]">
            A demonstration storefront, not a shop. Nothing here is for sale, no order is
            fulfilled, and every payment runs against Razorpay in test mode. Prices, stock
            and totals come from a simulated merchant catalogue; the agent proposes and a
            deterministic transaction kernel decides what may be paid for.
          </p>
          {/*
            Linked from here rather than from the header. Speaking is a way of asking, and
            the header carries the controls that commit -- cart and checkout. Keeping the
            two apart is the same distinction the launcher makes with colour: nothing a
            buyer reaches from this line can approve or pay.

            It points at the copilot rather than at a voice page, because there is no
            longer a voice page. `/voice` was a second front end for a microphone the
            copilot already holds -- `useVoiceSession` and the composer's own mic -- and it
            existed because when it was written the assistant was a drawer, where a second
            conversation would have meant two transcripts and two send buttons. The
            copilot is the application now, so the argument that separated them is spent
            and what is left is one surface that talks.
          */}
          <p className="mt-3 text-[12px] text-[var(--ink-4)]">
            <Link
              href="/"
              className="font-semibold text-[var(--blue)] transition hover:underline"
            >
              Talk to RazorAI
            </Link>{" "}
            — ask out loud; approving and paying stay on the store&rsquo;s own pages.
          </p>
        </div>
      </div>
    </footer>
  );
}
