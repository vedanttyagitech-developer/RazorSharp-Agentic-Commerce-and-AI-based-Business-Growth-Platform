/**
 * The header's search field.
 *
 * Two behaviours worth naming. The placeholder cycles through four prompts, one of them
 * transliterated Hindi, because the catalogue is bilingual and the buyer should be able
 * to guess that before typing. And typing navigates on a 250ms debounce rather than on
 * every keystroke, so a five-letter word is one navigation instead of five.
 *
 * While the buyer is already on the results page each debounced navigation replaces the
 * current history entry instead of stacking a new one, so a single back press returns to
 * wherever the search started rather than replaying the word letter by letter.
 */
"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { cx } from "@/components/ui";

const PROMPTS = ['Search "paneer"', 'Search "chips"', 'Search "butter"', 'Search "doodh"'] as const;

const ROTATION_MS = 3000;
const DEBOUNCE_MS = 250;

export function SearchBox({ id = "storefront-search", className }: { id?: string; className?: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const [query, setQuery] = useState("");
  const [promptIndex, setPromptIndex] = useState(0);

  useEffect(() => {
    /*
     * A placeholder that changes under a reader who needs longer to read it is a worse
     * placeholder, so the rotation stops entirely when the operating system asks for
     * reduced motion rather than merely slowing down.
     */
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (media.matches) return;
    const timer = window.setInterval(() => {
      setPromptIndex((index) => (index + 1) % PROMPTS.length);
    }, ROTATION_MS);
    return () => window.clearInterval(timer);
  }, []);

  const onResultsPage = pathname === "/search";

  // Held in a ref so a keystroke can cancel the navigation the previous keystroke queued.
  const debounce = useRef<number | null>(null);
  // Arriving on the results page re-runs the effect; without this the same query would
  // navigate a second time to the address it just reached.
  const lastNavigated = useRef<string | null>(null);

  useEffect(() => {
    const trimmed = query.trim();
    if (trimmed === "") return;
    const href = `/search?q=${encodeURIComponent(trimmed)}`;
    if (lastNavigated.current === href) return;
    if (debounce.current !== null) window.clearTimeout(debounce.current);
    debounce.current = window.setTimeout(() => {
      lastNavigated.current = href;
      if (onResultsPage) router.replace(href);
      else router.push(href);
    }, DEBOUNCE_MS);
    return () => {
      if (debounce.current !== null) window.clearTimeout(debounce.current);
    };
  }, [query, onResultsPage, router]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = query.trim();
    if (trimmed === "") return;
    if (debounce.current !== null) window.clearTimeout(debounce.current);
    const href = `/search?q=${encodeURIComponent(trimmed)}`;
    lastNavigated.current = href;
    router.push(href);
  }

  return (
    <form role="search" onSubmit={submit} className={cx("relative w-full", className)}>
      <label htmlFor={id} className="sr-only">
        Search for products
      </label>
      <svg
        aria-hidden="true"
        viewBox="0 0 20 20"
        className="pointer-events-none absolute left-4 top-1/2 h-[18px] w-[18px] -translate-y-1/2 text-[var(--ink-4)]"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      >
        <circle cx="9" cy="9" r="6" />
        <path d="M13.5 13.5 17.5 17.5" />
      </svg>
      <input
        id={id}
        name="q"
        type="search"
        value={query}
        autoComplete="off"
        onChange={(event) => setQuery(event.target.value)}
        placeholder={PROMPTS[promptIndex]}
        aria-label="Search for products"
        className="h-[48px] w-full rounded-[var(--r-md)] bg-[var(--tint-2)] pl-11 pr-4 text-[14px] text-[var(--ink)] placeholder:text-[var(--ink-5)] focus:bg-white focus:outline-none focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-[var(--green)]"
      />
    </form>
  );
}
