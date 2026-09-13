export type ChangeKind =
  | 'STOCK_ADJUSTMENT'
  | 'STOCK_RECEIPT'
  | 'PRICE_CHANGE'
  | 'LISTING_CHANGE'
  | 'OFFER_START'
  | 'OFFER_END';
export type ProposalInput = {
  kind: ChangeKind;
  target: string;
  value: string;
  reason: string;
  listed: boolean;
  label: string;
  discount: 'percent' | 'flat';
  starts: string;
  ends: string;
};
export function buildMerchantProposal(input: ProposalInput) {
  const { kind, target, value, reason } = input;
  if (!target.trim())
    throw Error('Choose a product or enter an offer identifier.');
  const proposal: Record<string, string | number | boolean> = {};
  if (reason.trim()) proposal.reason = reason.trim();
  if (kind === 'LISTING_CHANGE') proposal.listed = input.listed;
  else if (kind === 'OFFER_END') proposal.offer_id = target.trim();
  else {
    const n = Number(value);
    if (!value.trim() || !Number.isSafeInteger(n) || n < 0)
      throw Error('Enter a non-negative whole number.');
    if (kind === 'OFFER_START') {
      if (!input.label.trim()) throw Error('Give the offer a name.');
      const start = Date.parse(input.starts),
        end = Date.parse(input.ends);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start)
        throw Error('Choose an end time after the start time.');
      if (n <= 0 || (input.discount === 'percent' && n > 10000))
        throw Error(
          'Discount must be positive; percent basis points cannot exceed 10,000.',
        );
      Object.assign(proposal, {
        offer_id: target.trim(),
        label: input.label.trim(),
        [input.discount === 'percent' ? 'percent_bp' : 'flat_minor']: n,
        effective_from_epoch_ms: start,
        effective_to_epoch_ms: end,
      });
    } else proposal[kind === 'PRICE_CHANGE' ? 'unit_price_minor' : 'units'] = n;
  }
  return { kind, target: target.trim(), proposal };
}
