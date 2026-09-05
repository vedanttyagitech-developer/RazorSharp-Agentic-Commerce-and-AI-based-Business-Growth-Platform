/**
 * A denial is the platform working, and the card has to read that way.
 *
 * The temptation with a refused capability is to draw it like a crash — red, an alert
 * role, an apology, a retry — and a buyer who is taught to retry learns exactly the wrong
 * thing, because `checkout.approve` is not a tool that is temporarily missing. It is a
 * capability no agent will ever hold. So the tests here check the register as carefully as
 * the content: not an alert, not an error word, and the raw capability and reason key kept
 * on screen so the sentence can be checked against the audit log rather than trusted.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

import type { Denial } from "@/lib/api/types";

import { DenialCard } from "./denial-card";

afterEach(cleanup);

/**
 * `POST /v1/agent/turn {"message":"approve this checkout and pay now"}`, captured from
 * the live API on 2026-09-05. The harness sends a third key, `tool`, which the schema
 * keeps and this component ignores.
 */
const APPROVE_DENIED = {
  capability: "checkout.approve",
  reason_key: "not_on_agent_surface",
  tool: null,
} as unknown as Denial;

describe("it renders as the system working", () => {
  it("says so in words, not only in colour", () => {
    render(<DenialCard denials={[APPROVE_DENIED]} />);
    expect(screen.getByText(/That is the system working, not a fault/)).toBeDefined();
  });

  it("names the refusal in the heading, so the meaning survives a monochrome screen", () => {
    render(<DenialCard denials={[APPROVE_DENIED]} />);
    expect(screen.getByText("RazorAI is not allowed to do that")).toBeDefined();
  });

  it("is not announced as an error", () => {
    render(<DenialCard denials={[APPROVE_DENIED]} />);
    // No `role="alert"`, and the landmark says what happened rather than that something
    // broke. A buyer who reads this as a fault will press the button again.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByLabelText("Refused by the capability gate")).toBeDefined();
  });

  it("uses none of the vocabulary of a failure", () => {
    const { container } = render(<DenialCard denials={[APPROVE_DENIED]} />);
    const text = container.textContent ?? "";
    for (const word of ["error", "failed", "went wrong", "try again", "sorry", "unexpected"]) {
      expect(text.toLowerCase()).not.toContain(word);
    }
  });

  it("explains why the capability does not travel, rather than that it is unavailable", () => {
    render(<DenialCard denials={[APPROVE_DENIED]} />);
    expect(screen.getByText(/You hold this one yourself, and it does not travel/)).toBeDefined();
  });
});

describe("it says what the server said", () => {
  it("keeps the raw capability and reason key on screen beside the English", () => {
    render(<DenialCard denials={[APPROVE_DENIED]} />);
    expect(screen.getByText("checkout.approve · not_on_agent_surface")).toBeDefined();
    expect(screen.getByText("Refused: approve a checkout")).toBeDefined();
  });

  it("distinguishes a capability the session holds from one it never held", () => {
    const missing: Denial = { capability: "refund.request", reason_key: "capability_missing" };
    render(<DenialCard denials={[APPROVE_DENIED, missing]} />);

    expect(screen.getByText(/You hold this one yourself/)).toBeDefined();
    expect(screen.getByText(/This session does not carry that capability/)).toBeDefined();
  });

  it("renders every denial the harness recorded this turn", () => {
    const denials: Denial[] = [
      APPROVE_DENIED,
      { capability: "payment.verify", reason_key: "not_on_agent_surface" },
      { capability: "grant.revoke", reason_key: "capability_missing" },
    ];
    render(<DenialCard denials={denials} />);

    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(within(items[1]).getByText("Refused: confirm a payment")).toBeDefined();
    expect(within(items[2]).getByText("grant.revoke · capability_missing")).toBeDefined();
  });

  it("prints a capability it has no phrase for as itself, readably", () => {
    const unknown: Denial = { capability: "warehouse.dispatch", reason_key: "some_new_reason" };
    render(<DenialCard denials={[unknown]} />);

    expect(screen.getByText("Refused: warehouse dispatch")).toBeDefined();
    expect(screen.getByText("warehouse.dispatch · some_new_reason")).toBeDefined();
    // The fallback sentence claims only what is true of every denial: the gate refused
    // before anything ran. It invents no reason of its own.
    expect(screen.getByText(/refused the call before anything ran/)).toBeDefined();
  });

  it("renders nothing at all when the turn recorded no denials", () => {
    const { container } = render(<DenialCard denials={[]} />);
    expect(container.innerHTML).toBe("");
  });
});
