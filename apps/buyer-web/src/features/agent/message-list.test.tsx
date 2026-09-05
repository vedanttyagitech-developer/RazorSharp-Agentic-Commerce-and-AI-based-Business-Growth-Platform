/**
 * The written chat, on the dark scene.
 *
 * This list is what the RazorAI box shows whenever the voice socket is not carrying the
 * conversation -- with the gateway down, that is the whole visit -- so it has to be drawn
 * the way the voice transcript is: the buyer's turns in solid indigo on the right, the
 * assistant's as a hairline panel on the left, and the evidence under a reply (tool log,
 * refusal, proposal) as hairline panels of the same register. One class is pinned per
 * bubble, and the storefront's light tokens are checked for by name, because the bug this
 * file exists to hold off is exactly a white card reappearing on a near-black scene.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import type { Turn } from "@/lib/api/types";
import { ASSISTANT, BUBBLE, BUYER } from "@/features/voice/live-transcript";

import { MessageList, type Message } from "./message-list";

afterEach(cleanup);

/**
 * `POST /v1/agent/turn {"message":"check me out"}` against a basket in context, trimmed to
 * the fields the list reads: one tool call, one refusal, and a `checkout.create` proposal
 * naming the basket. Without a handler the proposal draws the plain handoff card.
 */
const TURN: Turn = {
  reply: "I read your basket. Shall I open a checkout for it?",
  language: "en",
  specialist: "shopping",
  routing_reason: "default_shopping",
  principal_id: "buyer-1",
  tool_calls: [{ name: "basket.read", summary: "2 lines", ok: true }],
  denials: [{ capability: "checkout.approve", reason_key: "not_on_agent_surface" }],
  structured: {
    kind: "proposal",
    proposal: {
      action: "checkout.create",
      basket_id: "01a07202-1ba8-7297-a54d-5116246acf0f",
      executes_on: "trusted_surface",
    },
  },
};

const CONVERSATION: readonly Message[] = [
  { id: "m1", role: "razorai", text: "I am RazorAI.", turn: null },
  { id: "m2", role: "buyer", text: "check me out" },
  { id: "m3", role: "razorai", text: TURN.reply, turn: TURN },
  { id: "m4", role: "problem", text: "The store could not be reached." },
];

