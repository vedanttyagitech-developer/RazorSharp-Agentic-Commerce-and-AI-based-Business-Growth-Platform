/**
 * A missing page says so and gets out of the way.
 *
 * It offers search rather than a guess at what was wanted, because guessing a product
 * from a broken address is how a storefront ends up showing something the buyer did not
 * ask for at a price they did not look up.
 */
import Link from "next/link";

export const metadata = { title: "Page not found" };

export default function NotFound() {
  return (
    <div className="column flex flex-col items-center py-24 text-center">
      <p className="text-[28px] font-extrabold leading-none">
        <span className="text-[var(--yellow)]">4</span>
        <span className="text-[var(--green)]">04</span>
      </p>
      <h1 className="mt-6 text-[20px] font-semibold text-[var(--ink)]">
        This page does not exist
      </h1>
      <p className="mt-2 max-w-md text-[14px] leading-relaxed text-[var(--ink-4)]">
        The address you followed does not match anything in this storefront. Nothing has
        gone wrong with your basket or with any order you have placed.
      </p>
      <Link
        href="/"
        className="mt-8 inline-flex h-12 items-center rounded-[var(--r-md)] bg-[var(--green)] px-6 text-[16px] font-semibold text-white transition hover:brightness-95"
      >
        Back to the storefront
      </Link>
    </div>
  );
}
