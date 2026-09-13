/** Surface navigation only. Never cancels a payment or a placed order. */
export function shopNavigation(text: string): 'basket' | 'close-review' | null {
  const s = text
    .toLowerCase()
    .trim()
    .replace(/[.!?।]+$/, '')
    .replace(/^(please|can you|could you)\s+/, '')
    .replace(/\s+please$/, '');
  if (
    /^(show|open)( me)?( my| the)? (basket|cart)$|^(mera |meri )?(cart|basket) (dikhao|kholo)$|^(मेरा |मेरी )?(कार्ट|बास्केट) (दिखाओ|खोलो)$/.test(
      s,
    )
  )
    return 'basket';
  if (
    /^(cancel|close|exit)( the| my)? (checkout|order review|review|checkout review)$|^(checkout|review|order review) (cancel|band)( karo| kar do)?$|^(चेकआउट|रिव्यू) (बंद|कैंसल) करो$/.test(
      s,
    )
  )
    return 'close-review';
  return null;
}

/** Explicit view shortcuts only; an order-specific question must keep its reply visible. */
export function shoppingView(
  text: string,
): 'orders' | 'support' | 'reserve' | null {
  const s = text
    .trim()
    .toLowerCase()
    .replace(/[.!?।]+$/, '');
  if (/^(?:(?:show|open)(?: me)? )?(?:my |your )?orders$/.test(s))
    return 'orders';
  if (/^(?:get help|(?:open )?(?:customer )?support)$/.test(s))
    return 'support';
  if (/^(?:open |show )?reserve pay$/.test(s)) return 'reserve';
  return null;
}
