import type { VoiceItem, VoiceOffer } from './voice/client';
type Row = Record<string, unknown>;
const object = (value: unknown): value is Row =>
  !!value && typeof value === 'object' && !Array.isArray(value);
/** Project only structured tool results; never extract a SKU or price from prose. */
export function projectTurn(structured: unknown): {
  items: VoiceItem[];
  proposal: VoiceOffer | null;
} {
  if (!object(structured)) return { items: [], proposal: null };
  let rows: Row[] =
    structured.kind === 'product' && typeof structured.sku === 'string'
      ? [structured]
      : object(structured.product)
        ? [structured.product]
        : Array.isArray(structured.hits)
          ? structured.hits.filter(object)
          : [];
  const proposal = object(structured.proposal) ? structured.proposal : null;
  const display = proposal && object(proposal.display) ? proposal.display : {};
  const basket =
    proposal?.action === 'basket.update' &&
    typeof proposal.sku === 'string' &&
    Number.isInteger(proposal.delta) &&
    proposal.delta !== 0;
  if (basket) {
    rows = rows.filter((row) => row.sku === proposal.sku);
    if (!rows.length)
      rows = [
        {
          sku: proposal.sku,
          display_name: display.name,
          unit_price: display.unit_price,
          stock_units: display.stock_units,
        },
      ];
  }
  const items = rows
    .filter((row) => typeof row.sku === 'string')
    .slice(0, 5)
    .map((row) => ({
      sku: row.sku as string,
      name: String(row.display_name || row.name || row.sku),
      unit_price: object(row.unit_price)
        ? (row.unit_price as VoiceItem['unit_price'])
        : null,
      stock_units:
        typeof row.stock_units === 'number' ? row.stock_units : undefined,
      available: row.is_available !== false && row.stock_units !== 0,
    }));
  return {
    items,
    proposal: basket
      ? {
          sku: proposal.sku as string,
          name: String(display.name || proposal.sku),
          quantity: proposal.delta as number,
          isProposal: true,
          cartId: proposal.cart_id as string | null,
          absoluteQuantity: proposal.quantity as number | null,
          blockedBy: proposal.blocked_by as string | null,
          binding: proposal.binding as VoiceOffer["binding"],
        }
      : null,
  };
}
