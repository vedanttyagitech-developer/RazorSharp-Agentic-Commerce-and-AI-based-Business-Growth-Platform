# Storefront design specification

Measured from the live Blinkit site on 2026-09-05 with the browser's own computed styles,
not eyeballed from a screenshot. Every number below came out of `getComputedStyle`. The
storefront is a deliberate, pixel-faithful clone: this file is the contract, and a value
that is not here should be derived from one that is rather than invented.

## Palette

| Token | Value | Where |
| --- | --- | --- |
| `--surface` | `#FFFFFF` | page and card background |
| `--header-bg` | `#FFFFFF` | header |
| `--header-line` | `#EEEEEE` | 1px header bottom border |
| `--card-line` | `#E8E8E8` | 0.5px product-card border |
| `--ink` | `#1F1F1F` | product name, price, headings |
| `--ink-2` | `#363636` | secondary headings |
| `--ink-3` | `#666666` | body default |
| `--ink-unit` | `#696969` | pack size under a product name |
| `--ink-4` | `#828282` | "2 options", captions |
| `--ink-5` | `#999999` | disabled, placeholder |
| `--tint-1` | `#F2F2F2` | cart pill, chips |
| `--tint-2` | `#F8F8F8` | search field |
| `--tint-3` | `#FCFCFC` | category tile background |
| `--green` | `#0C831F` | brand green, primary actions |
| `--green-add` | `#318616` | ADD control text and border |
| `--green-add-bg` | `#F7FFF9` | ADD control fill |
| `--blue` | `#256FEF` | discounts, links |
| `--yellow` | `#F8CB46` | promotional banner |

## Type

Family `Okra` with a system fallback stack. Weights in use: 400, 500, 600, 700, 800.
Sizes in use: 9, 12, 13, 14, 16, 18, 20, 28. No other size appears on the site; a
component needing something else is a component drawn wrong.

## Geometry

Radii: 6 (ADD control), 8 (cards, search), 12, 16 (banners), 50% (avatars, icon buttons).
Header height 86px, content column 1280px.

## Header

One row, 86px, white, bottom border 1px `--header-line`, no shadow:

```
[logo] │ Delivery in 8 minutes        [ search ................. ]   Login   [ My Cart ]
       │ Connaught Place, New Delhi ▾
```

- Delivery line: 16px/700 `--ink`; location line 12px/400 `--ink-3` with a caret.
- Search: full-width within the column, `--tint-2` fill, radius 8, magnifier at left,
  rotating placeholder (`Search "paneer"`, `Search "chips"`, `Search "butter"`).
- My Cart: `--tint-1` pill with a trolley icon.

## Product card — 191 × 315

The single most repeated element on the site. Exact:

- Container: white, `0.5px solid #E8E8E8`, radius 8, `box-shadow: 2px 2px 8px rgba(0,0,0,.04)`,
  padding `0 0 12px`.
- Image: 190 × 190, fills the card width, sits flush at the top.
- Delivery badge: `8 MINS`, 9px/700 `--ink`, on a light pill, over the image's lower left.
- Name: 13px/600 `--ink`, clamped to 2 lines.
- Pack size: 12px/500 `--ink-unit`.
- Footer row: price 12px/600 `--ink` on the left; ADD control on the right.
- ADD control: 66 × 33, radius 6, `1px solid #318616`, fill `#F7FFF9`, label 13px/600
  `#318616`. A product with variants shows `N options` beneath it at 9px/600 `--ink-4`.
- In-basket state replaces ADD with a green stepper of the same footprint: `−  qty  +`.

## Category grid

Ten columns on desktop. Each tile is a rounded `--tint-3` card holding a product
photograph, with a two-line 12px centred label beneath it. Tiles wrap to a second row.

## Promotional banners

Three across the content column, radius 16, each a full-bleed illustration with a heading,
a supporting line and a pill-shaped call to action.

## What this project adds

The clone is the shell. The parts that are ours and must not be mistaken for Blinkit:

- **RazorAI**, the buyer copilot, in a right-hand panel. It proposes; it never approves.
- **The trusted surface**: approval, payment, refund and cancellation happen on our own
  screens, visually distinct from anything the agent renders.
- **The refusal moment**: when the kernel refuses a stale approval, the screen holds the
  old total against the new one, every delta, version N struck through and N+1 offered.
  That single screen is the argument of the whole submission.
