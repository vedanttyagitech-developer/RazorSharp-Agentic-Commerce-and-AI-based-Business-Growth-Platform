/**
 * The shop's mark.
 *
 * It was "Razor" in yellow beside "Sharp" in green, which was fine over a white storefront
 * and wrong the moment the application became navy: two warm brand colours sitting on a
 * cool one, agreeing with nothing else on the screen. A mark that fights its own product is
 * a mark that gets remembered for the wrong reason.
 *
 * So: a monogram that means the name -- a razor's edge cut through a rounded square -- and
 * the word set in one weight, with the second half carrying the accent the rest of the
 * application already uses for anything that acts. Two colours, both already in the
 * palette, and nothing invented for the logo alone.
 */

import { cx } from "@/components/ui";

function Monogram({ className }: { className?: string }) {
  return (
    <span
      className={cx(
        "relative flex size-8 shrink-0 items-center justify-center overflow-hidden rounded-[10px]",
        className,
      )}
      aria-hidden="true"
      style={{
        backgroundImage:
          "linear-gradient(145deg, var(--rzp-blue) 0%, var(--rzp-blue-strong) 55%, #0d3f8f 100%)",
        boxShadow: "0 1px 0 rgb(255 255 255 / 22%) inset, 0 6px 18px -10px rgb(51 149 255 / 75%)",
      }}
    >
      <svg viewBox="0 0 24 24" className="size-[18px]" fill="none">
        {/* An R whose leg is a straight cut rather than a curve: the razor is the letter. */}
        <path
          d="M8 18V6h5.4a3.3 3.3 0 010 6.6H8"
          stroke="white"
          strokeWidth="2.1"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path d="M12.4 12.6L17 18" stroke="white" strokeWidth="2.1" strokeLinecap="round" />
      </svg>
    </span>
  );
}

export function Wordmark({ subtitle = "Shopping copilot" }: { subtitle?: string }) {
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <Monogram />
      <span className="min-w-0">
        <span className="block truncate text-[15px] font-semibold leading-none tracking-[-0.02em] text-white">
          Razor<span className="text-[var(--rzp-blue)]">Sharp</span>
        </span>
        <span className="mt-1 block truncate text-[10px] font-medium leading-none tracking-[0.06em] text-slate-500">
          {subtitle}
        </span>
      </span>
    </div>
  );
}
