// What the merchant copilot is allowed to say a number about.
//
// THE DEFECT THIS EXISTS TO REMOVE
// --------------------------------
// The copilot's replies were five string literals picked by regex, and two of them stated
// figures: "revenue is up 18.4%, with breakfast essentials driving the story" and "your
// sample inventory shows one low-stock product". Both were rendered inches from a panel
// showing the store's real confirmed order value, so a judge asking the copilot got an
// invented number sitting next to a true one, with nothing to tell them apart.
//
// The growth figure was the worse of the two, because the backend refuses to produce it on
// purpose. `GET /v1/merchant/insights` returns its own definition with every response:
//
//     "Confirmed order value before refunds; not net revenue, profit, or
//      campaign-attributed growth. Currencies are never combined."
//
// An uplift percentage is precisely what that sentence says the platform does not measure.
// So this module does not compute one either. It carries what the store actually recorded
// and leaves the rest unsaid -- a copilot that answers "I don't have that" is worth more at
// a demo than one that answers confidently and wrongly.
//
// Every field is nullable and every consumer must handle the null. A fact that has not
// arrived yet is not zero: "no confirmed orders" and "the figure has not loaded" are
// different sentences, and printing the first for the second is how this started.

import { useEffect, useState } from 'react';
import { merchantCall } from '@/components/live-merchant';

/** Stock at or below this is worth a merchant's attention. */
export const LOW_STOCK_UNITS = 10;

export type MerchantFacts = {
  /** Confirmed order value and count over `days`, per currency. Never combined. */
  sales: { currency: string; orders: number; salesMinor: number }[] | null;
  days: number | null;
  /** The backend's own words about what the figure is and is not. */
  salesDefinition: string | null;
  openCases: number | null;
  totalCases: number | null;
  /** The published policy version a change would be proposed against. */
  policyVersion: number | null;
  refundWindowDays: number | null;
  loading: boolean;
};

const EMPTY: MerchantFacts = {
  sales: null,
  days: null,
  salesDefinition: null,
  openCases: null,
  totalCases: null,
  policyVersion: null,
  refundWindowDays: null,
  loading: true,
};

type InsightsOut = {
  days: number;
  totals: { currency: string; orders: number; sales_minor: number }[];
  definition: string;
};
type CasesOut = { cases: { status: string }[] };
type PolicyOut = { version: number; terms?: { REFUND?: { window_days?: number } } };

/**
 * Read the three things the copilot quotes, once, when the workspace opens.
 *
 * Failures are swallowed into nulls rather than surfaced: the panels beside this each
 * report their own errors already, and a second copy of the same message inside a chat
 * bubble tells the merchant nothing new. What matters here is that a failed read leaves
 * the field null, so the copilot says it does not have the figure instead of guessing.
 */
export function useMerchantFacts(): MerchantFacts {
  const [facts, setFacts] = useState<MerchantFacts>(EMPTY);

  useEffect(() => {
    let live = true;
    const read = async <T,>(path: string): Promise<T | null> => {
      try {
        return (await merchantCall(path)) as T;
      } catch {
        return null;
      }
    };

    void (async () => {
      const [insights, cases, policy] = await Promise.all([
        read<InsightsOut>('merchant/insights?days=7'),
        read<CasesOut>('support/cases'),
        read<PolicyOut>('merchant/policy'),
      ]);
      if (!live) return;
      const rows = cases?.cases ?? null;
      setFacts({
        sales:
          insights?.totals.map((t) => ({
            currency: t.currency,
            orders: t.orders,
            salesMinor: t.sales_minor,
          })) ?? null,
        days: insights?.days ?? null,
        salesDefinition: insights?.definition ?? null,
        openCases: rows ? rows.filter((c) => c.status === 'OPEN').length : null,
        totalCases: rows ? rows.length : null,
        policyVersion: policy?.version ?? null,
        refundWindowDays: policy?.terms?.REFUND?.window_days ?? null,
        loading: false,
      });
    })();

    return () => {
      live = false;
    };
  }, []);

  return facts;
}

/** ₹13,78,037.80 from 137803780, in the store's own currency. */
export function formatMinor(minor: number, currency: string): string {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency,
    maximumFractionDigits: 2,
  }).format(minor / 100);
}

/** What a question is about. The answer and the panel it opens must agree on this. */
export type CopilotTopic = 'campaign' | 'stock' | 'support' | 'pricing' | 'sales';

/**
 * Route one question.
 *
 * Kept here, once, because it is read twice: to pick the answer, and to decide which
 * workspace panel to open beside it. Those lived as two copies of the same regexes in two
 * files, which is the shape that drifts -- widen one and the copilot starts answering
 * about stock while the screen behind it turns to campaigns.
 */
