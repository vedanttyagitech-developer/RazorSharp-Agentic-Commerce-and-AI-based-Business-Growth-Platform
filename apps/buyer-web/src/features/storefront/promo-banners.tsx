/**
 * Three banners across the column, radius 16.
 *
 * Every line of copy here is a claim the rest of the system can back. None of them names
 * a discount, a price or a saving, because this storefront has no offers engine and a
 * banner promising "50% off" would be the one number on the page the kernel never agreed
 * to. What they advertise instead is what is actually true: local catalogue, live prices,
 * and a copilot that proposes without ever holding the authority to pay.
 *
 * The dairy banner used to lead with "Delivery in 8 minutes", which broke that rule in the
 * file that states it. Nothing on this platform is fulfilled -- there is no courier, no
 * dispatch and no delivery estimate in any response the catalogue sends -- so the eyebrow
 * now names the aisle the banner actually opens, which is a claim the link itself keeps.
 *
 * The third banner said "RazorAI fills the cart", directly above its own supporting line
 * saying the copilot only proposes. Both cannot be true, and it was the heading that was
 * false: the agent has no basket write and is not getting one -- `basket.update` is absent
 * from the API's tool table, so a model that emitted it would be refused before any gate
 * even ran. What actually happens is that RazorAI searches, asks which product when several
 * match, prices the addition from the merchant's own catalogue, and the buyer adds it. The
 * heading now says that, and it agrees with the sentence underneath it instead of
 * contradicting it. A banner promising a capability the platform does not have is the same
 * defect as a banner promising a refund that never fires.
 */

/*
 * Plain <img>: the banner artwork is local, and the CSP names no external image host.
 */
/* eslint-disable @next/next/no-img-element */

import Link from "next/link";

interface Banner {
  href: string;
  eyebrow: string;
  heading: string;
  supporting: string;
  cta: string;
  image: string;
  surface: string;
  headingInk: string;
  supportingInk: string;
  pill: string;
}

const BANNERS: readonly Banner[] = [
  {
    href: "/c/dairy",
    eyebrow: "The cold shelf, listed live",
    heading: "Milk, curd and paneer",
    supporting: "Priced by the merchant's live catalogue, not by this page.",
    cta: "Shop dairy",
    image: "/categories/blinkit_dairy.webp",
    surface: "bg-[var(--green)]",
    headingInk: "text-white",
    supportingInk: "text-white/85",
    pill: "bg-white text-[var(--green)]",
  },
  {
    href: "/c/staples",
    eyebrow: "The whole kitchen shelf",
    heading: "Atta, rice and dal",
    supporting: "With the masalas that go beside them, in one aisle.",
    cta: "Shop staples",
    image: "/categories/blinkit_staples.webp",
    surface: "bg-[var(--yellow)]",
    headingInk: "text-[var(--ink)]",
    supportingInk: "text-[var(--ink-2)]",
    pill: "bg-[var(--ink)] text-white",
  },
  {
    href: "/search?q=doodh",
    eyebrow: "Ask in Hindi, Hinglish or English",
    heading: "RazorAI finds it. You add it.",
    supporting: "The copilot proposes. Every rupee still waits for you to approve it.",
    cta: "Try a search",
    image: "/categories/blinkit_packaged.webp",
    surface: "bg-[var(--tint-1)]",
    headingInk: "text-[var(--ink)]",
    supportingInk: "text-[var(--ink-3)]",
    pill: "bg-[var(--green)] text-white",
  },
];

export function PromoBanners() {
  return (
    <section aria-label="Featured aisles">
      <ul className="grid grid-cols-1 gap-4 md:grid-cols-3">
        {BANNERS.map((banner) => (
          <li key={banner.href}>
            <Link
              href={banner.href}
              className={`flex h-[180px] items-stretch overflow-hidden rounded-[var(--r-xl)] ${banner.surface}`}
            >
              <span className="flex flex-1 flex-col justify-center gap-1 py-5 pl-5">
                <span className={`text-[12px] font-semibold ${banner.supportingInk}`}>{banner.eyebrow}</span>
                <span className={`text-[18px] leading-[1.25] font-extrabold ${banner.headingInk}`}>
                  {banner.heading}
                </span>
                <span className={`text-[12px] leading-[1.4] ${banner.supportingInk}`}>{banner.supporting}</span>
                <span
                  className={`mt-2 inline-flex w-fit items-center rounded-full px-3 py-1.5 text-[12px] font-semibold ${banner.pill}`}
                >
                  {banner.cta}
                </span>
              </span>
              <img
                src={banner.image}
                alt=""
                width={140}
                height={180}
                loading="lazy"
                decoding="async"
                className="h-full w-[120px] shrink-0 self-end object-contain object-bottom"
              />
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