/** The storefront's light-surface tokens. None of them belongs on the dark scene. */
const LIGHT = /^(bg-white|bg-red-50|bg-amber-50|bg-blue-50|hover:bg-blue-50)(\/\d+)?$|var\(--(ink|tint|card-line|red|amber|blue|green-add)/;

function lightTokens(root: HTMLElement): string[] {
  const found: string[] = [];
  for (const node of Array.from(root.querySelectorAll<HTMLElement>("*"))) {
    for (const token of Array.from(node.classList)) if (LIGHT.test(token)) found.push(token);
  }
  return found;
}

function bubble(text: string): HTMLElement {
  const node = screen.getByText(text, { exact: false }).closest("p");
  if (!node) throw new Error(`no bubble for ${text}`);
  return node;
}

describe("MessageList draws the voice transcript's bubbles", () => {
  it("puts the buyer's turn on the right in solid indigo, one tight corner on their side", () => {
    render(<MessageList messages={CONVERSATION} pending={false} />);
    const buyer = bubble("check me out");
    for (const cls of ["bg-primary", "text-white", "rounded-br-[4px]", "max-w-[85%]"]) {
      expect(buyer.className).toContain(cls);
    }
    // The same strings the voice transcript draws with, not a copy that can drift.
    expect(buyer.className).toBe(`${BUBBLE} ${BUYER}`);
  });

  it("puts RazorAI's turn on the left as a hairline panel, under a mono label", () => {
    render(<MessageList messages={CONVERSATION} pending={false} />);
    const reply = bubble(TURN.reply);
    for (const cls of ["border-white/10", "bg-white/[0.06]", "text-slate-100", "rounded-bl-[4px]"]) {
      expect(reply.className).toContain(cls);
    }
    expect(reply.className).toBe(`${BUBBLE} ${ASSISTANT}`);
    const label = screen.getByText("RazorAI · Shopping");
    expect(label.className).toContain("font-mono");
    expect(label.className).toContain("text-slate-500");
  });

  it("draws a failed turn as a rose hairline alert, never as an answer", () => {
    render(<MessageList messages={CONVERSATION} pending={false} />);
    const problem = screen.getByRole("alert");
    expect(problem.textContent).toContain("That turn did not go through");
    expect(problem.textContent).toContain("The store could not be reached.");
    for (const cls of ["border-rose-400/40", "bg-rose-500/10", "text-slate-300"]) {
      expect(problem.className).toContain(cls);
    }
  });

  it("breathes three violet dots while a turn is in flight", () => {
    const { container } = render(<MessageList messages={CONVERSATION} pending />);
    expect(screen.getByText("RazorAI is working on your message")).toBeTruthy();
    const dots = container.querySelectorAll(".breathing-dot");
    expect(dots).toHaveLength(3);
    expect(dots[0].className).toContain("bg-[#B08CFF]");
  });
});

describe("the evidence under a reply is drawn in the same register", () => {
  it("gives the tool log, the refusal and the proposal the dark hairline panel", () => {
    render(<MessageList messages={CONVERSATION} pending={false} />);

    const PANEL = ["border-white/10", "bg-white/[0.04]", "text-slate-200"];

    const chips = screen.getByText("What it actually did").parentElement;
    const chip = within(chips!).getByRole("listitem");
    for (const cls of PANEL) expect(chip.classList.contains(cls)).toBe(true);
    expect(chip.textContent).toContain("read the basket");

    const denial = screen.getByLabelText("Refused by the capability gate");
    for (const cls of PANEL) expect(denial.classList.contains(cls)).toBe(true);

    const proposal = screen.getByLabelText("Proposal from RazorAI");
    for (const cls of [...PANEL, "border-l-primary"]) {
      expect(proposal.classList.contains(cls)).toBe(true);
    }
    expect(within(proposal).getByRole("link", { name: /open your basket/i }).className).toContain(
      "rounded-full border border-white/15",
    );
  });

  it("leaves none of the storefront's light tokens anywhere in the list", () => {
    const { container } = render(<MessageList messages={CONVERSATION} pending />);
    expect(lightTokens(container)).toEqual([]);
  });
});

/**
 * The written path draws the shelf too.
 *
 * The same products, from the same adapter, whether the buyer spoke or typed. A card that
 * appeared only on the voice path would make the typed conversation the poorer of two
 * registers of one conversation, which is exactly what this list exists to prevent.
 */
describe("the products a turn found are drawn as cards", () => {
  /** `POST /v1/agent/turn {"message":"doodh"}`: a search page, trimmed to what a card reads. */
  const SEARCH: Turn = {
    ...TURN,
    reply: "Amul Taaza is cheapest today.",
    structured: {
      query: "doodh",
      hits: [
        {
          sku: "AMUL-DAIRY-001",
          display_name: "Amul Taaza Toned Milk 500 ml",
          unit_price: { minor: 2800, currency: "INR", display: "28.00" },
          stock_units: 30,
          is_available: true,
        },
      ],
    },
  };

  const SEARCHED: readonly Message[] = [{ id: "s1", role: "razorai", text: SEARCH.reply, turn: SEARCH }];

  it("draws a card for a search page's hits, with the price the server sent", () => {
    render(<MessageList messages={SEARCHED} pending={false} />);
    expect(screen.getByText("Amul Taaza Toned Milk 500 ml")).toBeDefined();
    expect(screen.getByText("₹28.00")).toBeDefined();
  });

  it("presses the panel's own basket write, with the sku that was pressed", () => {
    const added: string[] = [];
    render(<MessageList messages={SEARCHED} pending={false} onAdd={(sku) => added.push(sku)} />);
    fireEvent.click(screen.getByRole("button", { name: "Add Amul Taaza Toned Milk 500 ml" }));
    expect(added).toEqual(["AMUL-DAIRY-001"]);
  });

  it("draws no shelf for a turn whose payload carries no product", () => {
    // `TURN`'s structured payload is a `checkout.create` proposal: a proposal is not a
    // product, and a card row here would be an invented shelf.
    render(<MessageList messages={CONVERSATION} pending={false} />);
    expect(screen.queryByLabelText("Products in this reply")).toBeNull();
  });
});