export function topicOf(message: string): CopilotTopic {
  if (/campaign/i.test(message)) return 'campaign';
  if (/stock|restock|inventory/i.test(message)) return 'stock';
  if (/case|customer|support|refund/i.test(message)) return 'support';
  if (/offer|pricing|price|discount/i.test(message)) return 'pricing';
  return 'sales';
}

/**
 * The copilot's answer, built from what the store recorded.
 *
 * Still routed by keywords -- this is not the language model, and pretending otherwise
 * would be a second invented thing. What changed is that every figure in the output came
 * out of the backend on this page load, and a figure that did not arrive is said to be
 * missing rather than filled in.
 */
export function copilotAnswer(
  message: string,
  facts: MerchantFacts,
  catalogue: { name: string; stock: number }[],
): string {
  const topic = topicOf(message);

  if (topic === 'campaign')
    return 'I have opened the storefront placement preview. Email campaign design is deferred, so there is no draft or send here.';

  if (topic === 'stock') {
    if (!catalogue.length) return 'The shelf has not loaded yet, so I cannot count stock.';
    const low = catalogue
      .filter((p) => p.stock <= LOW_STOCK_UNITS)
      .sort((a, b) => a.stock - b.stock);
    if (!low.length)
      return `Nothing is at or below ${LOW_STOCK_UNITS} units across the ${catalogue.length} products on your shelf. No restock to propose.`;
    const named = low
      .slice(0, 3)
      .map((p) => `${p.name} (${p.stock})`)
      .join(', ');
    return `${low.length} of ${catalogue.length} products are at or below ${LOW_STOCK_UNITS} units: ${named}${low.length > 3 ? ', and others' : ''}. I can prepare a stock adjustment for you to approve.`;
  }

  if (topic === 'support') {
    if (facts.openCases === null)
      return 'I could not read the support queue, so I will not guess how many cases are waiting.';
    const window =
      facts.refundWindowDays === null
        ? ''
        : ` Your published refund window is ${facts.refundWindowDays} days.`;
    return facts.openCases === 0
      ? `No open cases in your queue right now (${facts.totalCases ?? 0} in total, all handled).${window}`
      : `${facts.openCases} open ${facts.openCases === 1 ? 'case' : 'cases'} of ${facts.totalCases} in your queue. Read the sale terms and the amount actually paid before choosing a resolution.${window}`;
  }

  if (topic === 'pricing') {
    const version =
      facts.policyVersion === null
        ? 'your current published policy'
        : `policy version ${facts.policyVersion}`;
    return `Any offer is proposed against ${version} and takes effect only once you approve it. A checkout already open when the price moves must show the changed total and ask the buyer again — its original sale terms stay bound to it.`;
  }

  if (!facts.sales) return 'I do not have your sales figures yet.';
  if (!facts.sales.length)
    return `No confirmed orders in the last ${facts.days ?? 7} days.`;
  const stated = facts.sales
    .map(
      (row) =>
        `${formatMinor(row.salesMinor, row.currency)} across ${row.orders} confirmed ${row.orders === 1 ? 'order' : 'orders'}`,
    )
    .join('; ');
  // The caveat is the backend's own sentence rather than a paraphrase. It is the reason
  // there is no growth percentage here: the platform does not measure one.
  return `Over the last ${facts.days ?? 7} days: ${stated}. ${facts.salesDefinition ?? ''}`.trim();
}

// ------------------------------------------------------------------- the real copilot

export type CopilotTurn = {
  reply: string;
  /** Tool names the specialist actually called. Empty is a real answer, not a failure. */
  tools: string[];
  /** True when the deterministic fallback answered because the model did not. */
  fallback: boolean;
};

/**
 * One turn of the Merchant Copilot, against the Operations specialist.
 *
 * `copilotAnswer` above is what this replaces, and it stays for one reason: a network is
 * a thing that fails, and a keyword answer built from figures already on the page is a
 * better outage than a blank bubble. It is the fallback now, not the product.
 */
export async function askMerchantCopilot(
  message: string,
  signal?: AbortSignal,
): Promise<CopilotTurn> {
  const answer = await fetch('/api/merchant/merchant/agent/turn', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
    signal,
  });
  if (!answer.ok) throw new Error(`copilot turn failed: ${answer.status}`);
  const body = (await answer.json()) as {
    reply?: string;
    tool_calls?: { name: string }[];
  };
  const reply = (body.reply ?? '').trim();
  if (!reply) throw new Error('copilot returned an empty reply');
  return {
    reply,
    tools: (body.tool_calls ?? []).map((call) => call.name),
    fallback: reply.includes('reasoning layer is unavailable'),
  };
}
