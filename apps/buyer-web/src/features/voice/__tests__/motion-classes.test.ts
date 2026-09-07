/**
 * The RazorAI box's motion is plain CSS in `globals.css`, applied by class name from the
 * components. Nothing at runtime checks that a class a component reaches for is defined,
 * and nothing checks which cascade layer holds it: a rule written outside
 * `@layer components` would outrank the Tailwind utilities on the same element and
 * quietly break a `fixed` or a `hidden`. So this reads the stylesheet as text and holds
 * the motion vocabulary to two things: every class exists, and every one of them lives
 * inside the components layer. It also checks the reduced-motion contract, because a
 * missing entry there is invisible to anyone who has not turned the preference on.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// `import.meta.dirname` rather than a `new URL(..., import.meta.url)`: the bundler
// rewrites that second form into an asset URL before this code ever runs.
const stylesheet = readFileSync(
  join(import.meta.dirname, "..", "..", "..", "app", "globals.css"),
  "utf8",
);

/** The body of the first `@layer components { ... }` block, braces balanced. */
function componentsLayer(source: string): string {
  const open = source.indexOf("@layer components {");
  if (open === -1) throw new Error("globals.css has no @layer components block");
  let depth = 0;
  for (let index = source.indexOf("{", open); index < source.length; index += 1) {
    const char = source[index];
    if (char === "{") depth += 1;
    if (char === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open, index + 1);
    }
  }
  throw new Error("@layer components block never closes");
}

/** Every `prefers-reduced-motion: reduce` block inside `source`, joined. */
function reducedMotion(source: string): string {
  const blocks: string[] = [];
  let from = 0;
  for (;;) {
    const at = source.indexOf("@media (prefers-reduced-motion: reduce)", from);
    if (at === -1) return blocks.join("\n");
    blocks.push(componentsLayer(`@layer components ${source.slice(source.indexOf("{", at))}`));
    from = at + 1;
  }
}

/** The declarations of the first rule whose selector list includes `selector`. */
function rule(source: string, selector: string): string {
  const match = source.match(new RegExp(`(?:^|[\\n,])\\s*${selector.replace(/\./g, "\\.")}\\s*\\{([^}]*)\\}`));
  if (!match) throw new Error(`no rule for ${selector}`);
  return match[1];
}

const layer = componentsLayer(stylesheet);
const outsideLayer = stylesheet.replace(layer, "");

describe("motion classes", () => {
  const classes = [".bubble-enter", ".card-enter", ".cart-line-enter", ".cart-pulse", ".edge-glow-listening"];

  it.each(classes)("%s is defined inside @layer components", (name) => {
    expect(layer).toContain(`${name} {`);
    expect(outsideLayer).not.toContain(`${name} {`);
  });

  it("a bubble rises 8px and fades in over 320ms", () => {
    expect(rule(layer, ".bubble-enter")).toContain("bubble-enter 320ms ease-out both");
    expect(rule(layer, "@keyframes bubble-enter[^}]*from")).toContain("translateY(8px)");
  });

  it("a card rises 10px from 0.98 scale over 380ms on the reference curve", () => {
    expect(rule(layer, ".card-enter")).toContain("card-enter 380ms cubic-bezier(0.2, 0.8, 0.2, 1) both");
    expect(rule(layer, "@keyframes card-enter[^}]*from")).toContain("translateY(10px) scale(0.98)");
  });

  it("a cart line slides in from 8px right over 300ms", () => {
    expect(rule(layer, ".cart-line-enter")).toContain("cart-line-enter 300ms");
    expect(rule(layer, "@keyframes cart-line-enter[^}]*from")).toContain("translateX(8px)");
  });

  it("the arrivals rest on `transform: none`, not a kept translate", () => {
    // A kept transform makes the element the containing block for a fixed descendant.
    for (const name of ["bubble-enter", "card-enter", "cart-line-enter"]) {
      expect(rule(layer, `@keyframes ${name}[^}]*}[^}]*to`)).toContain("transform: none");
    }
  });

  it("the cart total pulses once over 700ms with a cyan glow", () => {
    expect(rule(layer, ".cart-pulse")).toContain("cart-pulse 700ms ease-out 1 both");
    const swell = rule(layer, "@keyframes cart-pulse[^}]*}[^}]*35%");
    expect(swell).toContain("scale(1.12)");
    expect(swell).toContain("rgba(79, 217, 242");
  });

  it("listening is a slow cyan conic ring at lower opacity", () => {
    const listening = rule(layer, ".edge-glow-listening");
    expect(listening).toContain("conic-gradient(from var(--glow-angle), #4fd9f2, #67e8f9, #4fd9f2)");
    expect(listening).toContain("animation-duration: 3.2s");
    expect(listening).toContain("opacity: 0.55");
  });

  it("keeps position off the arrival classes, so a fixed element stays fixed", () => {
    for (const name of [".bubble-enter", ".card-enter", ".cart-line-enter", ".cart-pulse"]) {
      expect(rule(layer, name)).not.toContain("position:");
    }
  });

  it("collapses every animation to no movement under reduced motion", () => {
    const reduced = reducedMotion(layer);
    for (const name of [".bubble-enter", ".card-enter", ".cart-line-enter", ".cart-pulse", ".edge-glow-ring"]) {
      expect(reduced).toContain(name);
    }
    expect(reduced).toContain("animation-name: motion-fade");
    expect(rule(layer, "@keyframes motion-fade[^}]*from")).not.toContain("transform");
    expect(rule(layer, "@keyframes motion-fade[^}]*from")).toContain("opacity");
  });
});
