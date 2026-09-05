/**
 * The ten aisles, across the content column.
 *
 * The slugs are the API's own vocabulary -- they go straight into
 * `api.products({ category })` -- so a tile links to `/c/<slug>` with no translation
 * table in between and no chance of a label drifting away from the query it stands for.
 */

/*
 * Plain <img>: the category photographs are local files under `public/categories`, served
 * from this origin under a CSP that names no external image host.
 */
/* eslint-disable @next/next/no-img-element */

import Link from "next/link";

import { CATEGORIES, categoryImage, categoryLabel } from "@/lib/product-images";

export function CategoryGrid({ heading = "Shop by category" }: { heading?: string }) {
  return (
    <section aria-labelledby="category-grid-heading">
      <h2 id="category-grid-heading" className="mb-4 text-[20px] font-bold text-[var(--ink)]">
        {heading}
      </h2>
      <ul className="grid grid-cols-4 gap-x-3 gap-y-5 sm:grid-cols-5 md:grid-cols-6 lg:grid-cols-10">
        {CATEGORIES.map((slug) => (
          <li key={slug}>
            <Link
              href={`/c/${slug}`}
              className="group flex flex-col items-center gap-2 rounded-[var(--r-md)]"
            >
              <span className="block w-full overflow-hidden rounded-[var(--r-lg)] bg-[var(--tint-3)] ring-[0.5px] ring-[var(--card-line)]">
                <img
                  src={categoryImage(slug)}
                  alt=""
                  width={120}
                  height={120}
                  loading="lazy"
                  decoding="async"
                  className="aspect-square w-full object-contain transition-transform duration-200 group-hover:scale-[1.05]"
                />
              </span>
              <span className="clamp-2 px-1 text-center text-[12px] leading-[1.3] font-medium text-[var(--ink-2)]">
                {categoryLabel(slug)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
